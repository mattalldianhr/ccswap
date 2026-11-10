# Jobs: use spare capacity for headless work

_Fork addition (mattalldianhr/ccswap)._


`ccswap jobs` is a queue of headless `claude -p` runs, each bound to a folder, that start on their own when your subscription has capacity you are not going to use. Every job carries its own **model**, **effort level**, **permission mode**, allowed tools, turn cap, priority, and a cost estimate in percentage points of the 5h window; the estimate is replaced by the measured usage delta after each run, so forecasts improve with every job.

```bash
ccswap jobs add ~/proj "Fix the flaky test in tests/test_io.py" --name flaky
ccswap jobs add . -f prompt.md --model opus --effort high --permission-mode acceptEdits
ccswap jobs list                    # queue, running, and recent failures
ccswap jobs start flaky             # detached, right now, ignoring capacity
ccswap jobs start flaky --wait      # foreground
ccswap jobs log flaky               # readable tail of the run (--raw for stream-json)
ccswap jobs capacity                # what the scheduler sees per account and window
ccswap jobs auto --once             # one scheduler tick (launchd/cron); exit code = outcome
ccswap jobs daemon install          # macOS launchd agent, ticks every 5 minutes
```

**When does a job start?** Each tick the scheduler asks two questions. First, can the accounts *together* afford the job? Per window (5h, 7d, and any per-model window such as Fable):

```
pooled spare = Σ(100 − used) − forecast of your own burn − reserve
```

The reserve and the forecast are counted **once**, not once per account: you spend from one account at a time, so charging both to every account reserves capacity that was never at risk. This matters because the accounts' windows reset at different times — with one weekly window spent and another half free, a per-account view shows everything blocked while the pool still has room.

The two windows are treated differently, because they mean different things. The **7d window is a budget**: it is the scarce one, and whatever is unspent when it resets is capacity you paid for and lost. The **5h window is a throttle**, not a budget: it refills 33 times before the weekly one does, so nothing is conserved by leaving it idle, and it is forecast only over the next 45 minutes — just far enough to keep a job from colliding with you. Applying budget logic to both is what made jobs startable only in the narrow intersection of "quiet right now" and "early in the week".

Each completed cycle is recorded to `cache/usage_cycles.jsonl` (peak reached, budget left unspent, and the trajectory on that cycle's own clock, per account). The accounts' windows reset at different times, so cycles are keyed per account; folding them onto one shared axis produces artifacts rather than insight.

After **8 complete cycles**, the weekly reserve stops being a guess and is derived from them (`jobs.learnReserve`, on by default). The input is your *own* peak demand — each cycle's peak minus what jobs spent during it — because deriving it from unspent budget would be self-confirming: jobs eat the slack, unspent falls, the reserve shrinks, jobs eat more. The reserve is then the headroom above a blend of your 80th-percentile and heaviest week, less a safety margin. Two guardrails: it can only ever hold back **more** than the configured floor, never less, and `ccswap jobs capacity` always shows where the number came from (`10% (need 8 cycles, have 2)`, or `6% (learned from 9 cycles)`).

Second, can a *single* account hold the job? A run cannot be split, so the job also has to fit in one account's remaining budget. The account with the most room wins, and `ccswap jobs capacity` shows both views plus when the next window refills.

The forecast blends your burn rate over the last hour with your typical burn for the remaining weekday/hour slots, weighted by how much evidence the recent rate has (a full hour of measurement trusts it outright; six minutes gives it a tenth of the weight), learned from `cache/usage_history.jsonl` (seed it from an existing auto-switch log with `ccswap jobs backfill`). A job starts on the account with the most spare only when spare covers its estimate in every window it touches, **and** every interactive Claude Code session has been idle for `jobs.quietMinutes`. Jobs run pinned to their account through a session profile (`ccswap run` machinery), so an auto-switch of your default login never touches a running job. A job whose account is already the active login runs with the plain environment instead.

**Who is the quiet gate protecting?** With `jobs.quietScope` at `account` (the default), a busy interactive session blocks only the account it is spending from: the live login for a normal session, or the profile's account for a `ccswap run` session. Jobs still start on the other accounts. `all` restores the old behaviour, where any busy session holds the whole queue. An idle 5h window (the API reports none until your next message) counts as 0% used, and a reading older than five minutes is refreshed before the decision instead of ruling its account out.

**Spend-down.** `--spend-down` marks a job as use-it-or-lose-it work: it auto-starts only on an account whose weekly window resets within `jobs.spendDownHours` (24) with at least `jobs.spendDownMinLeftPct` (25) unspent. Pair it with a short `--repeat` and a backlog the job pulls from.

**Loops.** `--repeat MINUTES` re-queues a job that long after each successful run (a failed run stays failed so you notice); `--then JOB` re-queues another job when this one succeeds, so a scrape can feed an analysis; `--weekly-budget PCT` stops auto-starts once the job's measured 7d cost over the last week reaches that many points. The same three fields are on the TUI job form.

**Reserves** are date-scheduled safety thresholds: `ccswap reserves add 7d 40 --until 2026-09-16 --note "deadline"` keeps 40% of the 7d window free of auto-started jobs until then; `100` is a blackout. Reserves apply per window, optionally per account, and stack by maximum over the `jobs.reservePct` / `jobs.weeklyReservePct` floors. Manual `ccswap jobs start` ignores reserves and the quiet gate.

In the TUI, **Jobs…** shows the queue with a capacity strip and a detail pane that explains why a job waits, tails a running job, and hosts the scheduler in dry-run (press `l` to go live). `n` opens the job form, `s` starts a job on a chosen account, `c` opens the Capacity screen (forecast breakdown and sparklines), `R` opens Reserves (list plus a 14-day timeline; dates accept `now`, `+3d`, `fri 18:00`, `next reset`). **Settings › Jobs…** edits every `jobs.*` setting and installs or removes the scheduler daemon.

