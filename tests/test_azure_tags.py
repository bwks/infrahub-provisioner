"""Offline Azure tag ownership and Azure naming rule checks."""

from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from scripts import check_azure_tags as module
from scripts.check_azure_tags import Tag, validate


@pytest.mark.parametrize("kind", module.OWNER_KINDS)
def test_independent_values_and_empty_value(kind):
    tags = [
        Tag("a", "Environment", "Production", "one"),
        Tag("b", "Environment", "Test", "two"),
        Tag("c", "Empty", "", "one"),
    ]
    assert validate(tags, {"one": kind, "two": kind}) == []


def test_empty_inventory():
    assert validate([], {}) == []


@pytest.mark.parametrize("key", ["", None, "x" * 513, *[f"a{c}b" for c in "<>%&\\?/"]])
def test_invalid_keys(key):
    assert validate([Tag("tag", key, "ok", "owner")], {"owner": "AzureSubscription"})


@pytest.mark.parametrize("value", [None, "x" * 257])
def test_invalid_values(value):
    assert validate([Tag("tag", "key", value, "owner")], {"owner": "AzureSubscription"})


def test_boundaries():
    assert (
        validate(
            [Tag("tag", "k" * 512, "v" * 256, "owner")], {"owner": "AzureSubscription"}
        )
        == []
    )
    tags = [Tag(str(i), f"key{i}", "", "owner") for i in range(50)]
    assert validate(tags, {"owner": "AzureSubscription"}) == []
    assert (
        "limit of 50"
        in validate(
            tags + [Tag("last", "last", "", "owner")], {"owner": "AzureSubscription"}
        )[0]
    )


def test_case_insensitive_keys_report_both_ids():
    findings = validate(
        [
            Tag("a", "Environment", "Production", "owner"),
            Tag("b", "environment", "production", "owner"),
        ],
        {"owner": "AzureSubscription"},
    )
    assert len(findings) == 1 and "a" in findings[0] and "b" in findings[0]


@pytest.mark.parametrize("owners", [{}, {"owner": "AzureRegion"}])
def test_bad_owner(owners):
    assert "unsupported owner" in validate([Tag("tag", "k", "v", "owner")], owners)[0]


def test_all_findings():
    findings = validate([Tag("tag", "", None, None)], {})
    assert len(findings) == 3


def test_cli(monkeypatch):
    client = MagicMock()
    client.all.return_value = []
    monkeypatch.setattr(module, "InfrahubClientSync", lambda: client)
    runner = CliRunner()
    result = runner.invoke(module.app, ["--branch", "test"])
    assert result.exit_code == 0 and "empty" in result.output
    assert all(c.kwargs["branch"] == "test" for c in client.all.call_args_list)
    client.all.side_effect = [
        [],
        [NS(id="bad", key=NS(value="k"), value=NS(value="v"), owner=NS(id=None))],
    ]
    assert runner.invoke(module.app, ["--branch", "test"]).exit_code == 1
    client.all.side_effect = RuntimeError("secret-token")
    result = runner.invoke(module.app, ["--branch", "test"])
    assert (
        result.exit_code == 1
        and "RuntimeError" in result.output
        and "secret-token" not in result.output
    )
    client.create.assert_not_called()
