import os

import yaml

class Config:
    __service_config:dict = {}
    __artist_config:dict = {}

    def __init__(self):
        app_config_path = os.path.expanduser("~/.config/shuffleupagus/config.yaml")
        if not os.path.exists(app_config_path):
            raise FileNotFoundError(f"Config file not found: {app_config_path}")

        with open(app_config_path, "r") as f:
            config_data = yaml.safe_load(f)
            self.__service_config = config_data.get("services", {})

        artist_config_path = os.path.expanduser("~/.config/shuffleupagus/artists.yaml")
        if not os.path.exists(artist_config_path):
            raise FileNotFoundError(f"Config file not found: {artist_config_path}")

        with open(artist_config_path, "r") as f:
            artist_data = yaml.safe_load(f)
            self.__artist_config = artist_data

    def is_enabled(self, name:str) -> bool:
        return self.__service_config.get(name, {}).get("enabled", True)

    def service(self, name:str) -> dict:
        return self.__service_config.get(name, {})

    def artists(self) -> dict:
        return self.__artist_config

    def playlist(self, service_name:str) -> str:
        pl = self.__service_config.get(service_name, {}).get("playlist", None)
        if pl is None:
            raise ValueError(f"Playlist not found for service: {service_name}")
        return pl

    def test_playlist(self, service_name:str) -> str:
        pl = self.__service_config.get(service_name, {}).get("test-playlist", None)
        if pl is None:
            raise ValueError(f"Test playlist not found for service: {service_name}")
        return pl

    def vip_artists(self, service_name:str) -> list:
        ret = []
        for artist_name in self.__artist_config:
            artist = self.__artist_config[artist_name]
            if service_name in artist.get("services") and artist.get("vip", False):
                ret.append(artist.get("services").get(service_name))
        return ret

    def service_artists(self, service_name:str) -> list:
        ret = []
        for artist_name in self.__artist_config:
            artist = self.__artist_config.get(artist_name)
            if artist and service_name in artist.get("services", {}):
                ret.append(artist.get("services").get(service_name))
        return ret
    
    def excluded_albums(self, service_name:str) -> list:
        ret = []
        for artist_name in self.__artist_config:
            artist = self.__artist_config[artist_name]
            if 'exclude' in artist and service_name in artist.get("exclude"):
                for album in artist.get("exclude").get(service_name).get('albums', []):
                    ret.append(album)
        return ret
    
    def excluded_tracks(self, service_name:str) -> list:
        ret = []
        for artist_name in self.__artist_config:
            artist = self.__artist_config[artist_name]
            if 'exclude' in artist and service_name in artist.get("exclude"):
                for track in artist.get("exclude").get(service_name).get('tracks', []):
                    ret.append(track)
        return ret