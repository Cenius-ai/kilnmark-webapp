#!/bin/sh
# Kilnmark end-to-end demo.
#
# Non-interactive, offline, and safe to re-run: it points KILNMARK_HOME at a
# scratch directory next to this script so your real ~/.kilnmark is untouched,
# and it deletes that scratch directory again at the end.
#
#   sh demo.sh

set -u
cd "$(dirname "$0")" || exit 1

DEMO_HOME="${KILNMARK_DEMO_HOME:-$(pwd)/.kilnmark-demo}"
export KILNMARK_HOME="$DEMO_HOME"
rm -rf "$DEMO_HOME"

KM="python3 kilnmark.py"

section() {
    printf '\n\033[1m== %s\033[0m\n' "$1"
}

section "kilnmark — version"
$KM --version

section "1 · plan a firing climb"
$KM schedule add bisque --start 20 \
    --segment rate=100,target=220,soak=10,cone=06 \
    --segment rate=200,target=620,soak=20,cone=6
$KM schedule add glaze --start 20 \
    --segment rate=80,target=600,soak=30,cone=8 \
    --segment rate=60,target=1240,soak=45,cone=10
$KM schedule list
$KM schedule show bisque
$KM schedule

section "2 · invalid input is refused with exit code 2"
$KM schedule add bad --start 20 --segment rate=0,target=220,soak=10
printf 'exit code: %s\n' "$?"

section "3 · log pyrometric cone deformation"
$KM cone add --cone 06 --at 150 --note "tip starts to bend"
$KM cone add --cone 5 --at 200 --note "leaning, still upright"
$KM cone add --cone 6 --at 205 --note "tip curling"
$KM cone add --cone 6 --at 240 --note "down on the shelf"
$KM cone list

section "4 · stoke interval cadence with an injected clock"
$KM timer run --interval-min 5 --repeat 3 --now 1700000000 --schedule glaze
$KM timer --tick --now 1700000180
$KM timer --last

section "5 · a damaged state file is reported, then repaired"
printf '{"version": 1, "schedules": [' > "$DEMO_HOME/state.json"
$KM schedule list
printf 'exit code: %s\n' "$?"
$KM --reset

section "6 · the state file on disk"
ls -l "$DEMO_HOME"
printf 'state file: %s\n' "$DEMO_HOME/state.json"

rm -rf "$DEMO_HOME"
printf '\nDemo finished; scratch state removed. Full help: python3 kilnmark.py --help\n'
