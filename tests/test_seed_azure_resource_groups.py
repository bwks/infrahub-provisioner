"""Offline resource-group seed tests; region values here are test fixtures."""

from copy import deepcopy
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest
import yaml
from typer.testing import CliRunner

from scripts import seed_azure_resource_groups as module


@pytest.fixture
def entries():
    return [
        {
            "name": "rg-conn-prd-network",
            "tenant": "fake-corp",
            "subscription": "Connectivity",
            "region": "testregion",
            "status": "planned",
        }
    ]


@pytest.fixture
def client():
    c = MagicMock()
    c.inventory = {
        "AzureTenant": [NS(id="tenant", name=NS(value="fake-corp"))],
        "AzureSubscription": [
            NS(id="sub", name=NS(value="Connectivity"), tenant=NS(id="tenant"))
        ],
        "AzureRegion": [NS(id="region", name=NS(value="testregion"))],
        "AzureResourceGroup": [],
    }
    c.all.side_effect = lambda kind, **kwargs: list(c.inventory[kind])

    def create(kind, branch, data):
        assert kind == "AzureResourceGroup" and branch == "test"
        n = NS(
            id="rg",
            name=NS(value=data["name"]),
            subscription=NS(id=data["subscription"]),
            location=NS(id=data["location"]),
            status=NS(value=data["status"]),
            tags=NS(peers=[]),
        )
        n.save = MagicMock(side_effect=lambda: c.inventory[kind].append(n))
        return n

    c.create.side_effect = create
    return c


def test_preview_apply_rerun(client, entries):
    assert module.seed(client, "test", entries) == 0
    client.create.assert_not_called()
    assert module.seed(client, "test", entries, True) == 0
    n = client.inventory["AzureResourceGroup"][0]
    assert n.subscription.id == "sub" and n.location.id == "region"
    n.status.value = "active"
    n.tags.peers = ["existing-tag"]
    assert module.seed(client, "test", entries, True) == 0
    assert client.create.call_count == n.save.call_count == 1
    assert n.status.value == "active" and n.tags.peers == ["existing-tag"]
    assert all(c.kwargs["branch"] == "test" for c in client.all.call_args_list)
    assert all("limit" not in c.kwargs for c in client.all.call_args_list)


@pytest.mark.parametrize("kind", ["AzureTenant", "AzureSubscription", "AzureRegion"])
@pytest.mark.parametrize("duplicate", [False, True])
def test_missing_or_ambiguous_dependencies(client, entries, kind, duplicate):
    if duplicate:
        client.inventory[kind].append(deepcopy(client.inventory[kind][0]))
    else:
        client.inventory[kind] = []
    assert module.seed(client, "test", entries, True) == 1
    client.create.assert_not_called()


@pytest.mark.parametrize("edit", ["region", "case", "duplicate"])
def test_existing_conflict_prevents_all_writes(client, entries, edit):
    module.seed(client, "test", entries, True)
    n = client.inventory["AzureResourceGroup"][0]
    if edit == "region":
        n.location.id = "other-region"
    elif edit == "case":
        n.name.value = n.name.value.upper()
    else:
        client.inventory["AzureResourceGroup"].append(deepcopy(n))
    entries.append({**entries[0], "name": "another-rg"})
    client.create.reset_mock()
    assert module.seed(client, "test", entries, True) == 1
    client.create.assert_not_called()


def test_same_name_other_subscription_preserved(client, entries):
    module.seed(client, "test", entries, True)
    client.inventory["AzureResourceGroup"][0].subscription.id = "other-sub"
    assert module.seed(client, "test", entries, True) == 0
    assert len(client.inventory["AzureResourceGroup"]) == 2


@pytest.mark.parametrize("edit", ["duplicate", "empty", "status", "extra"])
def test_invalid_catalog(tmp_path, entries, edit):
    if edit == "duplicate":
        entries.append({**entries[0], "name": entries[0]["name"].upper()})
    elif edit == "empty":
        entries[0]["region"] = ""
    elif edit == "status":
        entries[0]["status"] = "incorrect"
    else:
        entries[0]["tags"] = {}
    path = tmp_path / "catalog.yaml"
    path.write_text(yaml.safe_dump({"resource_groups": entries}))
    with pytest.raises(ValueError):
        module.load_catalog(path)


def test_cli_and_failure(monkeypatch, tmp_path, entries, client):
    path = tmp_path / "catalog.yaml"
    path.write_text(yaml.safe_dump({"resource_groups": entries}))
    monkeypatch.setattr(module, "InfrahubClientSync", lambda: client)
    args = ["--branch", "test", "--data", str(path), "--apply"]
    assert CliRunner().invoke(module.app, args).exit_code == 0
    client.all.side_effect = RuntimeError("secret")
    r = CliRunner().invoke(module.app, args)
    assert r.exit_code == 1 and "secret" not in r.output


def test_write_failure_reports_progress(client, entries, capsys):
    client.create.side_effect = RuntimeError("private")
    with pytest.raises(RuntimeError):
        module.seed(client, "test", entries, True)
    assert "created=0" in capsys.readouterr().err
