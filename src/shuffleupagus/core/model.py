import datetime
import random
import string
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed

from .cache import CACHE_DEFAULT_CUTOFF, Cache
from .config import Config
from .util import format_retry_message, logger, service_tag, spread_artist_playlists

MAX_TOP_TRACKS = 5
MAX_ARTIST_TRACKS = 10
MAX_TRACK_LENGTH_MS = 8 * 60 * 1000  # 8 minutes


class ShufObject:
    id: str
    name: str

    def __init__(self, id: str, name: str):
        self.id = id
        self.name = name

    def matches(self, val) -> bool:
        return val == self.id

    def is_excluded(self, ids: list[str]) -> bool:
        return any(self.matches(id) for id in ids)

    @staticmethod
    def sanitize_id(id: str) -> str:
        return id

    @staticmethod
    def from_dict(obj: any):  # type: ignore
        raise NotImplementedError


class Artist(ShufObject):
    def __str__(self) -> str:
        return f"Artist({self.id}): {self.name}"

    def __repr__(self):
        return f"artist[id={self.id}, name={self.name}]"


class Album(ShufObject):
    release_date: datetime.date | None = None

    def __init__(self, id: str, name: str, release_date=None):
        super().__init__(id, name)

        if release_date is not None:
            if isinstance(release_date, str):
                if "-" not in release_date:
                    if not (release_date.isdigit() and len(release_date) == 4):
                        raise ValueError(f"Invalid year string: {release_date!r}")
                    release_date = release_date + "-01-01"
                self.release_date = datetime.date.fromisoformat(release_date)
            elif isinstance(release_date, datetime.date):
                self.release_date = release_date
            else:
                raise ValueError(f"Invalid release_date type: {type(release_date).__name__}")

    def __str__(self) -> str:
        return f"Album({self.id}): {self.name}"

    def __repr__(self):
        return f"album[id={self.id}, name={self.name}]"


class Track(ShufObject):
    duration_ms = 0
    isrc: str | None = None
    album: Album | None = None
    artists: list[Artist]
    dedupe_hash = None

    def __init__(
        self,
        id: str,
        name: str,
        duration_ms: int,
        isrc: str | None = None,
        album: Album | None = None,
        artists: list[Artist] | None = None,
    ):
        super().__init__(id, name)
        self.duration_ms = duration_ms
        self.isrc = isrc
        self.album = album
        self.artists = list(artists) if artists else []

        # create a dedupe hash based on name and duration rounded to nearest second
        cleaned_name = unicodedata.normalize("NFKD", name).casefold().strip()
        cleaned_name = "".join(
            c for c in cleaned_name if unicodedata.category(c) != "Mn" and c not in string.punctuation
        )
        rounded_ms = str(int(duration_ms - (duration_ms % 2000)))
        self.dedupe_hash = f"{cleaned_name}:{rounded_ms}"

    def __str__(self) -> str:
        return f"Track({self.id}): {self.name}"

    def __repr__(self):
        return f"track[id={self.id}, name={self.name}]"

    def longer_than(self, duration_ms: int) -> bool:
        return self.duration_ms > duration_ms


_ARTIST_POOL_WORKERS = 4
_ALBUM_POOL_WORKERS = 8


class Service:
    name: str
    cache: Cache
    cache_cutoff: float = CACHE_DEFAULT_CUTOFF
    _artist_pool: ThreadPoolExecutor | None = None
    _album_pool: ThreadPoolExecutor | None = None

    @property
    def artist_pool(self) -> ThreadPoolExecutor:
        """Per-service artist pool, lazily created."""
        if self._artist_pool is None:
            self._artist_pool = ThreadPoolExecutor(max_workers=_ARTIST_POOL_WORKERS)
        return self._artist_pool

    @property
    def album_pool(self) -> ThreadPoolExecutor:
        """Per-service album pool, lazily created."""
        if self._album_pool is None:
            self._album_pool = ThreadPoolExecutor(max_workers=_ALBUM_POOL_WORKERS)
        return self._album_pool

    def __init__(self, config: Config):
        svc_config = config.service(self.name)
        ttl_days = svc_config.get("cache-ttl-days")
        if ttl_days is not None:
            ttl_days = float(ttl_days)
            if ttl_days <= 0:
                raise ValueError(f"cache-ttl-days must be positive, got {ttl_days}")
            cutoff = ttl_days * 24 * 60 * 60
        else:
            cutoff = self.cache_cutoff
        self.cache = Cache(self.name, cutoff=cutoff)
        self.config = svc_config
        self.tag = service_tag(self.name)

    def sanitize_id(self, id: str) -> str:
        return id

    def preflight(self) -> None:
        """Pre-check run sequentially before threaded processing starts.

        Override to validate credentials, prompt for re-auth, etc.
        Raising here aborts the entire run before any service threads start.
        """

    def login(self) -> None:
        raise NotImplementedError

    def _shutdown_pools(self, wait: bool) -> None:
        """Drop queued work in both pools. Tasks already running are not interrupted."""
        for pool in (self._artist_pool, self._album_pool):
            if pool is not None:
                pool.shutdown(wait=wait, cancel_futures=True)

    def close(self) -> None:
        """Shut down the worker pools, evict expired entries, release the connection.

        Pools come down first, and with wait=True: a worker still running would
        otherwise reach for a cache connection that is already closed. This also
        stops the pools' non-daemon threads from holding up process exit.
        """
        self._shutdown_pools(wait=True)
        self.cache.save()
        self.cache.close()

    _RATE_LIMIT_CACHE_KEY = "rate_limit_until"

    def _record_rate_limit(self, service_label: str, retry_after: int) -> str:
        """Persist a rate-limit window so the next run fails fast. Returns the message."""
        retry_epoch = time.time() + retry_after
        self.cache.write(self._RATE_LIMIT_CACHE_KEY, retry_epoch, ttl=retry_after)
        return format_retry_message(service_label, retry_epoch, retry_after)

    def _check_rate_limit(self, service_label: str) -> None:
        """Fail fast if an earlier run recorded a rate-limit window still in effect."""
        retry_epoch = self.cache.read_stale(self._RATE_LIMIT_CACHE_KEY)
        if not retry_epoch:
            return
        remaining = retry_epoch - time.time()
        if remaining <= 0:
            self.cache.delete(self._RATE_LIMIT_CACHE_KEY)
            return
        raise RuntimeError(format_retry_message(service_label, retry_epoch, remaining))

    def get_artist(self, artist: str | Artist) -> Artist | None:
        raise NotImplementedError

    def get_album_by_id(self, album_id: str) -> Album | None:
        raise NotImplementedError

    def get_artist_albums(self, artist: Artist) -> list[Album]:
        raise NotImplementedError

    def get_album_tracks(self, album: Album) -> list[Track]:
        raise NotImplementedError

    def get_artist_tracks(self, artist: Artist) -> list[Track]:
        raise NotImplementedError

    def get_artist_top_tracks(self, artist: Artist) -> list[Track]:
        raise NotImplementedError

    def get_playlist_id_for_name(self, playlist_name: str) -> str:
        raise NotImplementedError

    def collect_tracks(
        self,
        artist_ids: list[str] | None = None,
        excluded_album_ids: list[str] | None = None,
        excluded_track_ids: list[str] | None = None,
    ) -> dict[str, list[Track]]:
        """Fetch per-artist track lists (threaded, IO-heavy)."""
        _artist_ids: list[str] = [] if artist_ids is None else artist_ids
        _excluded_album_ids: list[str] = [] if excluded_album_ids is None else excluded_album_ids
        _excluded_track_ids: list[str] = [] if excluded_track_ids is None else excluded_track_ids
        artist_playlists: dict[str, list[Track]] = {}
        total = len(_artist_ids)
        logger.info(f"{self.tag}* collecting tracks for {total} artists")

        def _process(idx: int, artist_id: str) -> tuple[str, list[Track]]:
            tag = self.tag
            logger.info(f"{tag}* [{idx + 1}/{total}] fetching {artist_id}")
            artist = self.get_artist(artist_id)
            if artist is None:
                logger.warning(f"{tag}  ! artist {artist_id} not found, skipping")
                return artist_id, []
            logger.info(f"{tag}* [{idx + 1}/{total}] processing {artist.name}")

            top_tracks = self.get_artist_top_tracks(artist)
            top_tracks = [
                t
                for t in top_tracks
                if (
                    not t.is_excluded(_excluded_track_ids)
                    and not t.longer_than(MAX_TRACK_LENGTH_MS)
                    and t.album is not None
                    and not t.album.is_excluded(_excluded_album_ids)
                )
            ]
            top_track_ids = {t.id for t in top_tracks}
            seen_hashes = {t.dedupe_hash for t in top_tracks}

            artist_tracks = self.get_artist_tracks(artist)
            artist_tracks = [
                t
                for t in artist_tracks
                if (
                    t.id not in top_track_ids
                    and not t.is_excluded(_excluded_track_ids)
                    and not t.longer_than(MAX_TRACK_LENGTH_MS)
                    and t.album is not None
                    and not t.album.is_excluded(_excluded_album_ids)
                )
            ]

            deduped: list[Track] = []
            for track in artist_tracks:
                h = getattr(track, "dedupe_hash", None)
                if h and h in seen_hashes:
                    continue
                if h:
                    seen_hashes.add(h)
                deduped.append(track)

            random.shuffle(deduped)
            playlist = top_tracks[0:MAX_TOP_TRACKS] + deduped + top_tracks[MAX_TOP_TRACKS:-1]
            playlist = playlist[0 : MAX_TOP_TRACKS + MAX_ARTIST_TRACKS]
            logger.info(f"{tag}  * found {len(playlist)} valid tracks for {artist.name}")
            random.shuffle(playlist)
            return artist_id, playlist

        futures = {self.artist_pool.submit(_process, idx, aid): aid for idx, aid in enumerate(_artist_ids)}
        fatal_error = None
        for future in as_completed(futures):
            artist_id = futures[future]
            try:
                a_id, playlist = future.result()
                artist_playlists[a_id] = playlist
            except RuntimeError as e:
                fatal_error = e
                # Cancelling each future only drops the ones that have not started;
                # the queued backlog would still run and keep calling an API that
                # just rate-limited us. cancel_futures drops the whole backlog.
                # wait=False so the abort is not held up by in-flight calls —
                # close() shuts the pools down again with wait=True and reaps them.
                self._shutdown_pools(wait=False)
                break
            except Exception:
                logger.exception(f"{self.tag}  ! failed to process artist {artist_id}, skipping")

        if fatal_error is not None:
            raise fatal_error

        return artist_playlists

    def generate_playlist(
        self,
        artist_playlists: dict[str, list[Track]],
        vip_artist_ids: list[str] | None = None,
    ) -> list[str]:
        """Spread and merge collected tracks into a final playlist (fast, CPU-only)."""
        _vip_artist_ids: list[str] = [] if vip_artist_ids is None else vip_artist_ids
        return spread_artist_playlists(artist_playlists, _vip_artist_ids, self.tag)

    def sync(self, playlist_name: str, tracks: list[str] | None = None) -> None:
        raise NotImplementedError
