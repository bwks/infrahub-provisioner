"""Offline cloud catalog validation and create-only seed behavior."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest
import yaml
from typer.testing import CliRunner

from scripts import seed_cloud_locations as module


@pytest.fixture
def entries():
    return module.load_catalog(Path("data/cloud_locations.yaml"))


@pytest.fixture
def client():
    c = MagicMock()
    c.inventory = []
    c.all.side_effect = lambda **kwargs: [
        n
        for n in c.inventory
        if kwargs["kind"] == "LocationGeneric" or n.get_kind() == kwargs["kind"]
    ]

    def create(kind, branch, data):
        assert branch == "validation"
        node = NS(
            id=str(len(c.inventory)),
            name=NS(value=data["name"]),
            display_name=NS(value=data["display_name"]),
            parent=NS(id=data.get("parent")),
            status=NS(value="unmanaged"),
            get_kind=lambda: kind,
        )
        node.save = MagicMock(side_effect=lambda: c.inventory.append(node))
        return node

    c.create.side_effect = create
    return c


def test_catalog_snapshot_and_mapping(entries):
    regions = [e for e in entries if e["kind"] == "AzureRegion"]
    assert len(regions) == 57
    assert len(entries) == 66
    expected = {
        "north-america": 12,
        "south-america": 3,
        "europe": 19,
        "asia-pacific": 17,
        "middle-east": 4,
        "africa": 2,
    }
    assert {
        area: sum(r["parent"] == "cloud-azure-" + area for r in regions)
        for area in expected
    } == expected
    by_name = {e["name"]: e for e in entries}
    for name, area in [
        ("eastus", "north-america"),
        ("brazilsouth", "south-america"),
        ("northeurope", "europe"),
        ("australiaeast", "asia-pacific"),
        ("israelcentral", "middle-east"),
        ("southafricanorth", "africa"),
    ]:
        assert by_name[name]["parent"] == "cloud-azure-" + area
    assert not any(e["parent"] == "cloud-aws" for e in entries)
    assert all("china" not in r["name"] and "usgov" not in r["name"] for r in regions)
    assert by_name["australiacentral2"]["display_name"] == "Australia Central 2"


@pytest.mark.parametrize(
    "change",
    ["duplicate", "bad_parent", "cycle", "missing_group", "bad_id", "blank", "extra"],
)
def test_bad_catalog(tmp_path, change):
    data = yaml.safe_load(Path("data/cloud_locations.yaml").read_text())
    if change == "duplicate":
        data["regions"].append(deepcopy(data["regions"][0]))
    elif change == "bad_parent":
        data["regions"][0]["parent"] = "cloud-aws"
    elif change == "cycle":
        data["groups"][0]["parent"] = "cloud-azure"
    elif change == "missing_group":
        data["groups"].pop()
    elif change == "bad_id":
        data["regions"][0]["name"] = "Australia East"
    elif change == "blank":
        data["regions"][0]["display_name"] = " "
    else:
        data["regions"][0]["status"] = "planned"
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError):
        module.load_catalog(path)


def test_parent_order_independent_of_yaml(tmp_path, entries):
    data = yaml.safe_load(Path("data/cloud_locations.yaml").read_text())
    data["groups"].reverse()
    data["regions"].reverse()
    path = tmp_path / "reversed.yaml"
    path.write_text(yaml.safe_dump(data))
    resolved = {None}
    for entry in module.load_catalog(path):
        assert entry["parent"] in resolved
        resolved.add(entry["name"])
    assert len(resolved) == len(entries) + 1


def test_preview_then_apply_and_preserve_status(client, entries, capsys):
    assert module.seed(client, "validation", entries) == 0
    client.create.assert_not_called()
    assert module.seed(client, "validation", entries, True) == 0
    assert len(client.inventory) == 66
    for node in client.inventory:
        node.status.value = "active"
    ids = {n.id for n in client.inventory}
    assert module.seed(client, "validation", entries, True) == 0
    assert {n.id for n in client.inventory} == ids
    assert all(n.status.value == "active" for n in client.inventory)
    assert client.create.call_count == 66
    assert "missing=0, skipped=66, conflicting=0" in capsys.readouterr().out
    assert {call.kwargs["kind"] for call in client.all.call_args_list} == {
        "LocationGeneric",
        "LocationGroup",
        "AzureRegion",
    }


@pytest.mark.parametrize(
    "conflict", ["name", "display_name", "parent", "kind", "duplicate"]
)
def test_conflicts_prevent_all_writes(client, entries, conflict):
    module.seed(client, "validation", entries, True)
    node = client.inventory[0]
    if conflict in ("name", "display_name"):
        getattr(node, conflict).value = node.name.value.upper()
    elif conflict == "parent":
        node.parent.id = "wrong"
    elif conflict == "kind":
        node.get_kind = lambda: "AnotherLocation"
    else:
        client.inventory.append(node)
    client.inventory.pop()  # At least one entry is missing as well as a conflict.
    if conflict == "duplicate":
        client.inventory.append(node)
    client.create.reset_mock()
    assert module.seed(client, "validation", entries, True) == 1
    client.create.assert_not_called()


def test_partial_failure_can_resume(client, entries, capsys):
    create = client.create.side_effect

    def failing(**kwargs):
        if len(client.inventory) == 3:
            raise RuntimeError("private detail")
        return create(**kwargs)

    client.create.side_effect = failing
    with pytest.raises(RuntimeError):
        module.seed(client, "validation", entries, True)
    assert "created=3" in capsys.readouterr().err
    client.create.side_effect = create
    assert module.seed(client, "validation", entries, True) == 0
    assert len(client.inventory) == 66


def test_cli_success_and_read_failure(monkeypatch, client):
    monkeypatch.setattr(module, "InfrahubClientSync", lambda: client)
    runner = CliRunner()
    assert runner.invoke(module.app, ["--branch", "validation"]).exit_code == 0
    assert (
        runner.invoke(module.app, ["--branch", "validation", "--apply"]).exit_code == 0
    )
    client.all.side_effect = RuntimeError("secret-token")
    result = runner.invoke(module.app, ["--branch", "validation", "--apply"])
    assert result.exit_code == 1
    assert "secret-token" not in result.output
    assert "RuntimeError" in result.output


def test_cli_invalid_catalog(monkeypatch, tmp_path, client):
    monkeypatch.setattr(module, "InfrahubClientSync", lambda: client)
    path = tmp_path / "bad.yaml"
    path.write_text("{}")
    result = CliRunner().invoke(
        module.app, ["--branch", "validation", "--data", str(path)]
    )
    assert result.exit_code == 1
    client.all.assert_not_called()
