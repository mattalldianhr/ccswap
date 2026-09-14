"""Capacity screen: the numbers behind "spare", per account and per window.

For one account at a time (◀ ▶ to page): a table of every window with
used, left, the recent and typical forecasts, the effective reserve and
its source, and the resulting spare; two sparklines drawn from
``usage_history.jsonl`` (the 5h window over the last 24 hours, the 7d
window over its current cycle with the reset marked); and a fit matrix
for the queued jobs so a job that will not start explains itself here.

Everything is computed on a thread worker via :meth:`JobsEngine.capacities`
and the history reader; the screen only renders.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Static

from claude_swap.capacity import (
    AccountCapacity,
    WindowCapacity,
    pooled_capacity,
    windows_for_job,
)
from claude_swap.jobs import Job, JobStore
from claude_swap.jobs_engine import JobsEngine
from claude_swap.pace import WEEKLY_PERIOD_S
from claude_swap.reserves import WINDOW_5H, WINDOW_7D
from claude_swap.settings import JobsSettings, load_jobs_settings
from claude_swap.tui import data
from claude_swap.tui.theme import Palette
from claude_swap.usage_history import Sample

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp

REFRESH_S = 10.0
_BLOCKS = "▁▂▃▄▅▆▇█"


@dataclass(frozen=True)
class CapacityView:
    caps: tuple[AccountCapacity, ...]
    samples: dict[str, tuple[Sample, ...]]  # email → last 7d of samples
    queued: tuple[Job, ...]
    idle_s: float | None
    error: str | None
    taken_at: float


def sparkline(
    series: list[tuple[float, float]],
    *,
    start: float,
    end: float,
    columns: int,
) -> str:
    """One row of block characters: each column is the max pct seen in its
    time bucket; empty buckets are blank."""
    if columns <= 0 or end <= start:
        return ""
    width = (end - start) / columns
    buckets: list[float | None] = [None] * columns
    for t, v in series:
        if t < start or t > end:
            continue
        i = min(columns - 1, int((t - start) / width))
        buckets[i] = v if buckets[i] is None else max(buckets[i], v)
    out = []
    for b in buckets:
        if b is None:
            out.append(" ")
        else:
            idx = min(len(_BLOCKS) - 1, int(max(0.0, min(100.0, b)) / 100.0 * (len(_BLOCKS) - 1) + 0.5))
            out.append(_BLOCKS[idx])
    return "".join(out)


def _series(samples: tuple[Sample, ...], window: str) -> list[tuple[float, float]]:
    out = []
    for s in samples:
        v = s.five_hour if window == WINDOW_5H else s.seven_day if window == WINDOW_7D else s.scoped.get(window)
        if v is not None:
            out.append((s.t, v))
    return out


def _p(v: float | None, *, signed: bool = False) -> str:
    if v is None:
        return "?"
    return f"{v:+.0f}%" if signed else f"{v:.0f}%"


def pool_text(caps: list[AccountCapacity], *, now: float, palette: Palette, settings: JobsSettings) -> Text:
    """Capacity across accounts as one budget, with the next refill.

    The accounts' windows reset at different times, so a per-account view can
    show every account blocked while the pool still has room — the reserve
    and the burn forecast are charged once here, not once per account.
    """
    text = Text(no_wrap=True, overflow="ellipsis")
    pool = pooled_capacity(caps)
    if not pool:
        return text
    text.append(" pooled", style=palette.foreground)
    text.append("  reserve + forecast counted once\n", style=palette.muted)
    text.append(f" {'window':8} {'left':>6} {'fcst':>6} {'rsv':>5} {'spare':>7} {'best':>6}  next refill\n",
                style=palette.muted)
    for name, w in pool.items():
        style = (
            palette.sev_crit if w.blackout or w.spare_pct < 0
            else palette.sev_warn if w.spare_pct < settings.default_estimate_pct
            else palette.sev_ok
        )
        refill = "—"
        if w.next_reset is not None:
            refill = f"#{w.next_reset_account} in {max(0.0, w.next_reset - now) / 3600:.0f}h"
        text.append(f" {name:8}", style=palette.foreground)
        text.append(f" {w.remaining_pct:>5.0f}%", style=palette.muted)
        text.append(f" {w.forecast_pct:>5.0f}%", style=palette.muted)
        text.append(f" {w.reserve_pct:>4.0f}%", style=palette.muted)
        text.append(f" {w.spare_pct:>+6.0f}%", style=style)
        text.append(f" {w.best_single():>5.0f}%", style=palette.muted)
        text.append(f"  {refill}\n", style=palette.muted)
    return text


def window_table(cap: AccountCapacity, *, palette: Palette, settings: JobsSettings) -> Text:
    text = Text()
    text.append(
        f" {'window':8} {'used':>5} {'left':>5} {'recent':>9} {'typical':>8} {'forecast':>9} {'reserve':>8}  {'spare':>6}  resets\n",
        style=palette.muted,
    )
    for name, w in cap.windows.items():
        left = None if w.used_pct is None else 100.0 - w.used_pct
        recent = f"{w.recent_rate_pct_h:.1f}%/h" if w.recent_rate_pct_h is not None else "—"
        typical = f"{w.typical_forecast_pct:.0f}%" if w.typical_forecast_pct is not None else "—"
        resets = "?" if w.resets_at is None else datetime.fromtimestamp(w.resets_at).astimezone().strftime("%a %H:%M")
        src = f" ({w.reserve_source.note or w.reserve_source.short_id})" if w.reserve_source else ""
        spare_style = (
            palette.muted if w.spare_pct is None
            else palette.sev_crit if w.blackout or w.spare_pct < 0
            else palette.sev_warn if w.spare_pct < settings.default_estimate_pct
            else palette.sev_ok
        )
        text.append(f" {name:8}", style=palette.foreground)
        text.append(f" {_p(w.used_pct):>5}", style=palette.severity(w.used_pct))
        text.append(f" {_p(left):>5}", style=palette.muted)
        text.append(f" {recent:>9}", style=palette.muted)
        text.append(f" {typical:>8}", style=palette.muted)
        text.append(f" {_p(w.forecast_pct):>9}", style=palette.foreground)
        text.append(f" {_p(w.reserve_pct):>7}{'!' if w.blackout else ' '}", style=palette.sev_crit if w.blackout else palette.muted)
        text.append(f" {_p(w.spare_pct, signed=True) if w.spare_pct is not None else '?':>6}", style=spare_style)
        text.append(f"  {resets}", style=palette.muted)
        text.append(src, style=palette.muted)
        if w.samples == 0 and w.recent_rate_pct_h is None:
            text.append("  (no history)", style=palette.muted)
        text.append("\n")
    return text


def charts_text(
    cap: AccountCapacity, samples: tuple[Sample, ...], *, now: float, width: int, palette: Palette
) -> Text:
    text = Text()
    cols = max(20, min(60, (width - 12) // 2))
    five = cap.window(WINDOW_5H)
    seven = cap.window(WINDOW_7D)
    # 5h: last 24h
    s5 = sparkline(_series(samples, WINDOW_5H), start=now - 86400, end=now, columns=cols)
    text.append(" 5h, last 24h".ljust(cols + 4), style=palette.muted)
    if seven is not None:
        text.append(" 7d, this cycle", style=palette.muted)
    text.append("\n 100 ")
    text.append(s5, style=palette.severity(five.used_pct if five else None))
    if seven is not None and seven.resets_at is not None:
        start7 = seven.resets_at - WEEKLY_PERIOD_S
        s7 = sparkline(_series(samples, WINDOW_7D), start=start7, end=seven.resets_at, columns=cols)
        # mark "now" inside the cycle
        pos = int((now - start7) / WEEKLY_PERIOD_S * cols)
        pos = max(0, min(cols - 1, pos))
        text.append("   100 ")
        text.append(s7[:pos], style=palette.severity(seven.used_pct))
        text.append(s7[pos:pos + 1] or " ", style=palette.accent)
        text.append(s7[pos + 1:], style=palette.track)
    text.append("\n   0 ")
    text.append("┴" + "─" * (cols - 2) + "┘", style=palette.track)
    if seven is not None and seven.resets_at is not None:
        text.append("     0 ")
        text.append("┴" + "─" * (cols - 2) + "┘", style=palette.track)
    text.append("\n     ")
    text.append(datetime.fromtimestamp(now - 86400).astimezone().strftime("%H:%M").ljust(cols - 5), style=palette.muted)
    text.append("now  ", style=palette.muted)
    if seven is not None and seven.resets_at is not None:
        text.append("   ")
        text.append("cycle start".ljust(cols - 12), style=palette.muted)
        text.append("reset " + datetime.fromtimestamp(seven.resets_at).astimezone().strftime("%a %H:%M"), style=palette.accent)
    return text


def fit_matrix(
    queued: tuple[Job, ...], caps: tuple[AccountCapacity, ...], *, settings: JobsSettings, palette: Palette
) -> Text:
    text = Text()
    if not queued:
        text.append(" no queued jobs", style=palette.muted)
        return text
    text.append(f" {'queued':22} {'needs 5h / 7d':>14}", style=palette.muted)
    for cap in caps:
        text.append(f"   #{cap.number} fits?".ljust(18), style=palette.muted)
    text.append("\n")
    for job in queued[:8]:
        need7 = job.weekly_estimate(settings.weekly_cost_ratio)
        text.append(f" {job.name[:22]:22}", style=palette.foreground if job.auto else palette.muted)
        text.append(f" {job.estimate_pct:>5.0f}% / {need7:>4.1f}%", style=palette.muted)
        for cap in caps:
            windows = windows_for_job(job.model, cap.windows)
            blocker = None
            for name in windows:
                w = cap.window(name)
                need = job.estimate_pct if name == WINDOW_5H else need7
                if w is None or w.spare_pct is None:
                    blocker = f"{name} ?"
                    break
                if w.blackout:
                    blocker = f"{name} blackout"
                    break
                if w.spare_pct < need:
                    blocker = f"{name} {w.spare_pct:.0f}%<{need:.0f}%"
                    break
            cell = "yes" if blocker is None else f"no: {blocker}"
            text.append(f"   {cell:<15}", style=palette.sev_ok if blocker is None else palette.sev_crit)
        text.append("\n")
    return text


class CapacityScreen(Screen):
    BINDINGS = [
        Binding("left,h", "page(-1)", "Prev account"),
        Binding("right,l", "page(1)", "Next account"),
        Binding("f", "force_refresh", "Fetch usage"),
        Binding("escape,q", "back", "Back"),
    ]

    app: "CswapApp"

    def __init__(self) -> None:
        super().__init__()
        self._settings = JobsSettings()
        self._engine: JobsEngine | None = None
        self._store: JobStore | None = None
        self._view: CapacityView | None = None
        self._index = 0
        self._loading = False

    def compose(self) -> ComposeResult:
        yield Static("", id="cap-title")
        yield Static("", id="cap-pool")
        yield Static("", id="cap-table")
        yield Static("", id="cap-charts")
        yield Static("", id="cap-fit")
        yield Footer()

    def on_mount(self) -> None:
        switcher = self.app.switcher_for("claude")
        self._settings = load_jobs_settings(switcher.backup_dir)
        self._store = JobStore(switcher.backup_dir)
        self._engine = JobsEngine(switcher, self._settings, lambda _e: None, store=self._store)
        self.watch(self.app, "theme", lambda _t: self._repaint(), init=False)
        self.watch(self.app, "snapshots", lambda _s: self._load(), init=False)
        self.set_interval(REFRESH_S, self._load)
        self._load()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_page(self, delta: int) -> None:
        if self._view and self._view.caps:
            self._index = (self._index + delta) % len(self._view.caps)
            self._repaint()

    def action_force_refresh(self) -> None:
        self.app.request_refresh(full=True)
        self.notify("Refreshing usage…", timeout=2)

    # -- load -------------------------------------------------------------------

    def _load(self) -> None:
        if self._loading or self._engine is None:
            return
        self._loading = True
        self.run_worker(self._load_blocking, thread=True, group="cap-load", exit_on_error=False, name="cap-load")

    def _load_blocking(self) -> None:
        assert self._engine is not None and self._store is not None
        now = time.time()
        try:
            caps = tuple(self._engine.capacities(now=now))
            history = self._engine.switcher._usage_store.history
            samples = {
                cap.email: tuple(history.samples(cap.email, since=now - WEEKLY_PERIOD_S - 3600))
                for cap in caps
            }
            queued = tuple(self._store.queued())
            idle = self._engine.idle(now=now).idle_s
            view = CapacityView(caps=caps, samples=samples, queued=queued, idle_s=idle, error=None, taken_at=now)
        except Exception as e:  # noqa: BLE001
            view = CapacityView(caps=(), samples={}, queued=(), idle_s=None,
                                error=f"{type(e).__name__}: {e}"[:100], taken_at=now)
        try:
            self.app.call_from_thread(self._apply, view)
        except Exception:
            pass

    def _apply(self, view: CapacityView) -> None:
        self._loading = False
        if not self.is_attached:
            return
        self._view = view
        if view.caps:
            self._index = min(self._index, len(view.caps) - 1)
        self._repaint()

    # -- render -----------------------------------------------------------------

    def _repaint(self) -> None:
        palette = Palette.from_theme(self.app.current_theme)
        view = self._view
        title = Text()
        title.append("capacity", style=palette.foreground)
        if view is None:
            title.append("   computing…", style=palette.muted)
            self.query_one("#cap-title", Static).update(title)
            return
        if view.error:
            title.append(f"   {view.error}", style=palette.sev_warn)
            self.query_one("#cap-title", Static).update(title)
            self.query_one("#cap-pool", Static).update("")
            self.query_one("#cap-table", Static).update("")
            self.query_one("#cap-charts", Static).update("")
            self.query_one("#cap-fit", Static).update("")
            return
        idle = view.idle_s
        need = self._settings.quiet_minutes
        if idle is None:
            title.append("   idle ?", style=palette.muted)
        elif idle == float("inf"):
            title.append("   no interactive sessions", style=palette.sev_ok)
        else:
            quiet = idle >= need * 60
            title.append(f"   quiet {min(idle, need * 60) / 60:.0f}/{need:.0f}m", style=palette.sev_ok if quiet else palette.sev_warn)
        if not view.caps:
            title.append("   no accounts with usage", style=palette.muted)
            self.query_one("#cap-title", Static).update(title)
            return
        cap = view.caps[self._index]
        title.append(f"   #{cap.number} {cap.email}", style=palette.accent)
        title.append(f"   ({self._index + 1}/{len(view.caps)}  ◀ ▶)", style=palette.muted)
        if cap.usage_error:
            title.append(f"   {cap.usage_error}", style=palette.sev_warn)
        elif cap.usage_age_s is not None:
            title.append(f"   usage {data.format_duration(cap.usage_age_s)} old", style=palette.muted)
        self.query_one("#cap-title", Static).update(title)
        self.query_one("#cap-pool", Static).update(
            pool_text(list(view.caps), now=view.taken_at, palette=palette, settings=self._settings)
        )
        self.query_one("#cap-table", Static).update(window_table(cap, palette=palette, settings=self._settings))
        width = (self.size.width or 100) - 4
        self.query_one("#cap-charts", Static).update(
            charts_text(cap, view.samples.get(cap.email, ()), now=view.taken_at, width=width, palette=palette)
        )
        self.query_one("#cap-fit", Static).update(
            fit_matrix(view.queued, view.caps, settings=self._settings, palette=palette)
        )
