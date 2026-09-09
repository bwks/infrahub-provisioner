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
    if "AzureResource" not in schemas["AzureVirtualNetwork"].inherit_from:
        raise ValueError("AzureVirtualNetwork must inherit from AzureResource")
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


def verify(client: InfrahubClientSync, branch: str) -> None:
    schemas = client.schema.all(branch=branch, refresh=True)
    verify_azure(schemas)
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
        + " ".join(f"{kind} {{ count }}" for kind in (*AZURE_NODES, "LocationGroup"))
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
