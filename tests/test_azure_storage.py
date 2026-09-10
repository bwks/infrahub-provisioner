"""Offline storage and backend intent scenarios; no Azure resources are created."""

from copy import deepcopy
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from scripts import check_azure_storage as m


@pytest.fixture
def inventory():
    return {
        m.ACCOUNT: [
            dict(
                id="account",
                name="statetest123",
                resourcegroup="rg",
                location="region",
                **{k: v[0] for k, v in m.CHOICES.items()},
                **m.BOOLEANS,
                **{f: 7 for f in m.RETENTIONS},
            )
        ],
        m.CONTAINER: [
            dict(
                id="container",
                name="tfstate",
                storage_account="account",
                public_access="private",
            )
        ],
        m.BACKEND: [
            dict(
                id="backend",
                name="Connectivity",
                container="container",
                key="connectivity/terraform.tfstate",
                authentication="entra_id",
            )
        ],
        "AzureResourceGroup": [dict(id="rg", name="rg", subscription="subscription")],
        "AzureSubscription": [
            dict(id="subscription", name="subscription", tenant="tenant")
        ],
        "AzureTenant": [dict(id="tenant", name="tenant")],
        "AzureRegion": [dict(id="region", name="region")],
    }


def test_valid_and_empty(inventory):
    assert m.validate(inventory) == []
    assert m.validate({}) == []
    inventory[m.ACCOUNT][0]["public_network_access_enabled"] = False
    # Disabled public access is representable; reachability is not inferred.
    assert m.validate(inventory) == []


@pytest.mark.parametrize("sku", m.SKUS)
def test_standard_skus(inventory, sku):
    inventory[m.ACCOUNT][0]["sku"] = sku
    assert m.validate(inventory) == []


@pytest.mark.parametrize(
    "kind,name",
    [
        (m.ACCOUNT, "abc"),
        (m.ACCOUNT, "a" * 24),
        (m.CONTAINER, "abc"),
        (m.CONTAINER, "a" * 63),
        (m.CONTAINER, "tf-state-123"),
    ],
)
def test_name_boundaries(inventory, kind, name):
    inventory[kind][0]["name"] = name
    assert m.validate(inventory) == []


@pytest.mark.parametrize(
    "kind,name",
    [
        (m.ACCOUNT, "ab"),
        (m.ACCOUNT, "a" * 25),
        (m.ACCOUNT, "Mixed"),
        (m.ACCOUNT, "has-hyphen"),
        (m.CONTAINER, "ab"),
        (m.CONTAINER, "a" * 64),
        (m.CONTAINER, "-start"),
        (m.CONTAINER, "end-"),
        (m.CONTAINER, "double--hyphen"),
        (m.CONTAINER, "Mixed"),
        (m.CONTAINER, "trailing\n"),
    ],
)
def test_invalid_names(inventory, kind, name):
    inventory[kind][0]["name"] = name
    assert any("invalid Azure name" in f for f in m.validate(inventory))


@pytest.mark.parametrize("kind", [m.ACCOUNT, m.CONTAINER])
def test_duplicate_names(inventory, kind):
    n = deepcopy(inventory[kind][0])
    n["id"] = "duplicate"
    inventory[kind].append(n)
    assert any("duplicate name" in f for f in m.validate(inventory))


def test_container_scope(inventory):
    account = deepcopy(inventory[m.ACCOUNT][0])
    account.update(id="other", name="otheraccount")
    container = deepcopy(inventory[m.CONTAINER][0])
    container.update(id="other-container", storage_account="other")
    inventory[m.ACCOUNT].append(account)
    inventory[m.CONTAINER].append(container)
    assert m.validate(inventory) == []


@pytest.mark.parametrize("value", [None, 1, 365])
def test_retention_bounds(inventory, value):
    for field in m.RETENTIONS:
        inventory[m.ACCOUNT][0][field] = value
    assert m.validate(inventory) == []


@pytest.mark.parametrize("value", [0, 366, -1, True, 1.5, "7"])
def test_invalid_retention(inventory, value):
    inventory[m.ACCOUNT][0][m.RETENTIONS[0]] = value
    assert any("integer from 1 to 365" in f for f in m.validate(inventory))


@pytest.mark.parametrize(
    "key", ["x", "x" * 1024, "state/file.tfstate", "/".join(["a"] * 254)]
)
def test_valid_blob_keys(key):
    assert m.valid_blob_key(key)


@pytest.mark.parametrize(
    "key",
    [
        None,
        "",
        "x" * 1025,
        "/".join(["a"] * 255),
        "state\n",
        "a\x00b",
        "bad\ud800",
        "bad\uffff",
        "end.",
        "end/",
        "end\\",
    ],
)
def test_invalid_blob_keys(key):
    assert not m.valid_blob_key(key)


def test_backend_destination_scope_and_case(inventory):
    other = deepcopy(inventory[m.BACKEND][0])
    other["id"] = "other"
    inventory[m.BACKEND].append(other)
    assert any("duplicate backend destination" in f for f in m.validate(inventory))
    other["key"] = other["key"].upper()
    assert m.validate(inventory) == []
    other["key"] = inventory[m.BACKEND][0]["key"]
    container = deepcopy(inventory[m.CONTAINER][0])
    container.update(id="second", name="other-state")
    inventory[m.CONTAINER].append(container)
    other["container"] = "second"
    assert m.validate(inventory) == []


@pytest.mark.parametrize(
    "kind,field",
    [
        (m.ACCOUNT, "resourcegroup"),
        (m.ACCOUNT, "location"),
        (m.CONTAINER, "storage_account"),
        (m.BACKEND, "container"),
        ("AzureResourceGroup", "subscription"),
        ("AzureSubscription", "tenant"),
    ],
)
def test_missing_relationship(inventory, kind, field):
    inventory[kind][0][field] = "missing"
    assert any("missing" in f and field in f for f in m.validate(inventory))


@pytest.mark.parametrize(
    "field,value",
    [
        ("https_only", False),
        ("allow_blob_public_access", True),
        ("allow_shared_key_access", True),
        ("minimum_tls_version", "TLS1_0"),
        ("account_kind", "Storage"),
        ("sku", "Premium_LRS"),
        ("access_tier", "Archive"),
        ("blob_versioning_enabled", None),
    ],
)
def test_account_settings(inventory, field, value):
    inventory[m.ACCOUNT][0][field] = value
    assert any(field in f for f in m.validate(inventory))


def test_backend_security_and_all_findings(inventory):
    inventory[m.CONTAINER][0]["public_access"] = "blob"
    inventory[m.BACKEND][0].update(authentication="access_key", key="", name="")
    assert len(m.validate(inventory)) == 4


def test_cli_pagination_and_read_failure(inventory, monkeypatch):
    client = MagicMock()
    inventory[m.BACKEND] = [
        dict(inventory[m.BACKEND][0], id=f"b{i}", key=f"{i}.tfstate")
        for i in range(105)
    ]

    def all_nodes(kind, **kwargs):
        assert kwargs["branch"] == "test" and "limit" not in kwargs
        attrs, peers = m.FIELDS[kind]
        return (
            NS(
                id=n["id"],
                **{a: NS(value=n.get(a)) for a in attrs},
                **{p: NS(id=n.get(p)) for p in peers},
            )
            for n in inventory[kind]
        )

    client.all.side_effect = all_nodes
    monkeypatch.setattr(m, "InfrahubClientSync", lambda: client)
    runner = CliRunner()
    assert runner.invoke(m.app, ["--branch", "test"]).exit_code == 0
    inventory[m.BACKEND][-1]["key"] = ""
    result = runner.invoke(m.app, ["--branch", "test"])
    assert result.exit_code == 1 and "b104" in result.output
    client.all.return_value = []
    client.all.side_effect = None
    result = runner.invoke(m.app, ["--branch", "test"])
    assert result.exit_code == 0 and "empty" in result.output
    client.all.side_effect = RuntimeError("secret")
    result = runner.invoke(m.app, ["--branch", "test"])
    assert result.exit_code == 1 and "secret" not in result.output
    client.create.assert_not_called()
