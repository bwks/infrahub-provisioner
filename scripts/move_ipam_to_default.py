"""Move the reference catalog from global to default, then delete the empty source."""

import ipaddress
from pathlib import Path
from typing import Annotated

import typer
from infrahub_sdk import InfrahubClientSync

if __package__:
    from .seed_ipam import differences, load_catalog
else:
    from seed_ipam import differences, load_catalog

app = typer.Typer(help=__doc__, add_completion=False, pretty_exceptions_enable=False)


def namespace_counts(client, branch, namespace_id):
    result = client.execute_graphql(
        query="query($id: ID!) { BuiltinIPPrefix(ip_namespace__ids: [$id]) { count } "
        "BuiltinIPAddress(ip_namespace__ids: [$id]) { count } }",
        variables={"id": namespace_id},
        branch_name=branch,
    )
    return result["BuiltinIPPrefix"]["count"], result["BuiltinIPAddress"]["count"]


def inventory(client, branch, namespace_id):
    return client.filters(
        kind="IpamPrefix",
        branch=branch,
        ip_namespace__ids=[namespace_id],
        populate_store=False,
    )


def verify_destination(nodes, expected, original_ids):
    actual = {str(n.prefix.value): n for n in nodes}
    if len(actual) != len(nodes) or set(actual) != set(expected):
        raise ValueError(
            "Destination does not contain exactly the catalog; source retained"
        )
    for cidr, desired in expected.items():
        node = actual[cidr]
        if node.id != original_ids[cidr] or differences(node, desired):
            raise ValueError(f"{cidr}: ID or catalog fields changed; source retained")
        network = ipaddress.ip_network(cidr)
        parents = [
            p
            for p in actual
            if ipaddress.ip_network(p).version == network.version
            and ipaddress.ip_network(p) != network
            and network.subnet_of(ipaddress.ip_network(p))
        ]
        parent = (
            max(parents, key=lambda p: ipaddress.ip_network(p).prefixlen)
            if parents
            else None
        )
        expected_parent = actual[parent].id if parent else None
        if node.parent.id != expected_parent:
            raise ValueError(
                f"{cidr}: native hierarchy has not reconciled; source retained, rerun after reconciliation"
            )


def move(client, branch: str, apply: bool = False) -> int:
    namespace, prefixes = load_catalog(Path("data/ipam.yaml"))
    if namespace["name"] != "default":
        raise ValueError("Catalog must select default before migration")
    expected = {p["prefix"]: p for p in prefixes}
    namespaces = client.all(kind="IpamNamespace", branch=branch)
    destinations = [n for n in namespaces if n.name.value == "default"]
    sources = [n for n in namespaces if n.name.value == "global"]
    if len(destinations) != 1 or len(sources) > 1:
        raise ValueError(
            "Expected one default namespace and at most one global namespace"
        )
    destination = destinations[0]
    source = sources[0] if sources else None
    if destination.default.value is not True or (source and source.default.value):
        raise ValueError(
            "Namespace default designations differ from the expected migration state"
        )
    old = inventory(client, branch, source.id) if source else []
    new = inventory(client, branch, destination.id)
    for ns, nodes in ((source, old), (destination, new)):
        if ns is not None and namespace_counts(client, branch, ns.id) != (
            len(nodes),
            0,
        ):
            raise ValueError(
                f"Namespace {ns.name.value}: unexpected prefix types or IP addresses; no migration"
            )
    combined = old + new
    by_cidr = {str(n.prefix.value): n for n in combined}
    if len(by_cidr) != len(combined) or set(by_cidr) != set(expected):
        raise ValueError(
            "Source and destination must contain exactly the catalog with no duplicates or other prefixes"
        )
    for cidr, node in by_cidr.items():
        changed = differences(node, expected[cidr])
        if node.vrf.id is not None:
            changed.append("vrf")
        if changed:
            raise ValueError(f"{cidr}: catalog conflict: {', '.join(changed)}")
    original_ids = {cidr: node.id for cidr, node in by_cidr.items()}
    typer.echo(
        f"Preflight: move={len(old)}, already_in_default={len(new)}, source_exists={source is not None}"
    )
    if not apply:
        typer.echo(
            "Preview only; no changes. Use --apply to move prefixes and delete empty global."
        )
        return 0
    moved = 0
    try:
        # Parents first; Infrahub recalculates native prefix containment on updates.
        for node in sorted(
            old, key=lambda n: ipaddress.ip_network(str(n.prefix.value)).prefixlen
        ):
            node.ip_namespace = destination.id
            node.save()
            moved += 1
        fresh = inventory(client, branch, destination.id)
        verify_destination(fresh, expected, original_ids)
        if namespace_counts(client, branch, destination.id) != (len(expected), 0):
            raise ValueError(
                "Destination inventory changed during migration; source retained"
            )
        if source is not None:
            # Namespace deletion cascades. Verify both generic inventories immediately
            # before deleting; never rely on a cached namespace relationship list.
            if namespace_counts(client, branch, source.id) != (0, 0):
                raise ValueError("Source is not empty; refusing namespace deletion")
            source.delete()
    except Exception:
        typer.echo(
            f"Migration interrupted after {moved} moves; no rollback. Inspect branch and rerun.",
            err=True,
        )
        raise
    typer.echo(
        f"Moved={moved}; verified={len(fresh)} unchanged prefix IDs and native hierarchy; global deleted or already absent."
    )
    return 0


@app.command()
def main(
    branch: Annotated[str, typer.Option(help="Infrahub migration branch")],
    apply: Annotated[
        bool, typer.Option(help="Move catalog and delete empty global")
    ] = False,
) -> None:
    try:
        code = move(InfrahubClientSync(), branch, apply)
    except Exception as exc:
        detail = str(exc) if type(exc) is ValueError else type(exc).__name__
        typer.echo(f"IPAM migration failed: {detail}", err=True)
        code = 1
    raise typer.Exit(code=code)


if __name__ == "__main__":
    app()
