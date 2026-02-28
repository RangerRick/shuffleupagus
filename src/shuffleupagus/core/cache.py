import os
import random
import threading
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
        self._lock = threading.Lock()
        self._saving = False
        self._update_count = 0
        print(f"* loading '{name}' cache", flush=True)
        self._load()

    def _filename(self):
        return os.path.expanduser(f"~/.cache/shuffleupagus/{self.name}.joblib.gz")

    def _load(self):
        if os.path.exists(self._filename()):
            self._cache = joblib.load(self._filename())

    def _clean_locked(self):
        """Evict expired entries. Caller must hold self._lock."""
        count = 0
        now = time.time()
        for key in list(self._cache.keys()):
            entry = self._cache[key]
            stored_at = entry[1]
            key_ttl = entry[2] if len(entry) > 2 else self.cutoff
            jitter = key_ttl * random.randrange(80, 120) / 100.0
            if now - stored_at > jitter:
                del self._cache[key]
                count += 1
        return count

    def _clean(self):
        with self._lock:
            return self._clean_locked()

    def read(self, key: str):
        """Return the cached value if present and not expired, else None."""
        with self._lock:
            if key in self._cache:
                entry = self._cache[key]
                stored_at = entry[1]
                key_ttl = entry[2] if len(entry) > 2 else self.cutoff
                if time.time() - stored_at <= key_ttl:
                    return entry[0]
        return None

    def read_stale(self, key: str):
        """Return the cached value regardless of TTL, or None if not present."""
        with self._lock:
            if key in self._cache:
                return self._cache[key][0]
        return None

    def touch(self, key: str) -> bool:
        """Reset the timestamp of a cache entry to now. Returns True if found."""
        should_save = False
        with self._lock:
            if key in self._cache:
                self._cache[key][1] = time.time()
                if self.autosave:
                    should_save = self._check_autosave_threshold()
                found = True
            else:
                found = False
        if should_save:
            self.save()
        return found

    def delete(self, key: str) -> bool:
        """Remove a cache entry. Returns True if found."""
        should_save = False
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                if self.autosave:
                    should_save = self._check_autosave_threshold()
                found = True
            else:
                found = False
        if should_save:
            self.save()
        return found

    def write(self, key: str, obj, ttl: float | None = None):
        effective_ttl = ttl if ttl is not None else self.cutoff
        should_save = False
        with self._lock:
            self._cache[key] = [obj, time.time(), effective_ttl]
            if self.autosave:
                should_save = self._check_autosave_threshold()
        if should_save:
            self.save()
        return obj

    def _check_autosave_threshold(self) -> bool:
        """Check and update the autosave counter. Caller must hold self._lock.

        Returns True if save() should be called after releasing the lock.
        """
        self._update_count += 1
        if self._update_count > CACHE_AUTOSAVE_LIMIT:
            self._update_count = 0
            return True
        return False

    def save(self):
        with self._lock:
            if self._saving:
                return
            self._saving = True
            self._clean_locked()
            snapshot = self._cache.copy()

        try:
            os.makedirs(os.path.dirname(self._filename()), exist_ok=True)
            joblib.dump(snapshot, self._filename())
        finally:
            with self._lock:
                self._saving = False
