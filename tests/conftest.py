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
    """
    yield
    gc.collect()
