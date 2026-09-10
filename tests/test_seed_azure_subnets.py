"""Offline seed and delegation validation; no live scenario fixtures."""

from copy import deepcopy
import ipaddress
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest
import yaml
from typer.testing import CliRunner

from scripts import seed_azure_subnets as m
from scripts import check_azure_networks as networks


@pytest.fixture
def catalog():
    return m.load_catalog(Path("data/azure_hub_subnets.yaml"))


@pytest.fixture
def client(monkeypatch):
    c = MagicMock()
    c.inventory = {kind: [] for kind in networks.FIELDS}
    c.inventory.update(
        {
            "AzureTenant": [dict(id="tenant", name="fake-corp")],
            "AzureSubscription": [dict(id="sub", name="Connectivity", tenant="tenant")],
            "AzureResourceGroup": [
                dict(id="rg", name="rg-conn-prd-network", subscription="sub")
            ],
            "AzureRegion": [dict(id="region", name="australiaeast")],
            "IpamNamespace": [dict(id="namespace", name="default")],
            "BuiltinIPPrefix": [
                dict(id="space", prefix="10.150.0.0/24", ip_namespace="namespace")
            ],
            "AzureVirtualNetwork": [
                dict(
                    id="vnet",
                    name="vnet-conn-prd-hub",
                    resourcegroup="rg",
                    location="region",
                    address_space=["space"],
                )
            ],
        }
    )
    monkeypatch.setattr(
        m, "read_inventory", lambda client, branch: deepcopy(client.inventory)
    )

    def all_prefixes(kind, **kwargs):
        assert kind == "IpamPrefix" and kwargs["branch"] == "test"
        return [
            NS(
                id=p["id"],
                is_pool=NS(value=p.get("is_pool", False)),
                vrf=NS(id=p.get("vrf")),
            )
            for p in c.inventory["BuiltinIPPrefix"]
        ]

    c.all.side_effect = all_prefixes

    def create(kind, branch, data):
        assert branch == "test"
        row = dict(data, id=f"created-{c.create.call_count}")
        if kind == "AzureVirtualNetworkSubnet":
            row.update(network_security_group=None, route_table=None)
        target = "BuiltinIPPrefix" if kind == "IpamPrefix" else kind
        return NS(
            id=row["id"],
            save=MagicMock(side_effect=lambda: c.inventory[target].append(row)),
        )

    c.create.side_effect = create
    return c


def test_agreed_ranges(catalog):
    ranges = [ipaddress.ip_network(s["prefix"]) for s in catalog["subnets"]]
    assert list(ipaddress.collapse_addresses(ranges)) == [
        ipaddress.ip_network("10.150.0.0/24")
    ]
    assert sum(n.num_addresses for n in ranges) == 256
    assert sum(len(s["delegations"]) for s in catalog["subnets"]) == 2


def test_preview_apply_rerun(client, catalog):
    assert m.seed(client, "test", catalog) == 0
    client.create.assert_not_called()
    assert m.seed(client, "test", catalog, True) == 0
    assert client.create.call_count == 14
    kinds = [c.kwargs["kind"] for c in client.create.call_args_list]
    assert (
        kinds
        == ["IpamPrefix"] * 6
        + ["AzureVirtualNetworkSubnet"] * 6
        + ["AzureSubnetDelegation"] * 2
    )
    assert networks.validate(client.inventory) == []
    client.inventory["AzureVirtualNetworkSubnet"][0]["status"] = "active"
    client.inventory["BuiltinIPPrefix"][1]["description"] = "Operational edit"
    client.inventory["AzureSubnetDelegation"][0]["status"] = "active"
    assert m.seed(client, "test", catalog, True) == 0
    assert client.create.call_count == 14
    assert client.inventory["AzureVirtualNetworkSubnet"][0]["status"] == "active"
    assert client.inventory["BuiltinIPPrefix"][1]["description"] == "Operational edit"
    assert client.inventory["AzureSubnetDelegation"][0]["status"] == "active"


@pytest.mark.parametrize(
    "kind",
    [
        "AzureTenant",
        "AzureSubscription",
        "AzureResourceGroup",
        "AzureVirtualNetwork",
        "IpamNamespace",
    ],
)
@pytest.mark.parametrize("duplicate", [False, True])
def test_dependencies(client, catalog, kind, duplicate):
    client.inventory[kind] = client.inventory[kind] * 2 if duplicate else []
    with pytest.raises(ValueError, match="expected one"):
        m.seed(client, "test", catalog, True)
    client.create.assert_not_called()


@pytest.mark.parametrize(
    "edit", ["outside", "overlap", "dns_size", "dns_ipv6", "dns_other_service"]
)
def test_projected_conflicts_prevent_writes(client, catalog, edit):
    if edit == "outside":
        catalog["subnets"][0]["prefix"] = "10.151.0.0/26"
    elif edit == "overlap":
        catalog["subnets"][1]["prefix"] = "10.150.0.0/27"
    elif edit == "dns_size":
        catalog["subnets"][3]["prefix"] = "10.150.0.160/29"
    elif edit == "dns_ipv6":
        catalog["subnets"][3]["prefix"] = "2001:db8::/64"
    else:
        catalog["subnets"][3]["delegations"].append(
            dict(name="other", service_name="Microsoft.Test/service")
        )
    assert m.seed(client, "test", catalog, True) == 1
    client.create.assert_not_called()


@pytest.mark.parametrize(
    "edit", ["name", "prefix", "delegation_service", "extra_delegation", "pool", "vrf"]
)
def test_existing_conflicts(client, catalog, edit):
    m.seed(client, "test", catalog, True)
    if edit == "name":
        client.inventory["AzureVirtualNetworkSubnet"][0]["name"] = "azurefirewallsubnet"
    elif edit == "prefix":
        client.inventory["AzureVirtualNetworkSubnet"][0]["address_prefixes"] = ["space"]
    elif edit == "delegation_service":
        client.inventory["AzureSubnetDelegation"][0]["service_name"] = (
            "Microsoft.Test/service"
        )
    elif edit == "extra_delegation":
        client.inventory["AzureSubnetDelegation"].append(
            dict(
                id="extra",
                name="extra",
                subnet=client.inventory["AzureVirtualNetworkSubnet"][5]["id"],
                service_name="Microsoft.Test/service",
            )
        )
    elif edit == "pool":
        client.inventory["BuiltinIPPrefix"][1]["is_pool"] = True
    else:
        client.inventory["BuiltinIPPrefix"][1]["vrf"] = "vrf"
    client.create.reset_mock()
    assert m.seed(client, "test", catalog, True) == 1
    client.create.assert_not_called()


def test_partial_failure_rerun(client, catalog):
    create = client.create.side_effect

    def fail(kind, **kwargs):
        if kind == "AzureSubnetDelegation":
            raise RuntimeError("failure")
        return create(kind=kind, **kwargs)

    client.create.side_effect = fail
    with pytest.raises(RuntimeError):
        m.seed(client, "test", catalog, True)
    assert len(client.inventory["AzureVirtualNetworkSubnet"]) == 6
    client.create.side_effect = create
    assert m.seed(client, "test", catalog, True) == 0
    assert len(client.inventory["BuiltinIPPrefix"]) == 7
    assert len(client.inventory["AzureSubnetDelegation"]) == 2


@pytest.mark.parametrize(
    "edit",
    [
        "name",
        "host_bits",
        "empty",
        "duplicate_name",
        "duplicate_service",
        "service_syntax",
    ],
)
def test_invalid_catalog(tmp_path, catalog, edit):
    if edit == "name":
        catalog["subnets"][0]["name"] = "_invalid"
    elif edit == "host_bits":
        catalog["subnets"][0]["prefix"] = "10.150.0.1/26"
    elif edit == "empty":
        catalog["subnets"] = []
    elif edit == "duplicate_name":
        catalog["subnets"].append(deepcopy(catalog["subnets"][0]))
    elif edit == "duplicate_service":
        catalog["subnets"][3]["delegations"].append(
            dict(name="another", service_name="microsoft.network/dnsresolvers")
        )
    else:
        catalog["subnets"][3]["delegations"][0]["service_name"] = "bad service"
    path = tmp_path / "catalog.yaml"
    path.write_text(yaml.safe_dump(catalog))
    with pytest.raises(ValueError):
        m.load_catalog(path)


def test_cli_failure_redaction(client, monkeypatch):
    monkeypatch.setattr(m, "InfrahubClientSync", lambda: client)
    runner = CliRunner()
    assert runner.invoke(m.app, ["--branch", "test"]).exit_code == 0
    client.all.side_effect = RuntimeError("sensitive details")
    result = runner.invoke(m.app, ["--branch", "test", "--apply"])
    assert result.exit_code == 1 and "sensitive details" not in result.output
    client.create.assert_not_called()


def test_preserve_existing_policy_associations(client, catalog):
    m.seed(client, "test", catalog, True)
    client.inventory["AzureNetworkSecurityGroup"].append(
        dict(id="nsg", name="nsg", resourcegroup="rg", location="region")
    )
    client.inventory["AzureVirtualNetworkSubnet"][5]["network_security_group"] = "nsg"
    assert m.seed(client, "test", catalog, True) == 0
    assert (
        client.inventory["AzureVirtualNetworkSubnet"][5]["network_security_group"]
        == "nsg"
    )


def test_preserve_existing_service_endpoints(client, catalog):
    m.seed(client, "test", catalog, True)
    endpoint = dict(
        id="endpoint",
        service_name="Microsoft.KeyVault",
        subnet=client.inventory["AzureVirtualNetworkSubnet"][5]["id"],
    )
    client.inventory["AzureSubnetServiceEndpoint"].append(endpoint.copy())
    client.create.reset_mock()
    assert m.seed(client, "test", catalog, True) == 0
    assert client.inventory["AzureSubnetServiceEndpoint"] == [endpoint]
    client.create.assert_not_called()
