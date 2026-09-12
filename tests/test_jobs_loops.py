"""Tests for loop-shaped jobs: repeat interval, chaining, weekly budget."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from claude_swap.jobs import Job, JobRunner, JobStore, RunRecord, UsagePoint, new_job_id
from claude_swap.jobs_engine import HoldEvent, JobsEngine, TickOutcome
from claude_swap.settings import JobsSettings
from tests.test_autoswitch import EngineHarness, FakeClock
from tests.test_jobs import _fake_claude
from tests.test_jobs_engine import NOW, _entry

NOW_ISO = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _job(folder, **kw) -> Job:
    base = dict(id=new_job_id(), name=kw.pop("name", "j"), folder=str(folder), prompt="p", estimate_pct=5.0)
    base.update(kw)
    return Job(**base)


def _run(cost7: float, days_ago: float) -> RunRecord:
    when = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return RunRecord(started_at=when, finished_at=when, account="a", exit_code=0,
                     session_id=None, cost_5h=1.0, cost_7d=cost7)


class TestModel:
    def test_weekly_spent_and_budget(self, tmp_path):
        import time

        now = time.time()
        j = _job(tmp_path, weekly_budget_pct=5.0, runs=(_run(2.0, 1), _run(2.0, 3), _run(9.0, 8)))
        assert j.weekly_spent(now) == 4.0
        assert not j.over_budget(now)
        j2 = Job(**{**j.__dict__, "runs": (*j.runs, _run(1.5, 0.5))})
        assert j2.over_budget(now)

    def test_ready_respects_not_before(self, tmp_path):
        j = _job(tmp_path, not_before=NOW + 60)
        assert not j.ready(NOW) and j.ready(NOW + 60)
        assert not Job(**{**j.__dict__, "state": "done"}).ready(NOW + 100)

    def test_json_roundtrip(self, tmp_path):
        store = JobStore(tmp_path)
        j = store.add(_job(tmp_path, repeat_minutes=90.0, then_job="other", weekly_budget_pct=3.0, not_before=123.0))
        got = store.get(j.id)
        assert (got.repeat_minutes, got.then_job, got.weekly_budget_pct, got.not_before) == (90.0, "other", 3.0, 123.0)


@pytest.fixture
def harness(temp_home):
    h = EngineHarness(temp_home)
    h.seed(1, "a@example.com")
    h.seed(2, "b@example.com")
    h.make_live("a@example.com", 1)
    return h


class TestRunnerLoops:
    def _runner(self, harness):
        store = JobStore(harness.switcher.backup_dir)
        return store, JobRunner(harness.switcher, JobsSettings(), store)

    def test_repeat_requeues_with_cooldown(self, harness, tmp_path):
        store, runner = self._runner(harness)
        work = tmp_path / "w"
        work.mkdir()
        job = store.add(_job(work, account="1", repeat_minutes=30.0))
        fake = _fake_claude(tmp_path)
        with (
            patch("claude_swap.jobs.shutil.which", return_value=str(fake)),
            patch.object(runner, "_usage_point", return_value=UsagePoint(None, None, None, None, {}, None)),
            patch.object(runner, "clock", return_value=1e9),
        ):
            assert runner.run_worker(job.id) == 0
        got = store.get(job.id)
        assert got.state == "queued" and got.not_before == 1e9 + 1800
        assert len(got.runs) == 1 and got.worker_pid is None

    def test_repeat_does_not_requeue_on_failure(self, harness, tmp_path):
        store, runner = self._runner(harness)
        work = tmp_path / "w"
        work.mkdir()
        job = store.add(_job(work, account="1", repeat_minutes=30.0))
        fake = _fake_claude(tmp_path, exit_code=1, is_error=True)
        with (
            patch("claude_swap.jobs.shutil.which", return_value=str(fake)),
            patch.object(runner, "_usage_point", return_value=UsagePoint(None, None, None, None, {}, None)),
        ):
            assert runner.run_worker(job.id) == 1
        assert store.get(job.id).state == "failed"

    def test_chain_requeues_next_job(self, harness, tmp_path):
        store, runner = self._runner(harness)
        work = tmp_path / "w"
        work.mkdir()
        nxt = store.add(_job(work, name="second"))
        store.update(nxt.id, state="done")
        first = store.add(_job(work, account="1", name="first", then_job="second"))
        fake = _fake_claude(tmp_path)
        with (
            patch("claude_swap.jobs.shutil.which", return_value=str(fake)),
            patch.object(runner, "_usage_point", return_value=UsagePoint(None, None, None, None, {}, None)),
        ):
            assert runner.run_worker(first.id) == 0
        assert store.get(first.id).state == "done"
        assert store.get(nxt.id).state == "queued"

    def test_chain_unknown_target_is_logged_not_fatal(self, harness, tmp_path):
        store, runner = self._runner(harness)
        work = tmp_path / "w"
        work.mkdir()
        first = store.add(_job(work, account="1", then_job="ghost"))
        fake = _fake_claude(tmp_path)
        with (
            patch("claude_swap.jobs.shutil.which", return_value=str(fake)),
            patch.object(runner, "_usage_point", return_value=UsagePoint(None, None, None, None, {}, None)),
        ):
            assert runner.run_worker(first.id) == 0
        assert "chain:" in (store.log_dir(first.id) / "worker.log").read_text()

    def test_requeue_clears_hold(self, harness, tmp_path):
        store, runner = self._runner(harness)
        job = store.add(_job(tmp_path, not_before=9e12))
        store.update(job.id, state="done")
        assert runner.requeue(store.get(job.id)).not_before is None


@pytest.fixture
def eng_harness(temp_home):
    h = EngineHarness(temp_home)
    h.seed(1, "a@example.com")
    h.seed(2, "b@example.com")
    h.make_live("a@example.com", 1)
    h.clock = FakeClock(NOW)
    return h


class TestEngineLoops:
    def _engine(self, harness, tmp_path):
        events: list = []
        store = JobStore(harness.switcher.backup_dir)
        engine = JobsEngine(
            harness.switcher, JobsSettings(quiet_minutes=0), events.append, store=store,
            clock=harness.clock, dry_run=True, claude_home=tmp_path / "claude-home",
        )
        entries = {"1": _entry(10, 10, now=NOW), "2": _entry(10, 10, now=NOW)}
        return engine, store, events, patch.object(harness.switcher, "usage_entries_by_account", return_value=entries)

    def test_held_job_waits_then_starts(self, eng_harness, tmp_path):
        engine, store, events, p = self._engine(eng_harness, tmp_path)
        store.add(_job(tmp_path, not_before=NOW + 600))
        with p:
            assert engine.tick() is TickOutcome.HELD
            assert isinstance(events[-1], HoldEvent) and events[-1].reason == "held"
            assert "cooldown" in events[-1].detail
            eng_harness.clock.advance(601)
            assert engine.tick() is TickOutcome.STARTED

    def test_over_budget_job_is_skipped(self, eng_harness, tmp_path):
        engine, store, events, p = self._engine(eng_harness, tmp_path)
        store.add(_job(tmp_path, name="spent", weekly_budget_pct=2.0, runs=(_run(3.0, 1),)))
        with (
            p,
            patch("claude_swap.jobs.Job.weekly_spent", return_value=3.0),
        ):
            assert engine.tick() is TickOutcome.HELD
            assert "over weekly budget" in events[-1].detail
