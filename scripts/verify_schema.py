"""Read-only verification of deployed IPAM and Azure schemas; creates no objects."""

import sys
from typing import Annotated

import typer
from infrahub_sdk import InfrahubClientSync

app = typer.Typer(help=__doc__, add_completion=False, pretty_exceptions_enable=False)

AZURE_NODES = (
    "AzureLocation",
    "AzureTenant",
    "AzureSubscription",
    "AzureResourceGroup",
    "AzureVirtualNetwork",
    "AzureVirtualNetworkSubnet",
)


def verify_azure(schemas) -> None:
    for kind in (*AZURE_NODES, "AzureResource"):
        if kind not in schemas:
            raise ValueError(f"{kind} is missing")
    if "AzureResource" not in schemas["AzureVirtualNetwork"].inherit_from:
        raise ValueError("AzureVirtualNetwork must inherit from AzureResource")
    relationships = (
        ("AzureTenant", "subscriptions", "AzureSubscription", "many"),
        ("AzureSubscription", "tenant", "AzureTenant", "one"),
        ("AzureSubscription", "resourcegroups", "AzureResourceGroup", "many"),
        ("AzureResourceGroup", "subscription", "AzureSubscription", "one"),
        ("AzureResourceGroup", "location", "AzureLocation", "one"),
        ("AzureResource", "location", "AzureLocation", "one"),
        ("AzureResource", "resourcegroup", "AzureResourceGroup", "one"),
        ("AzureVirtualNetwork", "location", "AzureLocation", "one"),
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
        + " ".join(f"{kind} {{ count }}" for kind in AZURE_NODES)
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
