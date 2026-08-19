import os
from pathlib import Path

import yaml

_CONFIG_DIR = Path("~/.config/shuffleupagus").expanduser()


def contained_path(root: Path, name: str) -> str:
    """Join `name` under `root`, refusing anything that lands outside it.

    One implementation on purpose. The cache root needs the same guard as the
    config root (#76), and a security check kept in two places is one someone
    fixes once — the second copy then quietly becomes the way in.

    `resolve()` runs on both sides so a symlinked root compares against what it
    actually points at. An absolute `name` replaces the root under pathlib's
    join rules rather than extending it, which is why it has to be caught here
    and not by a `..` check.
    """
    resolved = (root / name).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"Path traversal detected: {name!r}")
    return str(resolved)


def get_filepath(name: str) -> str:
    return contained_path(_CONFIG_DIR, name)


def _load_yaml_mapping(path: str, empty_ok: bool = False) -> dict:
    """Parse a hand-edited YAML file that must hold a mapping.

    RuntimeError rather than the yaml.YAMLError, because these two files are
    edited by hand and a scanner traceback names a column offset and nothing
    the reader can act on. The mark YAML reports is kept, since the line number
    is the one genuinely useful part of it.
    """
    # encoding is explicit: open() otherwise decodes with the locale encoding,
    # so the same file would be read differently under a different LANG, and
    # the UnicodeDecodeError below would fire on one machine and not another.
    # YAML 1.2 files are UTF-8, regardless of what the shell is set to.
    with open(path, encoding="utf-8") as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
            # Truncated: the parser quotes the offending line back, and that
            # line is file content this program does not control.
            problem = getattr(exc, "problem", None) or "could not be parsed"
            raise RuntimeError(f"{path} is not valid YAML{where}: {problem!r:.60}. Fix the syntax and retry.") from exc
        except UnicodeDecodeError as exc:
            # Not a YAMLError: the decode fails before the parser sees anything.
            raise RuntimeError(
                f"{path} is not valid UTF-8 text at byte {exc.start}. "
                "Re-save it as UTF-8, or delete it to start from the defaults."
            ) from exc

    if data is None:
        if empty_ok:
            return {}
        raise RuntimeError(f"{path} is empty. Populate it, or delete it to start from the defaults.")

    # runner logs those with a traceback. This is a file the user hand-edited, so
    # it takes the RuntimeError path that prints a message and no traceback.
    if not isinstance(data, dict):
        raise RuntimeError(  # noqa: TRY004
            f"{path} must hold a mapping at the top level, not a {type(data).__name__}."
        )
    return data


def _require_mapping(value: object, what: str, path: str) -> dict:
    """Return a value that must be a mapping, naming the file and the entry.

    Checked here rather than at each accessor, so the message can name the file
    the user has to edit. An accessor only knows it was handed the wrong shape.
    """
    if not isinstance(value, dict):
        raise RuntimeError(  # noqa: TRY004 — a hand-edited file, see _load_yaml_mapping
            f"{path}: {what} must be a mapping, not a {type(value).__name__}."
        )
    return value


def _name(value: object) -> str:
    """A bounded, escaped form of a key taken from a config file.

    Same reasoning as the YAML parser message in _load_yaml_mapping: a key is
    file content this program does not control. repr matters more than the
    length here, because it escapes control characters and terminal escape
    sequences that would otherwise reach a log verbatim.
    """
    return f"{value!r:.60}"


def _check_services(services: dict, path: str) -> None:
    """Every service entry must be a mapping of settings."""
    for name, entry in services.items():
        _require_mapping(entry, f"service {_name(name)}", path)


def _check_artists(artists: dict, path: str) -> None:
    """Validate every artist entry and the blocks under it.

    A bare `Artist A:` key parses as None and is allowed: it is an artist the
    user has listed but not yet mapped to any service. `service_artists` already
    read it that way, while the other three accessors raised AttributeError on
    it, so this settles which of the two was right.
    """
    for name, entry in artists.items():
        if entry is None:
            continue
        artist = _require_mapping(entry, f"artist {_name(name)}", path)

        services = artist.get("services")
        if services is not None:
            _require_mapping(services, f"artist {_name(name)} services", path)

        excludes = artist.get("exclude")
        if excludes is None:
            continue
        for service_name, rules in _require_mapping(excludes, f"artist {_name(name)} exclude", path).items():
            if rules is None:
                continue
            block = _require_mapping(rules, f"artist {_name(name)} exclude.{_name(service_name)}", path)
            for kind in ("albums", "tracks"):
                listed = block.get(kind)
                if listed is not None and not isinstance(listed, list):
                    raise RuntimeError(
                        f"{path}: artist {_name(name)} exclude.{_name(service_name)}.{kind} must be a list, "
                        f"not a {type(listed).__name__}."
                    )


class Config:
    __service_config: dict = {}
    __artist_config: dict = {}

    def __init__(self):
        app_config_path = get_filepath("config.yaml")
        if not os.path.exists(app_config_path):
            raise FileNotFoundError(f"Config file not found: {app_config_path}")

        config_data = _load_yaml_mapping(app_config_path)
        # Presence, not just type. Defaulting to {} made every service look
        # enabled and then failed later with "Playlist not found for service:
        # X", which names a symptom and not the cause.
        if "services" not in config_data:
            raise RuntimeError(
                f"{app_config_path} has no 'services:' section, so there is nothing to sync. "
                "Add one naming the services to run."
            )
        services = config_data["services"]
        if not isinstance(services, dict):
            raise RuntimeError(  # noqa: TRY004 — a hand-edited file, see _load_yaml_mapping
                f"{app_config_path}: 'services' must be a mapping, not a {type(services).__name__}."
            )
        _check_services(services, app_config_path)
        self.__service_config = services

        artist_config_path = get_filepath("artists.yaml")
        if not os.path.exists(artist_config_path):
            raise FileNotFoundError(f"Config file not found: {artist_config_path}")

        # An empty artists.yaml is a legitimate first-run state: nothing to sync
        # yet. An empty config.yaml is not, since it names the services to run.
        artists = _load_yaml_mapping(artist_config_path, empty_ok=True)
        _check_artists(artists, artist_config_path)
        self.__artist_config = artists

    def is_enabled(self, name: str) -> bool:
        return self.__service_config.get(name, {}).get("enabled", True)

    def service(self, name: str) -> dict:
        return self.__service_config.get(name, {})

    def artists(self) -> dict:
        return self.__artist_config

    def playlist(self, service_name: str) -> str:
        pl = self.__service_config.get(service_name, {}).get("playlist", None)
        if pl is None:
            raise ValueError(f"Playlist not found for service: {service_name}")
        return pl

    def test_playlist(self, service_name: str) -> str:
        pl = self.__service_config.get(service_name, {}).get("test-playlist", None)
        if pl is None:
            raise ValueError(f"Test playlist not found for service: {service_name}")
        return pl

    def vip_artists(self, service_name: str) -> list:
        ret = []
        for artist_name in self.__artist_config:
            # A bare `Artist A:` key parses as None — an artist listed but not
            # yet mapped to a service. Shape is validated at load, so anything
            # that is not None here is a mapping.
            artist = self.__artist_config[artist_name] or {}
            services = artist.get("services", {}) or {}
            if service_name in services and artist.get("vip", False):
                ret.append(services.get(service_name))
        return ret

    def service_artists(self, service_name: str) -> list:
        ret = []
        for artist_name in self.__artist_config:
            artist = self.__artist_config.get(artist_name)
            if not artist:
                continue
            services = artist.get("services", {}) or {}
            if service_name in services:
                ret.append(services.get(service_name))
        return ret

    def excluded_albums(self, service_name: str) -> list:
        ret = []
        for artist_name in self.__artist_config:
            artist = self.__artist_config[artist_name] or {}
            excludes = (artist.get("exclude") or {}).get(service_name, {}) or {}
            for album in excludes.get("albums", []):
                ret.append(album)
        return ret

    def excluded_tracks(self, service_name: str) -> list:
        ret = []
        for artist_name in self.__artist_config:
            artist = self.__artist_config[artist_name] or {}
            excludes = (artist.get("exclude") or {}).get(service_name, {}) or {}
            for track in excludes.get("tracks", []):
                ret.append(track)
        return ret
