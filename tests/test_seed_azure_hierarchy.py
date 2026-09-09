"""Offline seed lifecycle tests; no Azure or Infrahub access."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest
import yaml
from typer.testing import CliRunner

from scripts import seed_azure_hierarchy as module


@pytest.fixture
def catalog():
    return module.load_catalog(Path("data/azure_hierarchy.yaml"))


@pytest.fixture
def client():
    client = MagicMock()
    client.inventory = {
        kind: []
        for kind in ("AzureTenant", "AzureManagementGroup", "AzureSubscription")
    }
    client.all.side_effect = lambda kind, **kwargs: list(client.inventory[kind])

    def create(kind, data, **kwargs):
        values = (
            {"name": None, "tenant_id": None}
            if kind == "AzureTenant"
            else {
                "management_group_id": None,
                "display_name": None,
                "tenant": None,
                "parent": None,
            }
        )
        values.update(data)
        node = NS(
            id=f"node-{client.create.call_count}",
            **{
                key: NS(
                    **(
                        {"id": value}
                        if key in ("tenant", "parent")
                        else {"value": value}
                    )
                )
                for key, value in values.items()
            },
        )
        node.save = MagicMock(side_effect=lambda: client.inventory[kind].append(node))
        return node

    client.create.side_effect = create
    return client


def test_catalog_exact_tree(catalog):
    assert catalog["tenant"] == {"name": "fake-corp"}
    assert catalog["root"] == {"display_name": "Tenant root group"}
    assert {
        g["management_group_id"]: g["parent"] for g in catalog["management_groups"]
    } == {
        "fake-corp": None,
        "platform": "fake-corp",
        "security": "platform",
        "management": "platform",
        "connectivity": "platform",
        "identity": "platform",
        "landing-zones": "fake-corp",
        "online": "landing-zones",
        "corp": "landing-zones",
        "local": "landing-zones",
        "sandbox": "fake-corp",
        "decommissioned": "fake-corp",
    }


def test_preview_no_writes(client, catalog, capsys):
    assert module.seed(client, "validation", catalog) == 0
    client.create.assert_not_called()
    assert "missing=14, skipped=0, conflicting=0" in capsys.readouterr().out
    for call in client.all.call_args_list:
        assert call.kwargs["branch"] == "validation"
        assert "limit" not in call.kwargs and "offset" not in call.kwargs


def test_creation_order_and_unchanged_rerun(client, catalog, capsys):
    assert module.seed(client, "validation", catalog, True) == 0
    calls = client.create.call_args_list
    assert len(calls) == 14
    assert calls[0].kwargs["data"] == {"name": "fake-corp"}
    assert calls[1].kwargs["data"] == {
        "display_name": "Tenant root group",
        "tenant": "node-1",
    }
    created_ids = {"node-1", "node-2"}
    for call, node in zip(
        calls[2:], client.inventory["AzureManagementGroup"][1:], strict=True
    ):
        assert call.kwargs["data"]["parent"] in created_ids
        assert call.kwargs["branch"] == "validation"
        created_ids.add(node.id)
    client.create.reset_mock()
    assert module.seed(client, "validation", catalog, True) == 0
    client.create.assert_not_called()
    assert "missing=0, skipped=14, conflicting=0" in capsys.readouterr().out


def test_preserve_populated_guids_and_unmanaged_data(client, catalog):
    assert module.seed(client, "validation", catalog, True) == 0
    tenant = client.inventory["AzureTenant"][0]
    root = client.inventory["AzureManagementGroup"][0]
    guid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    tenant.tenant_id.value = guid
    root.management_group_id.value = guid
    root.description = NS(value="Operator description")
    extra = client.create(
        kind="AzureManagementGroup",
        data={
            "management_group_id": "unmanaged",
            "display_name": "Unmanaged",
            "tenant": tenant.id,
            "parent": root.id,
        },
    )
    extra.save()
    client.create.reset_mock()
    assert module.seed(client, "validation", catalog, True) == 0
    client.create.assert_not_called()
    assert tenant.tenant_id.value == root.management_group_id.value == guid
    assert root.description.value == "Operator description"
    assert len(client.inventory["AzureManagementGroup"]) == 14


@pytest.mark.parametrize(
    "edit",
    [
        "display_name",
        "parent",
        "duplicate_group",
        "duplicate_root",
        "duplicate_tenant",
        "tenant_case",
        "invalid_guid",
        "invalid_subscription",
    ],
)
def test_conflicts_prevent_all_writes(client, catalog, edit):
    assert module.seed(client, "validation", catalog, True) == 0
    groups = client.inventory["AzureManagementGroup"]
    tenant = client.inventory["AzureTenant"][0]
    # Leave an unrelated earlier group missing; even late conflicts block its creation.
    groups.pop(-2)  # sandbox
    if edit == "display_name":
        groups[-1].display_name.value = "Operator edit"
    elif edit == "parent":
        groups[-1].parent.id = groups[2].id
    elif edit == "duplicate_group":
        duplicate = deepcopy(groups[-1])
        duplicate.id = "duplicate"
        duplicate.management_group_id.value = (
            duplicate.management_group_id.value.upper()
        )
        groups.append(duplicate)
    elif edit == "duplicate_root":
        duplicate = deepcopy(groups[0])
        duplicate.id = "duplicate"
        groups.append(duplicate)
    elif edit == "duplicate_tenant":
        duplicate = deepcopy(tenant)
        duplicate.id = "duplicate"
        client.inventory["AzureTenant"].append(duplicate)
    elif edit == "tenant_case":
        tenant.name.value = "FAKE-CORP"
    elif edit == "invalid_guid":
        tenant.tenant_id.value = "not-a-guid"
    else:
        client.inventory["AzureSubscription"].append(
            NS(
                id="sub",
                subscription_id=NS(value=None),
                tenant=NS(id=tenant.id),
                management_group=NS(id=None),
            )
        )
    client.create.reset_mock()
    assert module.seed(client, "validation", catalog, True) == 1
    client.create.assert_not_called()


def test_case_insensitive_group_identity(client, catalog):
    assert module.seed(client, "validation", catalog, True) == 0
    client.inventory["AzureManagementGroup"][
        -1
    ].management_group_id.value = "DECOMMISSIONED"
    client.create.reset_mock()
    assert module.seed(client, "validation", catalog, True) == 0
    client.create.assert_not_called()


def test_partial_failure_and_resume(client, catalog, capsys):
    operation = client.create.side_effect

    def fail_once(**kwargs):
        node = operation(**kwargs)
        if client.create.call_count == 5:
            node.save.side_effect = RuntimeError("secret-token")
        return node

    client.create.side_effect = fail_once
    with pytest.raises(RuntimeError):
        module.seed(client, "validation", catalog, True)
    assert "created=4" in capsys.readouterr().err
    client.create.side_effect = operation
    assert module.seed(client, "validation", catalog, True) == 0
    assert len(client.inventory["AzureTenant"]) == 1
    assert len(client.inventory["AzureManagementGroup"]) == 13


@pytest.mark.parametrize(
    "edit",
    [
        "duplicate",
        "cycle",
        "unknown_parent",
        "bad_id",
        "empty",
        "guid_field",
        "wrong_type",
        "deep",
    ],
)
def test_invalid_catalog(tmp_path, edit):
    data = yaml.safe_load(Path("data/azure_hierarchy.yaml").read_text())
    groups = data["management_groups"]
    if edit == "duplicate":
        groups.append(
            {
                **groups[-1],
                "management_group_id": groups[-1]["management_group_id"].upper(),
            }
        )
    elif edit == "cycle":
        groups[0]["parent"] = "platform"
    elif edit == "unknown_parent":
        groups[-1]["parent"] = "absent"
    elif edit == "bad_id":
        groups[-1]["management_group_id"] = "invalid."
    elif edit == "empty":
        data["management_groups"] = []
    elif edit == "guid_field":
        data["tenant"]["tenant_id"] = None
    elif edit == "wrong_type":
        groups[-1]["display_name"] = False
    else:
        data["management_groups"] = [
            {
                "management_group_id": f"g{i}",
                "display_name": f"G{i}",
                "parent": f"g{i - 1}" if i else None,
            }
            for i in range(7)
        ]
    path = tmp_path / "catalog.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError):
        module.load_catalog(path)


def test_catalog_order_independent(tmp_path, catalog):
    data = deepcopy(catalog)
    data["management_groups"].reverse()
    path = tmp_path / "catalog.yaml"
    path.write_text(yaml.safe_dump(data))
    loaded = module.load_catalog(path)
    seen = set()
    for group in loaded["management_groups"]:
        assert group["parent"] is None or group["parent"] in seen
        seen.add(group["management_group_id"])


@pytest.mark.parametrize("args,code", [([], 2), (["--help"], 0), (["--unknown"], 2)])
def test_cli_usage(monkeypatch, args, code):
    factory = MagicMock()
    monkeypatch.setattr(module, "InfrahubClientSync", factory)
    assert CliRunner().invoke(module.app, args).exit_code == code
    factory.assert_not_called()


@pytest.mark.parametrize("apply", [False, True])
def test_cli_options(monkeypatch, tmp_path, client, apply):
    path = tmp_path / "custom.yaml"
    path.write_bytes(Path("data/azure_hierarchy.yaml").read_bytes())
    monkeypatch.setattr(module, "InfrahubClientSync", lambda: client)
    args = ["--branch", "validation", "--data", str(path)] + (
        ["--apply"] if apply else []
    )
    assert CliRunner().invoke(module.app, args).exit_code == 0
    assert client.create.call_count == (14 if apply else 0)


def test_cli_read_failure_redacted(monkeypatch, client):
    client.all.side_effect = RuntimeError("secret-token")
    monkeypatch.setattr(module, "InfrahubClientSync", lambda: client)
    result = CliRunner().invoke(module.app, ["--branch", "validation", "--apply"])
    assert result.exit_code == 1
    assert "RuntimeError" in result.stderr and "secret-token" not in result.output
    client.create.assert_not_called()
