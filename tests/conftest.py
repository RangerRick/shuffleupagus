import gc

import pytest
from hypothesis import settings

# Hypothesis' default 200ms per-example deadline is a wall-clock measurement, and
# several property tests here do real sqlite I/O in a temp directory on every
# example. On a loaded machine or a shared CI runner they exceed it and fail as
# DeadlineExceeded, which says nothing about correctness. A slow example is not a
# wrong example, so the deadline is off and max_examples stays as each test sets it.
#
# HealthCheck.too_slow is deliberately NOT suppressed. It measures how long
# Hypothesis takes to *generate* data, not how long the test body runs, so it is
# a different signal: it catches a strategy that has degenerated, such as a
# filter that starts rejecting almost every draw. That is a real defect and
# should still fail.
settings.register_profile("default", deadline=None)
settings.load_profile("default")


@pytest.fixture(autouse=True)
def _collect_garbage_after_each_test():
    """Attribute a leaked resource to the test that leaked it.

    A ResourceWarning from a finalizer is unraisable, so pytest blames whichever
    test happened to be running when the collector fired — usually not the one
    that leaked. Sprint #44 hit this: an unclosed cache in the YouTube tests
    failed a test under tests/services/spotify/.

    Collecting at the end of every test makes the attribution deterministic.
    Measured on this suite: a leak in an early test is reported against that
    test, and without this fixture it is reported against a later, unrelated one.

    A full collect, not a partial one. CPython promotes by surviving collections
    rather than by elapsed time, so an allocation-heavy test can push its own
    leaked objects into the oldest generation before it finishes — measured at
    roughly 25,000 allocations, which a Hypothesis property test passes easily.
    A gen-1 collect would miss exactly those, and the property tests are where
    sprint #44's leak actually was.

    Full collects are affordable here because _freeze_startup_heap has already
    moved everything that existed at import time into the permanent generation,
    so each collect walks only what the tests themselves created.
    """
    yield
    gc.collect()


@pytest.fixture(scope="session", autouse=True)
def _freeze_startup_heap():
    """Exclude the import-time heap from every per-test collection.

    Without this, the per-test full collect walks ~10,000 interpreter and import
    objects that can never be a test's leak, once per test. gc.freeze() moves
    them into the permanent generation, which is not collected.
    """
    gc.collect()
    gc.freeze()
    yield
    gc.unfreeze()
