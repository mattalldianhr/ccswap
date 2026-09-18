"""Job queue: headless ``claude -p`` runs bound to a folder, kept in
``<backup_root>/jobs.json`` and executed under a stored account's session
profile.

A :class:`Job` is a prompt plus the folder it runs in and the knobs that
shape the run — account policy, model, effort, permission mode, allowed
tools, turn cap, priority, and a cost estimate in 5h-window percentage
points. The estimate starts as a guess and is replaced by the measured
usage delta of each completed run, so the scheduler's forecasts improve
with every job.

Execution model: ``JobRunner.launch`` spawns a detached *worker* process
(``ccswap jobs worker <id>``) so the launcher — a launchd ``--once`` tick,
the TUI, or a manual ``ccswap jobs start`` — never has to stay alive for the
run. The worker resolves the account, bootstraps its session profile via
:class:`~claude_swap.session.SessionManager` (so the job is pinned to that
account through ``CLAUDE_CONFIG_DIR`` and immune to default-login switches),
runs ``claude -p`` with ``--output-format stream-json`` into
``<backup_root>/jobs/<id>/stream.jsonl``, and writes the outcome and usage
delta back to the store. A job whose account is already the *active
default* login runs with the plain environment instead, mirroring
``ccswap run``'s fast path: two copies of one account's credentials would
drift when the server rotates the refresh token.

The store is a single JSON document guarded by ``.jobs.lock``; every write
is a read-modify-write under that lock and nothing holds the lock across a
``claude`` run. A ``running`` job whose worker pid is dead is reconciled to
``failed`` on the next store read that asks for it.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.locking import FileLock
from claude_swap.mappings import normalize_path
from claude_swap.models import get_timestamp
from claude_swap.process_detection import is_pid_alive
from claude_swap.settings import JobsSettings, atomic_write_json

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher

JOBS_FILENAME = "jobs.json"
JOBS_SCHEMA_VERSION = 1
JOBS_DIRNAME = "jobs"

STATES = ("queued", "running", "done", "failed", "cancelled", "paused")
ACTIVE_STATES = ("queued", "running", "paused")
PERMISSION_MODES = ("acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan")
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
ACCOUNT_AUTO = "auto"

# Grace between SIGTERM and SIGKILL when a job overruns its timeout.
_KILL_GRACE_S = 15.0
# How fresh the post-run usage fetch must be to count as this run's cost.
_COST_FRESH_S = 900.0


class JobError(ClaudeSwitchError):
    """A job could not be created, found, or run."""


@dataclass(frozen=True)
class RunRecord:
    """One completed (or failed) execution of a job."""

    started_at: str
    finished_at: str
    account: str  # email
    exit_code: int | None
    session_id: str | None
    cost_5h: float | None  # measured 5h-window delta, pct points
    cost_7d: float | None
    cost_scoped: dict[str, float] = field(default_factory=dict)
    error: str | None = None
    duration_s: float | None = None
    num_turns: int | None = None

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: dict) -> "RunRecord":
        return cls(
            started_at=str(raw.get("started_at") or ""),
            finished_at=str(raw.get("finished_at") or ""),
            account=str(raw.get("account") or ""),
            exit_code=raw.get("exit_code") if isinstance(raw.get("exit_code"), int) else None,
            session_id=raw.get("session_id") if isinstance(raw.get("session_id"), str) else None,
            cost_5h=_num_or_none(raw.get("cost_5h")),
            cost_7d=_num_or_none(raw.get("cost_7d")),
            cost_scoped={
                str(k): float(v)
                for k, v in (raw.get("cost_scoped") or {}).items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)
            },
            error=raw.get("error") if isinstance(raw.get("error"), str) else None,
            duration_s=_num_or_none(raw.get("duration_s")),
            num_turns=raw.get("num_turns") if isinstance(raw.get("num_turns"), int) else None,
        )


@dataclass(frozen=True)
class Job:
    id: str
    name: str
    folder: str
    prompt: str
    account: str = ACCOUNT_AUTO  # "auto" | slot number | email | alias
    model: str | None = None
    effort: str | None = None
    permission_mode: str = "acceptEdits"
    allowed_tools: tuple[str, ...] = ()
    max_turns: int | None = None
    priority: int = 50  # lower runs first
    estimate_pct: float = 10.0  # expected 5h-window cost, pct points
    weekly_estimate_pct: float | None = None  # None = estimate_pct * ratio
    auto: bool = True  # eligible for capacity-driven auto start
    state: str = "queued"
    created_at: str = ""
    updated_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    worker_pid: int | None = None
    claude_pid: int | None = None
    session_id: str | None = None
    account_used: str | None = None
    exit_code: int | None = None
    error: str | None = None
    result_text: str | None = None
    runs: tuple[RunRecord, ...] = ()
    extra_args: tuple[str, ...] = ()
    # Loop-shaped jobs: re-queue this long after a successful run (None =
    # one-shot); the queue holds it until ``not_before``.
    repeat_minutes: float | None = None
    not_before: float | None = None  # POSIX; scheduler skips it until then
    # Chaining: when this job finishes successfully, re-queue that job.
    then_job: str | None = None  # job id or exact name
    # Budget: skip auto-starts once this job's measured 7d cost over the
    # last 7 days reaches this many pct points (None = unlimited).
    weekly_budget_pct: float | None = None

    # -- derived --------------------------------------------------------------

    @property
    def short_id(self) -> str:
        return self.id[:8]

    @property
    def is_active(self) -> bool:
        return self.state in ACTIVE_STATES

    def weekly_spent(self, now: float) -> float:
        """Measured 7d cost of runs that finished within the last 7 days."""
        from datetime import datetime, timezone

        total = 0.0
        for r in self.runs:
            if r.cost_7d is None:
                continue
            try:
                t = datetime.strptime(r.finished_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                continue
            if now - t <= 7 * 86400:
                total += r.cost_7d
        return total

    def over_budget(self, now: float) -> bool:
        return self.weekly_budget_pct is not None and self.weekly_spent(now) >= self.weekly_budget_pct

    def ready(self, now: float) -> bool:
        """Queued and past its ``not_before`` hold."""
        return self.state == "queued" and (self.not_before is None or now >= self.not_before)

    def weekly_estimate(self, ratio: float) -> float:
        if self.weekly_estimate_pct is not None:
            return self.weekly_estimate_pct
        return self.estimate_pct * ratio

    def to_json(self) -> dict:
        data = asdict(self)
        data["allowed_tools"] = list(self.allowed_tools)
        data["extra_args"] = list(self.extra_args)
        data["runs"] = [r.to_json() for r in self.runs]
        return data

    @classmethod
    def from_json(cls, raw: dict) -> "Job | None":
        try:
            job_id = str(raw["id"])
            folder = str(raw["folder"])
            prompt = str(raw["prompt"])
        except (KeyError, TypeError):
            return None
        state = raw.get("state") if raw.get("state") in STATES else "queued"
        runs = tuple(
            RunRecord.from_json(r) for r in (raw.get("runs") or []) if isinstance(r, dict)
        )
        return cls(
            id=job_id,
            name=str(raw.get("name") or job_id[:8]),
            folder=folder,
            prompt=prompt,
            account=str(raw.get("account") or ACCOUNT_AUTO),
            model=raw.get("model") if isinstance(raw.get("model"), str) and raw.get("model") else None,
            effort=raw.get("effort") if raw.get("effort") in EFFORT_LEVELS else None,
            permission_mode=(
                raw.get("permission_mode")
                if raw.get("permission_mode") in PERMISSION_MODES
                else "acceptEdits"
            ),
            allowed_tools=tuple(str(t) for t in (raw.get("allowed_tools") or [])),
            max_turns=raw.get("max_turns") if isinstance(raw.get("max_turns"), int) else None,
            priority=raw.get("priority") if isinstance(raw.get("priority"), int) else 50,
            estimate_pct=_num_or_none(raw.get("estimate_pct")) or 10.0,
            weekly_estimate_pct=_num_or_none(raw.get("weekly_estimate_pct")),
            auto=bool(raw.get("auto", True)),
            state=state,
            created_at=str(raw.get("created_at") or ""),
            updated_at=str(raw.get("updated_at") or ""),
            started_at=raw.get("started_at") if isinstance(raw.get("started_at"), str) else None,
            finished_at=raw.get("finished_at") if isinstance(raw.get("finished_at"), str) else None,
            worker_pid=raw.get("worker_pid") if isinstance(raw.get("worker_pid"), int) else None,
            claude_pid=raw.get("claude_pid") if isinstance(raw.get("claude_pid"), int) else None,
            session_id=raw.get("session_id") if isinstance(raw.get("session_id"), str) else None,
            account_used=raw.get("account_used") if isinstance(raw.get("account_used"), str) else None,
            exit_code=raw.get("exit_code") if isinstance(raw.get("exit_code"), int) else None,
            error=raw.get("error") if isinstance(raw.get("error"), str) else None,
            result_text=raw.get("result_text") if isinstance(raw.get("result_text"), str) else None,
            runs=runs,
            extra_args=tuple(str(a) for a in (raw.get("extra_args") or [])),
            repeat_minutes=_num_or_none(raw.get("repeat_minutes")),
            not_before=_num_or_none(raw.get("not_before")),
            then_job=raw.get("then_job") if isinstance(raw.get("then_job"), str) and raw.get("then_job") else None,
            weekly_budget_pct=_num_or_none(raw.get("weekly_budget_pct")),
        )


def _num_or_none(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def new_job_id() -> str:
    return uuid.uuid4().hex[:12]


def validate_job_fields(
    *,
    folder: str,
    prompt: str,
    permission_mode: str,
    effort: str | None,
    max_turns: int | None,
    estimate_pct: float,
    priority: int,
) -> str | None:
    """Return a human error for the first invalid field, or None."""
    path = Path(folder).expanduser()
    if not path.is_dir():
        return f"Folder does not exist: {folder}"
    if not prompt.strip():
        return "Prompt is required."
    if permission_mode not in PERMISSION_MODES:
        return f"Permission mode must be one of: {', '.join(PERMISSION_MODES)}"
    if effort is not None and effort not in EFFORT_LEVELS:
        return f"Effort must be one of: {', '.join(EFFORT_LEVELS)}"
    if max_turns is not None and max_turns < 1:
        return "Max turns must be >= 1."
    if not 0 < estimate_pct <= 100:
        return "Estimate must be between 0 and 100 pct."
    if not 0 <= priority <= 999:
        return "Priority must be between 0 and 999."
    return None


# -- store -------------------------------------------------------------------


class JobStore:
    """Reads and writes ``<backup_dir>/jobs.json`` under ``.jobs.lock``."""

    def __init__(self, backup_dir: Path):
        self.backup_dir = Path(backup_dir)
        self.path = self.backup_dir / JOBS_FILENAME
        self._lock_path = self.backup_dir / ".jobs.lock"
        self.jobs_dir = self.backup_dir / JOBS_DIRNAME

    def _lock(self) -> FileLock:
        return FileLock(self._lock_path)

    def _read(self) -> dict[str, Job]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {}
        if not isinstance(raw, dict) or raw.get("schemaVersion") != JOBS_SCHEMA_VERSION:
            return {}
        out: dict[str, Job] = {}
        for item in raw.get("jobs") or []:
            if isinstance(item, dict):
                job = Job.from_json(item)
                if job is not None:
                    out[job.id] = job
        return out

    def _write(self, jobs: dict[str, Job]) -> None:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            self.path,
            {
                "schemaVersion": JOBS_SCHEMA_VERSION,
                "jobs": [j.to_json() for j in jobs.values()],
            },
        )

    def log_dir(self, job_id: str) -> Path:
        return self.jobs_dir / job_id

    # -- reads ----------------------------------------------------------------

    def all(self) -> list[Job]:
        """Every job, queue order first (priority, then creation)."""
        with self._lock():
            jobs = self._reconcile_locked(self._read())
        return sorted(jobs.values(), key=_queue_key)

    def get(self, identifier: str) -> Job:
        """Job by full id, unique id prefix, or exact name."""
        jobs = self.all()
        exact = [j for j in jobs if j.id == identifier]
        if exact:
            return exact[0]
        by_name = [j for j in jobs if j.name == identifier]
        if len(by_name) == 1:
            return by_name[0]
        prefix = [j for j in jobs if j.id.startswith(identifier)]
        if len(prefix) == 1:
            return prefix[0]
        if len(prefix) > 1 or len(by_name) > 1:
            raise JobError(f"'{identifier}' matches more than one job; use the full id")
        raise JobError(f"No job matches '{identifier}'")

    def queued(self, *, auto_only: bool = False) -> list[Job]:
        return [
            j for j in self.all()
            if j.state == "queued" and (j.auto or not auto_only)
        ]

    def running(self) -> list[Job]:
        return [j for j in self.all() if j.state == "running"]

    # -- writes ---------------------------------------------------------------

    def add(self, job: Job) -> Job:
        now = get_timestamp()
        job = replace(job, created_at=job.created_at or now, updated_at=now)
        with self._lock():
            jobs = self._read()
            if job.id in jobs:
                raise JobError(f"Job {job.id} already exists")
            jobs[job.id] = job
            self._write(jobs)
        return job

    def update(self, job_id: str, **fields) -> Job:
        """Read-modify-write one job; unknown ids raise."""
        with self._lock():
            jobs = self._read()
            job = jobs.get(job_id)
            if job is None:
                raise JobError(f"No job with id {job_id}")
            job = replace(job, updated_at=get_timestamp(), **fields)
            jobs[job_id] = job
            self._write(jobs)
        return job

    def remove(self, job_id: str, *, purge_logs: bool = True) -> bool:
        with self._lock():
            jobs = self._read()
            if jobs.pop(job_id, None) is None:
                return False
            self._write(jobs)
        if purge_logs:
            shutil.rmtree(self.log_dir(job_id), ignore_errors=True)
        return True

    def claim_for_run(self, job_id: str, worker_pid: int) -> Job | None:
        """Atomically move a queued job to ``running``; None if it was not
        queued any more (another launcher got there first)."""
        with self._lock():
            jobs = self._read()
            job = jobs.get(job_id)
            if job is None or job.state != "queued":
                return None
            job = replace(
                job,
                state="running",
                worker_pid=worker_pid,
                claude_pid=None,
                started_at=get_timestamp(),
                finished_at=None,
                updated_at=get_timestamp(),
                error=None,
                exit_code=None,
                result_text=None,
            )
            jobs[job_id] = job
            self._write(jobs)
        return job

    # -- reconciliation -------------------------------------------------------

    def _reconcile_locked(self, jobs: dict[str, Job]) -> dict[str, Job]:
        """Mark ``running`` jobs whose worker died as failed. Called under lock."""
        changed = False
        for job_id, job in list(jobs.items()):
            if job.state != "running":
                continue
            pid = job.worker_pid
            if pid is not None and is_pid_alive(pid):
                continue
            jobs[job_id] = replace(
                job,
                state="failed",
                finished_at=job.finished_at or get_timestamp(),
                updated_at=get_timestamp(),
                error=job.error or "worker process died",
                worker_pid=None,
                claude_pid=None,
            )
            changed = True
        if changed:
            self._write(jobs)
        return jobs


def _queue_key(job: Job) -> tuple:
    state_rank = {"running": 0, "queued": 1, "paused": 2, "failed": 3, "done": 4, "cancelled": 5}
    return (state_rank.get(job.state, 9), job.priority, job.created_at, job.id)


# -- runner ------------------------------------------------------------------


@dataclass(frozen=True)
class UsagePoint:
    """Per-window pct at one moment, for cost deltas."""

    five_hour: float | None
    five_reset: str | None
    seven_day: float | None
    seven_reset: str | None
    scoped: dict[str, float]
    fetched_at: float | None

    @classmethod
    def from_entry(cls, entry) -> "UsagePoint":
        lg = getattr(entry, "last_good", None)
        if not isinstance(lg, dict):
            return cls(None, None, None, None, {}, getattr(entry, "fetched_at", None))
        five = lg.get("five_hour") if isinstance(lg.get("five_hour"), dict) else {}
        seven = lg.get("seven_day") if isinstance(lg.get("seven_day"), dict) else {}
        scoped = {}
        for win in lg.get("scoped") or []:
            if isinstance(win, dict) and isinstance(win.get("name"), str):
                pct = _num_or_none(win.get("pct"))
                if pct is not None:
                    scoped[win["name"]] = pct
        return cls(
            five_hour=_num_or_none(five.get("pct")),
            five_reset=five.get("resets_at") if isinstance(five.get("resets_at"), str) else None,
            seven_day=_num_or_none(seven.get("pct")),
            seven_reset=seven.get("resets_at") if isinstance(seven.get("resets_at"), str) else None,
            scoped=scoped,
            fetched_at=getattr(entry, "fetched_at", None),
        )


def cost_delta(before: UsagePoint, after: UsagePoint) -> tuple[float | None, float | None, dict[str, float]]:
    """Window deltas between two points; a reset in between yields None for
    that window (the delta would be meaningless)."""

    def delta(a: float | None, b: float | None, ra: str | None, rb: str | None) -> float | None:
        if a is None or b is None:
            return None
        if ra and rb and ra != rb:
            return None  # window rolled over mid-run
        return max(0.0, b - a) if b >= a else None

    scoped: dict[str, float] = {}
    for name, b in after.scoped.items():
        a = before.scoped.get(name)
        if a is not None and b >= a:
            scoped[name] = b - a
    return (
        delta(before.five_hour, after.five_hour, before.five_reset, after.five_reset),
        delta(before.seven_day, after.seven_day, before.seven_reset, after.seven_reset),
        scoped,
    )


def build_claude_argv(job: Job, claude_bin: str) -> list[str]:
    """The ``claude -p`` command line for a job."""
    argv = [
        claude_bin,
        "-p",
        job.prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        job.permission_mode,
        "--name",
        f"job:{job.name}",
    ]
    if job.permission_mode == "bypassPermissions":
        argv.append("--dangerously-skip-permissions")
    if job.model:
        argv += ["--model", job.model]
    if job.effort:
        argv += ["--effort", job.effort]
    if job.allowed_tools:
        argv += ["--allowedTools", *job.allowed_tools]
    if job.max_turns:
        argv += ["--max-turns", str(job.max_turns)]
    argv += list(job.extra_args)
    return argv


def worker_argv(job_id: str) -> list[str]:
    """Command that runs the detached worker for a job."""
    return [sys.executable, "-m", "claude_swap", "jobs", "worker", job_id]


class JobRunner:
    """Launches and executes jobs. Stateless beyond its collaborators."""

    def __init__(
        self,
        switcher: "ClaudeAccountSwitcher",
        settings: JobsSettings,
        store: JobStore | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.switcher = switcher
        self.settings = settings
        self.store = store or JobStore(switcher.backup_dir)
        self.clock = clock

    # -- launch (detached) ----------------------------------------------------

    def launch(self, job: Job, *, account: str | None = None) -> Job:
        """Start a detached worker for a queued job. ``account`` pins the
        account for this run (slot/email); None lets the worker resolve the
        job's own policy. Returns the job as ``running``."""
        if job.state != "queued":
            raise JobError(f"Job {job.short_id} is {job.state}, not queued")
        log_dir = self.store.log_dir(job.id)
        log_dir.mkdir(parents=True, exist_ok=True)
        worker_log = log_dir / "worker.log"
        env = dict(os.environ)
        if account:
            env["CCSWAP_JOB_ACCOUNT"] = account
        with worker_log.open("ab") as out:
            proc = subprocess.Popen(
                worker_argv(job.id),
                cwd=str(self.switcher.backup_dir),
                stdout=out,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
            )
        claimed = self.store.claim_for_run(job.id, proc.pid)
        if claimed is None:
            # Lost the race; the worker will notice the job is not queued and exit.
            raise JobError(f"Job {job.short_id} was started elsewhere")
        return claimed

    # -- account selection ----------------------------------------------------

    def resolve_account(self, job: Job, pinned: str | None = None) -> tuple[str, str, str]:
        """(slot, email, org) for a run. ``pinned`` (from the launcher) wins,
        then the job's own policy; ``auto`` falls back to the active login."""
        identifier = pinned or (job.account if job.account != ACCOUNT_AUTO else None)
        if identifier is None:
            data = self.switcher._get_sequence_data() or {}
            active = data.get("activeAccountNumber")
            if active is None:
                raise JobError("No active account to run the job under")
            identifier = str(active)
        return self.switcher.resolve_account(identifier)

    def _run_env(self, account_num: str, email: str, org: str) -> dict[str, str]:
        """Environment for ``claude``: a session profile pinned to the account,
        or the plain env when that account is already the default login."""
        from claude_swap.session import AUTH_OVERRIDE_ENV_VARS, SessionManager

        env = {k: v for k, v in os.environ.items() if k not in AUTH_OVERRIDE_ENV_VARS}
        env.pop("CCSWAP_JOB_ACCOUNT", None)
        current = self.switcher._get_current_account()
        if current is not None and current == (email, org):
            return env
        manager = SessionManager(self.switcher)
        session_dir, _num, _email = manager.setup_session(account_num, share=True)
        env["CLAUDE_CONFIG_DIR"] = str(session_dir)
        return env

    def _usage_point(self, account_num: str, *, fetch: bool) -> UsagePoint:
        try:
            entries = self.switcher.usage_entries_by_account(
                fetch={account_num} if fetch else set()
            )
        except Exception:  # noqa: BLE001 - cost accounting is best-effort
            return UsagePoint(None, None, None, None, {}, None)
        entry = entries.get(account_num)
        if entry is None:
            return UsagePoint(None, None, None, None, {}, None)
        return UsagePoint.from_entry(entry)

    # -- worker (blocking) ----------------------------------------------------

    def run_worker(self, job_id: str, *, pinned_account: str | None = None) -> int:
        """Execute one job to completion in this process. Returns the exit
        code to report. Expects the job to already be ``running`` with this
        process as its worker (``launch``), or ``queued`` when invoked
        directly (``ccswap jobs start --wait``)."""
        store = self.store
        job = store.get(job_id)
        if job.state == "queued":
            claimed = store.claim_for_run(job.id, os.getpid())
            if claimed is None:
                return 3
            job = claimed
        elif job.state != "running" or job.worker_pid != os.getpid():
            _log(store, job.id, f"job is {job.state} (worker {job.worker_pid}); not running")
            return 3

        started = self.clock()
        log_dir = store.log_dir(job.id)
        log_dir.mkdir(parents=True, exist_ok=True)
        stream_path = log_dir / "stream.jsonl"
        stderr_path = log_dir / "stderr.log"

        try:
            account_num, email, org = self.resolve_account(job, pinned_account)
        except ClaudeSwitchError as e:
            return self._finish(job, started, email=None, exit_code=None, error=str(e))

        claude_bin = shutil.which("claude")
        if not claude_bin:
            return self._finish(job, started, email=email, exit_code=None,
                                error="'claude' was not found on PATH")
        try:
            env = self._run_env(account_num, email, org)
        except ClaudeSwitchError as e:
            return self._finish(job, started, email=email, exit_code=None,
                                error=f"session profile: {e}")

        before = self._usage_point(account_num, fetch=True)
        argv = build_claude_argv(job, claude_bin)
        _log(store, job.id, f"account {email} · cwd {job.folder}")
        _log(store, job.id, "argv " + " ".join(_short(a) for a in argv))

        timeout_s = self.settings.job_timeout_minutes * 60.0
        error: str | None = None
        exit_code: int | None = None
        try:
            with stream_path.open("ab") as out, stderr_path.open("ab") as err:
                proc = subprocess.Popen(
                    argv,
                    cwd=job.folder,
                    env=env,
                    stdout=out,
                    stderr=err,
                    stdin=subprocess.DEVNULL,
                )
                store.update(job.id, claude_pid=proc.pid, account_used=email)
                try:
                    exit_code = proc.wait(timeout=timeout_s)
                except subprocess.TimeoutExpired:
                    error = f"timed out after {self.settings.job_timeout_minutes:g} min"
                    _terminate(proc)
                    exit_code = proc.returncode
        except OSError as e:
            error = f"could not start claude: {e}"

        result = parse_stream_result(stream_path)
        if error is None and exit_code not in (0, None):
            error = result.get("error") or f"claude exited with {exit_code}"
        elif error is None and result.get("is_error"):
            error = result.get("error") or "claude reported an error"

        # Post-run usage: force a fetch so the delta reflects this run.
        after = self._usage_point(account_num, fetch=True)
        finished = self.clock()
        c5, c7, cs = cost_delta(before, after)
        fresh = after.fetched_at is not None and finished - after.fetched_at <= _COST_FRESH_S
        if not fresh:
            c5, c7, cs = None, None, {}

        return self._finish(
            job, started, email=email, exit_code=exit_code, error=error,
            session_id=result.get("session_id"),
            result_text=result.get("result"),
            num_turns=result.get("num_turns"),
            cost=(c5, c7, cs),
        )

    def _finish(
        self,
        job: Job,
        started: float,
        *,
        email: str | None,
        exit_code: int | None,
        error: str | None,
        session_id: str | None = None,
        result_text: str | None = None,
        num_turns: int | None = None,
        cost: tuple[float | None, float | None, dict[str, float]] = (None, None, {}),
    ) -> int:
        finished = self.clock()
        c5, c7, cs = cost
        record = RunRecord(
            started_at=job.started_at or get_timestamp(),
            finished_at=get_timestamp(),
            account=email or "",
            exit_code=exit_code,
            session_id=session_id,
            cost_5h=c5,
            cost_7d=c7,
            cost_scoped=cs,
            error=error,
            duration_s=finished - started,
            num_turns=num_turns,
        )
        fields: dict = {
            "state": "failed" if error else "done",
            "finished_at": record.finished_at,
            "worker_pid": None,
            "claude_pid": None,
            "exit_code": exit_code,
            "error": error,
            "session_id": session_id,
            "result_text": (result_text or "")[:4000] or None,
            "account_used": email,
            "runs": (*job.runs, record)[-20:],
        }
        # Learn the cost: a measured delta replaces the estimate.
        if c5 is not None and c5 > 0:
            fields["estimate_pct"] = round(c5, 1)
        if c7 is not None and c7 > 0:
            fields["weekly_estimate_pct"] = round(c7, 1)
        # Loop: a successful run of a repeating job goes straight back to the
        # queue, held until the cooldown elapses.
        if not error and job.repeat_minutes:
            fields.update(
                state="queued", not_before=finished + job.repeat_minutes * 60.0,
                started_at=None, worker_pid=None, claude_pid=None,
            )
        self.store.update(job.id, **fields)
        if not error and job.then_job:
            self._chain(job)
        _log(
            self.store, job.id,
            f"finished: {'error: ' + error if error else 'ok'}"
            f" · exit {exit_code} · 5h Δ {c5 if c5 is not None else '?'}"
            f" · 7d Δ {c7 if c7 is not None else '?'}",
        )
        return 1 if error else 0

    def _chain(self, job: Job) -> None:
        """Re-queue the job named by ``then_job`` (no-op if it is running or queued)."""
        try:
            nxt = self.store.get(job.then_job or "")
        except JobError as e:
            _log(self.store, job.id, f"chain: {e}")
            return
        if nxt.id == job.id or nxt.state in ("queued", "running"):
            return
        self.store.update(
            nxt.id, state="queued", error=None, exit_code=None, finished_at=None,
            started_at=None, worker_pid=None, claude_pid=None, result_text=None, not_before=None,
        )
        _log(self.store, job.id, f"chain: queued {nxt.name}")

    # -- control --------------------------------------------------------------

    def cancel(self, job: Job) -> Job:
        """Cancel a queued job, or terminate a running one."""
        if job.state == "queued" or job.state == "paused":
            return self.store.update(job.id, state="cancelled", finished_at=get_timestamp())
        if job.state == "running":
            # Signal the worker's process GROUP, not the bare pids. The worker is
            # a group leader (``start_new_session=True``) and its ``claude -p``
            # children — which may spawn further `claude -p` calls of their own —
            # share its PGID. Killing pids alone orphans them: on 2026-09-17 a
            # cancelled draft-collect left two of them running, still spending
            # quota on a job the queue had already marked failed.
            for pid in (job.claude_pid, job.worker_pid):
                if not (pid and is_pid_alive(pid)):
                    continue
                try:
                    pgid = os.getpgid(pid)
                except OSError:
                    pgid = None
                # Group-signal only a real group leader, and never our own group:
                # a pid sharing this process's group would take the caller down.
                if pgid is not None and pgid == pid and pgid != os.getpgrp():
                    try:
                        os.killpg(pgid, signal.SIGTERM)
                        continue
                    except OSError:
                        pass
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            return self.store.update(
                job.id, state="cancelled", finished_at=get_timestamp(),
                worker_pid=None, claude_pid=None, error="cancelled",
            )
        raise JobError(f"Job {job.short_id} is {job.state}; nothing to cancel")

    def requeue(self, job: Job) -> Job:
        if job.state == "running":
            raise JobError(f"Job {job.short_id} is running")
        return self.store.update(
            job.id, state="queued", error=None, exit_code=None, finished_at=None,
            started_at=None, worker_pid=None, claude_pid=None, result_text=None, not_before=None,
        )


def _signal_group(proc: subprocess.Popen, sig: int) -> bool:
    """Signal the worker's whole process group; fall back to the bare process.

    Workers are spawned with ``start_new_session=True``, so the worker is a
    process-group leader and its ``claude -p`` children share its PGID. Signalling
    only ``proc.pid`` leaves those children running: on 2026-09-17 a cancelled
    ``draft-collect`` left two ``claude -p`` processes burning quota against a job
    the queue had already marked ``failed``.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None
    # Only signal the group when this process actually LEADS one. A child that
    # shares our group (anything spawned without start_new_session, as in the
    # tests) would otherwise take the signal to our own process group down with
    # it — killpg there kills the test runner, not the job.
    if pgid is not None and pgid == proc.pid and pgid != os.getpgrp():
        try:
            os.killpg(pgid, sig)
            return True
        except OSError:
            pass
    try:
        proc.send_signal(sig)
        return True
    except OSError:
        return False


def _terminate(proc: subprocess.Popen) -> None:
    try:
        if not _signal_group(proc, signal.SIGTERM):
            return
        proc.wait(timeout=_KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        _signal_group(proc, signal.SIGKILL)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    except OSError:
        pass


def _log(store: JobStore, job_id: str, line: str) -> None:
    try:
        log_dir = store.log_dir(job_id)
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "worker.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {line}\n")
    except OSError:
        pass


def _short(arg: str, limit: int = 80) -> str:
    if len(arg) <= limit:
        return arg
    return arg[: limit - 1] + "…"


def parse_stream_result(stream_path: Path) -> dict:
    """The final ``result`` event of a stream-json run, as a plain dict.

    Keys: ``session_id``, ``result`` (assistant text), ``is_error``,
    ``num_turns``, ``error``. Missing file or no result event → empty dict.
    """
    out: dict = {}
    try:
        with stream_path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(evt, dict):
                    continue
                if evt.get("type") == "system" and evt.get("session_id"):
                    out.setdefault("session_id", evt["session_id"])
                if evt.get("type") == "result":
                    out["session_id"] = evt.get("session_id") or out.get("session_id")
                    out["is_error"] = bool(evt.get("is_error"))
                    if isinstance(evt.get("num_turns"), int):
                        out["num_turns"] = evt["num_turns"]
                    res = evt.get("result")
                    if isinstance(res, str):
                        out["result"] = res
                    if evt.get("is_error"):
                        errs = evt.get("errors")
                        if isinstance(errs, list) and errs:
                            out["error"] = "; ".join(str(e) for e in errs)[:500]
                        elif isinstance(res, str):
                            out["error"] = res[:500]
                        else:
                            out["error"] = str(evt.get("subtype") or "error")
    except OSError:
        return {}
    return out


def tail_stream_text(stream_path: Path, *, max_lines: int = 40) -> list[str]:
    """Human-readable tail of a stream-json log: assistant text, tool names,
    and the result line. For the TUI log pane."""
    lines: list[str] = []
    try:
        with stream_path.open("r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    evt = json.loads(raw)
                except ValueError:
                    continue
                for text in _describe_event(evt):
                    lines.append(text)
    except OSError:
        return []
    return lines[-max_lines:]


def _describe_event(evt: object) -> Iterable[str]:
    if not isinstance(evt, dict):
        return
    kind = evt.get("type")
    if kind == "assistant":
        msg = evt.get("message") or {}
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text"):
                for para in str(block["text"]).strip().splitlines():
                    if para.strip():
                        yield para.rstrip()
            elif block.get("type") == "tool_use":
                name = block.get("name") or "tool"
                inp = block.get("input") or {}
                hint = ""
                if isinstance(inp, dict):
                    for key in ("command", "file_path", "pattern", "description", "prompt"):
                        if isinstance(inp.get(key), str):
                            hint = inp[key]
                            break
                yield f"⚙ {name}  {_short(hint, 100)}".rstrip()
    elif kind == "result":
        status = "error" if evt.get("is_error") else "done"
        turns = evt.get("num_turns")
        yield f"■ {status}" + (f" · {turns} turns" if isinstance(turns, int) else "")
