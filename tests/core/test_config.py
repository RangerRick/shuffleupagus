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


# --- malformed YAML (#60) ---


def _configure_paths(tmp_path, monkeypatch):
    import shuffleupagus.core.config as cfg_mod

    cfg_dir = tmp_path / ".config" / "shuffleupagus"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cfg_mod, "get_filepath", lambda name: str(cfg_dir / name))
    return cfg_dir


def test_malformed_service_config_names_the_file(tmp_path, monkeypatch):
    cfg_dir = _configure_paths(tmp_path, monkeypatch)
    (cfg_dir / "config.yaml").write_text("services:\n  spotify:\n   - unclosed: '\n")
    (cfg_dir / "artists.yaml").write_text(yaml.dump({}))

    with pytest.raises(RuntimeError) as excinfo:
        Config()
    assert "config.yaml" in str(excinfo.value)


def test_malformed_artist_config_names_the_file(tmp_path, monkeypatch):
    cfg_dir = _configure_paths(tmp_path, monkeypatch)
    (cfg_dir / "config.yaml").write_text(yaml.dump({"services": {}}))
    (cfg_dir / "artists.yaml").write_text("Artist A:\n\tservices: bad tab\n")

    with pytest.raises(RuntimeError) as excinfo:
        Config()
    assert "artists.yaml" in str(excinfo.value)


def test_malformed_config_reports_the_line(tmp_path, monkeypatch):
    cfg_dir = _configure_paths(tmp_path, monkeypatch)
    (cfg_dir / "config.yaml").write_text("services:\n  spotify:\n   - unclosed: '\n")
    (cfg_dir / "artists.yaml").write_text(yaml.dump({}))

    with pytest.raises(RuntimeError) as excinfo:
        Config()
    assert "line" in str(excinfo.value).lower()


def test_malformed_config_does_not_leak_a_yaml_error(tmp_path, monkeypatch):
    cfg_dir = _configure_paths(tmp_path, monkeypatch)
    (cfg_dir / "config.yaml").write_text("services: [unclosed\n")
    (cfg_dir / "artists.yaml").write_text(yaml.dump({}))

    with pytest.raises(RuntimeError) as excinfo:
        Config()
    assert not isinstance(excinfo.value, yaml.YAMLError)


def test_non_mapping_service_config_is_rejected(tmp_path, monkeypatch):
    cfg_dir = _configure_paths(tmp_path, monkeypatch)
    (cfg_dir / "config.yaml").write_text("- just\n- a\n- list\n")
    (cfg_dir / "artists.yaml").write_text(yaml.dump({}))

    with pytest.raises(RuntimeError) as excinfo:
        Config()
    assert "config.yaml" in str(excinfo.value)


def test_empty_config_file_is_rejected(tmp_path, monkeypatch):
    cfg_dir = _configure_paths(tmp_path, monkeypatch)
    (cfg_dir / "config.yaml").write_text("")
    (cfg_dir / "artists.yaml").write_text(yaml.dump({}))

    with pytest.raises(RuntimeError) as excinfo:
        Config()
    assert "config.yaml" in str(excinfo.value)


def test_empty_artist_file_is_an_empty_mapping(tmp_path, monkeypatch):
    cfg_dir = _configure_paths(tmp_path, monkeypatch)
    (cfg_dir / "config.yaml").write_text(yaml.dump({"services": {}}))
    (cfg_dir / "artists.yaml").write_text("")

    assert Config().artists() == {}


def test_non_utf8_config_names_the_file(tmp_path, monkeypatch):
    cfg_dir = _configure_paths(tmp_path, monkeypatch)
    (cfg_dir / "config.yaml").write_bytes(b"services:\n  name: \xff\xfe\xfd\n")
    (cfg_dir / "artists.yaml").write_text(yaml.dump({}))

    with pytest.raises(RuntimeError) as excinfo:
        Config()
    assert "config.yaml" in str(excinfo.value)
    assert "UTF-8" in str(excinfo.value)


def test_non_utf8_config_does_not_leak_a_unicode_error(tmp_path, monkeypatch):
    cfg_dir = _configure_paths(tmp_path, monkeypatch)
    (cfg_dir / "config.yaml").write_bytes(b"\xff\xfe\x00\x01binary")
    (cfg_dir / "artists.yaml").write_text(yaml.dump({}))

    with pytest.raises(RuntimeError) as excinfo:
        Config()
    assert not isinstance(excinfo.value, UnicodeDecodeError)


def test_yaml_error_message_is_bounded(tmp_path, monkeypatch):
    """The parser quotes the offending line, which is untrusted file content."""
    cfg_dir = _configure_paths(tmp_path, monkeypatch)
    (cfg_dir / "config.yaml").write_text("services: [" + "x" * 5000 + "\n")
    (cfg_dir / "artists.yaml").write_text(yaml.dump({}))

    with pytest.raises(RuntimeError) as excinfo:
        Config()
    assert len(str(excinfo.value)) < 400


def test_services_must_be_a_mapping(tmp_path, monkeypatch):
    cfg_dir = _configure_paths(tmp_path, monkeypatch)
    (cfg_dir / "config.yaml").write_text(yaml.dump({"services": ["a", "b"]}))
    (cfg_dir / "artists.yaml").write_text(yaml.dump({}))

    with pytest.raises(RuntimeError, match="services"):
        Config()


def test_missing_services_section_names_the_cause(tmp_path, monkeypatch):
    """Defaulting to {} enabled every service, then failed with a symptom."""
    cfg_dir = _configure_paths(tmp_path, monkeypatch)
    (cfg_dir / "config.yaml").write_text(yaml.dump({"something-else": {}}))
    (cfg_dir / "artists.yaml").write_text(yaml.dump({}))

    with pytest.raises(RuntimeError, match="no 'services:' section"):
        Config()


def test_non_utf8_config_is_rejected_under_any_locale(tmp_path, monkeypatch):
    """The decode must not depend on the shell's LANG.

    latin-1 decodes every byte sequence without error, so under that locale an
    unreadable file would parse as mojibake instead of raising.
    """
    monkeypatch.setenv("LANG", "en_US.ISO8859-1")
    monkeypatch.setenv("LC_ALL", "en_US.ISO8859-1")
    cfg_dir = _configure_paths(tmp_path, monkeypatch)
    (cfg_dir / "config.yaml").write_bytes(b"services:\n  name: \xff\xfe\xfd\n")
    (cfg_dir / "artists.yaml").write_text(yaml.dump({}))

    with pytest.raises(RuntimeError, match="UTF-8"):
        Config()


# --- entry shape below the top level (#70, #73 item 5) ---


def _cfg(tmp_path, monkeypatch, services, artists):
    cfg_dir = _configure_paths(tmp_path, monkeypatch)
    (cfg_dir / "config.yaml").write_text(yaml.dump({"services": services}))
    (cfg_dir / "artists.yaml").write_text(yaml.dump(artists))
    return cfg_dir


@pytest.mark.parametrize("bad", ["enabled", ["spotify"], 42, True])
def test_a_non_mapping_service_entry_names_the_service(tmp_path, monkeypatch, bad):
    """`spotify: enabled` instead of `spotify: {enabled: true}` is a real typo."""
    _cfg(tmp_path, monkeypatch, {"spotify": bad}, {})
    with pytest.raises(RuntimeError) as excinfo:
        Config()
    assert "spotify" in str(excinfo.value)
    assert "config.yaml" in str(excinfo.value)


@pytest.mark.parametrize("bad", ["spotify", ["spotify"], 42])
def test_a_non_mapping_artist_entry_names_the_artist(tmp_path, monkeypatch, bad):
    _cfg(tmp_path, monkeypatch, {}, {"Some Band": bad})
    with pytest.raises(RuntimeError) as excinfo:
        Config()
    assert "Some Band" in str(excinfo.value)
    assert "artists.yaml" in str(excinfo.value)


def test_a_bare_artist_key_is_allowed(tmp_path, monkeypatch):
    """`Artist A:` with nothing under it is a legitimate not-yet-configured state.

    service_artists already tolerated it; the other three accessors crashed.
    """
    cfg_dir = _configure_paths(tmp_path, monkeypatch)
    (cfg_dir / "config.yaml").write_text(yaml.dump({"services": {}}))
    (cfg_dir / "artists.yaml").write_text("Artist A:\nArtist B:\n  services:\n    spotify: sid\n")
    config = Config()
    assert config.vip_artists("spotify") == []
    assert config.service_artists("spotify") == ["sid"]
    assert config.excluded_albums("spotify") == []
    assert config.excluded_tracks("spotify") == []


@pytest.mark.parametrize("accessor", ["vip_artists", "excluded_albums", "excluded_tracks", "service_artists"])
def test_no_accessor_raises_an_attribute_error(tmp_path, monkeypatch, accessor):
    """AttributeError from inside an accessor is the failure #70 is about."""
    cfg_dir = _configure_paths(tmp_path, monkeypatch)
    (cfg_dir / "config.yaml").write_text(yaml.dump({"services": {}}))
    (cfg_dir / "artists.yaml").write_text("Artist A:\n")
    config = Config()
    assert getattr(config, accessor)("spotify") == []


def test_a_non_mapping_services_block_on_an_artist(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch, {}, {"Band": {"services": "spotify"}})
    with pytest.raises(RuntimeError, match="Band"):
        Config()


def test_a_non_mapping_exclude_block(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch, {}, {"Band": {"services": {"spotify": "s"}, "exclude": "albums"}})
    with pytest.raises(RuntimeError, match="Band"):
        Config()


def test_a_non_list_exclude_albums(tmp_path, monkeypatch):
    _cfg(
        tmp_path,
        monkeypatch,
        {},
        {"Band": {"services": {"spotify": "s"}, "exclude": {"spotify": {"albums": "just-one"}}}},
    )
    with pytest.raises(RuntimeError, match="Band"):
        Config()


def test_a_valid_config_is_unchanged(tmp_path, monkeypatch):
    _cfg(
        tmp_path,
        monkeypatch,
        {"spotify": {"enabled": True, "playlist": "P", "test-playlist": "T"}},
        {
            "Band": {
                "services": {"spotify": "sid"},
                "vip": True,
                "exclude": {"spotify": {"albums": ["a1"], "tracks": ["t1"]}},
            }
        },
    )
    config = Config()
    assert config.is_enabled("spotify") is True
    assert config.playlist("spotify") == "P"
    assert config.vip_artists("spotify") == ["sid"]
    assert config.excluded_albums("spotify") == ["a1"]
    assert config.excluded_tracks("spotify") == ["t1"]


def test_a_malicious_artist_name_is_escaped_in_the_message(tmp_path, monkeypatch):
    """A key is file content, so it is escaped before it reaches a log.

    A raw name carrying a terminal escape sequence would otherwise be
    interpreted by whatever renders the message.
    """
    _cfg(tmp_path, monkeypatch, {}, {"\x1b[31mred\x1b[0m": "not-a-mapping"})
    with pytest.raises(RuntimeError) as excinfo:
        Config()
    assert "\x1b" not in str(excinfo.value)


def test_a_very_long_artist_name_is_truncated(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch, {}, {"x" * 5000: "not-a-mapping"})
    with pytest.raises(RuntimeError) as excinfo:
        Config()
    assert len(str(excinfo.value)) < 300
