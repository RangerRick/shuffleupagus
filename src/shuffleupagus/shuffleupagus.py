import argparse
from .core.config import Config
from .core.util import init_logging, logger, load_plugins

def main():

    parser = argparse.ArgumentParser(
                        prog='Shuffleupagus',
                        description='generate and synchronize smart, balanced playlists')

    parser.add_argument('--dry-run', default=False, action='store_true', help='no playlists will be updated')
    parser.add_argument('--production', default=False, action='store_true', help='use production playlists instead of test ones')
    parser.add_argument('--log-level', default='INFO', help='set the logging level')

    args = parser.parse_args()

    init_logging(args.log_level)

    config = Config()

    plugins = load_plugins()
    for plugin in plugins:
        pluginName = plugin.__name__.split('.')[-1]
        if not config.is_enabled(pluginName):
            logger.warning(f"Service {pluginName} is disabled in the configuration, skipping.")
            continue

        service = plugin.create(config)

        artists = list(map(lambda a: service.sanitize_id(a), config.service_artists(service.name)))
        vips = list(map(lambda a: service.sanitize_id(a), config.vip_artists(service.name)))
        excluded_albums = list(map(lambda a: service.sanitize_id(a), config.excluded_albums(service.name)))
        excluded_tracks = list(map(lambda a: service.sanitize_id(a), config.excluded_tracks(service.name)))

        service.login()

        playlist_track_ids = service.generate_playlist(artists, excluded_albums, excluded_tracks, vips)

        if args.dry_run:
            logger.info(f"* DRY RUN mode, not updating playlist on {service.name}")
            continue

        playlist_name = config.playlist(service.name)
        if args.production:
            logger.warning(f"* PRODUCTION mode, pushing to {service.name} playlist: {playlist_name}")
        else:
            playlist_name = config.test_playlist(service.name)
            logger.warning(f"* TEST RUN mode, pushing to {service.name} playlist: {playlist_name}")

        service.sync(playlist_name, playlist_track_ids)
        logger.info(f"* finished updating {service.name}")

        service.close()
