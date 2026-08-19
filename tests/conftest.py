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


def pytest_runtest_teardown(item):
    """Attribute a leaked resource to the test that leaked it.

    A ResourceWarning from a finalizer is unraisable, so pytest blames whichever
    test happened to be running when the collector fired — usually not the one
    that leaked. Sprint #44 hit this: an unclosed cache in the YouTube tests
    failed a test under tests/services/spotify/.

    A full collect, not a partial one. CPython promotes by surviving collections
    rather than elapsed time, so an allocation-heavy test can push its own leaked
    objects into the oldest generation before it finishes — measured at roughly
    25,000 allocations, which a Hypothesis property test passes easily. A gen-1
    collect misses exactly those, and the property tests are where sprint #44's
    leak was.

    This is a teardown hook rather than a fixture because a fixture's teardown
    still runs inside the test's own scope, where it collects PRNG objects
    Hypothesis is holding by weakref and trips HypothesisWarning. The hook runs
    after Hypothesis has finished with the item.

    Full collects are affordable because _freeze_startup_heap has already moved
    everything that existed at import time into the permanent generation, so each
    collect walks only what the tests created.
    """
    del item
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
