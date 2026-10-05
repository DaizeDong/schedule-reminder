#!/usr/bin/env python3
"""The migration gate must fail on the exact state it was written to catch.

The state is: the category exists, the channel exists, the bot can post to it, the registry entry is
correct -- and the production sender still has its own Discord client pointed at the operator's DM.
`agent_center_admin.py check-notification --probe` returns 0 in that state, because the probe is
posted by the checker itself and never touches the sender. The route check is supposed to be the
thing that says no, so its negative controls are what actually has to be tested.

Everything here is hermetic: fake senders written into tmp_path, a fake Discord read-back, a fake
registry. The live version of this check is
`scripts/verify_model_mapping_route.py`, which drives the real installed senders and is not
importable on a CI runner that has none.
"""
import os
import sys

import pytest
from make_fixtures import model_mapping_node_sender_files, model_mapping_sender_script

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, "..", "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import relay  # noqa: E402
import verify_model_mapping_route as check  # noqa: E402

CHANNEL_ID = "chan-1"
CATEGORY_ID = "cat-1"

NOTIFY_SENDER = model_mapping_sender_script('NOTIFY_SENDER')

APPLY_SENDER = model_mapping_sender_script('APPLY_SENDER')


def make_sender(tmp_path, filename, body, stream, monkeypatch, **flags):
    """Write a fake production sender and point the environment at its outbox."""
    directory = tmp_path / "sender"
    directory.mkdir(exist_ok=True)
    (directory / filename).write_text(body, encoding="utf-8")
    outbox = tmp_path / "outbox.txt"
    outbox.write_text("", encoding="utf-8")
    monkeypatch.setenv("FAKE_STREAM", stream)
    monkeypatch.setenv("FAKE_OUTBOX", str(outbox))
    for key, value in flags.items():
        monkeypatch.setenv(key, value)
    return str(directory), outbox


def wire_discord(monkeypatch, outbox, channel_name="model-mapping",
                 category_name="specific-notifications", webhook=None):
    registry = {"guild_id": "g", "reader": {"bot_token": "SECRET"},
                "streams": {"model-mapping": {"channel_id": CHANNEL_ID}}}
    if webhook:
        registry["streams"]["model-mapping"]["webhook"] = webhook
    monkeypatch.setattr(relay, "load_registry", lambda: registry)

    def fake_get(path, token):
        if path == "/channels/%s" % CHANNEL_ID:
            return {"id": CHANNEL_ID, "name": channel_name, "parent_id": CATEGORY_ID}
        if path == "/channels/%s" % CATEGORY_ID:
            return {"id": CATEGORY_ID, "name": category_name}
        if path.startswith("/channels/%s/messages" % CHANNEL_ID):
            lines = outbox.read_text(encoding="utf-8").splitlines()
            return [{"content": line} for line in reversed(lines)]
        raise AssertionError("unexpected Discord read: %s" % path)

    monkeypatch.setattr(check, "_get", fake_get)


def row(sender_dir, filename, entry, channel="model-mapping"):
    return {"stream": "model-mapping", "channel": channel,
            "category": "specific-notifications", "sender_dir": sender_dir,
            "sender_file": filename, "entry": entry, "desc": "test"}


def test_a_migrated_notify_sender_passes(tmp_path, monkeypatch):
    directory, outbox = make_sender(tmp_path, "refresh.py", NOTIFY_SENDER,
                                    "model-mapping", monkeypatch)
    wire_discord(monkeypatch, outbox)
    result = check.verify_one(row(directory, "refresh.py", "notify"), attempts=1)
    assert result["ok"] is True
    assert result["marker"] in outbox.read_text(encoding="utf-8"), (
        "the message the check looked for must be the one the sender actually wrote")


@pytest.mark.parametrize("reload_with_allow_drop", [False, True])
def test_a_migrated_apply_map_sender_passes(tmp_path, monkeypatch, reload_with_allow_drop):
    flags = {"FAKE_RELOAD_ALLOW_DROP": "1"} if reload_with_allow_drop else {}
    directory, outbox = make_sender(tmp_path, "apply.py", APPLY_SENDER,
                                    "model-mapping", monkeypatch, **flags)
    wire_discord(monkeypatch, outbox)
    assert check.verify_one(row(directory, "apply.py", "apply-map"), attempts=1)["ok"] is True


def test_a_sender_whose_message_arrives_in_english_fails(tmp_path, monkeypatch):
    """A delivered notification also satisfies its configured language requirement."""
    directory, outbox = make_sender(tmp_path, "apply.py", APPLY_SENDER, "model-mapping",
                                    monkeypatch, FAKE_ENGLISH="1")
    wire_discord(monkeypatch, outbox)
    with pytest.raises(check.RouteError) as exc:
        check.verify_one(row(directory, "apply.py", "apply-map"), attempts=1)
    assert "Simplified Chinese" in str(exc.value)


def test_a_missing_language_rule_fails_rather_than_skipping(tmp_path, monkeypatch):
    """A rule that cannot be loaded must not quietly become a route that was never checked."""
    directory, outbox = make_sender(tmp_path, "apply.py", APPLY_SENDER,
                                    "model-mapping", monkeypatch)
    wire_discord(monkeypatch, outbox)
    monkeypatch.setattr(check, "_RULE_PATH", str(tmp_path / "not-installed.py"))
    with pytest.raises(check.RouteError) as exc:
        check.verify_one(row(directory, "apply.py", "apply-map"), attempts=1)
    assert "language rule is not installed" in str(exc.value)
    assert outbox.read_text(encoding="utf-8") == ""


def test_a_sender_that_announces_nothing_fails(tmp_path, monkeypatch):
    """The incident: channel provisioned and probed green, sender never repointed."""
    directory, outbox = make_sender(tmp_path, "refresh.py", NOTIFY_SENDER,
                                    "model-mapping", monkeypatch, FAKE_SILENT="1")
    wire_discord(monkeypatch, outbox)
    with pytest.raises(check.RouteError, match="never appeared"):
        check.verify_one(row(directory, "refresh.py", "notify"), attempts=1)
    assert outbox.read_text(encoding="utf-8") == "", "the fake sender was supposed to stay silent"


def test_an_apply_map_sender_that_announces_nothing_fails(tmp_path, monkeypatch):
    directory, outbox = make_sender(tmp_path, "apply.py", APPLY_SENDER,
                                    "model-mapping", monkeypatch, FAKE_SILENT="1")
    wire_discord(monkeypatch, outbox)
    with pytest.raises(check.RouteError, match="never appeared"):
        check.verify_one(row(directory, "apply.py", "apply-map"), attempts=1)


def test_a_notifier_that_cannot_report_delivery_fails(tmp_path, monkeypatch):
    """Delivering while returning None is the old swallow-everything notifier, and it is not enough."""
    directory, outbox = make_sender(tmp_path, "refresh.py", NOTIFY_SENDER,
                                    "model-mapping", monkeypatch, FAKE_MUTE_VERDICT="1")
    wire_discord(monkeypatch, outbox)
    with pytest.raises(check.RouteError, match="did not report a successful delivery"):
        check.verify_one(row(directory, "refresh.py", "notify"), attempts=1)


def test_a_sender_still_on_another_stream_fails(tmp_path, monkeypatch):
    directory, outbox = make_sender(tmp_path, "refresh.py", NOTIFY_SENDER,
                                    "infra", monkeypatch)
    wire_discord(monkeypatch, outbox)
    with pytest.raises(check.RouteError, match="announces on stream"):
        check.verify_one(row(directory, "refresh.py", "notify"), attempts=1)


def test_an_apply_map_sender_reporting_no_change_fails(tmp_path, monkeypatch):
    directory, outbox = make_sender(tmp_path, "apply.py", APPLY_SENDER,
                                    "model-mapping", monkeypatch, FAKE_NO_CHANGE="1")
    wire_discord(monkeypatch, outbox)
    with pytest.raises(check.RouteError, match="never reached its notifier"):
        check.verify_one(row(directory, "apply.py", "apply-map"), attempts=1)


def test_a_missing_sender_is_a_failure_not_a_pass(tmp_path, monkeypatch):
    directory, outbox = make_sender(tmp_path, "refresh.py", NOTIFY_SENDER,
                                    "model-mapping", monkeypatch)
    wire_discord(monkeypatch, outbox)
    with pytest.raises(check.RouteError, match="no sender at"):
        check.verify_one(row(directory, "not-installed.py", "notify"), attempts=1)


def test_a_channel_outside_the_category_fails(tmp_path, monkeypatch):
    directory, outbox = make_sender(tmp_path, "refresh.py", NOTIFY_SENDER,
                                    "model-mapping", monkeypatch)
    wire_discord(monkeypatch, outbox, category_name="general-chatter")
    with pytest.raises(check.RouteError, match="filed under"):
        check.verify_one(row(directory, "refresh.py", "notify"), attempts=1)


def test_a_stream_bound_to_the_wrong_channel_fails(tmp_path, monkeypatch):
    directory, outbox = make_sender(tmp_path, "refresh.py", NOTIFY_SENDER,
                                    "model-mapping", monkeypatch)
    wire_discord(monkeypatch, outbox, channel_name="somewhere-else")
    with pytest.raises(check.RouteError, match="is bound to"):
        check.verify_one(row(directory, "refresh.py", "notify"), attempts=1)


def test_a_leftover_webhook_fails(tmp_path, monkeypatch):
    """A webhook is bound to the channel it was minted for, so it can outvote the new channel_id."""
    directory, outbox = make_sender(tmp_path, "refresh.py", NOTIFY_SENDER,
                                    "model-mapping", monkeypatch)
    wire_discord(monkeypatch, outbox, webhook="https://discord.example/api/webhooks/old")
    with pytest.raises(check.RouteError, match="webhook"):
        check.verify_one(row(directory, "refresh.py", "notify"), attempts=1)


def test_every_notification_type_owns_its_own_channel():
    check.check_routes_are_distinct()  # the shipped table must already satisfy this


def test_two_types_sharing_one_channel_is_rejected(monkeypatch):
    shared = dict(check.ROUTES[0])
    monkeypatch.setattr(check, "ROUTES", (check.ROUTES[0], dict(shared, stream="other-stream")))
    with pytest.raises(check.RouteError, match="share the same channel"):
        check.check_routes_are_distinct()


def test_the_route_table_is_well_formed():
    assert check.ROUTES, "an empty table would make every check above vacuous"
    for entry in check.ROUTES:
        missing = {"stream", "channel", "category", "sender_dir", "sender_file", "entry",
                   "desc"} - set(entry)
        assert not missing, "route %r is missing %s" % (entry.get("stream"), sorted(missing))
        assert entry["entry"] in ("notify", "apply-map"), (
            "route %r uses an entry style _drive cannot drive" % entry["stream"])
        assert entry["category"] == "specific-notifications", (
            "route %r is not filed in the specific-notifications folder" % entry["stream"])


def test_missing_installation_configuration_fails_before_delivery(monkeypatch):
    monkeypatch.delenv("SCHEDULE_ROUTE_SCRIPTS_DIR", raising=False)
    monkeypatch.setattr(check, "_drive", lambda *_args: pytest.fail("unconfigured verifier sent a notification"))
    with pytest.raises(check.RouteError, match="SCHEDULE_ROUTE_SCRIPTS_DIR"):
        check.verify_one(check.ROUTES[0], attempts=1)


def test_relative_installation_configuration_is_rejected(monkeypatch):
    monkeypatch.setenv("SCHEDULE_ROUTE_SCRIPTS_DIR", "route-scripts")
    with pytest.raises(check.RouteError, match="absolute"):
        check._scripts_root()


def test_configured_root_resolves_generated_sender(tmp_path, monkeypatch):
    directory, outbox = make_sender(tmp_path, "refresh.py", NOTIFY_SENDER,
                                    "model-mapping", monkeypatch)
    wire_discord(monkeypatch, outbox)
    rule_path = os.path.join(check._scripts_root(), "notification_language.py")
    monkeypatch.setattr(check, "_RULE_PATH", rule_path)
    monkeypatch.setenv("SCHEDULE_ROUTE_SCRIPTS_DIR", str(tmp_path))
    result = check.verify_one(row("sender", "refresh.py", "notify"), attempts=1)
    assert result["ok"] is True
    assert result["sender"] == os.path.join(directory, "refresh.py")


def make_node_sender(tmp_path, monkeypatch, **flags):
    directory = tmp_path / "node-sender"
    directory.mkdir()
    for name, text in model_mapping_node_sender_files().items():
        (directory / name).write_text(text, encoding="utf-8")
    outbox = tmp_path / "node-outbox.txt"
    outbox.write_text("", encoding="utf-8")
    monkeypatch.setenv("FAKE_STREAM", "model-mapping")
    monkeypatch.setenv("FAKE_OUTBOX", str(outbox))
    for name, value in flags.items():
        monkeypatch.setenv(name, value)
    wire_discord(monkeypatch, outbox)
    selected = dict(row(str(directory), "model-refresh.js", "notify"), runtime="node")
    return selected, outbox


def test_node_sender_delivers_through_its_real_module_entrypoint(tmp_path, monkeypatch):
    selected, outbox = make_node_sender(tmp_path, monkeypatch)
    result = check.verify_one(selected, attempts=1)
    assert result["marker"] in outbox.read_text(encoding="utf-8")
    assert result["ok"] is True


def test_node_sender_wrong_stream_fails_before_delivery(tmp_path, monkeypatch):
    selected, outbox = make_node_sender(tmp_path, monkeypatch, FAKE_STREAM="other-stream")
    with pytest.raises(check.RouteError, match="announces on stream"):
        check.verify_one(selected, attempts=1)
    assert outbox.read_text(encoding="utf-8") == ""


def test_node_sender_failure_is_reported(tmp_path, monkeypatch):
    selected, outbox = make_node_sender(tmp_path, monkeypatch, FAKE_REFUSE="1")
    with pytest.raises(check.RouteError, match="notify.*did not deliver"):
        check.verify_one(selected, attempts=1)
    assert outbox.read_text(encoding="utf-8") == ""


def test_sender_directory_override_selects_original_source(tmp_path, monkeypatch):
    selected, outbox = make_node_sender(tmp_path, monkeypatch)
    monkeypatch.setenv("SCHEDULE_ROUTE_CODEX_SENDER_DIR", selected["sender_dir"])
    selected.update(sender_dir="absent-install", sender_dir_env="SCHEDULE_ROUTE_CODEX_SENDER_DIR")
    assert check.verify_one(selected, attempts=1)["ok"] is True
    assert outbox.read_text(encoding="utf-8")


@pytest.mark.parametrize("value", ["", "relative-source"])
def test_sender_directory_override_must_be_absolute(tmp_path, monkeypatch, value):
    selected, outbox = make_node_sender(tmp_path, monkeypatch)
    monkeypatch.setenv("SCHEDULE_ROUTE_CODEX_SENDER_DIR", value)
    selected["sender_dir_env"] = "SCHEDULE_ROUTE_CODEX_SENDER_DIR"
    with pytest.raises(check.RouteError, match="absolute sender directory"):
        check.verify_one(selected, attempts=1)
    assert outbox.read_text(encoding="utf-8") == ""
