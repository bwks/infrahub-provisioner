"""Offline topology, routing, and paginated read regressions."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from scripts import check_azure_virtual_wan as v


@pytest.fixture
def inventory():
    data = {k: [] for k in v.REFERENCES}
    for kind, id_ in [(v.TENANT, "tenant"), (v.REGION, "region"), (v.NAMESPACE, "ns")]:
        data[kind] = [dict(id=id_, name=id_)]
    data[v.SUB] = [dict(id="sub", name="sub", tenant="tenant")]
    data[v.RG] = [dict(id="rg", name="rg", subscription="sub")]
    data[v.WAN] = [
        dict(
            id="wan",
            name="wan",
            resourcegroup="rg",
            location="region",
            wan_type="Standard",
        )
    ]
    for i in range(2):
        h = f"h{i}"
        data[v.HUB].append(
            dict(
                id=h,
                name=h,
                resourcegroup="rg",
                location="region",
                virtual_wan="wan",
                address_space=f"p{i}",
                router_capacity=2,
                routing_preference="ExpressRoute",
            )
        )
        data[v.PREFIX].append(
            dict(id=f"p{i}", prefix=f"10.{i}.0.0/24", ip_namespace="ns")
        )
        for name, labels in [
            ("defaultRouteTable", ["Default"]),
            ("noneRouteTable", []),
            ("custom", ["Shared"]),
        ]:
            data[v.TABLE].append(dict(id=h + name, name=name, hub=h, labels=labels))
        data[v.VNET].append(
            dict(
                id=f"v{i}",
                name=f"v{i}",
                resourcegroup="rg",
                location="region",
                address_space=[f"vp{i}"],
            )
        )
        data[v.PREFIX].append(
            dict(id=f"vp{i}", prefix=f"10.{i + 2}.0.0/24", ip_namespace="ns")
        )
        data[v.CONNECTION].append(
            dict(
                id=f"c{i}",
                name=f"c{i}",
                hub=h,
                virtual_network=f"v{i}",
                associated_route_table=h + "defaultRouteTable",
                propagated_route_tables=[],
                propagation_labels=["Default"],
                propagate_to_none=False,
            )
        )
    return data


def test_valid_multi_hub(inventory):
    assert v.validate(inventory) == []


def test_empty():
    assert v.validate({}) == []


@pytest.mark.parametrize(
    "none,labels,tables",
    [
        (True, [], []),
        (False, ["Shared"], []),
        (False, [], ["h0custom"]),
        (False, ["Default", "Shared"], ["h0custom"]),
    ],
)
def test_propagation_modes(inventory, none, labels, tables):
    inventory[v.CONNECTION][0].update(
        propagate_to_none=none,
        propagation_labels=labels,
        propagated_route_tables=tables,
    )
    assert v.validate(inventory) == []


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("propagate_to_none", True, "requires empty"),
        ("propagate_to_none", None, "must be Boolean"),
        ("propagation_labels", [], "select propagation"),
        ("propagation_labels", ["default"], "matches no table"),
        ("propagation_labels", ["Default", "Default"], "duplicate labels"),
        ("propagation_labels", "Default", "JSON list"),
        ("propagation_labels", [3], "JSON list"),
        ("propagated_route_tables", ["missing"], "missing propagated"),
        ("propagated_route_tables", ["h1custom"], "another hub"),
        ("propagated_route_tables", ["h0noneRouteTable"], "use propagate_to_none"),
        ("associated_route_table", "h1custom", "another hub"),
        ("associated_route_table", "h0noneRouteTable", "cannot associate"),
        ("associated_route_table", "missing", "missing AzureVirtualHubRouteTable"),
        ("virtual_network", "missing", "missing AzureVirtualNetwork"),
        ("hub", "missing", "missing AzureVirtualHub"),
        ("name", "-invalid", "invalid resource name"),
    ],
)
def test_connection_errors(inventory, field, value, message):
    inventory[v.CONNECTION][0][field] = value
    assert any(message in f for f in v.validate(inventory))


def test_duplicate_attachment(inventory):
    inventory[v.CONNECTION][1]["virtual_network"] = "v0"
    assert any("already attached" in f for f in v.validate(inventory))


def test_scoped_names(inventory):
    table = deepcopy(inventory[v.TABLE][2])
    table.update(id="duplicate", name="CUSTOM")
    inventory[v.TABLE].append(table)
    assert any("duplicate scoped name" in f for f in v.validate(inventory))


@pytest.mark.parametrize(
    "cidr,valid",
    [
        ("10.0.0.0/24", True),
        ("10.0.0.0/23", True),
        ("10.0.0.0/25", False),
        ("2001:db8::/48", False),
        ("10.0.0.1/24", False),
        ("invalid", False),
    ],
)
def test_hub_prefix_boundary(inventory, cidr, valid):
    inventory[v.PREFIX][0]["prefix"] = cidr
    assert (not v.validate(inventory)) == valid


@pytest.mark.parametrize("capacity", [1, 51, True, 2.5, None])
def test_capacity(inventory, capacity):
    inventory[v.HUB][0]["router_capacity"] = capacity
    assert any("router_capacity" in f for f in v.validate(inventory))


@pytest.mark.parametrize("capacity", [2, 50])
def test_capacity_valid(inventory, capacity):
    inventory[v.HUB][0]["router_capacity"] = capacity
    assert not v.validate(inventory)


@pytest.mark.parametrize("name", ["defaultRouteTable", "noneRouteTable"])
def test_missing_builtins(inventory, name):
    inventory[v.TABLE] = [t for t in inventory[v.TABLE] if t["id"] != "h0" + name]
    assert any("missing built-in" in f for f in v.validate(inventory))


def test_overlap_ignores_namespaces(inventory):
    inventory[v.NAMESPACE].append(dict(id="other", name="other"))
    inventory[v.PREFIX][-1].update(prefix="10.0.0.0/24", ip_namespace="other")
    assert any("overlaps" in f for f in v.validate(inventory))


def test_separate_wans_allow_overlap(inventory):
    inventory[v.WAN].append(dict(inventory[v.WAN][0], id="wan2", name="wan2"))
    inventory[v.HUB][1]["virtual_wan"] = "wan2"
    inventory[v.PREFIX][-1]["prefix"] = "10.0.0.0/24"
    assert not v.validate(inventory)


def test_cross_tenant_vnet_allowed_but_not_hub(inventory):
    inventory[v.TENANT].append(dict(id="t2", name="t2"))
    inventory[v.SUB].append(dict(id="s2", name="s2", tenant="t2"))
    inventory[v.RG].append(dict(id="rg2", name="rg2", subscription="s2"))
    inventory[v.VNET][0]["resourcegroup"] = "rg2"
    assert not v.validate(inventory)
    inventory[v.HUB][0]["resourcegroup"] = "rg2"
    assert any("same subscription" in f for f in v.validate(inventory))


def test_cli(monkeypatch, inventory):
    monkeypatch.setattr(v, "InfrahubClientSync", Mock())
    monkeypatch.setattr(v, "read_inventory", lambda *_: inventory)
    runner = CliRunner()
    assert runner.invoke(v.app, ["--branch", "test"]).exit_code == 0
    inventory[v.WAN][0]["wan_type"] = "Basic"
    result = runner.invoke(v.app, ["--branch", "test"])
    assert result.exit_code == 1
    assert "wan" in result.output and "Standard" in result.output
    monkeypatch.setattr(v, "read_inventory", lambda *_: {})
    # Reader normally includes empty lists for every kind.
    monkeypatch.setattr(v, "read_inventory", lambda *_: {k: [] for k in v.REFERENCES})
    result = runner.invoke(v.app, ["--branch", "test"])
    assert result.exit_code == 0 and "empty" in result.output
    monkeypatch.setattr(v, "read_inventory", Mock(side_effect=RuntimeError("secret")))
    result = runner.invoke(v.app, ["--branch", "test"])
    assert (
        result.exit_code == 1
        and "read failed" in result.output
        and "secret" not in result.output
    )


def test_paginated_reads(inventory):
    client = Mock()

    def all_nodes(kind, **kwargs):
        assert kwargs == dict(
            branch="test",
            include=v.ATTRIBUTES.get(kind, ["name"]) + list(v.REFERENCES[kind]),
            populate_store=False,
        )
        return [
            SimpleNamespace(
                id=n["id"],
                **{
                    a: SimpleNamespace(value=n[a])
                    for a in v.ATTRIBUTES.get(kind, ["name"])
                },
                **{p: SimpleNamespace(id=n[p]) for p in v.REFERENCES[kind]},
            )
            for n in inventory[kind]
        ]

    client.all.side_effect = all_nodes

    def graphql(query, variables, branch_name):
        assert branch_name == "test"
        kind, field = (
            (v.VNET, "address_space")
            if v.VNET + "(" in query
            else (v.CONNECTION, "propagated_route_tables")
        )
        ids = next(n[field] for n in inventory[kind] if n["id"] == variables["id"])
        # Force several pages regardless of requested limit.
        page = ids[variables["offset"] : variables["offset"] + 1]
        return {
            kind: {
                "edges": [
                    {
                        "node": {
                            field: {
                                "count": len(ids),
                                "edges": [{"node": {"id": id_}} for id_ in page],
                            }
                        }
                    }
                ]
            }
        }

    client.execute_graphql.side_effect = graphql
    inventory[v.CONNECTION][0]["propagated_route_tables"] = [
        "h0custom",
        "h0defaultRouteTable",
    ]
    assert v.read_inventory(client, "test") == inventory
    assert client.execute_graphql.call_count == 5
