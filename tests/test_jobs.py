"""Tests for jobs.py: store, runner, argv, stream parsing."""

from __future__ import annotations

import contextlib
import json
import os
import stat
import sys
import time
from unittest.mock import patch

import pytest

from claude_swap.jobs import (
    Job,
    JobError,
    JobRunner,
    JobStore,
    UsagePoint,
    build_claude_argv,
    cost_delta,
    new_job_id,
    parse_stream_result,
    tail_stream_text,
    validate_job_fields,
)
from claude_swap.process_detection import is_pid_alive
from claude_swap.settings import JobsSettings
from tests.test_autoswitch import EngineHarness


def _job(folder, **kw) -> Job:
    base = dict(id=new_job_id(), name="t", folder=str(folder), prompt="do it")
    base.update(kw)
    return Job(**base)


class TestStore:
    def test_add_get_by_prefix_and_name(self, tmp_path):
        store = JobStore(tmp_path)
        j = store.add(_job(tmp_path, name="alpha"))
        assert store.get(j.id).name == "alpha"
        assert store.get(j.id[:5]).id == j.id
        assert store.get("alpha").id == j.id
        with pytest.raises(JobError):
            store.get("zzz")
        with pytest.raises(JobError):
            store.add(j)

    def test_ambiguous_name_raises(self, tmp_path):
        store = JobStore(tmp_path)
        store.add(_job(tmp_path, name="dup"))
        store.add(_job(tmp_path, name="dup"))
        with pytest.raises(JobError):
            store.get("dup")

    def test_queue_order(self, tmp_path):
        store = JobStore(tmp_path)
        a = store.add(_job(tmp_path, name="a", priority=50, created_at="2026-01-01T00:00:00Z"))
        b = store.add(_job(tmp_path, name="b", priority=10, created_at="2026-01-02T00:00:00Z"))
        c = store.add(_job(tmp_path, name="c", priority=50, created_at="2025-12-31T00:00:00Z"))
        store.update(a.id, state="done")
        assert [j.name for j in store.all()] == ["b", "c", "a"]
        assert [j.name for j in store.queued()] == ["b", "c"]

    def test_claim_for_run_is_single_shot(self, tmp_path):
        store = JobStore(tmp_path)
        j = store.add(_job(tmp_path))
        assert store.claim_for_run(j.id, os.getpid()) is not None
        assert store.claim_for_run(j.id, os.getpid()) is None
        assert store.get(j.id).state == "running"

    def test_claim_respects_max_concurrent(self, tmp_path):
        """The limit is enforced inside the claim's lock, so a second scheduler
        cannot start a *different* job once the slots are full."""
        store = JobStore(tmp_path)
        a = store.add(_job(tmp_path, name="a"))
        b = store.add(_job(tmp_path, name="b"))
        with patch("claude_swap.jobs.is_pid_alive", return_value=True):
            assert store.claim_for_run(a.id, os.getpid(), max_concurrent=1) is not None
            assert store.claim_for_run(b.id, os.getpid(), max_concurrent=1) is None
            assert store.get(b.id).state == "queued"
            assert store.claim_for_run(b.id, os.getpid(), max_concurrent=2) is not None

    def test_claim_without_max_concurrent_is_unbounded(self, tmp_path):
        """Omitting the limit keeps the old behaviour, so `jobs start` and the
        worker's own re-claim are unaffected."""
        store = JobStore(tmp_path)
        a = store.add(_job(tmp_path, name="a"))
        b = store.add(_job(tmp_path, name="b"))
        with patch("claude_swap.jobs.is_pid_alive", return_value=True):
            assert store.claim_for_run(a.id, os.getpid()) is not None
            assert store.claim_for_run(b.id, os.getpid()) is not None

    def test_claim_ignores_dead_workers_when_counting(self, tmp_path):
        """A crashed worker must not occupy a concurrency slot forever."""
        store = JobStore(tmp_path)
        a = store.add(_job(tmp_path, name="a"))
        b = store.add(_job(tmp_path, name="b"))
        store.claim_for_run(a.id, 4242)
        with patch("claude_swap.jobs.is_pid_alive", return_value=False):
            assert store.claim_for_run(b.id, os.getpid(), max_concurrent=1) is not None
        assert store.get(a.id).state == "failed"

    def test_dead_worker_is_reconciled(self, tmp_path):
        store = JobStore(tmp_path)
        j = store.add(_job(tmp_path))
        store.claim_for_run(j.id, 4242)
        with patch("claude_swap.jobs.is_pid_alive", return_value=False):
            got = store.get(j.id)
        assert got.state == "failed" and "died" in (got.error or "")
        with patch("claude_swap.jobs.is_pid_alive", return_value=True):
            store.claim_for_run(store.update(j.id, state="queued").id, os.getpid())
            assert store.get(j.id).state == "running"

    def test_remove_purges_logs(self, tmp_path):
        store = JobStore(tmp_path)
        j = store.add(_job(tmp_path))
        log = store.log_dir(j.id)
        log.mkdir(parents=True)
        (log / "x").write_text("x")
        assert store.remove(j.id)
        assert not log.exists()

    def test_json_roundtrip_ignores_garbage(self, tmp_path):
        store = JobStore(tmp_path)
        j = store.add(_job(tmp_path, allowed_tools=("Bash(git *)",), effort="high"))
        raw = json.loads(store.path.read_text())
        raw["jobs"].append({"id": "bad"})  # missing folder/prompt
        raw["jobs"][0]["effort"] = "turbo"  # invalid → None
        store.path.write_text(json.dumps(raw))
        jobs = store.all()
        assert len(jobs) == 1
        assert jobs[0].allowed_tools == ("Bash(git *)",) and jobs[0].effort is None
        assert jobs[0].id == j.id


class TestArgvAndParsing:
    def test_build_claude_argv(self, tmp_path):
        j = _job(
            tmp_path, model="opus", effort="high", permission_mode="bypassPermissions",
            allowed_tools=("Edit", "Bash(git *)"), max_turns=7, extra_args=("--bare",),
        )
        argv = build_claude_argv(j, "/bin/claude")
        assert argv[:3] == ["/bin/claude", "-p", "do it"]
        assert "--dangerously-skip-permissions" in argv
        assert argv[argv.index("--model") + 1] == "opus"
        assert argv[argv.index("--effort") + 1] == "high"
        assert argv[argv.index("--max-turns") + 1] == "7"
        assert argv[argv.index("--allowedTools") + 1 : argv.index("--allowedTools") + 3] == ["Edit", "Bash(git *)"]
        assert argv[-1] == "--bare"
        assert "--dangerously-skip-permissions" not in build_claude_argv(
            _job(tmp_path, permission_mode="acceptEdits"), "/bin/claude"
        )

    def test_validate_fields(self, tmp_path):
        ok = dict(folder=str(tmp_path), prompt="x", permission_mode="acceptEdits",
                  effort=None, max_turns=None, estimate_pct=5.0, priority=50)
        assert validate_job_fields(**ok) is None
        assert "Folder" in validate_job_fields(**{**ok, "folder": str(tmp_path / "nope")})
        assert "Prompt" in validate_job_fields(**{**ok, "prompt": "  "})
        assert "Permission" in validate_job_fields(**{**ok, "permission_mode": "yolo"})
        assert "Effort" in validate_job_fields(**{**ok, "effort": "turbo"})
        assert "Estimate" in validate_job_fields(**{**ok, "estimate_pct": 0})

    def test_parse_stream_result(self, tmp_path):
        p = tmp_path / "s.jsonl"
        p.write_text(
            json.dumps({"type": "system", "session_id": "sid"}) + "\n"
            "garbage\n"
            + json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "hello\nworld"},
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
            ]}}) + "\n"
            + json.dumps({"type": "result", "session_id": "sid", "is_error": False,
                          "num_turns": 2, "result": "DONE"}) + "\n"
        )
        r = parse_stream_result(p)
        assert r == {"session_id": "sid", "is_error": False, "num_turns": 2, "result": "DONE"}
        assert tail_stream_text(p) == ["hello", "world", "⚙ Bash  ls", "■ done · 2 turns"]

    def test_parse_stream_error(self, tmp_path):
        p = tmp_path / "s.jsonl"
        p.write_text(json.dumps({"type": "result", "is_error": True, "errors": ["boom"]}) + "\n")
        assert parse_stream_result(p)["error"] == "boom"
        assert parse_stream_result(tmp_path / "missing") == {}

    def test_cost_delta_handles_reset(self):
        before = UsagePoint(10.0, "r1", 20.0, "w1", {"Fable": 5.0}, 0.0)
        after = UsagePoint(14.0, "r1", 3.0, "w2", {"Fable": 7.0}, 0.0)
        c5, c7, cs = cost_delta(before, after)
        assert (c5, c7, cs) == (4.0, None, {"Fable": 2.0})
        assert cost_delta(before, UsagePoint(None, None, None, None, {}, None)) == (None, None, {})


def _fake_claude(path, *, exit_code=0, is_error=False, sleep=0.0):
    """A stand-in `claude` that writes a stream-json result to stdout."""
    script = path / "claude"
    body = f"""#!{sys.executable}
import json, sys, time, os
time.sleep({sleep})
print(json.dumps({{"type": "system", "session_id": "fake-sid"}}))
print(json.dumps({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": "hi"}}]}}}}))
print(json.dumps({{"type": "result", "session_id": "fake-sid", "is_error": {str(is_error)},
                   "num_turns": 1, "result": "ok" if not {str(is_error)} else "bad",
                   "errors": ["bad"] if {str(is_error)} else []}}))
open(os.path.join(os.getcwd(), "touched"), "w").write(os.environ.get("CLAUDE_CONFIG_DIR", ""))
sys.exit({exit_code})
"""
    script.write_text(body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


@pytest.fixture
def harness(temp_home):
    h = EngineHarness(temp_home)
    h.seed(1, "a@example.com")
    h.seed(2, "b@example.com")
    h.make_live("a@example.com", 1)
    return h


class TestRunner:
    def _runner(self, harness, tmp_path, **settings):
        store = JobStore(harness.switcher.backup_dir)
        runner = JobRunner(harness.switcher, JobsSettings(**settings), store)
        return store, runner

    def test_run_worker_success_records_cost(self, harness, tmp_path):
        store, runner = self._runner(harness, tmp_path)
        work = tmp_path / "work"
        work.mkdir()
        job = store.add(_job(work, account="1"))
        fake = _fake_claude(tmp_path)
        before = UsagePoint(10.0, "r", 20.0, "w", {}, 0.0)
        after = UsagePoint(13.0, "r", 21.0, "w", {}, 1e12)
        points = iter([before, after])
        with (
            patch("claude_swap.jobs.shutil.which", return_value=str(fake)),
            patch.object(runner, "_usage_point", side_effect=lambda *_a, **_k: next(points)),
            patch.object(runner, "clock", side_effect=[1e12 - 5, 1e12, 1e12]),
        ):
            rc = runner.run_worker(job.id)
        assert rc == 0
        got = store.get(job.id)
        assert got.state == "done" and got.session_id == "fake-sid"
        assert got.result_text == "ok" and got.account_used == "a@example.com"
        assert got.estimate_pct == 3.0 and got.weekly_estimate_pct == 1.0
        assert got.runs[-1].cost_5h == 3.0 and got.runs[-1].num_turns == 1
        assert (work / "touched").read_text() == ""  # active account → plain env
        assert (store.log_dir(job.id) / "stream.jsonl").exists()

    def test_run_worker_uses_session_profile_for_other_account(self, harness, tmp_path):
        store, runner = self._runner(harness, tmp_path)
        work = tmp_path / "work"
        work.mkdir()
        job = store.add(_job(work, account="2"))
        fake = _fake_claude(tmp_path)
        profile = tmp_path / "profile"
        with (
            patch("claude_swap.jobs.shutil.which", return_value=str(fake)),
            patch.object(runner, "_usage_point", return_value=UsagePoint(None, None, None, None, {}, None)),
            patch("claude_swap.session.SessionManager.setup_session", return_value=(profile, "2", "b@example.com")),
        ):
            assert runner.run_worker(job.id) == 0
        assert (work / "touched").read_text() == str(profile)
        assert store.get(job.id).runs[-1].cost_5h is None  # no usage → unknown cost

    def test_run_worker_failure_paths(self, harness, tmp_path):
        store, runner = self._runner(harness, tmp_path)
        work = tmp_path / "work"
        work.mkdir()
        job = store.add(_job(work, account="1"))
        fake = _fake_claude(tmp_path, exit_code=2, is_error=True)
        with (
            patch("claude_swap.jobs.shutil.which", return_value=str(fake)),
            patch.object(runner, "_usage_point", return_value=UsagePoint(None, None, None, None, {}, None)),
        ):
            assert runner.run_worker(job.id) == 1
        got = store.get(job.id)
        assert got.state == "failed" and got.error == "bad" and got.exit_code == 2

        job2 = store.add(_job(work, account="1"))
        with patch("claude_swap.jobs.shutil.which", return_value=None):
            assert runner.run_worker(job2.id) == 1
        assert "not found" in store.get(job2.id).error

    def test_run_worker_timeout(self, harness, tmp_path):
        store, runner = self._runner(harness, tmp_path, job_timeout_minutes=1.0)
        runner.settings = JobsSettings(job_timeout_minutes=0.02)  # ~1.2s
        work = tmp_path / "work"
        work.mkdir()
        job = store.add(_job(work, account="1"))
        fake = _fake_claude(tmp_path, sleep=10)
        with (
            patch("claude_swap.jobs.shutil.which", return_value=str(fake)),
            patch.object(runner, "_usage_point", return_value=UsagePoint(None, None, None, None, {}, None)),
            patch("claude_swap.jobs._KILL_GRACE_S", 1.0),
        ):
            assert runner.run_worker(job.id) == 1
        assert "timed out" in store.get(job.id).error

    def test_run_worker_timeout_kills_nested_claude(self, harness, tmp_path):
        # claude -p can spawn further claude -p calls. On timeout the whole tree
        # must die, not just the top pid: bb81f0e only group-signals a real group
        # leader, so claude needs a session of its own for that to reach the tree.
        store, runner = self._runner(harness, tmp_path, job_timeout_minutes=1.0)
        runner.settings = JobsSettings(job_timeout_minutes=0.02)  # ~1.2s
        work = tmp_path / "work"
        work.mkdir()
        pidfile = tmp_path / "grandchild.pid"
        fake = tmp_path / "claude"
        fake.write_text(
            "#!/bin/sh\n"
            f"sleep 30 & echo $! > {pidfile}\n"
            "sleep 30\n"
        )
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        job = store.add(_job(work, account="1"))
        with (
            patch("claude_swap.jobs.shutil.which", return_value=str(fake)),
            patch.object(runner, "_usage_point", return_value=UsagePoint(None, None, None, None, {}, None)),
            patch("claude_swap.jobs._KILL_GRACE_S", 1.0),
        ):
            assert runner.run_worker(job.id) == 1
        assert "timed out" in store.get(job.id).error
        grandchild = int(pidfile.read_text())
        try:
            for _ in range(20):
                if not is_pid_alive(grandchild):
                    break
                time.sleep(0.1)
            assert not is_pid_alive(grandchild), "nested claude survived the timeout"
        finally:
            with contextlib.suppress(OSError):
                os.kill(grandchild, 9)

    def test_run_worker_refuses_non_queued(self, harness, tmp_path):
        store, runner = self._runner(harness, tmp_path)
        job = store.add(_job(tmp_path))
        store.update(job.id, state="done")
        assert runner.run_worker(job.id) == 3

    def test_launch_spawns_worker_and_claims(self, harness, tmp_path):
        store, runner = self._runner(harness, tmp_path)
        job = store.add(_job(tmp_path))
        with patch("claude_swap.jobs.worker_argv", return_value=[sys.executable, "-c", "import time; time.sleep(2)"]):
            started = runner.launch(job, account="2")
        assert started.state == "running" and started.worker_pid
        assert (store.log_dir(job.id) / "worker.log").exists()
        with pytest.raises(JobError):
            runner.launch(started)

    def test_cancel_and_requeue(self, harness, tmp_path):
        store, runner = self._runner(harness, tmp_path)
        job = store.add(_job(tmp_path))
        assert runner.cancel(job).state == "cancelled"
        assert runner.requeue(store.get(job.id)).state == "queued"
        with patch("claude_swap.jobs.worker_argv", return_value=[sys.executable, "-c", "import time; time.sleep(5)"]):
            running = runner.launch(store.get(job.id))
        pid = running.worker_pid
        cancelled = runner.cancel(running)
        assert cancelled.state == "cancelled" and cancelled.worker_pid is None
        import time

        for _ in range(50):
            try:
                os.kill(pid, 0)
                os.waitpid(pid, os.WNOHANG)
                time.sleep(0.05)
            except (ProcessLookupError, ChildProcessError):
                break
        with pytest.raises(JobError):
            runner.requeue(store.update(job.id, state="running", worker_pid=os.getpid()))
