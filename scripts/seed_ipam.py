"""Preview or create IPAM reference prefixes without overwriting existing objects."""

import ipaddress
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

import typer
import yaml
from infrahub_sdk import InfrahubClientSync

app = typer.Typer(help=__doc__, add_completion=False, pretty_exceptions_enable=False)


def text_field(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonempty text")
    return value


def fields(value, expected, label):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError(f"{label} must contain exactly: {', '.join(expected)}")


def load_catalog(path: Path):
    catalog = yaml.safe_load(path.read_text())
    fields(catalog, ["namespace", "prefixes"], "Catalog")
    namespace = catalog["namespace"]
    fields(namespace, ["name", "description"], "Namespace")
    for key, value in namespace.items():
        text_field(value, f"Namespace {key}")
    if not isinstance(catalog["prefixes"], list) or not catalog["prefixes"]:
        raise ValueError("prefixes must be a nonempty list")
    prefixes = []
    seen = set()
    for entry in catalog["prefixes"]:
        fields(entry, ["prefix", "description", "references"], "Prefix entry")
        cidr = text_field(entry["prefix"], "prefix")
        network = ipaddress.ip_network(cidr, strict=True)
        if str(network) != cidr:
            raise ValueError(f"Noncanonical prefix: {cidr}")
        if cidr in seen:
            raise ValueError(f"Duplicate prefix: {cidr}")
        seen.add(cidr)
        description = text_field(entry["description"], f"{cidr} description")
        refs = entry["references"]
        if not isinstance(refs, list) or not refs:
            raise ValueError(f"{cidr} requires references")
        for ref in refs:
            url = urlparse(text_field(ref, "reference"))
            if url.scheme != "https" or not url.netloc:
                raise ValueError(f"{cidr} requires HTTPS reference URLs")
        prefixes.append(
            {
                "prefix": cidr,
                "description": description + " References: " + ", ".join(refs),
                "status": "reserved",
                "role": None,
                "is_pool": False,
                "member_type": "address"
                if network.prefixlen == network.max_prefixlen
                else "prefix",
            }
        )
    prefixes.sort(
        key=lambda p: (
            ipaddress.ip_network(p["prefix"]).version,
            ipaddress.ip_network(p["prefix"]).prefixlen,
            int(ipaddress.ip_network(p["prefix"]).network_address),
        )
    )
    return {**namespace, "default": False}, prefixes


def differences(node, desired):
    changed = []
    for key, value in desired.items():
        actual = getattr(node, key).value
        if key == "prefix":
            actual = str(actual)
        # Infrahub returns null for a namespace without the default designation.
        if key == "default" and actual is None:
            actual = False
        if actual != value:
            changed.append(key)
    return changed


def seed(client, branch, namespace, prefixes, apply=False):
    existing_namespace = client.get(
        kind="IpamNamespace",
        name__value=namespace["name"],
        branch=branch,
        raise_when_missing=False,
    )
    conflicts = []
    pending = []
    skipped = 0
    if existing_namespace is not None:
        diff = differences(existing_namespace, namespace)
        if diff:
            conflicts.append(f"Namespace {namespace['name']}: {', '.join(diff)}")
        else:
            skipped += 1
    for prefix in prefixes:
        node = None
        if existing_namespace is not None:
            node = client.get(
                kind="IpamPrefix",
                hfid=[namespace["name"], prefix["prefix"]],
                branch=branch,
                raise_when_missing=False,
            )
        if node is None:
            pending.append(prefix)
        else:
            diff = differences(node, prefix)
            if node.vrf.id is not None:
                diff.append("vrf")
            if diff:
                conflicts.append(f"{prefix['prefix']}: {', '.join(diff)}")
            else:
                skipped += 1
    missing = len(pending) + int(existing_namespace is None)
    for conflict in conflicts:
        typer.echo(f"Conflict: {conflict}", err=True)
    typer.echo(
        f"Preflight: missing={missing}, skipped={skipped}, conflicting={len(conflicts)}"
    )
    if conflicts:
        typer.echo("Created=0; resolve conflicts before applying.", err=True)
        return 1
    for prefix in pending:
        typer.echo(f"Would create {namespace['name']}: {prefix['prefix']}")
    if not apply:
        typer.echo("Preview only; created=0. Use --apply to create missing entries.")
        return 0
    created = 0
    try:
        if existing_namespace is None:
            existing_namespace = client.create(
                kind="IpamNamespace", data=namespace, branch=branch
            )
            existing_namespace.save()
            created += 1
        for prefix in pending:
            node = client.create(
                kind="IpamPrefix",
                data={**prefix, "ip_namespace": existing_namespace.id},
                branch=branch,
            )
            node.save()
            created += 1
    except Exception:
        typer.echo(
            f"Apply interrupted: created={created}, skipped={skipped}. "
            "Inspect the branch and rerun; no rollback was attempted.",
            err=True,
        )
        raise
    typer.echo(f"Applied: created={created}, skipped={skipped}, conflicting=0")
    return 0


@app.command()
def main(
    branch: Annotated[str, typer.Option(help="Infrahub branch to seed")],
    data: Annotated[Path, typer.Option(help="Seed YAML catalog")] = Path(
        "data/ipam.yaml"
    ),
    apply: Annotated[bool, typer.Option(help="Create missing objects")] = False,
):
    try:
        namespace, prefixes = load_catalog(data)
        code = seed(InfrahubClientSync(), branch, namespace, prefixes, apply)
    except Exception as exc:
        detail = str(exc) if type(exc) is ValueError else type(exc).__name__
        typer.echo(f"IPAM seed failed: {detail}", err=True)
        code = 1
    raise typer.Exit(code=code)


if __name__ == "__main__":
    app()
