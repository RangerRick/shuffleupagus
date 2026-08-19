"""Tests that pyproject.toml's dependency pins hold the project convention.

The convention is PEP 440 compatible-release (`~=X.Y.Z`): patch bumps allowed,
minor and major blocked. It exists because an open-ended `>=` let a `ruff` minor
release walk in through Renovate's lockFileMaintenance and turn on new lint
rules, which broke main (#38). `uv.lock` is committed, so a plain checkout stays
reproducible either way -- the exposure is on lockfile refresh, not on install.

These are regression guards, not style checks. Each one has already been the
cause of a real breakage, and none of them are visible in a passing test run
otherwise. See #41.
"""

import tomllib
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from packaging.requirements import Requirement
from packaging.version import Version

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"

# Exact pins, deliberately not compatible-release. Each needs a comment in
# pyproject.toml saying why; this mapping is the test's copy of that decision.
_EXACT_PIN_ALLOWED = frozenset({"pyright"})


def _config() -> dict:
    with _PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)


def _runtime_requirements() -> list[Requirement]:
    return [Requirement(spec) for spec in _config()["project"]["dependencies"]]


def _dev_requirements() -> list[Requirement]:
    return [Requirement(spec) for spec in _config()["dependency-groups"]["dev"]]


def _build_requirements() -> list[Requirement]:
    return [Requirement(spec) for spec in _config()["build-system"]["requires"]]


def _all_requirements() -> list[Requirement]:
    return _runtime_requirements() + _dev_requirements() + _build_requirements()


def _ids(requirements: list[Requirement]) -> list[str]:
    return [req.name for req in requirements]


# --- every specifier is bounded above ----------------------------------------


@pytest.mark.parametrize("req", _all_requirements(), ids=_ids(_all_requirements()))
def test_specifier_is_bounded_above(req: Requirement):
    """An unbounded specifier accepts a future breaking release sight unseen.

    This is the invariant that actually matters. It is what `>=` violated in
    [project.dependencies], and what a bare "uv_build" violated in
    [build-system] -- where it is worse, because a breaking uv_build release
    breaks the build itself rather than a runtime import.
    """
    assert req.specifier, f"{req.name} declares no version constraint at all"
    absurd = Version("9999.0.0")
    assert absurd not in req.specifier, f"{req.name} is open-ended above: {req.specifier}"


@given(major=st.integers(min_value=1000, max_value=10**6))
def test_no_specifier_admits_an_arbitrarily_distant_major(major: int):
    """Bounded above must mean bounded for every future major, not just one probe."""
    future = Version(f"{major}.0.0")
    for req in _all_requirements():
        assert future not in req.specifier, f"{req.name} admits {future}: {req.specifier}"


# --- the convention itself ---------------------------------------------------


@pytest.mark.parametrize(
    "req",
    _runtime_requirements() + _dev_requirements(),
    ids=_ids(_runtime_requirements() + _dev_requirements()),
)
def test_dependency_uses_compatible_release(req: Requirement):
    """Both groups get the convention, not just the tooling that can break a build."""
    if req.name in _EXACT_PIN_ALLOWED:
        operators = {spec.operator for spec in req.specifier}
        assert operators == {"=="}, f"{req.name} is an allowed exact pin but uses {operators}"
        return
    operators = {spec.operator for spec in req.specifier}
    assert operators == {"~="}, f"{req.name} should use ~=X.Y.Z, has {req.specifier}"


@pytest.mark.parametrize(
    "req",
    _runtime_requirements() + _dev_requirements(),
    ids=_ids(_runtime_requirements() + _dev_requirements()),
)
def test_compatible_release_pins_to_patch_precision(req: Requirement):
    """`~=X.Y` would allow minor bumps. The convention is `~=X.Y.Z`."""
    if req.name in _EXACT_PIN_ALLOWED:
        return
    for spec in req.specifier:
        assert len(Version(spec.version).release) >= 3, (
            f"{req.name} pins {spec.version}, which allows minor bumps; use X.Y.Z"
        )


def test_compatible_release_still_admits_patch_upgrades():
    """The pins must not be so tight that Renovate can never move them.

    `~=X.Y.Z` is `>=X.Y.Z, ==X.Y.*`. A pin that admitted nothing above itself
    would be an exact pin wearing a range's clothes, and would silently freeze
    the dependency.
    """
    for req in _runtime_requirements() + _dev_requirements():
        if req.name in _EXACT_PIN_ALLOWED:
            continue
        pinned = Version(next(iter(req.specifier)).version)
        release = pinned.release
        next_patch = Version(f"{release[0]}.{release[1]}.{release[2] + 1}")
        assert next_patch in req.specifier, f"{req.name} cannot reach {next_patch}"


# --- requires-python is not a dependency -------------------------------------


def test_requires_python_floor_has_no_patch_component():
    """A patch-level floor stops Dependabot submitting the dependency graph (#65).

    Without a graph submission, GitHub keeps matching advisories against a stale
    snapshot, so alert counts silently stop tracking uv.lock. This is not a
    dependency pin and must not be swept up by the convention above.
    """
    requires_python = _config()["project"]["requires-python"]
    for spec in Requirement(f"python{requires_python}").specifier:
        assert len(Version(spec.version).release) <= 2, (
            f"requires-python is {requires_python}; a patch floor breaks the dependency graph (#65)"
        )


# --- the pins agree with the lockfile ----------------------------------------


def test_every_pin_admits_its_locked_version():
    """A constraint that excludes what uv.lock resolved is a contradiction.

    It surfaces as a resolution failure on the next `uv sync`, which is a
    confusing place to discover a typo in a pin.
    """
    lock_path = _PYPROJECT.parent / "uv.lock"
    with lock_path.open("rb") as handle:
        lock = tomllib.load(handle)
    locked = {pkg["name"]: pkg["version"] for pkg in lock["package"] if "version" in pkg}

    for req in _runtime_requirements() + _dev_requirements():
        version = locked.get(req.name)
        assert version is not None, f"{req.name} is not in uv.lock"
        assert Version(version) in req.specifier, (
            f"{req.name} is locked at {version}, which its pin {req.specifier} excludes"
        )
