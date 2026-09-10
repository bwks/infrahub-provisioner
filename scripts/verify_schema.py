"""Read-only verification of deployed IPAM and Azure schemas; creates no objects."""

import sys
from typing import Annotated

import typer
from infrahub_sdk import InfrahubClientSync

app = typer.Typer(help=__doc__, add_completion=False, pretty_exceptions_enable=False)

AZURE_NODES = (
    "AzureManagementGroup",
    "AzureRegion",
    "AzureTenant",
    "AzureSubscription",
    "AzureResourceGroup",
    "AzureVirtualNetwork",
    "AzureVirtualNetworkSubnet",
)


AZURE_STATUSES = {"planned", "active", "reserved", "deprecated", "unmanaged"}


def verify_azure(schemas) -> None:
    for kind in (*AZURE_NODES, "AzureResource", "AzureManagementGroupHierarchy"):
        if kind not in schemas:
            raise ValueError(f"{kind} is missing")
    for kind in (*AZURE_NODES, "AzureResource", "AzureManagementGroupHierarchy"):
        status = schemas[kind].get_attribute_or_none("status")
        default = "unmanaged" if kind == "AzureRegion" else "planned"
        if (
            status is None
            or status.kind != "Dropdown"
            or not status.optional
            or status.default_value != default
            or {choice["name"] for choice in status.choices or []} != AZURE_STATUSES
        ):
            raise ValueError(
                f"{kind}.status must be an Azure lifecycle dropdown defaulting to {default}"
            )
    if "AzureLocation" in schemas:
        raise ValueError("AzureLocation must be retired in favor of AzureRegion")
    for kind in ("AzureRegion", "LocationGroup"):
        if kind not in schemas:
            raise ValueError(f"{kind} is missing")
        node = schemas[kind]
        if (
            node.hierarchy != "LocationGeneric"
            or "LocationGeneric" not in node.inherit_from
        ):
            raise ValueError(f"{kind} must use the LocationGeneric hierarchy")
        if node.display_label != "display_name__value":
            raise ValueError(f"{kind} must display its readable name")
        if not node.get_attribute("name").unique:
            raise ValueError(f"{kind}.name must be unique")
        if node.get_attribute("display_name").optional:
            raise ValueError(f"{kind}.display_name must be required")
    for kind in ("AzureResource", "AzureResourceGroup", "AzureVirtualNetwork"):
        if schemas[kind].get_relationship("location").label != "Region":
            raise ValueError(f"{kind}.location must be labeled Region")
    resource_group = schemas["AzureResourceGroup"]
    if not any(
        set(c) == {"subscription", "name_key__value"}
        for c in resource_group.uniqueness_constraints or []
    ):
        raise ValueError(
            "AzureResourceGroup uniqueness must use subscription and normalized name"
        )
    normalized = resource_group.get_attribute_or_none("name_key")
    if (
        normalized is None
        or normalized.kind != "Text"
        or not normalized.read_only
        or normalized.optional
        or normalized.unique
    ):
        raise ValueError(
            "AzureResourceGroup.name_key must be required read-only Text, scoped rather than globally unique"
        )
    if "AzureResource" not in schemas["AzureVirtualNetwork"].inherit_from:
        raise ValueError("AzureVirtualNetwork must inherit from AzureResource")
    vnet = schemas["AzureVirtualNetwork"]
    if vnet.uniqueness_constraints != [["resourcegroup", "name_key__value"]]:
        raise ValueError(
            "AzureVirtualNetwork uniqueness must use resourcegroup and normalized name"
        )
    key = vnet.get_attribute_or_none("name_key")
    if (
        key is None
        or key.kind != "Text"
        or key.optional
        or not key.read_only
        or key.unique
    ):
        raise ValueError(
            "AzureVirtualNetwork.name_key must be required read-only scoped Text"
        )
    name = vnet.get_attribute("name")
    if (
        name.optional
        or name.unique
        or name.min_length != 2
        or name.max_length != 64
        or name.regex != r"^[A-Za-z0-9][A-Za-z0-9_.-]*[A-Za-z0-9_]\Z"
    ):
        raise ValueError("AzureVirtualNetwork.name must enforce Azure naming rules")
    for field in ("resourcegroup", "location", "address_space"):
        relationship = vnet.get_relationship(field)
        if relationship.optional or (
            field == "address_space" and relationship.min_count != 1
        ):
            raise ValueError(f"AzureVirtualNetwork.{field} must be required")
    group = schemas["AzureManagementGroup"]
    if (
        group.hierarchy != "AzureManagementGroupHierarchy"
        or "AzureManagementGroupHierarchy" not in group.inherit_from
    ):
        raise ValueError("AzureManagementGroup must use its native hierarchy generic")
    if "AzureResource" in group.inherit_from:
        raise ValueError("AzureManagementGroup must not inherit AzureResource")
    if not any(
        set(c) == {"tenant", "management_group_id__value"}
        for c in group.uniqueness_constraints
    ):
        raise ValueError("AzureManagementGroup uniqueness must be scoped to tenant")
    for name in ("management_group_id", "display_name", "description"):
        attribute = group.get_attribute(name)
        if attribute.kind != "Text" or attribute.optional != (name != "display_name"):
            raise ValueError(f"AzureManagementGroup.{name} has an invalid definition")
    for kind, name in (
        ("AzureTenant", "tenant_id"),
        ("AzureSubscription", "subscription_id"),
    ):
        attribute = schemas[kind].get_attribute(name)
        if attribute.kind != "Text" or not attribute.optional:
            raise ValueError(f"{kind}.{name} must be optional Text")
    for kind, name, optional in (
        ("AzureManagementGroup", "tenant", False),
        ("AzureSubscription", "management_group", True),
    ):
        relationship = schemas[kind].get_relationship_or_none(name)
        if relationship is None or relationship.optional != optional:
            raise ValueError(f"{kind}.{name} optional must be {optional}")
    relationships = (
        ("AzureTenant", "management_groups", "AzureManagementGroup", "many"),
        ("AzureManagementGroup", "tenant", "AzureTenant", "one"),
        ("AzureManagementGroup", "subscriptions", "AzureSubscription", "many"),
        ("AzureManagementGroup", "parent", "AzureManagementGroupHierarchy", "one"),
        ("AzureManagementGroup", "children", "AzureManagementGroupHierarchy", "many"),
        ("AzureSubscription", "management_group", "AzureManagementGroup", "one"),
        ("AzureTenant", "subscriptions", "AzureSubscription", "many"),
        ("AzureSubscription", "tenant", "AzureTenant", "one"),
        ("AzureSubscription", "resourcegroups", "AzureResourceGroup", "many"),
        ("AzureResourceGroup", "subscription", "AzureSubscription", "one"),
        ("AzureResourceGroup", "location", "AzureRegion", "one"),
        ("AzureResource", "location", "AzureRegion", "one"),
        ("AzureResource", "resourcegroup", "AzureResourceGroup", "one"),
        ("AzureVirtualNetwork", "location", "AzureRegion", "one"),
        ("AzureVirtualNetwork", "resourcegroup", "AzureResourceGroup", "one"),
        ("AzureVirtualNetwork", "address_space", "BuiltinIPPrefix", "many"),
        ("AzureVirtualNetwork", "subnets", "AzureVirtualNetworkSubnet", "many"),
        ("AzureVirtualNetworkSubnet", "virtualnetwork", "AzureVirtualNetwork", "one"),
        ("AzureVirtualNetworkSubnet", "address_prefixes", "BuiltinIPPrefix", "many"),
    )
    for kind, name, peer, cardinality in relationships:
        relationship = schemas[kind].get_relationship_or_none(name)
        if (
            relationship is None
            or relationship.peer != peer
            or relationship.cardinality != cardinality
        ):
            raise ValueError(f"{kind}.{name} must refer to {cardinality} {peer}")


def verify_tags(schemas) -> None:
    for kind in ("AzureTag", "AzureTaggable"):
        if kind not in schemas:
            raise ValueError(f"{kind} is missing")
    tag = schemas["AzureTag"]
    for name in ("key", "value"):
        attribute = tag.get_attribute_or_none(name)
        if attribute is None or attribute.kind != "Text" or attribute.optional:
            raise ValueError(f"AzureTag.{name} must be required Text")
    if not any(set(c) == {"owner", "key__value"} for c in tag.uniqueness_constraints):
        raise ValueError("AzureTag uniqueness must be scoped to owner and key")
    owner = tag.get_relationship("owner")
    if (
        owner.peer != "AzureTaggable"
        or owner.cardinality != "one"
        or owner.optional
        or owner.kind != "Parent"
    ):
        raise ValueError("AzureTag.owner must be one required AzureTaggable parent")
    supported = {"AzureSubscription", "AzureResourceGroup", "AzureVirtualNetwork"}
    for kind in (*supported, "AzureTaggable"):
        rel = schemas[kind].get_relationship_or_none("tags")
        if (
            rel is None
            or rel.peer != "AzureTag"
            or rel.cardinality != "many"
            or not rel.optional
            or rel.kind != "Component"
            or rel.max_count != 50
            or rel.identifier != owner.identifier
        ):
            raise ValueError(f"{kind}.tags must expose up to 50 owned Azure tags")
    for kind in AZURE_NODES:
        if ("AzureTaggable" in schemas[kind].inherit_from) != (kind in supported):
            raise ValueError(f"{kind} has incorrect Azure tag support")
    for kind in ("AzureRegion", "LocationGroup"):
        if schemas[kind].get_relationship("tags").peer != "BuiltinTag":
            raise ValueError(f"{kind} must preserve BuiltinTag labels")


def verify(client: InfrahubClientSync, branch: str) -> None:
    schemas = client.schema.all(branch=branch, refresh=True)
    verify_azure(schemas)
    verify_tags(schemas)
    expected = {
        "IpamNamespace": "BuiltinIPNamespace",
        "IpamPrefix": "BuiltinIPPrefix",
        "IpamIPAddress": "BuiltinIPAddress",
    }
    for kind, generic in expected.items():
        if kind not in schemas or generic not in schemas[kind].inherit_from:
            raise ValueError(f"{kind} must exist and inherit from {generic}")
    if "IpamVRF" not in schemas:
        raise ValueError("IpamVRF is missing")
    if "IpamRouteTarget" not in schemas:
        raise ValueError("IpamRouteTarget is missing")
    for relationship in ("import_rt", "export_rt"):
        if schemas["IpamVRF"].get_relationship(relationship).peer != "IpamRouteTarget":
            raise ValueError(f"IpamVRF.{relationship} must refer to IpamRouteTarget")
    if schemas["IpamVRF"].get_attribute("enforce_unique").kind != "Boolean":
        raise ValueError(
            "IpamVRF.enforce_unique must retain the upstream Boolean field"
        )
    for kind, field in (("IpamPrefix", "prefix"), ("IpamIPAddress", "address")):
        schema = schemas[kind]
        identity = {f"{field}__value", "ip_namespace"}
        if not any(
            set(constraint) == identity for constraint in schema.uniqueness_constraints
        ):
            raise ValueError(f"{kind} must scope address uniqueness to ip_namespace")
        namespace = schema.get_relationship("ip_namespace")
        if namespace.peer != "BuiltinIPNamespace":
            raise ValueError(f"{kind} must use the native IP namespace relationship")
        vrf = schema.get_relationship("vrf")
        if vrf.peer != "IpamVRF" or not vrf.optional or vrf.cardinality != "one":
            raise ValueError(f"{kind}.vrf must be optional and refer to one IpamVRF")
    # Verify API exposure as well as the REST schema, without creating sample data.
    counts = client.execute_graphql(
        query="{ IpamNamespace { count } IpamPrefix { count } "
        "IpamIPAddress { count } IpamVRF { count } IpamRouteTarget { count } "
        + " ".join(
            f"{kind} {{ count }}"
            for kind in (*AZURE_NODES, "LocationGroup", "AzureTag")
        )
        + " }",
        branch_name=branch,
    )
    print(f"IPAM and Azure schemas verified on branch {branch}.")
    for kind, result in counts.items():
        print(f"  {kind}: {result['count']} objects")


@app.command()
def main(
    branch: Annotated[str, typer.Option(help="Infrahub branch to inspect")],
) -> None:
    try:
        verify(InfrahubClientSync(), branch)
    except Exception as exc:
        # Avoid dumping SDK request/response details that could contain credentials.
        detail = str(exc) if type(exc) is ValueError else type(exc).__name__
        print(f"Schema verification failed: {detail}", file=sys.stderr)
        raise typer.Exit(code=1) from None


if __name__ == "__main__":
    app()
