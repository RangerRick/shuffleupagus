import copy
import os
import random
import time

import joblib

CACHE_DEFAULT_CUTOFF = 60 * 60 * 24 * 7 * 1.0  # yits-been
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
        print(f"  * cleaning expired cache entries out of '{name}'", flush=True)
        count = self._clean()
        print(f"  * {count} cache entries evicted from '{name}'", flush=True)

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
            cutoff = self.cutoff * random.randrange(80, 120) / 100.0
            if now - temp_cache[key][1] > cutoff:
                del self._cache[key]
                count += 1
        return count

    def read(self, key: str):
        if key in self._cache:
            return self._cache[key][0]
        return None

    def write(self, key: str, obj):
        self._cache[key] = [obj, time.time()]

        # logger.debug(f"* writing {self.name} cache entry: '{key}'")
        # logger.debug("  " + json.dumps(obj, sort_keys=True, indent=2))

        if self.autosave:
            if self._update_count > CACHE_AUTOSAVE_LIMIT:
                self.save()
                self._update_count = 0
            self._update_count += 1

        return obj

    def save(self):
        os.makedirs(os.path.dirname(self._filename()), exist_ok=True)
        joblib.dump(self._cache, self._filename())
