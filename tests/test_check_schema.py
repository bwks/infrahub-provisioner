"""Offline regression tests for schema checking."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts import check_schema


@pytest.fixture
def client(monkeypatch):
    client = MagicMock()
    client.schema.check = AsyncMock()
    monkeypatch.setattr(check_schema, "InfrahubClient", lambda: client)
    return client


def test_server_rejection_returns_failure(tmp_path, client, capsys):
    (tmp_path / "schema.yml").write_text("version: '1.0'\nnodes: []\n")
    client.schema.check.return_value = False, {"error": "Missing generic"}
    assert asyncio.run(check_schema.check("validation", tmp_path)) == 1
    assert "Missing generic" in capsys.readouterr().err


def test_empty_directory_fails_before_contacting_server(tmp_path, monkeypatch):
    factory = MagicMock()
    monkeypatch.setattr(check_schema, "InfrahubClient", factory)
    with pytest.raises(ValueError, match="No schema YAML"):
        asyncio.run(check_schema.check("validation", tmp_path))
    factory.assert_not_called()


def test_non_mapping_yaml_is_not_submitted(tmp_path, client):
    (tmp_path / "schema.yml").write_text("- invalid\n")
    with pytest.raises(ValueError, match="expected a YAML mapping"):
        asyncio.run(check_schema.check("validation", tmp_path))
    client.schema.check.assert_not_awaited()


def test_recursive_schema_discovery(tmp_path, client):
    (tmp_path / "local").mkdir()
    (tmp_path / "upstream.yml").write_text("version: '1.0'\nnodes: []\n")
    (tmp_path / "local/extension.yaml").write_text("version: '1.0'\ngenerics: []\n")
    (tmp_path / "local/README.md").write_text("Not a schema")
    client.schema.check.return_value = True, {"diff": {}}
    assert asyncio.run(check_schema.check("validation", tmp_path)) == 0
    assert client.schema.validate.call_count == 2
    assert client.schema.check.call_args.kwargs == {
        "schemas": [
            {"version": "1.0", "generics": []},
            {"version": "1.0", "nodes": []},
        ],
        "branch": "validation",
    }
