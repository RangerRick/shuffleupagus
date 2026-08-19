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
from packaging.requirements import Requirement
from packaging.version import Version

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"

# Exact pins, deliberately not compatible-release. Each needs a comment in
# pyproject.toml saying why; this mapping is the test's copy of that decision.
_EXACT_PIN_ALLOWED = frozenset({"pyright"})


def _config() -> dict:
    with _PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)


def _requirements(specs: list[str], where: str) -> list[Requirement]:
    """Parse one dependency table, refusing to return an empty list.

    Emptiness has to be an error rather than a quiet zero. These lists feed
    pytest.mark.parametrize at collection time, so an empty table generates no
    cases at all and the suite still reports green -- the gate would announce
    that it had checked the convention while checking nothing.
    """
    assert specs, f"{where} is empty; the pin-convention gate has nothing to check"
    return [Requirement(spec) for spec in specs]


def _runtime_requirements() -> list[Requirement]:
    return _requirements(_config()["project"]["dependencies"], "[project.dependencies]")


def _dev_requirements() -> list[Requirement]:
    return _requirements(_config()["dependency-groups"]["dev"], "[dependency-groups].dev")


def _build_requirements() -> list[Requirement]:
    return _requirements(_config()["build-system"]["requires"], "[build-system].requires")


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


def test_every_dependency_table_is_populated():
    """The gate must fail loudly when it has nothing to check.

    The tests above are parametrized over these tables, so an empty one produces
    zero cases and a green run. This states the expectation directly instead of
    leaving it to the absence of a failure.
    """
    assert len(_runtime_requirements()) >= 1
    assert len(_dev_requirements()) >= 1
    assert len(_build_requirements()) >= 1


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
def test_pin_states_a_patch_precision_version(req: Requirement):
    """`~=X.Y` would allow minor bumps. The convention is `~=X.Y.Z`.

    Exact pins are held to this too. Being on the exact-pin allowlist excuses the
    operator, not the precision: `pyright==1.1` would float across the patch
    releases the pin exists to hold still, and nothing else here would catch it.
    """
    for spec in req.specifier:
        assert len(Version(spec.version).release) >= 3, (
            f"{req.name} pins {spec.version} without patch precision; use X.Y.Z"
        )


# --- requires-python is not a dependency -------------------------------------


def test_requires_python_floor_has_no_patch_component():
    """A patch-level floor stops Dependabot submitting the dependency graph (#65).

    Without a graph submission, GitHub keeps matching advisories against a stale
    snapshot, so alert counts silently stop tracking uv.lock. This is not a
    dependency pin and must not be swept up by the convention above.
    """
    requires_python = _config()["project"]["requires-python"]
    assert requires_python.count(".") <= 1, (
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
    # Built without filtering, so "absent from the lock" and "present but
    # carrying no version" stay distinguishable in the failure message.
    for pkg in lock["package"]:
        assert "version" in pkg, f"{pkg['name']} is in uv.lock with no version field"
    locked = {pkg["name"]: pkg["version"] for pkg in lock["package"]}

    for req in _runtime_requirements() + _dev_requirements():
        version = locked.get(req.name)
        assert version is not None, f"{req.name} is not in uv.lock"
        assert Version(version) in req.specifier, (
            f"{req.name} is locked at {version}, which its pin {req.specifier} excludes"
        )
