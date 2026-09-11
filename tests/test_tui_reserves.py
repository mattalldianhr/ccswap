"""Pilot tests for the Reserves screen and form, plus the relative date parser."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest
from textual.widgets import Input, ListView, Select, Static

from claude_swap.reserves import Reserve, ReserveError, ReserveStore, parse_when_relative
from tests.test_tui import make_account, make_app, make_entry, settle
from tests.test_tui_jobs import JobsFakeSwitcher


class TestRelativeParser:
    def test_keywords(self):
        now = 1_800_000_000.0
        assert parse_when_relative("now", now=now) == now
        assert parse_when_relative("", now=now) is None
        assert parse_when_relative("open", now=now) is None
        assert parse_when_relative("next reset", now=now, next_reset=now + 5) == now + 5
        with pytest.raises(ReserveError):
            parse_when_relative("next reset", now=now)

    def test_offsets(self):
        now = 1_800_000_000.0
        assert parse_when_relative("+3d", now=now) == now + 3 * 86400
        assert parse_when_relative("+6h", now=now) == now + 6 * 3600
        assert parse_when_relative("+30m", now=now) == now + 1800
        assert parse_when_relative("+1w", now=now) == now + 7 * 86400
        with pytest.raises(ReserveError):
            parse_when_relative("+xd", now=now)

    def test_weekday_rolls_forward(self):
        base = datetime(2026, 9, 10, 12, 0).astimezone()  # Thursday noon
        now = base.timestamp()
        fri = parse_when_relative("fri 18:00", now=now)
        assert datetime.fromtimestamp(fri).astimezone().strftime("%a %H:%M") == "Fri 18:00"
        assert 0 < fri - now < 2 * 86400
        thu = parse_when_relative("thu 09:00", now=now)  # earlier today → next week
        assert 6 * 86400 < thu - now <= 7 * 86400
        with pytest.raises(ReserveError):
            parse_when_relative("fri 18h", now=now)

    def test_absolute_fallback(self):
        now = 0.0
        assert parse_when_relative("2026-09-16 20:00", now=now) > 1_700_000_000


@pytest.fixture
def fake(tmp_path):
    return JobsFakeSwitcher(
        [
            make_account(1, active=True, entry=make_entry(10, 10, scoped=[("Fable", 40.0)])),
            make_account(2, entry=make_entry(60, 30)),
        ],
        tmp_path,
    )


async def _open(pilot):
    await settle(pilot)
    from tests.test_tui import menu_select

    await menu_select(pilot, "reserves")
    await settle(pilot)
    await pilot.pause()


def _rows(app):
    from claude_swap.tui.reserves import ReserveRow

    return list(app.screen.query(ReserveRow))


@pytest.mark.asyncio
class TestReservesScreen:
    async def test_lists_reserves_and_timeline(self, fake, tmp_path):
        store = ReserveStore(tmp_path)
        now = time.time()
        store.add(Reserve(id="aaaaaaaaaaaa", window="7d", pct=40, ends_at=now + 3 * 86400, note="deadline"))
        store.add(Reserve(id="bbbbbbbbbbbb", window="5h", pct=100, starts_at=now + 86400, ends_at=now + 86400 + 4 * 3600))
        store.add(Reserve(id="cccccccccccc", window="Fable", pct=60, ends_at=now - 10))
        app = make_app(fake)
        async with app.run_test(size=(130, 40)) as pilot:
            await _open(pilot)
            from claude_swap.tui.reserves import ReservesScreen

            assert isinstance(app.screen, ReservesScreen)
            rows = _rows(app)
            assert [r.reserve.id[:1] for r in rows] == ["c", "b", "a"]  # soonest-ending first
            app.screen._paint_rows()
            painted = [str(r.query_one(Static).render()) for r in rows]
            assert "expired" in painted[0] and "pending" in painted[1] and "active" in painted[2]
            assert "blackout" in painted[1] and "deadline" in painted[2]
            tl = str(app.screen.query_one("#reserves-timeline", Static).render())
            assert "5h" in tl and "7d" in tl and "Fable" in tl
            assert "▓" in tl and "█" in tl and "│" in tl

    async def test_new_reserve_via_form(self, fake, tmp_path):
        app = make_app(fake)
        async with app.run_test(size=(130, 40)) as pilot:
            await _open(pilot)
            await pilot.press("n")
            await pilot.pause()
            from claude_swap.tui.reserves import ReserveFormModal

            assert isinstance(app.screen, ReserveFormModal)
            form = app.screen
            assert [v for _l, v in form.query_one("#r-window", Select)._options] and True
            form.query_one("#r-window", Select).value = "Fable"
            form.query_one("#r-pct", Input).value = "55"
            form.query_one("#r-from", Input).value = "now"
            form.query_one("#r-until", Input).value = "+2d"
            form.query_one("#r-account", Select).value = "user2@example.com"
            form.query_one("#r-note", Input).value = "pitch"
            await pilot.pause()
            preview = str(form.query_one("#r-resolves", Static).render())
            assert "Fable keep 55% free" in preview and "user2@example.com" in preview
            form.action_submit()
            await settle(pilot)
            rows = ReserveStore(tmp_path).all()
            assert len(rows) == 1
            r = rows[0]
            assert (r.window, r.pct, r.account, r.note) == ("Fable", 55.0, "user2@example.com", "pitch")
            assert r.starts_at is not None and r.ends_at is not None
            assert abs((r.ends_at - r.starts_at) - 2 * 86400) < 5

    async def test_form_next_reset_uses_window_reset(self, fake, tmp_path):
        app = make_app(fake)
        async with app.run_test(size=(130, 40)) as pilot:
            await _open(pilot)
            await pilot.press("n")
            await pilot.pause()
            form = app.screen
            form.query_one("#r-window", Select).value = "7d"
            form.query_one("#r-until", Input).value = "next reset"
            form.action_submit()
            await settle(pilot)
            r = ReserveStore(tmp_path).all()[0]
            # make_entry's 7d reset is ~3 days out
            assert 2.5 * 86400 < r.ends_at - time.time() < 3.5 * 86400

    async def test_form_rejects_bad_range(self, fake, tmp_path):
        app = make_app(fake)
        async with app.run_test(size=(130, 40)) as pilot:
            await _open(pilot)
            await pilot.press("n")
            await pilot.pause()
            form = app.screen
            form.query_one("#r-from", Input).value = "+3d"
            form.query_one("#r-until", Input).value = "+1d"
            form.action_submit()
            await pilot.pause()
            from claude_swap.tui.reserves import ReserveFormModal

            assert isinstance(app.screen, ReserveFormModal)
            assert "after start" in str(form.query_one("#form-error", Static).render())
            assert ReserveStore(tmp_path).all() == []

    async def test_edit_and_delete(self, fake, tmp_path):
        store = ReserveStore(tmp_path)
        r = store.add(Reserve(id="dddddddddddd", window="7d", pct=40, note="old"))
        app = make_app(fake)
        async with app.run_test(size=(130, 40)) as pilot:
            await _open(pilot)
            await pilot.press("e")
            await pilot.pause()
            form = app.screen
            assert form.query_one("#r-pct", Input).value == "40"
            form.query_one("#r-pct", Input).value = "70"
            form.query_one("#r-note", Input).value = "new"
            form.action_submit()
            await settle(pilot)
            got = store.get(r.id)
            assert got.pct == 70.0 and got.note == "new" and len(store.all()) == 1
            await pilot.press("d")
            await pilot.pause()
            await pilot.press("y")
            await settle(pilot)
            assert store.all() == []

    async def test_purge_expired(self, fake, tmp_path):
        store = ReserveStore(tmp_path)
        store.add(Reserve(id="eeeeeeeeeeee", window="7d", pct=40, ends_at=time.time() - 5))
        store.add(Reserve(id="ffffffffffff", window="7d", pct=40))
        app = make_app(fake)
        async with app.run_test(size=(130, 40)) as pilot:
            await _open(pilot)
            await pilot.press("p")
            await settle(pilot)
            assert [r.id[:1] for r in store.all()] == ["f"]

    async def test_reachable_from_jobs_screen_with_R(self, fake, tmp_path):
        app = make_app(fake)
        async with app.run_test(size=(130, 40)) as pilot:
            await settle(pilot)
            await pilot.press("b")
            await settle(pilot)
            await pilot.press("R")
            await pilot.pause()
            from claude_swap.tui.reserves import ReservesScreen

            assert isinstance(app.screen, ReservesScreen)
            await pilot.press("escape")
            await pilot.pause()
            from claude_swap.tui.jobs import JobsScreen

            assert isinstance(app.screen, JobsScreen)
