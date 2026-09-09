"""Preview or create the Cloud location catalog, preserving operational edits."""

import re
from pathlib import Path
from typing import Annotated

import typer
import yaml
from infrahub_sdk import InfrahubClientSync

if __package__:
    from .seed_azure_hierarchy import fields, text
else:
    from seed_azure_hierarchy import fields, text

app = typer.Typer(help=__doc__, add_completion=False, pretty_exceptions_enable=False)
AREAS = (
    "North America",
    "South America",
    "Europe",
    "Asia Pacific",
    "Middle East",
    "Africa",
)
GROUPS = {
    "cloud": ("Cloud", None),
    "cloud-azure": ("Azure", "cloud"),
    "cloud-aws": ("AWS", "cloud"),
    **{
        "cloud-azure-" + area.lower().replace(" ", "-"): (area, "cloud-azure")
        for area in AREAS
    },
}


def load_catalog(path: Path) -> list[dict]:
    catalog = yaml.safe_load(path.read_text())
    fields(catalog, ["source", "groups", "regions"], "Catalog")
    fields(catalog["source"], ["url", "retrieved", "source_sha256"], "Source")
    for key, value in catalog["source"].items():
        text(value, f"Source {key}")
    by_name = {}
    for section, kind in [("groups", "LocationGroup"), ("regions", "AzureRegion")]:
        entries = catalog[section]
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"{section} must be a nonempty list")
        for entry in entries:
            fields(entry, ["name", "display_name", "parent"], kind)
            name = text(entry["name"], "name")
            text(entry["display_name"], f"{name} display_name")
            if name.casefold() in by_name:
                raise ValueError(f"Duplicate location name: {name}")
            if kind == "LocationGroup":
                if GROUPS.get(name) != (entry["display_name"], entry["parent"]):
                    raise ValueError(
                        f"{name}: group must match the agreed Cloud hierarchy"
                    )
            elif re.fullmatch(r"[a-z][a-z0-9]+", name) is None or entry[
                "parent"
            ] not in set(GROUPS) - {"cloud", "cloud-azure", "cloud-aws"}:
                raise ValueError(
                    f"{name}: invalid region identifier or geographic parent"
                )
            by_name[name.casefold()] = {**entry, "kind": kind}
    if {e["name"] for e in by_name.values() if e["kind"] == "LocationGroup"} != set(
        GROUPS
    ):
        raise ValueError("Catalog must contain all nine Cloud location groups")
    ordered = []
    resolved = {None}
    while by_name:
        ready = [e for e in by_name.values() if e["parent"] in resolved]
        if not ready:
            raise ValueError("Catalog has a cycle or missing parent")
        for entry in ready:
            ordered.append(entry)
            resolved.add(entry["name"])
            del by_name[entry["name"].casefold()]
    return ordered


def seed(client, branch: str, entries: list[dict], apply: bool = False) -> int:
    # SDK all() follows pagination, including inventories larger than one page.
    inventory = client.all(
        kind="LocationGeneric",
        branch=branch,
        include=["name", "parent"],
        populate_store=False,
    )
    # Generic queries expose only generic fields; read subtype display names
    # explicitly, retaining generic inventory for collisions with other types.
    concrete = {}
    for kind in ("LocationGroup", "AzureRegion"):
        for node in client.all(
            kind=kind,
            branch=branch,
            include=["name", "display_name", "parent"],
            populate_store=False,
        ):
            concrete[node.id] = node
    inventory = [concrete.get(node.id, node) for node in inventory]
    by_name = {}
    conflicts = []
    for node in inventory:
        key = node.name.value.casefold()
        if key in by_name:
            conflicts.append(
                f"Duplicate location name {node.name.value}: {by_name[key].id}, {node.id}"
            )
        by_name[key] = node
    resolved = {None: None}
    pending = []
    skipped = 0
    for entry in entries:
        name = entry["name"]
        existing = by_name.get(name.casefold())
        parent_id = resolved[entry["parent"]]
        if existing is None:
            resolved[name] = f"planned:{name}"
            pending.append(entry)
            continue
        resolved[name] = existing.id
        changed = []
        if existing.get_kind() != entry["kind"]:
            changed.append("kind")
        if existing.name.value != name:
            changed.append("name")
        if (
            getattr(getattr(existing, "display_name", None), "value", None)
            != entry["display_name"]
        ):
            changed.append("display_name")
        if existing.parent.id != parent_id:
            changed.append("parent")
        if changed:
            conflicts.append(
                f"{name} ({existing.id}): conflicting {', '.join(changed)}"
            )
        else:
            skipped += 1
    for conflict in conflicts:
        typer.echo(f"Conflict: {conflict}", err=True)
    typer.echo(
        f"Preflight: missing={len(pending)}, skipped={skipped}, conflicting={len(conflicts)}"
    )
    if conflicts:
        typer.echo("Created=0; resolve conflicts before applying.", err=True)
        return 1
    for entry in pending:
        typer.echo(
            f"Would create {entry['kind']}: {entry['name']} ({entry['display_name']})"
        )
    if not apply:
        typer.echo("Preview only; created=0. Use --apply to create missing entries.")
        return 0
    created = 0
    try:
        for entry in pending:
            data = {k: entry[k] for k in ("name", "display_name")}
            if entry["parent"] is not None:
                data["parent"] = resolved[entry["parent"]]
            node = client.create(kind=entry["kind"], branch=branch, data=data)
            node.save()
            resolved[entry["name"]] = node.id
            created += 1
    except Exception:
        typer.echo(
            f"Apply interrupted: created={created}, skipped={skipped}. Inspect and rerun; no rollback was attempted.",
            err=True,
        )
        raise
    typer.echo(f"Applied: created={created}, skipped={skipped}, conflicting=0")
    return 0


@app.command()
def main(
    branch: Annotated[str, typer.Option(help="Infrahub branch to seed")],
    data: Annotated[Path, typer.Option(help="Seed YAML catalog")] = Path(
        "data/cloud_locations.yaml"
    ),
    apply: Annotated[bool, typer.Option(help="Create missing objects")] = False,
) -> None:
    try:
        entries = load_catalog(data)
        code = seed(InfrahubClientSync(), branch, entries, apply)
    except Exception as exc:
        detail = str(exc) if type(exc) is ValueError else type(exc).__name__
        typer.echo(f"Cloud locations seed failed: {detail}", err=True)
        code = 1
    raise typer.Exit(code=code)


if __name__ == "__main__":
    app()
