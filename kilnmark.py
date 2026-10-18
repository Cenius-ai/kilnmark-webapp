#!/usr/bin/env python3
"""Kilnmark: plan a wood-kiln firing, log cone deformation, keep the stoke cadence.

Single file, standard library only, fully offline. Every mutation of the state
file is atomic (temp file in the same directory, then os.replace onto
state.json), so an interrupted write can never truncate a firing plan.

State lives in ~/.kilnmark/state.json (override the directory with KILNMARK_HOME).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from fractions import Fraction
from typing import Any, Sequence

PROG = "kilnmark"
VERSION = "1.0.0"
STATE_VERSION = 1

EXIT_OK = 0
EXIT_INPUT = 2
EXIT_STATE = 3

DEFAULT_START_TEMP_C = 20
DEFAULT_INTERVAL_MIN = 10
DEFAULT_REPEAT = 3
MAX_NAME_LEN = 40
MAX_CONE_LEN = 12
MAX_NOTE_LEN = 120

# ── committed design direction ────────────────────────────────────────────────
# terminal dark · calm-precise · monospace · compact · aligned columns.
# One accent only: oklch(0.58 0.12 275), carried here as its exact hex.
# Colour never carries meaning on its own: every value it styles is also named
# in the plain text beside it.
ACCENT_HEX = "#6673c1"
MUTED_HEX = "#8b8da0"
RULE_HEX = "#45475a"
RULE = "\u2500"
SEP = " \u00b7 "

ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

DESCRIPTION = (
    "Plan a wood-kiln firing climb, log pyrometric cone deformation, and keep "
    "the stoke interval cadence. One local state file, no network."
)

EPILOG = """\
examples:
  kilnmark schedule add bisque --start 20 \\
      --segment rate=100,target=220,soak=10,cone=06 \\
      --segment rate=200,target=620,soak=20,cone=6
  kilnmark schedule list
  kilnmark schedule show bisque          # or bare: kilnmark schedule
  kilnmark cone add --cone 6 --at 180 --note "tip curls"
  kilnmark cone list
  kilnmark timer run --interval-min 5 --repeat 3 --now 1700000000
  kilnmark timer --tick --now 1700000120
  kilnmark timer --last                  # or bare: kilnmark timer
  kilnmark --reset

state file: ~/.kilnmark/state.json  (override the directory with KILNMARK_HOME)
durations:  minutes end to end, shown as hours+minutes with the raw minutes
exit codes: 0 success | 2 invalid input | 3 unreadable state
"""


# ── errors ───────────────────────────────────────────────────────────────────


class CliError(Exception):
    """A one-line, actionable failure carrying the process exit code."""

    def __init__(self, message: str, code: int = EXIT_INPUT, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.hint = hint


def corrupt_state(path: str, detail: str) -> CliError:
    return CliError(
        f"state file could not be read ({detail}): {path}",
        EXIT_STATE,
        hint="run `kilnmark --reset` to recreate it",
    )


# ── theme ────────────────────────────────────────────────────────────────────


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    value = hex_colour.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


class Palette:
    """ANSI styling for the committed terminal-dark palette."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _paint(self, text: str, hex_colour: str, bold: bool = False) -> str:
        if not self.enabled:
            return text
        red, green, blue = _rgb(hex_colour)
        prefix = "1;" if bold else ""
        return f"\x1b[{prefix}38;2;{red};{green};{blue}m{text}\x1b[0m"

    def accent(self, text: str) -> str:
        return self._paint(text, ACCENT_HEX, bold=True)

    def strong(self, text: str) -> str:
        if not self.enabled:
            return text
        return f"\x1b[1m{text}\x1b[0m"

    def muted(self, text: str) -> str:
        return self._paint(text, MUTED_HEX)

    def rule(self, text: str) -> str:
        return self._paint(text, RULE_HEX)


def colour_enabled(no_colour_flag: bool) -> bool:
    if no_colour_flag or os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


# ── small formatting helpers ─────────────────────────────────────────────────


def minutes_half_up(value: Fraction) -> int:
    """Round a Fraction of minutes to the nearest whole minute, halves up."""
    if value >= 0:
        return int(value + Fraction(1, 2))
    return -int(-value + Fraction(1, 2))


def format_duration(minutes: Fraction) -> str:
    """Hours+minutes with the raw minutes in parentheses; bare minutes under 1h."""
    whole = minutes_half_up(minutes)
    if whole < 60:
        return f"{whole} min"
    return f"{whole // 60}h {whole % 60}m ({whole} min)"


def format_count(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def parse_whole(raw: Any, field: str, prefix: str = "") -> int:
    text = str(raw).strip()
    try:
        return int(text, 10)
    except ValueError:
        raise CliError(f"{prefix}{field} must be a whole number (got {text!r})") from None


def iso_now() -> str:
    return iso_from_epoch(int(datetime.now(timezone.utc).timestamp()))


def iso_from_epoch(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime(ISO_FORMAT)


def epoch_from_iso(text: str) -> int:
    try:
        parsed = datetime.strptime(text, ISO_FORMAT).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        raise CliError(
            f"state file holds an unreadable timestamp: {text!r}",
            EXIT_STATE,
            hint="run `kilnmark --reset` to recreate it",
        ) from None
    return int(parsed.timestamp())


def render_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    palette: Palette,
    aligns: Sequence[str] | None = None,
) -> list[str]:
    """Aligned terminal columns: the committed signature of every Kilnmark view."""
    widths = [len(header) for header in headers]
    for row in rows:
        for position, cell in enumerate(row):
            widths[position] = max(widths[position], len(cell))
    how = list(aligns) if aligns else ["left"] * len(headers)

    def line_for(cells: Sequence[str]) -> str:
        padded = [
            cell.rjust(widths[index]) if how[index] == "right" else cell.ljust(widths[index])
            for index, cell in enumerate(cells)
        ]
        return "  ".join(padded).rstrip()

    lines = [
        palette.strong(line_for(headers)),
        palette.rule("  ".join(RULE * width for width in widths)),
    ]
    lines.extend(line_for(row) for row in rows)
    return lines


def emit_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


# ── state store ──────────────────────────────────────────────────────────────


def state_dir() -> str:
    override = os.environ.get("KILNMARK_HOME")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.expanduser("~"), ".kilnmark")


def state_path() -> str:
    return os.path.join(state_dir(), "state.json")


def seeded_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "seeded_at": iso_now(),
        "active_schedule": None,
        "schedules": [],
        "cone_entries": [],
        "timer": None,
    }


def _json_block(value: Any, indent: str) -> str:
    text = json.dumps(value, indent=2, ensure_ascii=False)
    if "\n" not in text:
        return text
    head, *rest = text.split("\n")
    return head + "\n" + "\n".join(indent + line for line in rest)


def serialize_state(state: dict[str, Any]) -> str:
    """Stable, hand-editable JSON: one cone entry per physical line.

    Cone entries keep one-per-line so a damaged record can be named by its real
    line number when it is skipped on read.
    """
    entries = state.get("cone_entries") or []
    if entries:
        cone_block = (
            "[\n"
            + ",\n".join("    " + json.dumps(entry, ensure_ascii=False) for entry in entries)
            + "\n  ]"
        )
    else:
        cone_block = "[]"
    timer = state.get("timer")
    timer_block = "null" if timer is None else _json_block(timer, "  ")
    lines = [
        "{",
        f'  "version": {json.dumps(state.get("version", STATE_VERSION))},',
        f'  "seeded_at": {json.dumps(state.get("seeded_at", iso_now()))},',
        f'  "active_schedule": {json.dumps(state.get("active_schedule"))},',
        '  "schedules": ' + _json_block(state.get("schedules") or [], "  ") + ",",
        '  "cone_entries": ' + cone_block + ",",
        '  "timer": ' + timer_block,
        "}",
    ]
    return "\n".join(lines) + "\n"


def _normalise_schedules(raw: Any, path: str) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise corrupt_state(path, "schedules is not a list")
    schedules: list[dict[str, Any]] = []
    for position, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise corrupt_state(path, f"schedule {position} is malformed")
        segments_raw = entry.get("segments")
        if not isinstance(segments_raw, list) or not segments_raw:
            raise corrupt_state(path, f"schedule {position} has no readable segments")
        start_raw = entry.get("start_temp_c", DEFAULT_START_TEMP_C)
        try:
            start = int(start_raw)
        except (TypeError, ValueError):
            raise corrupt_state(
                path, f"schedule {position} has an unreadable start temperature"
            ) from None
        segments: list[dict[str, Any]] = []
        for index, segment in enumerate(segments_raw):
            label = f"schedule {position} segment {index + 1}"
            if not isinstance(segment, dict):
                raise corrupt_state(path, f"{label} is malformed")
            try:
                rate = int(segment["rate_c_per_hr"])
                target = int(segment["target_temp_c"])
                soak = int(segment.get("soak_min", 0))
            except (KeyError, TypeError, ValueError):
                raise corrupt_state(path, f"{label} is malformed") from None
            if rate <= 0:
                raise corrupt_state(path, f"{label} has a non-positive rate")
            cone = segment.get("cone_label")
            segments.append(
                {
                    "index": index,
                    "rate_c_per_hr": rate,
                    "target_temp_c": target,
                    "soak_min": soak,
                    "cone_label": cone if isinstance(cone, str) else None,
                }
            )
        created = entry.get("created_at")
        schedules.append(
            {
                "name": entry["name"],
                "start_temp_c": start,
                "created_at": created if isinstance(created, str) else iso_now(),
                "segments": segments,
            }
        )
    return schedules


def _normalise_state(raw: dict[str, Any], path: str) -> dict[str, Any]:
    state = seeded_state()
    version = raw.get("version")
    state["version"] = version if isinstance(version, int) else STATE_VERSION
    seeded_at = raw.get("seeded_at")
    if isinstance(seeded_at, str):
        state["seeded_at"] = seeded_at
    active = raw.get("active_schedule")
    state["active_schedule"] = active if isinstance(active, str) else None
    state["schedules"] = _normalise_schedules(raw.get("schedules"), path)
    cone_entries = raw.get("cone_entries")
    if cone_entries is None:
        cone_entries = []
    if not isinstance(cone_entries, list):
        raise corrupt_state(path, "cone_entries is not a list")
    state["cone_entries"] = cone_entries
    timer = raw.get("timer")
    if timer is not None and not isinstance(timer, dict):
        raise corrupt_state(path, "timer is not an object")
    state["timer"] = timer
    return state


def parse_state_document(raw: str, path: str) -> dict[str, Any]:
    """Parse the state document with the JSON parser and validate its shape.

    The state file is plain JSON and is only ever read with json.loads, so
    parsing it cannot construct arbitrary objects or run code.
    """
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise corrupt_state(path, "invalid JSON") from None
    if not isinstance(parsed, dict):
        raise corrupt_state(path, "expected a JSON object")
    return _normalise_state(parsed, path)


class Store:
    """The single local state document plus its on-disk path."""

    def __init__(self, path: str, state: dict[str, Any], raw: str) -> None:
        self.path = path
        self.state = state
        self.raw = raw

    @classmethod
    def open_state(cls) -> "Store":
        path = state_path()
        if not os.path.exists(path):
            store = cls(path, seeded_state(), "")
            store.save()
            return store
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = handle.read()
        except OSError as exc:
            raise CliError(
                f"state file could not be read ({exc.strerror or exc}): {path}",
                EXIT_STATE,
                hint="run `kilnmark --reset` to recreate it",
            ) from None
        return cls(path, parse_state_document(raw, path), raw)

    def save(self) -> None:
        """Write to a temp file in the state directory, then os.replace it in."""
        directory = os.path.dirname(self.path) or "."
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        except OSError as exc:
            raise CliError(
                f"could not create the state directory {directory} ({exc.strerror or exc})",
                EXIT_STATE,
                hint="check the permissions on your home directory",
            ) from None
        text = serialize_state(self.state)
        temp_path = ""
        try:
            handle_fd, temp_path = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=directory)
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, self.path)
        except OSError as exc:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            raise CliError(
                f"could not write the state file {self.path} ({exc.strerror or exc})",
                EXIT_STATE,
                hint="check free disk space and the permissions of the state directory",
            ) from None
        self.raw = text


def reset_state() -> str:
    path = state_path()
    Store(path, seeded_state(), "").save()
    return path


# ── schedules ────────────────────────────────────────────────────────────────


def schedule_total_minutes(schedule: dict[str, Any]) -> Fraction:
    """sum(((target - previous) / rate) * 60 + soak); previous starts at start_temp_c."""
    previous = int(schedule.get("start_temp_c", DEFAULT_START_TEMP_C))
    total = Fraction(0)
    for segment in schedule["segments"]:
        rate = int(segment["rate_c_per_hr"])
        target = int(segment["target_temp_c"])
        soak = int(segment.get("soak_min", 0))
        total += Fraction((target - previous) * 60, rate) + soak
        previous = target
    return total


def parse_segment_fields(raw: str, number: int) -> dict[str, str]:
    fields: dict[str, str] = {}
    for chunk in raw.split(","):
        piece = chunk.strip()
        if not piece:
            continue
        if "=" not in piece:
            raise CliError(
                f"segment {number}: expected key=value pairs (got {piece!r})",
                EXIT_INPUT,
                hint="--segment rate=100,target=220,soak=10,cone=06",
            )
        key, value = piece.split("=", 1)
        key = key.strip().lower()
        if key not in ("rate", "target", "soak", "cone"):
            raise CliError(
                f"segment {number}: unknown key {key!r}",
                EXIT_INPUT,
                hint="known keys: rate, target, soak, cone",
            )
        fields[key] = value.strip()
    return fields


def build_segments(specs: Sequence[str], start_temp_c: int) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    previous = start_temp_c
    for position, raw in enumerate(specs):
        number = position + 1
        prefix = f"segment {number}: "
        fields = parse_segment_fields(raw, number)
        if "rate" not in fields:
            raise CliError(
                f"{prefix}rate is required",
                EXIT_INPUT,
                hint="--segment rate=100,target=220,soak=10,cone=06",
            )
        rate = parse_whole(fields["rate"], "rate", prefix)
        if rate <= 0:
            raise CliError(f"{prefix}rate must be greater than 0 (C/hr)")
        if "target" not in fields:
            raise CliError(
                f"{prefix}target is required",
                EXIT_INPUT,
                hint="--segment rate=100,target=220,soak=10,cone=06",
            )
        target = parse_whole(fields["target"], "target", prefix)
        if target <= previous:
            raise CliError(f"{prefix}target {target} C is not above the previous target {previous} C")
        soak = parse_whole(fields.get("soak", "0"), "soak", prefix)
        if soak < 0:
            raise CliError(f"{prefix}soak must be 0 or more minutes")
        cone = fields.get("cone") or None
        if cone and len(cone) > MAX_CONE_LEN:
            raise CliError(
                f"{prefix}cone label must be {MAX_CONE_LEN} characters or fewer (got {len(cone)})"
            )
        segments.append(
            {
                "index": position,
                "rate_c_per_hr": rate,
                "target_temp_c": target,
                "soak_min": soak,
                "cone_label": cone,
            }
        )
        previous = target
    return segments


def find_schedule(store: Store, name: str) -> dict[str, Any]:
    for schedule in store.state["schedules"]:
        if schedule["name"] == name:
            return schedule
    known = ", ".join(schedule["name"] for schedule in store.state["schedules"]) or "(none yet)"
    raise CliError(f"no schedule named {name}", EXIT_INPUT, hint=f"known schedules: {known}")


def active_schedule(store: Store) -> dict[str, Any] | None:
    schedules = store.state["schedules"]
    if not schedules:
        return None
    active_name = store.state.get("active_schedule")
    for schedule in schedules:
        if schedule["name"] == active_name:
            return schedule
    return schedules[-1]


def print_no_schedules(palette: Palette) -> None:
    print(palette.muted("no schedules saved yet"))
    print("  start one:")
    print("    kilnmark schedule add bisque --start 20 \\")
    print("      --segment rate=100,target=220,soak=10,cone=06")


def _schedule_payload(schedule: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": schedule["name"],
        "start_temp_c": schedule["start_temp_c"],
        "created_at": schedule["created_at"],
        "segments": schedule["segments"],
        "total_minutes": minutes_half_up(schedule_total_minutes(schedule)),
    }


def schedule_add(store: Store, args: argparse.Namespace, palette: Palette, as_json: bool) -> int:
    name = (args.name or "").strip()
    if not name:
        raise CliError(
            "schedule name is required",
            EXIT_INPUT,
            hint="kilnmark schedule add bisque --segment rate=100,target=220,soak=10,cone=06",
        )
    if len(name) > MAX_NAME_LEN:
        raise CliError(f"schedule name must be 1-{MAX_NAME_LEN} characters (got {len(name)})")
    if any(schedule["name"] == name for schedule in store.state["schedules"]):
        raise CliError(
            f"a schedule named {name} already exists",
            EXIT_INPUT,
            hint="saved schedules are add-only; choose another name",
        )
    start = DEFAULT_START_TEMP_C if args.start is None else parse_whole(args.start, "start temperature")
    if not args.segments:
        raise CliError(
            "at least one --segment is required",
            EXIT_INPUT,
            hint="--segment rate=100,target=220,soak=10,cone=06",
        )
    segments = build_segments(args.segments, start)
    schedule = {
        "name": name,
        "start_temp_c": start,
        "created_at": iso_now(),
        "segments": segments,
    }
    store.state["schedules"].append(schedule)
    store.state["active_schedule"] = name
    store.save()
    if as_json:
        emit_json(_schedule_payload(schedule))
        return EXIT_OK
    total = format_duration(schedule_total_minutes(schedule))
    print(f"{palette.accent('saved schedule')} {name} with {format_count(len(segments), 'segment', 'segments')}")
    print(f"  total firing time: {total}")
    print(f"  next: kilnmark schedule show {name}")
    return EXIT_OK


def schedule_list(store: Store, palette: Palette, as_json: bool) -> int:
    schedules = store.state["schedules"]
    if as_json:
        emit_json(
            {
                "active": store.state.get("active_schedule"),
                "schedules": [_schedule_payload(schedule) for schedule in schedules],
            }
        )
        return EXIT_OK
    if not schedules:
        print(palette.strong("schedules (0)"))
        print()
        print_no_schedules(palette)
        return EXIT_OK
    rows = [
        [
            schedule["name"],
            str(len(schedule["segments"])),
            format_duration(schedule_total_minutes(schedule)),
        ]
        for schedule in schedules
    ]
    print(palette.strong(f"schedules ({len(schedules)})"))
    print()
    for line in render_table(
        ["name", "segments", "total firing time"], rows, palette, ["left", "right", "left"]
    ):
        print("  " + line)
    print()
    active = store.state.get("active_schedule")
    if active:
        print(f"  {palette.muted('active')}: {active}")
    return EXIT_OK


def schedule_show(store: Store, name: str | None, palette: Palette, as_json: bool) -> int:
    if name is None:
        schedule = active_schedule(store)
    else:
        schedule = find_schedule(store, name)
    if schedule is None:
        if as_json:
            emit_json({"schedule": None})
            return EXIT_OK
        print(palette.strong("schedule (0)"))
        print()
        print_no_schedules(palette)
        return EXIT_OK
    if as_json:
        emit_json(_schedule_payload(schedule))
        return EXIT_OK
    total = schedule_total_minutes(schedule)
    header = f"start {schedule['start_temp_c']} C"
    segments_label = format_count(len(schedule["segments"]), "segment", "segments")
    saved = schedule.get("created_at", "")
    print(f"{palette.accent('schedule:')} {schedule['name']}")
    print(f"  {header}{SEP}{segments_label}{SEP}saved {saved}")
    print()
    rows = [
        [
            str(segment["index"] + 1),
            str(segment["rate_c_per_hr"]),
            str(segment["target_temp_c"]),
            str(segment["soak_min"]),
            segment.get("cone_label") or "-",
        ]
        for segment in schedule["segments"]
    ]
    for line in render_table(
        ["segment", "rate C/hr", "target C", "soak min", "cone"],
        rows,
        palette,
        ["left", "right", "right", "right", "left"],
    ):
        print("  " + line)
    print()
    print(f"  total firing time: {format_duration(total)}")
    return EXIT_OK


def run_schedule(store: Store, args: argparse.Namespace, palette: Palette, as_json: bool) -> int:
    action = args.action or "show"
    if action == "add":
        return schedule_add(store, args, palette, as_json)
    if action == "list":
        return schedule_list(store, palette, as_json)
    return schedule_show(store, args.name, palette, as_json)


# ── cone deformation log ─────────────────────────────────────────────────────


def _cone_entry_lines(raw_text: str) -> list[int]:
    """1-based physical line numbers of the cone entries inside the state file."""
    lines = raw_text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if '"cone_entries"' in line:
            start = index
            break
    if start is None:
        return []
    numbers: list[int] = []
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("]"):
            break
        if stripped.startswith("{"):
            numbers.append(index + 1)
    return numbers


def coerce_cone_entry(entry: Any) -> dict[str, Any] | None:
    """Return a readable entry, or None when the stored record is malformed."""
    if not isinstance(entry, dict):
        return None
    cone = entry.get("cone")
    if not isinstance(cone, str) or not cone.strip():
        return None
    at_min = entry.get("at_min")
    if isinstance(at_min, bool):
        return None
    if isinstance(at_min, int):
        pass
    elif isinstance(at_min, str) and at_min.strip().lstrip("-").isdigit():
        at_min = int(at_min.strip())
    else:
        return None
    if at_min < 0:
        return None
    note = entry.get("note")
    created = entry.get("created_at")
    return {
        "id": str(entry.get("id") or ""),
        "cone": cone.strip(),
        "at_min": at_min,
        "note": note if isinstance(note, str) else "",
        "created_at": created if isinstance(created, str) else "",
    }


def read_cone_entries(store: Store) -> tuple[list[dict[str, Any]], list[int]]:
    raw_entries = store.state.get("cone_entries") or []
    numbers = _cone_entry_lines(store.raw)
    readable: list[dict[str, Any]] = []
    malformed: list[int] = []
    for position, entry in enumerate(raw_entries):
        parsed = coerce_cone_entry(entry)
        if parsed is None:
            malformed.append(numbers[position] if position < len(numbers) else position + 1)
        else:
            readable.append(parsed)
    readable.sort(key=lambda item: (item["at_min"], item["created_at"]))
    return readable, malformed


def cone_summary(entries: Sequence[dict[str, Any]]) -> list[tuple[str, int, int]]:
    """(cone, entry count, earliest deform minutes), soonest deform first."""
    groups: dict[str, dict[str, int]] = {}
    for entry in entries:
        group = groups.setdefault(entry["cone"], {"count": 0, "first": entry["at_min"]})
        group["count"] += 1
        group["first"] = min(group["first"], entry["at_min"])
    ordered = sorted(groups.items(), key=lambda item: (item[1]["first"], item[0]))
    return [(cone, values["count"], values["first"]) for cone, values in ordered]


def describe_cone(cone: str, count: int, first: int) -> str:
    return f"cone {cone}: {format_count(count, 'entry', 'entries')}, first deform {first} min"


def normalise_cone_label(raw: str) -> str:
    label = raw.strip()
    if label.lower().startswith("cone "):
        label = label[5:].strip()
    if not label:
        raise CliError("cone label must not be empty", EXIT_INPUT, hint="--cone 6")
    if len(label) > MAX_CONE_LEN:
        raise CliError(f"cone label must be {MAX_CONE_LEN} characters or fewer (got {len(label)})")
    return label


def cone_add(store: Store, args: argparse.Namespace, palette: Palette, as_json: bool) -> int:
    if not args.cone:
        raise CliError(
            "cone label is required",
            EXIT_INPUT,
            hint='kilnmark cone add --cone 6 --at 180 --note "tip curls"',
        )
    cone = normalise_cone_label(args.cone)
    if args.at is None:
        raise CliError(
            "at (elapsed minutes) is required",
            EXIT_INPUT,
            hint="kilnmark cone add --cone 6 --at 180",
        )
    at_min = parse_whole(args.at, "at", "")
    if at_min < 0:
        raise CliError("at must be 0 or more minutes since the firing start")
    note = (args.note or "").strip()
    if len(note) > MAX_NOTE_LEN:
        raise CliError(f"note must be {MAX_NOTE_LEN} characters or fewer (got {len(note)})")
    entry = {
        "id": os.urandom(4).hex(),
        "cone": cone,
        "at_min": at_min,
        "note": note,
        "created_at": iso_now(),
    }
    store.state["cone_entries"].append(entry)
    store.save()
    readable, _ = read_cone_entries(store)
    summary = {item[0]: item for item in cone_summary(readable)}
    count, first = summary[cone][1], summary[cone][2]
    if as_json:
        emit_json(entry)
        return EXIT_OK
    suffix = f" ({note})" if note else ""
    print(f"logged cone {cone} at {at_min} min{suffix}")
    print(f"  {describe_cone(cone, count, first)}")
    print("  next: kilnmark cone list")
    return EXIT_OK


def cone_list(store: Store, palette: Palette, as_json: bool) -> int:
    entries, malformed = read_cone_entries(store)
    summary = cone_summary(entries)
    if as_json:
        emit_json(
            {
                "entries": entries,
                "summary": [
                    {"cone": cone, "entries": count, "first_deform_min": first}
                    for cone, count, first in summary
                ],
                "malformed_lines": malformed,
            }
        )
        return EXIT_OK
    for line in malformed:
        print(f"skipping malformed cone entry at line {line}")
    if not entries:
        print(palette.strong("cone log (0 entries)"))
        print()
        print(palette.muted("nothing logged yet"))
        print("  log a cone:")
        print('    kilnmark cone add --cone 6 --at 180 --note "tip curls"')
        return EXIT_OK
    print(palette.strong(f"cone log ({format_count(len(entries), 'entry', 'entries')})"))
    print()
    rows = [[f"{entry['at_min']} min", entry["cone"], entry["note"] or "-"] for entry in entries]
    for line in render_table(["elapsed", "cone", "note"], rows, palette, ["right", "left", "left"]):
        print("  " + line)
    print()
    print(f"  {palette.muted('per cone (earliest deform)')}")
    for cone, count, first in summary:
        print(f"    {describe_cone(cone, count, first)}")
    return EXIT_OK


def run_cone(store: Store, args: argparse.Namespace, palette: Palette, as_json: bool) -> int:
    if (args.action or "list") == "add":
        return cone_add(store, args, palette, as_json)
    return cone_list(store, palette, as_json)


# ── stoke interval timer ─────────────────────────────────────────────────────


def timer_settings(store: Store) -> dict[str, Any] | None:
    """The stored run, or None when the timer has never run."""
    timer = store.state.get("timer")
    if timer is None:
        return None
    try:
        interval = int(timer["interval_min"])
        repeat = int(timer["repeat_count"])
        started_at = str(timer["started_at"])
    except (KeyError, TypeError, ValueError):
        raise corrupt_state(store.path, "timer run is malformed") from None
    if interval < 1 or repeat < 1:
        raise corrupt_state(store.path, "timer run is malformed")
    last_at = timer.get("last_elapsed_at")
    if not isinstance(last_at, str):
        last_at = started_at
    schedule = timer.get("schedule")
    return {
        "interval_min": interval,
        "repeat_count": repeat,
        "started_at": started_at,
        "started_epoch": epoch_from_iso(started_at),
        "last_elapsed_at": last_at,
        "last_epoch": epoch_from_iso(last_at),
        "schedule": schedule if isinstance(schedule, str) else None,
    }


def resolve_epoch(raw: str | None) -> int:
    if raw is None:
        return int(datetime.now(timezone.utc).timestamp())
    return parse_whole(raw, "now (epoch seconds)")


def clock_text(total_seconds: int) -> str:
    minutes, seconds = divmod(max(0, total_seconds), 60)
    return f"{minutes:02d}:{seconds:02d}"


def setting_or_default(raw: str | None, stored: Any, fallback: int, field: str) -> int:
    """Explicit flag wins, then the stored run's value, then the built-in default."""
    if raw is not None:
        return parse_whole(raw, field)
    if isinstance(stored, int) and not isinstance(stored, bool):
        return stored
    return fallback


def print_no_timer(palette: Palette) -> None:
    print(palette.muted("no timer run yet"))
    print("  run one:    kilnmark timer run --interval-min 5 --repeat 3")
    print("  inspect:    kilnmark timer --last")
    print("  countdown:  kilnmark timer --tick --now 1700000120")
    print(f"  default interval: {DEFAULT_INTERVAL_MIN} min, {DEFAULT_REPEAT} repeats")


def timer_run(store: Store, args: argparse.Namespace, palette: Palette, as_json: bool) -> int:
    stored = store.state.get("timer") or {}
    interval = setting_or_default(
        args.interval_min, stored.get("interval_min"), DEFAULT_INTERVAL_MIN, "interval-min"
    )
    repeat = setting_or_default(args.repeat, stored.get("repeat_count"), DEFAULT_REPEAT, "repeat")
    if interval < 1:
        raise CliError("interval must be at least 1 minute")
    if repeat < 1:
        raise CliError("repeat must be at least 1")
    schedule_name = None
    if args.schedule:
        schedule_name = find_schedule(store, args.schedule)["name"]
    started_at = iso_from_epoch(resolve_epoch(args.now))
    store.state["timer"] = {
        "interval_min": interval,
        "repeat_count": repeat,
        "started_at": started_at,
        "last_elapsed_at": started_at,
        "schedule": schedule_name,
    }
    store.save()
    stokes = [{"stoke": index + 1, "at_min": (index + 1) * interval} for index in range(repeat)]
    if as_json:
        emit_json(
            {
                "interval_min": interval,
                "repeat_count": repeat,
                "started_at": started_at,
                "schedule": schedule_name,
                "stokes": stokes,
            }
        )
        return EXIT_OK
    print(
        f"{palette.accent('stoke plan:')} every {interval} min, "
        f"{format_count(repeat, 'repeat', 'repeats')}"
    )
    rows = [
        [str(item["stoke"]), f"+{item['at_min']} min", clock_text(item["at_min"] * 60)]
        for item in stokes
    ]
    for line in render_table(["stoke", "at", "clock"], rows, palette, ["left", "right", "right"]):
        print("  " + line)
    print(f"started at {started_at}")
    print(f"next stoke in {clock_text(interval * 60)} (stoke 1 of {repeat})")
    return EXIT_OK


def timer_tick(store: Store, args: argparse.Namespace, palette: Palette, as_json: bool) -> int:
    timer = timer_settings(store)
    if timer is None:
        if as_json:
            emit_json({"timer": None})
            return EXIT_OK
        print_no_timer(palette)
        return EXIT_OK
    now_epoch = resolve_epoch(args.now)
    period = timer["interval_min"] * 60
    elapsed = now_epoch - timer["started_epoch"]
    index = max(0, elapsed // period)
    complete = index >= timer["repeat_count"]
    if complete:
        message = (
            f"all {timer['repeat_count']} stokes complete "
            f"(interval {timer['interval_min']} min)"
        )
        remaining = 0
    else:
        remaining = period * (index + 1) - elapsed
        message = (
            f"next stoke in {clock_text(remaining)} "
            f"(stoke {index + 1} of {timer['repeat_count']})"
        )
    if now_epoch > timer["last_epoch"]:
        timer_doc = store.state.get("timer")
        if isinstance(timer_doc, dict):
            timer_doc["last_elapsed_at"] = iso_from_epoch(now_epoch)
            store.save()
    if as_json:
        emit_json(
            {
                "interval_min": timer["interval_min"],
                "repeat_count": timer["repeat_count"],
                "started_at": timer["started_at"],
                "elapsed_min": max(0, elapsed // 60),
                "remaining_seconds": remaining,
                "stoke": None if complete else index + 1,
                "complete": complete,
            }
        )
        return EXIT_OK
    print(message)
    print(
        f"  started {timer['started_at']}{SEP}elapsed {max(0, elapsed // 60)} min"
        f"{SEP}interval {timer['interval_min']} min"
    )
    return EXIT_OK


def timer_last(store: Store, palette: Palette, as_json: bool) -> int:
    timer = timer_settings(store)
    if timer is None:
        if as_json:
            emit_json({"timer": None})
            return EXIT_OK
        print_no_timer(palette)
        return EXIT_OK
    if as_json:
        emit_json({key: value for key, value in timer.items() if not key.endswith("_epoch")})
        return EXIT_OK
    print(
        f"last run: every {timer['interval_min']} min, "
        f"{format_count(timer['repeat_count'], 'repeat', 'repeats')}, "
        f"started {timer['started_at']}"
    )
    print(f"  last tick at {timer['last_elapsed_at']}")
    if timer["schedule"]:
        print(f"  schedule: {timer['schedule']}")
    print(
        f"  reuse: kilnmark timer run --interval-min {timer['interval_min']} "
        f"--repeat {timer['repeat_count']}"
    )
    return EXIT_OK


def run_timer(store: Store, args: argparse.Namespace, palette: Palette, as_json: bool) -> int:
    if args.action == "run":
        return timer_run(store, args, palette, as_json)
    if args.last and args.tick:
        raise CliError("use either --tick or --last, not both", EXIT_INPUT)
    if args.tick:
        return timer_tick(store, args, palette, as_json)
    return timer_last(store, palette, as_json)


# ── command line ─────────────────────────────────────────────────────────────


def _add_output_flags(parser: argparse.ArgumentParser, suppress: bool) -> None:
    default = argparse.SUPPRESS if suppress else False
    parser.add_argument(
        "--json", action="store_true", default=default, help="machine-readable JSON output"
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        default=default,
        dest="no_color",
        help="disable ANSI colour (NO_COLOR is honoured too)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{PROG} {VERSION} (python {sys.version.split()[0]}, standard library only)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="recreate the seeded state file at ~/.kilnmark/state.json",
    )
    _add_output_flags(parser, suppress=False)
    subparsers = parser.add_subparsers(dest="command", metavar="{schedule,cone,timer}")

    schedule = subparsers.add_parser(
        "schedule",
        help="plan a firing climb and read its total firing time",
        description=(
            "Add, list and show firing schedules. The total firing time is derived at read "
            "time as the sum over segments of ((target - previous_target) / rate) * 60 + soak, "
            "with previous_target starting at the schedule start temperature."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    schedule.add_argument(
        "action",
        nargs="?",
        choices=("add", "list", "show"),
        default=None,
        help="add, list, or show (default: show the active schedule)",
    )
    schedule.add_argument("name", nargs="?", default=None, help="schedule name for add and show")
    schedule.add_argument(
        "--start",
        metavar="C",
        default=None,
        help=f"start temperature in C (default {DEFAULT_START_TEMP_C})",
    )
    schedule.add_argument(
        "--segment",
        metavar="rate=R,target=T,soak=S,cone=C",
        action="append",
        default=[],
        dest="segments",
        help="one firing segment; repeat the flag and the input order is preserved",
    )
    _add_output_flags(schedule, suppress=True)

    cone = subparsers.add_parser(
        "cone",
        help="log pyrometric cone deformation and summarise the first deform",
        description=(
            "Record each cone that goes down, then read the firing back: entries in "
            "chronological order plus a per-cone summary giving the earliest deform time."
        ),
    )
    cone.add_argument(
        "action",
        nargs="?",
        choices=("add", "list"),
        default=None,
        help="add or list (default: list)",
    )
    cone.add_argument("--cone", metavar="LABEL", default=None, help="cone label, e.g. 06, 6, 10")
    cone.add_argument(
        "--at", metavar="MIN", default=None, help="whole minutes elapsed since the firing start"
    )
    cone.add_argument("--note", metavar="TEXT", default=None, help="free text, 120 chars max")
    _add_output_flags(cone, suppress=True)

    timer = subparsers.add_parser(
        "timer",
        help="set and reuse the stoke interval cadence",
        description=(
            "Plan and inspect a stoke interval timer. Pass --now <epoch seconds> to make a "
            "countdown deterministic; nothing ever sleeps or blocks."
        ),
    )
    timer.add_argument(
        "action", nargs="?", choices=("run",), default=None, help="run: start a new cadence"
    )
    timer.add_argument(
        "--interval-min",
        metavar="N",
        default=None,
        dest="interval_min",
        help=f"minutes between stokes (default: the stored run, else {DEFAULT_INTERVAL_MIN})",
    )
    timer.add_argument(
        "--repeat",
        metavar="K",
        default=None,
        help=f"number of stokes (default: the stored run, else {DEFAULT_REPEAT})",
    )
    timer.add_argument(
        "--now",
        metavar="EPOCH",
        default=None,
        help="injected clock in epoch seconds so a tick is deterministic",
    )
    timer.add_argument("--schedule", metavar="NAME", default=None, help="schedule name for context")
    timer.add_argument("--last", action="store_true", help="print the stored last run")
    timer.add_argument("--tick", action="store_true", help="print the countdown to the next stoke")
    _add_output_flags(timer, suppress=True)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    palette = Palette(colour_enabled(getattr(args, "no_color", False)))
    as_json = bool(getattr(args, "json", False))
    try:
        if getattr(args, "reset", False):
            path = reset_state()
            print(f"{palette.accent('state reset:')} {path}")
            return EXIT_OK
        if args.command is None:
            parser.print_help()
            return EXIT_OK
        store = Store.open_state()
        if args.command == "schedule":
            return run_schedule(store, args, palette, as_json)
        if args.command == "cone":
            return run_cone(store, args, palette, as_json)
        if args.command == "timer":
            return run_timer(store, args, palette, as_json)
        raise CliError(f"unknown command {args.command!r}")
    except CliError as exc:
        print(f"{palette.accent('error:')} {exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"hint: {exc.hint}", file=sys.stderr)
        return exc.code
    except BrokenPipeError:
        return EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        sys.exit(130)
