#!/usr/bin/env bash
# Fail if any module's coverage is below its own floor.
#
# The aggregate fail_under in pyproject.toml is a whole-project number, so one
# module can rot to 20% while another sits at 100% and the gate stays green.
# Floors live in [tool.coverage-floors]; `default` applies to every module
# without an explicit entry.
set -euo pipefail

COVERAGE_JSON="${1:-coverage.json}"
PYPROJECT="${2:-pyproject.toml}"

if [[ ! -f "$COVERAGE_JSON" ]]; then
	echo "error: $COVERAGE_JSON not found — run pytest with --cov-report=json:$COVERAGE_JSON first" >&2
	exit 2
fi

# tomllib is stdlib; emit "<path>\t<floor>" so the comparison below stays in bash.
floors="$(uv run python3 -c '
import sys, tomllib
with open(sys.argv[1], "rb") as fh:
    cfg = tomllib.load(fh).get("tool", {}).get("coverage-floors", {})
print(cfg.pop("default", 80))
for path, floor in cfg.items():
    print(f"{path}\t{floor}")
' "$PYPROJECT")"

default_floor="$(head -n1 <<<"$floors")"
overrides="$(tail -n +2 <<<"$floors")"

failed=0
while IFS=$'\t' read -r path percent; do
	[[ -z "$path" ]] && continue
	floor="$(awk -F'\t' -v p="$path" '$1 == p {print $2}' <<<"$overrides")"
	floor="${floor:-$default_floor}"
	# awk compares as floats. Rounding first would let 79.95 pass an 80 floor.
	if awk -v x="$percent" -v f="$floor" 'BEGIN { exit !(x < f) }'; then
		printf 'BELOW FLOOR  %6.2f%% < %s%%  %s\n' "$percent" "$floor" "$path" >&2
		failed=1
	fi
done < <(jq -r '.files | to_entries[] | "\(.key)\t\(.value.summary.percent_covered)"' "$COVERAGE_JSON")

if ((failed)); then
	echo "" >&2
	echo "Raise the module's coverage, or adjust its floor in [tool.coverage-floors]" >&2
	echo "with a comment saying why. Never lower a floor just to make a red run green." >&2
	exit 1
fi

echo "per-module coverage: all modules at or above their floor (default ${default_floor}%)"
