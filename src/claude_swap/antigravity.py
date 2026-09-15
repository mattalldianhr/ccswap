"""Read Antigravity (Gemini Code Assist) quota through the endpoint its CLI uses.

Antigravity is not a *switchable* provider here. Unlike Claude Code and Codex,
it holds exactly one login in the macOS Keychain and ccswap only reads its
quota, so there is no account list, no swap, and nothing written back. What it
adds is visibility: two quota groups on budgets entirely separate from the
Claude subscriptions, one of which serves Claude models.

Three things about this endpoint were only discoverable by observation, and
each will look like an auth failure if lost:

- The request body must name a project. The CLI writes the value it uses to
  ``~/.gemini/antigravity-cli/cache/default_project_id.txt``; for a consumer
  login it is the literal ``default-cli-project``.
- ``User-Agent`` must start with ``antigravity-cli/``. Any version passes, but
  an unrecognized client is refused with *"You do not have a valid license"* —
  a message about entitlement for what is really a client check. Do not read a
  403 here as "the account lost access" without first checking the header.
- The credential is a ``go-keyring`` blob, not raw JSON: Keychain holds
  ``go-keyring-base64:<base64 of the JSON document>``.

**Token refresh belongs to the Antigravity CLI, not to us.** Its OAuth client
is a *confidential* one: Google rejects a refresh that carries only the client
id with ``"client_secret is missing"``. The secret is the CLI's, and prising it
out to impersonate that client would be both fragile and wrong. Running any
``agy`` command refreshes the token in place (measured 2026-09-15: expiry moved
an hour out), so when the stored token has aged out the honest answer is to say
so and name the command that fixes it.

This is an internal Google endpoint with no compatibility promise. Every
failure path therefore raises :class:`AntigravityError` and callers are
expected to degrade to "unknown", never to break a dashboard that is also
showing healthy Claude data.
"""

from __future__ import annotations

import base64
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

QUOTA_URL = "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary"

# The Keychain item the Antigravity CLI writes its login to.
KEYCHAIN_SERVICE = "gemini"
KEYCHAIN_ACCOUNT = "antigravity"
GO_KEYRING_PREFIX = "go-keyring-base64:"

# Consumer logins are scoped to this pseudo-project; the CLI caches the value
# it resolved at ``cache/default_project_id.txt`` under its config directory.
DEFAULT_PROJECT = "default-cli-project"
PROJECT_ID_CACHE = "cache/default_project_id.txt"

# The server gates on this prefix. The version is not checked, but a wrong or
# absent prefix returns 403 with a licensing message. See the module docstring.
USER_AGENT = "antigravity-cli/1.2.3"

# Treat a token as unusable slightly before its stated expiry, so a request
# that would die in flight is reported as a stale login instead of a 401.
EXPIRY_SKEW_S = 60.0

# What the user runs to refresh. Any agy command re-mints the token in place.
REFRESH_HINT = "run any 'agy' command (for example 'agy models') to refresh it"

# ccswap's window vocabulary, so Antigravity rows sort and render beside the
# Claude and Codex ones instead of inventing a third set of names.
WINDOW_NAMES = {"5h": "five_hour", "weekly": "weekly"}


class AntigravityError(Exception):
    """Antigravity quota could not be read."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class QuotaBucket:
    """One quota window within a group."""

    bucket_id: str
    window: str  # ccswap's vocabulary: "five_hour" | "weekly" | the raw value
    used_pct: float
    resets_at: str | None
    label: str = ""

    @property
    def remaining_pct(self) -> float:
        return max(0.0, 100.0 - self.used_pct)


@dataclass(frozen=True)
class QuotaGroup:
    """A set of models sharing one pair of windows."""

    name: str
    description: str
    buckets: tuple[QuotaBucket, ...]

    @property
    def serves_claude(self) -> bool:
        """Whether this group's models include Claude.

        Worth surfacing: it is Claude capacity on a budget ccswap's own
        accounts do not draw from.
        """
        return "claude" in f"{self.name} {self.description}".lower()

    def window(self, name: str) -> QuotaBucket | None:
        for bucket in self.buckets:
            if bucket.window == name:
                return bucket
        return None


@dataclass(frozen=True)
class AntigravityUsage:
    """One reading of every Antigravity quota group."""

    email: str
    groups: tuple[QuotaGroup, ...]
    fetched_at: float

    def to_json(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "fetchedAt": self.fetched_at,
            "groups": [
                {
                    "name": group.name,
                    "description": group.description,
                    "servesClaude": group.serves_claude,
                    "windows": {
                        bucket.window: {
                            "pct": bucket.used_pct,
                            "remainingPct": bucket.remaining_pct,
                            "bucketId": bucket.bucket_id,
                            **({"resets_at": bucket.resets_at} if bucket.resets_at else {}),
                        }
                        for bucket in group.buckets
                    },
                }
                for group in self.groups
            ],
        }


# ---------------------------------------------------------------------------
# Credential
# ---------------------------------------------------------------------------


def decode_credential(raw: str) -> dict[str, Any]:
    """Decode the ``go-keyring-base64:`` blob the Antigravity CLI stores."""
    value = raw.strip()
    if not value:
        raise AntigravityError("no Antigravity login found in the Keychain")
    if value.startswith(GO_KEYRING_PREFIX):
        value = value[len(GO_KEYRING_PREFIX):]
        try:
            value = base64.b64decode(value).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise AntigravityError(f"Antigravity credential is not valid base64: {exc}") from exc
    try:
        document = json.loads(value)
    except json.JSONDecodeError as exc:
        raise AntigravityError(f"Antigravity credential is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise AntigravityError("Antigravity credential is not a JSON object")
    token = document.get("token")
    if not isinstance(token, dict) or not token.get("access_token"):
        raise AntigravityError(
            "Antigravity credential has no access token; sign in with 'agy' and retry"
        )
    return document


def read_credential(
    *, service: str = KEYCHAIN_SERVICE, account: str = KEYCHAIN_ACCOUNT
) -> dict[str, Any]:
    """Read and decode the stored Antigravity login.

    Uses ``macos_keychain`` so the read goes through the same stable
    ``security`` binary the rest of ccswap uses — reading via an in-process
    Security.framework call would anchor access to the Python interpreter and
    prompt after every upgrade.
    """
    from claude_swap import macos_keychain

    try:
        raw = macos_keychain.get_password(service, account)
    except macos_keychain.KEYCHAIN_ERRORS as exc:
        raise AntigravityError(f"could not read the Antigravity login: {exc}") from exc
    if raw is None:
        raise AntigravityError(
            "no Antigravity login found; install the Antigravity CLI and sign in with 'agy'"
        )
    return decode_credential(raw)


def account_email(credential: dict[str, Any]) -> str:
    """The signed-in address, from the login's id_token. Empty when unknown."""
    return str(_id_claims(credential).get("email") or "")


def _id_claims(credential: dict[str, Any]) -> dict[str, Any]:
    token = credential.get("id_token")
    if not isinstance(token, str):
        return {}
    pieces = token.split(".")
    if len(pieces) != 3:
        return {}
    try:
        padded = pieces[1] + "=" * (-len(pieces[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}  # claims are a convenience; a bad id_token is not fatal
    return claims if isinstance(claims, dict) else {}


def token_expiry(credential: dict[str, Any]) -> float | None:
    """Unix expiry of the access token, if the login records one."""
    token = credential.get("token")
    value = token.get("expiry") if isinstance(token, dict) else None
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def token_expired(credential: dict[str, Any], *, now: float, skew_s: float = EXPIRY_SKEW_S) -> bool:
    """Whether the stored access token has aged out.

    An unknown expiry is *not* treated as expired: the token may well work, and
    the only thing this decides is whether to attempt the request at all.
    """
    expiry = token_expiry(credential)
    return expiry is not None and now >= expiry - skew_s


# ---------------------------------------------------------------------------
# Quota
# ---------------------------------------------------------------------------


def _iso(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp, timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def project_id(config_dir: Any = None) -> str:
    """The project the request must name, from the CLI's cache when available."""
    base = Path(config_dir) if config_dir else Path.home() / ".gemini" / "antigravity-cli"
    try:
        value = (base / PROJECT_ID_CACHE).read_text(encoding="utf-8").strip()
    except OSError:
        return DEFAULT_PROJECT
    return value or DEFAULT_PROJECT


def _bucket(raw: object) -> QuotaBucket | None:
    if not isinstance(raw, dict):
        return None
    fraction = raw.get("remainingFraction")
    if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
        return None
    window = raw.get("window")
    window = window if isinstance(window, str) else ""
    resets_at = raw.get("resetTime")
    return QuotaBucket(
        bucket_id=str(raw.get("bucketId") or ""),
        window=WINDOW_NAMES.get(window, window),
        # ccswap speaks in utilization everywhere; Antigravity reports what is
        # left. Converting here keeps the inversion in one place.
        used_pct=max(0.0, min(100.0, (1.0 - float(fraction)) * 100.0)),
        resets_at=resets_at if isinstance(resets_at, str) and resets_at else None,
        label=str(raw.get("displayName") or ""),
    )


def parse_quota(payload: object, *, email: str = "", now: float | None = None) -> AntigravityUsage:
    """Convert a quota-summary response into groups of windows."""
    if not isinstance(payload, dict):
        raise AntigravityError("Antigravity quota response is not a JSON object")
    raw_groups = payload.get("groups")
    if not isinstance(raw_groups, list):
        raise AntigravityError("Antigravity quota response has no groups")
    groups: list[QuotaGroup] = []
    for raw in raw_groups:
        if not isinstance(raw, dict):
            continue
        buckets = tuple(
            bucket
            for bucket in (_bucket(item) for item in (raw.get("buckets") or []))
            if bucket is not None
        )
        if not buckets:
            continue
        groups.append(QuotaGroup(
            name=str(raw.get("displayName") or ""),
            description=str(raw.get("description") or ""),
            buckets=buckets,
        ))
    if not groups:
        raise AntigravityError("Antigravity returned no quota windows for this account")
    return AntigravityUsage(
        email=email,
        groups=tuple(groups),
        fetched_at=now if now is not None else time.time(),
    )


def fetch_quota(
    credential: dict[str, Any],
    *,
    project: str | None = None,
    url: str = QUOTA_URL,
    timeout: float = 15.0,
) -> AntigravityUsage:
    """Fetch quota for an already-valid credential.

    Callers wanting automatic refresh should use :func:`read_usage`.
    """
    token = credential.get("token")
    access_token = token.get("access_token") if isinstance(token, dict) else None
    if not isinstance(access_token, str) or not access_token:
        raise AntigravityError("Antigravity login has no access token")
    body = json.dumps({"project": project or project_id()}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            # Required. A different prefix is refused as a licensing error.
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise AntigravityError(
                "Antigravity refused the request (401/403). The token may be expired, or "
                "the client check may have changed — it requires a User-Agent starting "
                "with 'antigravity-cli/'. Sign in again with 'agy' if it persists.",
                status_code=exc.code,
            ) from exc
        raise AntigravityError(
            f"Antigravity quota request failed ({exc.code})", status_code=exc.code
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise AntigravityError(f"Antigravity quota request failed: {exc}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AntigravityError(f"Could not decode the Antigravity quota response: {exc}") from exc
    return parse_quota(payload, email=account_email(credential))


def read_usage(
    *,
    credential: dict[str, Any] | None = None,
    now: float | None = None,
    timeout: float = 15.0,
) -> AntigravityUsage:
    """Read Antigravity quota using the login the Antigravity CLI maintains.

    ccswap never refreshes and never writes this credential. The CLI owns it,
    its OAuth client is confidential (see the module docstring), and every
    ``agy`` command re-mints the token in place. So an aged-out token is
    reported as exactly that, with the one-line fix, rather than being papered
    over with a refresh that cannot succeed.
    """
    now = now if now is not None else time.time()
    credential = credential if credential is not None else read_credential()
    if token_expired(credential, now=now):
        raise AntigravityError(f"the Antigravity login has expired; {REFRESH_HINT}")
    try:
        return fetch_quota(credential, timeout=timeout)
    except AntigravityError as exc:
        # A token that still looks live can be refused anyway (clock skew, a
        # server-side revocation). Same remedy, so say the same thing.
        if exc.status_code in (401, 403):
            raise AntigravityError(
                f"Antigravity refused the stored login; {REFRESH_HINT}",
                status_code=exc.status_code,
            ) from exc
        raise


def available(*, service: str = KEYCHAIN_SERVICE, account: str = KEYCHAIN_ACCOUNT) -> bool:
    """Whether an Antigravity login exists, without decrypting it.

    Attribute-only, so it never prompts and never raises — a UI can call it to
    decide whether to show the section at all.
    """
    if sys.platform != "darwin":
        return False
    from claude_swap import macos_keychain

    return macos_keychain.item_exists(service, account)
