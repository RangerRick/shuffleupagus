import copy
import os
import random
import time

import joblib

CACHE_DEFAULT_CUTOFF = 60 * 60 * 24 * 7 * 1.0  # 1 week
CACHE_AUTOSAVE_LIMIT = 50


class Cache:
    name: str
    cutoff: float
    autosave: bool

    def __init__(self, name: str, cutoff: float = CACHE_DEFAULT_CUTOFF, autosave: bool = True):
        self.name = name
        self.cutoff = cutoff
        self.autosave = autosave
        self._cache: dict = {}
        self._update_count = 0
        print(f"* loading '{name}' cache", flush=True)
        self._load()

    def _filename(self):
        return os.path.expanduser(f"~/.cache/shuffleupagus/{self.name}.joblib.gz")

    def _load(self):
        if os.path.exists(self._filename()):
            self._cache = joblib.load(self._filename())

    def _clean(self):
        count = 0
        now = time.time()
        temp_cache = copy.deepcopy(self._cache)
        for key in temp_cache:
            entry = temp_cache[key]
            stored_at = entry[1]
            key_ttl = entry[2] if len(entry) > 2 else self.cutoff
            jitter = key_ttl * random.randrange(80, 120) / 100.0
            if now - stored_at > jitter:
                del self._cache[key]
                count += 1
        return count

    def read(self, key: str):
        """Return the cached value if present and not expired, else None."""
        if key in self._cache:
            entry = self._cache[key]
            stored_at = entry[1]
            key_ttl = entry[2] if len(entry) > 2 else self.cutoff
            if time.time() - stored_at <= key_ttl:
                return entry[0]
        return None

    def read_stale(self, key: str):
        """Return the cached value regardless of TTL, or None if not present."""
        if key in self._cache:
            return self._cache[key][0]
        return None

    def touch(self, key: str) -> bool:
        """Reset the timestamp of a cache entry to now. Returns True if found."""
        if key in self._cache:
            self._cache[key][1] = time.time()
            if self.autosave:
                self._maybe_autosave()
            return True
        return False

    def delete(self, key: str) -> bool:
        """Remove a cache entry. Returns True if found."""
        if key in self._cache:
            del self._cache[key]
            return True
        return False

    def write(self, key: str, obj, ttl: float | None = None):
        effective_ttl = ttl if ttl is not None else self.cutoff
        self._cache[key] = [obj, time.time(), effective_ttl]

        if self.autosave:
            self._maybe_autosave()

        return obj

    def _maybe_autosave(self):
        if self._update_count > CACHE_AUTOSAVE_LIMIT:
            self.save()
            self._update_count = 0
        self._update_count += 1

    def save(self):
        self._clean()
        os.makedirs(os.path.dirname(self._filename()), exist_ok=True)
        joblib.dump(self._cache, self._filename())
