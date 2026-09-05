# -*- coding: utf-8 -*-
"""An over-long relay body must arrive in parts, never vanish.

The failure this pins down: Discord rejects a single message over 2000 characters with HTTP 400,
and the relay used to have no length handling at all, so the caller lost the WHOLE message rather
than its tail. It fails in the worst direction, because an alert body that enumerates failures
grows with the number of failures: the more there is to report, the less likely the report arrives.
Measured before the fix: three consecutive 400s at 2066, 2072 and 2006 characters.

Every test here is written so it would have failed against the old code.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import relay  # noqa: E402


LIMIT = relay._DISCORD_LIMIT


def test_short_body_is_untouched():
    """Negative control. A body under the wall must pass through byte for byte, with no marker.

    Without this, a fix that marked every message would be indistinguishable from a fix that
    only marked the ones that needed it.
    """
    body = "one\ntwo\nthree"
    assert relay.split_for_discord(body) == [body]


def test_body_exactly_at_the_limit_is_not_split():
    body = "x" * LIMIT
    assert relay.split_for_discord(body) == [body]


def test_body_one_over_the_limit_is_split():
    body = "x" * (LIMIT + 1)
    parts = relay.split_for_discord(body)
    assert len(parts) > 1


@pytest.mark.parametrize("size", [2006, 2066, 2072, 5000, 20000])
def test_every_part_fits_and_nothing_is_lost(size):
    """The two properties that matter: each part is deliverable, and the concatenation is intact."""
    body = "\n".join("line %04d: %s" % (i, "y" * 40) for i in range(size // 50 + 1))[:size]
    parts = relay.split_for_discord(body)
    assert all(len(p) <= LIMIT for p in parts), "a part would still be rejected by Discord"
    # Strip the "(n/m)\n" marker off each part and rejoin.
    stripped = []
    for p in parts:
        head, _, rest = p.partition("\n")
        assert head.startswith("(") and head.endswith(")"), "part is missing its marker"
        stripped.append(rest)
    assert "\n".join(stripped) == body, "text was lost or duplicated by the split"


def test_parts_are_numbered_so_a_reader_can_tell_more_is_coming():
    body = "z" * 5000
    parts = relay.split_for_discord(body)
    n = len(parts)
    for i, p in enumerate(parts):
        assert p.startswith("(%d/%d)\n" % (i + 1, n))


def test_a_single_line_longer_than_the_budget_is_hard_split_not_dropped():
    """Splitting on line boundaries cannot handle a line that is itself over budget.
    It must hard-split rather than drop, and the content must still round-trip."""
    body = "short\n" + ("q" * 6000) + "\ntail"
    parts = relay.split_for_discord(body)
    assert all(len(p) <= LIMIT for p in parts)
    joined = "\n".join(p.partition("\n")[2] for p in parts)
    assert "q" * 6000 in joined.replace("\n", "")
    assert joined.startswith("short")
    assert joined.rstrip().endswith("tail")


def test_relay_delivers_every_part_and_reports_failure_if_one_is_lost(monkeypatch):
    """A partial delivery must NOT be reported as success: that would say 'sent' about a
    message whose tail never arrived, which is the exact lie this module exists to avoid."""
    sent = []

    def fake_post(url, payload):
        sent.append(payload["content"])
        return len(sent) != 2          # the second part fails

    monkeypatch.setattr(relay, "load_registry",
                        lambda: {"streams": {"infra": {"webhook": "https://example.com/hook"}}})
    monkeypatch.setattr(relay, "_post_webhook", fake_post)

    ok = relay.relay("infra", "w" * 5000)
    assert len(sent) >= 3, "all parts must be attempted, not abandoned at the first failure"
    assert ok is False, "one lost part must make the whole delivery report failure"


def test_relay_reports_success_when_every_part_lands(monkeypatch):
    sent = []
    monkeypatch.setattr(relay, "load_registry",
                        lambda: {"streams": {"infra": {"webhook": "https://example.com/hook"}}})
    monkeypatch.setattr(relay, "_post_webhook",
                        lambda url, payload: (sent.append(payload["content"]), True)[1])
    assert relay.relay("infra", "w" * 5000) is True
    assert len(sent) >= 3
    assert all(len(c) <= LIMIT for c in sent)


def test_big_brother_fallback_also_splits(monkeypatch):
    """The fallback is the last channel: an over-long body must not die on the one path
    that exists to catch failures of the others."""
    seen = []

    class FakeBB:
        @staticmethod
        def send_dm(t):
            seen.append(t)
            return True

    monkeypatch.setitem(sys.modules, "bigbrother", FakeBB)
    assert relay._big_brother("v" * 5000) is True
    assert len(seen) >= 3
    assert all(len(t) <= LIMIT for t in seen)
