"""claude_swap.sessions: records, transcripts, process trees, and the Sessions screen."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import sessions as S

NOW = time.time()


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + ".000Z"


def _assistant(mid: str, ts: float, text: str = "", out: int = 10, cache_read: int = 100) -> dict:
    return {"type": "assistant", "timestamp": _iso(ts), "message": {
        "id": mid, "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": text}] if text else [{"type": "tool_use", "name": "Bash", "input": {}}],
        "usage": {"input_tokens": 5, "output_tokens": out, "cache_read_input_tokens": cache_read,
                  "cache_creation_input_tokens": 1}}}


def _write_transcript(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(l) + "\n" for l in lines))


def _record(config_dir: Path, pid: int, sid: str, cwd: str, **extra) -> None:
    d = config_dir / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{pid}.json").write_text(json.dumps({"pid": pid, "sessionId": sid, "cwd": cwd, **extra}))


# -- transcripts ------------------------------------------------------------------


class TestTranscript:
    def test_usage_counted_once_per_message_not_per_line(self, tmp_path):
        p = tmp_path / "t.jsonl"
        _write_transcript(p, [
            {"type": "ai-title", "aiTitle": "Fix the grid"},
            {"type": "user", "message": {"content": "the slides are out of order"}},
            _assistant("m1", NOW - 120, "Looking."),
            _assistant("m1", NOW - 120),  # same message, second content block
            _assistant("m1", NOW - 120),
            _assistant("m2", NOW - 30, "Fixed it."),
        ])
        st = S.TranscriptCache().read(p)
        assert st.output_tokens == 20 and st.cache_read == 200
        assert st.title == "Fix the grid"
        assert st.last_prompt == "the slides are out of order" and st.last_reply == "Fixed it."
        assert st.model == "claude-opus-5-5"
        assert sum(st.timeline(NOW)) == 20

    def test_custom_title_wins_and_tool_results_are_not_prompts(self, tmp_path):
        p = tmp_path / "t.jsonl"
        _write_transcript(p, [
            {"type": "ai-title", "aiTitle": "auto"},
            {"type": "custom-title", "customTitle": "mine"},
            {"type": "user", "message": {"content": "real prompt"}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}},
            {"type": "user", "isMeta": True, "message": {"content": "meta"}},
        ])
        st = S.TranscriptCache().read(p)
        assert st.title == "mine" and st.last_prompt == "real prompt"

    def test_incremental_reads_and_partial_last_line(self, tmp_path):
        p = tmp_path / "t.jsonl"
        _write_transcript(p, [_assistant("m1", NOW - 60)])
        cache = S.TranscriptCache()
        assert cache.read(p).output_tokens == 10
        line = json.dumps(_assistant("m2", NOW - 10))
        with p.open("a") as f:
            f.write(line[:20])  # writer mid-line
        assert cache.read(p).output_tokens == 10
        with p.open("a") as f:
            f.write(line[20:] + "\n")
        assert cache.read(p).output_tokens == 20

    def test_truncated_file_starts_over(self, tmp_path):
        p = tmp_path / "t.jsonl"
        _write_transcript(p, [_assistant("m1", NOW), _assistant("m2", NOW)])
        cache = S.TranscriptCache()
        assert cache.read(p).output_tokens == 20
        _write_transcript(p, [_assistant("m3", NOW)])
        assert cache.read(p).output_tokens == 10

    def test_project_slug_matches_claude(self):
        assert S.project_slug("/Users/m/.claude") == "-Users-m--claude"
        assert S.project_slug("/Users/m/Work/LVMH Inc") == "-Users-m-Work-LVMH-Inc"


# -- process tree -----------------------------------------------------------------

PS = """\
  100     1  1.0  2048  01:00 claude --resume abc
  101   100  0.0  1024  00:59 npm exec @playwright/mcp@latest
  102   101  0.5  4096  00:58 node /x/node_modules/.bin/playwright-mcp
  103   100  2.0  1024  00:10 /bin/zsh -c npm test
  200     1  0.0  1024  02:00 claude -p do the thing --model x
  201   200  0.0  1024  01:59 /Users/m/.local/share/claude/versions/2.1.281 -p nested
  300     1  0.0  1024  10-00:00:00 /Users/m/.local/share/claude/versions/2.1.268 --agent-id a@s --agent-name draft-sweep --parent-session-id sess-100
  400     1  0.0  1024  05:00 /Users/m/.local/bin/claude daemon run --json-path x
"""


class TestProcesses:
    def test_classify(self):
        assert S.classify("claude --resume x") == "claude"
        assert S.classify("claude -p hi") == "claude -p"
        assert S.classify("/a/claude/versions/2.1 --agent-id x") == "claude teammate"
        assert S.classify("/a/bin/claude daemon run") == "claude daemon"
        assert S.classify("npm exec @playwright/mcp") == "mcp server"
        assert S.classify("/bin/zsh -c ls") == "shell"

    def test_tree_depths(self):
        procs = S.parse_ps(PS)
        kids = [(d, p.pid) for d, p in S.descendants(procs[100])]
        assert kids == [(1, 101), (2, 102), (1, 103)]


# -- snapshot ---------------------------------------------------------------------


class TestSnapshot:
    def test_joins_records_transcripts_ps_jobs_and_teammates(self, tmp_path):
        me = os.getpid()
        claude_dir, backup = tmp_path / "claude", tmp_path / "backup"
        cwd = "/work/deck"
        _record(claude_dir, me, "sess-100", cwd, name="derived", status="busy", kind="interactive",
                entrypoint="cli", tmux="s:@1.%1")
        _write_transcript(claude_dir / "projects" / S.project_slug(cwd) / "sess-100.jsonl",
                          [{"type": "ai-title", "aiTitle": "Deck work"}, _assistant("m1", NOW - 5, "ok")])
        prof = backup / "sessions" / "2-other_x.com"
        _record(prof, me, "dup", cwd)  # same pid via another profile: listed once
        (backup / "jobs.json").parent.mkdir(parents=True, exist_ok=True)
        (backup / "jobs.json").write_text(json.dumps({"jobs": [{"name": "nightly", "state": "running", "claude_pid": 200}]}))
        ps = S.parse_ps(PS.replace("  100     1", f"{me:>5}     1").replace("   100", f"{me:>6}"))
        rows = S.snapshot(claude_dir, backup, S.TranscriptCache(), ps=ps)
        by = {r.pid: r for r in rows}
        mine = by[me]
        assert mine.title == "Deck work" and mine.account == "default" and mine.status == "busy"
        assert mine.state.output_tokens == 10 and len(mine.children) == 3
        assert by[200].mode == "job" and by[200].job == "nightly" and not by[200].registered
        assert 201 not in by  # nested claude -p stays inside its parent's tree
        assert by[300].mode == "teammate" and "draft-sweep" in by[300].title and "Deck work" in by[300].title
        assert 400 not in by  # the daemon is not a session
        assert [r.pid for r in rows].count(me) == 1

    def test_dead_record_is_skipped(self, tmp_path):
        claude_dir = tmp_path / "claude"
        _record(claude_dir, 2**22 - 7, "gone", "/x")
        assert S.snapshot(claude_dir, None, S.TranscriptCache(), ps={}) == []


# -- screen -----------------------------------------------------------------------


@pytest.mark.asyncio
class TestSessionsScreen:
    async def test_lists_sessions_and_kill_asks_first(self, tmp_path, monkeypatch):
        from textual.widgets import DataTable

        from claude_swap.tui.modals import ConfirmModal
        from claude_swap.tui.sessions import SessionsScreen
        from tests.test_tui import FakeSwitcher, make_account, make_app, settle

        me = os.getpid()
        claude_dir = tmp_path / "claude"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
        _record(claude_dir, me, "sess-1", "/work/a", status="busy", kind="interactive", entrypoint="cli")
        _write_transcript(claude_dir / "projects" / S.project_slug("/work/a") / "sess-1.jsonl",
                          [{"type": "ai-title", "aiTitle": "Screen test"}, _assistant("m1", NOW - 5, "hi")])
        ps = S.parse_ps(f"{me:>5}     1  0.0  1024  00:10 claude\n")
        app = make_app(FakeSwitcher([make_account(1, active=True)], tmp_path / "backup"), start="sessions")
        with patch.object(S, "read_ps", return_value=ps), patch.object(S, "kill") as kill:
            async with app.run_test(size=(140, 44)) as pilot:
                await settle(pilot)
                await pilot.pause(0.3)
                await settle(pilot)
                assert isinstance(app.screen, SessionsScreen)
                table = app.screen.query_one("#sessions-table", DataTable)
                assert table.row_count == 1
                assert "Screen test" in str(table.get_row_at(0)[1])
                await pilot.press("K")
                await pilot.pause()
                assert isinstance(app.screen, ConfirmModal)
                kill.assert_not_called()
                await pilot.press("escape")
                await pilot.pause()
                kill.assert_not_called()


class TestLastActive:
    def test_prompt_time_is_tracked(self, tmp_path):
        p = tmp_path / "t.jsonl"
        _write_transcript(p, [
            {"type": "user", "timestamp": _iso(NOW - 3600), "message": {"content": "do it"}},
            {"type": "user", "timestamp": _iso(NOW - 10), "message": {"content": [{"type": "tool_result", "content": "x"}]}},
            _assistant("m1", NOW - 60, "done"),
        ])
        st = S.TranscriptCache().read(p)
        assert abs(st.last_user_ts - (NOW - 3600)) < 2  # tool results are not the person
        assert abs(st.last_ts - (NOW - 60)) < 2

    def test_latest_signal_wins_and_rows_sort_by_it(self, tmp_path):
        me = os.getpid()
        claude_dir = tmp_path / "claude"
        # Record A: transcript quiet for a day, but its status changed a minute ago.
        _record(claude_dir, me, "a", "/w/a", status="idle", statusUpdatedAt=(NOW - 60) * 1000)
        _write_transcript(claude_dir / "projects" / S.project_slug("/w/a") / "a.jsonl",
                          [_assistant("m1", NOW - 86400, "old")])
        rows = S.snapshot(claude_dir, None, S.TranscriptCache(), ps={})
        assert abs(rows[0].last_active - (NOW - 60)) < 2

    def test_falls_back_to_start_time(self):
        r = S.SessionInfo(pid=1, session_id="", cwd="", account="?", name=None, status=None, kind="",
                          entrypoint="", version=None, tmux=None, started_at=123.0, status_since=None,
                          transcript=None, state=S.TranscriptState(), proc=None)
        assert r.last_active == 123.0


class TestKill:
    def test_windows_tree_kill_uses_taskkill(self):
        # Windows has no os.getpgid/os.killpg; a tree kill must go through taskkill.
        done = type("R", (), {"returncode": 0})()
        with (
            patch.object(S.sys, "platform", "win32"),
            patch("claude_swap.process_detection.subprocess.run", return_value=done) as run,
        ):
            S.kill(4242, tree=True)
        assert run.call_args.args[0] == ["taskkill", "/PID", "4242", "/T", "/F"]

    def test_windows_tree_kill_failure_is_an_oserror(self):
        # The TUI reports OSError to the user; anything else would crash the screen.
        failed = type("R", (), {"returncode": 128})()
        with (
            patch.object(S.sys, "platform", "win32"),
            patch("claude_swap.process_detection.subprocess.run", return_value=failed),
            pytest.raises(OSError),
        ):
            S.kill(4242, tree=True)
