# Security & Quality Review — shuffleupagus feat/performance

**Branch:** `feat/performance` → `main`
**Commits reviewed:** `066446e` – `d9a98bc` (4 commits)
**Review date:** 2026-02-28
**Sources:** sharp-edges, differential-review, property-based-testing, variant-analysis
**Coverage at review:** 80% (235 tests passing)

---

## Summary

Four review techniques were applied to the performance branch and the surrounding codebase. A total of **19 issues** were identified and filed as Beads tasks. No exploitable authentication or cryptography flaws were found. The dominant risk themes are:

1. **Silent data loss** — cache saves skipped on exception, exception results silently dropped
2. **Crash-inducing None values** — `None` appended to track lists, propagating to attribute access
3. **Thread safety** — shared mutable `Cache._cache` dict with no lock
4. **Mutable shared defaults** — Python mutable default arguments producing cross-instance state contamination
5. **Missing input validation** — no timeout on network calls, no format checks on resolved IDs, no bounds on config values

---

## Findings by Source

### Sharp Edges (`/sharp-edges`)

*Identifies API and configuration designs where the easy path leads to insecurity.*

| ID | Severity | Title |
|----|----------|-------|
| shuffleupagus-0mq | **P0** | Cache thread safety: no lock on `_cache` dict |
| shuffleupagus-ibu | **P0** | Mutable default `artists=[]` in Track/service constructors |
| shuffleupagus-5zn | P1 | No timeout on YouTube handle HTTP fetch |
| shuffleupagus-1jn | P1 | Extracted YouTube channel ID not validated for format |
| shuffleupagus-l3j | P1 | Path traversal in `Cache._get_filepath` (not using `pathlib.resolve`) |
| shuffleupagus-u70 | P2 | Unsafe chained `.get()` on nested config dicts |
| shuffleupagus-5iw | P2 | Cache key namespace collision possible |
| shuffleupagus-pxr | P2 | `YoutubeService.get_artist` returns placeholder instead of `None` on failure |
| shuffleupagus-bop | P3 | `cache-ttl-days` config value not bounds-checked (accepts 0, negative, float) |
| shuffleupagus-0uo | P3 | Services default to `enabled=True`; missing service block silently runs |

#### Notable findings

**shuffleupagus-0mq — Cache thread safety (P0)**
`Cache._cache` is a plain `dict`. `generate_playlist` now runs per-artist in a `ThreadPoolExecutor(max_workers=4)`, and each artist's processing calls `get_artist`, `get_artist_albums`, and `get_album_tracks`, all of which call `cache.read()` and `cache.write()`. Concurrent dict mutations without a lock produce undefined behavior in CPython (despite the GIL protecting individual bytecode operations, compound read-modify-write sequences are not atomic).

**shuffleupagus-ibu — Mutable default arguments (P0)**
`Track.__init__` declares `artists: list[Artist] = []`. Python evaluates default argument expressions once at function definition time, so all `Track` instances that don't pass `artists` share the same list object. Any mutation (e.g., `track.artists.append(...)`) affects every other instance created without an explicit `artists` argument.

```python
# Current (dangerous)
def __init__(self, ..., artists: list[Artist] = []):
    self.artists = artists  # shared reference if not passed

# Fixed
def __init__(self, ..., artists: list[Artist] | None = None):
    self.artists = artists if artists is not None else []
```

The same pattern appears in `Service.generate_playlist`, `Service.sync`, and at least one service model.

**shuffleupagus-l3j — Path traversal (P1)**
`Cache._get_filepath` constructs a path from the cache name without calling `Path.resolve()`. A cache name like `../../etc/passwd` would write outside the intended cache directory. While cache names are currently hardcoded strings, this is a latent footgun if cache names ever come from config or user input.

---

### Differential Review

*Security-focused review of changes introduced by `feat/performance`.*

| ID | Severity | Title |
|----|----------|-------|
| shuffleupagus-qf4 | P1 | `_run_service` has no `try/finally`; `cache.save()` skipped on exception |
| shuffleupagus-0zx | P2 | Only first service exception surfaced; rest silently dropped |
| shuffleupagus-2iv | P3 | Spotify fingerprint assumes newest-first album ordering |
| shuffleupagus-8tr | P3 | `Cache.delete()` omits `_maybe_autosave()` unlike `write()`/`touch()` |

#### Notable findings

**shuffleupagus-qf4 — Cache lost on exception (P1)**
`_run_service` was extracted in this branch to support parallelism:

```python
def _run_service(plugin, config, args) -> None:
    service = plugin.create(config)
    service.login()
    playlist_track_ids = service.generate_playlist(...)  # can raise
    ...
    service.sync(...)                                     # can raise
    service.close()  # <-- never reached if above raises
```

`service.close()` calls `cache.save()`, which is the only path to persisting cache writes to disk. If `generate_playlist()` or `sync()` raises an exception, the entire session's worth of API responses are lost. On the next run, all API calls must be repeated in full.

Fix: wrap the body in `try/finally`:
```python
try:
    service.login()
    ...
    service.sync(...)
finally:
    service.close()
```

**shuffleupagus-0zx — Silently dropped exceptions (P2)**
```python
with ThreadPoolExecutor(max_workers=len(active_plugins) or 1) as executor:
    futures = {...}
    for future in as_completed(futures):
        future.result()  # re-raises first exception only
```

If Spotify fails and Apple Music also fails, only the first exception propagates. The second is silently discarded once the executor shuts down. The user sees one error and may incorrectly conclude the other service succeeded.

**shuffleupagus-2iv — Fingerprint ordering assumption (P3)**
`ret[0]["id"]` assumes Spotify's `artist_albums()` returns results sorted newest-first. If the ordering ever changes, the fingerprint will point to the wrong album, causing new releases to be missed indefinitely with no log indication.

---

### Variant Analysis

*Systematic search for all instances of known vulnerability patterns across the codebase.*

Five vulnerability classes were searched; findings across all files:

| ID | Severity | Pattern | Count |
|----|----------|---------|-------|
| shuffleupagus-5zn | P1 | Missing timeout on `requests.get` | 1 (youtube/service.py:135) |
| shuffleupagus-ibu | P0 | Mutable default `= []` | 11 instances (model.py, all service models) |
| shuffleupagus-u70 | P2 | Unsafe chained `.get()` | 6 instances (config.py + appleMusic/service.py:219) |
| shuffleupagus-o58 | P1 | `None` appended to track list | 1 (appleMusic/service.py ~194) |
| shuffleupagus-3b9 | P1 | Broad `except Exception: return None/[]` | 7 (appleMusic/service.py) |
| shuffleupagus-6cb | P2 | Bare `config["key"]` indexing | 8 (all three services' login methods) |

#### Notable findings

**shuffleupagus-o58 — None crash in Apple Music tracks (P1)**
In `AppleMusic.get_artist_top_tracks()`, the return value of `_get_track_by_id()` is appended to the tracks list without a None guard. `_get_track_by_id()` swallows exceptions and can return `None`. The `None` then propagates to `generate_playlist()`, which calls `t.is_excluded()` and `t.longer_than()` unconditionally, causing `AttributeError: 'NoneType' object has no attribute 'is_excluded'`.

**shuffleupagus-3b9 — Broad exception swallowing in Apple Music (P1)**
Seven methods in `appleMusic/service.py` use `except Exception: return None` or `return []`. This pattern hides authentication failures, network errors, and bugs as empty results. The playlist generation logic silently produces fewer tracks with no log message. Specific exceptions from the MusicKit API or requests library should be caught by type; unknown exceptions should propagate.

**shuffleupagus-6cb — Bare config key access (P2)**
All three services access required config keys with bare indexing:
```python
# Spotify login()
creds = SpotifyOAuth(
    client_id=self.config["client-id"],   # KeyError if missing
    client_secret=self.config["client-secret"],
```
A typo or missing key in the YAML produces a `KeyError: 'client-id'` with no context about which service or how to fix it. This makes configuration errors unnecessarily hard to diagnose.

---

### Property-Based Testing (Hypothesis)

*Generated test inputs to verify invariants hold across all valid inputs.*

Three test files were created covering `Cache`, `Track.dedupe_hash`, `Album`, and `spread_artist_playlists`. No invariant violations were found in production code; one test authoring issue was identified:

**Monkeypatch incompatibility with Hypothesis (test bug, fixed)**
The `spread_artist_playlists` tests initially used pytest's `monkeypatch` fixture with `@given()`. Hypothesis does not reset function-scoped fixtures between generated inputs, causing a `FailedHealthCheck`. Fixed by replacing `monkeypatch` with `unittest.mock.patch` context managers inside `_call_spread()`.

**Bugs discovered during property design:**

- **shuffleupagus-oxu — `Album` crashes on 1–3 digit year strings (P3)**: When a year-only string (no `-` separator) is passed as `release_date`, the code pads it to `"<year>-01-01"` and calls `datetime.date.fromisoformat()`, which requires exactly 4 digits. A value like `"999"` raises `ValueError`. Latent in practice since Spotify/Apple Music return 4-digit years, but unvalidated at the boundary.

- **shuffleupagus-y2q — `Album` falsy type check bypasses `ValueError` (P3)**: `if release_date:` is falsy for `0`, `[]`, `{}`, etc., so `Album("id", "Name", 0)` silently sets `release_date = None` instead of raising `ValueError`. Fix: `if release_date is not None:`.

**Coverage confirmed:** `Track.dedupe_hash` is deterministic, case-insensitive, and punctuation-stripping. `spread_artist_playlists` produces no duplicate IDs and always includes VIP tracks. All `sanitize_id` implementations are idempotent.

---

## Issue Register (All 17 Issues)

| ID | Priority | Source | Title |
|----|----------|--------|-------|
| shuffleupagus-0mq | **P0** | sharp-edges | Fix Cache thread safety: add RLock to `_cache` dict |
| shuffleupagus-ibu | **P0** | sharp-edges / variant | Fix mutable default `artists=[]` in Track constructors |
| shuffleupagus-qf4 | P1 | differential | Add `try/finally` to `_run_service` to ensure `cache.save()` on exception |
| shuffleupagus-5zn | P1 | sharp-edges / variant | Add timeout to YouTube handle HTTP fetch |
| shuffleupagus-1jn | P1 | sharp-edges | Validate extracted YouTube channel ID format after HTML parsing |
| shuffleupagus-l3j | P1 | sharp-edges | Fix path traversal in `Cache._get_filepath` to use `pathlib.resolve()` |
| shuffleupagus-o58 | P1 | variant | Fix `None` appended to tracks list in AppleMusic `get_artist_top_tracks` |
| shuffleupagus-3b9 | P1 | variant | Replace broad exception swallowing in AppleMusic service |
| shuffleupagus-0zx | P2 | differential | Surface all service exceptions in parallel execution |
| shuffleupagus-u70 | P2 | sharp-edges / variant | Fix chained `.get()` on config dicts |
| shuffleupagus-5iw | P2 | sharp-edges | Add cache key namespace separator to prevent collision |
| shuffleupagus-pxr | P2 | sharp-edges | Return `None` (not placeholder) from `YoutubeService.get_artist` on failure |
| shuffleupagus-6cb | P2 | variant | Replace bare `config["key"]` with descriptive error messages |
| shuffleupagus-bop | P3 | sharp-edges | Validate `cache-ttl-days` config value has reasonable bounds |
| shuffleupagus-2iv | P3 | differential | Document/assert Spotify `artist_albums` newest-first ordering assumption |
| shuffleupagus-0uo | P3 | sharp-edges | Change default service `enabled` state to `False` |
| shuffleupagus-8tr | P3 | differential | `Cache.delete()` should call `_maybe_autosave()` |
| shuffleupagus-oxu | P3 | property-testing | `Album` crashes on 1–3 digit year strings |
| shuffleupagus-y2q | P3 | property-testing | `Album` falsy type check bypasses `ValueError` |

---

## Coverage

| Test File | Purpose |
|-----------|---------|
| `tests/core/test_cache_properties.py` | write/read roundtrip, TTL expiry, stale reads, touch, delete, key isolation |
| `tests/core/test_model_properties.py` | `Track.dedupe_hash` determinism, case insensitivity, bucketing; `Album` date parsing |
| `tests/core/test_util_properties.py` | `spread_artist_playlists` output validity, no duplicates, VIP inclusion |
| `tests/services/spotify/test_model_properties.py` | `sanitize_id` idempotence, URL/prefix stripping |
| `tests/services/appleMusic/test_model_properties.py` | `sanitize_id` idempotence, URL/prefix stripping |
| `tests/services/youtube/test_model_properties.py` | `sanitize_id` idempotence, handle/channel-ID extraction format |

Overall coverage at end of review: **80%** (235 tests).
`shuffleupagus.py` (the CLI entry point) has 0% coverage — integration/smoke tests are a future gap.

---

## Recommended Fix Order

1. **shuffleupagus-ibu** — Mutable defaults: low risk, easy fix, affects every service
2. **shuffleupagus-qf4** — try/finally in `_run_service`: 5-line fix, high impact on cache reliability
3. **shuffleupagus-o58** — None guard in Apple Music: one-line fix, prevents crash
4. **shuffleupagus-3b9** — Apple Music exception handling: reduces silent failures
5. **shuffleupagus-0mq** — Cache thread safety: add `RLock`; most complex due to lock scope
6. Remaining P2–P3 issues at discretion
