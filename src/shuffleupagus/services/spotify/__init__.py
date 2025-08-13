from ...core.config import Config
from ...core.model import Service
from ..spotify.service import SpotifyService

def create(config:Config) -> Service:
    return SpotifyService(config)
