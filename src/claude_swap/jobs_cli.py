"""``ccswap jobs …`` and ``ccswap reserves …`` command handlers.

Pre-dispatched from :func:`claude_swap.cli.main` like ``run``/``auto``.
Everything here is thin: parse, call into jobs/jobs_engine/reserves, print.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

from claude_swap import paths
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.json_output import error_envelope
from claude_swap.jobs import (
    ACCOUNT_AUTO,
    EFFORT_LEVELS,
    PERMISSION_MODES,
    Job,
    JobRunner,
    JobStore,
    new_job_id,
    tail_stream_text,
    validate_job_fields,
)
from claude_swap.jobs_engine import JobsEngine, capacity_to_json
from claude_swap.mappings import normalize_path
from claude_swap.printer import accent, bolded, dimmed, error, muted, warning
from claude_swap.reserves import ReserveStore, format_when, make_reserve
from claude_swap.settings import load_jobs_settings
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.usage_history import backfill_from_auto_log

LAUNCHD_LABEL = "com.ccswap.jobs"


def _prog() -> str:
    return Path(sys.argv[0]).name or "ccswap"


# -- jobs ------------------------------------------------------------------------


def jobs_command(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog=f"{_prog()} jobs",
        description="Queue headless Claude Code jobs and run them on spare capacity.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  ccswap jobs add ~/proj "Fix the flaky test in tests/test_io.py" --name flaky
  ccswap jobs add . -f prompt.md --model opus --effort high --permission-mode acceptEdits
  ccswap jobs list
  ccswap jobs start flaky              # detached, right now, ignoring capacity
  ccswap jobs start flaky --wait       # run in the foreground
  ccswap jobs capacity                 # what the scheduler sees per account
  ccswap jobs auto --once              # one scheduler tick (launchd/cron)
  ccswap jobs daemon install           # launchd agent ticking every 5 minutes
        """,
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    sub = parser.add_subparsers(dest="action", required=True)

    p_add = sub.add_parser("add", help="Queue a job")
    p_add.add_argument("folder", help="Folder or repo the job runs in")
    p_add.add_argument("prompt", nargs="?", help="The prompt (or use -f)")
    p_add.add_argument("-f", "--prompt-file", help="Read the prompt from a file (- for stdin)")
    p_add.add_argument("-n", "--name", help="Short name (default: derived from the prompt)")
    p_add.add_argument("-a", "--account", default=ACCOUNT_AUTO, help="Slot, email, alias, or 'auto' (default)")
    p_add.add_argument("--model", help="Model (default: jobs.defaultModel or Claude's default)")
    p_add.add_argument("--effort", choices=EFFORT_LEVELS, help="Effort level")
    p_add.add_argument("--permission-mode", choices=PERMISSION_MODES, help="Permission mode")
    p_add.add_argument("--allowed-tools", nargs="*", default=None, help="Allowed tool patterns")
    p_add.add_argument("--max-turns", type=int, help="Turn cap")
    p_add.add_argument("--priority", type=int, default=50, help="Lower runs first (default 50)")
    p_add.add_argument("--estimate", type=float, help="Expected 5h-window cost in pct")
    p_add.add_argument("--manual", action="store_true", help="Never auto-start; only `jobs start`")
    p_add.add_argument("--", dest="_dashdash", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)

    p_list = sub.add_parser("list", help="List jobs")
    p_list.add_argument("--all", action="store_true", help="Include finished jobs")
    p_list.add_argument("--json", action="store_true")

    p_show = sub.add_parser("show", help="Show one job")
    p_show.add_argument("job")
    p_show.add_argument("--json", action="store_true")

    p_start = sub.add_parser("start", help="Start a queued job now")
    p_start.add_argument("job")
    p_start.add_argument("-a", "--account", help="Run under this account for this run")
    p_start.add_argument("--wait", action="store_true", help="Run in the foreground")

    p_cancel = sub.add_parser("cancel", help="Cancel a queued or running job")
    p_cancel.add_argument("job")

    p_retry = sub.add_parser("retry", help="Re-queue a finished job")
    p_retry.add_argument("job")

    p_rm = sub.add_parser("remove", help="Delete a job and its logs")
    p_rm.add_argument("job")

    p_edit = sub.add_parser("edit", help="Change a queued job's settings")
    p_edit.add_argument("job")
    p_edit.add_argument("--name")
    p_edit.add_argument("--account")
    p_edit.add_argument("--model")
    p_edit.add_argument("--effort", choices=EFFORT_LEVELS)
    p_edit.add_argument("--permission-mode", choices=PERMISSION_MODES)
    p_edit.add_argument("--priority", type=int)
    p_edit.add_argument("--estimate", type=float)
    p_edit.add_argument("--max-turns", type=int)
    p_edit.add_argument("--auto", choices=("on", "off"))

    p_log = sub.add_parser("log", help="Show a job's output")
    p_log.add_argument("job")
    p_log.add_argument("-n", "--lines", type=int, default=60)
    p_log.add_argument("--raw", action="store_true", help="Raw stream-json lines")

    p_cap = sub.add_parser("capacity", help="Spare capacity per account, as the scheduler sees it")
    p_cap.add_argument("--json", action="store_true")

    p_auto = sub.add_parser("auto", help="Run the scheduler")
    p_auto.add_argument("--once", action="store_true", help="One tick; exit code = outcome")
    p_auto.add_argument("--dry-run", action="store_true", help="Decide but never launch")
    p_auto.add_argument("--json", action="store_true", help="One JSON event per line")
    p_auto.add_argument("--interval", type=float, default=120.0, metavar="SECONDS")

    p_worker = sub.add_parser("worker", help=argparse.SUPPRESS)
    p_worker.add_argument("job_id")

    p_bf = sub.add_parser("backfill", help="Seed usage history from the ccswap auto log")
    p_bf.add_argument("log", nargs="?", help="Log file (default: ~/Library/Logs/cswap-auto.log)")

    p_daemon = sub.add_parser("daemon", help="Install or remove the launchd scheduler (macOS)")
    p_daemon.add_argument("op", choices=("install", "uninstall", "status"))
    p_daemon.add_argument("--interval", type=int, default=300, metavar="SECONDS")

    args = parser.parse_args(argv)
    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        settings = load_jobs_settings(switcher.backup_dir)
        store = JobStore(switcher.backup_dir)
        runner = JobRunner(switcher, settings, store)
        handler = {
            "add": _add, "list": _list, "show": _show, "start": _start,
            "cancel": _cancel, "retry": _retry, "remove": _remove, "edit": _edit,
            "log": _log, "capacity": _capacity, "auto": _auto, "worker": _worker,
            "backfill": _backfill, "daemon": _daemon,
        }[args.action]
        rc = handler(args, switcher=switcher, settings=settings, store=store, runner=runner)
        if rc:
            sys.exit(rc)
    except ClaudeSwitchError as e:
        if getattr(args, "json", False):
            print(json.dumps(error_envelope(e)))
        else:
            error(f"Error: {e}")
        sys.exit(1)


def _derive_name(prompt: str) -> str:
    words = [w for w in prompt.strip().split() if w]
    return "-".join(w.lower().strip(".,:;!?\"'`") for w in words[:4])[:40] or "job"


def _add(args, *, switcher, settings, store, runner) -> int:
    prompt = args.prompt
    if args.prompt_file:
        if args.prompt_file == "-":
            prompt = sys.stdin.read()
        else:
            prompt = Path(args.prompt_file).expanduser().read_text(encoding="utf-8")
    if not prompt:
        error("Error: give a prompt or -f FILE")
        return 1
    folder = normalize_path(args.folder)
    permission_mode = args.permission_mode or settings.default_permission_mode
    effort = args.effort or settings.default_effort
    model = args.model or settings.default_model
    estimate = args.estimate if args.estimate is not None else settings.default_estimate_pct
    problem = validate_job_fields(
        folder=folder, prompt=prompt, permission_mode=permission_mode, effort=effort,
        max_turns=args.max_turns, estimate_pct=estimate, priority=args.priority,
    )
    if problem:
        error(f"Error: {problem}")
        return 1
    if args.account != ACCOUNT_AUTO:
        switcher.resolve_account(args.account)  # raises on unknown
    job = Job(
        id=new_job_id(),
        name=args.name or _derive_name(prompt),
        folder=folder,
        prompt=prompt,
        account=args.account,
        model=model,
        effort=effort,
        permission_mode=permission_mode,
        allowed_tools=tuple(args.allowed_tools or ()),
        max_turns=args.max_turns,
        priority=args.priority,
        estimate_pct=estimate,
        auto=not args.manual,
        extra_args=tuple(getattr(args, "_dashdash", None) or ()),
    )
    job = store.add(job)
    print(f"{accent('Queued')} {bolded(job.name)} {muted(job.short_id)} in {job.folder}")
    print(dimmed(
        f"  account {job.account} · model {job.model or 'default'} · effort {job.effort or 'default'}"
        f" · {job.permission_mode} · priority {job.priority} · est {job.estimate_pct:g}%"
        f"{' · manual only' if not job.auto else ''}"
    ))
    return 0


_STATE_STYLE = {
    "running": accent, "queued": bolded, "paused": muted,
    "done": dimmed, "failed": lambda s: warning_text(s), "cancelled": dimmed,
}


def warning_text(text: str) -> str:
    from claude_swap.printer import yellowed

    return yellowed(text)


def _list(args, *, store, **_) -> int:
    jobs = store.all()
    if not args.all:
        jobs = [j for j in jobs if j.is_active or (j.state in ("failed",))]
    if args.json:
        print(json.dumps({"jobs": [j.to_json() for j in jobs]}, indent=2))
        return 0
    if not jobs:
        print(dimmed("No jobs. Add one with: ccswap jobs add <folder> \"<prompt>\""))
        return 0
    for j in jobs:
        style = _STATE_STYLE.get(j.state, str)
        line = (
            f"{style(j.state.ljust(9))} {bolded(j.name.ljust(24)[:24])} {muted(j.short_id)}  "
            f"p{j.priority:<3} est {j.estimate_pct:>4.0f}%  {_short_folder(j.folder)}"
        )
        tail = []
        if j.state == "running" and j.account_used:
            tail.append(f"on {j.account_used}")
        if j.state in ("done", "failed") and j.runs:
            r = j.runs[-1]
            tail.append(f"5h Δ{r.cost_5h if r.cost_5h is not None else '?'} · 7d Δ{r.cost_7d if r.cost_7d is not None else '?'}")
        if j.error:
            tail.append(j.error[:60])
        if tail:
            line += "  " + dimmed(" · ".join(tail))
        print(line)
    return 0


def _short_folder(folder: str) -> str:
    home = str(Path.home())
    return "~" + folder[len(home):] if folder.startswith(home) else folder


def _show(args, *, store, settings, **_) -> int:
    job = store.get(args.job)
    if args.json:
        print(json.dumps(job.to_json(), indent=2))
        return 0
    print(f"{bolded(job.name)}  {muted(job.id)}  {_STATE_STYLE.get(job.state, str)(job.state)}")
    print(f"  folder     {job.folder}")
    print(f"  account    {job.account}" + (f"  (last run: {job.account_used})" if job.account_used else ""))
    print(f"  model      {job.model or 'default'} · effort {job.effort or 'default'} · {job.permission_mode}")
    if job.allowed_tools:
        print(f"  tools      {' '.join(job.allowed_tools)}")
    print(f"  priority   {job.priority} · estimate {job.estimate_pct:g}% (7d {job.weekly_estimate(settings.weekly_cost_ratio):.1f}%)"
          f" · {'auto' if job.auto else 'manual only'}")
    if job.max_turns:
        print(f"  max turns  {job.max_turns}")
    print(f"  created    {job.created_at}")
    if job.started_at:
        print(f"  started    {job.started_at}" + (f" · finished {job.finished_at}" if job.finished_at else ""))
    if job.session_id:
        print(f"  session    {job.session_id}   (claude --resume {job.session_id})")
    if job.error:
        print(f"  error      {job.error}")
    if job.runs:
        print("  runs")
        for r in job.runs[-5:]:
            print(dimmed(
                f"    {r.started_at} → {r.finished_at}  {r.account}  exit {r.exit_code}"
                f"  5h Δ{r.cost_5h if r.cost_5h is not None else '?'}  7d Δ{r.cost_7d if r.cost_7d is not None else '?'}"
                + (f"  {r.error}" if r.error else "")
            ))
    print("  prompt")
    for line in job.prompt.strip().splitlines()[:12]:
        print(f"    {line}")
    if job.result_text:
        print("  result")
        for line in job.result_text.strip().splitlines()[:12]:
            print(f"    {line}")
    return 0


def _start(args, *, store, runner, switcher, **_) -> int:
    job = store.get(args.job)
    if job.state != "queued":
        error(f"Error: {job.name} is {job.state}; use `jobs retry` first")
        return 1
    if args.account:
        switcher.resolve_account(args.account)
    if args.wait:
        print(f"{accent('Running')} {bolded(job.name)} in {job.folder} …")
        rc = runner.run_worker(job.id, pinned_account=args.account)
        done = store.get(job.id)
        _print_outcome(done)
        return rc
    started = runner.launch(job, account=args.account)
    print(f"{accent('Started')} {bolded(started.name)} {muted(started.short_id)} (worker pid {started.worker_pid})")
    print(dimmed(f"  follow with: ccswap jobs log {started.short_id}"))
    return 0


def _print_outcome(job: Job) -> None:
    if job.state == "done":
        print(f"{accent('Done')} {job.name}")
    else:
        error(f"{job.state}: {job.error or ''}")
    if job.runs:
        r = job.runs[-1]
        print(dimmed(
            f"  {r.duration_s:.0f}s · exit {r.exit_code} · 5h Δ{r.cost_5h if r.cost_5h is not None else '?'}%"
            f" · 7d Δ{r.cost_7d if r.cost_7d is not None else '?'}%"
        ))
    if job.result_text:
        print(job.result_text.strip()[:2000])


def _cancel(args, *, store, runner, **_) -> int:
    job = runner.cancel(store.get(args.job))
    print(f"{accent('Cancelled')} {job.name}")
    return 0


def _retry(args, *, store, runner, **_) -> int:
    job = runner.requeue(store.get(args.job))
    print(f"{accent('Queued')} {job.name} {muted(job.short_id)}")
    return 0


def _remove(args, *, store, **_) -> int:
    job = store.get(args.job)
    if job.state == "running":
        error("Error: cancel the running job first")
        return 1
    store.remove(job.id)
    print(f"Removed {job.name}")
    return 0


def _edit(args, *, store, switcher, **_) -> int:
    job = store.get(args.job)
    fields: dict = {}
    if args.name is not None:
        fields["name"] = args.name
    if args.account is not None:
        if args.account != ACCOUNT_AUTO:
            switcher.resolve_account(args.account)
        fields["account"] = args.account
    if args.model is not None:
        fields["model"] = args.model or None
    if args.effort is not None:
        fields["effort"] = args.effort
    if args.permission_mode is not None:
        fields["permission_mode"] = args.permission_mode
    if args.priority is not None:
        fields["priority"] = args.priority
    if args.estimate is not None:
        fields["estimate_pct"] = args.estimate
    if args.max_turns is not None:
        fields["max_turns"] = args.max_turns
    if args.auto is not None:
        fields["auto"] = args.auto == "on"
    if not fields:
        error("Error: nothing to change")
        return 1
    job = store.update(job.id, **fields)
    print(f"Updated {job.name}: " + ", ".join(f"{k}={v}" for k, v in fields.items()))
    return 0


def _log(args, *, store, **_) -> int:
    job = store.get(args.job)
    stream = store.log_dir(job.id) / "stream.jsonl"
    worker = store.log_dir(job.id) / "worker.log"
    if worker.exists():
        for line in worker.read_text(encoding="utf-8", errors="replace").splitlines()[-8:]:
            print(dimmed(line))
    if not stream.exists():
        print(muted("(no output yet)"))
        return 0
    if args.raw:
        lines = stream.read_text(encoding="utf-8", errors="replace").splitlines()
        print("\n".join(lines[-args.lines:]))
        return 0
    for line in tail_stream_text(stream, max_lines=args.lines):
        print(line)
    return 0


def _capacity(args, *, switcher, settings, store, runner, **_) -> int:
    engine = JobsEngine(switcher, settings, lambda e: None, store=store, runner=runner)
    now = time.time()
    caps = engine.capacities(now=now)
    idle = engine.idle(now=now)
    if args.json:
        print(json.dumps({
            "idleSeconds": None if idle.idle_s == float("inf") else idle.idle_s,
            "quietMinutes": settings.quiet_minutes,
            "accounts": [capacity_to_json(c) for c in caps],
        }, indent=2))
        return 0
    idle_txt = "no interactive sessions" if idle.idle_s == float("inf") else (
        "unknown" if idle.idle_s is None else f"{idle.idle_s / 60:.0f}m"
    )
    quiet = idle.idle_s is not None and idle.idle_s >= settings.quiet_minutes * 60
    print(f"{bolded('Interactive idle')}: {idle_txt}  (need {settings.quiet_minutes:g}m) "
          + (accent("quiet") if quiet else warning_text("busy")))
    if idle.busy:
        print(dimmed("  busy: " + ", ".join(sorted({_short_folder(s.cwd) for s in idle.busy}))))
    for cap in caps:
        head = f"\n{bolded(f'#{cap.number}')} {cap.email}"
        if cap.usage_error:
            head += "  " + warning_text(cap.usage_error)
        elif cap.usage_age_s is not None:
            head += dimmed(f"  usage {cap.usage_age_s / 60:.0f}m old")
        print(head)
        if not cap.windows:
            print(dimmed("  (no usage)"))
            continue
        print(dimmed(f"  {'window':8} {'used':>6} {'left':>6} {'forecast':>9} {'reserve':>8} {'spare':>7}  resets"))
        for name, w in cap.windows.items():
            resets = "?" if w.resets_at is None else datetime.fromtimestamp(w.resets_at).astimezone().strftime("%a %H:%M")
            src = f" ({w.reserve_source.note or w.reserve_source.short_id})" if w.reserve_source else ""
            spare_s = "?" if w.spare_pct is None else f"{w.spare_pct:.0f}%"
            style = accent if (w.spare_pct or 0) >= settings.default_estimate_pct else (
                warning_text if w.spare_pct is not None else str)
            print(
                f"  {name:8} {_p(w.used_pct):>6} {_p(None if w.used_pct is None else 100 - w.used_pct):>6}"
                f" {w.forecast_pct:>8.0f}% {w.reserve_pct:>7.0f}%{'!' if w.blackout else ' '}"
                f"{style(spare_s):>7}  {resets}{dimmed(src)}"
            )
            detail = []
            if w.recent_rate_pct_h is not None:
                detail.append(f"recent {w.recent_rate_pct_h:.1f}%/h → {w.recent_forecast_pct:.0f}%")
            if w.typical_forecast_pct is not None:
                detail.append(f"typical → {w.typical_forecast_pct:.0f}% ({w.samples} samples)")
            if detail:
                print(dimmed("           " + " · ".join(detail)))
    queued = store.queued(auto_only=True)
    if queued:
        print()
        for job in queued[:5]:
            choice = engine.choose(job, caps)
            if choice:
                print(f"  {accent('fits')}  {job.name}: #{choice[0].number} (spare {choice[1]:.0f}%, needs {job.estimate_pct:.0f}%)")
            else:
                print(f"  {muted('wait')}  {job.name}: needs {job.estimate_pct:.0f}%")
    return 0


def _p(v: float | None) -> str:
    return "?" if v is None else f"{v:.0f}%"


def _auto(args, *, switcher, settings, store, runner, **_) -> int:
    def sink(event) -> None:
        if args.json:
            print(json.dumps(event.to_json()), flush=True)
        else:
            print(f"{time.strftime('%H:%M:%S')}  {event.human()}", flush=True)

    engine = JobsEngine(
        switcher, settings, sink, store=store, runner=runner,
        dry_run=args.dry_run, interval_s=args.interval,
    )
    if args.once:
        return engine.tick().value
    signal.signal(signal.SIGTERM, lambda *_: engine.stop())
    if not args.json:
        print(dimmed(
            f"Job scheduler running: every {args.interval:.0f}s, quiet {settings.quiet_minutes:g}m, "
            f"reserve {settings.reserve_pct:g}%/{settings.weekly_reserve_pct:g}%"
            f"{' (dry-run)' if args.dry_run else ''} — Ctrl-C to stop"
        ))
    try:
        return engine.run_loop()
    except KeyboardInterrupt:
        return 0


def _worker(args, *, runner, **_) -> int:
    pinned = os.environ.get("CCSWAP_JOB_ACCOUNT") or None
    return runner.run_worker(args.job_id, pinned_account=pinned)


def _backfill(args, *, switcher, **_) -> int:
    log = Path(args.log).expanduser() if args.log else Path.home() / "Library" / "Logs" / "cswap-auto.log"
    if not log.exists():
        error(f"Error: {log} not found")
        return 1
    data = switcher._get_sequence_data() or {}
    accounts = {
        num: (info.get("email", ""), info.get("organizationUuid", "") or "")
        for num, info in data.get("accounts", {}).items()
    }
    n = backfill_from_auto_log(switcher._usage_store.history, log, accounts)
    print(f"Backfilled {n} samples into {switcher._usage_store.history.path}")
    return 0


def _daemon(args, *, switcher, **_) -> int:
    if sys.platform != "darwin":
        error("Error: `jobs daemon` manages a launchd agent and only works on macOS")
        return 1
    import subprocess

    plist = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    log = Path.home() / "Library" / "Logs" / "ccswap-jobs.log"
    if args.op == "status":
        loaded = subprocess.run(["launchctl", "list", LAUNCHD_LABEL], capture_output=True).returncode == 0
        print(f"{plist}: {'installed' if plist.exists() else 'absent'} · {'loaded' if loaded else 'not loaded'}")
        print(dimmed(f"log: {log}"))
        return 0
    if args.op == "uninstall":
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
        if plist.exists():
            plist.unlink()
        print(f"Removed {LAUNCHD_LABEL}")
        return 0
    ccswap_bin = Path(sys.argv[0]).resolve()
    if ccswap_bin.name != "ccswap":
        # invoked as python -m; fall back to the interpreter
        program = [sys.executable, "-m", "claude_swap"]
    else:
        program = [str(ccswap_bin)]
    path_env = os.environ.get("PATH", "/usr/bin:/bin")
    claude_dir = Path(sys.argv[0]).resolve().parent
    args_xml = "".join(f"\n\t\t<string>{a}</string>" for a in (*program, "jobs", "auto", "--once"))
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
\t<key>Label</key>
\t<string>{LAUNCHD_LABEL}</string>
\t<key>ProgramArguments</key>
\t<array>{args_xml}
\t</array>
\t<key>EnvironmentVariables</key>
\t<dict>
\t\t<key>PATH</key>
\t\t<string>{claude_dir}:{path_env}</string>
\t</dict>
\t<key>StartInterval</key>
\t<integer>{args.interval}</integer>
\t<key>RunAtLoad</key>
\t<true/>
\t<key>StandardOutPath</key>
\t<string>{log}</string>
\t<key>StandardErrorPath</key>
\t<string>{log}</string>
</dict>
</plist>
""")
    subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
    rc = subprocess.run(["launchctl", "load", str(plist)], capture_output=True, text=True)
    if rc.returncode != 0:
        error(f"launchctl load failed: {rc.stderr.strip()}")
        return 1
    print(f"{accent('Installed')} {LAUNCHD_LABEL}: `{' '.join(program)} jobs auto --once` every {args.interval}s")
    print(dimmed(f"  log: {log}"))
    return 0


# -- reserves --------------------------------------------------------------------


def reserves_command(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog=f"{_prog()} reserves",
        description="Date-scheduled safety reserves that auto-started jobs must leave free.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  ccswap reserves add 7d 40 --until 2026-09-16 --note "client deadline"
  ccswap reserves add Fable 100 --from 2026-09-14 --until "2026-09-15 18:00"   # blackout
  ccswap reserves add 5h 30 --account matt@cardboardtoast.io
  ccswap reserves list
  ccswap reserves remove 3f2a
        """,
    )
    parser.add_argument("--debug", action="store_true")
    sub = parser.add_subparsers(dest="action", required=True)
    p_add = sub.add_parser("add", help="Add a reserve")
    p_add.add_argument("window", help="5h, 7d, or a model window name (e.g. Fable)")
    p_add.add_argument("pct", type=float, help="Percent to keep free; 100 = blackout")
    p_add.add_argument("--from", dest="starts", help="Start (YYYY-MM-DD [HH:MM]); default now")
    p_add.add_argument("--until", dest="ends", help="End (YYYY-MM-DD [HH:MM]); default open")
    p_add.add_argument("--account", help="Only this account (email); default all")
    p_add.add_argument("--note", default="")
    p_list = sub.add_parser("list", help="List reserves")
    p_list.add_argument("--json", action="store_true")
    p_rm = sub.add_parser("remove", help="Remove a reserve")
    p_rm.add_argument("reserve")
    sub.add_parser("purge", help="Drop expired reserves")

    args = parser.parse_args(argv)
    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        store = ReserveStore(switcher.backup_dir)
        if args.action == "add":
            r = store.add(make_reserve(
                window=args.window, pct=args.pct, starts=args.starts, ends=args.ends,
                account=args.account, note=args.note,
            ))
            print(f"{accent('Added')} reserve {muted(r.short_id)}: {_describe_reserve(r)}")
        elif args.action == "list":
            rows = store.all()
            if args.json:
                print(json.dumps({"reserves": [r.to_json() for r in rows]}, indent=2))
                return
            if not rows:
                print(dimmed("No reserves."))
            now = time.time()
            for r in rows:
                state = "active" if r.active_at(now) else ("expired" if r.expired_at(now) else "pending")
                print(f"{(accent if state == 'active' else dimmed)(state.ljust(8))} {muted(r.short_id)}  {_describe_reserve(r)}")
        elif args.action == "remove":
            r = store.get(args.reserve)
            store.remove(r.id)
            print(f"Removed reserve {r.short_id}")
        elif args.action == "purge":
            print(f"Purged {store.purge_expired(time.time())} expired reserve(s)")
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)


def _describe_reserve(r) -> str:
    what = "blackout" if r.is_blackout else f"keep {r.pct:g}% free"
    who = f" · {r.account}" if r.account else ""
    when = f"{format_when(r.starts_at)} → {format_when(r.ends_at)}"
    note = f" · {r.note}" if r.note else ""
    return f"{r.window}: {what}{who} · {when}{note}"
