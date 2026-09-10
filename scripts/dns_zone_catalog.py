"""Pinned Azure public-cloud Private Link zone choices (not resource seed data)."""

import re
from functools import cache
from pathlib import Path

import yaml

CATALOG = Path(__file__).resolve().parents[1] / "data/azure_private_dns_zones.yaml"
# Both attributes depend directly on editable inputs, avoiding computed chains.
NAME_TEMPLATE = "{{ ((custom_name__value or '') if zone_selection__value == 'custom' or '{' in (zone_selection__value or '') else (zone_selection__value or '')) | lower }}"


@cache
def choices():
    catalog = yaml.safe_load(CATALOG.read_text())
    return {entry["name"]: entry["services"] for entry in catalog["zones"]}


def selected_name(selection, custom_name):
    """Resolve a choice; require a complete matching name for parameterized zones."""
    if selection not in {"custom", *choices()}:
        raise ValueError("zone_selection must be an Azure catalog choice or Custom")
    if selection == "custom" or "{" in selection:
        if not isinstance(custom_name, str) or not custom_name:
            raise ValueError(
                "custom_name is required for Custom and parameterized zones"
            )
        value = custom_name.lower()
        if selection != "custom":
            pattern = "".join(
                r"[a-z0-9_-]+" if part.startswith("{") else re.escape(part.lower())
                for part in re.split(r"(\{[^}]+\})", selection)
            )
            if not re.fullmatch(pattern, value):
                raise ValueError(
                    f"custom_name must match the selected Azure template {selection}"
                )
        return value
    if custom_name:
        raise ValueError("custom_name must be empty when selecting a fixed Azure zone")
    return selection
