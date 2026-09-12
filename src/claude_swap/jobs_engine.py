"""Job scheduler: start queued jobs when the subscription has spare capacity.

``JobsEngine`` is UI-agnostic like :class:`~claude_swap.autoswitch.AutoSwitchEngine`:
it composes a switcher, a :class:`~claude_swap.jobs.JobStore`, a
:class:`~claude_swap.reserves.ReserveStore`, and the usage history, and
reports through typed events. ``ccswap jobs auto --once`` runs one tick
(launchd/cron), ``ccswap jobs auto`` loops, and the TUI hosts a live engine.

One tick, in order:

1. Reconcile: a ``running`` job whose worker died becomes ``failed``.
2. Concurrency: stop if ``max_concurrent`` jobs are already running.
3. Quiet gate: every interactive Claude Code session (default profile and
   every ccswap session profile) must have been idle for
   ``quiet_minutes``. A busy session is the strongest evidence the user
   still wants the capacity themselves, so the forecast is not consulted
   until the machine has gone quiet. Sessions belonging to running jobs
   are excluded from this check.
4. Capacity: for each account (store-only read — the auto daemon owns the
   fetch cadence) compute :func:`~claude_swap.capacity.account_capacity`.
5. Match: walk queued auto-eligible jobs in priority order; a job starts
   on the account whose binding spare is largest *and* covers the job's
   estimate in every window it touches (5h, 7d, and the scoped model
   window when its model has one). A job pinned to one account only
   considers that account.
6. Launch one job per tick (the next tick re-measures before the next
   job), through :meth:`~claude_swap.jobs.JobRunner.launch`.

The engine never fetches usage itself; if the store has no trusted usage
for an account that account is skipped and the tick reports it.
"""

from __future__ import annotations

import enum
import json
import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from claude_swap.capacity import AccountCapacity, account_capacity, windows_for_job
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.jobs import ACCOUNT_AUTO, Job, JobError, JobRunner, JobStore
from claude_swap.process_detection import ClaudeSession, scan_sessions
from claude_swap.reserves import ReserveStore
from claude_swap.settings import JobsSettings
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.usage_store import STALE_OK_S

logger = logging.getLogger("claude-swap.jobs")

# Usage older than this is not trusted for a start decision (matches the
# switcher's decision-trust bound).
USAGE_TRUST_S = STALE_OK_S


# -- events --------------------------------------------------------------------


@dataclass(frozen=True)
class JobsEvent:
    kind: str = "event"

    def to_json(self) -> dict:
        return {"event": self.kind, **self._fields()}

    def _fields(self) -> dict:
        return {}

    def human(self) -> str:  # pragma: no cover - overridden
        return self.kind


@dataclass(frozen=True)
class TickEvent(JobsEvent):
    kind: str = "tick"
    queued: int = 0
    running: int = 0
    idle_s: float | None = None
    quiet: bool = False
    capacities: tuple[AccountCapacity, ...] = ()

    def _fields(self) -> dict:
        return {
            "queued": self.queued,
            "running": self.running,
            "idleSeconds": self.idle_s,
            "quiet": self.quiet,
            "accounts": [capacity_to_json(c) for c in self.capacities],
        }

    def human(self) -> str:
        idle = "idle ?" if self.idle_s is None else f"idle {self.idle_s / 60:.0f}m"
        parts = [f"{self.queued} queued · {self.running} running · {idle}"]
        for cap in self.capacities:
            spare = cap.spare_for(("5h", "7d"))
            s5 = cap.window("5h")
            s7 = cap.window("7d")
            parts.append(
                f"#{cap.number} {cap.email}: 5h spare "
                f"{_pct(s5.spare_pct if s5 else None)} · 7d spare "
                f"{_pct(s7.spare_pct if s7 else None)}"
                + (f" · binding {_pct(spare)}" if spare is not None else "")
                + (f" · {cap.usage_error}" if cap.usage_error else "")
            )
        return " | ".join(parts)


@dataclass(frozen=True)
class StartEvent(JobsEvent):
    kind: str = "start"
    job_id: str = ""
    name: str = ""
    account: str = ""
    spare_pct: float | None = None
    estimate_pct: float = 0.0
    manual: bool = False

    def _fields(self) -> dict:
        return {
            "jobId": self.job_id,
            "name": self.name,
            "account": self.account,
            "sparePct": self.spare_pct,
            "estimatePct": self.estimate_pct,
            "manual": self.manual,
        }

    def human(self) -> str:
        how = "manual start" if self.manual else "auto-start"
        return (
            f"{how}: {self.name} on {self.account} "
            f"(spare {_pct(self.spare_pct)}, needs {self.estimate_pct:.0f}%)"
        )


@dataclass(frozen=True)
class HoldEvent(JobsEvent):
    """Nothing started, and why."""

    kind: str = "hold"
    reason: str = ""
    detail: str = ""

    def _fields(self) -> dict:
        return {"reason": self.reason, "detail": self.detail}

    def human(self) -> str:
        return f"hold: {self.reason}" + (f" ({self.detail})" if self.detail else "")


@dataclass(frozen=True)
class FinishedEvent(JobsEvent):
    kind: str = "finished"
    job_id: str = ""
    name: str = ""
    state: str = ""
    error: str | None = None

    def _fields(self) -> dict:
        return {"jobId": self.job_id, "name": self.name, "state": self.state, "error": self.error}

    def human(self) -> str:
        return f"{self.name}: {self.state}" + (f" — {self.error}" if self.error else "")


@dataclass(frozen=True)
class ErrorEvent(JobsEvent):
    kind: str = "error"
    message: str = ""

    def _fields(self) -> dict:
        return {"message": self.message}

    def human(self) -> str:
        return f"error: {self.message}"


def _pct(value: float | None) -> str:
    return "?" if value is None else f"{value:.0f}%"


def capacity_to_json(cap: AccountCapacity) -> dict:
    return {
        "number": cap.number,
        "email": cap.email,
        "usageAgeSeconds": cap.usage_age_s,
        "usageError": cap.usage_error,
        "windows": {
            name: {
                "usedPct": w.used_pct,
                "resetsAt": w.resets_at,
                "remainingSeconds": w.remaining_s,
                "recentRatePctPerHour": w.recent_rate_pct_h,
                "recentForecastPct": w.recent_forecast_pct,
                "typicalForecastPct": w.typical_forecast_pct,
                "forecastPct": w.forecast_pct,
                "reservePct": w.reserve_pct,
                "reserveSource": w.reserve_source.id if w.reserve_source else None,
                "sparePct": w.spare_pct,
                "samples": w.samples,
            }
            for name, w in cap.windows.items()
        },
    }


class TickOutcome(enum.Enum):
    STARTED = 0
    ERROR = 1
    NOTHING = 2
    HELD = 3


# -- idle detection --------------------------------------------------------------


@dataclass(frozen=True)
class IdleReport:
    idle_s: float | None  # seconds since the last busy interactive session; None = unknown
    busy: tuple[ClaudeSession, ...] = ()
    unreadable: int = 0


def interactive_idle(
    *,
    now: float,
    claude_home: Path,
    profile_dirs: tuple[Path, ...],
    exclude_pids: frozenset[int] = frozenset(),
) -> IdleReport:
    """How long every interactive session has been idle.

    Reads the session records under the default config home and every
    session profile. A ``busy`` record means idle 0; otherwise the newest
    ``statusUpdatedAt`` across records bounds the idle time from below. A
    machine with no interactive sessions at all is idle "forever".
    """
    newest_status: float | None = None
    busy: list[ClaudeSession] = []
    unreadable = 0
    for root in (claude_home, *profile_dirs):
        sessions, bad = scan_sessions(claude_dir=root)
        unreadable += bad
        for s in sessions:
            if s.pid in exclude_pids or s.kind != "interactive":
                continue
            if s.status == "busy":
                busy.append(s)
            stamp = _status_updated_at(root, s.pid)
            if stamp is not None:
                newest_status = stamp if newest_status is None else max(newest_status, stamp)
    if busy:
        return IdleReport(idle_s=0.0, busy=tuple(busy), unreadable=unreadable)
    if newest_status is None:
        return IdleReport(idle_s=float("inf") if unreadable == 0 else None, unreadable=unreadable)
    return IdleReport(idle_s=max(0.0, now - newest_status), unreadable=unreadable)


def _status_updated_at(root: Path, pid: int) -> float | None:
    try:
        raw = json.loads((root / "sessions" / f"{pid}.json").read_text(encoding="utf-8"))
        stamp = raw.get("statusUpdatedAt") or raw.get("updatedAt") or raw.get("startedAt")
        if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
            return float(stamp) / 1000.0
    except (OSError, ValueError, AttributeError):
        return None
    return None


# -- engine ----------------------------------------------------------------------


class JobsEngine:
    def __init__(
        self,
        switcher: ClaudeAccountSwitcher,
        settings: JobsSettings,
        on_event: Callable[[JobsEvent], None],
        *,
        store: JobStore | None = None,
        reserves: ReserveStore | None = None,
        runner: JobRunner | None = None,
        clock: Callable[[], float] = time.time,
        dry_run: bool = False,
        interval_s: float = 120.0,
        claude_home: Path | None = None,
    ) -> None:
        self.switcher = switcher
        self.settings = settings
        self.on_event = on_event
        self.store = store or JobStore(switcher.backup_dir)
        self.reserves = reserves or ReserveStore(switcher.backup_dir)
        self.runner = runner or JobRunner(switcher, settings, self.store, clock=clock)
        self.clock = clock
        self.dry_run = dry_run
        self.interval_s = interval_s
        self._claude_home = claude_home
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._seen_finished: set[tuple[str, str | None]] = set()

    # -- collaborators --------------------------------------------------------

    def _emit(self, event: JobsEvent) -> None:
        try:
            self.on_event(event)
        except Exception:  # noqa: BLE001 - a broken sink must not stop the engine
            logger.exception("jobs event sink failed")

    def _profile_dirs(self) -> tuple[Path, ...]:
        root = self.switcher.backup_dir / "sessions"
        if not root.is_dir():
            return ()
        return tuple(p for p in root.iterdir() if p.is_dir())

    def _claude_home_dir(self) -> Path:
        if self._claude_home is not None:
            return self._claude_home
        from claude_swap.paths import get_default_claude_config_home

        return get_default_claude_config_home()

    def capacities(self, *, now: float | None = None) -> list[AccountCapacity]:
        """Store-only capacity for every enabled OAuth account."""
        now = self.clock() if now is None else now
        entries = self.switcher.usage_entries_by_account(fetch=set())
        data = self.switcher._get_sequence_data() or {}
        accounts = data.get("accounts", {})
        out: list[AccountCapacity] = []
        for num, info in sorted(accounts.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0):
            if self.switcher._disabled_from_data(data, num):
                continue
            if self.switcher._account_kind(num) == "api_key":
                continue
            entry = entries.get(num)
            email = info.get("email", "")
            cap = account_capacity(
                number=num, email=email, entry=entry, now=now,
                history=self.switcher._usage_store.history,
                settings=self.settings, reserves=self.reserves,
            )
            if entry is not None and entry.age_s is not None and entry.age_s > USAGE_TRUST_S:
                cap = AccountCapacity(
                    number=cap.number, email=cap.email, windows=cap.windows,
                    usage_age_s=cap.usage_age_s,
                    usage_error=cap.usage_error or f"usage {entry.age_s / 60:.0f}m old",
                )
            out.append(cap)
        return out

    def idle(self, *, now: float | None = None) -> IdleReport:
        now = self.clock() if now is None else now
        running = self.store.running()
        exclude = frozenset(
            pid for j in running for pid in (j.claude_pid, j.worker_pid) if pid
        )
        return interactive_idle(
            now=now,
            claude_home=self._claude_home_dir(),
            profile_dirs=self._profile_dirs(),
            exclude_pids=exclude,
        )

    # -- policy ----------------------------------------------------------------

    def choose(
        self, job: Job, capacities: list[AccountCapacity]
    ) -> tuple[AccountCapacity, float] | None:
        """The account with the largest binding spare that fits ``job``."""
        pinned: tuple[str, str, str] | None = None
        if job.account != ACCOUNT_AUTO:
            try:
                pinned = self.switcher.resolve_account(job.account)
            except ClaudeSwitchError:
                return None
        best: tuple[AccountCapacity, float] | None = None
        for cap in capacities:
            if pinned is not None and cap.number != pinned[0]:
                continue
            if cap.usage_error and not cap.windows:
                continue
            if cap.usage_age_s is not None and cap.usage_age_s > USAGE_TRUST_S:
                continue
            windows = windows_for_job(job.model, cap.windows)
            fits = True
            binding: float | None = None
            for name in windows:
                w = cap.window(name)
                if w is None or w.spare_pct is None or w.blackout:
                    fits = False
                    break
                need = job.estimate_pct if name == "5h" else job.weekly_estimate(self.settings.weekly_cost_ratio)
                if w.spare_pct < need:
                    fits = False
                    break
                binding = w.spare_pct if binding is None else min(binding, w.spare_pct)
            if not fits or binding is None:
                continue
            if best is None or binding > best[1]:
                best = (cap, binding)
        return best

    def tick(self) -> TickOutcome:
        try:
            return self._tick_inner()
        except ClaudeSwitchError as e:
            self._emit(ErrorEvent(message=str(e)))
            return TickOutcome.ERROR
        except Exception as e:  # noqa: BLE001
            logger.exception("jobs tick failed")
            self._emit(ErrorEvent(message=f"{type(e).__name__}: {e}"))
            return TickOutcome.ERROR

    def _tick_inner(self) -> TickOutcome:
        now = self.clock()
        jobs = self.store.all()  # reconciles dead workers
        self._report_finished(jobs)
        running = [j for j in jobs if j.state == "running"]
        queued = [j for j in jobs if j.state == "queued" and j.auto]
        held = [j for j in queued if not j.ready(now)]
        budgeted = [j for j in queued if j.ready(now) and j.over_budget(now)]
        queued = [j for j in queued if j.ready(now) and not j.over_budget(now)]
        idle = self.idle(now=now)
        quiet = idle.idle_s is not None and idle.idle_s >= self.settings.quiet_minutes * 60.0
        caps = self.capacities(now=now) if queued else []
        self._emit(TickEvent(
            queued=len(queued), running=len(running), idle_s=None if idle.idle_s == float("inf") else idle.idle_s,
            quiet=quiet, capacities=tuple(caps),
        ))
        if not queued:
            if held or budgeted:
                bits = []
                if held:
                    soonest = min(j.not_before or now for j in held)
                    bits.append(f"{len(held)} waiting for cooldown (next in {max(0.0, soonest - now) / 60:.0f}m)")
                if budgeted:
                    bits.append(f"{len(budgeted)} over weekly budget")
                self._emit(HoldEvent(reason="held", detail="; ".join(bits)))
                return TickOutcome.HELD
            return TickOutcome.NOTHING
        if len(running) >= self.settings.max_concurrent:
            self._emit(HoldEvent(reason="max-concurrent", detail=f"{len(running)} running"))
            return TickOutcome.HELD
        if idle.idle_s is None:
            self._emit(HoldEvent(reason="idle-unknown", detail=f"{idle.unreadable} unreadable session records"))
            return TickOutcome.HELD
        if not quiet:
            busy = ", ".join(sorted({s.cwd.rsplit("/", 1)[-1] for s in idle.busy})) if idle.busy else ""
            self._emit(HoldEvent(
                reason="not-quiet",
                detail=(f"busy: {busy}" if busy else f"idle {idle.idle_s / 60:.0f}m < {self.settings.quiet_minutes:.0f}m"),
            ))
            return TickOutcome.HELD

        for job in queued:
            choice = self.choose(job, caps)
            if choice is None:
                continue
            cap, spare = choice
            self._emit(StartEvent(
                job_id=job.id, name=job.name, account=cap.email,
                spare_pct=spare, estimate_pct=job.estimate_pct,
            ))
            if self.dry_run:
                return TickOutcome.STARTED
            try:
                self.runner.launch(job, account=cap.number)
            except JobError as e:
                self._emit(ErrorEvent(message=str(e)))
                return TickOutcome.ERROR
            return TickOutcome.STARTED

        self._emit(HoldEvent(reason="no-capacity", detail=_no_capacity_detail(queued, caps)))
        return TickOutcome.HELD

    def _report_finished(self, jobs: list[Job]) -> None:
        for job in jobs:
            if job.state in ("done", "failed") and job.finished_at:
                key = (job.id, job.finished_at)
                if key not in self._seen_finished:
                    self._seen_finished.add(key)
                    if len(self._seen_finished) > 1:  # skip the initial backlog silently
                        self._emit(FinishedEvent(job_id=job.id, name=job.name, state=job.state, error=job.error))

    # -- loop -----------------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    def run_loop(self) -> int:
        while not self._stop.is_set():
            self.tick()
            delay = self.interval_s * random.uniform(0.9, 1.1)
            self._wake.wait(delay)
            self._wake.clear()
        return 0


def _no_capacity_detail(queued: list[Job], caps: list[AccountCapacity]) -> str:
    if not caps:
        return "no account usage available"
    first = queued[0]
    bits = []
    for cap in caps:
        spare = cap.spare_for(windows_for_job(first.model, cap.windows))
        bits.append(f"#{cap.number} spare {_pct(spare)}")
    return f"{first.name} needs {first.estimate_pct:.0f}%; " + ", ".join(bits)
