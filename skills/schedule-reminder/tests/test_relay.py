#!/usr/bin/env python3
"""Hermetic tests for relay.py (Agent Center multi-stream egress). No network."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, "..", "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import relay  # noqa: E402


def _registry(tmp_path, streams):
    p = tmp_path / "registry.json"
    p.write_text(json.dumps({"guild_id": "1", "streams": streams}), encoding="utf-8")
    return str(p)


def test_missing_registry_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CENTER_CONFIG", str(tmp_path / "nope.json"))
    assert relay.load_registry() == {}


def test_relay_known_stream_dryrun(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CENTER_CONFIG",
                       _registry(tmp_path, {"mail": {"webhook": "https://h/api/webhooks/1/t", "username": "mail"}}))
    monkeypatch.setenv("AGENT_CENTER_RELAY_DRYRUN", "1")
    assert relay.relay("mail", "hello") is True


def test_unknown_stream_falls_back_to_big_brother(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CENTER_CONFIG", _registry(tmp_path, {"mail": {"webhook": "https://h/api/webhooks/1/t"}}))
    calls = {}
    monkeypatch.setattr(relay, "_big_brother", lambda t: (calls.update(text=t), True)[1])
    assert relay.relay("ghost", "boo") is True
    assert "[ghost]" in calls["text"]  # stream name preserved for context


def test_digest_uses_big_brother(monkeypatch):
    seen = {}
    monkeypatch.setattr(relay, "_big_brother", lambda t: (seen.update(t=t), True)[1])
    assert relay.digest("daily summary") is True
    assert seen["t"] == "daily summary"


def test_list_never_leaks_webhook(monkeypatch, tmp_path, capsys):
    secret = "deadbeefSECRETtok123"
    monkeypatch.setenv("AGENT_CENTER_CONFIG",
                       _registry(tmp_path, {"mail": {"webhook": "https://h/api/webhooks/9/" + secret, "username": "mail"}}))
    rc = relay.main(["list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert secret not in out  # the webhook token must never appear in `list` output
    assert "mail" in out  # but the stream name + safe metadata is shown


def test_health_ok_and_bad(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_CENTER_CONFIG",
                       _registry(tmp_path, {"mail": {"webhook": "https://h/api/webhooks/1/t"}}))
    assert relay.main(["health"]) == 0
    capsys.readouterr()
    monkeypatch.setenv("AGENT_CENTER_CONFIG", _registry(tmp_path, {"mail": {"webhook": "not-a-url"}}))
    assert relay.main(["health"]) == 1


def test_send_cli_dryrun(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CENTER_CONFIG",
                       _registry(tmp_path, {"mail": {"webhook": "https://h/api/webhooks/1/t", "username": "mail"}}))
    monkeypatch.setenv("AGENT_CENTER_RELAY_DRYRUN", "1")
    assert relay.main(["send", "--stream", "mail", "--text", "hi"]) == 0
    assert relay.main(["send", "--stream", "mail", "--json", '{"content":"x","username":"u"}']) == 0


# ------------------------------------------------------------------ the bot transport (files, any channel)
# These capture the actual outgoing request rather than stubbing _post_bot, so a wrong transport
# choice, a lost attachment or a missing auth header fails the test instead of passing silently.
def _capture(monkeypatch):
    """Record every request relay would put on the wire. Returns the list it appends to."""
    sent = []

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout=None):
        sent.append({"url": req.full_url, "method": req.get_method(),
                     "headers": {k.lower(): v for k, v in req.header_items()},
                     "body": req.data or b""})
        return _Resp()

    monkeypatch.setattr(relay.urllib.request, "urlopen", fake_urlopen)
    return sent


_BOTREG = {"mail": {"webhook": "https://h/api/webhooks/1/t", "username": "mail",
                    "channel_id": "555"}}


def _bot_registry(tmp_path, streams=None):
    p = tmp_path / "registry.json"
    p.write_text(json.dumps({"guild_id": "1", "streams": streams or _BOTREG,
                             "reader": {"bot_token": "BOTTOK"}}), encoding="utf-8")
    return str(p)


def test_stream_only_still_uses_the_webhook(monkeypatch, tmp_path):
    """The frozen path must not drift onto the bot just because a bot token now exists."""
    monkeypatch.setenv("AGENT_CENTER_CONFIG", _bot_registry(tmp_path))
    sent = _capture(monkeypatch)
    assert relay.send("hello", stream="mail") is True
    assert len(sent) == 1
    assert sent[0]["url"].startswith("https://h/api/webhooks/"), "no files, no channel_id -> webhook"
    assert "authorization" not in sent[0]["headers"], "a webhook post must not carry the bot token"


def test_channel_id_switches_to_the_bot(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CENTER_CONFIG", _bot_registry(tmp_path))
    sent = _capture(monkeypatch)
    assert relay.send("答复", channel_id="999") is True
    assert sent[0]["url"] == "https://discord.com/api/v10/channels/999/messages"
    assert sent[0]["headers"]["authorization"] == "Bot BOTTOK"
    assert json.loads(sent[0]["body"].decode("utf-8"))["content"] == "答复"


def test_files_force_the_bot_even_with_only_a_stream(monkeypatch, tmp_path):
    """A webhook cannot carry a file, so an attachment must reroute onto the bot by itself."""
    monkeypatch.setenv("AGENT_CENTER_CONFIG", _bot_registry(tmp_path))
    png = tmp_path / "pic.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nPAYLOAD")
    sent = _capture(monkeypatch)
    assert relay.send("caption", stream="mail", files=[str(png)]) is True
    req = sent[0]
    assert req["url"].endswith("/channels/555/messages"), "resolved the stream's own channel"
    assert req["headers"]["content-type"].startswith("multipart/form-data; boundary=")
    assert b"PAYLOAD" in req["body"], "the file bytes must actually be in the request"
    assert b'filename="pic.png"' in req["body"]
    assert b'"filename": "pic.png"' in req["body"], "and declared in payload_json attachments"


def test_bot_send_never_falls_back_to_dm(monkeypatch, tmp_path):
    """A channel-addressed message that cannot be delivered fails visibly. Rerouting the answer to
    'what you just typed in #here' into a DM is a worse outcome than a False the caller can report."""
    monkeypatch.setenv("AGENT_CENTER_CONFIG",
                       _bot_registry(tmp_path, {"mail": {"webhook": "https://h/api/webhooks/1/t"}}))
    called = []
    monkeypatch.setattr(relay, "_big_brother", lambda t: called.append(t) or True)

    def boom(req, timeout=None):
        raise OSError("discord said no")

    monkeypatch.setattr(relay.urllib.request, "urlopen", boom)
    assert relay.send("答复", channel_id="999") is False
    assert called == [], "the bot transport must not reach for the Big Brother fallback"


def test_bot_send_without_token_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CENTER_CONFIG", _registry(tmp_path, {"mail": {"webhook": "https://h/api/webhooks/1/t"}}))
    sent = _capture(monkeypatch)
    assert relay.send("x", channel_id="999") is False
    assert sent == [], "no token means no request at all, not an unauthenticated one"


def test_send_cli_channel_and_file(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CENTER_CONFIG", _bot_registry(tmp_path))
    monkeypatch.setenv("AGENT_CENTER_RELAY_DRYRUN", "1")
    png = tmp_path / "a.png"
    png.write_bytes(b"x")
    assert relay.main(["send", "--channel-id", "42", "--text", "hi"]) == 0
    assert relay.main(["send", "--channel-id", "42", "--text", "hi", "--file", str(png)]) == 0
    assert relay.main(["send", "--text", "hi"]) == 2, "neither --stream nor --channel-id is an error"
