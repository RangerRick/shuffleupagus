# Shuffleupagus

Your music's best friend isn't just imaginary anymore... meet **Shuffleupagus**!

Shuffleupagus is an open-source tool for generating and synchronizing smart, balanced music playlists across multiple streaming services, including Spotify and Apple Music. It is designed to help you create playlists that fairly represent your favorite artists, avoid unwanted tracks or albums, and keep your playlists fresh and interesting.

## Features

- **Multi-Service Support:** Pluggable architecture with support for multiple services.
- **Artist Prioritization:** Mark favorite artists as VIPs to increase their presence in playlists.
- **Exclusion Rules:** Exclude specific albums or tracks from your playlists.
- **Balanced Shuffling:** Uses a balanced shuffle algorithm to distribute tracks evenly among artists.
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
