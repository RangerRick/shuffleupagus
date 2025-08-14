# Shuffleupagus

Your music's best friend isn't just imaginary anymore... meet **Shuffleupagus**!

Shuffleupagus is an open-source tool for generating and synchronizing smart, balanced music playlists across multiple streaming services, including Spotify and Apple Music. It is designed to help you create playlists that fairly represent your favorite artists, avoid unwanted tracks or albums, and keep your playlists fresh and interesting.

## About

This project started as a manual playlist that I was keeping up-to-date based on posts I saw on social media or other word-of-mouth.

I soon ran into an issue where I would miss adding new stuff from established artists, and also prolific artists would drown out artists with a smaller catalog.

I set out to make something that balances highlighting popular tracks from each artist, as well as filling in with a reasonable amount of back catalog, without overwhelming.

Thus: Shuffleupagus was born.

## Features

- **Multi-Service Support:** Pluggable architecture with support for multiple services.
- **Artist Prioritization:** Mark favorite artists [as VIPs](https://www.youtube.com/watch?v=Q4PE2hSqVnk "Always Be Closing") to increase their presence in playlists.
- **Exclusion Rules:** Exclude specific albums or tracks from your playlists.
- **Balanced Shuffling:** Uses a [balanced shuffle algorithm](http://keyj.emphy.de/balanced-shuffle/) to distribute tracks evenly among artists.
- **Caching:** Caches API responses for efficiency and reduced API usage.
- **Configurable:** All settings are managed via YAML configuration files.

## Requirements

- Python 3.10+
- [Spotipy](https://spotipy.readthedocs.io/) (for Spotify integration)
- [apple-music-python](https://github.com/afterxleep/apple-music-python) (for Apple Music integration)
- [py-applescript](https://github.com/rdhyee/py-applescript) (for Apple Music playlist management on macOS)
- See [pyproject.toml](pyproject.toml) for all dependencies.

## Installation

1. Clone the repository:
    ```sh
    git clone https://github.com/yourusername/shuffleupagus.git
    cd shuffleupagus
    ```

2. Install dependencies:
    ```sh
    pip install .
    ```

3. Copy the example configuration files and edit them to match your accounts and preferences:
    ```sh
    mkdir -p ~/.config/shuffleupagus
    cp config-example.yaml ~/.config/shuffleupagus/config.yaml
    cp artists-example.yaml ~/.config/shuffleupagus/artists.yaml
    ```

4. Fill in your API credentials and playlist details in the config files.

## Configuration

### Services

**NOTE:** You must create your playlists on each service before running `shuffleupagus`.

#### Apple Music

You must have an Apple Developer account, and [generate a media identifier and private key](https://developer.apple.com/help/account/capabilities/create-a-media-identifier-and-private-key/).

Additionally, you have to have a "media user token" which unfortunately can only be retrieved by logging in to https://music.apple.com/, viewing network connections in the developer console, and then looking for the `Media-User-Token` header in a request to `amp-api.music.apple.com`.

**NOTE**: The Apple Music API does not officially support deleting songs from playlists!
To work around this, Shuffleupagus must be run on a Mac with Music.app installed.
It will speak to Music.app to clear the playlist, rather than the Apple Music API, and then wait for the changes to sync, before re-pushing the playlist. 🤮

#### Spotify

Follow the instructions at [the Spotify web developer portal](https://developer.spotify.com/documentation/web-api) to create a client ID and secret.

### Artists

The artist configuration consists of a series of artists names, each with a "services" property that contains API URLs for the artist.
You can usually find these URLs by clicking "share" and "copy link" on an artist's profile in their respective apps.

Optionally, you can also include an "exclude" property on the artist, to exclude specific albums or even tracks
See the sample configuration for details.

## Usage

Run the main script to generate and sync playlists:

```sh
shuffleupagus
```

- By default, the script runs in test mode and will update the test playlist(s) specified in your config.
- Use `--dry-run` to do everything but publish to services.
- Use `--production` to publish to your production playlist rather than the test one.

## Configuration

- **[config.yaml](config-example.yaml):** Service credentials, playlist names, and enable/disable services.
- **[artists.yaml](artists-example.yaml):** List of artists, VIP status, and exclusion rules for albums/tracks.

## License

This project is licensed under the [Apache License 2.0](LICENSE).

## Contributing

Contributions are welcome! Please open issues or pull requests on GitHub.

## Playlists

Here are some example playlists that used Shuffleupagus to generate them:

* [<img src="logos/spotify.png" alt="Spotify" height="16" />](https://open.spotify.com/playlist/50aqIz3lyBbzYEz7K3lAgb) [<img src="logos/apple-music.png" alt="Apple Music" height="16" />](https://music.apple.com/us/playlist/no-fash-dungeon-synth-and-ds-adjacent/pl.u-kv9lbZ5u4yo6j) No Fash Dungeon Synth (and DS-Adjacent)