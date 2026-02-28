from ...core.config import Config
from ...core.model import Service
from .service import YoutubeService


def create(config: Config) -> Service:
    return YoutubeService(config)
