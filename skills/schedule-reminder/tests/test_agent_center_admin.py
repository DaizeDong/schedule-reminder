#!/usr/bin/env python3
"""Hermetic tests for dedicated Agent Center notification-channel provisioning."""
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, "..", "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import agent_center_admin as admin  # noqa: E402


class FakeDiscord:
    def __init__(self, channels=None):
        self.channels = list(channels or [])
        self.created_categories = 0
        self.created_channels = 0
        self.moves = 0

    def list_channels(self, guild_id):
        assert guild_id == "guild-1"
        return list(self.channels)

    def create_category(self, guild_id, name):
        self.created_categories += 1
        value = {"id": "cat-1", "name": name, "type": 4, "parent_id": None}
        self.channels.append(value)
        return value

    def create_text_channel(self, guild_id, name, parent_id):
        self.created_channels += 1
        value = {"id": "chan-1", "name": name, "type": 0, "parent_id": parent_id}
        self.channels.append(value)
        return value

    def move_channel(self, channel_id, parent_id):
        self.moves += 1
        channel = next(c for c in self.channels if c["id"] == channel_id)
        channel["parent_id"] = parent_id
        return dict(channel)


def registry(tmp_path, streams=None):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"guild_id": "guild-1", "reader": {"bot_token": "SECRET"},
                                "streams": streams or {}}), encoding="utf-8")
    return str(path)


def test_ensure_creates_category_channel_registers_bot_stream_and_probes(tmp_path):
    path = registry(tmp_path)
    api = FakeDiscord()
    sent = []

    result = admin.ensure_notification(
        path, "model-mapping", "specific-notifications", "model-mapping",
        "cc-model-refresh", "cc model mapping changes", test_text="verification",
        client=api, sender=lambda *args: sent.append(args) or True,
    )

    assert result["ok"] is True and result["probe_sent"] is True
    assert api.created_categories == 1 and api.created_channels == 1
    with open(path, encoding="utf-8") as fh:
        saved = json.load(fh)
    entry = saved["streams"]["model-mapping"]
    assert entry["channel_id"] == "chan-1"
    assert entry["category_id"] == "cat-1"
    assert entry["inbound"] is False and entry["listen"] is False
    assert "webhook" not in entry
    assert sent == [(path, "model-mapping", "verification")]


def test_ensure_is_idempotent(tmp_path):
    path = registry(tmp_path)
    api = FakeDiscord([
        {"id": "cat-1", "name": "specific-notifications", "type": 4, "parent_id": None},
        {"id": "chan-1", "name": "model-mapping", "type": 0, "parent_id": "cat-1"},
    ])
    first = admin.ensure_notification(path, "model-mapping", "specific-notifications",
                                      "model-mapping", "cc-model-refresh", "changes", client=api)
    second = admin.ensure_notification(path, "model-mapping", "specific-notifications",
                                       "model-mapping", "cc-model-refresh", "changes", client=api)
    assert first["category"]["created"] is False
    assert second["channel"]["created"] is False
    assert api.created_categories == api.created_channels == api.moves == 0


def test_existing_channel_is_moved_under_notification_category(tmp_path):
    path = registry(tmp_path)
    api = FakeDiscord([
        {"id": "cat-1", "name": "specific-notifications", "type": 4, "parent_id": None},
        {"id": "chan-1", "name": "model-mapping", "type": 0, "parent_id": "old-cat"},
    ])
    result = admin.ensure_notification(path, "model-mapping", "specific-notifications",
                                       "model-mapping", "cc-model-refresh", "changes", client=api)
    assert result["channel"]["moved"] is True
    assert api.moves == 1


def test_check_rejects_wrong_parent_and_stale_webhook(tmp_path):
    channels = [
        {"id": "cat-1", "name": "specific-notifications", "type": 4, "parent_id": None},
        {"id": "chan-1", "name": "model-mapping", "type": 0, "parent_id": "wrong"},
    ]
    path = registry(tmp_path, {"model-mapping": {"channel_id": "chan-1"}})
    with pytest.raises(admin.AdminError, match="not inside"):
        admin.check_notification(path, "model-mapping", "specific-notifications",
                                 "model-mapping", client=FakeDiscord(channels))

    channels[1]["parent_id"] = "cat-1"
    path = registry(tmp_path, {"model-mapping": {"channel_id": "chan-1",
                                                  "webhook": "https://old.example"}})
    with pytest.raises(admin.AdminError, match="still has a webhook"):
        admin.check_notification(path, "model-mapping", "specific-notifications",
                                 "model-mapping", client=FakeDiscord(channels))


def test_duplicate_names_fail_closed(tmp_path):
    path = registry(tmp_path)
    duplicate = [
        {"id": "cat-1", "name": "specific-notifications", "type": 4},
        {"id": "cat-2", "name": "Specific-Notifications", "type": 4},
    ]
    with pytest.raises(admin.AdminError, match="multiple Discord categories"):
        admin.ensure_notification(path, "model-mapping", "specific-notifications",
                                  "model-mapping", "cc-model-refresh", "changes",
                                  client=FakeDiscord(duplicate))
