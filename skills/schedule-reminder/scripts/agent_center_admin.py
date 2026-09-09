#!/usr/bin/env python3
"""Provision and verify dedicated Discord notification channels for Agent Center.

The registry remains the source of truth. This command reconciles one notification stream into a
Discord category, records its channel id, and proves the ordinary ``relay.py send --stream`` path by
posting a test message. It deliberately uses the existing Agent Center bot instead of minting a
webhook per low-volume notification type, so no new secret is created.

Output is JSON and never includes the bot token or webhook URLs.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

import relay

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

_API = "https://discord.com/api/v10"
_UA = "AgentCenter-Admin/1.0 (+https://discord.com)"
_CATEGORY = 4
_TEXT = 0


class AdminError(RuntimeError):
    pass


def _load_registry(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            reg = json.load(fh)
    except Exception as exc:
        raise AdminError("registry unreadable: %s" % exc) from exc
    if not isinstance(reg, dict):
        raise AdminError("registry root must be an object")
    if not reg.get("guild_id"):
        raise AdminError("registry.guild_id is required")
    if not (reg.get("reader") or {}).get("bot_token"):
        raise AdminError("registry.reader.bot_token is required")
    return reg


def _write_registry(path: str, reg: dict) -> None:
    """Crash-safe same-directory replacement; the secret never leaves its config directory."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, temp_path = tempfile.mkstemp(prefix="registry.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(reg, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(temp_path, os.stat(path).st_mode)
        except OSError:
            pass
        os.replace(temp_path, path)
    finally:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass


class DiscordAPI:
    def __init__(self, token: str, opener=None):
        self.token = token
        self.opener = opener or urllib.request.urlopen

    def request(self, method: str, path: str, payload: dict | None = None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Authorization": "Bot %s" % self.token, "User-Agent": _UA}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(_API + path, data=data, method=method, headers=headers)
        try:
            with self.opener(req, timeout=30) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise AdminError("Discord HTTP %s for %s %s: %s" %
                             (exc.code, method, path, detail)) from exc
        except Exception as exc:
            raise AdminError("Discord request failed for %s %s: %s" %
                             (method, path, exc)) from exc
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise AdminError("Discord returned invalid JSON for %s %s" % (method, path)) from exc

    def list_channels(self, guild_id: str) -> list[dict]:
        value = self.request("GET", "/guilds/%s/channels" % guild_id)
        if not isinstance(value, list):
            raise AdminError("Discord channel listing was not an array")
        return value

    def create_category(self, guild_id: str, name: str) -> dict:
        return self.request("POST", "/guilds/%s/channels" % guild_id,
                            {"name": name, "type": _CATEGORY})

    def create_text_channel(self, guild_id: str, name: str, parent_id: str) -> dict:
        return self.request("POST", "/guilds/%s/channels" % guild_id,
                            {"name": name, "type": _TEXT, "parent_id": parent_id})

    def move_channel(self, channel_id: str, parent_id: str) -> dict:
        return self.request("PATCH", "/channels/%s" % channel_id,
                            {"parent_id": parent_id})


def _one_named(channels: list[dict], name: str, kind: int, label: str) -> dict | None:
    matches = [c for c in channels
               if c.get("type") == kind and str(c.get("name", "")).casefold() == name.casefold()]
    if len(matches) > 1:
        raise AdminError("multiple Discord %s named %r; refusing an ambiguous migration" %
                         (label, name))
    return matches[0] if matches else None


def _safe_entry(entry: dict) -> dict:
    return {key: value for key, value in entry.items() if key != "webhook"}


def _default_sender(registry_path: str, stream: str, text: str) -> bool:
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    env = os.environ.copy()
    env["AGENT_CENTER_CONFIG"] = registry_path
    proc = subprocess.run(
        [sys.executable, os.path.join(os.path.dirname(__file__), "relay.py"), "send",
         "--stream", stream, "--text-b64", encoded],
        env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=60,
    )
    if proc.returncode:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise AdminError("Agent Center relay probe failed: %s" %
                         (detail or "exit %s" % proc.returncode))
    return True


def ensure_notification(registry_path: str, stream: str, category_name: str,
                        channel_name: str, skill: str, description: str,
                        username: str | None = None, test_text: str | None = None,
                        client=None, sender=None) -> dict:
    reg = _load_registry(registry_path)
    api = client or DiscordAPI(reg["reader"]["bot_token"])
    channels = api.list_channels(str(reg["guild_id"]))

    category = _one_named(channels, category_name, _CATEGORY, "categories")
    created_category = category is None
    if category is None:
        category = api.create_category(str(reg["guild_id"]), category_name)

    channel = _one_named(channels, channel_name, _TEXT, "text channels")
    created_channel = channel is None
    moved_channel = False
    if channel is None:
        channel = api.create_text_channel(str(reg["guild_id"]), channel_name, str(category["id"]))
    elif str(channel.get("parent_id") or "") != str(category["id"]):
        channel = api.move_channel(str(channel["id"]), str(category["id"]))
        moved_channel = True

    entry = dict((reg.setdefault("streams", {})).get(stream) or {})
    entry.update({
        "channel_id": str(channel["id"]),
        "category_id": str(category["id"]),
        "skill": skill,
        "desc": description,
        "username": username or stream,
        "inbound": False,
        "listen": False,
    })
    # Keeping an old webhook would silently retain the old channel binding and defeat the move.
    entry.pop("webhook", None)
    reg["streams"][stream] = entry
    _write_registry(registry_path, reg)

    probed = False
    if test_text:
        (sender or _default_sender)(registry_path, stream, test_text)
        probed = True
    return {
        "ok": True,
        "stream": stream,
        "category": {"id": str(category["id"]), "name": category_name,
                     "created": created_category},
        "channel": {"id": str(channel["id"]), "name": channel_name,
                    "created": created_channel, "moved": moved_channel},
        "registry_entry": _safe_entry(entry),
        "probe_sent": probed,
    }


def check_notification(registry_path: str, stream: str, category_name: str,
                       channel_name: str, probe_text: str | None = None,
                       client=None, sender=None) -> dict:
    reg = _load_registry(registry_path)
    entry = (reg.get("streams") or {}).get(stream)
    if not isinstance(entry, dict):
        raise AdminError("stream %r is not registered" % stream)
    api = client or DiscordAPI(reg["reader"]["bot_token"])
    channels = api.list_channels(str(reg["guild_id"]))
    category = _one_named(channels, category_name, _CATEGORY, "categories")
    channel = _one_named(channels, channel_name, _TEXT, "text channels")
    if category is None:
        raise AdminError("Discord category %r does not exist" % category_name)
    if channel is None:
        raise AdminError("Discord text channel %r does not exist" % channel_name)
    if str(channel.get("parent_id") or "") != str(category["id"]):
        raise AdminError("Discord channel %r is not inside category %r" %
                         (channel_name, category_name))
    if str(entry.get("channel_id") or "") != str(channel["id"]):
        raise AdminError("stream %r points at a different channel" % stream)
    if entry.get("webhook"):
        raise AdminError("stream %r still has a webhook; it may still target the old channel" % stream)
    probed = False
    if probe_text:
        (sender or _default_sender)(registry_path, stream, probe_text)
        probed = True
    return {"ok": True, "stream": stream,
            "category_id": str(category["id"]), "channel_id": str(channel["id"]),
            "probe_sent": probed}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent_center_admin.py")
    parser.add_argument("--registry", default=relay.registry_path())
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("ensure-notification", "check-notification"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--stream", required=True)
        cmd.add_argument("--category", required=True)
        cmd.add_argument("--channel", required=True)
    ensure = sub.choices["ensure-notification"]
    ensure.add_argument("--skill", required=True)
    ensure.add_argument("--description", required=True)
    ensure.add_argument("--username")
    ensure.add_argument("--test-text", default=None)
    check = sub.choices["check-notification"]
    check.add_argument("--probe", action="store_true")
    check.add_argument("--probe-text", default="Agent Center notification route verified")
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "ensure-notification":
            result = ensure_notification(args.registry, args.stream, args.category, args.channel,
                                         args.skill, args.description, args.username, args.test_text)
        else:
            result = check_notification(args.registry, args.stream, args.category, args.channel,
                                        args.probe_text if args.probe else None)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except AdminError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
