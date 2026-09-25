"""Live Claude Code sessions: who is running, what they are doing, what they cost.

One :func:`snapshot` call joins four sources into :class:`SessionInfo` rows:

- the ``sessions/<pid>.json`` records Claude Code writes into every config dir
  (the default ``~/.claude`` and each ccswap session profile, which is how a
  session is tied to an account);
- each session's transcript JSONL (title, last prompt and reply, model, and a
  per-minute token timeline);
- one ``ps`` listing, folded into a process tree under each session's pid;
- running ccswap jobs, matched on ``claude_pid``.

Transcripts can run to hundreds of MB, so :class:`TranscriptCache` keeps a byte
offset per file and parses only what was appended since the last call. An
assistant message is written as one line per content block, each repeating the
message's full ``usage``: usage is counted once per message id, never per line.

Nothing here writes, signals, or blocks on the network; it is safe to call on a
timer. Liveness follows :func:`~claude_swap.process_detection.scan_sessions`:
a record counts only if its pid is alive and was not recycled.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from claude_swap.process_detection import is_pid_alive, kill_tree_windows, pid_matches_record

BUCKET_S = 60
TIMELINE_S = 5 * 3600
EXCERPT_CHARS = 400
_SEEN_IDS_MAX = 4096


# -- process tree ---------------------------------------------------------------


@dataclass
class Proc:
    pid: int
    ppid: int
    cpu: float
    rss_kb: int
    etime: str
    command: str
    children: list[Proc] = field(default_factory=list)

    @property
    def label(self) -> str:
        return classify(self.command)


def classify(command: str) -> str:
    c = command.lower()
    exe = Path(command.split(" ", 1)[0]).name.lower()
    if exe == "claude" or "/claude/versions/" in c or exe == "claude.exe":
        if " -p " in f" {c} " or " --print" in c:
            return "claude -p"
        if "bg-pty-host" in c or "bg-spare" in c or " daemon " in f"{c} ":
            return "claude daemon"
        if "--agent-id" in c:
            return "claude teammate"
        return "claude"
    if "mcp" in c:
        return "mcp server"
    if exe in ("bash", "zsh", "sh", "fish"):
        return "shell"
    if exe.startswith("python") or exe in ("uv", "uvx"):
        return "python"
    if exe in ("node", "bun", "deno", "npm", "npx", "pnpm"):
        return "node"
    return exe or "?"


def parse_ps(text: str) -> dict[int, Proc]:
    """Parse ``ps -axo pid=,ppid=,pcpu=,rss=,etime=,command=`` output."""
    procs: dict[int, Proc] = {}
    for line in text.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
            cpu, rss = float(parts[2]), int(parts[3])
        except ValueError:
            continue
        procs[pid] = Proc(pid, ppid, cpu, rss, parts[4], parts[5])
    for p in procs.values():
        parent = procs.get(p.ppid)
        if parent is not None and parent is not p:
            parent.children.append(p)
    return procs


def read_ps() -> dict[int, Proc]:
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,pcpu=,rss=,etime=,command="],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return parse_ps(out)


def descendants(root: Proc) -> list[tuple[int, Proc]]:
    """Depth-first (depth, proc) pairs under root, root excluded."""
    out: list[tuple[int, Proc]] = []
    stack = [(1, c) for c in reversed(root.children)]
    while stack:
        depth, p = stack.pop()
        out.append((depth, p))
        stack.extend((depth + 1, c) for c in reversed(p.children))
    return out


# -- transcripts ----------------------------------------------------------------


def project_slug(cwd: str) -> str:
    """Claude Code's projects/ folder name for a working directory."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def transcript_path(config_dir: Path, cwd: str, session_id: str) -> Path | None:
    p = config_dir / "projects" / project_slug(cwd) / f"{session_id}.jsonl"
    if p.exists():
        return p
    hits = list((config_dir / "projects").glob(f"*/{session_id}.jsonl"))
    return hits[0] if hits else None


@dataclass
class TranscriptState:
    offset: int = 0
    custom_title: str | None = None
    ai_title: str | None = None
    model: str | None = None
    last_prompt: str | None = None
    last_reply: str | None = None
    last_ts: float | None = None  # last assistant activity
    last_user_ts: float | None = None  # last real prompt from the person
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    # minute bucket (epoch // 60) -> [output, total processed]
    buckets: dict[int, list[int]] = field(default_factory=dict)
    seen_ids: OrderedDict[str, None] = field(default_factory=OrderedDict)

    @property
    def title(self) -> str | None:
        return self.custom_title or self.ai_title

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read + self.cache_write

    def timeline(self, now: float, metric: str = "output", span_s: int = TIMELINE_S) -> list[int]:
        """Per-minute values for the last span_s seconds, oldest first."""
        idx = 0 if metric == "output" else 1
        end = int(now) // BUCKET_S
        n = span_s // BUCKET_S
        return [self.buckets.get(m, (0, 0))[idx] for m in range(end - n + 1, end + 1)]


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return ""


def _ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _excerpt(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= EXCERPT_CHARS else text[: EXCERPT_CHARS - 1] + "…"


def apply_line(st: TranscriptState, line: str) -> None:
    # Cheap prefilter: most lines are tool results and attachments.
    if '"assistant"' not in line and '"user"' not in line and '-title"' not in line:
        return
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return
    t = d.get("type")
    if t == "custom-title":
        st.custom_title = d.get("customTitle") or st.custom_title
    elif t == "ai-title":
        st.ai_title = d.get("aiTitle") or st.ai_title
    elif t == "user" and not d.get("isMeta") and not d.get("isSidechain"):
        text = _text_of((d.get("message") or {}).get("content"))
        if text.strip() and not text.startswith(("<local-command", "<command-", "<task-notification", "<system-reminder")):
            st.last_prompt = _excerpt(text)
            st.last_user_ts = _ts(d.get("timestamp")) or st.last_user_ts
    elif t == "assistant" and not d.get("isSidechain"):
        msg = d.get("message") or {}
        st.model = msg.get("model") or st.model
        text = _text_of(msg.get("content"))
        if text.strip():
            st.last_reply = _excerpt(text)
        ts = _ts(d.get("timestamp"))
        if ts is not None:
            st.last_ts = ts
        mid = msg.get("id")
        usage = msg.get("usage")
        if not usage or not mid or mid in st.seen_ids:
            return
        st.seen_ids[mid] = None
        if len(st.seen_ids) > _SEEN_IDS_MAX:
            st.seen_ids.popitem(last=False)
        i, o = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
        cr, cw = int(usage.get("cache_read_input_tokens") or 0), int(usage.get("cache_creation_input_tokens") or 0)
        st.input_tokens += i
        st.output_tokens += o
        st.cache_read += cr
        st.cache_write += cw
        if ts is not None:
            b = st.buckets.setdefault(int(ts) // BUCKET_S, [0, 0])
            b[0] += o
            b[1] += i + o + cr + cw


class TranscriptCache:
    """Incremental readers keyed by transcript path."""

    def __init__(self) -> None:
        self._states: dict[Path, TranscriptState] = {}

    def read(self, path: Path) -> TranscriptState:
        st = self._states.setdefault(path, TranscriptState())
        try:
            size = path.stat().st_size
        except OSError:
            return st
        if size < st.offset:  # truncated or replaced: start over
            st = self._states[path] = TranscriptState()
        if size == st.offset:
            return st
        with path.open("rb") as f:
            f.seek(st.offset)
            chunk = f.read(size - st.offset)
        end = chunk.rfind(b"\n")
        if end < 0:
            return st  # only a partial line so far
        for raw in chunk[: end + 1].splitlines():
            apply_line(st, raw.decode("utf-8", errors="replace"))
        st.offset += end + 1
        return st

    def prune(self, keep: set[Path]) -> None:
        for p in list(self._states):
            if p not in keep:
                del self._states[p]


# -- sessions -------------------------------------------------------------------


@dataclass
class Profile:
    """A config dir that may hold live sessions, and the account behind it."""

    config_dir: Path
    account: str  # "default" for ~/.claude, else "<num>-<email slug>"


def profiles(claude_dir: Path, backup_dir: Path | None) -> list[Profile]:
    out = [Profile(claude_dir, "default")]
    root = backup_dir / "sessions" if backup_dir else None
    if root and root.is_dir():
        for d in sorted(root.iterdir()):
            if (d / "sessions").is_dir():
                out.append(Profile(d, d.name))
    return out


@dataclass
class SessionInfo:
    pid: int
    session_id: str
    cwd: str
    account: str
    name: str | None
    status: str | None
    kind: str
    entrypoint: str
    version: str | None
    tmux: str | None
    started_at: float | None
    status_since: float | None
    transcript: Path | None
    state: TranscriptState
    proc: Proc | None
    job: str | None = None
    registered: bool = True

    @property
    def title(self) -> str:
        return self.state.title or self.name or Path(self.cwd).name or f"pid {self.pid}"

    @property
    def last_active(self) -> float | None:
        """Most recent sign of life: a prompt, agent activity, or a status change."""
        stamps = [t for t in (self.state.last_user_ts, self.state.last_ts, self.status_since) if t]
        return max(stamps) if stamps else self.started_at

    @property
    def mode(self) -> str:
        if self.job:
            return "job"
        if self.entrypoint == "sdk-cli" or (self.proc and self.proc.label == "claude -p"):
            return "headless"
        if self.kind in ("bg", "teammate"):
            return self.kind
        return "interactive" if self.registered else "unregistered"

    @property
    def children(self) -> list[tuple[int, Proc]]:
        return descendants(self.proc) if self.proc else []

    @property
    def resume_command(self) -> str:
        return f"cd {self.cwd!r} && claude --resume {self.session_id}" if self.session_id else ""


def _read_records(profile: Profile) -> list[dict]:
    out = []
    for path in (profile.config_dir / "sessions").glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pid = int(data["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not is_pid_alive(pid) or not pid_matches_record(pid, data.get("procStart")):
            continue
        out.append(data)
    return out


def running_jobs(backup_dir: Path | None) -> dict[int, str]:
    """claude_pid -> job name, for jobs ccswap is running right now."""
    if not backup_dir:
        return {}
    try:
        data = json.loads((backup_dir / "jobs.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    jobs = data.get("jobs", data) if isinstance(data, dict) else data
    out = {}
    for j in jobs if isinstance(jobs, list) else []:
        if isinstance(j, dict) and j.get("state") == "running" and j.get("claude_pid"):
            out[int(j["claude_pid"])] = j.get("name") or j.get("id", "")[:8]
    return out


def snapshot(
    claude_dir: Path,
    backup_dir: Path | None,
    cache: TranscriptCache,
    ps: dict[int, Proc] | None = None,
) -> list[SessionInfo]:
    """Every live session, newest activity first."""
    procs = read_ps() if ps is None else ps
    jobs = running_jobs(backup_dir)
    rows: list[SessionInfo] = []
    seen_pids: set[int] = set()
    live_paths: set[Path] = set()
    for prof in profiles(claude_dir, backup_dir):
        for rec in _read_records(prof):
            pid = int(rec["pid"])
            if pid in seen_pids:
                continue
            seen_pids.add(pid)
            sid, cwd = rec.get("sessionId", ""), rec.get("cwd", "")
            tpath = transcript_path(prof.config_dir, cwd, sid) if sid else None
            st = cache.read(tpath) if tpath else TranscriptState()
            if tpath:
                live_paths.add(tpath)
            started = rec.get("startedAt")
            since = rec.get("statusUpdatedAt") or rec.get("updatedAt")
            rows.append(SessionInfo(
                pid=pid, session_id=sid, cwd=cwd, account=prof.account,
                name=rec.get("name"), status=rec.get("status"), kind=rec.get("kind", ""),
                entrypoint=rec.get("entrypoint", ""), version=rec.get("version"),
                tmux=rec.get("tmux"), started_at=started / 1000 if started else None,
                status_since=since / 1000 if since else None,
                transcript=tpath, state=st, proc=procs.get(pid), job=jobs.get(pid),
            ))
    # claude processes with no record: old versions, or a record we cannot read.
    registered_tree = {p.pid for r in rows if r.proc for _, p in r.children} | seen_pids
    titles = {r.session_id: r.title for r in rows if r.session_id}
    for p in procs.values():
        if p.pid in registered_tree or p.label not in ("claude", "claude -p", "claude teammate"):
            continue
        parent = procs.get(p.ppid)
        if parent is not None and parent.label in ("claude", "claude -p"):
            continue  # nested under another claude; shown in that tree
        name, parent_sid = p.command[:60], None
        if p.label == "claude teammate":
            agent = _flag(p.command, "--agent-name") or "teammate"
            parent_sid = _flag(p.command, "--parent-session-id")
            owner = titles.get(parent_sid or "", (parent_sid or "?")[:8])
            name = f"{agent} (teammate of {owner})"
        rows.append(SessionInfo(
            pid=p.pid, session_id="", cwd="", account="?", name=name, status=None,
            kind="teammate" if parent_sid else "", entrypoint="", version=None, tmux=None,
            started_at=None, status_since=None, transcript=None, state=TranscriptState(), proc=p,
            job=jobs.get(p.pid), registered=False,
        ))
    cache.prune(live_paths)
    rows.sort(key=lambda r: -(r.last_active or 0))
    return rows


def _flag(command: str, name: str) -> str | None:
    m = re.search(rf"{re.escape(name)}[ =](\S+)", command)
    return m.group(1) if m else None


def combined_timeline(rows: list[SessionInfo], now: float | None = None, metric: str = "output") -> list[int]:
    now = time.time() if now is None else now
    series = [r.state.timeline(now, metric) for r in rows]
    return [sum(col) for col in zip(*series)] if series else []


def kill(pid: int, *, tree: bool) -> None:
    """SIGTERM a process, or its whole group when it leads one that is not ours."""
    import signal

    if tree and sys.platform == "win32":
        # No process groups on Windows; end the pid's tree instead.
        if not kill_tree_windows(pid):
            raise OSError(f"taskkill could not end process tree {pid}")
        return
    if tree:
        try:
            pgid = os.getpgid(pid)
        except OSError:
            pgid = None
        if pgid is not None and pgid == pid and pgid != os.getpgrp():
            os.killpg(pgid, signal.SIGTERM)
            return
    os.kill(pid, signal.SIGTERM)
