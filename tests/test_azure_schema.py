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
        Path("schemas/local/network_policy.yml"),
        Path("schemas/local/resource_tags.yml"),
        Path("schemas/local/virtual_networks.yml"),
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
        # SDK 1.22 exposes the server's legacy attribute fields, not parameters.
        for attribute in definition.get("attributes", []):
            attribute.update(attribute.get("parameters", {}))
        nodes[kind] = cls(**definition)
    for node in nodes.values():
        node.relationships.extend(node.hierarchical_relationship_schemas)
        for parent in getattr(node, "inherit_from", []):
            for field in ("relationships", "attributes"):
                inherited = {
                    item.name: item.model_copy(deep=True)
                    for item in getattr(nodes[parent], field)
                }
                inherited.update({item.name: item for item in getattr(node, field)})
                setattr(node, field, list(inherited.values()))

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


def test_subscription_seed_reads_all_sdk_pages(schemas, monkeypatch, capsys):
    from types import SimpleNamespace as NS
    from unittest.mock import MagicMock
    from infrahub_sdk import InfrahubClientSync
    from scripts.seed_azure_hierarchy import seed

    client = InfrahubClientSync()
    client.pagination_size = 1
    monkeypatch.setattr(client.schema, "get", lambda kind, **kwargs: schemas[kind])
    tenant = NS(id="t", name=NS(value="fake-corp"), tenant_id=NS(value=None))
    groups = [
        NS(
            id="root",
            management_group_id=NS(value=None),
            display_name=NS(value="Root"),
            tenant=NS(id="t"),
            parent=NS(id=None),
        ),
        NS(
            id="security",
            management_group_id=NS(value="security"),
            display_name=NS(value="Security"),
            tenant=NS(id="t"),
            parent=NS(id="root"),
        ),
    ]
    all_nodes = client.all

    def read(kind, **kwargs):
        if kind == "AzureTenant":
            return [tenant]
        if kind == "AzureManagementGroup":
            return groups
        return all_nodes(kind=kind, **kwargs)

    monkeypatch.setattr(client, "all", read)
    responses = []
    for i, name in enumerate(["Security", "Unmanaged extra"]):
        node = {
            "id": f"s{i}",
            "__typename": "AzureSubscription",
            "name": {"value": name},
            "subscription_id": {"value": None},
            "tenant": {"node": {"id": "t", "__typename": "AzureTenant"}},
            "management_group": {
                "node": {"id": "security", "__typename": "AzureManagementGroup"}
            },
        }
        responses.append({"AzureSubscription": {"count": 2, "edges": [{"node": node}]}})
    responses.append({"AzureSubscription": {"count": 2, "edges": []}})
    operation = MagicMock(side_effect=responses)
    monkeypatch.setattr(client, "execute_graphql", operation)
    catalog = {
        "tenant": {"name": "fake-corp"},
        "root": {"display_name": "Root"},
        "management_groups": [
            {
                "management_group_id": "security",
                "display_name": "Security",
                "parent": None,
            }
        ],
        "subscriptions": [
            {"name": "Security", "management_group": "security", "status": "planned"}
        ],
    }
    assert seed(client, "validation", catalog) == 0
    assert "missing=0, skipped=4" in capsys.readouterr().out
    assert "offset: 1" in operation.call_args_list[1].kwargs["query"]
    assert all(
        call.kwargs["branch_name"] == "validation" for call in operation.call_args_list
    )


def test_tag_schema_contract(schemas):
    from scripts.verify_schema import verify_tags

    verify_tags(schemas)


@pytest.mark.parametrize("edit", ["owner", "unique", "limit", "unsupported", "builtin"])
def test_invalid_tag_schema(schemas, edit):
    from scripts.verify_schema import verify_tags

    if edit == "owner":
        schemas["AzureTag"].get_relationship("owner").optional = True
    elif edit == "unique":
        schemas["AzureTag"].uniqueness_constraints = []
    elif edit == "limit":
        schemas["AzureTaggable"].get_relationship("tags").max_count = 0
    elif edit == "unsupported":
        schemas["AzureTenant"].inherit_from = ["AzureTaggable"]
    else:
        schemas["AzureRegion"].get_relationship("tags").peer = "AzureTag"
    with pytest.raises(ValueError):
        verify_tags(schemas)


def test_tag_checker_pagination(schemas, monkeypatch, capsys):
    from infrahub_sdk import InfrahubClientSync
    from unittest.mock import MagicMock
    from scripts.check_azure_tags import check

    client = InfrahubClientSync()
    client.pagination_size = 1
    schemas["AzureTaggable"].used_by = [
        "AzureSubscription",
        "AzureResourceGroup",
        "AzureVirtualNetwork",
    ]
    monkeypatch.setattr(client.schema, "get", lambda kind, **kwargs: schemas[kind])
    responses = [
        {
            "AzureTaggable": {
                "count": 1,
                "edges": [{"node": {"id": "owner", "__typename": "AzureSubscription"}}],
            }
        },
        {"AzureTaggable": {"count": 1, "edges": []}},
    ]
    for i in range(2):
        responses.append(
            {
                "AzureTag": {
                    "count": 2,
                    "edges": [
                        {
                            "node": {
                                "id": f"tag{i}",
                                "__typename": "AzureTag",
                                "key": {"value": f"key{i}"},
                                "value": {"value": ""},
                                "owner": {
                                    "node": {
                                        "id": "owner",
                                        "__typename": "AzureSubscription",
                                    }
                                },
                            }
                        }
                    ],
                }
            }
        )
    responses.append({"AzureTag": {"count": 2, "edges": []}})
    operation = MagicMock(side_effect=responses)
    monkeypatch.setattr(client, "execute_graphql", operation)
    assert check(client, "test") == 0
    assert "2 tags" in capsys.readouterr().out
    assert any(
        "offset: 1" in c.kwargs["query"] and "AzureTag(" in c.kwargs["query"]
        for c in operation.call_args_list
    )
    assert all(c.kwargs["branch_name"] == "test" for c in operation.call_args_list)


@pytest.mark.parametrize(
    "edit", ["missing", "region_scope", "writable", "optional", "global"]
)
def test_resource_group_uniqueness_contract(schemas, edit):
    rg = schemas["AzureResourceGroup"]
    if edit == "missing":
        rg.uniqueness_constraints = []
    elif edit == "region_scope":
        rg.uniqueness_constraints = [["location", "name_key__value"]]
    else:
        setattr(
            rg.get_attribute("name_key"),
            {"writable": "read_only", "optional": "optional", "global": "unique"}[edit],
            edit != "writable",
        )
    with pytest.raises(ValueError, match="AzureResourceGroup"):
        verify_azure(schemas)


def test_normalized_resource_group_identity():
    from jinja2 import Environment

    data = yaml.safe_load(Path("schemas/local/resource_tags.yml").read_text())
    rg = next(n for n in data["nodes"] if n["name"] == "ResourceGroup")
    attribute = next(a for a in rg["attributes"] if a["name"] == "name_key")
    assert attribute["computed_attribute"]["kind"] == "Jinja2"
    template = Environment().from_string(
        attribute["computed_attribute"]["jinja2_template"]
    )

    def identity(subscription, name):
        return subscription, template.render(name__value=name)

    assert identity("sub1", "RG-Conn-PRD-Network") == identity(
        "sub1", "rg-conn-prd-network"
    )
    assert identity("sub1", "rg-conn-prd-network") != identity(
        "sub2", "rg-conn-prd-network"
    )
    assert identity("sub1", "rg-network") != identity("sub1", "rg-other")


@pytest.mark.parametrize(
    "name,valid",
    [
        ("a", False),
        ("ab", True),
        ("a" * 64, True),
        ("a" * 65, False),
        ("A.b-c_9", True),
        ("a_", True),
        ("_ab", False),
        ("ab-", False),
        ("ab.", False),
        ("a b", False),
        ("a/b", False),
        ("éa", False),
        ("ab\n", False),
    ],
)
def test_vnet_name_rules(schemas, name, valid):
    import re

    attribute = schemas["AzureVirtualNetwork"].get_attribute("name")
    accepted = (
        attribute.min_length <= len(name) <= attribute.max_length
        and re.search(attribute.regex, name) is not None
    )
    assert accepted == valid


@pytest.mark.parametrize("field", ["resourcegroup", "location", "address_space"])
def test_vnet_required_relationships(schemas, field):
    schemas["AzureVirtualNetwork"].get_relationship(field).optional = True
    with pytest.raises(ValueError, match=f"AzureVirtualNetwork.{field}"):
        verify_azure(schemas)


def test_vnet_requires_nonempty_address_space(schemas):
    schemas["AzureVirtualNetwork"].get_relationship("address_space").min_count = 0
    with pytest.raises(ValueError, match="AzureVirtualNetwork.address_space"):
        verify_azure(schemas)


@pytest.mark.parametrize(
    "scope", [[], [["location", "name_key__value"]], [["name_key__value"]]]
)
def test_vnet_uniqueness_scope(schemas, scope):
    schemas["AzureVirtualNetwork"].uniqueness_constraints = scope
    with pytest.raises(ValueError, match="AzureVirtualNetwork uniqueness"):
        verify_azure(schemas)


@pytest.mark.parametrize(
    "field,value", [("optional", True), ("read_only", False), ("unique", True)]
)
def test_vnet_normalized_name_contract(schemas, field, value):
    setattr(schemas["AzureVirtualNetwork"].get_attribute("name_key"), field, value)
    with pytest.raises(ValueError, match="AzureVirtualNetwork.name_key"):
        verify_azure(schemas)


def test_vnet_normalized_identity():
    from jinja2 import Environment

    data = yaml.safe_load(Path("schemas/local/virtual_networks.yml").read_text())
    key = next(a for a in data["nodes"][0]["attributes"] if a["name"] == "name_key")
    assert key["computed_attribute"]["kind"] == "Jinja2"
    template = Environment().from_string(key["computed_attribute"]["jinja2_template"])

    def identity(group, name):
        return group, template.render(name__value=name)

    assert identity("rg1", "VNet-Prod") == identity("rg1", "vnet-prod")
    assert identity("rg1", "vnet-prod") != identity("rg2", "vnet-prod")


def test_vnet_overrides_do_not_change_other_resource_types(schemas):
    vnet = schemas["AzureVirtualNetwork"]
    assert not vnet.get_relationship("location").optional
    assert schemas["AzureResource"].get_relationship("location").optional
    assert schemas["AzureResource"].get_attribute("name").regex is None
    assert (
        not schemas["AzureVirtualNetworkSubnet"]
        .get_relationship("address_prefixes")
        .optional
    )
    assert sum(r.name == "location" for r in vnet.relationships) == 1


@pytest.mark.parametrize(
    "kind",
    [
        "AzureVirtualNetworkSubnet",
        "AzureNetworkSecurityGroup",
        "AzureNetworkSecurityRule",
        "AzureRouteTable",
        "AzureRoute",
    ],
)
@pytest.mark.parametrize("edit", ["scope", "writable", "name"])
def test_network_schema_identity(schemas, kind, edit):
    node = schemas[kind]
    if edit == "scope":
        node.uniqueness_constraints = []
    elif edit == "writable":
        node.get_attribute("name_key").read_only = False
    else:
        node.get_attribute("name").max_length = 81
    with pytest.raises(ValueError, match=kind):
        verify_azure(schemas)


@pytest.mark.parametrize(
    "kind,field",
    [
        ("AzureNetworkSecurityGroup", "location"),
        ("AzureRouteTable", "location"),
        ("AzureVirtualNetworkSubnet", "address_prefixes"),
    ],
)
def test_network_schema_required_links(schemas, kind, field):
    schemas[kind].get_relationship(field).optional = True
    with pytest.raises(ValueError, match=kind):
        verify_azure(schemas)


def test_network_schema_priority_parameters_and_computation():
    from jinja2 import Environment

    definitions = yaml.safe_load(Path("schemas/local/network_policy.yml").read_text())[
        "nodes"
    ]
    rule = next(n for n in definitions if n["name"] == "NetworkSecurityRule")
    priority = next(a for a in rule["attributes"] if a["name"] == "priority")
    assert priority["parameters"] == {"min_value": 100, "max_value": 4096}
    for node in definitions:
        key = next(a for a in node["attributes"] if a["name"] == "name_key")
        assert key["computed_attribute"]["kind"] == "Jinja2"
        template = Environment().from_string(
            key["computed_attribute"]["jinja2_template"]
        )
        assert template.render(name__value="MiXeD_Name") == "mixed_name"


@pytest.mark.parametrize("kind", ["AzureNetworkSecurityGroup", "AzureRouteTable"])
def test_network_tag_owners(schemas, kind):
    from scripts.check_azure_tags import Tag, validate
    from scripts.verify_schema import verify_tags

    verify_tags(schemas)
    assert (
        validate([Tag("tag", "Environment", "Production", "owner")], {"owner": kind})
        == []
    )
