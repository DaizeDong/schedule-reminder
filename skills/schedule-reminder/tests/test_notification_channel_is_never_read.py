#!/usr/bin/env python3
"""A provisioned notification channel must never be read back by the inbound bus.

`agent_center_admin.ensure_notification` writes `inbound: false`, and `ingest` is what has to
honor it. Those are two modules holding one assumption between them, and only the writing half
was under test: `test_agent_center_admin` asserts the flag lands in the registry, nothing
asserted that the reader obeys it.

That gap is not theoretical. A notification channel the bus reads gets its own alerts fed to the
judgment chain, which answers a "cc model map updated" post by filing a to-do about it. The same
shape (a channel read by something that could not act on it) is the incident that made
`ingest.channels()` sweep the whole guild in the first place, so the sweep is exactly what has to
keep being blocked here.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, "..", "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import agent_center_admin as admin  # noqa: E402
import ingest  # noqa: E402

NOTIFY_CHANNEL = "chan-notify"
ORDINARY_CHANNEL = "chan-ordinary"


class FakeDiscord:
    """Just enough of the admin client to provision one notification stream."""

    def __init__(self):
        self.channels = []

    def list_channels(self, guild_id):
        return list(self.channels)

    def create_category(self, guild_id, name):
        value = {"id": "cat-notify", "name": name, "type": 4, "parent_id": None}
        self.channels.append(value)
        return value

    def create_text_channel(self, guild_id, name, parent_id):
        value = {"id": NOTIFY_CHANNEL, "name": name, "type": 0, "parent_id": parent_id}
        self.channels.append(value)
        return value

    def move_channel(self, channel_id, parent_id):  # pragma: no cover - not reached here
        raise AssertionError("a freshly created channel must not need moving")


def provision(tmp_path):
    """Return the registry dict exactly as ensure_notification leaves it on disk."""
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({
        "guild_id": "guild-1",
        "reader": {"bot_token": "SECRET"},
        "streams": {"general": {"channel_id": "chan-general"}},
    }), encoding="utf-8")
    admin.ensure_notification(
        str(path), "model-mapping", "specific-notifications", "model-mapping",
        "cc-model-refresh", "model map changes", client=FakeDiscord(),
    )
    with open(str(path), encoding="utf-8") as fh:
        return json.load(fh)


def test_provisioned_notification_stream_is_not_polled(tmp_path):
    reg = provision(tmp_path)
    assert "model-mapping" in reg["streams"], "provisioning must register the stream at all"
    assert "model-mapping" not in ingest._streams(reg)
    assert "general" in ingest._streams(reg), "an ordinary stream must still be polled"


def test_guild_sweep_cannot_rediscover_the_notification_channel(tmp_path, monkeypatch):
    reg = provision(tmp_path)
    guild_listing = [
        {"id": NOTIFY_CHANNEL, "name": "model-mapping", "type": 0, "parent_id": "cat-notify"},
        {"id": ORDINARY_CHANNEL, "name": "somewhere-else", "type": 0, "parent_id": None},
    ]
    monkeypatch.setattr(ingest, "_get", lambda url, token: guild_listing)

    found = dict((cid, name) for name, cid in ingest.discovered_channels(reg, "SECRET"))

    assert NOTIFY_CHANNEL not in found, "the notification channel was swept back in"
    # Control: without this the test would also pass if discovery simply returned nothing.
    assert ORDINARY_CHANNEL in found, "discovery of ordinary channels must still work"
