"""Paired peering intent scenarios; no live Azure or Infrahub writes."""

from copy import deepcopy
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from scripts import check_azure_networks as m


def peering(id_="peer", a="a", b="b"):
    return {
        "id": id_,
        "virtual_network_a": a,
        "virtual_network_b": b,
        "peering_name_a": f"{id_}-to-{b}",
        "peering_name_b": f"{id_}-to-{a}",
        **{
            f"{side}_{option}": option == "allow_virtual_network_access"
            for side in "ab"
            for option in m.PEERING_OPTIONS
        },
    }


@pytest.fixture
def inventory():
    data = {kind: [] for kind in m.FIELDS}
    for i, side in enumerate("abc"):
        data["AzureTenant"].append(dict(id=f"tenant-{side}", name=side))
        data["AzureSubscription"].append(
            dict(id=f"sub-{side}", name=side, tenant=f"tenant-{side}")
        )
        data["AzureResourceGroup"].append(
            dict(id=f"rg-{side}", name=side, subscription=f"sub-{side}")
        )
        data["AzureRegion"].append(dict(id=f"region-{side}", name=side))
        data["IpamNamespace"].append(dict(id=side, name=side))
        data["AzureVirtualNetwork"].append(
            dict(
                id=side,
                name=f"vnet-{side}",
                resourcegroup=f"rg-{side}",
                location=f"region-{side}",
                address_space=[f"prefix-{side}"],
            )
        )
        data["BuiltinIPPrefix"].append(
            dict(id=f"prefix-{side}", prefix=f"10.{i}.0.0/16", ip_namespace=side)
        )
    data[m.PEERING_KIND] = [peering()]
    return data


def test_cross_context_connection(inventory):
    assert m.validate(inventory) == []
    # Independent directional traffic choices are allowed.
    inventory[m.PEERING_KIND][0]["b_allow_virtual_network_access"] = False
    inventory[m.PEERING_KIND][0]["a_allow_forwarded_traffic"] = True
    assert m.validate(inventory) == []


@pytest.mark.parametrize("side", "ab")
def test_gateway_transit(inventory, side):
    n = inventory[m.PEERING_KIND][0]
    n[f"{side}_use_remote_gateways"] = True
    other = "b" if side == "a" else "a"
    assert any("requires gateway transit" in f for f in m.validate(inventory))
    n[f"{other}_allow_gateway_transit"] = True
    assert m.validate(inventory) == []


@pytest.mark.parametrize("reverse", [False, True])
def test_duplicate_pairs(inventory, reverse):
    inventory[m.PEERING_KIND].append(
        peering("second", *(("b", "a") if reverse else ("a", "b")))
    )
    assert any("duplicate VNet pair" in f for f in m.validate(inventory))


def test_invalid_references_and_names(inventory):
    n = inventory[m.PEERING_KIND][0]
    n.update(
        virtual_network_a="missing",
        peering_name_a="bad.",
        peering_name_b="x" * 81,
        a_allow_forwarded_traffic=None,
    )
    findings = m.validate(inventory)
    assert len(findings) == 4
    assert all("peer" in f and "missing" in f and "B=b" in f for f in findings)


def test_self_peering(inventory):
    inventory[m.PEERING_KIND][0]["virtual_network_b"] = "a"
    assert any("self-peering" in f for f in m.validate(inventory))


def test_name_uniqueness_across_ends(inventory):
    second = peering("second", "c", "a")
    second["peering_name_b"] = inventory[m.PEERING_KIND][0]["peering_name_a"].upper()
    inventory[m.PEERING_KIND].append(second)
    assert any("duplicate peering name" in f for f in m.validate(inventory))
    second["peering_name_b"] = "unique"
    second["peering_name_a"] = inventory[m.PEERING_KIND][0]["peering_name_a"]
    assert m.validate(inventory) == []


@pytest.mark.parametrize(
    "prefixes", [("10.0.0.0/16", "10.0.1.0/24"), ("2001:db8::/32", "2001:db8:1::/48")]
)
def test_overlap_across_namespaces(inventory, prefixes):
    for n, prefix in zip(inventory["BuiltinIPPrefix"], prefixes):
        n["prefix"] = prefix
    assert any("overlapping VNet address spaces" in f for f in m.validate(inventory))


def test_different_ip_families(inventory):
    inventory["BuiltinIPPrefix"][1]["prefix"] = "2001:db8::/32"
    assert m.validate(inventory) == []


def test_bidirectional_remote_gateway_invalid(inventory):
    n = inventory[m.PEERING_KIND][0]
    for side in "ab":
        n[f"{side}_use_remote_gateways"] = True
        n[f"{side}_allow_gateway_transit"] = True
    assert any("both ends" in f for f in m.validate(inventory))


def test_remote_gateway_limit_across_ends(inventory):
    first = inventory[m.PEERING_KIND][0]
    first.update(a_use_remote_gateways=True, b_allow_gateway_transit=True)
    second = peering("second", "c", "a")
    second.update(b_use_remote_gateways=True, a_allow_gateway_transit=True)
    inventory[m.PEERING_KIND].append(second)
    assert any("multiple connections" in f for f in m.validate(inventory))


def test_cli_reads_every_peering_and_reports_failures(inventory, monkeypatch):
    client = MagicMock()
    # SDK all() owns pagination; consume its full iterable, without a manual limit.
    inventory[m.PEERING_KIND] += [peering(f"extra-{i}") for i in range(101)]

    def all_nodes(kind, **kwargs):
        assert kwargs["branch"] == "test" and "limit" not in kwargs
        attrs, peers = m.FIELDS[kind]
        return (
            NS(
                id=n["id"],
                **{a: NS(value=n.get(a)) for a in attrs},
                **{p: NS(id=n.get(p)) for p in peers},
            )
            for n in inventory[kind]
        )

    client.all.side_effect = all_nodes
    monkeypatch.setattr(
        m,
        "prefix_ids",
        lambda client, branch, kind, id_, field: next(
            n[field] for n in inventory[kind] if n["id"] == id_
        ),
    )
    monkeypatch.setattr(m, "InfrahubClientSync", lambda: client)
    result = CliRunner().invoke(m.app, ["--branch", "test"])
    assert result.exit_code == 1 and "extra-100" in result.output
    inventory[m.PEERING_KIND] = inventory[m.PEERING_KIND][:1]
    before = deepcopy(inventory)
    assert CliRunner().invoke(m.app, ["--branch", "test"]).exit_code == 0
    assert inventory == before
    client.all.side_effect = RuntimeError("secret")
    result = CliRunner().invoke(m.app, ["--branch", "test"])
    assert result.exit_code == 1 and "secret" not in result.output
    client.create.assert_not_called()
