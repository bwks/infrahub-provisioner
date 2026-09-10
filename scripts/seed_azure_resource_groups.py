"""Preview or create agreed Azure resource groups without overwriting operational edits."""

from pathlib import Path
from typing import Annotated

import typer
import yaml
from infrahub_sdk import InfrahubClientSync

if __package__:
    from .seed_azure_hierarchy import fields, text
    from .verify_schema import AZURE_STATUSES
else:
    from seed_azure_hierarchy import fields, text
    from verify_schema import AZURE_STATUSES

app = typer.Typer(help=__doc__, add_completion=False, pretty_exceptions_enable=False)


def load_catalog(path: Path) -> list[dict]:
    data = yaml.safe_load(path.read_text())
    fields(data, ["resource_groups"], "Catalog")
    entries = data["resource_groups"]
    if not isinstance(entries, list) or not entries:
        raise ValueError("resource_groups must be a nonempty list")
    seen = set()
    for entry in entries:
        fields(
            entry,
            ["name", "tenant", "subscription", "region", "status"],
            "Resource group",
        )
        for key, value in entry.items():
            text(value, key)
        identity = tuple(
            entry[k].casefold() for k in ("tenant", "subscription", "name")
        )
        if identity in seen:
            raise ValueError(f"Duplicate resource group: {entry['name']}")
        seen.add(identity)
        if entry["status"] not in AZURE_STATUSES:
            raise ValueError(f"Invalid status for {entry['name']}")
    return entries


def seed(client, branch: str, entries: list[dict], apply: bool = False) -> int:
    inventory = {
        kind: client.all(kind=kind, branch=branch, include=attrs, populate_store=False)
        for kind, attrs in [
            ("AzureTenant", ["name"]),
            ("AzureSubscription", ["name", "tenant"]),
            ("AzureRegion", ["name"]),
            ("AzureResourceGroup", ["name", "subscription", "location"]),
        ]
    }
    conflicts = []
    pending = []
    skipped = 0

    def unique(matches, label):
        if len(matches) != 1:
            conflicts.append(
                f"{label}: expected one existing match, found {len(matches)}"
            )
            return None
        return matches[0]

    for entry in entries:
        name = entry["name"]
        tenant = unique(
            [
                n
                for n in inventory["AzureTenant"]
                if n.name.value.casefold() == entry["tenant"].casefold()
            ],
            f"{name}: tenant {entry['tenant']}",
        )
        region = unique(
            [
                n
                for n in inventory["AzureRegion"]
                if n.name.value.casefold() == entry["region"].casefold()
            ],
            f"{name}: region {entry['region']}",
        )
        if tenant is None or region is None:
            continue
        sub = unique(
            [
                n
                for n in inventory["AzureSubscription"]
                if n.tenant.id == tenant.id
                and n.name.value.casefold() == entry["subscription"].casefold()
            ],
            f"{name}: subscription {entry['subscription']} in {entry['tenant']}",
        )
        if sub is None:
            continue
        matches = [
            n
            for n in inventory["AzureResourceGroup"]
            if n.subscription.id == sub.id
            and n.name.value.casefold() == name.casefold()
        ]
        if len(matches) > 1:
            conflicts.append(
                f"{name}: ambiguous resource groups {[n.id for n in matches]}"
            )
        elif matches:
            existing = matches[0]
            if existing.name.value != name or existing.location.id != region.id:
                conflicts.append(
                    f"{name} ({existing.id}): name or region differs from catalog"
                )
            else:
                skipped += 1
        else:
            pending.append(
                {
                    "name": name,
                    "subscription": sub.id,
                    "location": region.id,
                    "status": entry["status"],
                }
            )
    for conflict in conflicts:
        typer.echo(f"Conflict: {conflict}", err=True)
    typer.echo(
        f"Preflight: missing={len(pending)}, skipped={skipped}, conflicting={len(conflicts)}"
    )
    if conflicts:
        typer.echo("Created=0; resolve conflicts before applying.", err=True)
        return 1
    for data in pending:
        typer.echo(f"Would create resource group: {data['name']}")
    if not apply:
        typer.echo("Preview only; created=0. Use --apply to create missing entries.")
        return 0
    created = 0
    try:
        for data in pending:
            node = client.create(kind="AzureResourceGroup", branch=branch, data=data)
            node.save()
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
    data: Annotated[Path, typer.Option(help="Resource group catalog")] = Path(
        "data/azure_resource_groups.yaml"
    ),
    apply: Annotated[bool, typer.Option(help="Create missing objects")] = False,
) -> None:
    try:
        entries = load_catalog(data)
        code = seed(InfrahubClientSync(), branch, entries, apply)
    except Exception as exc:
        detail = str(exc) if type(exc) is ValueError else type(exc).__name__
        typer.echo(f"Resource group seed failed: {detail}", err=True)
        code = 1
    raise typer.Exit(code=code)


if __name__ == "__main__":
    app()
