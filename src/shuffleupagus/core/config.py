import os
from pathlib import Path

import yaml

_CONFIG_DIR = Path("~/.config/shuffleupagus").expanduser()


def get_filepath(name: str) -> str:
    resolved = (_CONFIG_DIR / name).resolve()
    if not resolved.is_relative_to(_CONFIG_DIR.resolve()):
        raise ValueError(f"Path traversal detected: {name!r}")
    return str(resolved)


class Config:
    __service_config: dict = {}
    __artist_config: dict = {}

    def __init__(self):
        app_config_path = get_filepath("config.yaml")
        if not os.path.exists(app_config_path):
            raise FileNotFoundError(f"Config file not found: {app_config_path}")

        with open(app_config_path) as f:
            config_data = yaml.safe_load(f)
            self.__service_config = config_data.get("services", {})

        artist_config_path = get_filepath("artists.yaml")
        if not os.path.exists(artist_config_path):
            raise FileNotFoundError(f"Config file not found: {artist_config_path}")

        with open(artist_config_path) as f:
            artist_data = yaml.safe_load(f)
            self.__artist_config = artist_data

    def is_enabled(self, name: str) -> bool:
        return self.__service_config.get(name, {}).get("enabled", False)

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
