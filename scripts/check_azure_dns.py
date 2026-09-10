"""Read-only validation of Azure private DNS intent; no DNS queries or writes."""

import ipaddress
import re
from collections import defaultdict
from typing import Annotated

import typer
from infrahub_sdk import InfrahubClientSync

if __package__:
    from .check_azure_networks import prefix_ids
    from .dns_zone_catalog import selected_name
else:
    from check_azure_networks import prefix_ids
    from dns_zone_catalog import selected_name

app = typer.Typer(help=__doc__, add_completion=False, pretty_exceptions_enable=False)
ZONE = "AzurePrivateDnsZone"
ZONE_LINK = "AzurePrivateDnsZoneLink"
RECORD = "AzurePrivateDnsRecordSet"
RESOLVER = "AzureDnsPrivateResolver"
INBOUND = "AzureDnsInboundEndpoint"
OUTBOUND = "AzureDnsOutboundEndpoint"
RULESET = "AzureDnsForwardingRuleset"
RULE = "AzureDnsForwardingRule"
RULESET_LINK = "AzureDnsForwardingRulesetLink"
DNS_KINDS = (
    ZONE,
    ZONE_LINK,
    RECORD,
    RESOLVER,
    INBOUND,
    OUTBOUND,
    RULESET,
    RULE,
    RULESET_LINK,
)
TAGGABLE = (ZONE, RESOLVER, RULESET)
VNET = "AzureVirtualNetwork"
SUBNET = "AzureVirtualNetworkSubnet"
PREFIX = "BuiltinIPPrefix"
ADDRESS = "BuiltinIPAddress"
RG = "AzureResourceGroup"
REGION = "AzureRegion"
SUBSCRIPTION = "AzureSubscription"
TENANT = "AzureTenant"
DELEGATION = "AzureSubnetDelegation"
NAMESPACE = "IpamNamespace"
# Single-valued references. Optional inbound address is validated separately.
REFERENCES = {
    ZONE: {"resourcegroup": RG},
    ZONE_LINK: {"zone": ZONE, "virtual_network": VNET},
    RECORD: {"zone": ZONE},
    RESOLVER: {"resourcegroup": RG, "location": REGION, "virtual_network": VNET},
    INBOUND: {"resolver": RESOLVER, "subnet": SUBNET},
    OUTBOUND: {"resolver": RESOLVER, "subnet": SUBNET},
    RULESET: {"resourcegroup": RG, "location": REGION},
    RULE: {"ruleset": RULESET},
    RULESET_LINK: {"ruleset": RULESET, "virtual_network": VNET},
    VNET: {"resourcegroup": RG, "location": REGION},
    SUBNET: {"virtualnetwork": VNET},
    PREFIX: {"ip_namespace": NAMESPACE},
    ADDRESS: {"ip_namespace": NAMESPACE},
    RG: {"subscription": SUBSCRIPTION},
    SUBSCRIPTION: {"tenant": TENANT},
    DELEGATION: {"subnet": SUBNET},
    REGION: {},
    TENANT: {},
    NAMESPACE: {},
}
ATTRIBUTES = {k: ["name"] for k in REFERENCES}
ATTRIBUTES.update(
    {
        ZONE: ["name", "zone_selection", "custom_name"],
        ZONE_LINK: ["name", "registration_enabled"],
        RECORD: ["name", "record_type", "ttl", "records"],
        INBOUND: ["name", "allocation_method"],
        RULE: ["name", "domain_name", "enabled", "target_servers"],
        PREFIX: ["prefix"],
        ADDRESS: ["address"],
        DELEGATION: ["name", "service_name"],
    }
)
SCOPES = {k: next(iter(REFERENCES[k])) for k in DNS_KINDS}
NAME_RE = r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9_])?\Z"
RESOLVER_NAME_RE = r"^[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?\Z"
RESERVED_ZONES = {
    "azclient.ms",
    "azure.com",
    "cloudapp.net",
    "core.windows.net",
    "microsoft.com",
    "msidentity.com",
    "trafficmanager.net",
    "windows.net",
}


def dns_name(value, *, relative=False, absolute=False):
    """Azure DNS labels; record owners additionally allow apex and wildcard notation."""
    if not isinstance(value, str) or not value:
        return False
    if relative and value in {"@", "*"}:
        return True
    if absolute:
        if not value.endswith("."):
            return False
        value = value[:-1]
        if not value:
            return True
    if not 1 <= len(value) <= 253:
        return False
    labels = value.split(".")
    if relative and labels[0] == "*":
        labels = labels[1:]
    pattern = r"[A-Za-z0-9_-]+"
    return all(
        1 <= len(label) <= 63 and re.fullmatch(pattern, label) for label in labels
    )


def ip(value, version=None):
    if not isinstance(value, str):
        return None
    try:
        result = ipaddress.ip_address(value)
        if "%" in value or version and result.version != version:
            return None
        return result
    except ValueError:
        return None


def record_errors(n, zone):
    errors = []
    name, kind, records = n.get("name"), n.get("record_type"), n.get("records")
    if not dns_name(name, relative=True):
        errors.append("invalid relative record name")
    elif zone and isinstance(zone.get("name"), str):
        fqdn = zone["name"] if name == "@" else f"{name}.{zone['name']}"
        if len(fqdn) > 253:
            errors.append("record FQDN exceeds 253 characters")
    if type(n.get("ttl")) is not int or not 1 <= n["ttl"] <= 2147483647:
        errors.append("TTL must be an integer from 1 to 2147483647")
    if kind not in {"A", "AAAA", "CNAME", "TXT"}:
        return errors + ["unsupported record type"]
    if not isinstance(records, list) or not 1 <= len(records) <= 20:
        return errors + ["records must be a list of 1–20 records"]
    if kind in {"A", "AAAA"}:
        values = [ip(value, 4 if kind == "A" else 6) for value in records]
        if None in values:
            errors.append(
                f"{kind} records require literal IPv{4 if kind == 'A' else 6} addresses"
            )
        elif len(set(values)) != len(values):
            errors.append("duplicate record values")
    elif kind == "CNAME":
        if (
            len(records) != 1
            or not isinstance(records[0], str)
            or not dns_name(records[0].removesuffix("."))
        ):
            errors.append("CNAME requires one DNS target")
        if name == "@":
            errors.append("CNAME cannot exist at the zone apex")
    else:
        total = 0
        values = []
        for record in records:
            if (
                not isinstance(record, list)
                or not record
                or not all(isinstance(chunk, str) for chunk in record)
            ):
                errors.append("TXT records must each be a nonempty list of text chunks")
                continue
            try:
                lengths = [len(chunk.encode("utf-8")) for chunk in record]
            except UnicodeEncodeError:
                errors.append("invalid TXT Unicode")
                continue
            if any(length > 255 for length in lengths):
                errors.append("TXT chunks must not exceed 255 UTF-8 bytes")
            total += sum(lengths)
            values.append("".join(record))
        if total > 4096:
            errors.append("TXT record set exceeds 4096 UTF-8 bytes")
        if len(set(values)) != len(values):
            errors.append("duplicate TXT record values")
    return errors


def validate(inventory):
    indexes = {
        kind: {n["id"]: n for n in inventory.get(kind, [])} for kind in REFERENCES
    }
    findings = []

    def issue(kind, n, message):
        findings.append(
            f"{kind} {n.get('name', n.get('address', n.get('prefix')))} ({n['id']}): {message}"
        )

    def get(kind, id_):
        return indexes[kind].get(id_, {})

    def context(n):
        sub = get(RG, n.get("resourcegroup")).get("subscription")
        return sub, get(SUBSCRIPTION, sub).get("tenant"), n.get("location")

    def duplicate(seen, key, kind, n, description):
        if key in seen:
            issue(kind, n, f"duplicate {description} with {seen[key]}")
        seen[key] = n["id"]

    # Check every context read; dangling relationships are not silently omitted.
    for kind, refs in REFERENCES.items():
        for n in inventory.get(kind, []):
            for field, target in refs.items():
                if n.get(field) not in indexes[target]:
                    issue(kind, n, f"{field}: missing {target} ({n.get(field)})")
    names = {}
    for kind in DNS_KINDS:
        for n in inventory.get(kind, []):
            name = n.get("name")
            if kind == ZONE:
                try:
                    expected_name = selected_name(
                        n.get("zone_selection"), n.get("custom_name")
                    )
                    if not isinstance(name, str) or name.lower() != expected_name:
                        issue(kind, n, "name does not match the selected zone")
                except ValueError as exc:
                    issue(kind, n, str(exc))
                valid = (
                    dns_name(name)
                    and 2 <= len(name.split(".")) <= 34
                    and name.lower() not in RESERVED_ZONES
                )
            elif kind == RECORD:
                valid = dns_name(name, relative=True)
            else:
                valid = (
                    isinstance(name, str)
                    and 1 <= len(name) <= 80
                    and re.fullmatch(
                        RESOLVER_NAME_RE
                        if kind in {RESOLVER, INBOUND, OUTBOUND, RULESET}
                        else NAME_RE,
                        name,
                    )
                )
            if not valid:
                issue(kind, n, "invalid Azure/DNS name")
            else:
                key = (
                    kind,
                    n.get(SCOPES[kind]),
                    name.lower(),
                    n.get("record_type") if kind == RECORD else None,
                )
                duplicate(names, key, kind, n, "scoped name")
    pairs, registrations, resolver_vnets = {}, {}, {}
    for n in inventory.get(ZONE_LINK, []):
        duplicate(
            pairs,
            (n.get("zone"), n.get("virtual_network")),
            ZONE_LINK,
            n,
            "zone/VNet link",
        )
        if type(n.get("registration_enabled")) is not bool:
            issue(ZONE_LINK, n, "registration_enabled must be Boolean")
        elif n["registration_enabled"]:
            duplicate(
                registrations,
                n.get("virtual_network"),
                ZONE_LINK,
                n,
                "autoregistration VNet",
            )
    owners = defaultdict(list)
    for n in inventory.get(RECORD, []):
        for message in record_errors(n, get(ZONE, n.get("zone"))):
            issue(RECORD, n, message)
        if isinstance(n.get("name"), str):
            owners[(n.get("zone"), n["name"].lower())].append(n)
    for records in owners.values():
        if len(records) > 1 and any(n.get("record_type") == "CNAME" for n in records):
            for n in records:
                issue(RECORD, n, "CNAME conflicts with another record set at this name")
    for n in inventory.get(RESOLVER, []):
        vnet = get(VNET, n.get("virtual_network"))
        duplicate(
            resolver_vnets, n.get("virtual_network"), RESOLVER, n, "resolver VNet"
        )
        if vnet and context(n) != context(vnet):
            issue(
                RESOLVER,
                n,
                "resolver and VNet must share subscription, tenant and region",
            )
    occupied, assigned_ips = {}, {}
    inbound_ips = defaultdict(list)
    for kind in (INBOUND, OUTBOUND):
        for n in inventory.get(kind, []):
            subnet = get(SUBNET, n.get("subnet"))
            resolver = get(RESOLVER, n.get("resolver"))
            duplicate(occupied, n.get("subnet"), kind, n, "endpoint subnet")
            if (
                subnet
                and resolver
                and subnet.get("virtualnetwork") != resolver.get("virtual_network")
            ):
                issue(kind, n, "endpoint subnet must belong to resolver VNet")
            delegations = [
                d
                for d in inventory.get(DELEGATION, [])
                if d.get("subnet") == n.get("subnet")
            ]
            if (
                len(delegations) != 1
                or str(delegations[0].get("service_name")).lower()
                != "microsoft.network/dnsresolvers"
            ):
                issue(
                    kind,
                    n,
                    "endpoint subnet requires exclusive Microsoft.Network/dnsResolvers delegation",
                )
            prefixes = [get(PREFIX, p) for p in subnet.get("address_prefixes", [])]
            prefix, network = (prefixes[0] if len(prefixes) == 1 else {}), None
            try:
                network = ipaddress.ip_network(prefix.get("prefix", ""), strict=True)
            except ValueError:
                pass
            if (
                network is None
                or network.version != 4
                or not 24 <= network.prefixlen <= 28
            ):
                issue(
                    kind, n, "endpoint subnet requires exactly one IPv4 /24–/28 prefix"
                )
                network = None
            if kind != INBOUND:
                continue
            allocation, address_id = n.get("allocation_method"), n.get("ip_address")
            if allocation not in {"Static", "Dynamic"}:
                issue(kind, n, "allocation_method must be Static or Dynamic")
            if address_id is None and allocation == "Dynamic":
                continue
            address = get(ADDRESS, address_id)
            if not address:
                issue(
                    kind,
                    n,
                    f"ip_address: missing {ADDRESS} ({address_id}); static allocation requires an address",
                )
                continue
            try:
                value = ipaddress.ip_interface(address.get("address", "")).ip
            except ValueError:
                issue(kind, n, "invalid IPAM endpoint address")
                continue
            duplicate(
                assigned_ips,
                (address.get("ip_namespace"), value),
                kind,
                n,
                "endpoint IP address",
            )
            if network and (
                value.version != 4
                or value not in network
                or int(value) - int(network.network_address) < 4
                or value == network.broadcast_address
            ):
                issue(
                    kind,
                    n,
                    "endpoint address must be usable in its subnet (first four and last addresses reserved)",
                )
            if prefix and address.get("ip_namespace") != prefix.get("ip_namespace"):
                issue(kind, n, "endpoint address and subnet must share IPAM namespace")
            inbound_ips[str(value)].append(resolver.get("virtual_network"))
    ruleset_pairs = {}
    linked_vnets = defaultdict(set)
    for n in inventory.get(RULESET_LINK, []):
        ruleset, vnet = (
            get(RULESET, n.get("ruleset")),
            get(VNET, n.get("virtual_network")),
        )
        duplicate(
            ruleset_pairs,
            (n.get("ruleset"), n.get("virtual_network")),
            RULESET_LINK,
            n,
            "ruleset/VNet link",
        )
        linked_vnets[n.get("ruleset")].add(n.get("virtual_network"))
        if ruleset and vnet and context(ruleset)[1:] != context(vnet)[1:]:
            issue(
                RULESET_LINK,
                n,
                "ruleset link VNet must share tenant and region; cross-subscription is allowed",
            )
    for n in inventory.get(RULESET, []):
        endpoints = n.get("outbound_endpoints", [])
        if not 1 <= len(endpoints) <= 2 or len(set(endpoints)) != len(endpoints):
            issue(RULESET, n, "ruleset requires one or two distinct outbound endpoints")
        resolvers = set()
        for id_ in endpoints:
            endpoint = get(OUTBOUND, id_)
            if not endpoint:
                issue(RULESET, n, f"outbound_endpoints: missing {OUTBOUND} ({id_})")
                continue
            resolvers.add(endpoint.get("resolver"))
            resolver = get(RESOLVER, endpoint.get("resolver"))
            if resolver and context(n) != context(resolver):
                issue(
                    RULESET,
                    n,
                    "ruleset and outbound resolver must share subscription, tenant and region",
                )
        if len(resolvers) > 1:
            issue(RULESET, n, "outbound endpoints must belong to the same resolver")
    domains = {}
    for n in inventory.get(RULE, []):
        domain = n.get("domain_name")
        if not dns_name(domain, absolute=True):
            issue(
                RULE,
                n,
                "domain_name must be an absolute DNS suffix ending in a dot (or .)",
            )
        else:
            duplicate(
                domains,
                (n.get("ruleset"), domain.lower()),
                RULE,
                n,
                "forwarding suffix",
            )
        if type(n.get("enabled")) is not bool:
            issue(RULE, n, "enabled must be Boolean")
        targets = n.get("target_servers")
        if not isinstance(targets, list) or not 1 <= len(targets) <= 6:
            issue(RULE, n, "target_servers must be a list of one to six targets")
            continue
        servers = set()
        for target in targets:
            if (
                not isinstance(target, dict)
                or not {"ip_address"} <= target.keys()
                or target.keys() - {"ip_address", "port"}
            ):
                issue(RULE, n, "each target requires ip_address and optional port only")
                continue
            address, port = ip(target["ip_address"]), target.get("port", 53)
            if (
                address is None
                or address.is_unspecified
                or address.is_multicast
                or address.is_loopback
                or str(address) == "168.63.129.16"
            ):
                issue(
                    RULE,
                    n,
                    "invalid forwarding target IP address (Azure DNS 168.63.129.16 is prohibited)",
                )
                continue
            if type(port) is not int or not 1 <= port <= 65535:
                issue(RULE, n, "target port must be an integer from 1 to 65535")
                continue
            key = (address, port)
            if key in servers:
                issue(RULE, n, "duplicate forwarding target")
            servers.add(key)
            if (
                n.get("enabled")
                and set(inbound_ips[str(address)]) & linked_vnets[n.get("ruleset")]
            ):
                issue(
                    RULE,
                    n,
                    "forwarding loop: target inbound endpoint VNet is linked to this ruleset",
                )
    return findings


def read_inventory(client, branch):
    inventory = {}
    for kind, attrs in ATTRIBUTES.items():
        peers = list(REFERENCES[kind]) + (["ip_address"] if kind == INBOUND else [])
        inventory[kind] = []
        for n in client.all(
            kind=kind, branch=branch, include=attrs + peers, populate_store=False
        ):
            row = {
                "id": n.id,
                **{a: getattr(n, a).value for a in attrs},
                **{p: getattr(n, p).id for p in peers},
            }
            field = {SUBNET: "address_prefixes", RULESET: "outbound_endpoints"}.get(
                kind
            )
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
            f"Azure DNS invalid on branch {branch}: {len(findings)} findings.", err=True
        )
        return 1
    count = sum(len(inventory.get(k, [])) for k in DNS_KINDS)
    typer.echo(
        f"Azure DNS valid on branch {branch}: {count} DNS objects."
        + (" DNS inventory is empty." if not count else "")
    )
    return 0


@app.command()
def main(branch: Annotated[str, typer.Option(help="Infrahub branch to validate")]):
    try:
        code = check(InfrahubClientSync(), branch)
    except Exception as exc:
        typer.echo(f"Azure DNS read failed: {type(exc).__name__}", err=True)
        code = 1
    raise typer.Exit(code=code)


if __name__ == "__main__":
    app()
