"""Offline checks for the single-VNet seed workflow."""

from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest
import yaml
from typer.testing import CliRunner

from scripts import seed_azure_virtual_network as module


@pytest.fixture
def entry():
    return module.load_catalog(Path("data/azure_hub_vnet.yaml"))


@pytest.fixture
def client():
    c = MagicMock()

    def named(id, name, **fields):
        return NS(id=id, name=NS(value=name), **fields)

    c.inventory = {
        "AzureTenant": [named("tenant", "fake-corp")],
        "AzureSubscription": [named("sub", "Connectivity", tenant=NS(id="tenant"))],
        "AzureResourceGroup": [
            named("rg", "rg-conn-prd-network", subscription=NS(id="sub"))
        ],
        "AzureRegion": [named("region", "australiaeast")],
        "IpamNamespace": [named("namespace", "default")],
        "IpamPrefix": [],
        "AzureVirtualNetwork": [],
    }
    c.all.side_effect = lambda kind, **kwargs: list(c.inventory[kind])

    def create(kind, branch, data):
        assert branch == "test"
        node = NS(id=f"{kind}-{len(c.inventory[kind])}")
        for k, v in data.items():
            if k in ["resourcegroup", "location", "ip_namespace"]:
                v = NS(id=v)
            elif k == "address_space":
                v = NS(peers=[NS(id=id) for id in v], fetch=MagicMock())
            else:
                v = NS(value=v)
            setattr(node, k, v)
        node.vrf = NS(id=None)
        node.tags = NS(peers=[])
        node.save = MagicMock(side_effect=lambda: c.inventory[kind].append(node))
        return node

    c.create.side_effect = create
    return c


def test_preview_apply_rerun_preserves_edits(client, entry):
    assert module.seed(client, "test", entry) == 0
    client.create.assert_not_called()
    assert module.seed(client, "test", entry, True) == 0
    assert [call.kwargs["kind"] for call in client.create.call_args_list] == [
        "IpamPrefix",
        "AzureVirtualNetwork",
    ]
    prefix = client.inventory["IpamPrefix"][0]
    vnet = client.inventory["AzureVirtualNetwork"][0]
    assert prefix.status.value == "reserved" and vnet.status.value == "planned"
    assert vnet.address_space.peers[0].id == prefix.id
    prefix.description.value = "Operational description"
    prefix.status.value = vnet.status.value = "active"
    vnet.tags.peers = ["existing tag"]
    assert module.seed(client, "test", entry, True) == 0
    assert client.create.call_count == 2
    assert prefix.description.value == "Operational description"
    assert vnet.tags.peers == ["existing tag"] and vnet.status.value == "active"
    assert all(
        call.kwargs["branch"] == "test" and "limit" not in call.kwargs
        for call in client.all.call_args_list
    )


@pytest.mark.parametrize(
    "kind",
    [
        "AzureTenant",
        "AzureSubscription",
        "AzureResourceGroup",
        "AzureRegion",
        "IpamNamespace",
    ],
)
@pytest.mark.parametrize("duplicate", [False, True])
def test_dependency_failure_no_writes(client, entry, kind, duplicate):
    client.inventory[kind] = client.inventory[kind] * 2 if duplicate else []
    with pytest.raises(ValueError, match="expected one"):
        module.seed(client, "test", entry, True)
    client.create.assert_not_called()


@pytest.mark.parametrize("edit", ["name", "region", "address", "pool", "vrf"])
def test_conflicts_no_writes(client, entry, edit):
    module.seed(client, "test", entry, True)
    vnet = client.inventory["AzureVirtualNetwork"][0]
    prefix = client.inventory["IpamPrefix"][0]
    if edit == "name":
        vnet.name.value = vnet.name.value.upper()
    elif edit == "region":
        vnet.location.id = "elsewhere"
    elif edit == "address":
        vnet.address_space.peers = []
    elif edit == "pool":
        prefix.is_pool.value = True
    else:
        prefix.vrf.id = "vrf"
    client.create.reset_mock()
    with pytest.raises(ValueError):
        module.seed(client, "test", entry, True)
    client.create.assert_not_called()


def test_overlap_other_vnet_rejected_but_other_namespace_allowed(client, entry):
    module.seed(client, "test", entry, True)
    entry["name"] = "another-vnet"
    with pytest.raises(ValueError, match="overlaps VNet"):
        module.seed(client, "test", entry, True)
    client.inventory["IpamPrefix"][0].ip_namespace.id = "other-namespace"
    assert module.seed(client, "test", entry, True) == 0


def test_partial_failure_rerun_reuses_prefix(client, entry):
    create = client.create.side_effect

    def fail(kind, **kwargs):
        if kind == "AzureVirtualNetwork":
            raise RuntimeError("secret detail")
        return create(kind=kind, **kwargs)

    client.create.side_effect = fail
    with pytest.raises(RuntimeError):
        module.seed(client, "test", entry, True)
    assert len(client.inventory["IpamPrefix"]) == 1
    client.create.side_effect = create
    assert module.seed(client, "test", entry, True) == 0
    assert (
        len(client.inventory["IpamPrefix"])
        == len(client.inventory["AzureVirtualNetwork"])
        == 1
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "x"),
        ("status", "bad"),
        ("address_space", []),
        ("address_space", ["10.150.0.1/24"]),
        ("address_space", ["10.150.0.0/24", "10.150.0.0/25"]),
    ],
)
def test_invalid_catalog(tmp_path, entry, field, value):
    entry[field] = value
    p = tmp_path / "data.yaml"
    p.write_text(yaml.safe_dump({"virtual_network": entry}))
    with pytest.raises(ValueError):
        module.load_catalog(p)


def test_cli_failures_and_preview(client, monkeypatch):
    monkeypatch.setattr(module, "InfrahubClientSync", lambda: client)
    runner = CliRunner()
    assert runner.invoke(module.app, ["--branch", "test"]).exit_code == 0
    client.create.assert_not_called()
    client.all.side_effect = RuntimeError("sensitive details")
    result = runner.invoke(module.app, ["--branch", "test", "--apply"])
    assert result.exit_code == 1 and "sensitive details" not in result.output


def test_existing_container_not_conflict(client, entry):
    container = client.create(
        kind="IpamPrefix",
        branch="test",
        data={"prefix": "10.0.0.0/8", "ip_namespace": "namespace", "is_pool": False},
    )
    container.save()
    assert module.seed(client, "test", entry, True) == 0
    assert len(client.inventory["IpamPrefix"]) == 2
