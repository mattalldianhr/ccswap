"""Reserves screen: date-scheduled safety thresholds per usage window.

Two views of ``reserves.json``: a list (state, window, pct, from, until,
account, note) and a 14-day timeline, one row per window, drawn at 6-hour
resolution so overlaps and gaps are visible at a glance. The form accepts
absolute dates and the shorthand in :func:`~claude_swap.reserves.parse_when_relative`
(``now``, ``+3d``, ``fri 18:00``, ``next reset``) and shows the resolved
range before you commit.

Window choices are 5h, 7d, and every scoped window name the usage store
currently reports for a Claude account, so a new per-model window appears
here without a code change. ``next reset`` resolves against the earliest
known reset of the chosen window across accounts.
"""

from __future__ import annotations

import time
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING

from rich.text import Text
from textual.css.query import NoMatches
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Input, Label, ListItem, ListView, Select, Static

from claude_swap.reserves import (
    WINDOW_5H,
    WINDOW_7D,
    Reserve,
    ReserveError,
    ReserveStore,
    format_when,
    make_reserve,
    normalize_window,
    parse_when_relative,
)
from claude_swap.settings import JobsSettings, load_jobs_settings
from claude_swap.tui.modals import ConfirmModal
from claude_swap.tui.theme import Palette
from claude_swap.tui.widgets import CyclingListView

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp

TIMELINE_DAYS = 14
TIMELINE_STEP_S = 6 * 3600.0
_CELLS = int(TIMELINE_DAYS * 86400 / TIMELINE_STEP_S)  # 56


def _reset_ts(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def known_windows(app: "CswapApp") -> tuple[list[str], dict[str, float]]:
    """(window names, earliest next reset per window) from the live snapshot."""
    names = [WINDOW_5H, WINDOW_7D]
    resets: dict[str, float] = {}
    snap = app.snapshots.get("claude")
    for acc in (snap.accounts if snap else ()):
        lg = acc.usage.last_good
        if not isinstance(lg, dict):
            continue
        for key, name in (("five_hour", WINDOW_5H), ("seven_day", WINDOW_7D)):
            ts = _reset_ts((lg.get(key) or {}).get("resets_at")) if isinstance(lg.get(key), dict) else None
            if ts is not None:
                resets[name] = min(resets.get(name, ts), ts)
        for win in lg.get("scoped") or []:
            if isinstance(win, dict) and isinstance(win.get("name"), str):
                if win["name"] not in names:
                    names.append(win["name"])
                ts = _reset_ts(win.get("resets_at"))
                if ts is not None:
                    resets[win["name"]] = min(resets.get(win["name"], ts), ts)
    return names, resets


def reserve_row_text(r: Reserve, now: float, width: int, *, palette: Palette) -> Text:
    state = "active" if r.active_at(now) else ("expired" if r.expired_at(now) else "pending")
    style = {"active": palette.accent, "pending": palette.foreground, "expired": palette.muted}[state]
    text = Text()
    text.append(f"{state:<8}", style=style)
    text.append(f" {r.short_id}  ", style=palette.muted)
    text.append(f"{r.window:<7}", style=palette.foreground if state != "expired" else palette.muted)
    keep = "blackout" if r.is_blackout else f"keep {r.pct:.0f}%"
    text.append(f" {keep:<10}", style=palette.sev_crit if r.is_blackout else style)
    text.append(f" {format_when(r.starts_at) if r.starts_at else '—':<17}", style=palette.muted)
    text.append(f" {format_when(r.ends_at) if r.ends_at else 'open':<17}", style=palette.muted)
    text.append(f" {(r.account or 'all'):<26}", style=palette.muted)
    if r.note:
        text.append(f" {r.note}"[: max(0, width - 95)], style=palette.foreground)
    return text


def timeline_text(
    reserves: list[Reserve],
    windows: list[str],
    resets: dict[str, float],
    *,
    now: float,
    settings: JobsSettings,
    palette: Palette,
) -> Text:
    """Fourteen days, one row per window, six-hour cells."""
    text = Text()
    label_w = 8
    text.append(" " * label_w)
    # Day labels: one per 4 cells.
    start = datetime.fromtimestamp(now).astimezone().replace(minute=0, second=0, microsecond=0)
    for day in range(TIMELINE_DAYS):
        stamp = (start.timestamp() + day * 86400)
        text.append(datetime.fromtimestamp(stamp).astimezone().strftime("%a")[:3].ljust(4), style=palette.muted)
    text.append("\n")
    for name in windows:
        floor = settings.reserve_pct if name == WINDOW_5H else settings.weekly_reserve_pct
        text.append(f" {name:<{label_w - 1}}", style=palette.foreground)
        reset_ts = resets.get(name)
        for cell in range(_CELLS):
            t0 = now + cell * TIMELINE_STEP_S
            t1 = t0 + TIMELINE_STEP_S
            best = floor
            for r in reserves:
                if not r.matches_window(name):
                    continue
                s = r.starts_at if r.starts_at is not None else -1.0
                e = r.ends_at if r.ends_at is not None else float("inf")
                if s < t1 and e > t0:
                    best = max(best, r.pct)
            if reset_ts is not None and t0 <= reset_ts < t1:
                text.append("│", style=palette.accent)
            elif best >= 100:
                text.append("█", style=palette.sev_crit)
            elif best > floor:
                text.append("▓", style=palette.sev_warn)
            else:
                text.append("░", style=palette.track)
        text.append("\n")
    text.append(" " * label_w)
    text.append("░ floor   ", style=palette.muted)
    text.append("▓", style=palette.sev_warn)
    text.append(" raised   ", style=palette.muted)
    text.append("█", style=palette.sev_crit)
    text.append(" blackout   ", style=palette.muted)
    text.append("│", style=palette.accent)
    text.append(" window reset", style=palette.muted)
    return text


class ReserveRow(ListItem):
    def __init__(self, reserve: Reserve) -> None:
        super().__init__(Static("", markup=False))
        self.reserve = reserve

    def paint(self, now: float, *, palette: Palette) -> None:
        width = (self.size.width or 100) - 2
        try:
            static = self.query_one(Static)
        except NoMatches:
            return  # row is being removed
        static.update(reserve_row_text(self.reserve, now, width, palette=palette))


class ReservesScreen(Screen):
    BINDINGS = [
        Binding("n", "new_reserve", "New"),
        Binding("e", "edit_reserve", "Edit"),
        Binding("d", "delete_reserve", "Delete"),
        Binding("p", "purge_expired", "Purge expired"),
        Binding("escape,q", "back", "Back"),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    app: "CswapApp"

    def __init__(self) -> None:
        super().__init__()
        self._store: ReserveStore | None = None
        self._settings = JobsSettings()
        self._reserves: list[Reserve] = []
        self._selected_id: str | None = None

    def compose(self) -> ComposeResult:
        yield Static("", id="reserves-title")
        yield CyclingListView(id="reserves-list")
        yield Static("", id="reserves-timeline")
        yield Footer()

    def on_mount(self) -> None:
        switcher = self.app.switcher_for("claude")
        self._store = ReserveStore(switcher.backup_dir)
        self._settings = load_jobs_settings(switcher.backup_dir)
        self.query_one("#reserves-list", ListView).focus()
        self.watch(self.app, "theme", lambda _t: self.reload(), init=False)
        self.watch(self.app, "snapshots", lambda _s: self._paint_timeline(), init=False)
        self.reload()
        self.set_interval(30.0, self.reload)

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_cursor_down(self) -> None:
        self.query_one("#reserves-list", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#reserves-list", ListView).action_cursor_up()

    # -- render -----------------------------------------------------------------

    def reload(self) -> None:
        if self._store is None:
            return
        self._reserves = self._store.all()
        palette = Palette.from_theme(self.app.current_theme)
        now = time.time()
        title = Text()
        title.append("reserves", style=palette.foreground)
        title.append(
            f"   floor: 5h {self._settings.reserve_pct:.0f}% · 7d {self._settings.weekly_reserve_pct:.0f}%"
            f" · scoped {self._settings.weekly_reserve_pct:.0f}%",
            style=palette.muted,
        )
        title.append(f"   now {datetime.fromtimestamp(now).astimezone().strftime('%a %H:%M')}", style=palette.muted)
        self.query_one("#reserves-title", Static).update(title)

        listview = self.query_one("#reserves-list", ListView)
        rows = list(listview.query(ReserveRow))
        if [r.reserve.id for r in rows] == [r.id for r in self._reserves]:
            for row, r in zip(rows, self._reserves):
                row.reserve = r
                row.paint(now, palette=palette)
        else:
            listview.clear()
            for r in self._reserves:
                listview.append(ReserveRow(r))
            self.call_after_refresh(self._paint_rows)
            if self._reserves:
                index = next((i for i, r in enumerate(self._reserves) if r.id == self._selected_id), 0)
                listview.index = index
                self._selected_id = self._reserves[index].id
            else:
                self._selected_id = None
        self._paint_timeline()

    def _paint_rows(self) -> None:
        palette = Palette.from_theme(self.app.current_theme)
        now = time.time()
        for row in self.query("#reserves-list ReserveRow"):
            row.paint(now, palette=palette)

    def _paint_timeline(self) -> None:
        windows, resets = known_windows(self.app)
        self.query_one("#reserves-timeline", Static).update(
            timeline_text(
                self._reserves, windows, resets, now=time.time(),
                settings=self._settings, palette=Palette.from_theme(self.app.current_theme),
            )
        )

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if isinstance(event.item, ReserveRow):
            self._selected_id = event.item.reserve.id

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, ReserveRow):
            self.action_edit_reserve()

    def _selected(self) -> Reserve | None:
        return next((r for r in self._reserves if r.id == self._selected_id), None)

    # -- actions ----------------------------------------------------------------

    def action_new_reserve(self) -> None:
        windows, resets = known_windows(self.app)
        self.app.push_screen(
            ReserveFormModal(None, windows=windows, resets=resets, accounts=self._account_choices()),
            self._on_form,
        )

    def action_edit_reserve(self) -> None:
        r = self._selected()
        if r is None:
            return
        windows, resets = known_windows(self.app)
        if r.window not in windows:
            windows.append(r.window)
        self.app.push_screen(
            ReserveFormModal(r, windows=windows, resets=resets, accounts=self._account_choices()),
            self._on_form,
        )

    def _account_choices(self) -> list[tuple[str, str]]:
        snap = self.app.snapshots.get("claude")
        out = [("all accounts", "")]
        for acc in (snap.accounts if snap else ()):
            out.append((f"#{acc.number} {acc.alias or acc.email}", acc.email))
        return out

    def _on_form(self, result: Reserve | None) -> None:
        if result is None or self._store is None:
            return
        existing = {r.id for r in self._reserves}
        if result.id in existing:
            self._store.update(
                result.id, window=result.window, pct=result.pct, starts_at=result.starts_at,
                ends_at=result.ends_at, account=result.account, note=result.note,
            )
            self.notify(f"Updated reserve {result.short_id}")
        else:
            self._store.add(result)
            self.notify(f"Added reserve: {result.window} keep {result.pct:.0f}%")
        self._selected_id = result.id
        self.reload()

    def action_delete_reserve(self) -> None:
        r = self._selected()
        if r is None:
            return
        self.app.push_screen(
            ConfirmModal(f"Delete the {r.window} reserve ({r.note or r.short_id})?", title="Delete reserve", yes_label="Delete"),
            partial(self._on_delete, r),
        )

    def _on_delete(self, r: Reserve, confirmed: bool | None) -> None:
        if confirmed and self._store is not None:
            self._store.remove(r.id)
            self.reload()

    def action_purge_expired(self) -> None:
        if self._store is None:
            return
        n = self._store.purge_expired(time.time())
        self.notify(f"Purged {n} expired reserve(s)")
        self.reload()


# -- form ------------------------------------------------------------------------


class ReserveFormModal(ModalScreen["Reserve | None"]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+s", "submit", "Save", show=False),
    ]

    def __init__(
        self,
        reserve: Reserve | None,
        *,
        windows: list[str],
        resets: dict[str, float],
        accounts: list[tuple[str, str]],
    ) -> None:
        super().__init__()
        self._reserve = reserve
        self._windows = windows
        self._resets = resets
        self._accounts = accounts

    def compose(self) -> ComposeResult:
        r = self._reserve
        account_values = {v for _l, v in self._accounts}
        if r and r.account and r.account not in account_values:
            self._accounts = [*self._accounts, (r.account, r.account)]
        with Vertical(classes="modal-box modal-box-wide", id="reserveform-box"):
            yield Label("Edit reserve" if r else "New reserve", classes="modal-title")
            with Horizontal(classes="form-row"):
                with Vertical(classes="form-col"):
                    yield Label("window", classes="form-label")
                    yield Select([(w, w) for w in self._windows], value=(r.window if r else WINDOW_7D), allow_blank=False, id="r-window")
                with Vertical(classes="form-col"):
                    yield Label("keep free, % (100 = blackout)", classes="form-label")
                    yield Input(f"{r.pct:g}" if r else "40", id="r-pct", type="number")
            with Horizontal(classes="form-row"):
                with Vertical(classes="form-col"):
                    yield Label("from   (now · +1d · fri 09:00 · 2026-09-14 09:00)", classes="form-label")
                    yield Input(format_when(r.starts_at) if r and r.starts_at else "now", id="r-from")
                with Vertical(classes="form-col"):
                    yield Label("until   (open · +3d · next reset · 2026-09-16 20:00)", classes="form-label")
                    yield Input(format_when(r.ends_at) if r and r.ends_at else "next reset", id="r-until")
            with Horizontal(classes="form-row"):
                with Vertical(classes="form-col"):
                    yield Label("account", classes="form-label")
                    yield Select(self._accounts, value=(r.account or "") if r else "", allow_blank=False, id="r-account")
                with Vertical(classes="form-col"):
                    yield Label("note", classes="form-label")
                    yield Input(r.note if r else "", id="r-note", placeholder="why this reserve exists")
            yield Static("", id="r-resolves", classes="form-help")
            yield Static("", id="form-error", classes="form-error")
            with Horizontal(classes="modal-buttons"):
                yield Button("Save" if r else "Add", id="submit")
                yield Button("Cancel", id="cancel")
            yield Static("tab next · ctrl-s save · esc cancel", classes="modal-hint")

    def on_mount(self) -> None:
        self._preview()
        self.query_one("#r-pct", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._preview()

    def on_select_changed(self, event: Select.Changed) -> None:
        self._preview()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        else:
            self.action_submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_submit()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _build(self) -> Reserve:
        window = normalize_window(str(self.query_one("#r-window", Select).value))
        pct_raw = self.query_one("#r-pct", Input).value.strip()
        try:
            pct = float(pct_raw)
        except ValueError:
            raise ReserveError("Percent must be a number") from None
        now = time.time()
        next_reset = self._resets.get(window)
        for name, ts in self._resets.items():
            if name.lower() == window.lower():
                next_reset = ts
        starts = parse_when_relative(self.query_one("#r-from", Input).value, now=now, next_reset=next_reset)
        ends = parse_when_relative(self.query_one("#r-until", Input).value, now=now, next_reset=next_reset)
        if starts is not None and ends is not None and ends <= starts:
            raise ReserveError("End must be after start")
        if not 0.0 <= pct <= 100.0:
            raise ReserveError("Percent must be between 0 and 100")
        account = str(self.query_one("#r-account", Select).value) or None
        note = self.query_one("#r-note", Input).value.strip()
        return Reserve(
            id=self._reserve.id if self._reserve else make_reserve(window=window, pct=pct).id,
            window=window, pct=pct, starts_at=starts, ends_at=ends, account=account, note=note,
        )

    def _preview(self) -> None:
        palette = Palette.from_theme(self.app.current_theme)
        try:
            r = self._build()
        except ReserveError as e:
            self.query_one("#r-resolves", Static).update(Text(str(e), style=palette.sev_warn))
            return
        what = "blackout" if r.is_blackout else f"keep {r.pct:g}% free"
        who = f" · {r.account}" if r.account else ""
        self.query_one("#r-resolves", Static).update(
            Text(f"resolves to: {r.window} {what}{who} · {format_when(r.starts_at)} → {format_when(r.ends_at)}",
                 style=palette.muted)
        )

    def action_submit(self) -> None:
        try:
            r = self._build()
        except ReserveError as e:
            self.query_one("#form-error", Static).update(str(e))
            return
        self.dismiss(r)
