"""Read-only validation of planned or provisioned Azure management-group trees."""

import re
import sys
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

import typer
from infrahub_sdk import InfrahubClientSync

app = typer.Typer(help=__doc__, add_completion=False, pretty_exceptions_enable=False)
# Microsoft.Management naming rules: 1–90 characters, alphanumeric start, no final dot.
GROUP_ID = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_.()-]{0,88}[A-Za-z0-9_()-])?")


@dataclass(frozen=True)
class Tenant:
    id: str
    tenant_id: str | None


@dataclass(frozen=True)
class Group:
    id: str
    management_group_id: str | None
    tenant: str | None
    parent: str | None = None


@dataclass(frozen=True)
class Subscription:
    id: str
    subscription_id: str | None
    tenant: str | None
    management_group: str | None = None


def validate(
    tenants: list[Tenant], groups: list[Group], subscriptions: list[Subscription]
) -> list[str]:
    findings = []
    tenant_by_id = {t.id: t for t in tenants}
    group_by_id = {g.id: g for g in groups}

    def label(obj):
        identifier = getattr(
            obj,
            "management_group_id",
            getattr(obj, "tenant_id", getattr(obj, "subscription_id", "")),
        )
        return f"{identifier!r} (Infrahub {obj.id})"

    def guid(value, obj):
        if value is None:
            return
        try:
            if str(UUID(value)) != value.lower():
                raise ValueError
        except (ValueError, AttributeError, TypeError):
            findings.append(f"{label(obj)}: invalid Azure GUID {value!r}")

    seen_tenants = {}
    for tenant in tenants:
        guid(tenant.tenant_id, tenant)
        key = tenant.tenant_id.casefold() if tenant.tenant_id is not None else None
        if key is not None and key in seen_tenants:
            findings.append(
                f"Duplicate tenant ID: {label(tenant)} and {label(seen_tenants[key])}"
            )
        seen_tenants[key] = tenant
        roots = [g for g in groups if g.tenant == tenant.id and g.parent is None]
        if len(roots) != 1:
            findings.append(
                f"Tenant {label(tenant)}: expected exactly one root, found {len(roots)}: {[label(g) for g in roots]}"
            )
        for root in roots:
            if (
                root.management_group_id is not None
                and key is not None
                and root.management_group_id.casefold() != key
            ):
                findings.append(
                    f"Root {label(root)}: ID must match tenant {label(tenant)}"
                )

    seen_groups = {}
    for group in groups:
        identifier = group.management_group_id
        key = (group.tenant, identifier.casefold()) if identifier is not None else None
        if key is not None and key in seen_groups:
            findings.append(
                f"Duplicate management-group ID in tenant {group.tenant}: {label(group)} and {label(seen_groups[key])}"
            )
        seen_groups[key] = group
        if identifier is None and group.parent is not None:
            findings.append(
                f"Group {label(group)}: non-root management-group ID is required"
            )
        if group.parent is None:
            guid(identifier, group)
        if identifier is not None and GROUP_ID.fullmatch(identifier) is None:
            findings.append(
                f"Group {label(group)}: invalid management-group identifier (1–90 allowed characters, alphanumeric start, no final period)"
            )
        if group.tenant not in tenant_by_id:
            findings.append(
                f"Group {label(group)}: missing or unknown tenant {group.tenant!r}"
            )
        # Walk iteratively so even a malformed, very deep inventory cannot overflow.
        path = []
        visited = set()
        current = group
        while True:
            if current.id in visited:
                findings.append(
                    f"Group {label(group)}: cycle/self-parenting in path {' -> '.join(path + [label(current)])}"
                )
                break
            visited.add(current.id)
            path.append(label(current))
            if current.parent is None:
                if len(path) - 1 > 6:
                    findings.append(
                        f"Group {label(group)}: depth {len(path) - 1} exceeds six levels beneath root {label(current)}"
                    )
                break
            parent = group_by_id.get(current.parent)
            if parent is None:
                findings.append(
                    f"Group {label(group)}: path has missing parent {current.parent!r} at {label(current)}"
                )
                break
            if parent.tenant != current.tenant:
                findings.append(
                    f"Group {label(group)}: cross-tenant parent {label(parent)} (tenant {parent.tenant}) at {label(current)} (tenant {current.tenant})"
                )
                break
            current = parent

    for subscription in subscriptions:
        guid(subscription.subscription_id, subscription)
        if subscription.tenant not in tenant_by_id:
            findings.append(
                f"Subscription {label(subscription)}: missing or unknown tenant {subscription.tenant!r}"
            )
        group = group_by_id.get(subscription.management_group)
        if group is None:
            findings.append(
                f"Subscription {label(subscription)}: missing or unknown management group {subscription.management_group!r}"
            )
        elif group.tenant != subscription.tenant:
            findings.append(
                f"Subscription {label(subscription)}: group {label(group)} belongs to tenant {group.tenant}, expected {subscription.tenant}"
            )
    return findings


def check(client: InfrahubClientSync, branch: str) -> int:
    # SDK all() follows pagination when no limit/offset is supplied. Request only
    # validation fields; relationship IDs suffice, with no peer fetches or writes.
    tenants = [
        Tenant(n.id, n.tenant_id.value)
        for n in client.all(
            kind="AzureTenant",
            branch=branch,
            include=["tenant_id"],
            populate_store=False,
        )
    ]
    groups = [
        Group(n.id, n.management_group_id.value, n.tenant.id, n.parent.id)
        for n in client.all(
            kind="AzureManagementGroup",
            branch=branch,
            include=["management_group_id", "tenant", "parent"],
            populate_store=False,
        )
    ]
    subscriptions = [
        Subscription(n.id, n.subscription_id.value, n.tenant.id, n.management_group.id)
        for n in client.all(
            kind="AzureSubscription",
            branch=branch,
            include=["subscription_id", "tenant", "management_group"],
            populate_store=False,
        )
    ]
    findings = validate(tenants, groups, subscriptions)
    if findings:
        print(f"Azure hierarchy invalid on branch {branch}:", file=sys.stderr)
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        return 1
    if not (tenants or groups or subscriptions):
        print(f"Azure hierarchy valid on branch {branch}: empty Azure inventory.")
    else:
        print(
            f"Azure hierarchy valid on branch {branch}: {len(tenants)} tenants, {len(groups)} groups, {len(subscriptions)} subscriptions."
        )
    pending = (
        sum(t.tenant_id is None for t in tenants)
        + sum(g.parent is None and g.management_group_id is None for g in groups)
        + sum(s.subscription_id is None for s in subscriptions)
    )
    if pending:
        print(
            f"Azure identifiers pending: {pending}; hierarchy is valid as planned intent."
        )
    return 0


@app.command()
def main(
    branch: Annotated[str, typer.Option(help="Infrahub branch to inspect")],
) -> None:
    try:
        code = check(InfrahubClientSync(), branch)
    except Exception as exc:
        # SDK exceptions may contain credentials or request details.
        print(f"Azure hierarchy read failed: {type(exc).__name__}", file=sys.stderr)
        code = 1
    raise typer.Exit(code=code)


if __name__ == "__main__":
    app()
