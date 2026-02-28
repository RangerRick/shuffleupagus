import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

from .core.config import Config
from .core.model import Service
from .core.util import init_logging, load_plugins, logger


def _run_service(plugin, config, args) -> None:
    """Initialize, run, and close a single service."""
    service: Service = plugin.create(config)

    artists = list(map(lambda a: service.sanitize_id(a), config.service_artists(service.name)))
    vips = list(map(lambda a: service.sanitize_id(a), config.vip_artists(service.name)))
    excluded_albums = list(map(lambda a: service.sanitize_id(a), config.excluded_albums(service.name)))
    excluded_tracks = list(map(lambda a: service.sanitize_id(a), config.excluded_tracks(service.name)))

    service.login()

    playlist_track_ids = service.generate_playlist(artists, excluded_albums, excluded_tracks, vips)

    if args.dry_run:
        logger.info(f"* DRY RUN mode, not updating playlist on {service.name}")
        service.close()
        return

    playlist_name = config.playlist(service.name)
    if args.production:
        logger.warning(f"* PRODUCTION mode, pushing to {service.name} playlist: {playlist_name}")
    else:
        playlist_name = config.test_playlist(service.name)
        logger.warning(f"* TEST RUN mode, pushing to {service.name} playlist: {playlist_name}")

    service.sync(playlist_name, playlist_track_ids)
    logger.info(f"* finished updating {service.name}")

    service.close()


def main():

    parser = argparse.ArgumentParser(
        prog="Shuffleupagus", description="generate and synchronize smart, balanced playlists"
    )

    parser.add_argument("--dry-run", default=False, action="store_true", help="no playlists will be updated")
    parser.add_argument(
        "--production",
        default=False,
        action="store_true",
        help="use production playlists instead of test ones",
    )
    parser.add_argument("--log-level", default="INFO", help="set the logging level")

    args = parser.parse_args()

    init_logging(args.log_level)

    config = Config()

    active_plugins = []
    for plugin in load_plugins():
        plugin_name = plugin.__name__.split(".")[-1]
        if config.is_enabled(plugin_name):
            active_plugins.append(plugin)
        else:
            logger.warning(f"Service {plugin_name} is disabled in the configuration, skipping.")

    with ThreadPoolExecutor(max_workers=len(active_plugins) or 1) as executor:
        futures = {executor.submit(_run_service, plugin, config, args): plugin for plugin in active_plugins}
        for future in as_completed(futures):
            future.result()  # re-raise any exceptions from the service
