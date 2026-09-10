"""Preview/create agreed subnets, prefixes and delegations; preserve operational edits."""

from copy import deepcopy
from pathlib import Path
import re
from typing import Annotated

import typer
import yaml
from infrahub_sdk import InfrahubClientSync

if __package__:
    from .check_azure_networks import (
        DELEGATION_SERVICE_RE,
        NAME_RE,
        network,
        read_inventory,
        validate,
    )
    from .seed_ipam import fields, text_field
    from .verify_schema import AZURE_STATUSES
else:
    from check_azure_networks import (
        DELEGATION_SERVICE_RE,
        NAME_RE,
        network,
        read_inventory,
        validate,
    )
    from seed_ipam import fields, text_field
    from verify_schema import AZURE_STATUSES

app = typer.Typer(help=__doc__, add_completion=False, pretty_exceptions_enable=False)


def load_catalog(path):
    data = yaml.safe_load(path.read_text())
    fields(data, ["virtual_network", "subnets"], "Catalog")
    fields(
        data["virtual_network"],
        ["name", "tenant", "subscription", "resource_group", "namespace"],
        "VNet selector",
    )
    for value in data["virtual_network"].values():
        text_field(value, "VNet selector")
    if not isinstance(data["subnets"], list) or not data["subnets"]:
        raise ValueError("subnets must be a nonempty list")
    seen = set()
    for entry in data["subnets"]:
        fields(entry, ["name", "prefix", "status", "delegations"], "Subnet")
        name = text_field(entry["name"], "Subnet name")
        if (
            not 1 <= len(name) <= 80
            or not re.fullmatch(NAME_RE, name)
            or name.lower() in seen
        ):
            raise ValueError(f"Invalid or duplicate subnet name: {name}")
        seen.add(name.lower())
        network(entry["prefix"])
        if entry["status"] not in AZURE_STATUSES:
            raise ValueError(f"Invalid status: {name}")
        if not isinstance(entry["delegations"], list):
            raise ValueError(f"{name}: delegations must be a list")
        names, services = set(), set()
        for delegation in entry["delegations"]:
            fields(delegation, ["name", "service_name"], "Delegation")
            dname = text_field(delegation["name"], "Delegation name")
            service = text_field(delegation["service_name"], "Delegated service")
            if (
                not 1 <= len(dname) <= 80
                or not re.fullmatch(NAME_RE, dname)
                or not re.fullmatch(DELEGATION_SERVICE_RE, service)
            ):
                raise ValueError(f"{name}: invalid delegation")
            if dname.lower() in names or service.lower() in services:
                raise ValueError(f"{name}: duplicate delegation name or service")
            names.add(dname.lower())
            services.add(service.lower())
    return data


def seed(client, branch, catalog, apply=False):
    inventory = read_inventory(client, branch)
    projected = deepcopy(inventory)
    selector = catalog["virtual_network"]

    def one(kind, name, **conditions):
        matches = [
            n
            for n in inventory[kind]
            if n["name"].casefold() == name.casefold()
            and all(n.get(k) == v for k, v in conditions.items())
        ]
        if len(matches) != 1:
            raise ValueError(
                f"{kind} {name}: expected one existing match, found {len(matches)}"
            )
        return matches[0]

    tenant = one("AzureTenant", selector["tenant"])
    sub = one("AzureSubscription", selector["subscription"], tenant=tenant["id"])
    rg = one("AzureResourceGroup", selector["resource_group"], subscription=sub["id"])
    vnet = one("AzureVirtualNetwork", selector["name"], resourcegroup=rg["id"])
    namespace = one("IpamNamespace", selector["namespace"])
    # Inspect existing concrete prefix metadata before choosing to reuse it.
    concrete = {
        n.id: n
        for n in client.all(kind="IpamPrefix", branch=branch, populate_store=False)
    }
    pending = []
    conflicts = []
    matched = 0
    for entry in catalog["subnets"]:
        name, cidr = entry["name"], entry["prefix"]
        prefix_matches = [
            p
            for p in projected["BuiltinIPPrefix"]
            if p["ip_namespace"] == namespace["id"] and str(p["prefix"]) == cidr
        ]
        if len(prefix_matches) > 1:
            conflicts.append(f"{name}: ambiguous prefix {cidr}")
            continue
        if prefix_matches:
            prefix_id = prefix_matches[0]["id"]
            p = concrete.get(prefix_id)
            if p is None or p.is_pool.value or p.vrf.id is not None:
                conflicts.append(
                    f"{name}: prefix {cidr} has incompatible type, pool, or VRF"
                )
                continue
            matched += 1
        else:
            prefix_id = f"pending-prefix:{cidr}"
            projected["BuiltinIPPrefix"].append(
                dict(id=prefix_id, prefix=cidr, ip_namespace=namespace["id"])
            )
            pending.append(
                (
                    "IpamPrefix",
                    prefix_id,
                    dict(
                        prefix=cidr,
                        ip_namespace=namespace["id"],
                        status="reserved",
                        is_pool=False,
                        member_type="prefix",
                        description=f"Subnet {name} in {selector['name']}",
                    ),
                )
            )
        matches = [
            n
            for n in inventory["AzureVirtualNetworkSubnet"]
            if n["virtualnetwork"] == vnet["id"] and n["name"].lower() == name.lower()
        ]
        if len(matches) > 1:
            conflicts.append(f"{name}: ambiguous subnet")
            continue
        if matches:
            existing = matches[0]
            subnet_id = existing["id"]
            if existing["name"] != name or set(existing["address_prefixes"]) != {
                prefix_id
            }:
                conflicts.append(f"{name}: existing name or prefixes differ")
                continue
            matched += 1
        else:
            subnet_id = f"pending-subnet:{name}"
            row = dict(
                id=subnet_id,
                name=name,
                virtualnetwork=vnet["id"],
                address_prefixes=[prefix_id],
                network_security_group=None,
                route_table=None,
            )
            projected["AzureVirtualNetworkSubnet"].append(row)
            pending.append(
                (
                    "AzureVirtualNetworkSubnet",
                    subnet_id,
                    dict(
                        name=name,
                        virtualnetwork=vnet["id"],
                        address_prefixes=[prefix_id],
                        status=entry["status"],
                    ),
                )
            )
        existing_delegations = [
            d for d in inventory["AzureSubnetDelegation"] if d["subnet"] == subnet_id
        ]
        desired = {d["name"].lower(): d for d in entry["delegations"]}
        for existing in existing_delegations:
            wanted = desired.get(existing["name"].lower())
            if (
                wanted is None
                or existing["name"] != wanted["name"]
                or existing["service_name"] != wanted["service_name"]
            ):
                conflicts.append(
                    f"{name}: existing delegation {existing['name']} differs from catalog"
                )
        for d in entry["delegations"]:
            matches = [
                existing
                for existing in existing_delegations
                if existing["name"].lower() == d["name"].lower()
            ]
            if matches:
                matched += 1
                continue
            delegation_id = f"pending-delegation:{name}:{d['name']}"
            projected["AzureSubnetDelegation"].append(
                dict(id=delegation_id, subnet=subnet_id, **d)
            )
            pending.append(
                (
                    "AzureSubnetDelegation",
                    delegation_id,
                    dict(subnet=subnet_id, status="planned", **d),
                )
            )
    conflicts.extend(validate(projected))
    typer.echo(
        f"Preflight: missing={len(pending)}, matched={matched}, conflicting={len(conflicts)}"
    )
    for conflict in conflicts:
        typer.echo(conflict, err=True)
    if conflicts:
        typer.echo("Created=0; resolve conflicts before applying.", err=True)
        return 1
    for kind, _, data in pending:
        typer.echo(f"Would create {kind}: {data.get('name', data.get('prefix'))}")
    if not apply:
        typer.echo("Preview only; created=0. Use --apply to create missing records.")
        return 0
    created = 0
    ids = {}
    order = {
        "IpamPrefix": 0,
        "AzureVirtualNetworkSubnet": 1,
        "AzureSubnetDelegation": 2,
    }
    try:
        for kind, temporary, data in sorted(pending, key=lambda p: order[p[0]]):
            values = deepcopy(data)
            if "address_prefixes" in values:
                values["address_prefixes"] = [
                    ids.get(id_, id_) for id_ in values["address_prefixes"]
                ]
            if "subnet" in values:
                values["subnet"] = ids.get(values["subnet"], values["subnet"])
            node = client.create(kind=kind, branch=branch, data=values)
            node.save()
            ids[temporary] = node.id
            created += 1
    except Exception:
        typer.echo(
            f"Apply interrupted: created={created}. Inspect and rerun; no rollback was attempted.",
            err=True,
        )
        raise
    typer.echo(f"Applied: created={created}, matched={matched}")
    return 0


@app.command()
def main(
    branch: Annotated[str, typer.Option(help="Infrahub branch to seed")],
    data: Annotated[Path, typer.Option(help="Subnet catalog")] = Path(
        "data/azure_hub_subnets.yaml"
    ),
    apply: Annotated[bool, typer.Option(help="Create missing records")] = False,
):
    try:
        catalog = load_catalog(data)
        code = seed(InfrahubClientSync(), branch, catalog, apply)
    except Exception as exc:
        detail = str(exc) if type(exc) is ValueError else type(exc).__name__
        typer.echo(f"Subnet seed failed: {detail}", err=True)
        code = 1
    raise typer.Exit(code=code)


if __name__ == "__main__":
    app()
