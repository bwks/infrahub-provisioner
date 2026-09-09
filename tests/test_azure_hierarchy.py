"""Offline hierarchy scenarios and SDK/CLI boundary tests."""

from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from scripts import check_azure_hierarchy as hierarchy
from scripts.check_azure_hierarchy import Group, Subscription, Tenant, validate


@pytest.fixture
def inventory():
    return (
        [
            Tenant("t1", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"),
            Tenant("t2", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        ],
        [
            Group("r1", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "t1"),
            Group("r2", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "t2"),
            Group("g1", "shared", "t1", "r1"),
            Group("g2", "SHARED", "t2", "r2"),
        ],
        [
            Subscription("s1", "cccccccc-cccc-4ccc-8ccc-cccccccccccc", "t1", "g1"),
            Subscription("s2", "dddddddd-dddd-4ddd-8ddd-dddddddddddd", "t2", "r2"),
        ],
    )


def test_valid_multi_tenant(inventory):
    assert validate(*inventory) == []


def test_empty():
    assert validate([], [], []) == []


@pytest.mark.parametrize(
    "change,expected",
    [
        (lambda t, g, s: g.pop(0), "missing parent"),
        (lambda t, g, s: g.append(Group("extra", "extra", "t1")), "found 2"),
        (
            lambda t, g, s: g.__setitem__(
                0, replace(g[0], management_group_id="wrong")
            ),
            "ID must match",
        ),
        (
            lambda t, g, s: g.__setitem__(2, replace(g[2], parent="g1")),
            "cycle/self-parenting",
        ),
        (
            lambda t, g, s: g.__setitem__(0, replace(g[0], parent="g1")),
            "cycle/self-parenting",
        ),
        (
            lambda t, g, s: g.__setitem__(2, replace(g[2], parent="r2")),
            "cross-tenant parent",
        ),
        (
            lambda t, g, s: g.__setitem__(2, replace(g[2], tenant="unknown")),
            "unknown tenant",
        ),
        (
            lambda t, g, s: s.__setitem__(0, replace(s[0], management_group=None)),
            "unknown management group",
        ),
        (
            lambda t, g, s: s.__setitem__(0, replace(s[0], management_group="missing")),
            "unknown management group",
        ),
        (
            lambda t, g, s: s.__setitem__(0, replace(s[0], management_group="g2")),
            "belongs to tenant",
        ),
        (
            lambda t, g, s: s.__setitem__(0, replace(s[0], tenant=None)),
            "unknown tenant",
        ),
        (
            lambda t, g, s: t.append(
                Tenant("t3", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA")
            ),
            "Duplicate tenant ID",
        ),
        (
            lambda t, g, s: g.append(Group("g3", "SHARED", "t1", "r1")),
            "Duplicate management-group ID",
        ),
    ],
)
def test_invalid_inventory(inventory, change, expected):
    change(*inventory)
    findings = validate(*inventory)
    assert any(expected in finding for finding in findings)
    assert all("Infrahub" in finding for finding in findings)


def test_tenant_without_groups():
    assert (
        "found 0"
        in validate([Tenant("t", "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")], [], [])[0]
    )


@pytest.mark.parametrize(
    "depth,valid", [(0, True), (6, True), (7, False), (1001, False)]
)
def test_depth_boundary(depth, valid):
    groups = [Group("0", "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", "t")]
    groups.extend(
        Group(str(i), f"group{i}", "t", str(i - 1)) for i in range(1, depth + 1)
    )
    findings = validate(
        [Tenant("t", "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")],
        groups,
        [Subscription("s", "ffffffff-ffff-4fff-8fff-ffffffffffff", "t", str(depth))],
    )
    assert (not findings) == valid
    if not valid:
        assert "exceeds six" in findings[-1]


@pytest.mark.parametrize(
    "identifier,valid",
    [
        ("a", True),
        ("0", True),
        ("A-b_c.d(e)", True),
        ("a" * 90, True),
        ("", False),
        ("a" * 91, False),
        ("-a", False),
        ("_a", False),
        ("a.", False),
        ("a b", False),
        ("a/b", False),
        ("a\n", False),
    ],
)
def test_identifier_rules(identifier, valid):
    findings = validate(
        [Tenant("t", "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")],
        [
            Group("r", "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", "t"),
            Group("g", identifier, "t", "r"),
        ],
        [],
    )
    assert (not findings) == valid


def test_all_findings_are_reported(inventory):
    tenants, groups, subscriptions = inventory
    groups.append(Group("bad", "bad id", None, "missing"))
    subscriptions.append(Subscription("bad-sub", None, None))
    findings = validate(tenants, groups, subscriptions)
    assert len(findings) == 5


def test_sdk_reads_all_without_pagination_limit(capsys):
    client = MagicMock()
    client.all.side_effect = [
        [NS(id="t", tenant_id=NS(value="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"))],
        [
            NS(
                id="g",
                management_group_id=NS(value="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
                tenant=NS(id="t"),
                parent=NS(id=None),
            )
        ],
        [
            NS(
                id="s",
                subscription_id=NS(value="ffffffff-ffff-4fff-8fff-ffffffffffff"),
                tenant=NS(id="t"),
                management_group=NS(id="g"),
            )
        ],
    ]
    assert hierarchy.check(client, "validation") == 0
    assert "1 tenants, 1 groups, 1 subscriptions" in capsys.readouterr().out
    assert [c.kwargs["kind"] for c in client.all.call_args_list] == [
        "AzureTenant",
        "AzureManagementGroup",
        "AzureSubscription",
    ]
    for call in client.all.call_args_list:
        assert call.kwargs["branch"] == "validation"
        assert "limit" not in call.kwargs and "offset" not in call.kwargs
    assert len(client.method_calls) == 3


@pytest.mark.parametrize(
    "mode,code,output",
    [
        ("empty", 0, "empty Azure inventory"),
        ("invalid", 1, "expected exactly one root"),
        ("error", 1, "RuntimeError"),
    ],
)
def test_cli_results(monkeypatch, mode, code, output):
    client = MagicMock()
    if mode == "error":
        client.all.side_effect = RuntimeError("secret-token")
    else:
        client.all.side_effect = [
            (
                []
                if mode == "empty"
                else [
                    NS(
                        id="t",
                        tenant_id=NS(value="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
                    )
                ]
            ),
            [],
            [],
        ]
    monkeypatch.setattr(hierarchy, "InfrahubClientSync", lambda: client)
    result = CliRunner().invoke(hierarchy.app, ["--branch", "validation"])
    assert result.exit_code == code
    assert output in result.output
    assert "secret-token" not in result.output
    if code:
        assert output in result.stderr


@pytest.mark.parametrize("args,code", [([], 2), (["--help"], 0), (["--unknown"], 2)])
def test_usage(monkeypatch, args, code):
    factory = MagicMock(side_effect=AssertionError("Unexpected SDK call"))
    monkeypatch.setattr(hierarchy, "InfrahubClientSync", factory)
    assert CliRunner().invoke(hierarchy.app, args).exit_code == code
    factory.assert_not_called()


@pytest.mark.parametrize(
    "tenant_id,root_id",
    [
        (None, None),
        (None, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        ("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", None),
    ],
)
def test_pending_guids(tenant_id, root_id):
    assert (
        validate(
            [Tenant("t", tenant_id)],
            [Group("r", root_id, "t"), Group("g", "platform", "t", "r")],
            [Subscription("s", None, "t", "g")],
        )
        == []
    )


def test_multiple_pending_tenants_are_not_duplicates():
    assert (
        validate(
            [Tenant("t1", None), Tenant("t2", None)],
            [Group("r1", None, "t1"), Group("r2", None, "t2")],
            [],
        )
        == []
    )


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-a-guid",
        "aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa",
        "{aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa}",
    ],
)
def test_invalid_guid_values(value):
    findings = validate(
        [Tenant("t", value)],
        [Group("r", value, "t")],
        [Subscription("s", value, "t", "r")],
    )
    assert sum("invalid Azure GUID" in finding for finding in findings) == 3


def test_nonroot_still_requires_id():
    findings = validate(
        [Tenant("t", None)], [Group("r", None, "t"), Group("g", None, "t", "r")], []
    )
    assert (
        len(findings) == 1 and "non-root management-group ID is required" in findings[0]
    )


def test_pending_output(capsys):
    client = MagicMock()
    client.all.side_effect = [
        [NS(id="t", tenant_id=NS(value=None))],
        [
            NS(
                id="r",
                management_group_id=NS(value=None),
                tenant=NS(id="t"),
                parent=NS(id=None),
            )
        ],
        [],
    ]
    assert hierarchy.check(client, "validation") == 0
    assert "Azure identifiers pending: 2" in capsys.readouterr().out
