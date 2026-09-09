"""Ensure vendored schemas remain byte-for-byte copies of pinned upstream files."""

import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FILES = json.loads((ROOT / "third_party/schema-library/manifest.json").read_text())[
    "files"
]


def test_schema_file_inventory_matches_manifest():
    actual = {
        str(path.relative_to(ROOT))
        for path in (ROOT / "schemas").rglob("*")
        if path.suffix in {".yml", ".yaml"}
        and not path.is_relative_to(ROOT / "schemas/local")
    }
    assert actual == set(FILES)


@pytest.mark.parametrize("name,source", FILES.items(), ids=FILES.keys())
def test_vendored_file_matches_manifest(name, source):
    assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == source["sha256"]
