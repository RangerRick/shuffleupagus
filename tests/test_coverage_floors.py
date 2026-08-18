"""Tests for scripts/check_per_module_coverage.py.

The script is a gate. A gate that passes when it could not evaluate its own
input is worse than no gate, so most of these assert on the failure paths.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_per_module_coverage.py"
_spec = importlib.util.spec_from_file_location("check_per_module_coverage", _SCRIPT)
assert _spec is not None
assert _spec.loader is not None
cov = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = cov
_spec.loader.exec_module(cov)


def _report(tmp_path: Path, files: dict) -> Path:
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps({"files": files, "totals": {}}))
    return path


def _entry(percent):
    return {"summary": {"percent_covered": percent}}


def _pyproject(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(body)
    return path


# --- find_violations ---------------------------------------------------------


def test_module_at_floor_passes():
    assert cov.find_violations({"a.py": 80.0}, 80.0, {}) == []


def test_module_below_floor_fails():
    problems = cov.find_violations({"a.py": 79.0}, 80.0, {})
    assert len(problems) == 1
    assert "a.py" in problems[0]


def test_fraction_below_floor_fails():
    """79.95 must not round up past an 80 floor. This was a real bug in the bash version."""
    assert cov.find_violations({"a.py": 79.95}, 80.0, {}) != []


def test_per_module_override_beats_default():
    assert cov.find_violations({"a.py": 20.0}, 80.0, {"a.py": 15.0}) == []


def test_override_of_zero_is_honoured():
    """0 is a real floor, not an absent one."""
    assert cov.find_violations({"a.py": 0.0}, 80.0, {"a.py": 0.0}) == []


def test_floor_for_unmeasured_module_is_a_failure():
    """Re-adding a path to coverage's omit would otherwise silently exempt it."""
    problems = cov.find_violations({"a.py": 90.0}, 80.0, {"gone.py": 10.0})
    assert any("NOT MEASURED" in p for p in problems)


def test_paths_with_awkward_characters_match_their_override():
    """The bash version looked overrides up through awk, which ate backslashes."""
    weird = "src/a b\tc\\d.py"
    assert cov.find_violations({weird: 5.0}, 80.0, {weird: 5.0}) == []


@given(
    percent=st.floats(min_value=0, max_value=100),
    floor=st.floats(min_value=0, max_value=100),
)
def test_violation_iff_below_floor(percent, floor):
    """The only rule: a module is reported exactly when it is under its floor."""
    problems = cov.find_violations({"a.py": percent}, floor, {})
    assert bool(problems) == (percent < floor)


@given(
    floor=st.floats(min_value=1, max_value=100),
    drop=st.floats(min_value=0.01, max_value=1.0),
)
def test_lowering_coverage_never_turns_a_failure_into_a_pass(floor, drop):
    """Monotonicity: less coverage is never more acceptable."""
    failing = cov.find_violations({"a.py": floor - drop}, floor, {})
    assert failing != []


# --- load_coverage: every unreadable input is an error, never a pass ---------


def test_missing_report_is_an_error(tmp_path):
    with pytest.raises(cov.CoverageError, match="cannot read"):
        cov.load_coverage(tmp_path / "nope.json")


def test_corrupt_report_is_an_error(tmp_path):
    path = tmp_path / "coverage.json"
    path.write_text("not json at all")
    with pytest.raises(cov.CoverageError, match="not valid JSON"):
        cov.load_coverage(path)


def test_report_with_no_files_key_is_an_error(tmp_path):
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps({"totals": {}}))
    with pytest.raises(cov.CoverageError, match="measured nothing"):
        cov.load_coverage(path)


def test_report_with_empty_files_is_an_error(tmp_path):
    """An empty report is what you get when --cov stops matching. It is not a pass."""
    with pytest.raises(cov.CoverageError, match="zero measured files"):
        cov.load_coverage(_report(tmp_path, {}))


def test_non_numeric_coverage_is_an_error(tmp_path):
    """jq's strnum typing made the bash version compare 'null' lexically and pass."""
    with pytest.raises(cov.CoverageError, match="non-numeric"):
        cov.load_coverage(_report(tmp_path, {"a.py": _entry(None)}))


# --- load_floors -------------------------------------------------------------


def test_floors_default_when_table_absent(tmp_path):
    default, floors = cov.load_floors(_pyproject(tmp_path, "[project]\nname = 'x'\n"))
    assert default == cov.DEFAULT_FLOOR
    assert floors == {}


def test_floors_parsed_from_table(tmp_path):
    body = '[tool.coverage-floors]\ndefault = 70\n"src/a.py" = 15\n'
    default, floors = cov.load_floors(_pyproject(tmp_path, body))
    assert default == 70
    assert floors == {"src/a.py": 15.0}


def test_non_numeric_floor_is_an_error(tmp_path):
    body = '[tool.coverage-floors]\n"src/a.py" = "high"\n'
    with pytest.raises(cov.CoverageError, match="not a number"):
        cov.load_floors(_pyproject(tmp_path, body))


def test_malformed_pyproject_is_an_error(tmp_path):
    with pytest.raises(cov.CoverageError, match="cannot read floors"):
        cov.load_floors(_pyproject(tmp_path, "[unclosed\n"))


# --- exit codes --------------------------------------------------------------


def test_main_returns_0_when_all_modules_pass(tmp_path):
    report = _report(tmp_path, {"a.py": _entry(95.0)})
    pyproject = _pyproject(tmp_path, "[tool.coverage-floors]\ndefault = 80\n")
    assert cov.main([str(report), "--pyproject", str(pyproject)]) == 0


def test_main_returns_1_on_violation(tmp_path):
    report = _report(tmp_path, {"a.py": _entry(10.0)})
    pyproject = _pyproject(tmp_path, "[tool.coverage-floors]\ndefault = 80\n")
    assert cov.main([str(report), "--pyproject", str(pyproject)]) == 1


def test_main_returns_2_when_it_cannot_evaluate(tmp_path):
    """Distinct from 1: this is 'I could not check', not 'I checked and it failed'."""
    pyproject = _pyproject(tmp_path, "[tool.coverage-floors]\ndefault = 80\n")
    assert cov.main([str(tmp_path / "missing.json"), "--pyproject", str(pyproject)]) == 2
