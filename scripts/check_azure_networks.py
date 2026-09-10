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
PARENTS = {
    "AzureVirtualNetwork": ("resourcegroup", "AzureResourceGroup"),
    "AzureVirtualNetworkSubnet": ("virtualnetwork", "AzureVirtualNetwork"),
    "AzureNetworkSecurityGroup": ("resourcegroup", "AzureResourceGroup"),
    "AzureNetworkSecurityRule": ("network_security_group", "AzureNetworkSecurityGroup"),
    "AzureRouteTable": ("resourcegroup", "AzureResourceGroup"),
    "AzureRoute": ("route_table", "AzureRouteTable"),
}
# Attribute and single-peer fields to read. Prefix relationships are paginated separately.
FIELDS = {
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


def check(client, branch):
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
    findings = validate(inventory)
    for finding in findings:
        typer.echo(finding, err=True)
    if findings:
        typer.echo(
            f"Azure networks invalid on branch {branch}: {len(findings)} findings.",
            err=True,
        )
        return 1
    count = sum(len(inventory[k]) for k in PARENTS)
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
