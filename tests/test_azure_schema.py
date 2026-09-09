"""Offline Azure contract checks using the unchanged upstream schema."""

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts.verify_schema import AZURE_NODES, verify_azure


@pytest.fixture
def schemas():
    data = yaml.safe_load(Path("schemas/azure.yml").read_text())
    nodes = {}
    for definition in data["generics"] + data["nodes"]:
        relationships = {
            r["name"]: SimpleNamespace(**r) for r in definition.get("relationships", [])
        }
        for parent in definition.get("inherit_from", []):
            relationships.update(nodes[parent].relationships)
        nodes[definition["namespace"] + definition["name"]] = SimpleNamespace(
            inherit_from=definition.get("inherit_from", []),
            relationships=relationships,
            get_relationship_or_none=relationships.get,
        )
    return nodes


def test_upstream_azure_contract(schemas):
    verify_azure(schemas)


@pytest.mark.parametrize("kind", [*AZURE_NODES, "AzureResource"])
def test_missing_azure_model(schemas, kind):
    del schemas[kind]
    with pytest.raises(ValueError, match=f"{kind} is missing"):
        verify_azure(schemas)


@pytest.mark.parametrize(
    "kind,field",
    [
        ("AzureSubscription", "tenant"),
        ("AzureResourceGroup", "subscription"),
        ("AzureVirtualNetwork", "address_space"),
        ("AzureVirtualNetworkSubnet", "address_prefixes"),
    ],
)
def test_wrong_relationship_target(schemas, kind, field):
    schemas[kind].relationships[field].peer = "WrongModel"
    with pytest.raises(ValueError, match=f"{kind}.{field}"):
        verify_azure(schemas)


def test_missing_relationship(schemas):
    del schemas["AzureVirtualNetwork"].relationships["subnets"]
    with pytest.raises(ValueError, match="AzureVirtualNetwork.subnets"):
        verify_azure(schemas)


def test_wrong_relationship_cardinality(schemas):
    schemas["AzureVirtualNetworkSubnet"].relationships[
        "address_prefixes"
    ].cardinality = "one"
    with pytest.raises(ValueError, match="address_prefixes"):
        verify_azure(schemas)


def test_missing_resource_inheritance(schemas):
    schemas["AzureVirtualNetwork"].inherit_from = []
    with pytest.raises(ValueError, match="inherit"):
        verify_azure(schemas)
