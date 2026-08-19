import os
from pathlib import Path

import yaml

_CONFIG_DIR = Path("~/.config/shuffleupagus").expanduser()


def get_filepath(name: str) -> str:
    resolved = (_CONFIG_DIR / name).resolve()
    if not resolved.is_relative_to(_CONFIG_DIR.resolve()):
        raise ValueError(f"Path traversal detected: {name!r}")
    return str(resolved)


def _load_yaml_mapping(path: str, empty_ok: bool = False) -> dict:
    """Parse a hand-edited YAML file that must hold a mapping.

    RuntimeError rather than the yaml.YAMLError, because these two files are
    edited by hand and a scanner traceback names a column offset and nothing
    the reader can act on. The mark YAML reports is kept, since the line number
    is the one genuinely useful part of it.
    """
    with open(path) as f:
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


class Config:
    __service_config: dict = {}
    __artist_config: dict = {}

    def __init__(self):
        app_config_path = get_filepath("config.yaml")
        if not os.path.exists(app_config_path):
            raise FileNotFoundError(f"Config file not found: {app_config_path}")

        config_data = _load_yaml_mapping(app_config_path)
        services = config_data.get("services", {})
        if not isinstance(services, dict):
            raise RuntimeError(  # noqa: TRY004 — a hand-edited file, see _load_yaml_mapping
                f"{app_config_path}: 'services' must be a mapping, not a {type(services).__name__}."
            )
        self.__service_config = services

        artist_config_path = get_filepath("artists.yaml")
        if not os.path.exists(artist_config_path):
            raise FileNotFoundError(f"Config file not found: {artist_config_path}")

        # An empty artists.yaml is a legitimate first-run state: nothing to sync
        # yet. An empty config.yaml is not, since it names the services to run.
        self.__artist_config = _load_yaml_mapping(artist_config_path, empty_ok=True)

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
            artist = self.__artist_config[artist_name]
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
            artist = self.__artist_config[artist_name]
            excludes = (artist.get("exclude") or {}).get(service_name, {}) or {}
            for album in excludes.get("albums", []):
                ret.append(album)
        return ret

    def excluded_tracks(self, service_name: str) -> list:
        ret = []
        for artist_name in self.__artist_config:
            artist = self.__artist_config[artist_name]
            excludes = (artist.get("exclude") or {}).get(service_name, {}) or {}
            for track in excludes.get("tracks", []):
                ret.append(track)
        return ret
