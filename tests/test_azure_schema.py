"""Offline Azure contract checks combining upstream and local extensions."""

from pathlib import Path

import pytest
import yaml

from scripts.verify_schema import AZURE_NODES, verify_azure


@pytest.fixture
def schemas():
    from infrahub_sdk.schema import NodeSchemaAPI, GenericSchemaAPI

    definitions = {}
    for path in (
        Path("schemas/azure.yml"),
        Path("schemas/local/azure_management_groups.yml"),
    ):
        data = yaml.safe_load(path.read_text())
        for section in ("generics", "nodes"):
            for definition in data.get(section, []):
                kind = definition["namespace"] + definition["name"]
                if kind in definitions:
                    original = definitions[kind][1]
                    for field in ("attributes", "relationships"):
                        merged = {
                            item["name"]: item for item in original.get(field, [])
                        }
                        for item in definition.get(field, []):
                            merged[item["name"]] = {
                                **merged.get(item["name"], {}),
                                **item,
                            }
                        original[field] = list(merged.values())
                    if "display_label" in definition:
                        original["display_label"] = definition["display_label"]
                else:
                    definitions[kind] = (section, definition)
    nodes = {}
    for kind, (section, definition) in definitions.items():
        cls = GenericSchemaAPI if section == "generics" else NodeSchemaAPI
        if kind == "AzureManagementGroup":
            definition["hierarchy"] = "AzureManagementGroupHierarchy"
        nodes[kind] = cls(**definition)
    for node in nodes.values():
        node.relationships.extend(node.hierarchical_relationship_schemas)
        for parent in getattr(node, "inherit_from", []):
            node.relationships.extend(nodes[parent].relationships)

    return nodes


def test_combined_azure_contract(schemas):
    verify_azure(schemas)


@pytest.mark.parametrize(
    "kind", [*AZURE_NODES, "AzureResource", "AzureManagementGroupHierarchy"]
)
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
    schemas[kind].get_relationship(field).peer = "WrongModel"
    with pytest.raises(ValueError, match=f"{kind}.{field}"):
        verify_azure(schemas)


def test_missing_relationship(schemas):
    schemas["AzureVirtualNetwork"].relationships = [
        r for r in schemas["AzureVirtualNetwork"].relationships if r.name != "subnets"
    ]
    with pytest.raises(ValueError, match="AzureVirtualNetwork.subnets"):
        verify_azure(schemas)


def test_wrong_relationship_cardinality(schemas):
    schemas["AzureVirtualNetworkSubnet"].get_relationship(
        "address_prefixes"
    ).cardinality = "one"
    with pytest.raises(ValueError, match="address_prefixes"):
        verify_azure(schemas)


def test_missing_resource_inheritance(schemas):
    schemas["AzureVirtualNetwork"].inherit_from = []
    with pytest.raises(ValueError, match="inherit"):
        verify_azure(schemas)


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("hierarchy", None, "native hierarchy"),
        (
            "inherit_from",
            ["AzureManagementGroupHierarchy", "AzureResource"],
            "must not inherit",
        ),
        (
            "uniqueness_constraints",
            [["management_group_id__value"]],
            "scoped to tenant",
        ),
    ],
)
def test_management_group_contract(schemas, field, value, expected):
    setattr(schemas["AzureManagementGroup"], field, value)
    with pytest.raises(ValueError, match=expected):
        verify_azure(schemas)


def test_subscription_membership_stays_optional(schemas):
    schemas["AzureSubscription"].get_relationship("management_group").optional = False
    with pytest.raises(ValueError, match="optional must be True"):
        verify_azure(schemas)


def test_hierarchy_reads_paginated_sdk_responses(schemas, monkeypatch, capsys):
    from unittest.mock import MagicMock

    from infrahub_sdk import InfrahubClientSync

    from scripts.check_azure_hierarchy import check

    client = InfrahubClientSync()
    client.pagination_size = 1
    monkeypatch.setattr(client.schema, "get", lambda kind, **kwargs: schemas[kind])

    def page(kind, count, node=None):
        if node is not None:
            node["__typename"] = kind
        return {
            kind: {"count": count, "edges": [] if node is None else [{"node": node}]}
        }

    responses = [
        page(
            "AzureTenant",
            2,
            {
                "id": "t1",
                "tenant_id": {"value": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
            },
        ),
        page(
            "AzureTenant",
            2,
            {
                "id": "t2",
                "tenant_id": {"value": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"},
            },
        ),
        page("AzureTenant", 2),
        page(
            "AzureManagementGroup",
            2,
            {
                "id": "r1",
                "management_group_id": {
                    "value": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
                },
                "tenant": {"node": {"id": "t1", "__typename": "AzureTenant"}},
                "parent": {"node": None},
            },
        ),
        page(
            "AzureManagementGroup",
            2,
            {
                "id": "r2",
                "management_group_id": {
                    "value": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
                },
                "tenant": {"node": {"id": "t2", "__typename": "AzureTenant"}},
                "parent": {"node": None},
            },
        ),
        page("AzureManagementGroup", 2),
        page("AzureSubscription", 0),
    ]
    operation = MagicMock(side_effect=responses)
    monkeypatch.setattr(client, "execute_graphql", operation)
    assert check(client, "validation") == 0
    assert "2 tenants, 2 groups" in capsys.readouterr().out
    assert operation.call_count == 7
    assert all(
        call.kwargs["branch_name"] == "validation" for call in operation.call_args_list
    )
    assert "offset: 1" in operation.call_args_list[1].kwargs["query"]


@pytest.mark.parametrize(
    "kind,attribute",
    [
        ("AzureTenant", "tenant_id"),
        ("AzureSubscription", "subscription_id"),
        ("AzureManagementGroup", "management_group_id"),
    ],
)
def test_azure_identifier_optionality(schemas, kind, attribute):
    assert schemas[kind].get_attribute(attribute).optional
    schemas[kind].get_attribute(attribute).optional = False
    with pytest.raises(ValueError, match=attribute):
        verify_azure(schemas)
