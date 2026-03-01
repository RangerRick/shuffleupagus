import argparse
import signal
from concurrent.futures import ThreadPoolExecutor, as_completed

from .core.config import Config
from .core.model import Service
from .core.util import init_logging, load_plugins, logger


def _run_service(service: Service, config, args) -> None:
    """Log in, generate playlist, and sync a single service."""
    artists = [service.sanitize_id(a) for a in config.service_artists(service.name)]
    vips = [service.sanitize_id(a) for a in config.vip_artists(service.name)]
    excluded_albums = [service.sanitize_id(a) for a in config.excluded_albums(service.name)]
    excluded_tracks = [service.sanitize_id(a) for a in config.excluded_tracks(service.name)]

    service.login()

    playlist_track_ids = service.generate_playlist(
        artists,
        excluded_albums,
        excluded_tracks,
        vips,
    )

    if args.dry_run:
        logger.info(
            f"* DRY RUN mode, not updating playlist on {service.name}",
        )
        return

    playlist_name = config.playlist(service.name)
    if args.production:
        logger.warning(
            f"* PRODUCTION mode, pushing to {service.name} playlist: {playlist_name}",
        )
    else:
        playlist_name = config.test_playlist(service.name)
        logger.warning(
            f"* TEST RUN mode, pushing to {service.name} playlist: {playlist_name}",
        )

    service.sync(playlist_name, playlist_track_ids)
    logger.info(f"* finished updating {service.name}")


def main():

    parser = argparse.ArgumentParser(
        prog="Shuffleupagus",
        description="generate and synchronize smart, balanced playlists",
    )

    parser.add_argument(
        "--dry-run",
        default=False,
        action="store_true",
        help="no playlists will be updated",
    )
    parser.add_argument(
        "--production",
        default=False,
        action="store_true",
        help="use production playlists instead of test ones",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="set the logging level",
    )

    args = parser.parse_args()

    init_logging(args.log_level)

    config = Config()

    services: list[Service] = []
    for plugin in load_plugins():
        plugin_name = plugin.__name__.split(".")[-1]
        if config.is_enabled(plugin_name):
            services.append(plugin.create(config))
        else:
            logger.warning(
                f"Service {plugin_name} is disabled in the configuration, skipping.",
            )

    for service in services:
        service.preflight()

    executor = ThreadPoolExecutor(max_workers=16)
    interrupted = False

    def _handle_sigint(_signum, _frame):
        nonlocal interrupted
        interrupted = True
        logger.warning("* interrupted, shutting down...")
        executor.shutdown(wait=False, cancel_futures=True)

    prev_handler = signal.signal(signal.SIGINT, _handle_sigint)

    errors: list[tuple[str, Exception]] = []
    try:
        for service in services:
            service.executor = executor

        futures = {executor.submit(_run_service, service, config, args): service for service in services}
        for future in as_completed(futures):
            service = futures[future]
            try:
                future.result()
            except Exception as exc:
                logger.exception(
                    f"Service {service.name} raised an exception",
                )
                errors.append((service.name, exc))
    finally:
        for service in services:
            service.close()
        executor.shutdown(wait=not interrupted)
        signal.signal(signal.SIGINT, prev_handler)

    if errors:
        failed = ", ".join(name for name, _ in errors)
        raise RuntimeError(f"The following services failed: {failed}")
