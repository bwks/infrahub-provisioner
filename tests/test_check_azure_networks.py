"""Network-policy scenarios are offline fixtures, never live seed data."""

from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from scripts import check_azure_networks as m


@pytest.fixture
def inventory():
    return {
        "AzureTenant": [dict(id="tenant", name="tenant")],
        "AzureSubscription": [
            dict(id="subscription", name="subscription", tenant="tenant")
        ],
        "AzureResourceGroup": [
            dict(id="rg", name="rg", subscription="subscription"),
            dict(id="other-rg", name="other", subscription="subscription"),
        ],
        "AzureRegion": [dict(id="region", name="region")],
        "IpamNamespace": [dict(id="namespace", name="namespace")],
        "BuiltinIPPrefix": [
            dict(id="v4", prefix="10.0.0.0/16", ip_namespace="namespace"),
            dict(id="s4", prefix="10.0.1.0/24", ip_namespace="namespace"),
            dict(id="v6", prefix="2001:db8::/48", ip_namespace="namespace"),
            dict(id="s6", prefix="2001:db8:0:1::/64", ip_namespace="namespace"),
        ],
        "AzureVirtualNetwork": [
            dict(
                id="vnet",
                name="test-vnet",
                resourcegroup="rg",
                location="region",
                address_space=["v4", "v6"],
            )
        ],
        "AzureVirtualNetworkSubnet": [
            dict(
                id="subnet",
                name="subnet",
                virtualnetwork="vnet",
                address_prefixes=["s4", "s6"],
                network_security_group="nsg",
                route_table="table",
            )
        ],
        "AzureNetworkSecurityGroup": [
            dict(id="nsg", name="nsg", resourcegroup="other-rg", location="region")
        ],
        "AzureRouteTable": [
            dict(
                id="table",
                name="table",
                resourcegroup="other-rg",
                location="region",
                disable_bgp_route_propagation=False,
            )
        ],
        "AzureNetworkSecurityRule": [
            dict(
                id="rule",
                name="allow-https",
                network_security_group="nsg",
                priority=100,
                direction="inbound",
                access="allow",
                protocol="tcp",
                source_addresses="10.1.0.0/16, 10.2.1.1",
                destination_addresses="VirtualNetwork",
                source_ports="*",
                destination_ports="443,1000-2000",
                description=None,
            )
        ],
        "AzureRoute": [
            dict(
                id="route",
                name="default-route",
                route_table="table",
                address_prefix="0.0.0.0/0",
                next_hop_type="virtual_appliance",
                next_hop_ip_address="10.0.1.4",
            )
        ],
    }


def test_valid_dual_stack_and_cross_resource_group(inventory):
    assert m.validate(inventory) == []
    for n in inventory["AzureVirtualNetworkSubnet"]:
        n["network_security_group"] = n["route_table"] = None
    assert m.validate(inventory) == []
    assert m.validate({}) == []


@pytest.mark.parametrize(
    "kind,field", [(kind, parent) for kind, (parent, _) in m.PARENTS.items()]
)
def test_missing_parents(inventory, kind, field):
    inventory[kind][0][field] = "missing"
    assert any("missing" in f for f in m.validate(inventory))


@pytest.mark.parametrize("value", ["10.1.0.0/24", "2001:db9::/64", "10.0.1.1/24"])
def test_bad_containment(inventory, value):
    inventory["BuiltinIPPrefix"][1]["prefix"] = value
    assert m.validate(inventory)


def test_namespace_mismatch(inventory):
    inventory["IpamNamespace"].append(dict(id="other", name="other"))
    inventory["BuiltinIPPrefix"][1]["ip_namespace"] = "other"
    assert any("namespace" in f for f in m.validate(inventory))


def test_overlap_and_adjacent_siblings(inventory):
    other = dict(
        inventory["AzureVirtualNetworkSubnet"][0],
        id="sibling",
        name="sibling",
        address_prefixes=["s4"],
    )
    inventory["AzureVirtualNetworkSubnet"].append(other)
    assert any("overlaps" in f for f in m.validate(inventory))
    inventory["BuiltinIPPrefix"].append(
        dict(id="sibling-prefix", prefix="10.0.2.0/24", ip_namespace="namespace")
    )
    other["address_prefixes"] = ["sibling-prefix"]
    assert m.validate(inventory) == []


@pytest.mark.parametrize("kind", ["AzureNetworkSecurityGroup", "AzureRouteTable"])
@pytest.mark.parametrize("change", ["region", "subscription"])
def test_association_mismatch(inventory, kind, change):
    if change == "region":
        inventory["AzureRegion"].append(dict(id="other-region", name="other-region"))
        inventory[kind][0]["location"] = "other-region"
    else:
        inventory["AzureSubscription"].append(
            dict(id="other-sub", name="other-sub", tenant="tenant")
        )
        inventory["AzureResourceGroup"][1]["subscription"] = "other-sub"
    assert any("subscription or region differs" in f for f in m.validate(inventory))


@pytest.mark.parametrize("kind", list(m.PARENTS))
def test_case_insensitive_scoped_names(inventory, kind):
    original = inventory[kind][0]
    duplicate = dict(original, id="duplicate", name=original["name"].upper())
    inventory[kind].append(duplicate)
    assert any("duplicate name" in f for f in m.validate(inventory))


@pytest.mark.parametrize("value", ["", "_bad", "bad-", "a" * 81, "bad\n"])
def test_invalid_names(inventory, value):
    inventory["AzureNetworkSecurityGroup"][0]["name"] = value
    assert any("invalid Azure name" in f for f in m.validate(inventory))


@pytest.mark.parametrize("value", [99, 4097, 100.5, True, None])
def test_priority_boundaries(inventory, value):
    inventory["AzureNetworkSecurityRule"][0]["priority"] = value
    assert any("priority must" in f for f in m.validate(inventory))


def test_priority_scope_and_boundaries(inventory):
    rule = inventory["AzureNetworkSecurityRule"][0]
    inventory["AzureNetworkSecurityRule"].append(dict(rule, id="r2", name="other-rule"))
    assert any("duplicate priority" in f for f in m.validate(inventory))
    inventory["AzureNetworkSecurityRule"][1]["direction"] = "outbound"
    assert m.validate(inventory) == []
    rule["priority"] = 4096
    assert m.validate(inventory) == []


@pytest.mark.parametrize(
    "value",
    [
        "*",
        "VirtualNetwork",
        "Storage.AustraliaEast",
        "10.0.0.0/8,192.0.2.1",
        "2001:db8::/32,2001:db8::1",
    ],
)
def test_valid_address_expressions(value):
    m.address_expression(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        None,
        "*,10.0.0.0/8",
        "Internet,VirtualNetwork",
        "Internet,10.0.0.0/8",
        "10.0.0.0/8,",
        "10.0.0.1/8",
        "999.1.1.1",
    ],
)
def test_invalid_address_expressions(value):
    with pytest.raises(ValueError):
        m.address_expression(value)


@pytest.mark.parametrize("value", ["*", "0", "65535", "0-65535", "80,443,1000-2000"])
def test_valid_ports(value):
    m.port_expression(value)


@pytest.mark.parametrize(
    "value", ["", None, "65536", "1-0", "80,", "*,443", "-1", "1.5", "a"]
)
def test_invalid_ports(value):
    with pytest.raises(ValueError):
        m.port_expression(value)


@pytest.mark.parametrize(
    "hop", ["internet", "none", "virtual_network_gateway", "vnet_local"]
)
def test_next_hop_constraints(inventory, hop):
    route = inventory["AzureRoute"][0]
    route["next_hop_type"] = hop
    assert any("only allowed" in f for f in m.validate(inventory))
    route["next_hop_ip_address"] = None
    assert m.validate(inventory) == []


@pytest.mark.parametrize("ip", [None, "", "10.0.1.0/24", "bad"])
def test_appliance_requires_ip(inventory, ip):
    inventory["AzureRoute"][0]["next_hop_ip_address"] = ip
    assert any("requires a valid" in f for f in m.validate(inventory))


@pytest.mark.parametrize("value", ["0.0.0.0/0", "::/0", "Storage.AustraliaEast"])
def test_route_destinations(value):
    m.address_expression(value, route=True)


@pytest.mark.parametrize("value", ["*", "10.0.0.1", "10.0.0.0/8,192.168.0.0/16"])
def test_invalid_route_destinations(value):
    with pytest.raises(ValueError):
        m.address_expression(value, route=True)


def test_all_findings_reported(inventory):
    inventory["AzureNetworkSecurityRule"][0].update(
        priority=0, source_ports="bad", protocol="bad"
    )
    inventory["AzureRoute"][0]["next_hop_ip_address"] = None
    assert len(m.validate(inventory)) == 4


def connection(ids, count=None):
    return {
        "count": len(ids) if count is None else count,
        "edges": [{"node": {"id": id_}} for id_ in ids],
    }


def test_nested_pagination():
    c = MagicMock()

    def page(**kwargs):
        offset = kwargs["variables"]["offset"]
        assert kwargs["branch_name"] == "test"
        ids = [str(i) for i in range(offset, min(offset + 100, 101))]
        return {
            "AzureVirtualNetwork": {
                "edges": [{"node": {"address_space": connection(ids, 101)}}]
            }
        }

    c.execute_graphql.side_effect = page
    assert (
        len(m.prefix_ids(c, "test", "AzureVirtualNetwork", "v", "address_space")) == 101
    )
    assert c.execute_graphql.call_count == 2


def test_incomplete_nested_read_fails():
    c = MagicMock()
    c.execute_graphql.return_value = {
        "AzureVirtualNetwork": {
            "edges": [{"node": {"address_space": connection([], 1)}}]
        }
    }
    with pytest.raises(ValueError, match="Incomplete"):
        m.prefix_ids(c, "test", "AzureVirtualNetwork", "v", "address_space")


def test_cli_reads_and_errors(inventory, monkeypatch):
    c = MagicMock()

    def all_nodes(kind, **kwargs):
        assert kwargs["branch"] == "test" and "limit" not in kwargs
        attrs, peers = m.FIELDS[kind]
        return [
            NS(
                id=n["id"],
                **{a: NS(value=n.get(a)) for a in attrs},
                **{p: NS(id=n.get(p)) for p in peers},
            )
            for n in inventory.get(kind, [])
        ]

    c.all.side_effect = all_nodes

    def query(**kwargs):
        kind = (
            "AzureVirtualNetworkSubnet"
            if "AzureVirtualNetworkSubnet(" in kwargs["query"]
            else "AzureVirtualNetwork"
        )
        field = "address_prefixes" if kind.endswith("Subnet") else "address_space"
        return {
            kind: {"edges": [{"node": {field: connection(inventory[kind][0][field])}}]}
        }

    c.execute_graphql.side_effect = query
    monkeypatch.setattr(m, "InfrahubClientSync", lambda: c)
    runner = CliRunner()
    assert runner.invoke(m.app, ["--branch", "test"]).exit_code == 0
    inventory["AzureNetworkSecurityRule"][0]["priority"] = 0
    result = runner.invoke(m.app, ["--branch", "test"])
    assert result.exit_code == 1 and "priority must" in result.output
    c.all.side_effect = RuntimeError("secret credentials")
    result = runner.invoke(m.app, ["--branch", "test"])
    assert result.exit_code == 1 and "secret credentials" not in result.output
    c.create.assert_not_called()


def test_empty_cli(monkeypatch):
    c = MagicMock()
    c.all.return_value = []
    monkeypatch.setattr(m, "InfrahubClientSync", lambda: c)
    result = CliRunner().invoke(m.app, ["--branch", "test"])
    assert result.exit_code == 0 and "empty" in result.output
