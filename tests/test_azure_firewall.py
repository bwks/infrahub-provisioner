"""Offline Firewall Policy expressions, hierarchy, and CLI regressions."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from scripts import check_azure_firewall as f


def sync_children(data):
    for kind, fields in f.MANY.items():
        for node in data[kind]:
            for field, child in fields.items():
                node[field] = [
                    n["id"] for n in data[child] if n[f.SCOPES[child]] == node["id"]
                ]
    return data


@pytest.fixture
def inventory():
    data = {k: [] for k in f.REFERENCES}
    data["AzureTenant"] = [dict(id="t", name="tenant")]
    data["AzureSubscription"] = [dict(id="s", name="sub", tenant="t")]
    data["AzureResourceGroup"] = [dict(id="rg", name="rg", subscription="s")]
    data["AzureRegion"] = [dict(id="r", name="region")]
    data[f.POLICY] = [
        dict(
            id="p",
            name="policy",
            resourcegroup="rg",
            location="r",
            sku="Standard",
            threat_intelligence_mode="Alert",
            dns_proxy_enabled=False,
            dns_servers=None,
        )
    ]
    data[f.GROUP] = [dict(id="g", name="group", policy="p", priority=100)]
    for i, (kind, category) in enumerate(f.RULES.items()):
        c = "c" + str(i)
        data[f.COLLECTION].append(
            dict(
                id=c,
                name=c,
                collection_group="g",
                priority=100 + i,
                category=category,
                action="DNAT" if kind == f.NAT else "Allow",
            )
        )
        rule = dict(
            id="rule" + str(i),
            name="rule",
            collection=c,
            position=1,
            source_addresses="10.0.0.0/24",
        )
        if kind == f.NETWORK:
            rule.update(
                destination_addresses="AzureCloud",
                destination_fqdns=None,
                protocols="TCP, UDP",
                destination_ports="53,443,8000-8080",
            )
        elif kind == f.APPLICATION:
            rule.update(
                target_fqdns="*.example.com",
                protocols=[dict(protocol_type="Https", port=443)],
            )
        else:
            rule.update(
                destination_addresses="203.0.113.1",
                destination_ports="443",
                protocols="TCP",
                translated_address="10.0.0.4",
                translated_fqdn=None,
                translated_port=8443,
            )
        data[kind].append(rule)
    return sync_children(data)


def test_valid(inventory):
    assert f.validate(inventory) == []


def test_empty():
    assert f.validate({}) == []


def test_empty_containers(inventory):
    for kind in (f.COLLECTION, *f.RULES):
        inventory[kind] = []
    assert not f.validate(sync_children(inventory))


@pytest.mark.parametrize(
    "kind,field,value,message",
    [
        (f.POLICY, "sku", "Premium", "sku"),
        (f.POLICY, "threat_intelligence_mode", "Allow", "threat_intelligence_mode"),
        (f.POLICY, "dns_proxy_enabled", None, "Boolean"),
        (f.POLICY, "dns_servers", "example.com", "dns_servers"),
        (f.POLICY, "resourcegroup", "missing", "missing AzureResourceGroup"),
        (f.POLICY, "location", "missing", "missing AzureRegion"),
        (f.GROUP, "policy", "missing", "missing AzureFirewallPolicy"),
        (
            f.COLLECTION,
            "collection_group",
            "missing",
            "missing AzureFirewallRuleCollectionGroup",
        ),
        (f.COLLECTION, "category", "wrong", "category"),
        (f.COLLECTION, "action", "DNAT", "Allow or Deny"),
        (f.NETWORK, "collection", "missing", "missing AzureFirewallRuleCollection"),
        (f.NETWORK, "name", "-bad", "invalid name"),
        (f.NETWORK, "protocols", "ICMP", "ICMP requires"),
        (f.NETWORK, "protocols", "Any,TCP", "unsupported protocols"),
        (f.NETWORK, "source_addresses", "AzureCloud", "source_addresses"),
        (f.NETWORK, "destination_addresses", "", "destinations"),
        (f.APPLICATION, "target_fqdns", "https://example.com/path", "target_fqdns"),
        (f.NAT, "protocols", "ICMP", "unsupported protocols"),
        (f.NAT, "translated_address", None, "exactly one"),
        (f.NAT, "translated_fqdn", "backend.example.com", "exactly one"),
        (f.NAT, "translated_address", "10.0.0.0/24", "translated_address"),
        (f.NAT, "destination_addresses", "AzureCloud", "destination_addresses"),
        (f.NAT, "translated_port", True, "translated_port"),
        (f.NAT, "translated_port", 65536, "translated_port"),
    ],
)
def test_invalid_fields(inventory, kind, field, value, message):
    inventory[kind][0][field] = value
    if message == "destinations":
        message = "destination addresses or FQDNs"
    assert any(message in x for x in f.validate(inventory))


def test_network_fqdn_dns_dependency(inventory):
    node = inventory[f.NETWORK][0]
    node.update(destination_addresses=None, destination_fqdns="api.example.com")
    assert any("DNS proxy" in x for x in f.validate(inventory))
    inventory[f.POLICY][0]["dns_proxy_enabled"] = True
    assert not f.validate(inventory)
    node["destination_fqdns"] = "*.example.com"
    assert any("destination_fqdns" in x for x in f.validate(inventory))


def test_nat_fqdn(inventory):
    inventory[f.NAT][0].update(
        translated_address=None, translated_fqdn="backend.example.com"
    )
    assert not f.validate(inventory)


def test_wrong_rule_type_and_empty_collection(inventory):
    inventory[f.NETWORK][0]["collection"] = "c1"
    findings = f.validate(sync_children(inventory))
    assert any("must match" in x for x in findings)
    assert any("must contain" in x for x in findings)
    assert any("duplicate scoped name" in x for x in findings)
    assert any("duplicate position" in x for x in findings)


@pytest.mark.parametrize("kind", [f.POLICY, f.GROUP, f.COLLECTION, f.NETWORK])
def test_duplicate_scoped_names(inventory, kind):
    inventory[kind].append(
        dict(
            inventory[kind][0], id="duplicate", name=inventory[kind][0]["name"].upper()
        )
    )
    assert any(
        "duplicate scoped name" in x for x in f.validate(sync_children(inventory))
    )


@pytest.mark.parametrize("kind", [f.GROUP, f.COLLECTION])
@pytest.mark.parametrize(
    "priority,valid",
    [
        (99, False),
        (100, True),
        (65000, True),
        (65001, False),
        (1.5, False),
        (True, False),
        (None, False),
    ],
)
def test_priority_boundaries(inventory, kind, priority, valid):
    inventory[kind][0]["priority"] = priority
    assert (not f.validate(inventory)) == valid


@pytest.mark.parametrize("position", [0, -1, 1.5, True, None])
def test_position_invalid(inventory, position):
    inventory[f.NETWORK][0]["position"] = position
    assert any("position" in x for x in f.validate(inventory))


def test_duplicate_priority(inventory):
    inventory[f.COLLECTION][1]["priority"] = 100
    assert any("duplicate priority" in x for x in f.validate(inventory))


def test_inconsistent_children(inventory):
    inventory[f.POLICY][0]["collection_groups"] = []
    assert any("inconsistent child relationships" in x for x in f.validate(inventory))


@pytest.mark.parametrize(
    "value",
    ["*", "10.0.0.1", "10.0.0.0/24", "10.0.0.1-10.0.0.10", "10.0.0.1, 10.1.0.0/16"],
)
def test_addresses_valid(value):
    f.addresses(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        None,
        "*,10.0.0.1",
        "10.0.0.1,",
        "10.0.0.1,10.0.0.1",
        "10.0.0.1/24",
        "2001:db8::1",
        "10.0.0.10-10.0.0.1",
        "https://example.com",
    ],
)
def test_addresses_invalid(value):
    with pytest.raises(ValueError):
        f.addresses(value)


@pytest.mark.parametrize("value", ["AzureCloud", "Storage.AustraliaEast,10.0.0.0/24"])
def test_destination_tags(value):
    f.addresses(value, tags=True)


@pytest.mark.parametrize("value", ["*", "1", "65535", "80,443", "1000-2000"])
def test_ports_valid(value):
    f.ports(value)


@pytest.mark.parametrize("value", ["0", "65536", "2000-1000", "80,", "*,443", "80.0"])
def test_ports_invalid(value):
    with pytest.raises(ValueError):
        f.ports(value)


@pytest.mark.parametrize(
    "value",
    [
        [],
        None,
        "Https:443",
        [{"protocol_type": "Mssql", "port": 1433}],
        [{"protocol_type": "Https", "port": True}],
        [{"protocol_type": "Https", "port": 64001}],
        [{"protocol_type": "Https", "port": 443, "extra": True}],
        [{"protocol_type": "Https", "port": 443}] * 2,
    ],
)
def test_application_protocols_invalid(value):
    with pytest.raises(ValueError):
        f.application_protocols(value)


@pytest.mark.parametrize("port", [1, 80, 443, 64000])
def test_application_ports(port):
    f.application_protocols([dict(protocol_type="Https", port=port)])


@pytest.mark.parametrize("value", ["example.com", "*.example.com", "*example.com", "*"])
def test_application_fqdns(value):
    f.fqdn(value, wildcard=True)


@pytest.mark.parametrize(
    "value",
    [
        "*.example.com",
        "example.*",
        "https://example.com",
        "example.com/path",
        "example.com.",
        "10.0.0.1",
        "-bad.example.com",
    ],
)
def test_exact_fqdns_invalid(value):
    with pytest.raises(ValueError):
        f.fqdn(value)


def test_cli(monkeypatch, inventory):
    monkeypatch.setattr(f, "InfrahubClientSync", Mock())
    monkeypatch.setattr(f, "read_inventory", lambda *_: inventory)
    runner = CliRunner()
    assert runner.invoke(f.app, ["--branch", "test"]).exit_code == 0
    inventory[f.POLICY][0]["sku"] = "Premium"
    result = runner.invoke(f.app, ["--branch", "test"])
    assert (
        result.exit_code == 1 and "policy" in result.output and "sku" in result.output
    )
    monkeypatch.setattr(f, "read_inventory", lambda *_: {k: [] for k in f.REFERENCES})
    result = runner.invoke(f.app, ["--branch", "test"])
    assert result.exit_code == 0 and "empty" in result.output
    monkeypatch.setattr(f, "read_inventory", Mock(side_effect=RuntimeError("secret")))
    result = runner.invoke(f.app, ["--branch", "test"])
    assert (
        result.exit_code == 1
        and "read failed" in result.output
        and "secret" not in result.output
    )


def test_paginated_reader(inventory):
    client = Mock()

    def all_nodes(kind, **kwargs):
        attrs = f.ATTRIBUTES.get(kind, ["name"])
        assert kwargs == dict(
            branch="test",
            include=attrs + list(f.REFERENCES[kind]),
            populate_store=False,
        )
        return iter(
            SimpleNamespace(
                id=n["id"],
                **{a: SimpleNamespace(value=n[a]) for a in attrs},
                **{p: SimpleNamespace(id=n[p]) for p in f.REFERENCES[kind]},
            )
            for n in inventory[kind]
        )

    client.all.side_effect = all_nodes

    def graphql(query, variables, branch_name):
        assert branch_name == "test"
        kind = next(k for k in f.MANY if k + "(" in query)
        field = next(p for p in f.MANY[kind] if p + "(" in query)
        ids = next(n[field] for n in inventory[kind] if n["id"] == variables["id"])
        page = ids[variables["offset"] : variables["offset"] + 1]
        return {
            kind: {
                "edges": [
                    {
                        "node": {
                            field: {
                                "count": len(ids),
                                "edges": [{"node": {"id": i}} for i in page],
                            }
                        }
                    }
                ]
            }
        }

    client.execute_graphql.side_effect = graphql
    assert f.read_inventory(client, "test") == inventory
    assert client.execute_graphql.call_count == 13
    client.execute_graphql.return_value = None
    client.execute_graphql.side_effect = lambda **kwargs: {
        f.POLICY: {
            "edges": [{"node": {"collection_groups": {"count": 1, "edges": []}}}]
        }
    }
    with pytest.raises(ValueError, match="Incomplete"):
        f.read_inventory(client, "test")


def test_multi_policy_scope(inventory):
    other = deepcopy(inventory)
    for kind, nodes in other.items():
        for node in nodes:
            node["id"] += "2"
            for field in f.REFERENCES[kind]:
                node[field] += "2"
    for kind in inventory:
        inventory[kind].extend(other[kind])
    assert not f.validate(sync_children(inventory))


@pytest.mark.parametrize("field", ["destination_ports", "translated_port"])
@pytest.mark.parametrize("port,valid", [(63999, True), (64000, False), (65535, False)])
def test_nat_port_limits(inventory, field, port, valid):
    inventory[f.NAT][0][field] = str(port) if field == "destination_ports" else port
    assert (not f.validate(inventory)) == valid
