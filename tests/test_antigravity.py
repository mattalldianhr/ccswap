"""Tests for the Antigravity quota provider.

The three things that were only discoverable by observation — the project in
the body, the ``antigravity-cli/`` User-Agent prefix, and the ``go-keyring``
credential envelope — each get a test that fails loudly if a refactor drops
them, because losing any one of them presents as a 403 that reads like a
revoked account rather than a client mistake.
"""

from __future__ import annotations

import base64
import json
import urllib.error
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from claude_swap import antigravity
from claude_swap.antigravity import (
    GO_KEYRING_PREFIX,
    AntigravityError,
    REFRESH_HINT,
    account_email,
    available,
    decode_credential,
    fetch_quota,
    parse_quota,
    project_id,
    read_credential,
    read_usage,
    token_expired,
    token_expiry,
)

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc).timestamp()


def _id_token(email: str = "matt@example.com", aud: str = "client-123") -> str:
    def seg(payload: dict) -> str:
        raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
        return raw.rstrip("=")
    return f"{seg({'alg': 'RS256'})}.{seg({'email': email, 'aud': aud})}.signature"


def _credential(*, expiry: float | None = None, refresh: str | None = "refresh-abc",
                email: str = "matt@example.com") -> dict:
    token: dict = {"access_token": "access-xyz", "token_type": "Bearer"}
    if refresh is not None:
        token["refresh_token"] = refresh
    if expiry is not None:
        token["expiry"] = (
            datetime.fromtimestamp(expiry, tz=timezone.utc).isoformat()
        )
    return {"auth_method": "consumer", "id_token": _id_token(email), "token": token}


def _blob(credential: dict) -> str:
    return GO_KEYRING_PREFIX + base64.b64encode(
        json.dumps(credential).encode()
    ).decode()


QUOTA_PAYLOAD = {
    "description": "Within each group, models share a weekly limit and a 5-hour limit.",
    "groups": [
        {
            "displayName": "Gemini Models",
            "description": "Models within this group: Gemini Flash, Gemini Pro",
            "buckets": [
                {"bucketId": "gemini-weekly", "displayName": "Weekly Limit Remaining",
                 "window": "weekly", "resetTime": "2026-09-22T20:48:58Z",
                 "remainingFraction": 0.9932733},
                {"bucketId": "gemini-5h", "displayName": "Five Hour Limit Remaining",
                 "window": "5h", "resetTime": "2026-09-16T01:48:58Z",
                 "remainingFraction": 0.75},
            ],
        },
        {
            "displayName": "Claude and GPT models",
            "description": "Models within this group: Claude Opus, Claude Sonnet, GPT-OSS",
            "buckets": [
                {"bucketId": "3p-weekly", "window": "weekly",
                 "resetTime": "2026-09-22T21:01:02Z", "remainingFraction": 1},
                {"bucketId": "3p-5h", "window": "5h",
                 "resetTime": "2026-09-16T02:01:02Z", "remainingFraction": 1},
            ],
        },
    ],
}


class _Response:
    def __init__(self, payload: object):
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("url", code, "denied", {}, None)


# ---------------------------------------------------------------------------
# Credential decoding
# ---------------------------------------------------------------------------


class TestDecodeCredential:
    def test_decodes_the_go_keyring_envelope(self):
        """The Keychain holds base64 behind a prefix, not raw JSON."""
        credential = decode_credential(_blob(_credential()))
        assert credential["token"]["access_token"] == "access-xyz"

    def test_accepts_plain_json_too(self):
        credential = decode_credential(json.dumps(_credential()))
        assert credential["auth_method"] == "consumer"

    def test_empty_and_malformed_are_named_clearly(self):
        with pytest.raises(AntigravityError, match="no Antigravity login"):
            decode_credential("   ")
        with pytest.raises(AntigravityError, match="not valid JSON"):
            decode_credential("not json at all")
        with pytest.raises(AntigravityError, match="not valid base64"):
            decode_credential(GO_KEYRING_PREFIX + "!!!not base64!!!")

    def test_a_credential_without_a_token_is_rejected(self):
        with pytest.raises(AntigravityError, match="no access token"):
            decode_credential(json.dumps({"auth_method": "consumer"}))

    def test_a_json_array_is_not_a_credential(self):
        with pytest.raises(AntigravityError, match="not a JSON object"):
            decode_credential("[1, 2, 3]")


class TestReadCredential:
    def test_reads_from_the_keychain(self, block_real_keychain):
        block_real_keychain.set_password("gemini", "antigravity", _blob(_credential()))
        assert read_credential()["token"]["access_token"] == "access-xyz"

    def test_absent_login_points_at_the_fix(self, block_real_keychain):
        with pytest.raises(AntigravityError, match="sign in with 'agy'"):
            read_credential()

    def test_a_keychain_failure_is_not_read_as_a_missing_login(self):
        from claude_swap import macos_keychain

        with patch.object(macos_keychain, "get_password",
                          side_effect=macos_keychain.KeychainError("locked")):
            with pytest.raises(AntigravityError, match="could not read"):
                read_credential()

    def test_available_is_attribute_only(self, block_real_keychain):
        with patch.object(antigravity.sys, "platform", "darwin"):
            assert available() is False
            block_real_keychain.set_password("gemini", "antigravity", _blob(_credential()))
            assert available() is True

    def test_not_available_off_macos(self, block_real_keychain):
        block_real_keychain.set_password("gemini", "antigravity", _blob(_credential()))
        with patch.object(antigravity.sys, "platform", "linux"):
            assert available() is False


class TestIdentity:
    def test_email_comes_from_the_id_token(self):
        assert account_email(_credential(email="a@b.com")) == "a@b.com"

    def test_a_broken_id_token_is_not_fatal(self):
        assert account_email({"id_token": "garbage", "token": {}}) == ""
        assert account_email({"token": {}}) == ""


# ---------------------------------------------------------------------------
# Token lifetime
# ---------------------------------------------------------------------------


class TestTokenExpiry:
    def test_reads_the_recorded_expiry(self):
        assert token_expiry(_credential(expiry=NOW + 600)) == pytest.approx(NOW + 600)

    def test_missing_or_unparseable_expiry_is_unknown(self):
        assert token_expiry(_credential()) is None
        assert token_expiry({"token": {"expiry": "soon"}}) is None

    def test_expired_within_the_skew(self):
        """Refresh ahead of expiry: a token dying mid-flight costs a whole poll."""
        assert token_expired(_credential(expiry=NOW + 30), now=NOW) is True
        assert token_expired(_credential(expiry=NOW + 600), now=NOW) is False

    def test_unknown_expiry_is_not_treated_as_expired(self):
        """Refreshing on a guess would spend the rotation for nothing."""
        assert token_expired(_credential(), now=NOW) is False


# ---------------------------------------------------------------------------
# Quota parsing
# ---------------------------------------------------------------------------


class TestParseQuota:
    def test_both_groups_with_their_windows(self):
        usage = parse_quota(QUOTA_PAYLOAD, email="matt@example.com", now=NOW)
        assert [g.name for g in usage.groups] == ["Gemini Models", "Claude and GPT models"]
        assert usage.email == "matt@example.com" and usage.fetched_at == NOW

    def test_remaining_is_converted_to_utilization(self):
        """Antigravity reports what is left; ccswap speaks in what is used."""
        gemini = parse_quota(QUOTA_PAYLOAD, now=NOW).groups[0]
        assert gemini.window("five_hour").used_pct == pytest.approx(25.0)
        assert gemini.window("five_hour").remaining_pct == pytest.approx(75.0)
        assert gemini.window("weekly").used_pct == pytest.approx(0.67, abs=0.01)

    def test_windows_use_ccswaps_vocabulary(self):
        """So these rows sort beside the Claude and Codex ones."""
        windows = {b.window for g in parse_quota(QUOTA_PAYLOAD, now=NOW).groups for b in g.buckets}
        assert windows == {"five_hour", "weekly"}

    def test_reset_times_are_carried_through(self):
        gemini = parse_quota(QUOTA_PAYLOAD, now=NOW).groups[0]
        assert gemini.window("weekly").resets_at == "2026-09-22T20:48:58Z"

    def test_the_claude_group_is_identified(self):
        """It is Claude capacity on a budget the managed accounts do not draw from."""
        groups = parse_quota(QUOTA_PAYLOAD, now=NOW).groups
        assert [g.serves_claude for g in groups] == [False, True]

    def test_an_unknown_window_name_survives_unmapped(self):
        payload = {"groups": [{"displayName": "G", "buckets": [
            {"bucketId": "x", "window": "monthly", "remainingFraction": 0.5}]}]}
        assert parse_quota(payload, now=NOW).groups[0].buckets[0].window == "monthly"

    def test_buckets_without_a_fraction_are_dropped(self):
        payload = {"groups": [{"displayName": "G", "buckets": [
            {"bucketId": "bad", "window": "weekly"},
            {"bucketId": "bad2", "window": "weekly", "remainingFraction": True},
            {"bucketId": "good", "window": "weekly", "remainingFraction": 0.5}]}]}
        buckets = parse_quota(payload, now=NOW).groups[0].buckets
        assert [b.bucket_id for b in buckets] == ["good"]

    def test_a_fraction_outside_the_range_is_clamped(self):
        payload = {"groups": [{"displayName": "G", "buckets": [
            {"bucketId": "a", "window": "weekly", "remainingFraction": 1.4},
            {"bucketId": "b", "window": "5h", "remainingFraction": -0.2}]}]}
        used = [b.used_pct for b in parse_quota(payload, now=NOW).groups[0].buckets]
        assert used == [0.0, 100.0]

    def test_groups_with_no_usable_buckets_are_skipped(self):
        payload = {"groups": [
            {"displayName": "Empty", "buckets": []},
            {"displayName": "Real", "buckets": [
                {"bucketId": "x", "window": "weekly", "remainingFraction": 0.5}]}]}
        assert [g.name for g in parse_quota(payload, now=NOW).groups] == ["Real"]

    def test_a_response_with_nothing_usable_raises(self):
        with pytest.raises(AntigravityError, match="no quota windows"):
            parse_quota({"groups": []}, now=NOW)
        with pytest.raises(AntigravityError, match="no groups"):
            parse_quota({"description": "..."}, now=NOW)
        with pytest.raises(AntigravityError, match="not a JSON object"):
            parse_quota([], now=NOW)

    def test_window_lookup_misses_return_none(self):
        assert parse_quota(QUOTA_PAYLOAD, now=NOW).groups[0].window("monthly") is None

    def test_json_round_trip_is_serializable(self):
        payload = parse_quota(QUOTA_PAYLOAD, email="m@x", now=NOW).to_json()
        assert json.loads(json.dumps(payload))["groups"][1]["servesClaude"] is True
        weekly = payload["groups"][0]["windows"]["weekly"]
        assert weekly["bucketId"] == "gemini-weekly" and "resets_at" in weekly


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


class TestFetchQuota:
    def test_sends_the_project_and_the_client_user_agent(self):
        """Both are load-bearing. Dropping either returns a 403 that reads
        like a revoked account rather than a malformed request."""
        with patch.object(antigravity.urllib.request, "urlopen",
                          return_value=_Response(QUOTA_PAYLOAD)) as urlopen:
            fetch_quota(_credential(), project="default-cli-project")
        request = urlopen.call_args.args[0]
        assert json.loads(request.data) == {"project": "default-cli-project"}
        assert request.get_header("User-agent").startswith("antigravity-cli/")
        assert request.get_header("Authorization") == "Bearer access-xyz"
        assert request.get_full_url() == antigravity.QUOTA_URL

    def test_the_signed_in_email_is_attached(self):
        with patch.object(antigravity.urllib.request, "urlopen",
                          return_value=_Response(QUOTA_PAYLOAD)):
            usage = fetch_quota(_credential(email="who@x.com"))
        assert usage.email == "who@x.com"

    def test_a_credential_without_a_token_never_reaches_the_network(self):
        with patch.object(antigravity.urllib.request, "urlopen") as urlopen:
            with pytest.raises(AntigravityError, match="no access token"):
                fetch_quota({"token": {}})
        assert not urlopen.called

    @pytest.mark.parametrize("code", [401, 403])
    def test_a_refusal_mentions_the_user_agent_requirement(self, code):
        """The server's own message blames licensing; that misleads."""
        with patch.object(antigravity.urllib.request, "urlopen",
                          side_effect=_http_error(code)):
            with pytest.raises(AntigravityError, match="antigravity-cli/") as caught:
                fetch_quota(_credential())
        assert caught.value.status_code == code

    def test_other_http_errors_are_reported_plainly(self):
        with patch.object(antigravity.urllib.request, "urlopen",
                          side_effect=_http_error(503)):
            with pytest.raises(AntigravityError, match=r"failed \(503\)"):
                fetch_quota(_credential())

    def test_a_network_failure_is_an_antigravity_error(self):
        with patch.object(antigravity.urllib.request, "urlopen",
                          side_effect=urllib.error.URLError("offline")):
            with pytest.raises(AntigravityError, match="request failed"):
                fetch_quota(_credential())

    def test_undecodable_output_is_an_antigravity_error(self):
        class Garbage:
            def read(self): return b"<html>nope"
            def __enter__(self): return self
            def __exit__(self, *e): return False

        with patch.object(antigravity.urllib.request, "urlopen", return_value=Garbage()):
            with pytest.raises(AntigravityError, match="Could not decode"):
                fetch_quota(_credential())


class TestProjectId:
    def test_reads_the_clis_cached_value(self, tmp_path):
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "default_project_id.txt").write_text("my-project\n")
        assert project_id(tmp_path) == "my-project"

    def test_falls_back_when_the_cache_is_absent_or_empty(self, tmp_path):
        assert project_id(tmp_path) == antigravity.DEFAULT_PROJECT
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "default_project_id.txt").write_text("  \n")
        assert project_id(tmp_path) == antigravity.DEFAULT_PROJECT


class TestReadUsage:
    """ccswap reads this login; the Antigravity CLI maintains it.

    Refresh is deliberately not attempted. Antigravity's OAuth client is
    confidential — Google answers a client-id-only refresh with
    "client_secret is missing" — and every ``agy`` command re-mints the token
    anyway. Measured 2026-09-15.
    """

    def test_a_live_token_is_used(self):
        with patch.object(antigravity.urllib.request, "urlopen",
                          return_value=_Response(QUOTA_PAYLOAD)):
            usage = read_usage(credential=_credential(expiry=NOW + 3600), now=NOW)
        assert len(usage.groups) == 2

    def test_an_aged_token_names_the_one_line_fix(self):
        """Never a silent failure, and never a refresh that cannot succeed."""
        with patch.object(antigravity.urllib.request, "urlopen") as urlopen:
            with pytest.raises(AntigravityError, match="expired") as caught:
                read_usage(credential=_credential(expiry=NOW - 1), now=NOW)
        assert not urlopen.called          # no pointless request
        assert "agy" in str(caught.value)

    def test_an_unknown_expiry_is_still_attempted(self):
        """A token with no recorded expiry may well work; try it."""
        with patch.object(antigravity.urllib.request, "urlopen",
                          return_value=_Response(QUOTA_PAYLOAD)):
            assert read_usage(credential=_credential(), now=NOW).groups

    @pytest.mark.parametrize("code", [401, 403])
    def test_a_refused_live_token_gets_the_same_remedy(self, code):
        """Clock skew or a revocation can refuse a token that looks live."""
        with patch.object(antigravity.urllib.request, "urlopen",
                          side_effect=_http_error(code)):
            with pytest.raises(AntigravityError, match="agy") as caught:
                read_usage(credential=_credential(expiry=NOW + 3600), now=NOW)
        assert caught.value.status_code == code

    def test_a_non_auth_failure_keeps_its_own_message(self):
        with patch.object(antigravity.urllib.request, "urlopen",
                          side_effect=_http_error(500)):
            with pytest.raises(AntigravityError, match=r"\(500\)"):
                read_usage(credential=_credential(expiry=NOW + 3600), now=NOW)

    def test_reads_the_keychain_when_given_no_credential(self, block_real_keychain):
        block_real_keychain.set_password("gemini", "antigravity",
                                         _blob(_credential(expiry=NOW + 3600)))
        with patch.object(antigravity.urllib.request, "urlopen",
                          return_value=_Response(QUOTA_PAYLOAD)):
            assert read_usage(now=NOW).email == "matt@example.com"

    def test_the_keychain_is_never_written(self, block_real_keychain):
        """ccswap does not own this login and must not race the CLI for it."""
        original = _blob(_credential(expiry=NOW + 3600))
        block_real_keychain.set_password("gemini", "antigravity", original)
        with patch.object(antigravity.urllib.request, "urlopen",
                          return_value=_Response(QUOTA_PAYLOAD)):
            read_usage(now=NOW)
        assert block_real_keychain.get_password("gemini", "antigravity") == original

    def test_the_hint_names_a_command_that_exists(self):
        assert "agy" in REFRESH_HINT


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestAntigravityCommand:
    def _run(self, argv, **patches):
        from claude_swap import cli

        with patch.object(antigravity, "read_usage", **patches):
            cli._antigravity_command(argv)

    def test_usage_prints_every_group(self, capsys):
        self._run(["usage"], return_value=parse_quota(
            QUOTA_PAYLOAD, email="matt@example.com", now=NOW))
        out = capsys.readouterr().out
        assert "matt@example.com" in out
        assert "Gemini Models" in out and "Claude and GPT models" in out
        assert "5h:" in out and "Weekly:" in out

    def test_the_claude_group_is_called_out(self, capsys):
        """The whole point of showing this: Claude capacity on a separate budget."""
        self._run(["usage"], return_value=parse_quota(QUOTA_PAYLOAD, now=NOW))
        assert "serves Claude models" in capsys.readouterr().out

    def test_bare_invocation_behaves_like_usage(self, capsys):
        self._run([], return_value=parse_quota(QUOTA_PAYLOAD, now=NOW))
        assert "Gemini Models" in capsys.readouterr().out

    def test_json_mode_is_machine_readable(self, capsys):
        self._run(["usage", "--json"], return_value=parse_quota(
            QUOTA_PAYLOAD, email="m@x", now=NOW))
        payload = json.loads(capsys.readouterr().out)
        assert payload["provider"] == "antigravity" and payload["email"] == "m@x"
        assert payload["groups"][1]["servesClaude"] is True

    def test_a_missing_login_exits_nonzero_with_the_fix(self, capsys):
        with pytest.raises(SystemExit) as exit_info:
            self._run(["usage"], side_effect=AntigravityError("sign in with 'agy'"))
        assert exit_info.value.code == 1
        assert "agy" in capsys.readouterr().err

    def test_a_failure_in_json_mode_stays_json(self, capsys):
        with pytest.raises(SystemExit):
            self._run(["usage", "--json"], side_effect=AntigravityError("offline"))
        assert "offline" in json.loads(capsys.readouterr().out)["error"]["message"]


class TestDispatch:
    def test_the_subcommand_is_routed(self):
        from claude_swap import cli

        with patch.object(cli, "_antigravity_command") as command:
            with patch.object(cli.sys, "argv", ["ccswap", "antigravity", "usage"]):
                try:
                    cli.main()
                except SystemExit:
                    pass
        command.assert_called_once_with(["usage"])
