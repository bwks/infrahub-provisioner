"""Offline checks for moving catalog records and safely deleting an empty namespace."""

import ipaddress
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from scripts import move_ipam_to_default as module
from scripts.seed_ipam import load_catalog


@pytest.fixture
def client():
    c = MagicMock()
    source = NS(id="global-id", name=NS(value="global"), default=NS(value=None))
    target = NS(id="default-id", name=NS(value="default"), default=NS(value=True))
    c.namespaces = [source, target]
    c.nodes = []
    c.saved = []
    c.addresses = 0
    c.reconcile = True
    _, prefixes = load_catalog(Path("data/ipam.yaml"))
    for i, fields in enumerate(prefixes):
        n = NS(
            id=f"prefix-{i}",
            location=source.id,
            ip_namespace=NS(id=source.id),
            vrf=NS(id=None),
            parent=NS(id=None),
            **{k: NS(value=v) for k, v in fields.items()},
        )

        def save(node=n):
            node.location = node.ip_namespace
            node.ip_namespace = NS(id=node.location)
            c.saved.append(node.id)

        n.save = MagicMock(side_effect=save)
        c.nodes.append(n)
    c.all.side_effect = lambda **kwargs: list(c.namespaces)

    def filtered(**kwargs):
        nodes = [n for n in c.nodes if n.location == kwargs["ip_namespace__ids"][0]]
        if c.reconcile:
            for n in nodes:
                network = ipaddress.ip_network(n.prefix.value)
                parents = [
                    p
                    for p in nodes
                    if p is not n
                    and ipaddress.ip_network(p.prefix.value).version == network.version
                    and network.subnet_of(ipaddress.ip_network(p.prefix.value))
                ]
                parent = (
                    max(
                        parents,
                        key=lambda p: ipaddress.ip_network(p.prefix.value).prefixlen,
                    )
                    if parents
                    else None
                )
                n.parent.id = parent.id if parent else None
        return nodes

    c.filters.side_effect = filtered

    def counts(**kwargs):
        nsid = kwargs["variables"]["id"]
        return {
            "BuiltinIPPrefix": {"count": sum(n.location == nsid for n in c.nodes)},
            "BuiltinIPAddress": {"count": c.addresses if nsid == source.id else 0},
        }

    c.execute_graphql.side_effect = counts
    source.delete = MagicMock(side_effect=lambda: c.namespaces.remove(source))
    c.source = source
    return c


def test_preview(client):
    assert module.move(client, "migration") == 0
    assert not client.saved
    client.source.delete.assert_not_called()


def test_move_preserves_ids_and_hierarchy_and_reruns(client):
    original = {n.prefix.value: n.id for n in client.nodes}
    assert module.move(client, "migration", True) == 0
    assert len(client.saved) == 19
    assert all(n.location == "default-id" for n in client.nodes)
    assert {n.prefix.value: n.id for n in client.nodes} == original
    fd = next(n for n in client.nodes if n.prefix.value == "fd00::/8")
    assert fd.parent.id == original["fc00::/7"]
    client.source.delete.assert_called_once()
    assert module.move(client, "migration", True) == 0
    assert len(client.saved) == 19


@pytest.mark.parametrize("conflict", ["duplicate", "unexpected", "address", "edited"])
def test_preflight_blocks_writes(client, conflict):
    if conflict == "duplicate":
        client.nodes.append(client.nodes[0])
    elif conflict == "unexpected":
        client.nodes.pop()
    elif conflict == "address":
        client.addresses = 1
    else:
        client.nodes[-1].description.value = "Operator edit"
    with pytest.raises(ValueError):
        module.move(client, "migration", True)
    assert not client.saved
    client.source.delete.assert_not_called()


def test_failed_save_retains_source_and_can_resume(client):
    node = client.nodes[1]
    operation = node.save.side_effect
    node.save.side_effect = RuntimeError("failure")
    with pytest.raises(RuntimeError):
        module.move(client, "migration", True)
    client.source.delete.assert_not_called()
    node.save.side_effect = operation
    assert module.move(client, "migration", True) == 0
    assert len(client.saved) == 19


def test_unreconciled_hierarchy_prevents_delete(client):
    client.reconcile = False
    with pytest.raises(ValueError, match="hierarchy"):
        module.move(client, "migration", True)
    client.source.delete.assert_not_called()


def test_source_rechecked_immediately_before_delete(client):
    original = client.execute_graphql.side_effect

    def counts(**kwargs):
        result = original(**kwargs)
        if len(client.saved) == 19 and kwargs["variables"]["id"] == client.source.id:
            result["BuiltinIPAddress"]["count"] = 1
        return result

    client.execute_graphql.side_effect = counts
    with pytest.raises(ValueError, match="Source is not empty"):
        module.move(client, "migration", True)
    client.source.delete.assert_not_called()


@pytest.mark.parametrize("args,code", [([], 2), (["--help"], 0)])
def test_cli_usage(monkeypatch, args, code):
    factory = MagicMock()
    monkeypatch.setattr(module, "InfrahubClientSync", factory)
    assert CliRunner().invoke(module.app, args).exit_code == code
    factory.assert_not_called()


def test_cli_failure_is_redacted(client, monkeypatch):
    client.all.side_effect = RuntimeError("secret-token")
    monkeypatch.setattr(module, "InfrahubClientSync", lambda: client)
    result = CliRunner().invoke(module.app, ["--branch", "migration", "--apply"])
    assert result.exit_code == 1
    assert "RuntimeError" in result.stderr and "secret-token" not in result.output
