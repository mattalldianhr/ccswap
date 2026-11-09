"""``ccswap resets``: everything known about each account's limit resets.

Two sources, both local by default (no API call unless ``--refresh``):

- the latest stored usage reading per account: every window with its reset
  time, plus the extra fields ``oauth.usage_details`` keeps (idle 5h window,
  limit severities, overage state, per-surface split, unknown API fields);
- the usage history, from which ``resets.detect`` rebuilds past resets and
  flags the ones Anthropic made off schedule.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime

from claude_swap import oauth, resets
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.json_output import error_envelope, usage_to_json
from claude_swap.printer import accent, bolded, dimmed, error, muted
from claude_swap.printer import yellowed as warning_text


def _prog() -> str:
    import os

    return os.path.basename(sys.argv[0]) or "ccswap"


def _clock(iso: str | None) -> str:
    if not iso:
        return "?"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return iso
    return dt.strftime("%a %b %d %H:%M")


def _window_line(label: str, w: dict, note: str = "") -> str:
    body = f"{w['pct']:>4.0f}%"
    cell = oauth.fresh_reset_strings(w)
    due = resets._ts(w.get("resets_at"))
    if due is not None and due <= time.time():
        # A stale reading whose window has since reset: its percentage and
        # reset time describe a window that no longer exists.
        body += f"  {_clock(w['resets_at'])}  " + muted("passed — reading predates the reset")
    elif cell:
        body += f"  {_clock(w.get('resets_at'))}  in {cell[0]}"
    elif w.get("resets_at"):
        body += f"  {_clock(w['resets_at'])}"
    if note:
        body += f"   {muted(note)}"
    return f"    {label:<8}{body}"


def _account_lines(usage: dict) -> list[str]:
    details = usage.get("details") if isinstance(usage.get("details"), dict) else {}
    lines = [dimmed("  next resets")]
    if usage.get("five_hour"):
        lines.append(_window_line("5h", usage["five_hour"]))
    elif details.get("five_hour_idle"):
        lines.append(f"    {'5h':<8}{muted('idle — the next message opens a 5-hour window')}")
    if usage.get("seven_day"):
        started = (details.get("breakdown") or {}).get("window_started_at")
        note = f"window opened {_clock(started)}" if started else ""
        if details.get("seven_day_locked"):
            note = f"LOCKED: {details['seven_day_locked']}  " + note
        lines.append(_window_line("7d", usage["seven_day"], note))
    shares = {s["name"]: s for s in details.get("scoped_shares") or []}
    for w in usage.get("scoped") or []:
        share = shares.get(w["name"])
        note = ""
        if share and share.get("used_pct_of_weekly") is not None:
            note = f"{share['used_pct_of_weekly']}% of weekly so far"
            if share.get("allowance_pct_of_weekly") is not None:
                note += f" · capped at {share['allowance_pct_of_weekly']}% of weekly"
        lines.append(_window_line(w["name"], w, note))
    for w in details.get("extra_windows") or []:
        lines.append(_window_line(w["label"], w, f"LOCKED: {w['locked']}" if w.get("locked") else ""))
    spend = usage.get("spend")
    if spend:
        lines.append(_window_line("$$", spend, f"${spend['used']:,.2f} / ${spend['limit']:,.2f}"))

    limits = details.get("limits") or []
    if limits:
        parts = []
        for lim in limits:
            name = lim.get("model") or lim.get("kind") or "?"
            text = f"{name} {lim.get('pct', '?')}% {lim.get('severity', '?')}"
            if lim.get("active"):
                text += " (binding)"
            parts.append(text)
        lines.append(f"  {dimmed('limits'):<8}  " + " · ".join(parts))

    extra = details.get("extra_usage")
    if isinstance(extra, dict):
        if extra.get("enabled"):
            state = accent("on")
        else:
            why = extra.get("disabled_reason") or ("turned off" if extra.get("user_disabled") else "off")
            state = warning_text(f"off ({str(why).replace('_', ' ')})")
        text = f"overage {state}"
        if "limit" in extra:
            text += f" · ${extra.get('used', 0.0):,.2f} / ${extra['limit']:,.2f} {extra.get('currency', '')}".rstrip()
        if extra.get("limit_reached"):
            text += " · " + warning_text("spend limit reached")
        for key in ("balance", "auto_reload", "daily", "weekly", "severity"):
            if extra.get(key) is not None:
                text += f" · {key}: {json.dumps(extra[key])}"
        lines.append(f"  {dimmed('extra')}     {text}")

    rows = (details.get("breakdown") or {}).get("rows") or []
    if rows:
        lines.append(f"  {dimmed('7d split')}  " + " · ".join(f"{r['name']} {r['pct']}%" for r in rows))

    for key, value in sorted((details.get("other") or {}).items()):
        lines.append(f"  {warning_text('new')}       {key}: {json.dumps(value)}")
    return lines


def resets_command(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog=f"{_prog()} resets",
        description=(
            "Show every limit window's next reset, the extra limit fields the "
            "usage API returns, and past resets rebuilt from usage history, "
            "with off-schedule (early/partial/moved) resets flagged."
        ),
    )
    parser.add_argument("account", nargs="?", help="Account number or email (default: all)")
    parser.add_argument("--days", type=float, default=90.0, help="History to scan (default 90)")
    parser.add_argument("--all", action="store_true", help="Include on-schedule resets")
    parser.add_argument("--refresh", action="store_true", help="Fetch fresh usage first")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    from claude_swap.switcher import ClaudeAccountSwitcher

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        data = switcher._get_sequence_data() or {}
        accounts = data.get("accounts", {})
        nums = sorted(accounts, key=lambda n: int(n) if str(n).isdigit() else 0)
        if args.account:
            nums = [n for n in nums if args.account in (str(n), accounts[n].get("email"))]
            if not nums:
                raise ClaudeSwitchError(f"No account matches {args.account!r}")
        entries = switcher.usage_entries_by_account(fetch=None if args.refresh else set())
        now = time.time()
        events = resets.detect(switcher._usage_store.history.samples(since=now - args.days * 86400))
    except ClaudeSwitchError as e:
        if args.json:
            print(json.dumps(error_envelope(e)))
        else:
            error(str(e))
        sys.exit(1)

    shown = [e for e in events if args.all or e.unscheduled]
    if args.json:
        out = []
        for num in nums:
            email = accounts[num].get("email", "")
            entry = entries.get(str(num))
            usage = entry.last_good if entry is not None else None
            out.append({
                "number": int(num) if str(num).isdigit() else num,
                "email": email,
                "fetchedAt": entry.fetched_at if entry is not None else None,
                "usage": usage_to_json(usage, entry.fetched_at) if isinstance(usage, dict) else None,
                "events": [e.to_json() for e in shown if e.email == email],
            })
        print(json.dumps({"schemaVersion": 1, "days": args.days, "accounts": out}, indent=2))
        return

    for i, num in enumerate(nums):
        email = accounts[num].get("email", "")
        entry = entries.get(str(num))
        head = f"{bolded(f'#{num}')} {email}"
        if entry is not None and entry.fetched_at:
            head += dimmed(f"  reading {max(0, now - entry.fetched_at) / 60:.0f}m old")
        if entry is not None and entry.sentinel:
            head += "  " + warning_text(entry.sentinel)
        print(("\n" if i else "") + head)
        usage = entry.last_good if entry is not None else None
        if isinstance(usage, dict):
            for line in _account_lines(usage):
                print(line)
        else:
            print(dimmed("  no usage reading"))
        mine = [e for e in shown if e.email == email]
        label = "resets" if args.all else "off-schedule resets"
        print(dimmed(f"  {label}, last {args.days:g} days"))
        if not mine:
            print(muted("    none"))
        for e in reversed(mine):
            mark = "⚡" if e.unscheduled else "·"
            print(f"    {mark} {resets.describe(e)}")
