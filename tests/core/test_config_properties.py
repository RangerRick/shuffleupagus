"""Property-based tests for reading the two hand-edited YAML config files.

Issue #60 is about one invariant: whatever a user types into config.yaml or
artists.yaml, the program reports it as a RuntimeError naming the file, never as
a yaml.YAMLError traceback pointing at a column offset. Arbitrary text is the
right input for that claim, because the interesting cases are the ones nobody
would think to write down.
"""

from contextlib import contextmanager

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from shuffleupagus.core.config import _load_yaml_mapping

# Arbitrary text, plus fragments that steer Hypothesis toward real YAML syntax
# errors rather than text that happens to parse as a plain string.
_yaml_text = st.one_of(
    st.text(max_size=200),
    st.lists(
        st.sampled_from(
            [
                "a:",
                "  b: 1",
                "\tc: 2",
                "- item",
                "d: 'unclosed",
                'e: "unclosed',
                "[unclosed",
                "{unclosed",
                "f: |",
                "g: >",
                "*anchor",
                "&anchor",
                "---",
                "...",
                "%YAML 1.2",
                ": no key",
                "h: : :",
            ],
        ),
        max_size=8,
    ).map("\n".join),
)


def _write(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return str(path)


@given(text=_yaml_text)
def test_load_raises_runtime_error_or_returns_a_mapping(text, tmp_path_factory):
    path = _write(tmp_path_factory.mktemp("cfg"), text)
    try:
        loaded = _load_yaml_mapping(path)
    except RuntimeError:
        return
    assert isinstance(loaded, dict)


@given(text=_yaml_text)
def test_load_never_leaks_a_yaml_error(text, tmp_path_factory):
    path = _write(tmp_path_factory.mktemp("cfg"), text)
    try:
        _load_yaml_mapping(path)
    except yaml.YAMLError as exc:
        pytest.fail(f"a yaml.YAMLError escaped for {text!r:.100}: {exc}")
    except RuntimeError:
        pass


@given(text=_yaml_text)
def test_an_error_names_the_file(text, tmp_path_factory):
    path = _write(tmp_path_factory.mktemp("cfg"), text)
    try:
        _load_yaml_mapping(path)
    except RuntimeError as exc:
        message = str(exc)
    else:
        return
    assert path in message


@given(text=_yaml_text)
def test_empty_ok_only_changes_the_empty_case(text, tmp_path_factory):
    """empty_ok concerns an absent document and nothing else."""
    path = _write(tmp_path_factory.mktemp("cfg"), text)

    def outcome(empty_ok):
        try:
            return ("ok", _load_yaml_mapping(path, empty_ok=empty_ok))
        except RuntimeError:
            return ("error", None)

    strict, lenient = outcome(False), outcome(True)
    if _is_empty_document(text):
        assert strict[0] == "error"
        assert lenient == ("ok", {})
    else:
        assert strict == lenient


def _is_empty_document(text: str) -> bool:
    """Whether the text parses, and parses to nothing at all."""
    try:
        return yaml.safe_load(text) is None
    except yaml.YAMLError:
        return False


@given(data=st.dictionaries(st.text(min_size=1, max_size=8), st.integers(), max_size=5))
def test_a_mapping_round_trips(data, tmp_path_factory):
    path = _write(tmp_path_factory.mktemp("cfg"), yaml.dump(data))
    assert _load_yaml_mapping(path) == data


@given(data=st.lists(st.integers(), min_size=1, max_size=5))
def test_a_top_level_list_is_rejected(data, tmp_path_factory):
    path = _write(tmp_path_factory.mktemp("cfg"), yaml.dump(data))
    with pytest.raises(RuntimeError, match="mapping"):
        _load_yaml_mapping(path)


# --- entry shape below the top level (#70) ---

_scalar = st.none() | st.booleans() | st.integers() | st.text(max_size=8)
_shallow = st.recursive(
    _scalar,
    lambda c: st.lists(c, max_size=3) | st.dictionaries(st.text(min_size=1, max_size=6), c, max_size=3),
    max_leaves=8,
)
_artists = st.dictionaries(st.text(min_size=1, max_size=6), _shallow, max_size=4)
_services = st.dictionaries(st.text(min_size=1, max_size=6), _shallow, max_size=4)


@contextmanager
def _config_dir(tmp_path, services, artists):
    """Point the config directory at a temp dir, writing both files.

    Hypothesis rejects a function-scoped monkeypatch under @given, because the
    fixture is set up once and then reused across every generated example, so
    the patch is saved and restored by hand here instead.

    _CONFIG_DIR is redirected rather than get_filepath, so the real
    get_filepath still runs — including its path-traversal guard, which
    resolves against whatever _CONFIG_DIR currently is.
    """
    import shuffleupagus.core.config as cfg_mod

    cfg_dir = tmp_path / ".config" / "shuffleupagus"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(yaml.dump({"services": services}))
    (cfg_dir / "artists.yaml").write_text(yaml.dump(artists) if artists else "{}\n")

    original = cfg_mod._CONFIG_DIR
    cfg_mod._CONFIG_DIR = cfg_dir
    try:
        yield
    finally:
        cfg_mod._CONFIG_DIR = original


@given(services=_services, artists=_artists)
def test_no_accessor_leaks_a_raw_error(services, artists, tmp_path_factory):
    """Either the file is rejected at load, or every accessor answers cleanly.

    An AttributeError or TypeError from inside an accessor is the failure #70
    is about, and it is a claim about every config, not the ones written down.
    """
    from shuffleupagus.core.config import Config

    with _config_dir(tmp_path_factory.mktemp("cfg"), services, artists):
        try:
            config = Config()
        except RuntimeError:
            return
        for accessor in ("vip_artists", "service_artists", "excluded_albums", "excluded_tracks"):
            try:
                assert isinstance(getattr(config, accessor)("spotify"), list)
            except (AttributeError, TypeError) as exc:
                pytest.fail(f"{accessor} leaked {type(exc).__name__}: {exc}")
        assert isinstance(config.is_enabled("spotify"), bool)
        assert isinstance(config.service("spotify"), dict)


@given(services=_services, artists=_artists)
def test_a_rejection_always_names_the_file(services, artists, tmp_path_factory):
    from shuffleupagus.core.config import Config

    with _config_dir(tmp_path_factory.mktemp("cfg"), services, artists):
        try:
            Config()
        except RuntimeError as exc:
            message = str(exc)
        else:
            return
    assert "config.yaml" in message or "artists.yaml" in message
