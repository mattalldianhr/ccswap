"""Spare-capacity forecast: how much of each usage window a queued job may
take without crowding out the user.

For one account and one window the arithmetic is::

    remaining  = 100 - used
    forecast   = the user's own expected burn through the window's reset
    reserve    = max(settings floor, active scheduled reserves)   (reserves.py)
    spare      = remaining - forecast - reserve

A job fits when ``spare >= its estimate`` for **every** window it touches:
the 5h and 7d windows always, plus the scoped model window when the job
names a model that has one (``Fable`` jobs check the Fable weekly window).

The forecast is the larger of two views of the user's own burn, both read
from ``usage_history.jsonl`` (usage_history.py):

* **Recent rate** — pct/hour over the last ``RECENT_WINDOW_S`` (resets
  excluded), extrapolated over the time left in the window. Catches "the
  user is mid-session right now".
* **Typical burn** — for each remaining hour of the window, the median
  pct/hour the user burned in that same weekday+hour slot over the last
  ``lookback_weeks``. Catches "it's 8 am, the user always starts at 9".

Both are linear extrapolations with wide error bars; that is what the
reserve and the quiet-period gate (jobs_engine.py) are for. Every number
is exposed in :class:`WindowCapacity` so the TUI and ``ccswap jobs
capacity`` can show *why* a job did or did not start.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from claude_swap.pace import WEEKLY_PERIOD_S
from claude_swap.reserves import WINDOW_5H, WINDOW_7D, Reserve, ReserveStore
from claude_swap.settings import JobsSettings
from claude_swap.usage_history import Sample, UsageHistory

FIVE_HOUR_PERIOD_S = 5 * 3600.0
# The 5h window is a THROTTLE, not a budget. It refills 33 times before the
# weekly window does, so nothing is conserved by leaving it unused — its only
# job is to keep a job from colliding with the user right now. Forecasting it
# across its whole remaining span (as the weekly window deserves) made its
# spare swing negative whenever Matt was working, and since a job must clear
# both windows, that shrank the startable moments to the intersection of
# "quiet" and "early in the week".
THROTTLE_HORIZON_S = 45 * 60.0
RECENT_WINDOW_S = 3600.0  # burn-rate lookback for the "right now" view
MIN_RECENT_SPAN_S = 600.0  # need at least this much span to trust a rate
# Hours of a window with no history at all assume this pct/h of own burn —
# deliberately non-zero so a fresh install does not read as "user never
# works".
DEFAULT_TYPICAL_PCT_PER_H = {WINDOW_5H: 4.0, WINDOW_7D: 0.4}


@dataclass(frozen=True)
class WindowCapacity:
    window: str  # "5h" | "7d" | scoped name
    used_pct: float | None
    resets_at: float | None  # POSIX
    remaining_s: float | None
    recent_rate_pct_h: float | None
    recent_forecast_pct: float | None
    typical_forecast_pct: float | None
    forecast_pct: float
    reserve_pct: float
    reserve_source: Reserve | None
    spare_pct: float | None  # None when used is unknown
    samples: int  # history samples that informed the forecast

    @property
    def blackout(self) -> bool:
        return self.reserve_pct >= 100.0

    def fits(self, estimate_pct: float) -> bool:
        return self.spare_pct is not None and not self.blackout and self.spare_pct >= estimate_pct


@dataclass(frozen=True)
class AccountCapacity:
    number: str
    email: str
    windows: dict[str, WindowCapacity] = field(default_factory=dict)
    usage_age_s: float | None = None
    usage_error: str | None = None

    def window(self, name: str) -> WindowCapacity | None:
        for key, cap in self.windows.items():
            if key.lower() == name.lower():
                return cap
        return None

    def spare_for(self, windows: tuple[str, ...]) -> float | None:
        """The binding (smallest) spare across ``windows``; None if any is unknown."""
        spares: list[float] = []
        for name in windows:
            cap = self.window(name)
            if cap is None or cap.spare_pct is None:
                return None
            if cap.blackout:
                return -100.0
            spares.append(cap.spare_pct)
        return min(spares) if spares else None


# -- forecasting ---------------------------------------------------------------


def _window_series(samples: list[Sample], window: str) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for s in samples:
        if window == WINDOW_5H:
            v = s.five_hour
        elif window == WINDOW_7D:
            v = s.seven_day
        else:
            v = s.scoped.get(window)
            if v is None:
                for name, pct in s.scoped.items():
                    if name.lower() == window.lower():
                        v = pct
                        break
        if v is not None:
            out.append((s.t, v))
    return out


def _increments(series: list[tuple[float, float]]) -> list[tuple[float, float, float]]:
    """(t0, t1, +pct) between consecutive samples; drops (resets) are skipped."""
    inc: list[tuple[float, float, float]] = []
    for (t0, v0), (t1, v1) in zip(series, series[1:]):
        if t1 <= t0:
            continue
        d = v1 - v0
        if d < 0:
            continue  # window reset between samples
        inc.append((t0, t1, d))
    return inc


def recent_rate(series: list[tuple[float, float]], *, now: float, span_s: float = RECENT_WINDOW_S) -> float | None:
    """pct/hour over the last ``span_s``; None without enough span."""
    recent = [x for x in series if x[0] >= now - span_s]
    if len(recent) < 2:
        return None
    span = recent[-1][0] - recent[0][0]
    if span < MIN_RECENT_SPAN_S:
        return None
    gained = sum(d for _t0, _t1, d in _increments(recent))
    return gained / (span / 3600.0)


def hourly_profile(series: list[tuple[float, float]], *, lookback_s: float, now: float) -> dict[tuple[int, int], list[float]]:
    """(weekday, hour) → list of pct/hour observations over the lookback."""
    buckets: dict[tuple[int, int], list[float]] = defaultdict(list)
    hour_gain: dict[tuple[int, int, int], float] = defaultdict(float)  # (day-ordinal, weekday, hour) → pct
    hour_seen: set[tuple[int, int, int]] = set()
    for t0, t1, d in _increments([x for x in series if x[0] >= now - lookback_s]):
        mid = datetime.fromtimestamp((t0 + t1) / 2).astimezone()
        key = (mid.toordinal(), mid.weekday(), mid.hour)
        hour_gain[key] += d
        hour_seen.add(key)
    for (_ord, wd, hr), gain in hour_gain.items():
        buckets[(wd, hr)].append(gain)
    return buckets


def typical_forecast(
    profile: dict[tuple[int, int], list[float]],
    *,
    now: float,
    remaining_s: float,
    default_pct_h: float,
) -> tuple[float, int]:
    """Sum of median pct/h over each remaining hour slot; also the sample count."""
    if remaining_s <= 0:
        return 0.0, 0
    total = 0.0
    used = 0
    t = now
    end = now + remaining_s
    while t < end:
        dt = datetime.fromtimestamp(t).astimezone()
        slot_end = (dt.replace(minute=0, second=0, microsecond=0).timestamp() + 3600.0)
        frac = (min(slot_end, end) - t) / 3600.0
        obs = profile.get((dt.weekday(), dt.hour))
        if obs:
            rate = statistics.median(obs)
            used += len(obs)
        else:
            rate = default_pct_h
        total += rate * frac
        t = slot_end
    return total, used


# NOTE (2026-09-15): a `paced_reserve` that relaxed the weekly floor toward
# reset was built and then removed. The idea is sound — unspent weekly budget
# is waste — but it cannot be calibrated yet: the history holds four complete
# cycles, and an analysis that seemed to show front-loading turned out to be an
# artifact of averaging two accounts whose resets are 16h apart onto one
# elapsed axis. `cycles.py` now records each completed cycle so the shape can
# come from measurement rather than a guess.


def blend_forecast(
    recent_pct: float | None,
    typical_pct: float | None,
    *,
    observed_s: float,
    full_confidence_s: float = RECENT_WINDOW_S,
) -> float:
    """Combine the two forecasts, weighted by how much evidence the recent
    rate actually has.

    Taking ``max`` let one busy hour dominate: ten minutes into a cycle, a
    heavy burst projected across the remaining time produced a near-certain
    "you will use it all" from almost no evidence. Taking ``min`` would be
    worse — measured over 36 of Matt's 5h cycles the median peak is 82% and
    half exceed 80%, so the pessimistic view is usually right *eventually*.

    So weight the recent rate by the fraction of ``full_confidence_s`` it has
    been observed over, and fall back to the typical profile for the rest.
    With a full hour of evidence the recent rate stands alone; with six
    minutes it contributes a tenth.
    """
    if recent_pct is None:
        return typical_pct or 0.0
    if typical_pct is None:
        return recent_pct
    weight = max(0.0, min(1.0, observed_s / full_confidence_s)) if full_confidence_s > 0 else 1.0
    return weight * recent_pct + (1.0 - weight) * typical_pct


def _reset_ts(value: str | None) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def window_capacity(
    *,
    window: str,
    used_pct: float | None,
    resets_at: str | None,
    now: float,
    samples: list[Sample],
    settings: JobsSettings,
    reserves: ReserveStore | None,
    email: str | None,
    floor_pct: float,
) -> WindowCapacity:
    period = FIVE_HOUR_PERIOD_S if window == WINDOW_5H else WEEKLY_PERIOD_S
    reset_ts = _reset_ts(resets_at)
    remaining_s: float | None = None
    if reset_ts is not None:
        remaining_s = max(0.0, reset_ts - now)
        if remaining_s > period:
            remaining_s = period
    # How far into this cycle we are: the recent rate is only as trustworthy
    # as the span it was measured over.
    elapsed_s = None if remaining_s is None else max(0.0, period - remaining_s)
    # A throttle only needs to know the next stretch, not the whole window.
    horizon_s = remaining_s
    if window == WINDOW_5H and remaining_s is not None:
        horizon_s = min(remaining_s, THROTTLE_HORIZON_S)
    series = _window_series(samples, window)
    rate = recent_rate(series, now=now)
    recent_fc = None
    if rate is not None and horizon_s is not None:
        recent_fc = rate * (horizon_s / 3600.0)
    typical_fc: float | None = None
    n = 0
    if remaining_s is not None:
        profile = hourly_profile(
            series, lookback_s=settings.lookback_weeks * WEEKLY_PERIOD_S, now=now
        )
        default_rate = DEFAULT_TYPICAL_PCT_PER_H.get(window, DEFAULT_TYPICAL_PCT_PER_H[WINDOW_7D])
        typical_fc, n = typical_forecast(
            profile, now=now, remaining_s=horizon_s, default_pct_h=default_rate
        )
    forecast = blend_forecast(
        recent_fc, typical_fc, observed_s=min(RECENT_WINDOW_S, elapsed_s or 0.0)
    )
    if used_pct is not None:
        forecast = min(forecast, max(0.0, 100.0 - used_pct))

    if reserves is not None:
        reserve, source = reserves.effective(window, now=now, email=email, floor=floor_pct)
    else:
        reserve, source = floor_pct, None

    spare: float | None = None
    if used_pct is not None:
        spare = 100.0 - used_pct - forecast - reserve
    return WindowCapacity(
        window=window,
        used_pct=used_pct,
        resets_at=reset_ts,
        remaining_s=remaining_s,
        recent_rate_pct_h=rate,
        recent_forecast_pct=recent_fc,
        typical_forecast_pct=typical_fc,
        forecast_pct=forecast,
        reserve_pct=reserve,
        reserve_source=source,
        spare_pct=spare,
        samples=n,
    )


def account_capacity(
    *,
    number: str,
    email: str,
    entry,
    now: float,
    history: UsageHistory,
    settings: JobsSettings,
    reserves: ReserveStore | None,
) -> AccountCapacity:
    """Capacity for every window an account's latest usage entry reports."""
    last_good = getattr(entry, "last_good", None)
    age = getattr(entry, "age_s", None)
    err = getattr(entry, "last_error", None) or getattr(entry, "sentinel", None)
    samples = history.samples(email, since=now - settings.lookback_weeks * WEEKLY_PERIOD_S)
    windows: dict[str, WindowCapacity] = {}
    if not isinstance(last_good, dict):
        return AccountCapacity(number=number, email=email, windows={}, usage_age_s=age, usage_error=err)

    def pct(win: object) -> float | None:
        if isinstance(win, dict) and isinstance(win.get("pct"), (int, float)) and not isinstance(win.get("pct"), bool):
            return float(win["pct"])
        return None

    def reset(win: object) -> str | None:
        return win.get("resets_at") if isinstance(win, dict) and isinstance(win.get("resets_at"), str) else None

    five = last_good.get("five_hour")
    seven = last_good.get("seven_day")
    windows[WINDOW_5H] = window_capacity(
        window=WINDOW_5H, used_pct=pct(five), resets_at=reset(five), now=now,
        samples=samples, settings=settings, reserves=reserves, email=email,
        floor_pct=settings.reserve_pct,
    )
    windows[WINDOW_7D] = window_capacity(
        window=WINDOW_7D, used_pct=pct(seven), resets_at=reset(seven), now=now,
        samples=samples, settings=settings, reserves=reserves, email=email,
        floor_pct=settings.weekly_reserve_pct,
    )
    for win in last_good.get("scoped") or []:
        if not isinstance(win, dict) or not isinstance(win.get("name"), str):
            continue
        name = win["name"]
        cap = window_capacity(
            window=name, used_pct=pct(win), resets_at=reset(win), now=now,
            samples=samples, settings=settings, reserves=reserves, email=email,
            floor_pct=settings.weekly_reserve_pct,
        )
        # A scoped window with no history of its own tracks the weekly
        # window: scale the 7d forecast by how much of the weekly burn this
        # model accounts for (its used share), instead of a blind default.
        if cap.samples == 0 and cap.recent_rate_pct_h is None:
            weekly = windows[WINDOW_7D]
            share = 1.0
            if cap.used_pct is not None and weekly.used_pct:
                share = min(1.5, cap.used_pct / weekly.used_pct)
            forecast = weekly.forecast_pct * share
            if cap.used_pct is not None:
                forecast = min(forecast, max(0.0, 100.0 - cap.used_pct))
            spare = None if cap.used_pct is None else 100.0 - cap.used_pct - forecast - cap.reserve_pct
            cap = WindowCapacity(
                window=cap.window, used_pct=cap.used_pct, resets_at=cap.resets_at,
                remaining_s=cap.remaining_s, recent_rate_pct_h=None,
                recent_forecast_pct=None, typical_forecast_pct=forecast,
                forecast_pct=forecast, reserve_pct=cap.reserve_pct,
                reserve_source=cap.reserve_source, spare_pct=spare, samples=0,
            )
        windows[name] = cap
    return AccountCapacity(number=number, email=email, windows=windows, usage_age_s=age, usage_error=err)


def windows_for_job(model: str | None, available: dict[str, WindowCapacity]) -> tuple[str, ...]:
    """Windows a job must fit: 5h, 7d, and the scoped window matching its model."""
    out: list[str] = [WINDOW_5H, WINDOW_7D]
    if model:
        m = model.lower()
        for name in available:
            if name in (WINDOW_5H, WINDOW_7D):
                continue
            if name.lower() in m or m in name.lower():
                out.append(name)
    return tuple(out)


def utc_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


# -- pooled capacity -----------------------------------------------------------


@dataclass(frozen=True)
class PooledWindow:
    """One window's capacity across every account, treated as one budget.

    Per-account accounting double-counts two things. The reserve is held on
    each account, but the user spends from one account at a time, so holding
    it twice reserves capacity that was never at risk. The burn forecast has
    the same shape: the user has one appetite, and requiring every account to
    survive the *whole* forecast independently is the same error again.

    Pooling fixes both: sum the remaining across accounts, subtract one
    reserve and one forecast. ``per_account`` keeps each member's own
    remaining, because a job runs on a single account and cannot be split.
    """

    window: str
    remaining_pct: float  # summed 100-used across accounts with known usage
    forecast_pct: float  # the user's own expected burn, counted once
    reserve_pct: float  # the largest reserve any member carries, once
    spare_pct: float
    accounts: int  # how many accounts contributed
    per_account: dict[str, float]  # number → that account's own remaining
    next_reset: float | None  # soonest reset among the accounts, POSIX
    next_reset_account: str | None
    blackout: bool  # every contributing account is blacked out

    def best_single(self) -> float:
        """Most remaining on any one account — the cap for one job."""
        return max(self.per_account.values(), default=0.0)


def pool_window(caps: list[AccountCapacity], window: str) -> PooledWindow | None:
    """Pool one window across accounts. None when no account reports it."""
    remaining = 0.0
    forecast = 0.0
    reserve = 0.0
    per_account: dict[str, float] = {}
    next_reset: float | None = None
    next_reset_account: str | None = None
    contributing = 0
    blackouts = 0
    for cap in caps:
        w = cap.window(window)
        if w is None or w.used_pct is None:
            continue
        contributing += 1
        left = max(0.0, 100.0 - w.used_pct)
        remaining += left
        per_account[cap.number] = left
        # One appetite, so the forecast is the largest single-account
        # prediction rather than their sum: whichever account the user works
        # on, that is roughly what they will spend.
        forecast = max(forecast, w.forecast_pct)
        reserve = max(reserve, w.reserve_pct)
        if w.blackout:
            blackouts += 1
        if w.resets_at is not None and (next_reset is None or w.resets_at < next_reset):
            next_reset, next_reset_account = w.resets_at, cap.number
    if not contributing:
        return None
    return PooledWindow(
        window=window,
        remaining_pct=remaining,
        forecast_pct=forecast,
        reserve_pct=reserve,
        spare_pct=remaining - forecast - reserve,
        accounts=contributing,
        per_account=per_account,
        next_reset=next_reset,
        next_reset_account=next_reset_account,
        blackout=blackouts == contributing,
    )


def pooled_capacity(caps: list[AccountCapacity]) -> dict[str, PooledWindow]:
    """Every window any account reports, pooled."""
    names: list[str] = [WINDOW_5H, WINDOW_7D]
    for cap in caps:
        for name in cap.windows:
            if name not in names:
                names.append(name)
    out: dict[str, PooledWindow] = {}
    for name in names:
        pooled = pool_window(caps, name)
        if pooled is not None:
            out[name] = pooled
    return out


def next_refill(pool: dict[str, PooledWindow], windows: tuple[str, ...]) -> tuple[float, str, str] | None:
    """(timestamp, window, account) of the soonest reset among ``windows``.

    What a held job is waiting for — a hold that names the refill is far more
    useful than one that just says there is no capacity.
    """
    best: tuple[float, str, str] | None = None
    for name in windows:
        pooled = pool.get(name)
        if pooled is None or pooled.next_reset is None or pooled.next_reset_account is None:
            continue
        if best is None or pooled.next_reset < best[0]:
            best = (pooled.next_reset, name, pooled.next_reset_account)
    return best
