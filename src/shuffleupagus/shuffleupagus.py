import argparse
import os
import signal
import threading

from .core.config import Config
from .core.model import Service, Track
from .core.util import init_logging, load_plugins, logger


def _collect_service(
    service: Service,
    config: Config,
) -> dict[str, list[Track]]:
    """Fetch per-artist track data (IO-heavy, runs in a thread)."""
    artists = [service.sanitize_id(a) for a in config.service_artists(service.name)]
    excluded_albums = [service.sanitize_id(a) for a in config.excluded_albums(service.name)]
    excluded_tracks = [service.sanitize_id(a) for a in config.excluded_tracks(service.name)]

    return service.collect_tracks(artists, excluded_albums, excluded_tracks)


def _finalize_service(
    service: Service,
    config: Config,
    args,
    artist_playlists: dict[str, list[Track]],
) -> None:
    """Generate final playlist and sync (fast, runs sequentially)."""
    vips = [service.sanitize_id(a) for a in config.vip_artists(service.name)]
    playlist_track_ids = service.generate_playlist(artist_playlists, vips)

    if args.dry_run:
        logger.info(f"{service.tag}* DRY RUN mode, not updating playlist on {service.name}")
        return

    playlist_name = config.playlist(service.name)
    if args.production:
        logger.warning(f"{service.tag}* PRODUCTION mode, pushing to playlist: {playlist_name}")
    else:
        playlist_name = config.test_playlist(service.name)
        logger.warning(f"{service.tag}* TEST RUN mode, pushing to playlist: {playlist_name}")

    service.sync(playlist_name, playlist_track_ids)
    logger.info(f"{service.tag}* finished updating {service.name}")


def _load_services(config: Config, only: list[str] | None = None) -> list[Service]:
    services: list[Service] = []
    for plugin in load_plugins():
        plugin_name = plugin.__name__.split(".")[-1]
        if only is not None and plugin_name not in only:
            continue
        if config.is_enabled(plugin_name):
            services.append(plugin.create(config))
        else:
            logger.warning(
                f"Service {plugin_name} is disabled in the configuration, skipping.",
            )

    for service in services:
        service.preflight()
        service.login()

    return services


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
    parser.add_argument(
        "--only-services",
        default=None,
        help="comma-separated list of services to run (e.g. spotify,youtube)",
    )

    args = parser.parse_args()

    init_logging(args.log_level)

    only = None
    if args.only_services:
        only = [s.strip() for s in args.only_services.split(",")]

    config = Config()
    services = _load_services(config, only=only)

    def _handle_sigint(_signum, _frame):
        logger.warning("* interrupted, exiting")
        os._exit(130)

    prev_handler = signal.signal(signal.SIGINT, _handle_sigint)

    # Phase 1: collect track data in parallel
    errors: list[tuple[str, Exception]] = []
    results: dict[str, dict[str, list[Track]]] = {}

    def _worker(service):
        try:
            results[service.name] = _collect_service(service, config)
        except RuntimeError as exc:
            logger.warning(f"{service.tag}! {exc}")
            errors.append((service.name, exc))
        except Exception as exc:
            logger.exception(f"Service {service.name} raised an exception")
            errors.append((service.name, exc))

    threads = [threading.Thread(target=_worker, args=(svc,)) for svc in services]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        signal.signal(signal.SIGINT, prev_handler)

    # Phase 2: generate playlists and sync sequentially
    try:
        for service in services:
            if service.name not in results:
                continue
            try:
                _finalize_service(service, config, args, results[service.name])
            except Exception as exc:
                logger.exception(f"Service {service.name} raised an exception")
                errors.append((service.name, exc))
    finally:
        for service in services:
            service.close()

    if errors:
        failed = ", ".join(name for name, _ in errors)
        raise RuntimeError(f"The following services failed: {failed}")
