from hypothesis import HealthCheck, settings

# Hypothesis' default 200ms per-example deadline is a wall-clock measurement, and
# several property tests here do real sqlite I/O in a temp directory on every
# example. On a loaded machine or a shared CI runner they exceed it and fail as
# DeadlineExceeded, which says nothing about correctness. A slow example is not a
# wrong example, so the deadline is off and max_examples stays as each test sets it.
settings.register_profile(
    "default",
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("default")
