"""Offline DNS intent scenarios. No domains, endpoints, or records are seeded."""

from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from scripts import check_azure_dns as m


@pytest.fixture
def inventory():
    return {
        m.TENANT: [dict(id="tenant", name="tenant")],
        m.SUBSCRIPTION: [dict(id="sub", name="sub", tenant="tenant")],
        m.RG: [dict(id="rg", name="rg", subscription="sub")],
        m.REGION: [dict(id="region", name="region")],
        m.NAMESPACE: [dict(id="ns", name="default")],
        m.VNET: [
            dict(id="hub", name="hub", resourcegroup="rg", location="region"),
            dict(id="spoke", name="spoke", resourcegroup="rg", location="region"),
        ],
        m.SUBNET: [
            dict(
                id=f"s{i}",
                name=f"s{i}",
                virtualnetwork="hub",
                address_prefixes=[f"p{i}"],
            )
            for i in range(2)
        ],
        m.PREFIX: [
            dict(id=f"p{i}", prefix=f"10.0.{i}.0/28", ip_namespace="ns")
            for i in range(2)
        ],
        m.ADDRESS: [dict(id="ip", address="10.0.0.4/32", ip_namespace="ns")],
        m.DELEGATION: [
            dict(
                id=f"d{i}",
                name="dns",
                subnet=f"s{i}",
                service_name="Microsoft.Network/dnsResolvers",
            )
            for i in range(2)
        ],
        m.ZONE: [
            dict(
                id="zone",
                name="example.internal",
                resourcegroup="rg",
                zone_selection="custom",
                custom_name="example.internal",
            )
        ],
        m.ZONE_LINK: [
            dict(
                id="zl",
                name="hub",
                zone="zone",
                virtual_network="hub",
                registration_enabled=False,
            )
        ],
        m.RECORD: [
            dict(
                id="record",
                name="www",
                zone="zone",
                record_type="A",
                ttl=3600,
                records=["10.0.2.4"],
            )
        ],
        m.RESOLVER: [
            dict(
                id="resolver",
                name="resolver",
                resourcegroup="rg",
                location="region",
                virtual_network="hub",
            )
        ],
        m.INBOUND: [
            dict(
                id="inbound",
                name="inbound",
                resolver="resolver",
                subnet="s0",
                allocation_method="Static",
                ip_address="ip",
            )
        ],
        m.OUTBOUND: [
            dict(id="outbound", name="outbound", resolver="resolver", subnet="s1")
        ],
        m.RULESET: [
            dict(
                id="ruleset",
                name="ruleset",
                resourcegroup="rg",
                location="region",
                outbound_endpoints=["outbound"],
            )
        ],
        m.RULE: [
            dict(
                id="rule",
                name="rule",
                ruleset="ruleset",
                domain_name="example.internal.",
                enabled=True,
                target_servers=[dict(ip_address="10.0.0.4")],
            )
        ],
        m.RULESET_LINK: [
            dict(id="rl", name="spoke", ruleset="ruleset", virtual_network="spoke")
        ],
    }


def test_valid_and_empty(inventory):
    assert m.validate(inventory) == []
    assert m.validate({}) == []


@pytest.mark.parametrize(
    "kind,field,value,expected",
    [
        (m.ZONE, "name", "internal", "invalid"),
        (m.ZONE, "name", "core.windows.net", "invalid"),
        (m.ZONE, "name", "example.internal.", "invalid"),
        (m.ZONE, "name", "bad zone.internal", "invalid"),
        (m.ZONE, "resourcegroup", "absent", "missing"),
        (m.RECORD, "name", "a.*", "invalid"),
        (m.RECORD, "records", ["2001:db8::1"], "IPv4"),
        (m.RECORD, "records", ["10.0.0.1/32"], "IPv4"),
        (m.RECORD, "records", None, "list"),
        (m.RECORD, "records", [], "list"),
        (m.RECORD, "records", ["10.0.0.1"] * 21, "1–20"),
        (m.RECORD, "records", ["10.0.0.1"] * 2, "duplicate"),
        (m.RECORD, "ttl", 0, "TTL"),
        (m.RECORD, "ttl", 2147483648, "TTL"),
        (m.RECORD, "ttl", True, "TTL"),
        (m.RECORD, "record_type", "MX", "unsupported"),
        (m.ZONE_LINK, "registration_enabled", None, "Boolean"),
        (m.RESOLVER, "location", "other", "region"),
        (m.INBOUND, "subnet", "s1", "duplicate endpoint subnet"),
        (m.INBOUND, "allocation_method", None, "allocation_method"),
        (m.INBOUND, "ip_address", None, "static"),
        (m.INBOUND, "ip_address", "absent", "missing"),
        (m.DELEGATION, "service_name", "Microsoft.Web/serverFarms", "delegation"),
        (m.PREFIX, "prefix", "10.0.0.0/29", "/24–/28"),
        (m.PREFIX, "prefix", "2001:db8::/64", "/24–/28"),
        (m.ADDRESS, "address", "10.0.0.0/32", "reserved"),
        (m.ADDRESS, "address", "10.0.0.3/32", "reserved"),
        (m.ADDRESS, "address", "10.0.0.15/32", "reserved"),
        (m.ADDRESS, "address", "10.0.3.4/32", "usable"),
        (m.ADDRESS, "ip_namespace", "other", "namespace"),
        (m.RULESET, "outbound_endpoints", [], "one or two"),
        (m.RULESET, "outbound_endpoints", ["absent"], "missing"),
        (m.RULESET, "outbound_endpoints", ["outbound"] * 2, "distinct"),
        (m.RULE, "domain_name", "example.internal", "suffix"),
        (m.RULE, "enabled", None, "Boolean"),
        (m.RULE, "target_servers", [], "one to six"),
        (m.RULE, "target_servers", ["10.0.0.4"], "target requires"),
        (m.RULE, "target_servers", [{"ip_address": "168.63.129.16"}], "prohibited"),
        (m.RULE, "target_servers", [{"ip_address": "not-an-ip"}], "invalid"),
        (m.RULE, "target_servers", [{"ip_address": "10.1.1.1", "port": True}], "port"),
        (m.RULE, "target_servers", [{"ip_address": "10.1.1.1", "port": 0}], "port"),
        (m.RULE, "target_servers", [{"ip_address": "10.1.1.1", "port": 65536}], "port"),
        (
            m.RULE,
            "target_servers",
            [{"ip_address": "10.1.1.1", "extra": 1}],
            "target requires",
        ),
        (m.RULE, "target_servers", [{"ip_address": "10.1.1.1"}] * 2, "duplicate"),
        (m.RULESET_LINK, "virtual_network", "hub", "forwarding loop"),
    ],
)
def test_invalid(inventory, kind, field, value, expected):
    inventory[kind][0][field] = value
    findings = m.validate(inventory)
    assert any(expected in f for f in findings), findings
    assert all("(" in f and ")" in f for f in findings)


@pytest.mark.parametrize(
    "kind,records",
    [
        ("A", ["10.0.0.4", "10.0.0.5"]),
        ("AAAA", ["2001:db8::1", "2001:db8::2"]),
        ("CNAME", ["target.example.internal."]),
        ("TXT", [["v=spf1 ", "include:example.org ~all"], ["Other Text"]]),
        ("TXT", [[""]]),
    ],
)
def test_record_types(inventory, kind, records):
    inventory[m.RECORD][0].update(record_type=kind, records=records)
    assert m.validate(inventory) == []


@pytest.mark.parametrize(
    "kind,records",
    [
        ("AAAA", ["10.0.0.4"]),
        ("AAAA", ["fe80::1%eth0"]),
        ("CNAME", ["one.internal", "two.internal"]),
        ("CNAME", ["bad..internal"]),
        ("TXT", ["not-chunks"]),
        ("TXT", [[]]),
        ("TXT", [["x" * 256]]),
        ("TXT", [["é" * 128]]),
        ("TXT", [["x"] * 4097]),
        ("TXT", [["same"], ["sa", "me"]]),
    ],
)
def test_bad_record_values(inventory, kind, records):
    inventory[m.RECORD][0].update(record_type=kind, records=records)
    assert m.validate(inventory)


def test_cname_conflicts(inventory):
    inventory[m.RECORD].append(
        dict(
            inventory[m.RECORD][0],
            id="alias",
            name="WWW",
            record_type="CNAME",
            records=["target.internal"],
        )
    )
    assert any("CNAME conflicts" in f for f in m.validate(inventory))
    inventory[m.RECORD] = [dict(inventory[m.RECORD][-1], name="@")]
    assert any("apex" in f for f in m.validate(inventory))


@pytest.mark.parametrize("name", ["@", "*", "*.apps", "_verification"])
def test_relative_names(inventory, name):
    inventory[m.RECORD][0]["name"] = name
    assert m.validate(inventory) == []


@pytest.mark.parametrize("ttl", [1, 2147483647])
def test_ttl_bounds(inventory, ttl):
    inventory[m.RECORD][0]["ttl"] = ttl
    assert m.validate(inventory) == []


@pytest.mark.parametrize("kind", m.DNS_KINDS)
def test_scoped_names(inventory, kind):
    inventory[kind].append(
        dict(
            inventory[kind][0], id="duplicate", name=inventory[kind][0]["name"].upper()
        )
    )
    assert any("duplicate scoped name" in f for f in m.validate(inventory))


def test_zone_scope_and_autoregistration(inventory):
    inventory[m.RG].append(dict(id="other-rg", name="other", subscription="sub"))
    inventory[m.ZONE].append(
        dict(
            id="zone2",
            name="EXAMPLE.INTERNAL",
            resourcegroup="other-rg",
            zone_selection="custom",
            custom_name="example.internal",
        )
    )
    assert m.validate(inventory) == []
    inventory[m.ZONE_LINK][0]["registration_enabled"] = True
    inventory[m.ZONE_LINK].append(
        dict(
            id="zl2",
            name="hub",
            zone="zone2",
            virtual_network="hub",
            registration_enabled=True,
        )
    )
    assert any("autoregistration" in f for f in m.validate(inventory))
    inventory[m.ZONE_LINK][-1]["registration_enabled"] = False
    assert m.validate(inventory) == []


def test_link_and_suffix_uniqueness(inventory):
    for kind in (m.ZONE_LINK, m.RULESET_LINK, m.RULE):
        inventory[kind].append(
            dict(inventory[kind][0], id="dup-" + kind, name="different")
        )
    findings = m.validate(inventory)
    for message in ("zone/VNet", "ruleset/VNet", "forwarding suffix"):
        assert any(message in f for f in findings)


def test_dynamic_address_and_disabled_loop(inventory):
    inventory[m.INBOUND][0].update(allocation_method="Dynamic", ip_address=None)
    inventory[m.RULESET_LINK][0]["virtual_network"] = "hub"
    # An unknown dynamic VIP cannot be used for loop detection.
    assert m.validate(inventory) == []
    inventory[m.INBOUND][0]["ip_address"] = "ip"
    assert any("loop" in f for f in m.validate(inventory))
    inventory[m.RULE][0]["enabled"] = False
    assert m.validate(inventory) == []


def test_subnet_ownership_and_exclusive_delegation(inventory):
    inventory[m.SUBNET][0]["virtualnetwork"] = "spoke"
    inventory[m.DELEGATION].append(
        dict(
            id="d2", name="other", service_name="Microsoft.Web/serverFarms", subnet="s0"
        )
    )
    inventory[m.SUBNET][1]["address_prefixes"].append("p0")
    findings = m.validate(inventory)
    assert any("resolver VNet" in f for f in findings)
    assert any("exclusive" in f for f in findings)
    assert any("exactly one" in f for f in findings)


def test_cross_subscription_links_and_tenants(inventory):
    inventory[m.SUBSCRIPTION].append(dict(id="sub2", name="sub2", tenant="tenant"))
    inventory[m.RG].append(dict(id="rg2", name="rg2", subscription="sub2"))
    inventory[m.VNET][1]["resourcegroup"] = "rg2"
    assert m.validate(inventory) == []
    inventory[m.TENANT].append(dict(id="t2", name="t2"))
    inventory[m.SUBSCRIPTION][-1]["tenant"] = "t2"
    assert any("share tenant and region" in f for f in m.validate(inventory))


def test_second_outbound_endpoint(inventory):
    inventory[m.SUBNET].append(
        dict(id="s2", name="s2", virtualnetwork="hub", address_prefixes=["p2"])
    )
    inventory[m.PREFIX].append(dict(id="p2", prefix="10.0.2.0/28", ip_namespace="ns"))
    inventory[m.DELEGATION].append(
        dict(
            id="d2",
            name="dns",
            subnet="s2",
            service_name="Microsoft.Network/dnsResolvers",
        )
    )
    inventory[m.OUTBOUND].append(
        dict(id="out2", name="out2", resolver="resolver", subnet="s2")
    )
    inventory[m.RULESET][0]["outbound_endpoints"].append("out2")
    assert m.validate(inventory) == []
    inventory[m.RESOLVER].append(
        dict(
            id="r2",
            name="r2",
            virtual_network="spoke",
            resourcegroup="rg",
            location="region",
        )
    )
    inventory[m.OUTBOUND][-1]["resolver"] = "r2"
    inventory[m.SUBNET][-1]["virtualnetwork"] = "spoke"
    assert any("same resolver" in f for f in m.validate(inventory))


def test_wildcard_forwarding_and_ports(inventory):
    inventory[m.RULE][0].update(
        domain_name=".",
        target_servers=[
            dict(ip_address="192.0.2.1", port=65535),
            dict(ip_address="192.0.2.2", port=1),
        ],
    )
    assert m.validate(inventory) == []


def test_cli_all_pages_and_errors(inventory, monkeypatch):
    client = MagicMock()
    inventory[m.RECORD] = [
        dict(inventory[m.RECORD][0], id=f"r{i}", name=f"host{i}") for i in range(105)
    ]

    def all_nodes(kind, **kwargs):
        assert kwargs["branch"] == "test" and "limit" not in kwargs
        attrs = m.ATTRIBUTES[kind]
        peers = list(m.REFERENCES[kind]) + (["ip_address"] if kind == m.INBOUND else [])
        return (
            NS(
                id=n["id"],
                **{a: NS(value=n.get(a)) for a in attrs},
                **{p: NS(id=n.get(p)) for p in peers},
            )
            for n in inventory.get(kind, [])
        )

    def graphql(query, variables, branch_name):
        assert branch_name == "test"
        kind, field = (
            (m.SUBNET, "address_prefixes")
            if m.SUBNET in query
            else (m.RULESET, "outbound_endpoints")
        )
        row = next(n for n in inventory[kind] if n["id"] == variables["id"])
        ids = row[field]
        return {
            kind: {
                "edges": [
                    {
                        "node": {
                            field: {
                                "count": len(ids),
                                "edges": [{"node": {"id": id_}} for id_ in ids],
                            }
                        }
                    }
                ]
            }
        }

    client.all.side_effect = all_nodes
    client.execute_graphql.side_effect = graphql
    monkeypatch.setattr(m, "InfrahubClientSync", lambda: client)
    runner = CliRunner()
    assert runner.invoke(m.app, ["--branch", "test"]).exit_code == 0
    inventory[m.RECORD][-1]["ttl"] = 0
    result = runner.invoke(m.app, ["--branch", "test"])
    assert result.exit_code == 1 and "r104" in result.output
    inventory.clear()
    result = runner.invoke(m.app, ["--branch", "test"])
    assert result.exit_code == 0 and "empty" in result.output
    client.all.side_effect = RuntimeError("secret")
    result = runner.invoke(m.app, ["--branch", "test"])
    assert (
        result.exit_code == 1
        and "read failed" in result.output
        and "secret" not in result.output
    )
    client.create.assert_not_called()


def test_nested_connection_pagination():
    client = MagicMock()
    ids = [str(i) for i in range(105)]

    def graphql(query, variables, branch_name):
        assert branch_name == "test"
        page = ids[variables["offset"] : variables["offset"] + 100]
        return {
            m.SUBNET: {
                "edges": [
                    {
                        "node": {
                            "address_prefixes": {
                                "count": len(ids),
                                "edges": [{"node": {"id": id_}} for id_ in page],
                            }
                        }
                    }
                ]
            }
        }

    client.execute_graphql.side_effect = graphql
    assert m.prefix_ids(client, "test", m.SUBNET, "subnet", "address_prefixes") == ids
    assert client.execute_graphql.call_count == 2
    client.execute_graphql.side_effect = None
    client.execute_graphql.return_value = {
        m.SUBNET: {"edges": [{"node": {"address_prefixes": {"count": 2, "edges": []}}}]}
    }
    with pytest.raises(ValueError, match="Incomplete"):
        m.prefix_ids(client, "test", m.SUBNET, "subnet", "address_prefixes")


def test_all_findings(inventory):
    inventory[m.RECORD][0].update(ttl=0, records=["bad"])
    inventory[m.ZONE][0]["resourcegroup"] = "missing"
    assert len(m.validate(inventory)) >= 3


@pytest.mark.parametrize("kind", [m.RESOLVER, m.INBOUND, m.OUTBOUND, m.RULESET])
@pytest.mark.parametrize("name", ["bad.name", "trailing_", "-leading", "trailing-"])
def test_resolver_resource_names(inventory, kind, name):
    inventory[kind][0]["name"] = name
    assert any("invalid Azure/DNS name" in f for f in m.validate(inventory))


def test_zone_underscores_and_link_periods(inventory):
    inventory[m.ZONE][0].update(
        name="internal_zone.example", custom_name="internal_zone.example"
    )
    inventory[m.ZONE_LINK][0]["name"] = "link.name_"
    assert m.validate(inventory) == []
