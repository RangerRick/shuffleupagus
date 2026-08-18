#!/usr/bin/env bash
# Install the built wheel into a clean prefix and run the real command.
#
# The unit suite imports the package; it never runs the installed console script.
# This catches what that misses: a broken entry point, a missing runtime
# dependency that only dev extras were providing, a packaging error that leaves a
# module out of the wheel.
#
# No network and no credentials: config-example.yaml has every service
# `enabled: false`, so a --dry-run loads both config files, walks the plugin
# list, skips each service, and exits 0.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

FAKE_HOME="$WORK_DIR/home"
CONFIG_DIR="$FAKE_HOME/.config/shuffleupagus"
mkdir -p "$CONFIG_DIR"

echo "==> building wheel"
cd "$REPO_ROOT"
rm -rf dist
uv build --wheel >/dev/null
WHEEL="$(ls dist/*.whl)"
echo "    $WHEEL"

echo "==> installing into a clean prefix"
uv venv "$WORK_DIR/venv" >/dev/null 2>&1
VENV_BIN="$WORK_DIR/venv/bin"
VIRTUAL_ENV="$WORK_DIR/venv" uv pip install --quiet "$WHEEL"

CMD="$VENV_BIN/shuffleupagus"
[[ -x "$CMD" ]] || {
	echo "FAIL: console script not installed at $CMD" >&2
	exit 1
}

# 1. The entry point runs at all.
echo "==> shuffleupagus --help"
HOME="$FAKE_HOME" "$CMD" --help >"$WORK_DIR/help.txt"
grep -q -- "--dry-run" "$WORK_DIR/help.txt" || {
	echo "FAIL: --help did not list --dry-run" >&2
	cat "$WORK_DIR/help.txt" >&2
	exit 1
}

# 2. A missing config is reported as a missing config, naming the file. This is
#    the config-load path failing for the expected reason rather than an
#    ImportError or an argparse error wearing its clothes.
echo "==> missing config is reported clearly"
if HOME="$FAKE_HOME" "$CMD" --dry-run >"$WORK_DIR/noconfig.txt" 2>&1; then
	echo "FAIL: expected a non-zero exit with no config present" >&2
	exit 1
fi
grep -q "config.yaml" "$WORK_DIR/noconfig.txt" || {
	echo "FAIL: missing-config error did not name config.yaml" >&2
	cat "$WORK_DIR/noconfig.txt" >&2
	exit 1
}

# 3. The real thing: a full --dry-run against the example config. Every service
#    is disabled there, so this reaches the end of main() without a network call.
echo "==> full --dry-run against the example config"
cp "$REPO_ROOT/config-example.yaml" "$CONFIG_DIR/config.yaml"
cp "$REPO_ROOT/artists-example.yaml" "$CONFIG_DIR/artists.yaml"
HOME="$FAKE_HOME" "$CMD" --dry-run >"$WORK_DIR/dryrun.txt" 2>&1 || {
	echo "FAIL: --dry-run against the example config exited non-zero" >&2
	cat "$WORK_DIR/dryrun.txt" >&2
	exit 1
}
# Proves the config was parsed and applied, not merely opened.
grep -q "disabled in the configuration" "$WORK_DIR/dryrun.txt" || {
	echo "FAIL: --dry-run did not report the example services as disabled" >&2
	cat "$WORK_DIR/dryrun.txt" >&2
	exit 1
}

echo "smoke test: ok"
