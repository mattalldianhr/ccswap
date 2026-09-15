"""Antigravity screen: read-only quota for a provider ccswap does not manage.

Separate from the dashboard's account list on purpose. That list is built for
switching — every row is a switch target, and add/remove/disable act on it.
Antigravity has exactly one login and ccswap only reads it, so a row there
would offer actions that cannot work. This screen shows the numbers and
nothing else.

The reason it earns a screen at all is the second quota group: Antigravity
serves Claude models on a budget entirely separate from the managed Claude
subscriptions. When the pooled 7d window is spent, that group may not be.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Static

from claude_swap.antigravity import AntigravityError, AntigravityUsage, read_usage
from claude_swap.oauth import format_reset
from claude_swap.tui.theme import Palette

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp

# Antigravity's windows move slowly and this is a hand-opened screen, not a
# background poller, so a slow cadence is plenty. Each tick costs one request.
REFRESH_S = 60.0
BAR_WIDTH = 24
# Order windows shortest-first, matching every other surface in ccswap.
WINDOW_ORDER = ("five_hour", "weekly")
WINDOW_LABELS = {"five_hour": "5h", "weekly": "Weekly"}


@dataclass(frozen=True)
class AntigravityStatus:
    """One reading, or the reason there isn't one.

    Shared by the dashboard panel and the detail screen so both describe the
    same failure in the same words, and so the panel never has to decide what
    an exception from an undocumented endpoint means.
    """

    usage: AntigravityUsage | None
    error: str | None
    taken_at: float

    @classmethod
    def read(cls, *, now: float | None = None) -> "AntigravityStatus":
        """Read quota, converting every failure into an ``error`` string.

        Deliberately non-raising: this runs on a worker thread feeding a
        reactive that several widgets render. An exception escaping here would
        blank a dashboard that is otherwise healthy.
        """
        now = now if now is not None else time.time()
        try:
            return cls(usage=read_usage(now=now), error=None, taken_at=now)
        except AntigravityError as exc:
            return cls(usage=None, error=str(exc), taken_at=now)
        except Exception as exc:  # noqa: BLE001 - see the docstring
            return cls(usage=None, error=f"{type(exc).__name__}: {exc}"[:200], taken_at=now)


# The screen's own view is the same thing; kept as an alias so the screen and
# the panel can never drift into two shapes.
AntigravityView = AntigravityStatus


def bar(pct: float, *, width: int = BAR_WIDTH) -> str:
    """A used/left bar. Shares the dashboard's vocabulary of filled blocks."""
    filled = round(max(0.0, min(100.0, pct)) / 100.0 * width)
    return "█" * filled + "░" * (width - filled)


def _severity(pct: float, palette: Palette) -> str:
    if pct >= 90:
        return palette.sev_crit
    if pct >= 70:
        return palette.sev_warn
    return palette.sev_ok


def groups_text(usage: AntigravityUsage, *, now: float, palette: Palette) -> Text:
    """Every quota group with its windows, bars, and reset countdowns."""
    text = Text(no_wrap=True, overflow="ellipsis")
    for index, group in enumerate(usage.groups):
        if index:
            text.append("\n")
        text.append(f" {group.name}", style=palette.foreground)
        if group.serves_claude:
            # The headline fact: Claude capacity on a separate budget.
            text.append("   serves Claude models", style=palette.accent)
        text.append("\n")
        if group.description:
            text.append(f"   {group.description}\n", style=palette.muted)
        for name in WINDOW_ORDER:
            window = group.window(name)
            if window is None:
                continue
            label = WINDOW_LABELS.get(name, name)
            text.append(f"   {label:<7}", style=palette.foreground)
            text.append(bar(window.used_pct), style=_severity(window.used_pct, palette))
            text.append(f" {window.used_pct:>5.1f}% used", style=palette.muted)
            if window.resets_at:
                countdown, clock = format_reset(window.resets_at)
                text.append(f"  · resets in {countdown} ({clock})", style=palette.muted)
            text.append("\n")
        # A group can carry a window ccswap does not name; show it rather than
        # silently dropping quota the user is being charged for.
        for window in group.buckets:
            if window.window in WINDOW_ORDER:
                continue
            text.append(f"   {window.window:<7}", style=palette.foreground)
            text.append(bar(window.used_pct), style=_severity(window.used_pct, palette))
            text.append(f" {window.used_pct:>5.1f}% used\n", style=palette.muted)
    return text


class AntigravityScreen(Screen):
    BINDINGS = [
        Binding("f,r", "force_refresh", "Refresh"),
        Binding("escape,q", "back", "Back"),
    ]

    app: "CswapApp"

    def __init__(self) -> None:
        super().__init__()
        self._view: AntigravityView | None = None
        self._loading = False

    def compose(self) -> ComposeResult:
        yield Static("", id="agy-title")
        yield Static("", id="agy-groups")
        yield Static("", id="agy-note")
        yield Footer()

    def on_mount(self) -> None:
        self.watch(self.app, "theme", lambda _t: self._repaint(), init=False)
        self.set_interval(REFRESH_S, self._load)
        self._load()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_force_refresh(self) -> None:
        self._load()
        self.notify("Refreshing Antigravity quota…", timeout=2)

    # -- load -------------------------------------------------------------------

    def _load(self) -> None:
        if self._loading:
            return
        self._loading = True
        self.run_worker(
            self._load_blocking, thread=True, group="agy-load",
            exit_on_error=False, name="agy-load",
        )

    def _load_blocking(self) -> None:
        """Network + Keychain, off the UI thread.

        Every failure lands in the view as text. This is an undocumented
        endpoint on another tool's login: it going away must degrade this
        screen, never take down a TUI that is also showing healthy Claude data.
        """
        view = AntigravityStatus.read()
        try:
            self.app.call_from_thread(self._apply, view)
        except Exception:
            pass  # the app went away mid-fetch

    def _apply(self, view: AntigravityView) -> None:
        self._loading = False
        if not self.is_attached:
            return
        self._view = view
        self._repaint()

    # -- render -----------------------------------------------------------------

    def _repaint(self) -> None:
        palette = Palette.from_theme(self.app.current_theme)
        view = self._view
        title = Text(no_wrap=True, overflow="ellipsis")
        title.append("antigravity", style=palette.foreground)
        groups = self.query_one("#agy-groups", Static)
        note = self.query_one("#agy-note", Static)

        if view is None:
            title.append("   reading…", style=palette.muted)
            self.query_one("#agy-title", Static).update(title)
            return
        if view.error is not None:
            title.append("   unavailable", style=palette.sev_warn)
            self.query_one("#agy-title", Static).update(title)
            message = Text(no_wrap=False)
            message.append(f" {view.error}\n", style=palette.sev_warn)
            groups.update(message)
            note.update(Text(
                " Sign in with 'agy' if this persists. Quota here is read-only;\n"
                " ccswap never modifies the Antigravity login.",
                style=palette.muted,
            ))
            return

        usage = view.usage
        assert usage is not None
        title.append(f"   {usage.email or 'signed in'}", style=palette.accent)
        age = max(0.0, time.time() - usage.fetched_at)
        title.append(f"   read {age:.0f}s ago", style=palette.muted)
        self.query_one("#agy-title", Static).update(title)
        groups.update(groups_text(usage, now=view.taken_at, palette=palette))
        note.update(Text(
            " Read-only. Separate from the managed Claude and Codex quotas.",
            style=palette.muted,
        ))


def panel_text(
    status: "AntigravityStatus | None", width: int, *, palette: Palette
) -> Text:
    """The compact dashboard form: one line per quota group.

    ``Claude/GPT  5h ━━╸───  12% · 7d ────── 0%   serves Claude models``

    Deliberately terser than the screen. On the dashboard this competes with
    the accounts it sits beside, and the only question it has to answer at a
    glance is whether there is room left in a budget the managed accounts do
    not draw from. Press 'y' for the full reading.
    """
    from claude_swap.tui.widgets import bar_cells

    text = Text(no_wrap=True, overflow="ellipsis")
    if status is None:
        text.append("loading…", style=palette.muted)
        return text
    if status.error is not None:
        text.append(status.error, style=palette.sev_warn)
        return text
    usage = status.usage
    if usage is None or not usage.groups:
        text.append("no quota reported", style=palette.muted)
        return text

    # Bars shrink with the panel but never below a width where the fill is
    # unreadable; the label column is fixed so the bars line up down the list.
    bar_width = max(6, min(10, (width - 46) // 2))
    for index, group in enumerate(usage.groups):
        if index:
            text.append("\n")
        text.append(f"{_short_name(group.name):<11}", style=palette.foreground)
        for position, name in enumerate(WINDOW_ORDER):
            window = group.window(name)
            if window is None:
                continue
            if position:
                text.append(" · ", style=palette.muted)
            text.append(f"{WINDOW_LABELS[name]} ", style=palette.muted)
            text.append(bar_cells(window.used_pct, bar_width, palette=palette))
            text.append(f" {window.used_pct:3.0f}%", style=palette.severity(window.used_pct))
        if group.serves_claude:
            text.append("   serves Claude", style=palette.accent)
    return text


def _short_name(name: str) -> str:
    """Fit a group's name into the panel's label column.

    Antigravity's own names ("Claude and GPT models") are written for a
    settings page, not a status line.
    """
    lowered = name.lower()
    if "claude" in lowered:
        return "Claude/GPT"
    if "gemini" in lowered:
        return "Gemini"
    return name[:11]
