"""Atomicity of the Kilnmark state write.

The plan requires that every mutation is written to a temp file inside the state
directory and os.replace()d onto state.json, so an interrupted write leaves the
previous state fully intact. These tests fail the write mid-flight and check the
old file is untouched and no temp file survives.
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


def run(home: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["KILNMARK_HOME"] = str(home)
    env["NO_COLOR"] = "1"
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, str(CLI), *args], capture_output=True, text=True, env=env, timeout=60
    )


def test_failed_replace_leaves_previous_state_intact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "kilnmark-home"
    monkeypatch.setenv("KILNMARK_HOME", str(home))
    added = run(
        home,
        "schedule",
        "add",
        "bisque",
        "--start",
        "20",
        "--segment",
        "rate=100,target=220,soak=10,cone=06",
    )
    assert added.returncode == 0
    state_file = home / "state.json"
    before = state_file.read_text(encoding="utf-8")
    assert json.loads(before)["active_schedule"] == "bisque"

    state_path = str(state_file)
    store = kilnmark.Store(state_path, kilnmark.parse_state_document(before, state_path), before)
    store.state["schedules"].append(
        {
            "name": "glaze",
            "start_temp_c": 20,
            "created_at": kilnmark.iso_now(),
            "segments": [
                {"index": 0, "rate_c_per_hr": 80, "target_temp_c": 1240, "soak_min": 45, "cone_label": "10"}
            ],
        }
    )

    def explode(source: str, destination: str) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(kilnmark.os, "replace", explode)
    with pytest.raises(kilnmark.CliError) as failure:
        store.save()
    assert failure.value.code == kilnmark.EXIT_STATE
    assert "could not write the state file" in failure.value.message

    assert state_file.read_text(encoding="utf-8") == before
    assert json.loads(state_file.read_text(encoding="utf-8"))["schedules"][0]["name"] == "bisque"
    assert sorted(path.name for path in home.iterdir()) == ["state.json"]
    assert oct(state_file.stat().st_mode & 0o777) == oct(0o600)


def test_reset_is_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "kilnmark-home"
    first = run(home, "--reset")
    second = run(home, "--reset")
    assert first.returncode == 0 and second.returncode == 0
    state_file = home / "state.json"
    assert f"state reset: {state_file}" in first.stdout
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["version"] == 1
    assert state["schedules"] == []
    assert state["cone_entries"] == []
    assert state["timer"] is None
