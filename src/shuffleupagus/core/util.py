import importlib
import logging
import pkgutil
import random

import coloredlogs

import shuffleupagus.services

logger = logging.getLogger("root")
logging.getLogger("urllib3").setLevel(logging.FATAL)
logging.getLogger("spotipy").setLevel(logging.FATAL)
logging.getLogger("spotipy.client").setLevel(logging.FATAL)

def init_logging(level:str):
    coloredlogs.install(level=level, logger=logger, fmt='%(message)s')

def iter_namespace(ns_pkg):
    return pkgutil.iter_modules(ns_pkg.__path__, ns_pkg.__name__ + ".")


def load_plugins():
    plugins = []

    for _, name, _ in iter_namespace(shuffleupagus.services):
        module = importlib.import_module(name)
        plugins.append(module)

    return plugins


# see: https://web.archive.org/web/20250107040006/http://keyj.emphy.de/balanced-shuffle/
def spread_artist_playlists(artist_playlists, vip_artist_ids) -> list[str]:

    logging.info("* spreading out artist playlists")
    max_length = 0

    for artistId in artist_playlists:
        max_length += len(artist_playlists[artistId])

    for artistId in artist_playlists:
        p = artist_playlists[artistId]
        new_p = []

        n = max_length
        k = len(p)

        # Create a random-ish even distribution of tracks for each artist.
        # These will get meshed together later.
        while k > 0:
            r = n/k
            noise = 0 - .10 + (random.randrange(0, 20) / 100) + 1
            r = r * noise
            r = round(r)
            if r == 0:
                r = 1
            r = min(r, n-k+1)

            segment = [None] * r
            segment[0] = p.pop(0)
            k = k-1
            n = n-r
            new_p = new_p + segment

        if len(p) > 0:
            logging.warning(f"something is wrong, we had p left over ({len(p)})")
            exit(1)

        offset = random.randint(0,20)
        if (artistId in vip_artist_ids):
            offset = vip_artist_ids.index(artistId)

        # make sure we've filled new_p to the max length properly,
        # also insert a random number of empty entries at the start
        new_p = list([None] * offset) + new_p + list([None] * max_length)
        new_p = new_p[0:max_length]

        # replace the artist playlist with the new spread playlist
        artist_playlists[artistId] = new_p

    logging.info("* merging artist playlists")
    artist_ids = list(filter(lambda id : id not in vip_artist_ids, artist_playlists.keys()))
    random.shuffle(artist_ids)

    min_position = int(len(artist_ids) * 0.05)
    max_position = int(len(artist_ids) * 0.15)
    for vip_id in vip_artist_ids:
        # insert the VIPs at a semi-random position in the list in the first 5-15%
        # so that they get mixed in with the other artists
        artist_ids.insert(random.randint(min_position, max_position), vip_id)

    logging.info(f"* combining {max_length} slots for {len(artist_ids)} artists")
    # build the final playlist, merging the artist spreads
    playlist = []
    for i in range(max_length):
        for artist_id in artist_ids:
            p = artist_playlists[artist_id]
            track = p[i]
            if track:
                playlist.append(track)

    return list(dict.fromkeys([track.id for track in playlist]))
