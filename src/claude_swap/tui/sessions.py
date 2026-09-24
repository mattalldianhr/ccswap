"""Sessions: every live Claude Code session on this machine, what it is doing,
what it has spent, and what it has spawned.

Read-only by default. ``enter`` jumps to the session's tmux pane, ``o`` opens its
transcript, ``y`` copies the resume command. ``K`` (whole session) and ``x``
(one child process) send SIGTERM, each behind a confirm dialog naming the pid.
Data comes from :mod:`claude_swap.sessions`, refreshed on a worker thread.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Sparkline, Static

from claude_swap import sessions as S
from claude_swap.tui.data import format_duration
from claude_swap.tui.modals import ConfirmModal, OutputModal

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp

REFRESH_S = 3.0
FILTERS = ("all", "busy", "headless")
STATUS_DOT = {"busy": ("●", "green"), "idle": ("○", "yellow"), "shell": ("◐", "cyan")}


def human(n: int) -> str:
    for unit, div in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if n >= div:
            return f"{n / div:.1f}{unit}"
    return str(n)


def ago(seconds: float) -> str:
    return "now" if seconds < 60 else format_duration(seconds) + " ago"


def account_label(account: str) -> str:
    if account == "default":
        return "active"
    num = account.partition("-")[0]
    return f"#{num}" if num.isdigit() else account[:10]


class SessionsScreen(Screen):
    BINDINGS = [
        Binding("enter", "jump", "tmux"),
        Binding("o", "open_transcript", "Transcript"),
        Binding("y", "copy_resume", "Copy resume"),
        Binding("f", "cycle_filter", "Filter"),
        Binding("m", "toggle_metric", "Metric"),
        Binding("tab", "focus_next_table", "Sessions/procs", show=False),
        Binding("K", "kill_session", "Kill session"),
        Binding("x", "kill_child", "Kill process"),
        Binding("escape,q", "back", "Back"),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    app: "CswapApp"

    def __init__(self) -> None:
        super().__init__()
        self._cache = S.TranscriptCache()
        self._rows: list[S.SessionInfo] = []
        self._filter = "all"
        self._metric = "output"
        self._selected_pid: int | None = None
        self._refreshing = False

    def compose(self) -> ComposeResult:
        with Vertical(id="sessions-top"):
            yield Static("", id="sessions-summary")
            yield Sparkline([0], id="sessions-chart")
        yield DataTable(id="sessions-table", cursor_type="row", zebra_stripes=True)
        with Horizontal(id="sessions-bottom"):
            with Vertical(id="sessions-detail-col"):
                yield Static("", id="sessions-detail")
                yield Sparkline([0], id="sessions-spark")
            yield DataTable(id="sessions-procs", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#sessions-table", DataTable)
        t.add_columns("", "Title", "Mode", "Acct", "Folder", "Active", "Out", "Total", "Procs")
        p = self.query_one("#sessions-procs", DataTable)
        p.add_columns("PID", "Kind", "CPU%", "Mem", "Age", "Command")
        t.focus()
        self.set_interval(REFRESH_S, self._refresh)
        self._refresh()

    # -- data -----------------------------------------------------------------

    def _dirs(self) -> tuple[Path, Path | None]:
        claude_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
        try:
            backup = self.app.switcher_for("claude").backup_dir
        except Exception:  # noqa: BLE001 - a monitor must render without a switcher
            backup = None
        return claude_dir, backup

    def _refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        self.run_worker(self._load, thread=True, group="sessions-load", exclusive=True, exit_on_error=False)

    def _load(self) -> None:
        try:
            claude_dir, backup = self._dirs()
            rows = S.snapshot(claude_dir, backup, self._cache)
        except Exception as e:  # noqa: BLE001
            rows, err = None, e
        else:
            err = None
        self.app.call_from_thread(self._apply, rows, err)

    def _apply(self, rows: list[S.SessionInfo] | None, err: Exception | None) -> None:
        self._refreshing = False
        if not self.is_attached:
            return
        if rows is None:
            self.query_one("#sessions-summary", Static).update(Text(f"could not read sessions: {err}", style="red"))
            return
        self._rows = rows
        self._render_summary()
        self._render_table()
        self._render_detail()

    def _visible(self) -> list[S.SessionInfo]:
        if self._filter == "busy":
            return [r for r in self._rows if r.status == "busy"]
        if self._filter == "headless":
            return [r for r in self._rows if r.mode in ("headless", "job", "bg", "teammate")]
        return self._rows

    # -- rendering ------------------------------------------------------------

    def _render_summary(self) -> None:
        now = time.time()
        series = S.combined_timeline(self._rows, now, self._metric)
        busy = sum(1 for r in self._rows if r.status == "busy")
        kids = sum(len(r.children) for r in self._rows)
        label = "output tokens" if self._metric == "output" else "tokens processed (incl. cache)"
        s = Text()
        s.append(f" {len(self._rows)} sessions", style="bold")
        s.append(f" · {busy} busy · {kids} child processes · ")
        s.append(f"{human(sum(series))} {label} in the last 5h", style="bold")
        s.append(f" · peak {human(max(series, default=0))}/min · filter: {self._filter}  (m: metric, f: filter)")
        self.query_one("#sessions-summary", Static).update(s)
        self.query_one("#sessions-chart", Sparkline).data = series or [0]

    def _render_table(self) -> None:
        t = self.query_one("#sessions-table", DataTable)
        rows = self._visible()
        keep = self._selected_pid
        t.clear()
        now = time.time()
        for r in rows:
            dot, colour = STATUS_DOT.get(r.status or "", ("·", "dim"))
            last = r.state.last_ts
            t.add_row(
                Text(dot, style=colour),
                Text(r.title[:48]),
                r.mode,
                account_label(r.account),
                Path(r.cwd).name[:22] if r.cwd else "",
                ago(now - last) if last else "",
                human(r.state.output_tokens),
                human(r.state.total_tokens),
                str(len(r.children)),
                key=str(r.pid),
            )
        if keep is not None:
            for i, r in enumerate(rows):
                if r.pid == keep:
                    t.move_cursor(row=i)
                    break

    def _current(self) -> S.SessionInfo | None:
        t = self.query_one("#sessions-table", DataTable)
        rows = self._visible()
        if not rows or t.cursor_row is None or t.cursor_row >= len(rows):
            return None
        return rows[t.cursor_row]

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # DataTable owns enter; a selected session row means "take me there".
        if event.data_table.id == "sessions-table":
            self.action_jump()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "sessions-table":
            return
        r = self._current()
        self._selected_pid = r.pid if r else None
        self._render_detail()

    def _render_detail(self) -> None:
        r = self._current()
        detail = self.query_one("#sessions-detail", Static)
        procs = self.query_one("#sessions-procs", DataTable)
        spark = self.query_one("#sessions-spark", Sparkline)
        procs.clear()
        if r is None:
            detail.update("")
            spark.data = [0]
            return
        st = r.state
        d = Text()
        d.append(f"{r.title}\n", style="bold")
        d.append(f"pid {r.pid} · {r.mode} · {account_label(r.account)} · {st.model or '?'} · v{r.version or '?'}\n", style="dim")
        if r.cwd:
            d.append(f"{r.cwd}\n", style="dim")
        if r.job:
            d.append(f"ccswap job: {r.job}\n", style="magenta")
        if r.tmux:
            d.append(f"tmux {r.tmux}\n", style="cyan")
        d.append(f"tokens: out {human(st.output_tokens)} · in {human(st.input_tokens)} · cache read "
                 f"{human(st.cache_read)} · cache write {human(st.cache_write)}\n")
        if st.last_prompt:
            d.append("\nlast prompt  ", style="bold")
            d.append(st.last_prompt[:280] + "\n")
        if st.last_reply:
            d.append("\nlast reply   ", style="bold")
            d.append(st.last_reply[:280] + "\n")
        detail.update(d)
        spark.data = st.timeline(time.time(), self._metric) or [0]
        for depth, p in r.children:
            procs.add_row(str(p.pid), p.label, f"{p.cpu:.0f}", f"{p.rss_kb // 1024}M", p.etime,
                          ("  " * (depth - 1)) + p.command[:90], key=str(p.pid))

    # -- actions --------------------------------------------------------------

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_cursor_down(self) -> None:
        self.focused.action_cursor_down() if isinstance(self.focused, DataTable) else None

    def action_cursor_up(self) -> None:
        self.focused.action_cursor_up() if isinstance(self.focused, DataTable) else None

    def action_focus_next_table(self) -> None:
        t = self.query_one("#sessions-table", DataTable)
        p = self.query_one("#sessions-procs", DataTable)
        (p if self.focused is t else t).focus()

    def action_cycle_filter(self) -> None:
        self._filter = FILTERS[(FILTERS.index(self._filter) + 1) % len(FILTERS)]
        self._render_summary()
        self._render_table()
        self._render_detail()

    def action_toggle_metric(self) -> None:
        self._metric = "total" if self._metric == "output" else "output"
        self._render_summary()
        self._render_detail()

    def action_jump(self) -> None:
        r = self._current()
        if r is None or not r.tmux:
            self.notify("This session has no tmux pane.", severity="warning")
            return
        if os.environ.get("TMUX") and shutil.which("tmux"):
            res = subprocess.run(["tmux", "switch-client", "-t", r.tmux], capture_output=True, text=True, check=False)
            if res.returncode == 0:
                return
            self.notify(res.stderr.strip() or "tmux switch failed", severity="error")
            return
        session = r.tmux.split(":", 1)[0]
        self.app.push_screen(OutputModal("Attach in another terminal", f"tmux attach -t {session}\n\npane: {r.tmux}"))

    def action_open_transcript(self) -> None:
        r = self._current()
        if r is None or not r.transcript:
            self.notify("No transcript for this session.", severity="warning")
            return
        pager = os.environ.get("PAGER", "less")
        with self.app.suspend():
            argv = [pager, "+G", str(r.transcript)] if Path(pager).name == "less" else [pager, str(r.transcript)]
            subprocess.run(argv, check=False)

    def action_copy_resume(self) -> None:
        r = self._current()
        if r is None or not r.resume_command:
            self.notify("Nothing to resume for this row.", severity="warning")
            return
        for tool in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"]):
            if shutil.which(tool[0]):
                subprocess.run(tool, input=r.resume_command, text=True, check=False)
                self.notify("Copied: " + r.resume_command)
                return
        self.app.push_screen(OutputModal("Resume command", r.resume_command))

    def action_kill_session(self) -> None:
        r = self._current()
        if r is None:
            return
        cmd = (r.proc.command if r.proc else "")[:160]
        self.app.push_screen(
            ConfirmModal(
                f"Send SIGTERM to session pid {r.pid} and its process group?\n\n{r.title}\n{cmd}",
                title="Kill session", yes_label="Kill",
            ),
            partial(self._on_kill, r.pid, True),
        )

    def action_kill_child(self) -> None:
        p = self.query_one("#sessions-procs", DataTable)
        r = self._current()
        if r is None or not r.children or p.cursor_row is None or p.cursor_row >= len(r.children):
            self.notify("Select a process in the process table first (tab).", severity="warning")
            return
        _, proc = r.children[p.cursor_row]
        self.app.push_screen(
            ConfirmModal(
                f"Send SIGTERM to pid {proc.pid} ({proc.label})?\n\n{proc.command[:200]}",
                title="Kill process", yes_label="Kill",
            ),
            partial(self._on_kill, proc.pid, False),
        )

    def _on_kill(self, pid: int, tree: bool, confirmed: bool | None) -> None:
        if not confirmed:
            return
        try:
            S.kill(pid, tree=tree)
            self.notify(f"SIGTERM sent to {pid}")
        except OSError as e:
            self.notify(f"Could not signal {pid}: {e}", severity="error")
        self._refresh()
