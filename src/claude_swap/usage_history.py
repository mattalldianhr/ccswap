"""Append-only usage history: one line per successful usage fetch.

The usage store keeps only the latest snapshot per account, which is all the
switcher needs. The job scheduler needs a *trend*: how fast the user burns
the 5h and 7d windows, by time of day and weekday, so it can forecast how
much of the remaining window the user will consume themselves before it
hands the rest to a queued job. This module records that trend as JSONL in
``<backup_root>/cache/usage_history.jsonl`` and exposes the sampled series.

Every line::

    {"t": <fetched_at epoch>, "email": ..., "org": ..., "src": "poll"|"backfill",
     "5h": pct|null, "5h_reset": iso|null, "7d": pct|null, "7d_reset": iso|null,
     "scoped": {"Fable": pct, ...}}

``UsageStore.record`` appends after every accepted successful fetch; the
append is best-effort and never propagates an error into the store. Lines
older than ``max_age_s`` are pruned opportunistically on append so the file
stays bounded (a 2-minute cadence across two accounts is ~4 MB/week).

``backfill_from_auto_log`` seeds the file from the human-readable lines the
``ccswap auto`` daemon writes (``Account-2 (a@x): 67% used (switch at 85%) |
others: #1: 5h 0% · 7d 83%``). Those lines carry a clock time but no date,
so dates are reconstructed by walking backwards from the file's mtime and
treating every backwards jump in clock time as a midnight rollover. The
active account's line reports only its *binding* pct (max of 5h/7d), so it
is stored as the 5h value only when it exceeds that account's most recently
seen 7d value — otherwise the 5h reading is unknown and the line is skipped
for that account.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from claude_swap.fsutil import replace_with_retry
from claude_swap.locking import FileLock

HISTORY_FILENAME = "usage_history.jsonl"
DEFAULT_MAX_AGE_S = 12 * 7 * 86400.0  # 12 weeks: the widest lookback setting
_PRUNE_EVERY = 500  # appends between opportunistic prunes


@dataclass(frozen=True)
class Sample:
    """One usage reading for one account."""

    t: float
    email: str
    org: str
    five_hour: float | None
    five_hour_reset: str | None
    seven_day: float | None
    seven_day_reset: str | None
    scoped: dict[str, float] = field(default_factory=dict)
    src: str = "poll"

    def to_json(self) -> dict:
        return {
            "t": self.t,
            "email": self.email,
            "org": self.org,
            "src": self.src,
            "5h": self.five_hour,
            "5h_reset": self.five_hour_reset,
            "7d": self.seven_day,
            "7d_reset": self.seven_day_reset,
            "scoped": dict(self.scoped),
        }

    @classmethod
    def from_json(cls, raw: dict) -> "Sample | None":
        try:
            t = float(raw["t"])
            email = str(raw["email"])
        except (KeyError, TypeError, ValueError):
            return None
        scoped_raw = raw.get("scoped")
        scoped: dict[str, float] = {}
        if isinstance(scoped_raw, dict):
            for name, pct in scoped_raw.items():
                if isinstance(pct, (int, float)) and not isinstance(pct, bool):
                    scoped[str(name)] = float(pct)
        return cls(
            t=t,
            email=email,
            org=str(raw.get("org") or ""),
            five_hour=_pct_or_none(raw.get("5h")),
            five_hour_reset=raw.get("5h_reset") if isinstance(raw.get("5h_reset"), str) else None,
            seven_day=_pct_or_none(raw.get("7d")),
            seven_day_reset=raw.get("7d_reset") if isinstance(raw.get("7d_reset"), str) else None,
            scoped=scoped,
            src=str(raw.get("src") or "poll"),
        )

    @classmethod
    def from_last_good(
        cls, email: str, org: str, last_good: dict, fetched_at: float, *, src: str = "poll"
    ) -> "Sample":
        five = last_good.get("five_hour") if isinstance(last_good, dict) else None
        seven = last_good.get("seven_day") if isinstance(last_good, dict) else None
        scoped: dict[str, float] = {}
        for win in (last_good.get("scoped") or []) if isinstance(last_good, dict) else []:
            if not isinstance(win, dict):
                continue
            name = win.get("name")
            pct = _pct_or_none(win.get("pct"))
            if isinstance(name, str) and pct is not None:
                scoped[name] = pct
        return cls(
            t=fetched_at,
            email=email,
            org=org,
            five_hour=_pct_or_none(five.get("pct")) if isinstance(five, dict) else None,
            five_hour_reset=five.get("resets_at") if isinstance(five, dict) and isinstance(five.get("resets_at"), str) else None,
            seven_day=_pct_or_none(seven.get("pct")) if isinstance(seven, dict) else None,
            seven_day_reset=seven.get("resets_at") if isinstance(seven, dict) and isinstance(seven.get("resets_at"), str) else None,
            scoped=scoped,
            src=src,
        )


def _pct_or_none(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


class UsageHistory:
    """Reads and appends ``<cache_dir>/usage_history.jsonl``."""

    def __init__(self, cache_dir: Path, *, max_age_s: float = DEFAULT_MAX_AGE_S):
        self.path = Path(cache_dir) / HISTORY_FILENAME
        self._lock_path = Path(cache_dir) / ".usage_history.lock"
        self.max_age_s = max_age_s
        self._appends = 0

    def _lock(self) -> FileLock:
        return FileLock(self._lock_path)

    # -- write ----------------------------------------------------------------

    def append(self, sample: Sample) -> bool:
        """Append one sample. Returns False when it duplicates the account's
        last line (same ``t``) or on any I/O error — never raises."""
        try:
            with self._lock():
                last = self._last_t_for(sample.email)
                if last is not None and last >= sample.t:
                    return False
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(sample.to_json(), separators=(",", ":")) + "\n")
                self._appends += 1
                if self._appends % _PRUNE_EVERY == 0:
                    self._prune_locked(time.time() - self.max_age_s)
            return True
        except Exception:  # noqa: BLE001 - history is best-effort
            return False

    def append_many(self, samples: Iterable[Sample]) -> int:
        """Append samples in ``t`` order, skipping ones at or before each
        account's last line. Returns how many were written."""
        written = 0
        try:
            with self._lock():
                last_by_email: dict[str, float] = {}
                for s in self._iter_locked():
                    prev = last_by_email.get(s.email)
                    if prev is None or s.t > prev:
                        last_by_email[s.email] = s.t
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    for sample in sorted(samples, key=lambda s: s.t):
                        prev = last_by_email.get(sample.email)
                        if prev is not None and prev >= sample.t:
                            continue
                        fh.write(json.dumps(sample.to_json(), separators=(",", ":")) + "\n")
                        last_by_email[sample.email] = sample.t
                        written += 1
        except Exception:  # noqa: BLE001
            return written
        return written

    def prune(self, max_age_s: float | None = None) -> int:
        """Drop lines older than ``max_age_s``; returns how many were removed."""
        cutoff = time.time() - (self.max_age_s if max_age_s is None else max_age_s)
        with self._lock():
            return self._prune_locked(cutoff)

    def _prune_locked(self, cutoff: float) -> int:
        kept: list[str] = []
        removed = 0
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        t = float(json.loads(line)["t"])
                    except (ValueError, KeyError, TypeError):
                        removed += 1
                        continue
                    if t < cutoff:
                        removed += 1
                    else:
                        kept.append(line if line.endswith("\n") else line + "\n")
        except FileNotFoundError:
            return 0
        if removed == 0:
            return 0
        tmp = self.path.with_suffix(".jsonl.tmp")
        tmp.write_text("".join(kept), encoding="utf-8")
        replace_with_retry(tmp, self.path)
        return removed

    def _last_t_for(self, email: str) -> float | None:
        """``t`` of the newest line for ``email``, reading from the tail."""
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return None
        if size == 0:
            return None
        chunk = min(size, 64 * 1024)
        with self.path.open("rb") as fh:
            fh.seek(size - chunk)
            tail = fh.read(chunk).decode("utf-8", errors="replace")
        for line in reversed(tail.splitlines()):
            try:
                raw = json.loads(line)
            except ValueError:
                continue
            if raw.get("email") == email:
                try:
                    return float(raw["t"])
                except (KeyError, TypeError, ValueError):
                    return None
        return None

    # -- read -----------------------------------------------------------------

    def _iter_locked(self) -> Iterable[Sample]:
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(raw, dict):
                        sample = Sample.from_json(raw)
                        if sample is not None:
                            yield sample
        except FileNotFoundError:
            return

    def samples(
        self, email: str | None = None, *, since: float | None = None
    ) -> list[Sample]:
        """Samples (oldest first), optionally for one account and/or after
        ``since``. Read without the lock: appends are whole lines and a torn
        trailing line is skipped by the parser."""
        out: list[Sample] = []
        for s in self._iter_locked():
            if email is not None and s.email != email:
                continue
            if since is not None and s.t < since:
                continue
            out.append(s)
        out.sort(key=lambda s: s.t)
        return out


# -- backfill from the auto daemon's log -------------------------------------

_LINE_RE = re.compile(
    r"^(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2})\s+"
    r"Account-(?P<num>\d+) \((?P<email>[^)]+)\): (?P<binding>\d+(?:\.\d+)?)% used"
    r".*?\| others: (?P<others>.*)$"
)
_OTHER_RE = re.compile(
    r"#(?P<num>\d+): 5h (?P<five>\d+(?:\.\d+)?)% · 7d (?P<seven>\d+(?:\.\d+)?)%"
)


def parse_auto_log(
    lines: Iterable[str],
    accounts: dict[str, tuple[str, str]],
    *,
    end_date: datetime,
) -> list[Sample]:
    """Reconstruct samples from ``ccswap auto`` log lines.

    ``accounts`` maps slot number → ``(email, org)`` so ``#1``-style
    references in the "others" clause resolve to an identity. ``end_date``
    is the local date of the *last* line (normally the log's mtime); every
    backwards clock jump walking the file bottom-up moves one day earlier.
    Output is oldest-first and deduplicated per (email, t).
    """
    parsed: list[tuple[int, str, str, float | None, float | None, float | None]] = []
    # (seconds-of-day, active_num, active_email, binding, others...) kept per line
    rows: list[dict] = []
    for line in lines:
        m = _LINE_RE.match(line.rstrip("\n"))
        if not m:
            continue
        sod = int(m["h"]) * 3600 + int(m["m"]) * 60 + int(m["s"])
        others = {
            om["num"]: (float(om["five"]), float(om["seven"]))
            for om in _OTHER_RE.finditer(m["others"])
        }
        rows.append({
            "sod": sod,
            "num": m["num"],
            "email": m["email"],
            "binding": float(m["binding"]),
            "others": others,
        })
    del parsed

    # Assign dates walking backwards from the end.
    day = end_date.replace(hour=0, minute=0, second=0, microsecond=0)
    prev_sod: int | None = None
    stamped: list[tuple[float, dict]] = []
    for row in reversed(rows):
        if prev_sod is not None and row["sod"] > prev_sod:
            day -= timedelta(days=1)
        prev_sod = row["sod"]
        t = (day + timedelta(seconds=row["sod"])).timestamp()
        stamped.append((t, row))
    stamped.reverse()

    samples: dict[tuple[str, float], Sample] = {}
    last_seven: dict[str, float] = {}  # email -> most recently seen 7d pct
    for t, row in stamped:
        for num, (five, seven) in row["others"].items():
            ident = accounts.get(num)
            if ident is None:
                continue
            email, org = ident
            last_seven[email] = seven
            samples[(email, t)] = Sample(
                t=t, email=email, org=org,
                five_hour=five, five_hour_reset=None,
                seven_day=seven, seven_day_reset=None,
                src="backfill",
            )
        ident = accounts.get(row["num"])
        email = ident[0] if ident else row["email"]
        org = ident[1] if ident else ""
        seven_known = last_seven.get(email)
        binding = row["binding"]
        # The binding pct is the 5h reading only when it beats the weekly one.
        if seven_known is not None and binding > seven_known:
            samples[(email, t)] = Sample(
                t=t, email=email, org=org,
                five_hour=binding, five_hour_reset=None,
                seven_day=seven_known, seven_day_reset=None,
                src="backfill",
            )
        elif seven_known is None and binding == 0.0:
            samples[(email, t)] = Sample(
                t=t, email=email, org=org,
                five_hour=0.0, five_hour_reset=None,
                seven_day=None, seven_day_reset=None,
                src="backfill",
            )
    return sorted(samples.values(), key=lambda s: (s.t, s.email))


def backfill_from_auto_log(
    history: UsageHistory,
    log_path: Path,
    accounts: dict[str, tuple[str, str]],
) -> int:
    """Seed ``history`` from a ``ccswap auto`` log file; returns lines written."""
    try:
        mtime = os.path.getmtime(log_path)
        text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    end_date = datetime.fromtimestamp(mtime).astimezone()
    samples = parse_auto_log(text.splitlines(), accounts, end_date=end_date)
    return history.append_many(samples)
