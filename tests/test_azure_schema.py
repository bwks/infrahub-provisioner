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
        Path("schemas/location.yml"),
        Path("schemas/local/azure_management_groups.yml"),
        Path("schemas/local/azure_status.yml"),
        Path("schemas/local/cloud_locations.yml"),
    ):
        data = yaml.safe_load(path.read_text())
        for section in ("generics", "nodes"):
            for definition in data.get(section, []):
                kind = definition["namespace"] + definition["name"]
                if definition.get("state") == "absent":
                    definitions.pop(kind, None)
                    continue
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
                    for field, value in definition.items():
                        if field not in ("attributes", "relationships"):
                            original[field] = value
                else:
                    definitions[kind] = (section, definition)
    nodes = {}
    for kind, (section, definition) in definitions.items():
        cls = GenericSchemaAPI if section == "generics" else NodeSchemaAPI
        if kind == "AzureManagementGroup":
            definition["hierarchy"] = "AzureManagementGroupHierarchy"
        if kind in ("AzureRegion", "LocationGroup"):
            definition["hierarchy"] = "LocationGeneric"
        nodes[kind] = cls(**definition)
    for node in nodes.values():
        node.relationships.extend(node.hierarchical_relationship_schemas)
        for parent in getattr(node, "inherit_from", []):
            node.relationships.extend(nodes[parent].relationships)
            node.attributes.extend(nodes[parent].attributes)

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


@pytest.mark.parametrize(
    "kind", [*AZURE_NODES, "AzureResource", "AzureManagementGroupHierarchy"]
)
def test_azure_status_missing(schemas, kind):
    schemas[kind].attributes = [
        a for a in schemas[kind].attributes if a.name != "status"
    ]
    with pytest.raises(ValueError, match=f"{kind}.status"):
        verify_azure(schemas)


@pytest.mark.parametrize(
    "field,value", [("default_value", "active"), ("optional", False), ("choices", [])]
)
def test_azure_status_contract(schemas, field, value):
    setattr(schemas["AzureSubscription"].get_attribute("status"), field, value)
    with pytest.raises(ValueError, match="AzureSubscription.status"):
        verify_azure(schemas)


def test_region_status_default_is_unmanaged(schemas):
    assert schemas["AzureRegion"].get_attribute("status").default_value == "unmanaged"
    schemas["AzureRegion"].get_attribute("status").default_value = "planned"
    with pytest.raises(ValueError, match="AzureRegion.status"):
        verify_azure(schemas)


def test_status_labels_and_colors(schemas):
    expected = {
        "planned": ("Planned", "#a855f7"),
        "active": ("Active", "#00d25b"),
        "reserved": ("Reserved", "#4d90fe"),
        "deprecated": ("Deprecated", "#e04040"),
        "unmanaged": ("Unmanaged", "#9ca3af"),
    }
    for kind in AZURE_NODES:
        assert {
            c["name"]: (c["label"], c["color"])
            for c in schemas[kind].get_attribute("status").choices
        } == expected


@pytest.mark.parametrize("kind", ["AzureRegion", "LocationGroup"])
@pytest.mark.parametrize(
    "field,value", [("hierarchy", None), ("display_label", "name__value")]
)
def test_cloud_schema_contract(schemas, kind, field, value):
    setattr(schemas[kind], field, value)
    with pytest.raises(ValueError, match=kind):
        verify_azure(schemas)


def test_old_location_must_be_absent(schemas):
    schemas["AzureLocation"] = schemas["AzureRegion"]
    with pytest.raises(ValueError, match="AzureLocation must be retired"):
        verify_azure(schemas)


def test_cloud_seed_uses_sdk_pagination(schemas, monkeypatch, capsys):
    from unittest.mock import MagicMock
    from infrahub_sdk import InfrahubClientSync
    from scripts.seed_cloud_locations import seed

    client = InfrahubClientSync()
    client.pagination_size = 1
    schemas["LocationGeneric"].used_by = ["LocationGroup", "AzureRegion"]
    monkeypatch.setattr(client.schema, "get", lambda kind, **kwargs: schemas[kind])
    entries = [
        {
            "kind": "LocationGroup",
            "name": "cloud",
            "display_name": "Cloud",
            "parent": None,
        },
        {
            "kind": "LocationGroup",
            "name": "cloud-azure",
            "display_name": "Azure",
            "parent": "cloud",
        },
    ]
    responses = []
    for index, entry in enumerate(entries):
        node = {
            "id": str(index),
            "__typename": "LocationGroup",
            "name": {"value": entry["name"]},
            "display_name": {"value": entry["display_name"]},
            "parent": {
                "node": None
                if index == 0
                else {"id": "0", "__typename": "LocationGroup"}
            },
        }
        responses.append({"LocationGeneric": {"count": 2, "edges": [{"node": node}]}})
    responses.append({"LocationGeneric": {"count": 2, "edges": []}})
    # The generic response deliberately omits subtype-only display_name.
    concrete_pages = [{"LocationGroup": page["LocationGeneric"]} for page in responses]
    from copy import deepcopy

    responses = deepcopy(responses)
    for page in responses:
        for edge in page["LocationGeneric"]["edges"]:
            edge["node"].pop("display_name", None)
    responses.extend(concrete_pages)
    responses.append({"AzureRegion": {"count": 0, "edges": []}})
    operation = MagicMock(side_effect=responses)
    monkeypatch.setattr(client, "execute_graphql", operation)
    assert seed(client, "validation", entries) == 0
    assert "missing=0, skipped=2" in capsys.readouterr().out
    assert "offset: 1" in operation.call_args_list[1].kwargs["query"]
    assert all(
        call.kwargs["branch_name"] == "validation" for call in operation.call_args_list
    )
