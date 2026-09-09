import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml
from typer.testing import CliRunner

from scripts import seed_ipam as module


@pytest.fixture
def catalog():
    return module.load_catalog(Path("data/ipam.yaml"))


def node(values, **extra):
    return SimpleNamespace(
        **{k: SimpleNamespace(value=v) for k, v in values.items()}, **extra
    )


def test_catalog(catalog):
    namespace, prefixes = catalog
    assert namespace == {"name": "default"}
    assert len(prefixes) == 19
    cidrs = [p["prefix"] for p in prefixes]
    assert cidrs.index("fc00::/7") < cidrs.index("fd00::/8")
    assert all(p["status"] == "reserved" and not p["is_pool"] for p in prefixes)


@pytest.mark.parametrize("apply", [False, True])
def test_create_order_and_preview(catalog, apply):
    namespace, prefixes = catalog
    client = MagicMock()
    client.get.side_effect = [node(namespace, id="namespace-id")] + [None] * 19
    client.create.return_value.id = "namespace-id"
    assert module.seed(client, "seed-test", namespace, prefixes, apply) == 0
    if not apply:
        client.create.assert_not_called()
    else:
        calls = client.create.call_args_list
        assert len(calls) == 19
        assert all(c.kwargs["kind"] == "IpamPrefix" for c in calls)
        assert [c.kwargs["data"]["prefix"] for c in calls] == [
            p["prefix"] for p in prefixes
        ]
        assert all(c.kwargs["branch"] == "seed-test" for c in calls)
        assert all(c.kwargs["data"]["ip_namespace"] == "namespace-id" for c in calls)


def test_matching_rerun_and_namespace_isolation(catalog):
    namespace, prefixes = catalog
    client = MagicMock()
    client.get.side_effect = [node(namespace, id="default-id")] + [
        node(p, vrf=SimpleNamespace(id=None)) for p in prefixes
    ]
    assert module.seed(client, "seed-test", namespace, prefixes, True) == 0
    client.create.assert_not_called()
    assert all(
        call.kwargs["hfid"][0] == "default" for call in client.get.call_args_list[1:]
    )


def test_late_conflict_prevents_all_writes(catalog):
    namespace, prefixes = catalog
    client = MagicMock()
    changed = {**prefixes[-1], "description": "operator edit"}
    client.get.side_effect = (
        [node(namespace, id="default-id")]
        + [None] * 18
        + [node(changed, vrf=SimpleNamespace(id=None))]
    )
    assert module.seed(client, "seed-test", namespace, prefixes, True) == 1
    client.create.assert_not_called()


def test_partial_failure_reports_count(catalog, capsys):
    namespace, prefixes = catalog
    client = MagicMock()
    client.get.side_effect = [node(namespace, id="namespace-id")] + [None] * 19
    client.create.return_value.save.side_effect = [None, RuntimeError("failure")]
    with pytest.raises(RuntimeError):
        module.seed(client, "seed-test", namespace, prefixes, True)
    assert "created=1" in capsys.readouterr().err


@pytest.mark.parametrize("change", ["duplicate", "hostbits", "references", "unknown"])
def test_invalid_catalog_rejected(tmp_path, change):
    data = yaml.safe_load(Path("data/ipam.yaml").read_text())
    if change == "duplicate":
        data["prefixes"].append(copy.deepcopy(data["prefixes"][0]))
    elif change == "hostbits":
        data["prefixes"][0]["prefix"] = "192.0.2.1/24"
    elif change == "references":
        data["prefixes"][0]["references"] = []
    else:
        data["prefixes"][0]["vrf"] = "unmanaged"
    path = tmp_path / "data.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError):
        module.load_catalog(path)


def test_cli_api_failure_and_required_branch(monkeypatch):
    client = MagicMock()
    client.get.side_effect = RuntimeError("secret-token")
    monkeypatch.setattr(module, "InfrahubClientSync", lambda: client)
    runner = CliRunner()
    assert runner.invoke(module.app, []).exit_code == 2
    result = runner.invoke(module.app, ["--branch", "test"])
    assert result.exit_code == 1
    assert "secret-token" not in result.output
    client.create.assert_not_called()


def test_sdk_network_values_and_unset_default():
    import ipaddress

    assert (
        module.differences(
            node({"prefix": ipaddress.ip_network("fd00::/8")}), {"prefix": "fd00::/8"}
        )
        == []
    )
    assert module.differences(node({"default": None}), {"default": False}) == []
    assert module.differences(node({"default": True}), {"default": False}) == [
        "default"
    ]


def test_exact_seed_ranges(catalog):
    assert {p["prefix"] for p in catalog[1]} == {
        "192.0.2.0/24",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "2001:db8::/32",
        "3fff::/20",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "fc00::/7",
        "fd00::/8",
        "100.64.0.0/10",
        "169.254.0.0/16",
        "fe80::/10",
        "127.0.0.0/8",
        "::1/128",
        "198.18.0.0/15",
        "2001:2::/48",
        "64:ff9b::/96",
        "64:ff9b:1::/48",
    }


@pytest.mark.parametrize("apply", [False, True])
def test_cli_forwards_apply_and_data(tmp_path, monkeypatch, apply):
    data = tmp_path / "custom.yaml"
    data.write_bytes(Path("data/ipam.yaml").read_bytes())
    client = MagicMock()
    operation = MagicMock(return_value=0)
    monkeypatch.setattr(module, "InfrahubClientSync", lambda: client)
    monkeypatch.setattr(module, "seed", operation)
    args = ["--branch", "validation", "--data", str(data)]
    if apply:
        args.append("--apply")
    result = CliRunner().invoke(module.app, args)
    assert result.exit_code == 0
    assert operation.call_args.args[0] is client
    assert operation.call_args.args[1] == "validation"
    assert operation.call_args.args[-1] is apply


def test_reference_namespace_metadata_is_unmanaged(catalog):
    namespace, prefixes = catalog
    client = MagicMock()
    client.get.side_effect = [
        node(
            {"name": "default", "description": "Operator-owned", "default": True},
            id="default-id",
        )
    ] + [node(p, vrf=SimpleNamespace(id=None)) for p in prefixes]
    assert module.seed(client, "test", namespace, prefixes, True) == 0
    client.create.assert_not_called()


def test_missing_reference_namespace_fails(catalog):
    client = MagicMock()
    client.get.return_value = None
    assert module.seed(client, "test", *catalog, True) == 1
    client.create.assert_not_called()


def test_named_namespace_creation_still_supported(catalog):
    _, prefixes = catalog
    namespace = {"name": "custom", "description": "Reference data", "default": False}
    client = MagicMock()
    client.get.return_value = None
    client.create.return_value.id = "custom-id"
    assert module.seed(client, "test", namespace, prefixes, True) == 0
    assert len(client.create.call_args_list) == 20
    assert client.create.call_args_list[0].kwargs["data"] == namespace


def test_catalog_with_description(tmp_path):
    data = yaml.safe_load(Path("data/ipam.yaml").read_text())
    data["namespace"] = {"name": "custom", "description": "Reference data"}
    path = tmp_path / "catalog.yaml"
    path.write_text(yaml.safe_dump(data))
    namespace, _ = module.load_catalog(path)
    assert namespace == {**data["namespace"], "default": False}
