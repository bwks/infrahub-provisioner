"""Preview or create one agreed VNet and its IPAM address space without overwrites."""

import ipaddress
import re
from pathlib import Path
from typing import Annotated

import typer
import yaml
from infrahub_sdk import InfrahubClientSync

if __package__:
    from .seed_ipam import fields, text_field
    from .verify_schema import AZURE_STATUSES
else:
    from seed_ipam import fields, text_field
    from verify_schema import AZURE_STATUSES

app = typer.Typer(help=__doc__, add_completion=False, pretty_exceptions_enable=False)


def load_catalog(path):
    catalog = yaml.safe_load(path.read_text())
    fields(catalog, ["virtual_network"], "Catalog")
    entry = catalog["virtual_network"]
    fields(
        entry,
        [
            "name",
            "tenant",
            "subscription",
            "resource_group",
            "region",
            "status",
            "namespace",
            "address_space",
        ],
        "Virtual network",
    )
    for key, value in entry.items():
        if key != "address_space":
            text_field(value, key)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}[A-Za-z0-9_]", entry["name"]):
        raise ValueError("Invalid Azure VNet name")
    if entry["status"] not in AZURE_STATUSES:
        raise ValueError("Invalid VNet status")
    if not isinstance(entry["address_space"], list) or not entry["address_space"]:
        raise ValueError("address_space must be a nonempty list")
    networks = []
    for cidr in entry["address_space"]:
        text_field(cidr, "prefix")
        network = ipaddress.ip_network(cidr, strict=True)
        if str(network) != cidr:
            raise ValueError(f"Noncanonical prefix: {cidr}")
        if any(network.overlaps(other) for other in networks):
            raise ValueError(f"Duplicate or overlapping address space: {cidr}")
        networks.append(network)
    return entry


def seed(client, branch, entry, apply=False):
    inventory = {
        kind: client.all(kind=kind, branch=branch, populate_store=False)
        for kind in [
            "AzureTenant",
            "AzureSubscription",
            "AzureResourceGroup",
            "AzureRegion",
            "IpamNamespace",
            "IpamPrefix",
            "AzureVirtualNetwork",
        ]
    }

    def one(nodes, label):
        if len(nodes) != 1:
            raise ValueError(
                f"{label}: expected one existing match, found {len(nodes)}"
            )
        return nodes[0]

    def named(kind, name):
        return [
            n for n in inventory[kind] if n.name.value.casefold() == name.casefold()
        ]

    tenant = one(named("AzureTenant", entry["tenant"]), "Tenant")
    subscription = one(
        [
            n
            for n in named("AzureSubscription", entry["subscription"])
            if n.tenant.id == tenant.id
        ],
        "Subscription",
    )
    group = one(
        [
            n
            for n in named("AzureResourceGroup", entry["resource_group"])
            if n.subscription.id == subscription.id
        ],
        "Resource group",
    )
    region = one(named("AzureRegion", entry["region"]), "Region")
    namespace = one(named("IpamNamespace", entry["namespace"]), "Namespace")
    existing = named("AzureVirtualNetwork", entry["name"])
    existing = [n for n in existing if n.resourcegroup.id == group.id]
    if len(existing) > 1:
        raise ValueError("Ambiguous existing VNet")
    vnet = existing[0] if existing else None
    prefixes = {}
    pending = []
    for cidr in entry["address_space"]:
        matches = [
            n
            for n in inventory["IpamPrefix"]
            if n.ip_namespace.id == namespace.id and str(n.prefix.value) == cidr
        ]
        if len(matches) > 1:
            raise ValueError(f"Ambiguous prefix: {cidr}")
        if matches:
            prefix = matches[0]
            if prefix.is_pool.value or prefix.vrf.id is not None:
                raise ValueError(
                    f"Prefix {cidr}: existing pool or VRF assignment conflicts"
                )
            prefixes[cidr] = prefix.id
        else:
            pending.append(cidr)
    if vnet:
        vnet.address_space.fetch()
        if (
            vnet.name.value != entry["name"]
            or vnet.location.id != region.id
            or pending
            or {p.id for p in vnet.address_space.peers} != set(prefixes.values())
        ):
            raise ValueError(
                f"VNet {entry['name']} ({vnet.id}): name, region, or address space differs"
            )
    # Catalog parent containers such as 10/8 are expected. Reject overlaps only
    # with other modeled VNet address spaces in the selected namespace.
    by_id = {n.id: n for n in inventory["IpamPrefix"]}
    wanted = [ipaddress.ip_network(cidr) for cidr in entry["address_space"]]
    for other in inventory["AzureVirtualNetwork"]:
        if vnet and other.id == vnet.id:
            continue
        other.address_space.fetch()
        for peer in other.address_space.peers:
            prefix = by_id.get(peer.id)
            if prefix is None:
                raise ValueError(
                    f"VNet {other.name.value}: address space uses an unsupported prefix type"
                )
            if prefix.ip_namespace.id == namespace.id and any(
                n.overlaps(ipaddress.ip_network(str(prefix.prefix.value)))
                for n in wanted
            ):
                raise ValueError(
                    f"Address space overlaps VNet {other.name.value}: {prefix.prefix.value}"
                )
    missing = len(pending) + int(vnet is None)
    typer.echo(
        f"Preflight: missing={missing}, matched={len(prefixes) + int(vnet is not None)}"
    )
    for cidr in pending:
        typer.echo(f"Would create prefix: {entry['namespace']} {cidr}")
    if vnet is None:
        typer.echo(f"Would create VNet: {entry['name']}")
    if not apply:
        typer.echo("Preview only; created=0. Use --apply to create missing entries.")
        return 0
    created = 0
    try:
        for cidr in pending:
            network = ipaddress.ip_network(cidr)
            prefix = client.create(
                kind="IpamPrefix",
                branch=branch,
                data={
                    "prefix": cidr,
                    "ip_namespace": namespace.id,
                    "status": "reserved",
                    "description": f"Address space for {entry['name']}",
                    "is_pool": False,
                    "member_type": "address"
                    if network.prefixlen == network.max_prefixlen
                    else "prefix",
                },
            )
            prefix.save()
            prefixes[cidr] = prefix.id
            created += 1
        if vnet is None:
            vnet = client.create(
                kind="AzureVirtualNetwork",
                branch=branch,
                data={
                    "name": entry["name"],
                    "resourcegroup": group.id,
                    "location": region.id,
                    "status": entry["status"],
                    "address_space": list(prefixes.values()),
                },
            )
            vnet.save()
            created += 1
    except Exception:
        typer.echo(
            f"Apply interrupted: created={created}. Inspect and rerun; no rollback was attempted.",
            err=True,
        )
        raise
    typer.echo(f"Applied: created={created}")
    return 0


@app.command()
def main(
    branch: Annotated[str, typer.Option(help="Infrahub branch to seed")],
    data: Annotated[Path, typer.Option(help="Single VNet catalog")] = Path(
        "data/azure_hub_vnet.yaml"
    ),
    apply: Annotated[bool, typer.Option(help="Create missing records")] = False,
):
    try:
        entry = load_catalog(data)
        code = seed(InfrahubClientSync(), branch, entry, apply)
    except Exception as exc:
        detail = str(exc) if type(exc) is ValueError else type(exc).__name__
        typer.echo(f"VNet seed failed: {detail}", err=True)
        code = 1
    raise typer.Exit(code=code)


if __name__ == "__main__":
    app()
