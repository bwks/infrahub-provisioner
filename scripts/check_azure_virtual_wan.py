"""Read-only validation of Standard Virtual WAN topology and routing intent."""

import ipaddress
import re
from collections import defaultdict
from typing import Annotated

import typer
from infrahub_sdk import InfrahubClientSync

if __package__:
    from .check_azure_networks import prefix_ids
else:
    from check_azure_networks import prefix_ids

app = typer.Typer(help=__doc__, add_completion=False, pretty_exceptions_enable=False)
WAN = "AzureVirtualWan"
HUB = "AzureVirtualHub"
TABLE = "AzureVirtualHubRouteTable"
CONNECTION = "AzureVirtualHubConnection"
KINDS = (WAN, HUB, TABLE, CONNECTION)
VNET = "AzureVirtualNetwork"
PREFIX = "BuiltinIPPrefix"
RG = "AzureResourceGroup"
REGION = "AzureRegion"
SUB = "AzureSubscription"
TENANT = "AzureTenant"
NAMESPACE = "IpamNamespace"
NAME_RE = r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9_])?\Z"
SCOPES = {WAN: "resourcegroup", HUB: "resourcegroup", TABLE: "hub", CONNECTION: "hub"}
REFERENCES = {
    WAN: {"resourcegroup": RG, "location": REGION},
    HUB: {
        "resourcegroup": RG,
        "location": REGION,
        "virtual_wan": WAN,
        "address_space": PREFIX,
    },
    TABLE: {"hub": HUB},
    CONNECTION: {"hub": HUB, "virtual_network": VNET, "associated_route_table": TABLE},
    VNET: {"resourcegroup": RG, "location": REGION},
    RG: {"subscription": SUB},
    SUB: {"tenant": TENANT},
    TENANT: {},
    REGION: {},
    PREFIX: {"ip_namespace": NAMESPACE},
    NAMESPACE: {},
}
ATTRIBUTES = {
    WAN: ["name", "wan_type"],
    HUB: ["name", "router_capacity", "routing_preference"],
    TABLE: ["name", "labels"],
    CONNECTION: ["name", "propagation_labels", "propagate_to_none"],
    PREFIX: ["prefix"],
}
MANY = {VNET: {"address_space": PREFIX}, CONNECTION: {"propagated_route_tables": TABLE}}


def read_inventory(client, branch):
    inventory = {}
    for kind, peers in REFERENCES.items():
        attrs = ATTRIBUTES.get(kind, ["name"])
        inventory[kind] = []
        for node in client.all(
            kind=kind, branch=branch, include=attrs + list(peers), populate_store=False
        ):
            row = {
                "id": node.id,
                **{a: getattr(node, a).value for a in attrs},
                **{p: getattr(node, p).id for p in peers},
            }
            for field in MANY.get(kind, {}):
                row[field] = prefix_ids(client, branch, kind, node.id, field)
            inventory[kind].append(row)
    return inventory


def validate(inventory):
    indexes = {k: {n["id"]: n for n in inventory.get(k, [])} for k in REFERENCES}
    findings = []
    identities = {}
    tables_by_hub = defaultdict(dict)
    networks_by_wan = defaultdict(list)
    attachments = {}

    def issue(kind, node, message):
        findings.append(
            f"{kind} {node.get('name', node.get('prefix'))} ({node['id']}): {message}"
        )

    def labels(kind, node, field):
        value = node.get(field)
        if not isinstance(value, list) or any(
            not isinstance(v, str) or not v.strip() or v != v.strip() for v in value
        ):
            issue(
                kind,
                node,
                f"{field} must be a JSON list of nonempty label strings without surrounding whitespace",
            )
            return []
        if len(set(value)) != len(value):
            issue(kind, node, f"{field} contains duplicate labels")
        return value

    def subscription(node):
        return indexes[RG].get(node.get("resourcegroup"), {}).get("subscription")

    def network(kind, node, prefix_id, hub=False):
        prefix = indexes[PREFIX].get(prefix_id)
        if not prefix:
            issue(kind, node, f"missing address-space prefix {prefix_id}")
            return None
        try:
            net = ipaddress.ip_network(prefix.get("prefix"), strict=True)
            if str(net) != prefix["prefix"]:
                raise ValueError
        except (TypeError, ValueError):
            issue(kind, node, f"prefix {prefix_id} must be canonical CIDR")
            return None
        if prefix.get("ip_namespace") not in indexes[NAMESPACE]:
            issue(kind, node, f"prefix {prefix_id} has missing IP namespace")
        if hub and (net.version != 4 or net.prefixlen > 24):
            issue(kind, node, f"hub prefix {prefix_id} must be IPv4 /24 or larger")
        return net

    # Validate context references too, as ownership cannot otherwise be established.
    for kind, peers in REFERENCES.items():
        for node in inventory.get(kind, []):
            for field, target in peers.items():
                if node.get(field) not in indexes[target]:
                    issue(kind, node, f"{field}: missing {target} ({node.get(field)})")
            if kind in KINDS:
                name = node.get("name")
                if (
                    not isinstance(name, str)
                    or not 1 <= len(name) <= 80
                    or not re.fullmatch(NAME_RE, name)
                ):
                    issue(
                        kind,
                        node,
                        "invalid resource name (1–80 network-name characters required)",
                    )
                else:
                    identity = (kind, node.get(SCOPES[kind]), name.lower())
                    if identity in identities:
                        issue(
                            kind,
                            node,
                            f"duplicate scoped name with {identities[identity]}",
                        )
                    identities[identity] = node["id"]
            if kind == WAN and node.get("wan_type") != "Standard":
                issue(kind, node, "wan_type must be Standard")
            if kind == TABLE:
                value = labels(kind, node, "labels")
                name = str(node.get("name", "")).lower()
                tables_by_hub[node.get("hub")][name] = node
                if name == "defaultroutetable" and "Default" not in value:
                    issue(kind, node, "defaultRouteTable must have the Default label")
                if name == "noneroutetable" and value:
                    issue(kind, node, "noneRouteTable must have no labels")

    for node in inventory.get(HUB, []):
        capacity = node.get("router_capacity")
        if type(capacity) is not int or not 2 <= capacity <= 50:
            issue(HUB, node, "router_capacity must be an integer from 2 to 50")
        if node.get("routing_preference") not in {
            "ExpressRoute",
            "ASPath",
            "VpnGateway",
        }:
            issue(HUB, node, "unsupported routing_preference")
        wan = indexes[WAN].get(node.get("virtual_wan"))
        if wan and subscription(node) != subscription(wan):
            issue(HUB, node, f"WAN {wan['id']} must be in the same subscription")
        for required in ("defaultroutetable", "noneroutetable"):
            if required not in tables_by_hub[node["id"]]:
                issue(HUB, node, f"missing built-in route table {required}")
        net = network(HUB, node, node.get("address_space"), hub=True)
        if net is not None:
            networks_by_wan[node.get("virtual_wan")].append((HUB, node, net))

    for node in inventory.get(CONNECTION, []):
        hub = indexes[HUB].get(node.get("hub"))
        vnet = indexes[VNET].get(node.get("virtual_network"))
        if vnet:
            if vnet["id"] in attachments:
                issue(
                    CONNECTION,
                    node,
                    f"VNet {vnet['id']} already attached by {attachments[vnet['id']]}",
                )
            attachments[vnet["id"]] = node["id"]
            prefixes = vnet.get("address_space", [])
            if not prefixes:
                issue(CONNECTION, node, f"VNet {vnet['id']} has no address space")
            for pid in prefixes:
                net = network(VNET, vnet, pid)
                if net is not None and hub:
                    networks_by_wan[hub.get("virtual_wan")].append((VNET, vnet, net))
        associated = indexes[TABLE].get(node.get("associated_route_table"))
        if associated:
            if associated.get("hub") != node.get("hub"):
                issue(
                    CONNECTION,
                    node,
                    f"associated table {associated['id']} belongs to another hub",
                )
            if str(associated.get("name")).lower() == "noneroutetable":
                issue(CONNECTION, node, "cannot associate noneRouteTable")
        selected_labels = labels(CONNECTION, node, "propagation_labels")
        targets = node.get("propagated_route_tables", [])
        if len(set(targets)) != len(targets):
            issue(CONNECTION, node, "duplicate propagated route tables")
        for tid in targets:
            table = indexes[TABLE].get(tid)
            if not table:
                issue(CONNECTION, node, f"missing propagated route table {tid}")
            elif table.get("hub") != node.get("hub"):
                issue(
                    CONNECTION,
                    node,
                    f"propagated table {tid} belongs to another hub; use labels across hubs",
                )
            elif str(table.get("name")).lower() == "noneroutetable":
                issue(
                    CONNECTION,
                    node,
                    "use propagate_to_none instead of selecting noneRouteTable",
                )
        none = node.get("propagate_to_none")
        if type(none) is not bool:
            issue(CONNECTION, node, "propagate_to_none must be Boolean")
        elif none and (targets or selected_labels):
            issue(
                CONNECTION,
                node,
                "Propagate to None requires empty propagation tables and labels",
            )
        elif not none and not targets and not selected_labels:
            issue(
                CONNECTION,
                node,
                "select propagation tables/labels or explicitly Propagate to None",
            )
        if hub:
            available = {
                label
                for table in inventory.get(TABLE, [])
                if indexes[HUB].get(table.get("hub"), {}).get("virtual_wan")
                == hub.get("virtual_wan")
                and str(table.get("name")).lower() != "noneroutetable"
                for label in (
                    table.get("labels") if isinstance(table.get("labels"), list) else []
                )
                if isinstance(label, str)
            }
            for label in selected_labels:
                if label not in available:
                    issue(
                        CONNECTION,
                        node,
                        f"propagation label {label!r} matches no table in this WAN",
                    )

    for entries in networks_by_wan.values():
        for i, (kind, node, net) in enumerate(entries):
            for other_kind, other, other_net in entries[:i]:
                if net.version == other_net.version and net.overlaps(other_net):
                    issue(
                        kind,
                        node,
                        f"address space {net} overlaps {other_kind} {other['id']} ({other_net}) in the same WAN",
                    )
    return findings


def check(client, branch):
    inventory = read_inventory(client, branch)
    findings = validate(inventory)
    for finding in findings:
        typer.echo(finding, err=True)
    count = sum(len(inventory[k]) for k in KINDS)
    if findings:
        typer.echo(
            f"Azure Virtual WAN invalid on branch {branch}: {len(findings)} findings.",
            err=True,
        )
        return 1
    typer.echo(
        f"Azure Virtual WAN valid on branch {branch}: {count} WAN/hub/routing objects."
        + (" Virtual WAN inventory is empty." if not count else "")
    )
    return 0


@app.command()
def main(branch: Annotated[str, typer.Option(help="Infrahub branch to validate")]):
    try:
        code = check(InfrahubClientSync(), branch)
    except Exception as exc:
        typer.echo(f"Azure Virtual WAN read failed: {type(exc).__name__}", err=True)
        code = 1
    raise typer.Exit(code=code)


if __name__ == "__main__":
    app()
