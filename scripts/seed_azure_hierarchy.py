"""Preview or create a planned Azure hierarchy without overwriting operational edits."""

from pathlib import Path
from typing import Annotated

import typer
import yaml
from infrahub_sdk import InfrahubClientSync

if __package__:
    from .check_azure_hierarchy import GROUP_ID, Group, Subscription, Tenant, validate
else:
    from check_azure_hierarchy import GROUP_ID, Group, Subscription, Tenant, validate

app = typer.Typer(help=__doc__, add_completion=False, pretty_exceptions_enable=False)


def fields(value, expected, label):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError(f"{label} must contain exactly: {', '.join(expected)}")


def text(value, label):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(
            f"{label} must be nonempty text without surrounding whitespace"
        )
    return value


def load_catalog(path: Path) -> dict:
    catalog = yaml.safe_load(path.read_text())
    required = ["tenant", "root", "management_groups"]
    fields(
        catalog,
        required
        + (
            ["subscriptions"]
            if isinstance(catalog, dict) and "subscriptions" in catalog
            else []
        ),
        "Catalog",
    )
    fields(catalog["tenant"], ["name"], "Tenant")
    fields(catalog["root"], ["display_name"], "Root")
    text(catalog["tenant"]["name"], "Tenant name")
    text(catalog["root"]["display_name"], "Root display_name")
    entries = catalog["management_groups"]
    if not isinstance(entries, list) or not entries:
        raise ValueError("management_groups must be a nonempty list")
    by_id = {}
    for entry in entries:
        fields(entry, ["management_group_id", "display_name", "parent"], "Group")
        identifier = text(entry["management_group_id"], "management_group_id")
        if GROUP_ID.fullmatch(identifier) is None:
            raise ValueError(f"Invalid management-group ID: {identifier!r}")
        text(entry["display_name"], f"{identifier} display_name")
        if entry["parent"] is not None:
            text(entry["parent"], f"{identifier} parent")
        key = identifier.casefold()
        if key in by_id:
            raise ValueError(f"Duplicate management-group ID: {identifier}")
        by_id[key] = entry
    # YAML parent=null means the separately declared tenant root, not a second root.
    projected = [Group("root", None, "tenant")]
    for key, entry in by_id.items():
        parent = entry["parent"]
        parent_key = parent.casefold() if parent is not None else None
        if parent_key is not None and parent_key not in by_id:
            raise ValueError(f"{key}: unknown parent {parent!r}")
        projected.append(
            Group(
                f"group:{key}",
                entry["management_group_id"],
                "tenant",
                f"group:{parent_key}" if parent_key is not None else "root",
            )
        )
    subscription_entries = catalog.get("subscriptions", [])
    if not isinstance(subscription_entries, list):
        raise ValueError("subscriptions must be a list")
    names = set()
    projected_subscriptions = []
    for entry in subscription_entries:
        fields(entry, ["name", "management_group", "status"], "Subscription")
        name = text(entry["name"], "Subscription name")
        group = text(entry["management_group"], f"{name} management_group").casefold()
        status = text(entry["status"], f"{name} status")
        if name.casefold() in names:
            raise ValueError(f"Duplicate subscription name: {name}")
        names.add(name.casefold())
        if group not in by_id:
            raise ValueError(f"{name}: unknown management group {group}")
        if status not in {"planned", "active", "reserved", "deprecated", "unmanaged"}:
            raise ValueError(f"{name}: invalid subscription status {status}")
        projected_subscriptions.append(
            Subscription(f"subscription:{name}", None, "tenant", f"group:{group}")
        )
    findings = validate([Tenant("tenant", None)], projected, projected_subscriptions)
    if findings:
        raise ValueError("Invalid catalog hierarchy: " + "; ".join(findings))
    # Topological order, independent of YAML entry order.
    ordered = []
    remaining = dict(by_id)
    resolved = set()
    while remaining:
        for key, entry in list(remaining.items()):
            if entry["parent"] is None or entry["parent"].casefold() in resolved:
                ordered.append(entry)
                resolved.add(key)
                del remaining[key]
    return {**catalog, "management_groups": ordered}


def seed(client, branch: str, catalog: dict, apply: bool = False) -> int:
    tenants = client.all(
        kind="AzureTenant",
        branch=branch,
        include=["name", "tenant_id"],
        populate_store=False,
    )
    groups = client.all(
        kind="AzureManagementGroup",
        branch=branch,
        include=["management_group_id", "display_name", "tenant", "parent"],
        populate_store=False,
    )
    subscriptions = client.all(
        kind="AzureSubscription",
        branch=branch,
        include=["name", "subscription_id", "tenant", "management_group"],
        populate_store=False,
    )
    conflicts = []
    matches = [
        t
        for t in tenants
        if t.name.value.casefold() == catalog["tenant"]["name"].casefold()
    ]
    if len(matches) > 1:
        conflicts.append(
            f"Ambiguous tenant name {catalog['tenant']['name']!r}: {[n.id for n in matches]}"
        )
    tenant = matches[0] if matches else None
    if tenant is not None and tenant.name.value != catalog["tenant"]["name"]:
        conflicts.append(f"Tenant {tenant.id}: name differs from catalog")
    tenant_id = tenant.id if tenant is not None else "planned:tenant"
    projected_tenants = [Tenant(t.id, t.tenant_id.value) for t in tenants]
    if tenant is None:
        projected_tenants.append(Tenant(tenant_id, None))
    projected_groups = [
        Group(g.id, g.management_group_id.value, g.tenant.id, g.parent.id)
        for g in groups
    ]
    projected_subscriptions = [
        Subscription(s.id, s.subscription_id.value, s.tenant.id, s.management_group.id)
        for s in subscriptions
    ]
    tenant_groups = [g for g in groups if g.tenant.id == tenant_id]
    resolved = {}
    pending = []
    skipped = int(tenant is not None)
    # None is the catalog reference for the tenant root.
    entries = [(None, catalog["root"])] + [
        (g["management_group_id"].casefold(), g) for g in catalog["management_groups"]
    ]
    for key, entry in entries:
        matches = [
            g
            for g in tenant_groups
            if (
                g.parent.id is None
                if key is None
                else g.management_group_id.value is not None
                and g.management_group_id.value.casefold() == key
            )
        ]
        if len(matches) > 1:
            conflicts.append(
                f"Ambiguous group {key or 'tenant root'}: {[g.id for g in matches]}"
            )
        existing = matches[0] if matches else None
        parent_key = entry.get("parent")
        parent_id = (
            None
            if key is None
            else resolved[parent_key.casefold() if parent_key is not None else None]
        )
        if existing is not None:
            resolved[key] = existing.id
            changed = []
            if existing.display_name.value != entry["display_name"]:
                changed.append("display_name")
            if existing.parent.id != parent_id:
                changed.append("parent")
            if changed:
                conflicts.append(
                    f"Group {key or 'tenant root'} ({existing.id}): {', '.join(changed)}"
                )
            else:
                skipped += 1
        else:
            projected_id = f"planned:group:{key}" if key is not None else "planned:root"
            resolved[key] = projected_id
            projected_groups.append(
                Group(
                    projected_id, entry.get("management_group_id"), tenant_id, parent_id
                )
            )
            pending.append((key, entry))
    pending_subscriptions = []
    for entry in catalog.get("subscriptions", []):
        name = entry["name"]
        group_id = resolved[entry["management_group"].casefold()]
        matches = [
            sub
            for sub in subscriptions
            if sub.tenant.id == tenant_id
            and sub.name.value.casefold() == name.casefold()
        ]
        if len(matches) > 1:
            conflicts.append(
                f"Ambiguous subscription {name!r} in tenant {tenant_id}: {[n.id for n in matches]}"
            )
        if matches:
            existing = matches[0]
            changed = []
            if existing.name.value != name:
                changed.append("name")
            if existing.management_group.id != group_id:
                changed.append("management_group")
            if changed:
                conflicts.append(
                    f"Subscription {name} ({existing.id}): {', '.join(changed)}"
                )
            else:
                skipped += 1
        else:
            pending_subscriptions.append(entry)
            projected_subscriptions.append(
                Subscription(f"planned:subscription:{name}", None, tenant_id, group_id)
            )
    conflicts.extend(
        validate(projected_tenants, projected_groups, projected_subscriptions)
    )
    missing = len(pending) + len(pending_subscriptions) + int(tenant is None)
    for conflict in conflicts:
        typer.echo(f"Conflict: {conflict}", err=True)
    typer.echo(
        f"Preflight: missing={missing}, skipped={skipped}, conflicting={len(conflicts)}"
    )
    if conflicts:
        typer.echo("Created=0; resolve conflicts before applying.", err=True)
        return 1
    if tenant is None:
        typer.echo(f"Would create tenant: {catalog['tenant']['name']}")
    for key, entry in pending:
        typer.echo(
            f"Would create group: {key or 'tenant root'} ({entry['display_name']})"
        )
    for entry in pending_subscriptions:
        typer.echo(
            f"Would create subscription: {entry['name']} -> {entry['management_group']} ({entry['status']})"
        )
    if not apply:
        typer.echo("Preview only; created=0. Use --apply to create missing entries.")
        return 0
    created = 0
    try:
        if tenant is None:
            tenant = client.create(
                kind="AzureTenant", branch=branch, data=catalog["tenant"]
            )
            tenant.save()
            created += 1
        for key, entry in pending:
            data = {"display_name": entry["display_name"], "tenant": tenant.id}
            if key is not None:
                parent = entry["parent"]
                data.update(
                    management_group_id=entry["management_group_id"],
                    parent=resolved[parent.casefold() if parent is not None else None],
                )
            node = client.create(kind="AzureManagementGroup", branch=branch, data=data)
            node.save()
            created += 1
            resolved[key] = node.id
        for entry in pending_subscriptions:
            node = client.create(
                kind="AzureSubscription",
                branch=branch,
                data={
                    "name": entry["name"],
                    "tenant": tenant.id,
                    "management_group": resolved[entry["management_group"].casefold()],
                    "status": entry["status"],
                },
            )
            node.save()
            created += 1
    except Exception:
        typer.echo(
            f"Apply interrupted: created={created}, skipped={skipped}. Inspect the branch and rerun; no rollback was attempted.",
            err=True,
        )
        raise
    typer.echo(f"Applied: created={created}, skipped={skipped}, conflicting=0")
    return 0


@app.command()
def main(
    branch: Annotated[str, typer.Option(help="Infrahub branch to seed")],
    data: Annotated[Path, typer.Option(help="Seed YAML catalog")] = Path(
        "data/azure_hierarchy.yaml"
    ),
    apply: Annotated[bool, typer.Option(help="Create missing objects")] = False,
) -> None:
    try:
        catalog = load_catalog(data)
        code = seed(InfrahubClientSync(), branch, catalog, apply)
    except Exception as exc:
        detail = str(exc) if type(exc) is ValueError else type(exc).__name__
        typer.echo(f"Azure hierarchy seed failed: {detail}", err=True)
        code = 1
    raise typer.Exit(code=code)


if __name__ == "__main__":
    app()
