"""Offline tests of public CLI arguments, output, and exit status."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from typer.testing import CliRunner

from scripts import check_schema, verify_schema


@pytest.fixture
def runner():
    return CliRunner()


@pytest.mark.parametrize("module", [check_schema, verify_schema])
@pytest.mark.parametrize("args,code", [(["--help"], 0), ([], 2), (["--unknown"], 2)])
def test_usage_does_not_contact_server(module, args, code, runner, monkeypatch):
    factory = MagicMock(side_effect=AssertionError("Unexpected SDK construction"))
    name = "InfrahubClient" if module is check_schema else "InfrahubClientSync"
    monkeypatch.setattr(module, name, factory)
    result = runner.invoke(module.app, args)
    assert result.exit_code == code
    if args == ["--help"]:
        assert "branch" in result.stdout
    factory.assert_not_called()


@pytest.mark.parametrize("code", [0, 1])
@pytest.mark.parametrize("custom", [False, True])
def test_check_forwards_options_and_exit_code(
    code, custom, runner, monkeypatch, tmp_path
):
    operation = AsyncMock(return_value=code)
    monkeypatch.setattr(check_schema, "check", operation)
    args = ["--branch", "validation"]
    if custom:
        args += ["--schemas", str(tmp_path)]
    result = runner.invoke(check_schema.app, args)
    assert result.exit_code == code
    operation.assert_awaited_once_with(
        "validation", tmp_path if custom else Path("schemas")
    )


def test_verify_forwards_branch(runner, monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(verify_schema, "InfrahubClientSync", lambda: client)
    operation = MagicMock()
    monkeypatch.setattr(verify_schema, "verify", operation)
    result = runner.invoke(verify_schema.app, ["--branch", "validation"])
    assert result.exit_code == 0
    operation.assert_called_once_with(client, "validation")


@pytest.mark.parametrize("module", [check_schema, verify_schema])
@pytest.mark.parametrize(
    "error,expected",
    [
        (ValueError("Missing schema"), "Missing schema"),
        (RuntimeError("secret-token"), "RuntimeError"),
    ],
)
def test_operational_errors_use_stderr(module, error, expected, runner, monkeypatch):
    if module is check_schema:
        monkeypatch.setattr(module, "check", AsyncMock(side_effect=error))
    else:
        monkeypatch.setattr(module, "InfrahubClientSync", MagicMock())
        monkeypatch.setattr(module, "verify", MagicMock(side_effect=error))
    result = runner.invoke(module.app, ["--branch", "validation"])
    assert result.exit_code == 1
    assert expected in result.stderr
    assert "secret-token" not in result.output
