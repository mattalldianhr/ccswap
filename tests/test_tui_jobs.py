"""Pilot tests for the Jobs screen, job form, and start modal."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from textual.widgets import Input, ListView, Select, Static, TextArea

from claude_swap.jobs import Job, JobStore, new_job_id
from claude_swap.usage_store import UsageEntry
from tests.test_tui import FakeSwitcher, make_account, make_app, make_entry, settle

NOW = datetime.now(timezone.utc).timestamp()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class JobsFakeSwitcher(FakeSwitcher):
    """FakeSwitcher plus the store-only surface the jobs engine reads."""

    def __init__(self, accounts, backup_dir: Path, *, five=(10.0, 10.0), seven=(10.0, 10.0)):
        super().__init__(accounts, backup_dir)
        self._five = five
        self._seven = seven

        class _Store:
            pass

        from claude_swap.usage_history import UsageHistory

        self._usage_store = _Store()
        self._usage_store.history = UsageHistory(backup_dir / "cache")

    def usage_entries_by_account(self, fetch=None, *, scheduled=False):
        out = {}
        for i, acc in enumerate(self._accounts):
            out[acc.number] = UsageEntry(
                last_good={
                    "five_hour": {"pct": self._five[i], "resets_at": _iso(NOW + 3600)},
                    "seven_day": {"pct": self._seven[i], "resets_at": _iso(NOW + 86400)},
                },
                fetched_at=NOW - 5, age_s=5.0,
            )
        return out

    def _get_sequence_data(self):
        return {
            "activeAccountNumber": int(self.active) if self.active else None,
            "accounts": {a.number: {"email": a.email, "organizationUuid": ""} for a in self._accounts},
        }

    def _account_kind(self, num):
        return "oauth"

    @staticmethod
    def _disabled_from_data(data, num):
        return False

    def resolve_account(self, identifier):
        for a in self._accounts:
            if a.number == str(identifier) or a.email == identifier:
                return a.number, a.email, ""
        from claude_swap.exceptions import AccountNotFoundError

        raise AccountNotFoundError(identifier)

    def _get_current_account(self):
        return None


def _job(folder: Path, **kw) -> Job:
    base = dict(id=new_job_id(), name="j", folder=str(folder), prompt="do the thing", estimate_pct=5.0)
    base.update(kw)
    return Job(**base)


@pytest.fixture
def fake(tmp_path):
    return JobsFakeSwitcher(
        [make_account(1, active=True, entry=make_entry(10, 10)), make_account(2, entry=make_entry(60, 30))],
        tmp_path,
    )


async def _open(pilot):
    await settle(pilot)
    await pilot.press("b")
    await pilot.pause()
    await settle(pilot)
    await pilot.pause()


def _rows(app):
    from claude_swap.tui.jobs import JobRow

    return list(app.screen.query(JobRow))


@pytest.mark.asyncio
class TestJobsScreen:
    async def test_opens_from_menu_lists_queue_in_order(self, fake, tmp_path):
        store = JobStore(tmp_path)
        store.add(_job(tmp_path, name="later", priority=50))
        store.add(_job(tmp_path, name="sooner", priority=10))
        done = store.add(_job(tmp_path, name="old"))
        store.update(done.id, state="done")
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await _open(pilot)
            from claude_swap.tui.jobs import JobsScreen

            assert isinstance(app.screen, JobsScreen)
            names = [r.job.name for r in _rows(app)]
            assert names == ["sooner", "later", "old"]
            summary = app.screen.query_one("#jobs-summary", Static).render()
            assert "2 queued" in str(summary)
            badge = app.screen.query_one("#jobs-badge", Static)
            assert badge.has_class("dry")

    async def test_dashboard_strip_hidden_without_jobs_and_shown_with(self, fake, tmp_path):
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            from claude_swap.tui.widgets import JobsStrip

            strip = app.screen.query_one(JobsStrip)
            assert strip.display is False
            JobStore(tmp_path).add(_job(tmp_path, name="queued-one"))
            strip._reload()
            assert strip.display is True
            assert "1 queued" in str(strip.render())

    async def test_detail_explains_why_a_job_waits(self, fake, tmp_path):
        store = JobStore(tmp_path)
        store.add(_job(tmp_path, name="big", estimate_pct=95.0))
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await _open(pilot)
            await settle(pilot)
            await pilot.pause()
            app.screen._update_detail()
            detail = str(app.screen.query_one("#jobs-detail", Static).render())
            assert "needs 95%" in detail
            assert "spare" in detail or "capacity" in detail

    async def test_capacity_strip_renders_accounts(self, fake, tmp_path):
        JobStore(tmp_path).add(_job(tmp_path))
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await _open(pilot)
            await settle(pilot)
            await pilot.pause()
            app.screen._update_capacity()
            cap = str(app.screen.query_one("#jobs-capacity", Static).render())
            assert "#1" in cap and "#2" in cap and "spare" in cap

    async def test_toggle_auto_and_priority_keys(self, fake, tmp_path):
        store = JobStore(tmp_path)
        job = store.add(_job(tmp_path, name="p", priority=50))
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await _open(pilot)
            await pilot.press("a")
            await settle(pilot)
            assert store.get(job.id).auto is False
            await pilot.press("plus")
            await settle(pilot)
            assert store.get(job.id).priority == 40
            await pilot.press("minus")
            await settle(pilot)
            assert store.get(job.id).priority == 50

    async def test_cancel_confirms_then_cancels(self, fake, tmp_path):
        store = JobStore(tmp_path)
        job = store.add(_job(tmp_path, name="c"))
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await _open(pilot)
            await pilot.press("x")
            await pilot.pause()
            from claude_swap.tui.modals import ConfirmModal

            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("y")
            await settle(pilot)
            assert store.get(job.id).state == "cancelled"
            await pilot.press("r")
            await settle(pilot)
            assert store.get(job.id).state == "queued"

    async def test_delete_removes_job(self, fake, tmp_path):
        store = JobStore(tmp_path)
        job = store.add(_job(tmp_path, name="gone"))
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await _open(pilot)
            await pilot.press("d")
            await pilot.pause()
            await pilot.press("y")
            await settle(pilot)
            assert all(j.id != job.id for j in store.all())

    async def test_start_modal_launches_on_chosen_account(self, fake, tmp_path):
        store = JobStore(tmp_path)
        job = store.add(_job(tmp_path, name="go"))
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await _open(pilot)
            await pilot.press("s")
            await pilot.pause()
            from claude_swap.tui.jobs import StartJobModal

            assert isinstance(app.screen, StartJobModal)
            lv = app.screen.query_one("#start-accounts", ListView)
            assert len(lv.children) == 3  # auto + two accounts
            lv.index = 2  # account 2
            with patch("claude_swap.jobs.JobRunner.launch", return_value=job) as launch:
                await pilot.press("enter")
                await settle(pilot)
            launch.assert_called_once()
            assert launch.call_args.kwargs["account"] == "2"

    async def test_open_shows_log_screen(self, fake, tmp_path):
        store = JobStore(tmp_path)
        job = store.add(_job(tmp_path, name="log-me"))
        (store.log_dir(job.id)).mkdir(parents=True)
        (store.log_dir(job.id) / "stream.jsonl").write_text(
            '{"type":"assistant","message":{"content":[{"type":"text","text":"hello"}]}}\n'
        )
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await _open(pilot)
            await pilot.press("enter")
            await pilot.pause()
            from textual.widgets import RichLog

            from claude_swap.tui.jobs import JobLogScreen

            assert isinstance(app.screen, JobLogScreen)
            log = app.screen.query_one("#joblog-stream", RichLog)
            assert any("hello" in str(line) for line in log.lines)
            assert "do the thing" in str(app.screen.query_one("#joblog-prompt", Static).render())
            await pilot.press("escape")
            await pilot.pause()
            from claude_swap.tui.jobs import JobsScreen

            assert isinstance(app.screen, JobsScreen)

    async def test_go_live_needs_confirm(self, fake, tmp_path):
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await _open(pilot)
            engine_before = app.screen._engine
            await pilot.press("l")
            await pilot.pause()
            await pilot.press("n")  # decline
            await pilot.pause()
            assert app.screen._engine is engine_before and engine_before.dry_run
            await pilot.press("l")
            await pilot.pause()
            await pilot.press("y")
            await settle(pilot)
            assert app.screen._engine is not engine_before
            assert app.screen._engine.dry_run is False
            assert app.screen.query_one("#jobs-badge", Static).has_class("live")

    async def test_escape_returns_to_dashboard_and_stops_engine(self, fake, tmp_path):
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await _open(pilot)
            engine = app.screen._engine
            await pilot.press("escape")
            await pilot.pause()
            from claude_swap.tui.dashboard import DashboardScreen

            assert isinstance(app.screen, DashboardScreen)
            assert engine._stop.is_set()


@pytest.mark.asyncio
class TestJobForm:
    async def test_new_job_form_queues_with_selected_knobs(self, fake, tmp_path):
        work = tmp_path / "work"
        work.mkdir()
        app = make_app(fake)
        async with app.run_test(size=(120, 50)) as pilot:
            await _open(pilot)
            await pilot.press("n")
            await pilot.pause()
            from claude_swap.tui.jobform import JobFormModal

            assert isinstance(app.screen, JobFormModal)
            form = app.screen
            form.query_one("#f-folder", Input).value = str(work)
            form.query_one("#f-name", Input).value = "formed"
            form.query_one("#f-prompt", TextArea).text = "write tests\nthen run them"
            form.query_one("#f-account", Select).value = "2"
            form.query_one("#f-model", Select).value = "opus"
            form.query_one("#f-effort", Select).value = "high"
            form.query_one("#f-mode", Select).value = "bypassPermissions"
            form.query_one("#f-tools", Input).value = "Bash(git *) Edit"
            form.query_one("#f-turns", Input).value = "12"
            form.query_one("#f-priority", Input).value = "20"
            form.query_one("#f-estimate", Input).value = "7.5"
            form.query_one("#f-auto", Select).value = "manual"
            await pilot.pause()
            form.action_submit()
            await settle(pilot)
            jobs = JobStore(tmp_path).all()
            assert len(jobs) == 1
            j = jobs[0]
            assert j.name == "formed" and j.folder == str(work.resolve())
            assert j.prompt == "write tests\nthen run them"
            assert (j.account, j.model, j.effort, j.permission_mode) == ("2", "opus", "high", "bypassPermissions")
            assert j.allowed_tools == ("Bash(git *)", "Edit")
            assert (j.max_turns, j.priority, j.estimate_pct, j.auto) == (12, 20, 7.5, False)

    async def test_form_validation_blocks_missing_folder(self, fake, tmp_path):
        app = make_app(fake)
        async with app.run_test(size=(120, 50)) as pilot:
            await _open(pilot)
            await pilot.press("n")
            await pilot.pause()
            form = app.screen
            form.query_one("#f-folder", Input).value = str(tmp_path / "nope")
            form.query_one("#f-prompt", TextArea).text = "x"
            form.action_submit()
            await pilot.pause()
            from claude_swap.tui.jobform import JobFormModal

            assert isinstance(app.screen, JobFormModal)
            assert "Folder" in str(form.query_one("#form-error", Static).render())
            assert JobStore(tmp_path).all() == []

    async def test_custom_model_field_appears_and_is_used(self, fake, tmp_path):
        app = make_app(fake)
        async with app.run_test(size=(120, 50)) as pilot:
            await _open(pilot)
            await pilot.press("n")
            await pilot.pause()
            form = app.screen
            custom = form.query_one("#f-model-custom", Input)
            assert custom.display is False
            form.query_one("#f-model", Select).value = "__custom__"
            await pilot.pause()
            assert custom.display is True
            form.query_one("#f-folder", Input).value = str(tmp_path)
            form.query_one("#f-prompt", TextArea).text = "x"
            custom.value = "claude-opus-5"
            form.action_submit()
            await settle(pilot)
            assert JobStore(tmp_path).all()[0].model == "claude-opus-5"

    async def test_edit_prefills_and_updates(self, fake, tmp_path):
        store = JobStore(tmp_path)
        job = store.add(_job(tmp_path, name="orig", model="sonnet", effort="low", priority=30))
        app = make_app(fake)
        async with app.run_test(size=(120, 50)) as pilot:
            await _open(pilot)
            await pilot.press("e")
            await pilot.pause()
            form = app.screen
            assert form.query_one("#f-name", Input).value == "orig"
            assert form.query_one("#f-model", Select).value == "sonnet"
            assert form.query_one("#f-effort", Select).value == "low"
            form.query_one("#f-name", Input).value = "renamed"
            form.query_one("#f-effort", Select).value = "max"
            form.action_submit()
            await settle(pilot)
            got = store.get(job.id)
            assert got.name == "renamed" and got.effort == "max" and got.priority == 30
            assert len(store.all()) == 1

    async def test_escape_cancels_form(self, fake, tmp_path):
        app = make_app(fake)
        async with app.run_test(size=(120, 50)) as pilot:
            await _open(pilot)
            await pilot.press("n")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            from claude_swap.tui.jobs import JobsScreen

            assert isinstance(app.screen, JobsScreen)
            assert JobStore(tmp_path).all() == []
