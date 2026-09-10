"""Offline private-zone selector and pinned Microsoft catalog checks."""

import re

import pytest
import yaml
from jinja2 import Template

from scripts import dns_zone_catalog as m
from scripts.check_azure_dns import ZONE, validate


def test_pinned_catalog():
    data = yaml.safe_load(m.CATALOG.read_text())
    catalog = m.choices()
    assert len(catalog) == len(data["zones"]) == 91
    assert len([n for n in catalog if "{" in n]) == 8
    assert (
        data["source"]["url"]
        == "https://learn.microsoft.com/en-us/azure/private-link/private-endpoint-dns"
    )
    assert re.fullmatch("[a-f0-9]{64}", data["source"]["source_sha256"])
    assert not set(data["excluded"]) & catalog.keys()
    assert "scm.privatelink.azurewebsites.net" in data["excluded"]
    assert "{regionName}.data.privatelink.azurecr.io" in data["excluded"]
    assert "privatelink.blob.core.windows.net" in catalog
    assert "privatelink.openai.azure.com" in catalog
    assert all(not n.endswith((".us", ".cn")) for n in catalog)


@pytest.mark.parametrize("selection", list(m.choices()))
def test_every_azure_choice_and_computed_name(selection):
    name = selection
    inputs = {
        "regionName": "australiaeast",
        "region": "australiaeast",
        "regionCode": "aue",
        "dnsPrefix": "sql123",
        "subzone": "cluster",
        "partitionId": "3",
    }
    for key, value in inputs.items():
        name = name.replace("{" + key + "}", value)
    custom = name.upper() if "{" in selection else None
    assert m.selected_name(selection, custom) == name
    assert (
        Template(m.NAME_TEMPLATE).render(
            zone_selection__value=selection, custom_name__value=custom
        )
        == name
    )


@pytest.mark.parametrize(
    "selection,custom",
    [
        ("absent", None),
        (None, None),
        ("custom", None),
        ("custom", ""),
        ("privatelink.blob.core.windows.net", "different.internal"),
        ("privatelink.{regionName}.azmk8s.io", None),
        ("privatelink.{regionName}.azmk8s.io", "privatelink.wrong.example"),
        ("privatelink.{regionName}.azmk8s.io", "privatelink.{regionName}.azmk8s.io"),
        (
            "{subzone}.privatelink.{regionName}.azmk8s.io",
            "privatelink.australiaeast.azmk8s.io",
        ),
    ],
)
def test_invalid_selections(selection, custom):
    with pytest.raises(ValueError):
        m.selected_name(selection, custom)


def test_custom_and_schema_choices():
    assert m.selected_name("custom", "Corp.Example") == "corp.example"
    definition = yaml.safe_load(
        (m.CATALOG.parents[1] / "schemas/local/dns.yml").read_text()
    )["nodes"][0]
    attrs = {a["name"]: a for a in definition["attributes"]}
    choices = attrs["zone_selection"]["choices"]
    assert {c["name"] for c in choices} == {"custom", *m.choices()}
    for field in ("name", "name_key"):
        assert attrs[field]["read_only"] is True
        assert attrs[field]["computed_attribute"]["jinja2_template"] == m.NAME_TEMPLATE


def test_dns_gate_checks_selection():
    zone = dict(
        id="zone",
        name="privatelink.blob.core.windows.net",
        zone_selection="privatelink.blob.core.windows.net",
        custom_name=None,
        resourcegroup="rg",
    )
    # Ignore deliberately omitted context here; exercise the selection gate itself.
    findings = validate({ZONE: [zone]})
    assert len(findings) == 1 and "missing AzureResourceGroup" in findings[0]
    zone["name"] = "other.example"
    assert any("does not match" in f for f in validate({ZONE: [zone]}))
    zone["zone_selection"] = "absent"
    assert any("catalog choice" in f for f in validate({ZONE: [zone]}))
