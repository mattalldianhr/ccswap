"""Jobs screen: the queue, a detail pane, and the scheduler watching over it.

Layout (top to bottom): a header with the scheduler badge and the quiet /
next-tick summary; a capacity strip (one line per account: used, forecast,
reserve, spare for 5h and 7d and any scoped window); the queue as a
``ListView`` of one-line :class:`JobRow` items; a detail pane that follows
the selection — a queued job shows why it waits, a running job tails its
stream, a finished job shows its result and measured cost.

Like :class:`~claude_swap.tui.autoview.AutoScreen`, opening this screen
starts a :class:`~claude_swap.jobs_engine.JobsEngine` in a thread in
**dry-run**: it evaluates and logs, never launches. Going live is an
explicit, confirmed action. Manual ``s`` starts bypass the engine entirely
(and its quiet gate and reserves) through :meth:`JobRunner.launch`.

Store reads (jobs.json, the stream tail, capacity) run on a thread worker
every ``REFRESH_S`` and are applied on the UI thread; nothing here blocks
the event loop.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text
from textual.css.query import NoMatches
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, ListItem, ListView, RichLog, Static

from claude_swap.capacity import AccountCapacity, windows_for_job
from claude_swap.jobs import Job, JobError, JobRunner, JobStore, tail_stream_text
from claude_swap.jobs_engine import JobsEngine, JobsEvent
from claude_swap.settings import JobsSettings, load_jobs_settings
from claude_swap.tui import data
from claude_swap.tui.modals import ConfirmModal
from claude_swap.tui.theme import Palette
from claude_swap.tui.widgets import CyclingListView

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp

REFRESH_S = 2.0
CAPACITY_EVERY = 5  # refresh ticks between capacity recomputes
HISTORY_ROWS = 20  # finished rows shown unless history is toggled on
_EVENT_KEEP = 40

_GLYPH = {
    "running": "▶",
    "queued": "○",
    "paused": "‖",
    "done": "✓",
    "failed": "✗",
    "cancelled": "–",
}
_EVENT_ROLES = {"start": "accent", "error": "sev_crit", "finished": "foreground"}


@dataclass(frozen=True)
class QueueSnapshot:
    """One refresh pass, taken off the UI thread."""

    jobs: tuple[Job, ...]
    tail: dict[str, tuple[str, ...]]  # job id → stream tail (running jobs only)
    capacities: tuple[AccountCapacity, ...] | None  # None = unchanged this pass
    capacity_error: str | None
    idle_s: float | None
    taken_at: float


def _short_folder(folder: str) -> str:
    home = str(Path.home())
    if folder.startswith(home):
        folder = "~" + folder[len(home):]
    return folder


def _fit(text: str, width: int) -> str:
    if len(text) <= width:
        return text.ljust(width)
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"


def _pct(v: float | None, *, signed: bool = False) -> str:
    if v is None:
        return "  ?"
    return f"{v:+.0f}%" if signed else f"{v:.0f}%"


def event_text(event: JobsEvent, *, palette: Palette) -> Text:
    role = _EVENT_ROLES.get(event.kind)
    style = getattr(palette, role) if role else palette.muted
    text = Text()
    text.append(f"{data.clock_stamp()}  ", style=palette.muted)
    text.append(event.human(), style=style)
    return text


def job_row_text(job: Job, width: int, *, palette: Palette, settings: JobsSettings) -> Text:
    """One queue row: glyph · priority · name · folder · account · knobs · est · last."""
    glyph = _GLYPH.get(job.state, "?")
    if job.state == "queued" and not job.auto:
        glyph = "◌"
    state_style = {
        "running": palette.accent,
        "queued": palette.foreground if job.auto else palette.muted,
        "paused": palette.muted,
        "done": palette.muted,
        "failed": palette.sev_crit,
        "cancelled": palette.muted,
    }.get(job.state, palette.foreground)
    dim = job.state in ("done", "cancelled")
    body = palette.muted if dim else palette.foreground

    knobs = f"{job.model or 'default'} · {job.effort or '—'} · {job.permission_mode}"
    acct = "auto" if job.account == "auto" else f"#{job.account}" if job.account.isdigit() else job.account
    # Column budget: fixed columns first, the folder gets what is left.
    fixed = 2 + 5 + 22 + 2 + 8 + 2 + 28 + 2 + 5 + 2
    last = _last_col(job)
    folder_w = max(10, width - fixed - len(last) - 2)

    text = Text()
    text.append(f"{glyph} ", style=state_style)
    text.append(f"{job.priority:>3}  ", style=palette.muted)
    text.append(_fit(job.name, 22), style=state_style if job.state == "running" else body)
    text.append("  ")
    text.append(_fit(_short_folder(job.folder), folder_w), style=palette.muted)
    text.append("  ")
    text.append(_fit(acct, 8), style=body)
    text.append("  ")
    text.append(_fit(knobs, 28), style=palette.muted)
    text.append("  ")
    text.append(f"{job.estimate_pct:>4.0f}%", style=body)
    text.append("  ")
    text.append(last, style=palette.sev_crit if job.state == "failed" else palette.muted)
    return text


def _last_col(job: Job) -> str:
    if job.state == "running":
        started = _parse_ts(job.started_at)
        elapsed = data.format_duration(time.time() - started) if started else "…"
        who = f" · {job.account_used}" if job.account_used else ""
        return f"{elapsed}{who}"
    if job.state == "queued":
        if not job.auto:
            return "manual only"
        now = time.time()
        if job.not_before and job.not_before > now:
            return f"held {data.format_duration(job.not_before - now)}"
        if job.over_budget(now):
            return "over weekly budget"
        return f"every {job.repeat_minutes:g}m" if job.repeat_minutes else "—"
    if job.state in ("done", "failed", "cancelled") and job.runs:
        r = job.runs[-1]
        cost = f"Δ{r.cost_5h:.0f}%" if r.cost_5h is not None else "Δ?"
        when = job.finished_at[11:16] if job.finished_at and len(job.finished_at) >= 16 else ""
        if job.state == "failed":
            return f"failed · {(job.error or '')[:28]}"
        return f"{job.state} {when} · {cost}".strip()
    return job.state


def _parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    from datetime import datetime, timezone

    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def capacity_text(
    caps: tuple[AccountCapacity, ...] | None,
    error: str | None,
    width: int,
    *,
    palette: Palette,
    settings: JobsSettings,
) -> Text:
    text = Text()
    if caps is None and error is None:
        text.append("capacity: computing…", style=palette.muted)
        return text
    if error:
        text.append(f"capacity unavailable: {error}", style=palette.muted)
        return text
    assert caps is not None
    if not caps:
        text.append("capacity: no accounts with usage", style=palette.muted)
        return text
    # Header
    names = ["5h", "7d"]
    for cap in caps:
        for name in cap.windows:
            if name not in names:
                names.append(name)
    text.append(" acct ", style=palette.muted)
    for name in names:
        text.append(f"  {name:<6} used  fcst  rsv  spare", style=palette.muted)
    for cap in caps:
        text.append("\n")
        text.append(f" #{cap.number:<4}", style=palette.foreground)
        for name in names:
            w = cap.window(name)
            if w is None:
                text.append(f"  {'':<6}    —     —    —      —", style=palette.muted)
                continue
            used = _pct(w.used_pct)
            spare = w.spare_pct
            spare_style = (
                palette.muted if spare is None
                else palette.sev_crit if w.blackout or spare < 0
                else palette.sev_warn if spare < settings.default_estimate_pct
                else palette.sev_ok
            )
            text.append(f"  {'':<6}", style=palette.muted)
            text.append(f"{used:>5}", style=palette.severity(w.used_pct))
            text.append(f" {_pct(w.forecast_pct):>5}", style=palette.muted)
            text.append(f" {_pct(w.reserve_pct):>4}{'!' if w.blackout else ' '}", style=palette.muted)
            text.append(f"{_pct(spare, signed=True) if spare is not None else '   ?':>6}", style=spare_style)
        if cap.usage_error:
            text.append(f"  {cap.usage_error}", style=palette.sev_warn)
    return text


class JobRow(ListItem):
    def __init__(self, job: Job) -> None:
        super().__init__(Static("", markup=False))
        self.job = job

    def set_job(self, job: Job, *, palette: Palette, settings: JobsSettings) -> None:
        self.job = job
        width = (self.size.width or 100) - 2
        try:
            static = self.query_one(Static)
        except NoMatches:
            return  # row is being removed
        static.update(job_row_text(job, width, palette=palette, settings=settings))


class JobsScreen(Screen):
    BINDINGS = [
        Binding("n", "new_job", "New"),
        Binding("e", "edit_job", "Edit"),
        Binding("enter", "open_job", "Open"),
        Binding("s", "start_job", "Start"),
        Binding("x", "cancel_job", "Cancel"),
        Binding("r", "retry_job", "Retry"),
        Binding("d", "delete_job", "Delete"),
        Binding("a", "toggle_auto", "Auto/manual"),
        Binding("plus,equals_sign", "priority_step(-10)", "Sooner", show=False),
        Binding("minus", "priority_step(10)", "Later", show=False),
        Binding("h", "toggle_history", "History", show=False),
        Binding("l", "toggle_live", "Go live / dry-run"),
        Binding("c", "app.open_capacity", "Capacity"),
        Binding("R", "app.open_reserves", "Reserves"),
        Binding("escape,q", "back", "Back"),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    app: "CswapApp"

    def __init__(self) -> None:
        super().__init__()
        self._settings: JobsSettings = JobsSettings()
        self._store: JobStore | None = None
        self._runner: JobRunner | None = None
        self._engine: JobsEngine | None = None
        self._events: deque[Text] = deque(maxlen=_EVENT_KEEP)
        self._last_event: JobsEvent | None = None
        self._snapshot: QueueSnapshot | None = None
        self._caps: tuple[AccountCapacity, ...] | None = None
        self._cap_error: str | None = None
        self._tick = 0
        self._refreshing = False
        self._show_history = False
        self._selected_id: str | None = None

    # -- compose / lifecycle --------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="jobs-top"):
            with Horizontal(id="jobs-title-row"):
                yield Static(" DRY-RUN ", id="jobs-badge", classes="dry")
                yield Static("", id="jobs-summary")
            yield Static("", id="jobs-capacity")
        yield CyclingListView(id="jobs-list")
        yield Static("", id="jobs-detail")
        yield Footer()

    def on_mount(self) -> None:
        switcher = self.app.switcher_for("claude")
        self._settings = load_jobs_settings(switcher.backup_dir)
        self._store = JobStore(switcher.backup_dir)
        self._runner = JobRunner(switcher, self._settings, self._store)
        self.query_one("#jobs-list", ListView).focus()
        self.watch(self.app, "theme", self._on_theme_change, init=False)
        self._start_engine(dry_run=True)
        self.set_interval(REFRESH_S, self._refresh)
        self._refresh()

    def on_unmount(self) -> None:
        if self._engine is not None:
            self._engine.stop()

    def _on_theme_change(self, _theme: str) -> None:
        self._apply(self._snapshot, force=True)

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_cursor_down(self) -> None:
        self.query_one("#jobs-list", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#jobs-list", ListView).action_cursor_up()

    # -- engine -----------------------------------------------------------------

    def _start_engine(self, *, dry_run: bool) -> None:
        switcher = self.app.switcher_for("claude")
        engine = JobsEngine(
            switcher, self._settings, self._emit_from_thread,
            store=self._store, runner=self._runner, dry_run=dry_run,
        )
        self._engine = engine
        self.run_worker(
            engine.run_loop, thread=True, group="engine", exit_on_error=False,
            name=f"jobs-engine-{'dry' if dry_run else 'live'}",
        )
        self._update_badge()
        palette = Palette.from_theme(self.app.current_theme)
        mode = "DRY-RUN (deciding only)" if dry_run else "LIVE (will start jobs)"
        self._events.append(Text(f"— scheduler started: {mode} —", style=palette.muted))

    def _emit_from_thread(self, event: JobsEvent) -> None:
        try:
            self.app.call_from_thread(self._on_engine_event, event)
        except Exception:
            pass

    def _on_engine_event(self, event: JobsEvent) -> None:
        if not self.is_attached:
            return
        self._last_event = event
        if event.kind == "tick":
            caps = getattr(event, "capacities", ())
            if caps:
                self._caps, self._cap_error = tuple(caps), None
        else:
            self._events.append(event_text(event, palette=Palette.from_theme(self.app.current_theme)))
        if event.kind in ("start", "finished"):
            self._refresh()
        self._update_summary()
        self._update_detail()

    def action_toggle_live(self) -> None:
        if self._engine is None:
            return
        if self._engine.dry_run:
            self.app.push_screen(
                ConfirmModal(
                    "Go live? This screen's scheduler will start queued jobs "
                    "when it finds spare capacity and the machine is quiet.\n\n"
                    "(Same behavior as `ccswap jobs auto`.)",
                    title="Go live",
                    yes_label="Go live",
                ),
                self._on_live_confirm,
            )
        else:
            self._restart_engine(dry_run=True)

    def _on_live_confirm(self, confirmed: bool | None) -> None:
        if confirmed:
            self._restart_engine(dry_run=False)

    def _restart_engine(self, *, dry_run: bool) -> None:
        if self._engine is not None:
            self._engine.stop()
        self._start_engine(dry_run=dry_run)

    def _update_badge(self) -> None:
        badge = self.query_one("#jobs-badge", Static)
        if self._engine is not None and not self._engine.dry_run:
            badge.update(" LIVE ")
            badge.set_classes("live")
        else:
            badge.update(" DRY-RUN ")
            badge.set_classes("dry")

    # -- refresh ----------------------------------------------------------------

    def _refresh(self) -> None:
        if self._refreshing or self._store is None:
            return
        self._refreshing = True
        self._tick += 1
        want_caps = self._tick % CAPACITY_EVERY == 1
        self.run_worker(
            partial(self._refresh_blocking, want_caps),
            thread=True, group="jobs-refresh", exit_on_error=False, name="jobs-refresh",
        )

    def _refresh_blocking(self, want_caps: bool) -> None:
        assert self._store is not None
        try:
            jobs = tuple(self._store.all())
            tail: dict[str, tuple[str, ...]] = {}
            for job in jobs:
                if job.state == "running":
                    tail[job.id] = tuple(
                        tail_stream_text(self._store.log_dir(job.id) / "stream.jsonl", max_lines=12)
                    )
            caps: tuple[AccountCapacity, ...] | None = None
            cap_error: str | None = None
            idle_s: float | None = None
            if want_caps and self._engine is not None:
                try:
                    caps = tuple(self._engine.capacities())
                    idle_s = self._engine.idle().idle_s
                except Exception as e:  # noqa: BLE001 - shown, never fatal
                    cap_error = f"{type(e).__name__}: {e}"[:80]
            snap = QueueSnapshot(
                jobs=jobs, tail=tail, capacities=caps, capacity_error=cap_error,
                idle_s=idle_s, taken_at=time.time(),
            )
        except Exception as e:  # noqa: BLE001
            snap = QueueSnapshot(jobs=(), tail={}, capacities=None,
                                 capacity_error=f"{type(e).__name__}: {e}"[:80],
                                 idle_s=None, taken_at=time.time())
        try:
            self.app.call_from_thread(self._apply, snap)
        except Exception:
            pass

    def _apply(self, snap: QueueSnapshot | None, *, force: bool = False) -> None:
        self._refreshing = False
        if not self.is_attached or snap is None:
            return
        if snap.capacities is not None or snap.capacity_error is not None:
            self._caps, self._cap_error = snap.capacities, snap.capacity_error
        if snap.idle_s is not None or self._snapshot is None:
            self._idle_s = snap.idle_s
        self._snapshot = snap
        self._rebuild_list(snap, force=force)
        self._update_summary()
        self._update_capacity()
        self._update_detail()

    def _visible_jobs(self, snap: QueueSnapshot) -> list[Job]:
        active = [j for j in snap.jobs if j.is_active]
        finished = [j for j in snap.jobs if not j.is_active]
        if not self._show_history:
            finished = finished[:HISTORY_ROWS]
        return active + finished

    def _rebuild_list(self, snap: QueueSnapshot, *, force: bool) -> None:
        listview = self.query_one("#jobs-list", ListView)
        palette = Palette.from_theme(self.app.current_theme)
        jobs = self._visible_jobs(snap)
        rows = list(listview.query(JobRow))
        same_shape = not force and [r.job.id for r in rows] == [j.id for j in jobs]
        if same_shape:
            for row, job in zip(rows, jobs):
                row.set_job(job, palette=palette, settings=self._settings)
            return
        selected = self._selected_id
        listview.clear()
        for job in jobs:
            row = JobRow(job)
            listview.append(row)
        # Rows size after mount; paint them on the next frame.
        self.call_after_refresh(self._paint_rows)
        if jobs:
            index = next((i for i, j in enumerate(jobs) if j.id == selected), 0)
            listview.index = index
            self._selected_id = jobs[index].id
        else:
            self._selected_id = None

    def _paint_rows(self) -> None:
        palette = Palette.from_theme(self.app.current_theme)
        for row in self.query("#jobs-list JobRow"):
            row.set_job(row.job, palette=palette, settings=self._settings)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        item = event.item
        if isinstance(item, JobRow):
            self._selected_id = item.job.id
            self._update_detail()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, JobRow):
            self.action_open_job()

    def _selected(self) -> Job | None:
        if self._snapshot is None or self._selected_id is None:
            return None
        return next((j for j in self._snapshot.jobs if j.id == self._selected_id), None)

    # -- render ----------------------------------------------------------------

    def _update_summary(self) -> None:
        palette = Palette.from_theme(self.app.current_theme)
        snap = self._snapshot
        text = Text()
        text.append("jobs", style=palette.foreground)
        if snap is not None:
            queued = sum(1 for j in snap.jobs if j.state == "queued" and j.auto)
            manual = sum(1 for j in snap.jobs if j.state == "queued" and not j.auto)
            running = sum(1 for j in snap.jobs if j.state == "running")
            text.append(f" · {queued} queued", style=palette.muted)
            if manual:
                text.append(f" (+{manual} manual)", style=palette.muted)
            text.append(f" · {running} running", style=palette.accent if running else palette.muted)
        idle = getattr(self, "_idle_s", None)
        need = self._settings.quiet_minutes
        if idle is None:
            text.append(" · idle ?", style=palette.muted)
        elif idle == float("inf"):
            text.append(" · no interactive sessions", style=palette.sev_ok)
        else:
            quiet = idle >= need * 60
            text.append(
                f" · quiet {min(idle, need * 60) / 60:.0f}/{need:.0f}m",
                style=palette.sev_ok if quiet else palette.sev_warn,
            )
        if self._last_event is not None and self._last_event.kind in ("hold", "start", "error"):
            text.append(f" · {self._last_event.human()[:60]}", style=palette.muted)
        self.query_one("#jobs-summary", Static).update(text)

    def _update_capacity(self) -> None:
        width = (self.size.width or 100) - 4
        self.query_one("#jobs-capacity", Static).update(
            capacity_text(
                self._caps, self._cap_error, width,
                palette=Palette.from_theme(self.app.current_theme), settings=self._settings,
            )
        )

    def _update_detail(self) -> None:
        palette = Palette.from_theme(self.app.current_theme)
        job = self._selected()
        text = Text()
        if job is None:
            text.append("No jobs yet. Press n to queue one.\n\n", style=palette.muted)
            for line in list(self._events)[-8:]:
                text.append(line)
                text.append("\n")
            self.query_one("#jobs-detail", Static).update(text)
            return
        text.append(f"{job.name}", style=palette.accent)
        text.append(f"  {job.short_id}  ", style=palette.muted)
        text.append(job.state, style=palette.sev_crit if job.state == "failed" else palette.muted)
        text.append("\n")
        if job.state == "queued":
            text.append(self._wait_reason(job, palette))
        elif job.state == "running":
            tail = self._snapshot.tail.get(job.id, ()) if self._snapshot else ()
            if tail:
                for line in tail[-8:]:
                    text.append(f"  {line[:200]}\n", style=palette.foreground)
            else:
                text.append("  starting…\n", style=palette.muted)
        elif job.state in ("done", "failed", "cancelled"):
            if job.runs:
                r = job.runs[-1]
                cost5 = f"{r.cost_5h:.1f}%" if r.cost_5h is not None else "?"
                cost7 = f"{r.cost_7d:.1f}%" if r.cost_7d is not None else "?"
                dur = data.format_duration(r.duration_s) if r.duration_s is not None else "?"
                text.append(
                    f"  {r.account} · {dur} · exit {r.exit_code} · 5h Δ{cost5} · 7d Δ{cost7}"
                    + (f" · {r.num_turns} turns" if r.num_turns else "") + "\n",
                    style=palette.muted,
                )
            if job.error:
                text.append(f"  {job.error}\n", style=palette.sev_crit)
            if job.result_text:
                for line in job.result_text.strip().splitlines()[:5]:
                    text.append(f"  {line[:200]}\n", style=palette.foreground)
            if job.session_id:
                text.append(f"  claude --resume {job.session_id}\n", style=palette.muted)
        prompt_lines = job.prompt.strip().splitlines()
        text.append("  prompt: ", style=palette.muted)
        text.append(" ".join(prompt_lines)[:220], style=palette.muted)
        if self._events and job.state == "queued":
            text.append("\n")
            for line in list(self._events)[-3:]:
                text.append("\n")
                text.append(line)
        self.query_one("#jobs-detail", Static).update(text)

    def _wait_reason(self, job: Job, palette: Palette) -> Text:
        text = Text()
        if not job.auto:
            text.append("  manual only — press s to start\n", style=palette.muted)
            return text
        now = time.time()
        loop = []
        if job.repeat_minutes:
            loop.append(f"repeats every {job.repeat_minutes:g}m")
        if job.then_job:
            loop.append(f"then {job.then_job}")
        if job.weekly_budget_pct:
            loop.append(f"weekly budget {job.weekly_spent(now):.1f}/{job.weekly_budget_pct:g}%")
        if loop:
            text.append("  " + " · ".join(loop) + "\n", style=palette.muted)
        if job.not_before and job.not_before > now:
            text.append(f"  held until {data.format_duration(job.not_before - now)} from now (cooldown)\n", style=palette.sev_warn)
            return text
        if job.over_budget(now):
            text.append("  over its weekly budget — waits for older runs to age out\n", style=palette.sev_warn)
            return text
        caps = self._caps
        if not caps:
            text.append("  waiting: capacity unknown\n", style=palette.muted)
            return text
        need7 = job.weekly_estimate(self._settings.weekly_cost_ratio)
        text.append(f"  needs {job.estimate_pct:.0f}% on 5h and {need7:.1f}% on 7d", style=palette.muted)
        best = self._engine.choose(job, list(caps)) if self._engine else None
        if best is not None:
            text.append(f"  → fits on #{best[0].number} (spare {best[1]:.0f}%)\n", style=palette.sev_ok)
        else:
            text.append("\n")
            for cap in caps:
                windows = windows_for_job(job.model, cap.windows)
                blockers = []
                for name in windows:
                    w = cap.window(name)
                    need = job.estimate_pct if name == "5h" else need7
                    if w is None or w.spare_pct is None:
                        blockers.append(f"{name} unknown")
                    elif w.blackout:
                        blockers.append(f"{name} blackout")
                    elif w.spare_pct < need:
                        blockers.append(f"{name} spare {w.spare_pct:.0f}% < {need:.0f}%")
                text.append(f"  #{cap.number}: ", style=palette.muted)
                text.append(", ".join(blockers) if blockers else "usage stale", style=palette.sev_warn)
                text.append("\n")
        return text

    # -- actions ----------------------------------------------------------------

    def action_new_job(self) -> None:
        from claude_swap.tui.jobform import JobFormModal

        last_folder = self._selected().folder if self._selected() else str(Path.cwd())
        self.app.push_screen(
            JobFormModal(None, settings=self._settings, default_folder=last_folder,
                         accounts=self._account_choices()),
            self._on_job_form,
        )

    def action_edit_job(self) -> None:
        from claude_swap.tui.jobform import JobFormModal

        job = self._selected()
        if job is None:
            return
        if job.state not in ("queued", "paused"):
            self.notify("Only queued jobs can be edited; retry it first", severity="warning")
            return
        self.app.push_screen(
            JobFormModal(job, settings=self._settings, default_folder=job.folder,
                         accounts=self._account_choices()),
            self._on_job_form,
        )

    def _account_choices(self) -> list[tuple[str, str]]:
        snap = self.app.snapshots.get("claude")
        choices = [("auto (most spare at start)", "auto")]
        for acc in (snap.accounts if snap else ()):
            if acc.kind == "api_key":
                continue
            label = f"#{acc.number} {acc.alias or acc.email}"
            choices.append((label, acc.number))
        return choices

    def _on_job_form(self, result) -> None:
        if result is None or self._store is None:
            return
        try:
            if result.job_id is None:
                job = self._store.add(result.to_job())
                self.notify(f"Queued {job.name}")
            else:
                self._store.update(result.job_id, **result.fields())
                self.notify(f"Updated {result.name}")
        except JobError as e:
            self.notify(str(e), severity="error")
        self._refresh()

    def action_open_job(self) -> None:
        job = self._selected()
        if job is None or self._store is None:
            return
        self.app.push_screen(JobLogScreen(job.id, self._store))

    def action_start_job(self) -> None:
        job = self._selected()
        if job is None:
            return
        if job.state != "queued":
            self.notify(f"{job.name} is {job.state}; retry it first", severity="warning")
            return
        self.app.push_screen(
            StartJobModal(job, self._caps or (), self._account_choices(), self._settings, self._engine),
            partial(self._on_start_choice, job),
        )

    def _on_start_choice(self, job: Job, account: str | None) -> None:
        if account is None or self._runner is None:
            return
        pinned = None if account == "auto" else account
        if pinned is None and self._engine is not None and self._caps:
            choice = self._engine.choose(job, list(self._caps))
            if choice is not None:
                pinned = choice[0].number
        try:
            started = self._runner.launch(job, account=pinned)
        except JobError as e:
            self.notify(str(e), severity="error")
            return
        self.notify(f"Started {started.name}" + (f" on #{pinned}" if pinned else ""))
        self._refresh()

    def action_cancel_job(self) -> None:
        job = self._selected()
        if job is None:
            return
        if not job.is_active:
            self.notify(f"{job.name} is already {job.state}", severity="warning")
            return
        verb = "Stop the running job" if job.state == "running" else "Cancel the queued job"
        self.app.push_screen(
            ConfirmModal(f"{verb} {job.name}?", title="Cancel job", yes_label="Cancel job"),
            partial(self._on_cancel_confirm, job),
        )

    def _on_cancel_confirm(self, job: Job, confirmed: bool | None) -> None:
        if not confirmed or self._runner is None:
            return
        try:
            self._runner.cancel(job)
        except JobError as e:
            self.notify(str(e), severity="error")
        self._refresh()

    def action_retry_job(self) -> None:
        job = self._selected()
        if job is None or self._runner is None:
            return
        try:
            self._runner.requeue(job)
            self.notify(f"Queued {job.name}")
        except JobError as e:
            self.notify(str(e), severity="error")
        self._refresh()

    def action_delete_job(self) -> None:
        job = self._selected()
        if job is None:
            return
        if job.state == "running":
            self.notify("Cancel the running job first", severity="warning")
            return
        self.app.push_screen(
            ConfirmModal(f"Delete {job.name} and its logs?", title="Delete job", yes_label="Delete"),
            partial(self._on_delete_confirm, job),
        )

    def _on_delete_confirm(self, job: Job, confirmed: bool | None) -> None:
        if confirmed and self._store is not None:
            self._store.remove(job.id)
            self._refresh()

    def action_toggle_auto(self) -> None:
        job = self._selected()
        if job is None or self._store is None:
            return
        self._store.update(job.id, auto=not job.auto)
        self.notify(f"{job.name}: {'auto-start' if not job.auto else 'manual only'}")
        self._refresh()

    def action_priority_step(self, delta: int) -> None:
        job = self._selected()
        if job is None or self._store is None:
            return
        value = max(0, min(999, job.priority + delta))
        if value != job.priority:
            self._store.update(job.id, priority=value)
            self._refresh()

    def action_toggle_history(self) -> None:
        self._show_history = not self._show_history
        if self._snapshot is not None:
            self._rebuild_list(self._snapshot, force=True)


# -- start modal --------------------------------------------------------------


class StartJobModal(ModalScreen["str | None"]):
    """Pick the account for a manual start; shows each account's spare."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("enter", "choose", "Start", show=False),
    ]

    def __init__(
        self,
        job: Job,
        caps: tuple[AccountCapacity, ...],
        accounts: list[tuple[str, str]],
        settings: JobsSettings,
        engine: JobsEngine | None,
    ) -> None:
        super().__init__()
        self._job = job
        self._caps = caps
        self._accounts = accounts
        self._settings = settings
        self._engine = engine

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Static(f"Start {self._job.name} now", classes="modal-title")
            yield Static(
                "Manual starts skip the quiet gate and reserves. Pick the account:",
                classes="modal-body",
            )
            yield CyclingListView(id="start-accounts")
            yield Static("↑ ↓ choose · enter start · esc cancel", classes="modal-hint")

    def on_mount(self) -> None:
        palette = Palette.from_theme(self.app.current_theme)
        listview = self.query_one("#start-accounts", ListView)
        best = self._engine.choose(self._job, list(self._caps)) if self._engine and self._caps else None
        for label, value in self._accounts:
            text = Text(label, style=palette.foreground)
            if value == "auto":
                if best is not None:
                    text.append(f"  → #{best[0].number}, spare {best[1]:.0f}%", style=palette.sev_ok)
                else:
                    text.append("  → active login", style=palette.muted)
            else:
                cap = next((c for c in self._caps if c.number == value), None)
                spare = cap.spare_for(windows_for_job(self._job.model, cap.windows)) if cap else None
                if spare is None:
                    text.append("  spare ?", style=palette.muted)
                else:
                    ok = spare >= self._job.estimate_pct
                    text.append(f"  spare {spare:.0f}%", style=palette.sev_ok if ok else palette.sev_warn)
            item = ListItem(Static(text))
            item.value = value  # type: ignore[attr-defined]
            listview.append(item)
        listview.index = 0
        listview.focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.dismiss(getattr(event.item, "value", None))

    def action_choose(self) -> None:
        listview = self.query_one("#start-accounts", ListView)
        item = listview.highlighted_child
        self.dismiss(getattr(item, "value", None) if item else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# -- log screen -----------------------------------------------------------------


class JobLogScreen(Screen):
    """Full-screen view of one job: prompt, worker log, and the stream tail,
    refreshed while the job runs."""

    BINDINGS = [Binding("escape,q", "back", "Back")]

    def __init__(self, job_id: str, store: JobStore) -> None:
        super().__init__()
        self._job_id = job_id
        self._store = store
        self._lines_seen = 0

    def compose(self) -> ComposeResult:
        yield Static("", id="joblog-title")
        with VerticalScroll(id="joblog-prompt-wrap"):
            yield Static("", id="joblog-prompt")
        yield RichLog(id="joblog-stream", highlight=False, markup=False, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        self._load(full=True)
        self.set_interval(REFRESH_S, self._load)

    def action_back(self) -> None:
        self.app.pop_screen()

    def _load(self, full: bool = False) -> None:
        palette = Palette.from_theme(self.app.current_theme)
        try:
            job = self._store.get(self._job_id)
        except JobError:
            self.app.pop_screen()
            return
        title = Text()
        title.append(job.name, style=palette.accent)
        title.append(f"  {job.state}", style=palette.sev_crit if job.state == "failed" else palette.muted)
        title.append(f"  ·  {_short_folder(job.folder)}", style=palette.muted)
        title.append(
            f"  ·  {job.model or 'default'} · {job.effort or '—'} · {job.permission_mode}"
            + (f"  ·  on {job.account_used}" if job.account_used else ""),
            style=palette.muted,
        )
        self.query_one("#joblog-title", Static).update(title)
        if full:
            self.query_one("#joblog-prompt", Static).update(Text(job.prompt.strip(), style=palette.foreground))
        lines = tail_stream_text(self._store.log_dir(job.id) / "stream.jsonl", max_lines=400)
        log = self.query_one("#joblog-stream", RichLog)
        if full or len(lines) < self._lines_seen:
            log.clear()
            self._lines_seen = 0
            worker = self._store.log_dir(job.id) / "worker.log"
            if worker.exists():
                for wl in worker.read_text(encoding="utf-8", errors="replace").splitlines()[-6:]:
                    log.write(Text(wl, style=palette.muted))
        for line in lines[self._lines_seen:]:
            style = palette.muted if line.startswith("⚙") else palette.accent if line.startswith("■") else palette.foreground
            log.write(Text(line, style=style))
        self._lines_seen = len(lines)
        if job.error and job.state == "failed" and full:
            log.write(Text(f"error: {job.error}", style=palette.sev_crit))
