"""Read-only validation of standalone Standard Firewall Policies and rule intent."""

import ipaddress
import re
from collections import defaultdict
from typing import Annotated

import typer
from infrahub_sdk import InfrahubClientSync

if __package__:
    from .check_azure_networks import NAME_RE, SERVICE_TAG_RE, prefix_ids
else:
    from check_azure_networks import NAME_RE, SERVICE_TAG_RE, prefix_ids

app = typer.Typer(help=__doc__, add_completion=False, pretty_exceptions_enable=False)
POLICY = "AzureFirewallPolicy"
GROUP = "AzureFirewallRuleCollectionGroup"
COLLECTION = "AzureFirewallRuleCollection"
NETWORK = "AzureFirewallNetworkRule"
APPLICATION = "AzureFirewallApplicationRule"
NAT = "AzureFirewallNatRule"
KINDS = (POLICY, GROUP, COLLECTION, NETWORK, APPLICATION, NAT)
RULES = {NETWORK: "Network", APPLICATION: "Application", NAT: "DNAT"}
SCOPES = {
    POLICY: "resourcegroup",
    GROUP: "policy",
    COLLECTION: "collection_group",
    **dict.fromkeys(RULES, "collection"),
}
REFERENCES = {
    POLICY: {"resourcegroup": "AzureResourceGroup", "location": "AzureRegion"},
    GROUP: {"policy": POLICY},
    COLLECTION: {"collection_group": GROUP},
    **{k: {"collection": COLLECTION} for k in RULES},
    "AzureResourceGroup": {"subscription": "AzureSubscription"},
    "AzureSubscription": {"tenant": "AzureTenant"},
    "AzureTenant": {},
    "AzureRegion": {},
}
ATTRIBUTES = {
    POLICY: [
        "name",
        "sku",
        "threat_intelligence_mode",
        "dns_proxy_enabled",
        "dns_servers",
    ],
    GROUP: ["name", "priority"],
    COLLECTION: ["name", "priority", "category", "action"],
    NETWORK: [
        "name",
        "position",
        "source_addresses",
        "destination_addresses",
        "destination_fqdns",
        "protocols",
        "destination_ports",
    ],
    APPLICATION: ["name", "position", "source_addresses", "target_fqdns", "protocols"],
    NAT: [
        "name",
        "position",
        "source_addresses",
        "destination_addresses",
        "destination_ports",
        "protocols",
        "translated_address",
        "translated_fqdn",
        "translated_port",
    ],
}
MANY = {
    POLICY: {"collection_groups": GROUP},
    GROUP: {"collections": COLLECTION},
    COLLECTION: {
        "network_rules": NETWORK,
        "application_rules": APPLICATION,
        "nat_rules": NAT,
    },
}
CHOICES = {
    (POLICY, "sku"): ("Standard",),
    (POLICY, "threat_intelligence_mode"): ("Off", "Alert", "Deny"),
    (COLLECTION, "category"): ("Network", "Application", "DNAT"),
    (COLLECTION, "action"): ("Allow", "Deny", "DNAT"),
}
DEFAULTS = {
    (POLICY, "sku"): "Standard",
    (POLICY, "threat_intelligence_mode"): "Alert",
    (POLICY, "dns_proxy_enabled"): False,
}


def split_list(value, optional=False):
    if optional and (value is None or value == ""):
        return []
    if not isinstance(value, str) or not value.strip():
        raise ValueError("a nonempty comma-separated list is required")
    items = [s.strip() for s in value.split(",")]
    if not all(items) or len({s.lower() for s in items}) != len(items):
        raise ValueError("empty or duplicate list entries")
    if "*" in items and len(items) != 1:
        raise ValueError("* must stand alone")
    return items


def ipv4(value):
    address = ipaddress.IPv4Address(value)
    if str(address) != value:
        raise ValueError("IPv4 address must be canonical")
    return address


def addresses(value, *, optional=False, tags=False, host_only=False, wildcard=True):
    for item in split_list(value, optional):
        if wildcard and item == "*":
            continue
        try:
            ipv4(item)
            continue
        except ValueError:
            pass
        if not host_only:
            if "/" in item:
                net = ipaddress.IPv4Network(item, strict=True)
                if str(net) != item:
                    raise ValueError("CIDR must be canonical")
                continue
            if "-" in item:
                ends = item.split("-")
                if len(ends) != 2 or ipv4(ends[0]) > ipv4(ends[1]):
                    raise ValueError("invalid or reversed IPv4 range")
                continue
            if tags and re.fullmatch(SERVICE_TAG_RE, item):
                continue
        raise ValueError(f"invalid IPv4 address expression: {item!r}")


def fqdn(value, wildcard=False):
    if not isinstance(value, str) or not value or len(value) > 253:
        raise ValueError("invalid FQDN length")
    if wildcard and value == "*":
        return
    if wildcard and value.startswith("*"):
        value = value[1:].removeprefix(".")
    if "*" in value or "." not in value or value.endswith("."):
        raise ValueError("expected a host FQDN, without a URL or trailing dot")
    if re.fullmatch(r"[0-9.]+", value):
        raise ValueError("an IP address is not an FQDN")
    for label in value.split("."):
        if not 1 <= len(label) <= 63 or not re.fullmatch(
            r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label
        ):
            raise ValueError("invalid FQDN label")


def ports(value, maximum=65535):
    for item in split_list(value):
        if item == "*":
            continue
        if not re.fullmatch(r"[0-9]+(?:-[0-9]+)?", item):
            raise ValueError("expected ports, ranges, or * alone")
        ends = [int(s) for s in item.split("-")]
        if not 1 <= ends[0] <= ends[-1] <= maximum:
            raise ValueError(f"ports must be ordered and within 1–{maximum}")


def protocols(value, nat=False):
    items = split_list(value)
    allowed = {"TCP", "UDP"} if nat else {"TCP", "UDP", "ICMP", "Any"}
    if not set(items) <= allowed or ("Any" in items and len(items) != 1):
        raise ValueError("unsupported protocols, or Any combined with another protocol")
    return items


def application_protocols(value):
    if not isinstance(value, list) or not value:
        raise ValueError("expected a nonempty JSON list of protocol_type/port objects")
    seen = set()
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"protocol_type", "port"}:
            raise ValueError("each protocol must have exactly protocol_type and port")
        protocol, port = entry["protocol_type"], entry["port"]
        if (
            protocol not in ("Http", "Https")
            or type(port) is not int
            or not 1 <= port <= 64000
        ):
            raise ValueError(
                "Http/Https protocols require an integer port from 1 to 64000"
            )
        if (protocol, port) in seen:
            raise ValueError("duplicate protocol/port pair")
        seen.add((protocol, port))


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
    findings, identities, ordering = [], {}, {}
    children = defaultdict(list)

    def issue(kind, node, message):
        findings.append(f"{kind} {node.get('name')} ({node['id']}): {message}")

    def check_value(kind, node, field, fn, **kwargs):
        try:
            return fn(node.get(field), **kwargs)
        except (TypeError, ValueError) as exc:
            issue(kind, node, f"{field}: {exc}")
            return None

    for kind, peers in REFERENCES.items():
        for node in inventory.get(kind, []):
            for field, target in peers.items():
                if node.get(field) not in indexes[target]:
                    issue(kind, node, f"{field}: missing {target} ({node.get(field)})")
            if kind not in KINDS:
                continue
            parent = node.get(SCOPES[kind])
            scope = "rule" if kind in RULES else kind
            name = node.get("name")
            if (
                not isinstance(name, str)
                or not 1 <= len(name) <= 80
                or not re.fullmatch(NAME_RE, name)
            ):
                issue(
                    kind, node, "invalid name (1–80 network-name characters required)"
                )
            else:
                identity = (scope, parent, name.lower())
                if identity in identities:
                    issue(
                        kind, node, f"duplicate scoped name with {identities[identity]}"
                    )
                identities[identity] = node["id"]
            if kind != POLICY:
                field = "position" if kind in RULES else "priority"
                number = node.get(field)
                low, high = (1, None) if kind in RULES else (100, 65000)
                if (
                    type(number) is not int
                    or number < low
                    or (high is not None and number > high)
                ):
                    issue(
                        kind,
                        node,
                        f"{field} must be an integer in {low}–{high or 'unbounded'}",
                    )
                else:
                    identity = (scope, parent, number)
                    if identity in ordering:
                        issue(
                            kind, node, f"duplicate {field} with {ordering[identity]}"
                        )
                    ordering[identity] = node["id"]
            for (choice_kind, field), choices in CHOICES.items():
                if choice_kind == kind and node.get(field) not in choices:
                    issue(kind, node, f"{field} must be one of {choices}")
            if kind == POLICY:
                if type(node.get("dns_proxy_enabled")) is not bool:
                    issue(kind, node, "dns_proxy_enabled must be Boolean")
                check_value(
                    kind,
                    node,
                    "dns_servers",
                    addresses,
                    optional=True,
                    host_only=True,
                    wildcard=False,
                )
            elif kind == COLLECTION:
                if node.get("category") in ("Network", "Application") and node.get(
                    "action"
                ) not in ("Allow", "Deny"):
                    issue(
                        kind,
                        node,
                        "network/application collection action must be Allow or Deny",
                    )
                if node.get("category") == "DNAT" and node.get("action") != "DNAT":
                    issue(kind, node, "DNAT collection action must be DNAT")
            elif kind in RULES:
                children[parent].append(node["id"])
                collection = indexes[COLLECTION].get(parent, {})
                if collection and collection.get("category") != RULES[kind]:
                    issue(
                        kind, node, f"rule type must match collection {parent} category"
                    )
                check_value(kind, node, "source_addresses", addresses)
                if kind == APPLICATION:
                    check_value(kind, node, "protocols", application_protocols)
                    values = check_value(kind, node, "target_fqdns", split_list)
                    if values:
                        for value in values:
                            try:
                                fqdn(value, wildcard=True)
                            except ValueError as exc:
                                issue(kind, node, f"target_fqdns {value!r}: {exc}")
                    continue
                proto = check_value(kind, node, "protocols", protocols, nat=kind == NAT)
                check_value(
                    kind,
                    node,
                    "destination_ports",
                    ports,
                    maximum=63999 if kind == NAT else 65535,
                )
                if (
                    proto
                    and "ICMP" in proto
                    and str(node.get("destination_ports", "")).strip() != "*"
                ):
                    issue(kind, node, "ICMP requires wildcard destination_ports")
                check_value(
                    kind,
                    node,
                    "destination_addresses",
                    addresses,
                    optional=kind == NETWORK,
                    tags=kind == NETWORK,
                    host_only=kind == NAT,
                )
                if kind == NETWORK:
                    values = check_value(
                        kind, node, "destination_fqdns", split_list, optional=True
                    )
                    if not node.get("destination_addresses") and not values:
                        issue(kind, node, "destination addresses or FQDNs are required")
                    if values:
                        group = indexes[GROUP].get(
                            collection.get("collection_group"), {}
                        )
                        policy = indexes[POLICY].get(group.get("policy"), {})
                        if policy.get("dns_proxy_enabled") is not True:
                            issue(
                                kind,
                                node,
                                "network FQDNs require policy DNS proxy enabled",
                            )
                        if proto and not set(proto) <= {"TCP", "UDP"}:
                            issue(
                                kind,
                                node,
                                "network FQDNs support TCP/UDP protocols only",
                            )
                        for value in values:
                            try:
                                fqdn(value)
                            except ValueError as exc:
                                issue(kind, node, f"destination_fqdns {value!r}: {exc}")
                else:
                    address, host = (
                        node.get("translated_address"),
                        node.get("translated_fqdn"),
                    )
                    if bool(address) == bool(host):
                        issue(
                            kind,
                            node,
                            "exactly one translated_address or translated_fqdn is required",
                        )
                    if address:
                        check_value(kind, node, "translated_address", ipv4)
                    if host:
                        check_value(kind, node, "translated_fqdn", fqdn)
                    port = node.get("translated_port")
                    if type(port) is not int or not 1 <= port <= 63999:
                        issue(
                            kind,
                            node,
                            "translated_port must be an integer from 1 to 63999",
                        )

    for node in inventory.get(COLLECTION, []):
        if not children[node["id"]]:
            issue(
                COLLECTION,
                node,
                "collection must contain at least one correctly typed rule",
            )
    # Cross-check paged reverse relationships against all child objects. This
    # catches incomplete/inconsistent reads, including concurrent ownership edits.
    for kind, fields in MANY.items():
        for node in inventory.get(kind, []):
            for field, target in fields.items():
                expected = {
                    n["id"]
                    for n in inventory.get(target, [])
                    if n.get(SCOPES[target]) == node["id"]
                }
                actual = node.get(field, [])
                if len(actual) != len(set(actual)) or set(actual) != expected:
                    issue(
                        kind,
                        node,
                        f"{field}: inconsistent child relationships; expected {sorted(expected)}, read {actual}",
                    )
    return findings


def check(client, branch):
    inventory = read_inventory(client, branch)
    findings = validate(inventory)
    for finding in findings:
        typer.echo(finding, err=True)
    if findings:
        typer.echo(
            f"Azure Firewall Policy invalid on branch {branch}: {len(findings)} findings.",
            err=True,
        )
        return 1
    count = sum(len(inventory[k]) for k in KINDS)
    typer.echo(
        f"Azure Firewall Policy valid on branch {branch}: {count} policy/rule objects."
        + (" Firewall Policy inventory is empty." if not count else "")
    )
    return 0


@app.command()
def main(branch: Annotated[str, typer.Option(help="Infrahub branch to validate")]):
    try:
        code = check(InfrahubClientSync(), branch)
    except Exception as exc:
        typer.echo(f"Azure Firewall Policy read failed: {type(exc).__name__}", err=True)
        code = 1
    raise typer.Exit(code=code)


if __name__ == "__main__":
    app()
