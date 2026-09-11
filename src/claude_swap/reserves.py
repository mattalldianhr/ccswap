"""Date-scheduled safety reserves for the job scheduler.

A *reserve* is the share of a usage window that auto-started jobs must leave
untouched. ``jobs.reservePct`` / ``jobs.weeklyReservePct`` in settings.json
are the always-on floor; this module adds **scheduled** reserves — "keep 40%
of the 7d window free until the 16th", "no jobs at all this weekend" — kept
in ``<backup_root>/reserves.json``.

Each entry names a window (``5h``, ``7d``, or a scoped model window such as
``Fable``), a percentage, a start and end (ISO local datetimes; either may
be open), and optionally one account (email) — otherwise it applies to all.
``pct = 100`` is a blackout: nothing auto-starts against that window while
the entry is active. The effective reserve for a window at a moment is the
**maximum** of the settings floor and every active entry, so overlapping
entries never cancel each other out.

Windows are matched case-insensitively; scoped windows match the usage
API's display name (``Fable``), the same spelling ``autoswitch.model`` uses.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.locking import FileLock
from claude_swap.settings import atomic_write_json

RESERVES_FILENAME = "reserves.json"
RESERVES_SCHEMA_VERSION = 1

WINDOW_5H = "5h"
WINDOW_7D = "7d"
BASE_WINDOWS = (WINDOW_5H, WINDOW_7D)


class ReserveError(ClaudeSwitchError):
    """A reserve entry could not be parsed or stored."""


def normalize_window(name: str) -> str:
    """Canonical window key: ``5h``/``7d`` lowercased, scoped names as-is."""
    key = name.strip()
    low = key.lower()
    if low in ("5h", "five_hour", "5hour", "5-hour"):
        return WINDOW_5H
    if low in ("7d", "seven_day", "7day", "7-day", "weekly", "week"):
        return WINDOW_7D
    if not key:
        raise ReserveError("Window is required (5h, 7d, or a model name such as Fable)")
    return key


def parse_when(value: str | None) -> float | None:
    """Local ISO datetime or date → POSIX timestamp; None/empty → None (open)."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).astimezone().timestamp()
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ReserveError(
            f"Could not parse '{value}'; use YYYY-MM-DD or YYYY-MM-DD HH:MM"
        ) from None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.timestamp()


def format_when(ts: float | None) -> str:
    if ts is None:
        return "open"
    return datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M")


@dataclass(frozen=True)
class Reserve:
    id: str
    window: str
    pct: float
    starts_at: float | None = None  # POSIX; None = already active
    ends_at: float | None = None  # POSIX; None = never expires
    account: str | None = None  # email; None = every account
    note: str = ""

    @property
    def short_id(self) -> str:
        return self.id[:8]

    @property
    def is_blackout(self) -> bool:
        return self.pct >= 100.0

    def active_at(self, now: float) -> bool:
        if self.starts_at is not None and now < self.starts_at:
            return False
        if self.ends_at is not None and now >= self.ends_at:
            return False
        return True

    def expired_at(self, now: float) -> bool:
        return self.ends_at is not None and now >= self.ends_at

    def applies_to(self, email: str | None) -> bool:
        return self.account is None or email is None or self.account.lower() == email.lower()

    def matches_window(self, window: str) -> bool:
        return self.window.lower() == normalize_window(window).lower()

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: dict) -> "Reserve | None":
        try:
            rid = str(raw["id"])
            window = normalize_window(str(raw["window"]))
            pct = float(raw["pct"])
        except (KeyError, TypeError, ValueError, ReserveError):
            return None
        starts = raw.get("starts_at")
        ends = raw.get("ends_at")
        return cls(
            id=rid,
            window=window,
            pct=max(0.0, min(100.0, pct)),
            starts_at=float(starts) if isinstance(starts, (int, float)) and not isinstance(starts, bool) else None,
            ends_at=float(ends) if isinstance(ends, (int, float)) and not isinstance(ends, bool) else None,
            account=raw.get("account") if isinstance(raw.get("account"), str) and raw.get("account") else None,
            note=str(raw.get("note") or ""),
        )


def make_reserve(
    *,
    window: str,
    pct: float,
    starts: str | None = None,
    ends: str | None = None,
    account: str | None = None,
    note: str = "",
    reserve_id: str | None = None,
) -> Reserve:
    """Validate user-entered fields into a :class:`Reserve`."""
    if not 0.0 <= pct <= 100.0:
        raise ReserveError("Reserve pct must be between 0 and 100")
    starts_ts = parse_when(starts)
    ends_ts = parse_when(ends)
    if starts_ts is not None and ends_ts is not None and ends_ts <= starts_ts:
        raise ReserveError("End must be after start")
    return Reserve(
        id=reserve_id or uuid.uuid4().hex[:12],
        window=normalize_window(window),
        pct=float(pct),
        starts_at=starts_ts,
        ends_at=ends_ts,
        account=account.strip() or None if account else None,
        note=note.strip(),
    )


class ReserveStore:
    """Reads and writes ``<backup_dir>/reserves.json`` under ``.reserves.lock``."""

    def __init__(self, backup_dir: Path):
        self.backup_dir = Path(backup_dir)
        self.path = self.backup_dir / RESERVES_FILENAME
        self._lock_path = self.backup_dir / ".reserves.lock"

    def _lock(self) -> FileLock:
        return FileLock(self._lock_path)

    def _read(self) -> dict[str, Reserve]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {}
        if not isinstance(raw, dict) or raw.get("schemaVersion") != RESERVES_SCHEMA_VERSION:
            return {}
        out: dict[str, Reserve] = {}
        for item in raw.get("reserves") or []:
            if isinstance(item, dict):
                r = Reserve.from_json(item)
                if r is not None:
                    out[r.id] = r
        return out

    def _write(self, reserves: dict[str, Reserve]) -> None:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            self.path,
            {
                "schemaVersion": RESERVES_SCHEMA_VERSION,
                "reserves": [r.to_json() for r in reserves.values()],
            },
        )

    def all(self) -> list[Reserve]:
        """Every entry, soonest-ending first (open-ended last)."""
        return sorted(
            self._read().values(),
            key=lambda r: (r.ends_at is None, r.ends_at or 0.0, r.starts_at or 0.0, r.id),
        )

    def get(self, identifier: str) -> Reserve:
        reserves = self._read()
        if identifier in reserves:
            return reserves[identifier]
        prefix = [r for r in reserves.values() if r.id.startswith(identifier)]
        if len(prefix) == 1:
            return prefix[0]
        if len(prefix) > 1:
            raise ReserveError(f"'{identifier}' matches more than one reserve")
        raise ReserveError(f"No reserve matches '{identifier}'")

    def add(self, reserve: Reserve) -> Reserve:
        with self._lock():
            reserves = self._read()
            reserves[reserve.id] = reserve
            self._write(reserves)
        return reserve

    def update(self, reserve_id: str, **fields) -> Reserve:
        with self._lock():
            reserves = self._read()
            current = reserves.get(reserve_id)
            if current is None:
                raise ReserveError(f"No reserve with id {reserve_id}")
            updated = replace(current, **fields)
            reserves[reserve_id] = updated
            self._write(reserves)
        return updated

    def remove(self, reserve_id: str) -> bool:
        with self._lock():
            reserves = self._read()
            if reserves.pop(reserve_id, None) is None:
                return False
            self._write(reserves)
        return True

    def purge_expired(self, now: float) -> int:
        with self._lock():
            reserves = self._read()
            keep = {rid: r for rid, r in reserves.items() if not r.expired_at(now)}
            removed = len(reserves) - len(keep)
            if removed:
                self._write(keep)
        return removed

    # -- evaluation -----------------------------------------------------------

    def active(self, now: float, *, email: str | None = None) -> list[Reserve]:
        return [r for r in self.all() if r.active_at(now) and r.applies_to(email)]

    def effective(
        self, window: str, *, now: float, email: str | None, floor: float
    ) -> tuple[float, Reserve | None]:
        """(effective reserve pct, the entry that set it or None for the floor)."""
        best = floor
        source: Reserve | None = None
        for r in self.active(now, email=email):
            if r.matches_window(window) and r.pct > best:
                best, source = r.pct, r
        return best, source
