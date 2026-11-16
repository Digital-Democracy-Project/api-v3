"""OPEN-538: api-v3 takes its bill-version rules from DDP's openstates-core fork
(openstates.utils.version_ordering, pinned in ddp-openstates-core.txt) and keeps no copy."""

import ast
import importlib
import pathlib

import pytest
from openstates.utils import version_ordering as core

from api import bills, search_projection

API_DIR = pathlib.Path(__file__).resolve().parents[1]


def test_routes_use_core_module():
    assert bills.version_sort_key is core.version_sort_key
    assert bills.note_stage is core.note_stage
    assert bills.origin_chamber is core.origin_chamber
    assert search_projection.version_sort_key is core.version_sort_key
    assert search_projection.note_stage is core.note_stage


def test_no_local_copy():
    assert not (API_DIR / "version_ordering.py").exists()
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("api.version_ordering")
    # No module in api/ defines the rules again under another name.
    rule_names = {
        "note_stage",
        "version_sort_key",
        "extract_ordinal",
        "origin_chamber",
        "document_number",
    }
    for path in API_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text())
        defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        assert not defined & rule_names, f"{path} defines {defined & rule_names}"


def test_installed_openstates_is_the_ddp_fork():
    # Rules only DDP's fork has (PyPI's openstates has no utils.version_ordering at all):
    # Alabama's floor-motion papers (OPEN-525) rank as amendment papers, where api-v3's old copy
    # left them unplaced; and Alabama orders versions by date first (OPEN-569).
    assert core.note_stage("Motion to Adopt - Adopted Roll Call 755") == (
        core.STAGE_AMENDMENT,
        0.0,
    )
    assert "Alabama" in core._DATE_ORDERED_JURISDICTIONS
    assert hasattr(core, "is_bill_version")
