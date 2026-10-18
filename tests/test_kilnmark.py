"""End-to-end tests for the Kilnmark CLI.

Kilnmark itself is standard-library only; pytest is the harness. Every test
drives the real command line through a subprocess with its own state directory,
so nothing here depends on the invoking user's ~/.kilnmark.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "kilnmark.py"

sys.path.insert(0, str(ROOT))
import kilnmark  # noqa: E402

BISQUE = [
    "schedule",
    "add",
    "bisque",
    "--start",
    "20",
    "--segment",
    "rate=100,target=220,soak=10,cone=06",
    "--segment",
    "rate=200,target=620,soak=20,cone=6",
]


def run(home: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["KILNMARK_HOME"] = str(home)
    env["NO_COLOR"] = "1"
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def combined(result: subprocess.CompletedProcess) -> str:
    return result.stdout + result.stderr


def read_state(home: Path) -> dict:
    return json.loads((home / "state.json").read_text(encoding="utf-8"))


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    return tmp_path / "kilnmark-home"


# ── state store ──────────────────────────────────────────────────────────────


def test_first_run_creates_seeded_state(home: Path) -> None:
    result = run(home, "schedule", "list")
    assert result.returncode == 0
    assert "no schedules saved yet" in result.stdout
    state_file = home / "state.json"
    assert state_file.exists()
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["version"] == 1
    assert state["schedules"] == []
    assert state["cone_entries"] == []
    assert state["timer"] is None
    assert state["active_schedule"] is None
    assert oct(state_file.stat().st_mode & 0o777) == oct(0o600)


def test_mutation_leaves_only_the_state_file(home: Path) -> None:
    assert run(home, *BISQUE).returncode == 0
    assert sorted(path.name for path in home.iterdir()) == ["state.json"]


def test_unreadable_state_reports_path_and_reset(home: Path) -> None:
    home.mkdir(parents=True)
    state_file = home / "state.json"
    state_file.write_text('{"version": 1, "schedules": [', encoding="utf-8")
    result = run(home, "schedule", "list")
    assert result.returncode == 3
    message = combined(result)
    assert str(state_file) in message
    assert "could not be read (invalid JSON)" in message
    assert "kilnmark --reset" in message

    reset = run(home, "--reset")
    assert reset.returncode == 0
    assert f"state reset: {state_file}" in reset.stdout
    assert read_state(home)["schedules"] == []
    after = run(home, "schedule", "list")
    assert after.returncode == 0
    assert "no schedules saved yet" in after.stdout


# ── schedules ────────────────────────────────────────────────────────────────


def test_schedule_add_and_show(home: Path) -> None:
    added = run(home, *BISQUE)
    assert added.returncode == 0
    assert "saved schedule bisque with 2 segments" in added.stdout
    assert "4h 30m (270 min)" in added.stdout

    shown = run(home, "schedule", "show", "bisque")
    assert shown.returncode == 0
    lines = shown.stdout.splitlines()
    assert "rate C/hr" in shown.stdout
    rate_rows = [line for line in lines if line.strip().startswith(("1 ", "2 "))]
    assert len(rate_rows) == 2
    assert "100" in rate_rows[0] and "220" in rate_rows[0] and "06" in rate_rows[0]
    assert "200" in rate_rows[1] and "620" in rate_rows[1]
    assert shown.stdout.index("100") < shown.stdout.index("200")
    assert "total firing time: 4h 30m (270 min)" in shown.stdout


def test_schedule_list_reports_total(home: Path) -> None:
    run(home, *BISQUE)
    listed = run(home, "schedule", "list")
    assert listed.returncode == 0
    row = next(line for line in listed.stdout.splitlines() if "bisque" in line)
    assert "4h 30m" in row
    assert "270" in row


def test_bare_schedule_shows_active(home: Path) -> None:
    run(home, *BISQUE)
    run(
        home,
        "schedule",
        "add",
        "glaze",
        "--start",
        "20",
        "--segment",
        "rate=150,target=1200,soak=30,cone=10",
    )
    bare = run(home, "schedule")
    shown = run(home, "schedule", "show", "glaze")
    assert bare.returncode == 0
    assert bare.stdout == shown.stdout
    assert "schedule: glaze" in bare.stdout


def test_unknown_schedule_lists_known_names(home: Path) -> None:
    run(home, *BISQUE)
    result = run(home, "schedule", "show", "nope")
    assert result.returncode == 2
    message = combined(result)
    assert "no schedule named nope" in message
    assert "bisque" in message


def test_rate_zero_is_rejected(home: Path) -> None:
    run(home, *BISQUE)
    result = run(home, "schedule", "add", "bad", "--start", "20", "--segment", "rate=0,target=220,soak=10")
    assert result.returncode == 2
    assert "segment 1: rate must be greater than 0 (C/hr)" in combined(result)
    listed = run(home, "schedule", "list")
    assert "bisque" in listed.stdout
    assert "bad" not in listed.stdout


def test_target_must_climb(home: Path) -> None:
    result = run(
        home,
        "schedule",
        "add",
        "bad",
        "--start",
        "20",
        "--segment",
        "rate=200,target=620,soak=20",
        "--segment",
        "rate=200,target=500,soak=10",
    )
    assert result.returncode == 2
    assert "segment 2: target 500 C is not above the previous target 620 C" in combined(result)
    assert read_state(home)["schedules"] == []


def test_non_numeric_rate_is_rejected(home: Path) -> None:
    result = run(home, "schedule", "add", "bad", "--segment", "rate=fast,target=220,soak=10")
    assert result.returncode == 2
    assert "segment 1: rate must be a whole number" in combined(result)
    assert read_state(home)["schedules"] == []


def test_duplicate_schedule_name_is_rejected(home: Path) -> None:
    run(home, *BISQUE)
    result = run(home, "schedule", "add", "bisque", "--segment", "rate=100,target=220")
    assert result.returncode == 2
    assert "already exists" in combined(result)
    assert len(read_state(home)["schedules"]) == 1


def test_total_minutes_formula_and_format() -> None:
    schedule = {
        "name": "bisque",
        "start_temp_c": 20,
        "segments": [
            {"index": 0, "rate_c_per_hr": 100, "target_temp_c": 220, "soak_min": 10, "cone_label": "06"},
            {"index": 1, "rate_c_per_hr": 200, "target_temp_c": 620, "soak_min": 20, "cone_label": "6"},
        ],
    }
    assert kilnmark.schedule_total_minutes(schedule) == 270
    assert kilnmark.format_duration(kilnmark.schedule_total_minutes(schedule)) == "4h 30m (270 min)"
    assert kilnmark.format_duration(kilnmark.Fraction(45)) == "45 min"


# ── cone log ─────────────────────────────────────────────────────────────────


def test_cone_add_and_earliest_summary(home: Path) -> None:
    first = run(home, "cone", "add", "--cone", "6", "--at", "240", "--note", "down")
    assert first.returncode == 0
    assert "cone 6" in first.stdout and "240 min" in first.stdout
    run(home, "cone", "add", "--cone", "6", "--at", "180", "--note", "tip curls")
    run(home, "cone", "add", "--cone", "5", "--at", "200")

    listed = run(home, "cone", "list")
    assert listed.returncode == 0
    text = listed.stdout
    assert text.index("180 min") < text.index("200 min") < text.index("240 min")
    assert "cone 6: 2 entries, first deform 180 min" in text
    assert "cone 5: 1 entry, first deform 200 min" in text
    # earliest deform, never the latest
    assert "first deform 240 min" not in text


def test_empty_cone_log_invites_an_entry(home: Path) -> None:
    result = run(home, "cone", "list")
    assert result.returncode == 0
    assert "nothing logged yet" in result.stdout
    assert "kilnmark cone add" in result.stdout


def test_malformed_cone_entry_is_skipped_by_line(home: Path) -> None:
    run(home, "cone", "add", "--cone", "6", "--at", "180", "--note", "tip curls")
    run(home, "cone", "add", "--cone", "5", "--at", "200", "--note", "leaning")
    run(home, "cone", "add", "--cone", "6", "--at", "240", "--note", "down")

    state_file = home / "state.json"
    lines = state_file.read_text(encoding="utf-8").splitlines()
    broken_line = 0
    for index, line in enumerate(lines):
        if '"at_min": 200' in line:
            lines[index] = line.replace(', "at_min": 200', "")
            broken_line = index + 1
            break
    assert broken_line, "the cone entry with at_min 200 was not found on its own line"
    state_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = run(home, "cone", "list")
    assert result.returncode == 0
    assert f"skipping malformed cone entry at line {broken_line}" in result.stdout
    assert "180 min" in result.stdout
    assert "240 min" in result.stdout
    assert "cone 6: 2 entries, first deform 180 min" in result.stdout


def test_cone_note_is_length_checked(home: Path) -> None:
    result = run(home, "cone", "add", "--cone", "6", "--at", "180", "--note", "x" * 121)
    assert result.returncode == 2
    assert "note must be 120 characters or fewer" in combined(result)


# ── stoke timer ──────────────────────────────────────────────────────────────


def test_timer_run_plan_and_state(home: Path) -> None:
    result = run(home, "timer", "run", "--interval-min", "5", "--repeat", "3", "--now", "1700000000")
    assert result.returncode == 0
    assert "+5 min" in result.stdout
    assert "+10 min" in result.stdout
    assert "+15 min" in result.stdout
    timer = read_state(home)["timer"]
    assert timer["interval_min"] == 5
    assert timer["repeat_count"] == 3
    assert timer["started_at"] == "2023-11-14T22:13:20Z"
    assert timer["last_elapsed_at"] == "2023-11-14T22:13:20Z"


def test_timer_tick_counts_down(home: Path) -> None:
    run(home, "timer", "run", "--interval-min", "5", "--repeat", "3", "--now", "1700000000")
    tick = run(home, "timer", "--tick", "--now", "1700000120")
    assert tick.returncode == 0
    assert "next stoke in 03:00 (stoke 1 of 3)" in tick.stdout


def test_timer_reports_completion(home: Path) -> None:
    run(home, "timer", "run", "--interval-min", "5", "--repeat", "3", "--now", "1700000000")
    tick = run(home, "timer", "--tick", "--now", "1700002000")
    assert tick.returncode == 0
    assert "all 3 stokes complete (interval 5 min)" in tick.stdout


def test_timer_last_and_bare_timer_agree(home: Path) -> None:
    run(home, "timer", "run", "--interval-min", "5", "--repeat", "3", "--now", "1700000000")
    last = run(home, "timer", "--last")
    bare = run(home, "timer")
    assert last.returncode == 0 and bare.returncode == 0
    expected = "last run: every 5 min, 3 repeats, started 2023-11-14T22:13:20Z"
    assert expected in last.stdout
    assert expected in bare.stdout


def test_timer_without_a_run_explains_itself(home: Path) -> None:
    result = run(home, "timer")
    assert result.returncode == 0
    assert "no timer run yet" in result.stdout
    assert "kilnmark timer run --interval-min 5 --repeat 3" in result.stdout
    assert "default interval: 10 min" in result.stdout


def test_timer_rejects_interval_below_one_and_keeps_state(home: Path) -> None:
    run(home, "timer", "run", "--interval-min", "5", "--repeat", "3", "--now", "1700000000")
    result = run(home, "timer", "run", "--interval-min", "0")
    assert result.returncode == 2
    assert "interval must be at least 1 minute" in combined(result)
    assert read_state(home)["timer"]["interval_min"] == 5


def test_timer_rejects_non_numeric_interval(home: Path) -> None:
    result = run(home, "timer", "run", "--interval-min", "soon")
    assert result.returncode == 2
    assert "interval-min must be a whole number" in combined(result)


def test_timer_reuses_stored_defaults(home: Path) -> None:
    run(home, "timer", "run", "--interval-min", "7", "--repeat", "2", "--now", "1700000000")
    result = run(home, "timer", "run", "--now", "1700000000")
    assert result.returncode == 0
    assert read_state(home)["timer"]["interval_min"] == 7
    assert read_state(home)["timer"]["repeat_count"] == 2


# ── surface ──────────────────────────────────────────────────────────────────


def test_help_and_version() -> None:
    version = subprocess.run(
        [sys.executable, str(CLI), "--version"], capture_output=True, text=True, timeout=60
    )
    assert version.returncode == 0
    assert "kilnmark 1.0.0" in version.stdout

    help_text = subprocess.run(
        [sys.executable, str(CLI), "--help"], capture_output=True, text=True, timeout=60
    )
    assert help_text.returncode == 0
    assert "schedule" in help_text.stdout and "cone" in help_text.stdout and "timer" in help_text.stdout
    assert "examples:" in help_text.stdout


def test_json_output_is_machine_readable(home: Path) -> None:
    run(home, *BISQUE)
    run(home, "cone", "add", "--cone", "6", "--at", "180")
    schedules = json.loads(run(home, "schedule", "list", "--json").stdout)
    assert schedules["schedules"][0]["name"] == "bisque"
    assert schedules["schedules"][0]["total_minutes"] == 270
    cones = json.loads(run(home, "cone", "list", "--json").stdout)
    assert cones["summary"] == [{"cone": "6", "entries": 1, "first_deform_min": 180}]
