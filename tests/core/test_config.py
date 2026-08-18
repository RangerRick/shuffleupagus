import pytest
import yaml

from shuffleupagus.core.config import Config, get_filepath

# --- get_filepath ---


def test_get_filepath_simple():
    path = get_filepath("config.yaml")
    assert path.endswith("/.config/shuffleupagus/config.yaml")


def test_get_filepath_rejects_traversal():
    with pytest.raises(ValueError, match="Path traversal"):
        get_filepath("../etc/passwd")


# --- Config helpers ---


def _write_configs(tmp_path, services: dict, artists: dict):
    cfg_dir = tmp_path / ".config" / "shuffleupagus"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.yaml").write_text(yaml.dump({"services": services}))
    (cfg_dir / "artists.yaml").write_text(yaml.dump(artists))
    return cfg_dir


@pytest.fixture
def config(tmp_path, monkeypatch):
    import shuffleupagus.core.config as cfg_mod

    monkeypatch.setattr(
        cfg_mod,
        "get_filepath",
        lambda name: str(tmp_path / ".config" / "shuffleupagus" / name),
    )
    _write_configs(
        tmp_path,
        services={
            "spotify": {
                "enabled": True,
                "playlist": "My Playlist",
                "test-playlist": "Test Playlist",
            },
            "youtube": {
                "enabled": False,
                "playlist": "YT Playlist",
                "test-playlist": "YT Test",
            },
        },
        artists={
            "Artist A": {
                "services": {"spotify": "spotify-id-a", "youtube": "yt-id-a"},
                "vip": True,
            },
            "Artist B": {
                "services": {"spotify": "spotify-id-b"},
                "exclude": {
                    "spotify": {
                        "albums": ["album-x"],
                        "tracks": ["track-y"],
                    }
                },
            },
        },
    )
    return Config()


def test_is_enabled_true(config):
    assert config.is_enabled("spotify") is True


def test_is_enabled_false(config):
    assert config.is_enabled("youtube") is False


def test_is_enabled_missing_defaults_true(config):
    assert config.is_enabled("nonexistent") is True


def test_service_returns_dict(config):
    svc = config.service("spotify")
    assert svc["playlist"] == "My Playlist"


def test_service_missing_returns_empty(config):
    assert config.service("missing") == {}


def test_playlist(config):
    assert config.playlist("spotify") == "My Playlist"


def test_test_playlist(config):
    assert config.test_playlist("spotify") == "Test Playlist"


def test_playlist_missing_raises(config):
    with pytest.raises(ValueError):
        config.playlist("nonexistent")


def test_test_playlist_missing_raises(config):
    with pytest.raises(ValueError):
        config.test_playlist("nonexistent")


def test_service_artists_spotify(config):
    artists = config.service_artists("spotify")
    assert sorted(artists) == ["spotify-id-a", "spotify-id-b"]


def test_service_artists_youtube(config):
    artists = config.service_artists("youtube")
    assert artists == ["yt-id-a"]


def test_vip_artists(config):
    vips = config.vip_artists("spotify")
    assert vips == ["spotify-id-a"]


def test_vip_artists_youtube(config):
    vips = config.vip_artists("youtube")
    assert vips == ["yt-id-a"]


def test_excluded_albums(config):
    assert config.excluded_albums("spotify") == ["album-x"]


def test_excluded_tracks(config):
    assert config.excluded_tracks("spotify") == ["track-y"]


def test_excluded_empty_for_other_service(config):
    assert config.excluded_albums("youtube") == []


def test_missing_config_file_raises(tmp_path, monkeypatch):
    import shuffleupagus.core.config as cfg_mod

    monkeypatch.setattr(
        cfg_mod,
        "get_filepath",
        lambda name: str(tmp_path / name),
    )
    with pytest.raises(FileNotFoundError):
        Config()
