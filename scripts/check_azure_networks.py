"""Read-only Azure network validation; no Azure deployment or traffic simulation."""

import ipaddress
import re
from collections import defaultdict
from typing import Annotated

import typer
from infrahub_sdk import InfrahubClientSync

app = typer.Typer(help=__doc__, add_completion=False, pretty_exceptions_enable=False)
NAME_RE = r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9_])?\Z"
SERVICE_TAG_RE = r"[A-Za-z][A-Za-z0-9]*(?:\.[A-Za-z0-9]+)*"
DELEGATION_SERVICE_RE = (
    "^[A-Za-z][A-Za-z0-9.]*/[A-Za-z][A-Za-z0-9]*(?:/[A-Za-z][A-Za-z0-9]*)*\\Z"
)
SERVICE_ENDPOINTS = (
    "Microsoft.Storage",
    "Microsoft.Storage.Global",
    "Microsoft.Sql",
    "Microsoft.AzureCosmosDB",
    "Microsoft.KeyVault",
    "Microsoft.ServiceBus",
    "Microsoft.EventHub",
    "Microsoft.Web",
    "Microsoft.CognitiveServices",
    "Microsoft.ContainerRegistry",
)
PEERING_OPTIONS = (
    "allow_virtual_network_access",
    "allow_forwarded_traffic",
    "allow_gateway_transit",
    "use_remote_gateways",
)
PEERING_KIND = "AzureVirtualNetworkPeering"
DNS_SERVICE = "microsoft.network/dnsresolvers"
PARENTS = {
    "AzureSubnetDelegation": ("subnet", "AzureVirtualNetworkSubnet"),
    "AzureVirtualNetwork": ("resourcegroup", "AzureResourceGroup"),
    "AzureVirtualNetworkSubnet": ("virtualnetwork", "AzureVirtualNetwork"),
    "AzureNetworkSecurityGroup": ("resourcegroup", "AzureResourceGroup"),
    "AzureNetworkSecurityRule": ("network_security_group", "AzureNetworkSecurityGroup"),
    "AzureRouteTable": ("resourcegroup", "AzureResourceGroup"),
    "AzureRoute": ("route_table", "AzureRouteTable"),
}
# Attribute and single-peer fields to read. Prefix relationships are paginated separately.
FIELDS = {
    PEERING_KIND: (
        ["peering_name_a", "peering_name_b"]
        + [f"{side}_{option}" for side in "ab" for option in PEERING_OPTIONS],
        ["virtual_network_a", "virtual_network_b"],
    ),
    "AzureSubnetServiceEndpoint": (["service_name"], ["subnet"]),
    "AzureSubnetDelegation": (["name", "service_name"], ["subnet"]),
    "AzureSubscription": (["name"], ["tenant"]),
    "AzureTenant": (["name"], []),
    "AzureRegion": (["name"], []),
    "IpamNamespace": (["name"], []),
    "BuiltinIPPrefix": (["prefix"], ["ip_namespace"]),
    "AzureResourceGroup": (["name"], ["subscription"]),
    "AzureVirtualNetwork": (["name"], ["resourcegroup", "location"]),
    "AzureVirtualNetworkSubnet": (
        ["name"],
        ["virtualnetwork", "network_security_group", "route_table"],
    ),
    "AzureNetworkSecurityGroup": (["name"], ["resourcegroup", "location"]),
    "AzureRouteTable": (
        ["name", "disable_bgp_route_propagation"],
        ["resourcegroup", "location"],
    ),
    "AzureNetworkSecurityRule": (
        [
            "name",
            "description",
            "priority",
            "direction",
            "access",
            "protocol",
            "source_addresses",
            "destination_addresses",
            "source_ports",
            "destination_ports",
        ],
        ["network_security_group"],
    ),
    "AzureRoute": (
        ["name", "address_prefix", "next_hop_type", "next_hop_ip_address"],
        ["route_table"],
    ),
}


def network(value):
    if not isinstance(value, str):
        raise ValueError("CIDR must be text")
    result = ipaddress.ip_network(value, strict=True)
    if str(result) != value:
        raise ValueError("CIDR must be canonical")
    return result


def address_expression(value, route=False):
    """Validate syntax only; service-tag availability requires Azure discovery."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("address expression is required")
    parts = [p.strip() for p in value.split(",")]
    if route and len(parts) != 1:
        raise ValueError("route requires one CIDR or service tag")
    if len(parts) == 1 and (
        (parts[0] == "*" and not route) or re.fullmatch(SERVICE_TAG_RE, parts[0])
    ):
        return
    for part in parts:
        if "/" in part or route:
            network(part)
        else:
            ipaddress.ip_address(part)


def port_expression(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("port expression is required")
    if value.strip() == "*":
        return
    for part in value.split(","):
        if not re.fullmatch(r"[0-9]+(?:-[0-9]+)?", part.strip()):
            raise ValueError("expected ports or ranges, or * alone")
        ends = [int(n) for n in part.strip().split("-")]
        if any(n > 65535 for n in ends) or ends[0] > ends[-1]:
            raise ValueError("ports must be ordered and within 0–65535")


def validate(inventory):
    findings = []
    indexes = {kind: {n["id"]: n for n in inventory.get(kind, [])} for kind in FIELDS}

    def issue(kind, node, message):
        findings.append(
            f"{kind} {node.get('name', node['id'])} ({node['id']}): {message}"
        )

    def reference(kind, node, field, peer_kind, required=True):
        id_ = node.get(field)
        if id_ is None and not required:
            return None
        peer = indexes[peer_kind].get(id_)
        if peer is None:
            issue(kind, node, f"{field}: missing {peer_kind} ({id_})")
        return peer

    for kind, (field, peer) in {
        **PARENTS,
        "AzureResourceGroup": ("subscription", "AzureSubscription"),
        "AzureSubscription": ("tenant", "AzureTenant"),
    }.items():
        identities = {}
        for n in inventory.get(kind, []):
            reference(kind, n, field, peer)
            if kind not in PARENTS:
                continue
            name = n.get("name")
            low, high = (2, 64) if kind == "AzureVirtualNetwork" else (1, 80)
            if (
                not isinstance(name, str)
                or not low <= len(name) <= high
                or not re.fullmatch(NAME_RE, name)
            ):
                issue(kind, n, "invalid Azure name")
                continue
            identity = (n.get(field), name.lower())
            if identity in identities:
                issue(kind, n, f"duplicate name with {identities[identity]}")
            identities[identity] = n["id"]
    contexts = {}
    for kind in ["AzureVirtualNetwork", "AzureNetworkSecurityGroup", "AzureRouteTable"]:
        for n in inventory.get(kind, []):
            reference(kind, n, "location", "AzureRegion")
            group = indexes["AzureResourceGroup"].get(n.get("resourcegroup"))
            contexts[n["id"]] = (
                group.get("subscription") if group else None,
                n.get("location"),
            )
    prefixes = indexes["BuiltinIPPrefix"]

    def ranges(kind, node, field):
        values = []
        if not node.get(field):
            issue(kind, node, f"{field}: at least one prefix is required")
        for id_ in node.get(field, []):
            p = prefixes.get(id_)
            if p is None:
                issue(kind, node, f"{field}: missing prefix {id_}")
                continue
            namespace = p.get("ip_namespace")
            if namespace not in indexes["IpamNamespace"]:
                issue(kind, node, f"prefix {id_}: missing namespace {namespace}")
                continue
            try:
                values.append((namespace, network(str(p.get("prefix"))), id_))
            except ValueError:
                issue(kind, node, f"prefix {id_}: invalid CIDR {p.get('prefix')}")
        return values

    vnet_ranges = {
        n["id"]: ranges("AzureVirtualNetwork", n, "address_space")
        for n in inventory.get("AzureVirtualNetwork", [])
    }
    findings.extend(validate_peerings(inventory, vnet_ranges))
    siblings = defaultdict(list)
    for n in inventory.get("AzureVirtualNetworkSubnet", []):
        kind = "AzureVirtualNetworkSubnet"
        parent = n.get("virtualnetwork")
        for namespace, subnet, id_ in ranges(kind, n, "address_prefixes"):
            if not any(
                namespace == ns
                and subnet.version == vnet.version
                and subnet.subnet_of(vnet)
                for ns, vnet, _ in vnet_ranges.get(parent, [])
            ):
                issue(
                    kind,
                    n,
                    f"prefix {id_} ({subnet}): outside parent VNet address space or namespace",
                )
            for ns, other, owner in siblings[parent]:
                if (
                    namespace == ns
                    and subnet.version == other.version
                    and subnet.overlaps(other)
                ):
                    issue(
                        kind, n, f"prefix {subnet}: overlaps {other} on subnet {owner}"
                    )
            siblings[parent].append((namespace, subnet, n["id"]))
        for field, peer_kind in [
            ("network_security_group", "AzureNetworkSecurityGroup"),
            ("route_table", "AzureRouteTable"),
        ]:
            peer = reference(kind, n, field, peer_kind, required=False)
            if peer and contexts.get(peer["id"]) != contexts.get(parent):
                issue(
                    kind,
                    n,
                    f"{field} {peer['id']}: subscription or region differs from VNet {parent}",
                )
    priorities = {}
    for n in inventory.get("AzureNetworkSecurityRule", []):
        kind = "AzureNetworkSecurityRule"
        priority = n.get("priority")
        if type(priority) is not int or not 100 <= priority <= 4096:
            issue(kind, n, "priority must be an integer from 100 to 4096")
        else:
            identity = (n.get("network_security_group"), n.get("direction"), priority)
            if identity in priorities:
                issue(
                    kind, n, f"duplicate priority/direction with {priorities[identity]}"
                )
            priorities[identity] = n["id"]
        for field, choices in [
            ("direction", {"inbound", "outbound"}),
            ("access", {"allow", "deny"}),
            ("protocol", {"any", "tcp", "udp", "icmp", "esp", "ah"}),
        ]:
            if n.get(field) not in choices:
                issue(kind, n, f"invalid {field}")
        description = n.get("description")
        if description is not None and (
            not isinstance(description, str) or len(description) > 140
        ):
            issue(kind, n, "description must be text up to 140 characters")
        for side in ["source", "destination"]:
            for suffix, parser in [
                ("addresses", address_expression),
                ("ports", port_expression),
            ]:
                field = f"{side}_{suffix}"
                try:
                    parser(n.get(field))
                except ValueError as exc:
                    issue(kind, n, f"{field}: {exc}")
    for n in inventory.get("AzureRoute", []):
        kind = "AzureRoute"
        try:
            address_expression(n.get("address_prefix"), route=True)
        except ValueError as exc:
            issue(kind, n, f"address_prefix: {exc}")
        hop = n.get("next_hop_type")
        if hop not in {
            "internet",
            "none",
            "virtual_appliance",
            "virtual_network_gateway",
            "vnet_local",
        }:
            issue(kind, n, "invalid next_hop_type")
        address = n.get("next_hop_ip_address")
        if hop == "virtual_appliance":
            try:
                if not isinstance(address, str):
                    raise ValueError("next hop must be text")
                ipaddress.ip_address(address)
            except ValueError:
                issue(kind, n, "Virtual Appliance requires a valid next_hop_ip_address")
        elif address is not None:
            issue(kind, n, "next_hop_ip_address is only allowed for Virtual Appliance")
    services = set()
    by_subnet = defaultdict(list)
    for n in inventory.get("AzureSubnetDelegation", []):
        service = n.get("service_name")
        if not isinstance(service, str) or not re.fullmatch(
            DELEGATION_SERVICE_RE, service
        ):
            issue("AzureSubnetDelegation", n, "invalid delegated service identifier")
            continue
        identity = (n.get("subnet"), service.lower())
        if identity in services:
            issue(
                "AzureSubnetDelegation", n, "duplicate service delegation within subnet"
            )
        services.add(identity)
        by_subnet[n.get("subnet")].append(service.lower())
    for subnet_id, services in by_subnet.items():
        if DNS_SERVICE not in services:
            continue
        subnet = indexes["AzureVirtualNetworkSubnet"].get(subnet_id)
        if subnet is None:
            continue
        values = ranges("AzureVirtualNetworkSubnet", subnet, "address_prefixes")
        if (
            len(services) != 1
            or len(values) != 1
            or any(p.version != 4 or not 24 <= p.prefixlen <= 28 for _, p, _ in values)
        ):
            issue(
                "AzureVirtualNetworkSubnet",
                subnet,
                "DNS resolver requires one IPv4 /24–/28 prefix and exclusive Microsoft.Network/dnsResolvers delegation",
            )
    endpoint_services = {}
    for n in inventory.get("AzureSubnetServiceEndpoint", []):
        kind = "AzureSubnetServiceEndpoint"
        reference(kind, n, "subnet", "AzureVirtualNetworkSubnet")
        service = n.get("service_name")
        if service not in SERVICE_ENDPOINTS:
            issue(kind, n, f"unsupported service endpoint {service!r}")
        if isinstance(service, str):
            key = service.lower().replace(
                "microsoft.storage.global", "microsoft.storage"
            )
            identity = (n.get("subnet"), key)
            if identity in endpoint_services:
                issue(
                    kind,
                    n,
                    f"duplicate service or conflicting Storage endpoints with {endpoint_services[identity]}",
                )
            endpoint_services[identity] = n["id"]
    return findings


def validate_peerings(inventory, vnet_ranges):
    """Validate paired intent, independent of endpoint orientation and IPAM namespace."""
    findings = []
    pairs, names, remote_users = {}, {}, {}
    vnets = {n["id"]: n for n in inventory.get("AzureVirtualNetwork", [])}
    for n in inventory.get(PEERING_KIND, []):
        ends = {side: n.get(f"virtual_network_{side}") for side in "ab"}

        def issue(message):
            findings.append(
                f"{PEERING_KIND} {n['id']} (A={ends['a']}, B={ends['b']}): {message}"
            )

        if ends["a"] == ends["b"]:
            issue("self-peering is not allowed")
        pair = frozenset(ends.values())
        if pair in pairs:
            issue(f"duplicate VNet pair with {pairs[pair]}")
        pairs[pair] = n["id"]
        for side, vnet in ends.items():
            opposite = "b" if side == "a" else "a"
            if vnet not in vnets:
                issue(f"end {side}: missing VNet {vnet}")
            name = n.get(f"peering_name_{side}")
            if (
                not isinstance(name, str)
                or not 1 <= len(name) <= 80
                or not re.fullmatch(NAME_RE, name)
            ):
                issue(f"end {side}: invalid Azure peering name {name!r}")
            else:
                identity = (vnet, name.lower())
                if identity in names:
                    issue(f"end {side}: duplicate peering name with {names[identity]}")
                names[identity] = n["id"]
            for option in PEERING_OPTIONS:
                if type(n.get(f"{side}_{option}")) is not bool:
                    issue(f"{side}_{option} must be Boolean")
            if n.get(f"{side}_use_remote_gateways") is True:
                if n.get(f"{opposite}_allow_gateway_transit") is not True:
                    issue(
                        f"end {side}: remote gateway use requires gateway transit on end {opposite}"
                    )
                if vnet in remote_users:
                    issue(
                        f"VNet {vnet} uses remote gateways through multiple connections including {remote_users[vnet]}"
                    )
                remote_users[vnet] = n["id"]
        if (
            n.get("a_use_remote_gateways") is True
            and n.get("b_use_remote_gateways") is True
        ):
            issue("both ends cannot use remote gateways")
        for _, a, _ in vnet_ranges.get(ends["a"], []):
            for _, b, _ in vnet_ranges.get(ends["b"], []):
                if a.version == b.version and a.overlaps(b):
                    issue(f"overlapping VNet address spaces: {a} and {b}")
    return findings


def prefix_ids(client, branch, kind, id_, field):
    """Page nested prefix connections too: SDK all() only pages top-level nodes."""
    result = []
    while True:
        query = f"""query($id: ID!, $offset: Int!) {{ {kind}(ids: [$id]) {{ edges {{ node {{
            {field}(offset: $offset, limit: 100) {{ count edges {{ node {{ id }} }} }}
        }} }} }} }}"""
        response = client.execute_graphql(
            query=query,
            variables={"id": id_, "offset": len(result)},
            branch_name=branch,
        )
        nodes = response[kind]["edges"]
        if len(nodes) != 1:
            raise ValueError("Network object disappeared during read")
        connection = nodes[0]["node"][field]
        page = [p["node"]["id"] for p in connection["edges"]]
        if set(page).intersection(result) or len(set(page)) != len(page):
            raise ValueError("Prefix connection changed during pagination")
        result.extend(page)
        if len(result) == connection["count"]:
            return result
        if not page or len(result) > connection["count"]:
            raise ValueError("Incomplete prefix connection read")


def read_inventory(client, branch):
    inventory = {}
    for kind, (attrs, peers) in FIELDS.items():
        inventory[kind] = []
        for n in client.all(
            kind=kind, branch=branch, include=attrs + peers, populate_store=False
        ):
            row = {
                "id": n.id,
                **{a: getattr(n, a).value for a in attrs},
                **{p: getattr(n, p).id for p in peers},
            }
            field = {
                "AzureVirtualNetwork": "address_space",
                "AzureVirtualNetworkSubnet": "address_prefixes",
            }.get(kind)
            if field:
                row[field] = prefix_ids(client, branch, kind, n.id, field)
            inventory[kind].append(row)
    return inventory


def check(client, branch):
    inventory = read_inventory(client, branch)
    findings = validate(inventory)
    for finding in findings:
        typer.echo(finding, err=True)
    if findings:
        typer.echo(
            f"Azure networks invalid on branch {branch}: {len(findings)} findings.",
            err=True,
        )
        return 1
    count = sum(
        len(inventory[k])
        for k in (*PARENTS, "AzureSubnetServiceEndpoint", PEERING_KIND)
    )
    typer.echo(
        f"Azure networks valid on branch {branch}: {count} network objects."
        + (" Network inventory is empty." if not count else "")
    )
    return 0


@app.command()
def main(branch: Annotated[str, typer.Option(help="Infrahub branch to validate")]):
    try:
        code = check(InfrahubClientSync(), branch)
    except Exception as exc:
        typer.echo(f"Azure network read failed: {type(exc).__name__}", err=True)
        code = 1
    raise typer.Exit(code=code)


if __name__ == "__main__":
    app()
