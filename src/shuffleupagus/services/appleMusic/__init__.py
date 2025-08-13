from ...core.config import Config
from ...core.model import Service
from ..appleMusic.service import AppleMusicService

def create(config:Config) -> Service:
    return AppleMusicService(config)
