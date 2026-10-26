#!/usr/bin/env python3
"""schedule-reminder — Agent Center multi-stream relay (the single Discord egress for all skills).

WHY THIS EXISTS
    Before this, every skill shelled out to the Big Brother DM relay, so all alerts piled into one
    DM stream. The Agent Center model gives each message *type* its own channel + identity. This
    module is the single, frozen egress every downstream skill calls — so the transport (webhook vs
    bot vs anything else) can change forever without touching any skill.

REGISTRY (secret, versioned only in the PRIVATE companion)
    Discovery order: env AGENT_CENTER_CONFIG, else the registry file in the Agent Center config dir.
    Shape: {"streams": {"<name>": {"webhook": "...", "username": "..."}}, "big_brother": {...}}
    A stream normally posts to its webhook; a notification-only stream may instead carry only a
    `channel_id` and use the registry's canonical bot token. Per-message `username` is available on
    the webhook path; the bot-only path uses the bot's Discord identity.

CONTRACT (frozen surface downstream skills depend on — subprocess, never import internals)
    relay(stream, content, username=None) -> bool          # True = delivered
    send(content, stream=..., channel_id=..., files=[...]) -> bool
    CLI:
      relay.py send   --stream NAME (--text T | --json '{"content":..,"username":..}')
      relay.py send   --channel-id ID --text T [--file PATH ...]
      relay.py send   --dm (--text T | --text-file PATH|-) [--file PATH ...]   # owner DM
      relay.py edit   --channel-id ID --message-id ID --text-file - [--file PATH ...]
      relay.py list                       # show configured streams (NO secrets)
      relay.py health                     # registry present? streams sane? (NO network, NO secrets)

TWO TRANSPORTS, ONE EGRESS
    Webhook is the default: it carries the per-stream identity (username + avatar) that makes the
    Agent Center readable at a glance, and it needs no bot permissions. But a webhook is BOUND to
    the channel it was created for and cannot carry a file, so two jobs are impossible on it:
    answering in whichever channel a command was typed in, and posting an image. Those go over the
    bot token instead (registry.reader.bot_token_ref or reader.bot_token, the same one ingest reads
    with; see bot_credential.py).

    Transport is selected without exposing transport details to the caller:
        files given, or channel_id given   -> bot
        stream has a webhook               -> webhook
        stream has only channel_id         -> bot
    This exists so a caller never has to know which one it is on. Before it, every job the webhook
    could not do grew its own hand written Discord client (three of them: the backdrop bot, the
    guestbook moderator, the promotion sender), each with its own UA, retry and 403 handling. The
    point of a single egress is that adding a capability here retires a fork out there.

ROBUSTNESS
    Unknown stream / missing registry  -> fall back to Big Brother DM (_big_brother) so a message is
    never silently lost; a one-line warning goes to stderr (never the webhook URL).
    A bot send has NO such fallback and returns False: it is addressed at one specific channel, and
    silently rerouting "the answer to what you just typed in #here" into a DM is worse than a
    visible failure the caller can report in place.

SECRETS
    Webhook URLs live ONLY in the registry file. This module never logs, prints, or echoes them.

GOTCHA (encoded here so it is never relearned)
    Discord/Cloudflare returns HTTP 403 for the default python-urllib User-Agent. A real UA header
    is mandatory; see _UA below.
"""
from __future__ import annotations

import argparse
import base64
import contextvars
import copy
from dataclasses import dataclass, field
import hashlib
import math
from pathlib import Path
import json
import os
import bot_credential
import machine_role
import private_data
import sys
import urllib.request
import uuid

# Output is always UTF-8 regardless of host console code page (Windows GBK consoles 403 emoji otherwise).
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Discord 403s the default urllib UA, a real User-Agent is mandatory.
_UA = "AgentCenter-Relay/1.0 (+https://discord.com)"
_API = "https://discord.com/api/v10"

# Discord rejects a single message whose `content` exceeds 2000 characters with HTTP 400,
# and the whole message is then lost. _CHUNK_BUDGET leaves room for the "(n/m)" marker that
# every part of a split message carries.
_DISCORD_LIMIT = 2000
_CHUNK_BUDGET = 1900

_REGISTRY_SNAPSHOT = contextvars.ContextVar("relay_registry_snapshot", default=None)


class PreparationError(ValueError):
    """A definite failure before any transport request; safe for delivery-only retry."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class _PreparedAttachment:
    """Transient immutable bytes; only their digest enters the private event receipt."""

    path: str
    data: bytes = field(repr=False)

    def __fspath__(self):
        return self.path


def prepare_command(command, *, stream, files=None, content=None, snapshot_dir=None):
    """Pin an explicit standalone notifier. Never resolve it as an Agent Center stream.

    The fingerprint identifies the owner-selected argv/cwd/protocol, not the destination
    hidden inside a third-party script. Such scripts remain responsible for their own routing.
    Only argv execution through the existing finite process boundary is supported.
    """
    from llmcall import process
    if not isinstance(command, dict) or set(command) - {'argv', 'payload', 'timeout', 'cwd', 'message_file'}:
        raise PreparationError('invalid_command_policy')
    argv = command.get('argv')
    if not isinstance(argv, list) or not argv or any(not isinstance(a, str) or not a for a in argv):
        raise PreparationError('invalid_command_argv')
    if files:
        raise PreparationError('custom_notifier_attachments_unsupported')
    payload = command.get('payload', 'text')
    timeout = command.get('timeout', 30)
    if payload not in ('text', 'base64', 'at-file'):
        raise PreparationError('unsupported_command_payload')
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise PreparationError('invalid_command_timeout')
    context = process.resolve_context(cwd=command.get('cwd'))
    message_file = command.get('message_file')
    if payload == 'at-file':
        if not isinstance(message_file, str):
            raise PreparationError('custom_notifier_message_file_required')
        message_file = context.path(message_file)
        with open(message_file, encoding='utf-8-sig') as source:
            if source.read() != content:
                raise PreparationError('custom_notifier_message_file_changed')
    elif message_file is not None:
        raise PreparationError('unexpected_message_file')
    executable = process.find(argv[0], [], context=context)
    if not executable:
        raise PreparationError('custom_notifier_missing')
    argv = [executable, *argv[1:]]
    for arg in argv[1:]:
        if arg.lower().endswith('.py') and not os.path.isfile(context.path(arg)):
            raise PreparationError('custom_notifier_missing')
    policy = {'argv': argv, 'payload': payload, 'cwd': context.cwd, 'message_file': message_file}
    fingerprint = hashlib.sha256(json.dumps(policy, sort_keys=True).encode('utf-8')).hexdigest()
    snapshot_file = None
    if payload == 'at-file':
        if snapshot_dir is None:
            raise PreparationError('custom_notifier_snapshot_directory_required')
        snapshot_file = Path(snapshot_dir) / (uuid.uuid4().hex + '.txt')
        created = False
        try:
            with private_data.open_for_write(snapshot_file, 'xb') as output:
                created = True
                output.write(content.encode('utf-8'))
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            if created:
                release_prepared({'snapshot_file': snapshot_file})
            raise
    return {'target': {'kind': 'command', 'stream': stream, 'fallback': False,
                       'command_sha256': fingerprint},
            'argv': argv, 'payload': payload, 'timeout': timeout, 'context': context,
            'message_file': message_file, 'snapshot_file': snapshot_file}


def release_prepared(prepared):
    """Remove the private snapshot only before invocation or after confirmed process cleanup."""
    path = prepared.get('snapshot_file')
    if path is not None and prepared.get('snapshot_cleanup_safe', True):
        try:
            private_data.assert_writable_path(path)
            private_data.check_destination(path)
            Path(path).unlink(missing_ok=True)
        except (OSError, ValueError):
            # A leftover private artifact does not change a confirmed delivery verdict.
            sys.stderr.write('relay: private notifier snapshot cleanup failed\n')


def _send_command(content, prepared):
    machine_role.refuse_on_secondary()  # the secondary machine never sends; see machine_role.py
    from llmcall import process
    # Legacy @file notifiers consume this attempt's immutable snapshot once. Text transports use
    # the same canonical chunker as stream delivery; no chunk can gain a second receipt.
    parts = [content] if prepared['payload'] == 'at-file' else split_for_discord(content)
    for part in parts:
        argv = list(prepared['argv'])
        if prepared['payload'] == 'text':
            argv.append(part)
        elif prepared['payload'] == 'base64':
            argv.append(base64.b64encode(part.encode('utf-8')).decode('ascii'))
        elif prepared['payload'] == 'at-file':
            argv.append('@' + str(prepared['snapshot_file']))
        # An exception or unconfirmed cleanup may leave a child reading its @file payload.
        # Retain the snapshot until an owner reconciles that job.
        prepared['snapshot_cleanup_safe'] = False
        result = process.run(argv, '', prepared['timeout'], context=prepared['context'])
        prepared['snapshot_cleanup_safe'] = getattr(result, 'cleanup_confirmed', None) is True
        if (result.outcome != 'success' or result.error is not None
                or not prepared['snapshot_cleanup_safe']):
            return False
    return True


def prepare_send(*, stream, channel_id=None, files=None, username=None, fallback="none"):
    """Resolve a receipt target without sending; never select an implicit default stream.

    The returned registry snapshot contains credentials and is transient. Only `target` may
    enter a receipt. Explicit channels/attachments retain the existing no-DM-fallback rule.
    """
    if not isinstance(stream, str) or not stream.strip():
        raise PreparationError("missing_stream")
    if fallback not in ("none", "big_brother"):
        raise PreparationError("invalid_fallback_policy")
    if username is not None and (not isinstance(username, str) or not username.strip()):
        raise PreparationError("invalid_username")
    reg = copy.deepcopy(load_registry())
    streams = reg.get("streams") or {}
    if not isinstance(streams, dict):
        raise PreparationError("malformed_registry")
    entry = streams.get(stream) or {}
    if not isinstance(entry, dict):
        raise PreparationError("malformed_stream")
    token = bot_token(reg)
    channel = channel_id or entry.get("channel_id")
    reason = None
    if files or channel_id:
        if not channel:
            raise PreparationError("missing_channel")
        if not token:
            raise PreparationError("missing_credentials")
        kind = "bot"
    elif entry.get("webhook"):
        if not str(entry["webhook"]).startswith("https://"):
            raise PreparationError("invalid_webhook")
        kind = "webhook"
    elif channel and token:
        kind = "bot"
    else:
        reason = "missing_stream" if not entry else "missing_credentials_or_target"
        if fallback != "big_brother":
            raise PreparationError(reason)
        if not token or not (reg.get("big_brother") or {}).get("user_id"):
            raise PreparationError("missing_fallback_credentials_or_target")
        kind = "big_brother"
    target = {"kind": kind, "stream": stream, "fallback": kind == "big_brother",
              "reason": reason}
    if kind == "webhook":
        resolved_username = username or entry.get("username") or stream
        if not isinstance(resolved_username, str) or not resolved_username.strip():
            raise PreparationError("invalid_username")
        target.update(webhook_sha256=hashlib.sha256(entry["webhook"].encode()).hexdigest(),
                      channel_id=str(channel) if channel else None,
                      username=resolved_username)
    elif kind == "bot":
        target["channel_id"] = str(channel)
    else:
        target["user_id"] = str(reg["big_brother"]["user_id"])
    attachments = []
    for path in files or ():
        with open(path, "rb") as source:
            attachments.append(_PreparedAttachment(os.fspath(path), source.read()))
    return {"target": target, "registry": reg, "stream": stream,
            "channel_id": channel_id, "username": username, "files": tuple(attachments)}


def _send_prepared(content, files, prepared):
    """Use exactly the recorded target, retaining the canonical transports and chunker."""
    target = prepared["target"]
    if target['kind'] == 'command':
        return _send_command(content, prepared)
    reg = prepared["registry"]
    snapshot = _REGISTRY_SNAPSHOT.set(reg)
    try:
        if target["kind"] == "big_brother":
            sys.stderr.write("relay: explicit Big Brother fallback; target changed\n")
            return _big_brother("[%s] %s" % (prepared["stream"], content))
        if target["kind"] == "bot":
            return _post_bot(target["channel_id"], content,
                             prepared["files"] or None, bot_token(reg))
        return relay(prepared["stream"], content, prepared["username"])
    finally:
        _REGISTRY_SNAPSHOT.reset(snapshot)



def split_for_discord(text: str, budget: int = _CHUNK_BUDGET) -> list[str]:
    """Split an over-long body into deliverable parts, each marked "(n/m)".

    Why this exists. The original design refused to truncate, on the correct grounds that a
    summary which quietly loses its tail reads as complete. But refusing to truncate was
    implemented as refusing to adapt at all, so an over-long body produced HTTP 400 and the
    caller lost the ENTIRE message rather than its tail. Losing everything is strictly worse
    than losing the end, and it fails in the worst possible direction: an alert body that
    enumerates failures grows with the number of failures, so the more there is to report,
    the less likely the report is to arrive. Measured on a monitoring digest: three
    consecutive HTTP 400s at 2066, 2072 and 2006 characters, all just over the wall.

    So: never silently drop anything, and never drop everything. Split on line boundaries
    where possible, hard-split only a single line that is itself over budget, and mark every
    part so a reader can see that more is coming. A caller that wants the old all-or-nothing
    behaviour can check len(text) itself before calling.
    """
    if len(text) <= _DISCORD_LIMIT:
        return [text]

    pieces: list[str] = []
    cur = None
    for line in text.split("\n"):
        while len(line) > budget:
            # A single line longer than the budget: emit what is left of the current piece,
            # then hard-split the line. Nothing is dropped, the break is just not on a newline.
            if cur is not None:
                pieces.append(cur)
                cur = None
            pieces.append(line[:budget])
            line = line[budget:]
        if cur is None:
            cur = line
        elif len(cur) + 1 + len(line) <= budget:
            cur += "\n" + line
        else:
            pieces.append(cur)
            cur = line
    if cur is not None:
        pieces.append(cur)

    total = len(pieces)
    out = ["(%d/%d)\n%s" % (i + 1, total, p) for i, p in enumerate(pieces)]
    # The marker must never be what pushes a part back over the wall.
    assert all(len(p) <= _DISCORD_LIMIT for p in out), "chunking produced an over-limit part"
    return out


def registry_path() -> str:
    return str(private_data.registry_path())


def load_registry() -> dict:
    """Return the registry dict, or {} if absent/unreadable (caller falls back to Big Brother)."""
    snapshot = _REGISTRY_SNAPSHOT.get()
    if snapshot is not None:
        return snapshot
    p = registry_path()
    try:
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:  # malformed registry must not crash a skill's alert path
        sys.stderr.write("relay: registry unreadable (%s)\n" % type(e).__name__)
        return {}


def _post_webhook(url: str, payload: dict) -> bool:
    """POST a webhook payload. Honors AGENT_CENTER_RELAY_DRYRUN (no network) for tests/CI.

    Suppress Discord's auto-generated link-preview embeds by default (flags=4 = SUPPRESS_EMBEDS):
    this relay is content-only by design, and the unsolicited link cards are pure noise. A caller
    that genuinely wants embeds can pass flags=0 in a --json payload to opt back in.
    """
    payload.setdefault("flags", 4)
    if os.environ.get("AGENT_CENTER_RELAY_DRYRUN"):
        sys.stdout.write("DRYRUN webhook <%s> %s\n" % (payload.get("username", "?"),
                                                       (payload.get("content", "") or "")[:80]))
        return True
    machine_role.refuse_on_secondary()  # the secondary machine never sends; see machine_role.py
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json", "User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status in (200, 204)
    except Exception as e:
        sys.stderr.write("relay: webhook POST failed (%s)\n" % type(e).__name__)
        return False


def bot_token(reg: dict) -> str | None:
    """The bot token, canonical source. Same resolver ingest.py reads, deliberately: one credential
    for the whole bus means one place to rotate it. Degrades to None (with one stderr line naming
    the cause) when reader.bot_token_ref cannot be resolved, so webhook delivery keeps working."""
    return bot_credential.resolve_or_none(reg, "relay")


def _bot_request(url, payload, files, token, method="POST"):
    """Build the shared JSON or multipart bot request for sends and edits."""
    headers = {"Authorization": "Bot %s" % token, "User-Agent": _UA}
    if not files:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), method=method,
            headers={**headers, "Content-Type": "application/json"})
    else:
        paths = [os.fspath(f) for f in files]
        boundary = "----agentcenter" + os.urandom(8).hex()
        payload = {**payload, "attachments": [
            {"id": i, "filename": os.path.basename(p)} for i, p in enumerate(paths)]}
        parts: list[bytes] = []

        def field(name, value, filename=None, ctype=None):
            head = '--%s\r\nContent-Disposition: form-data; name="%s"' % (boundary, name)
            if filename:
                head += '; filename="%s"' % filename
            head += "\r\n"
            if ctype:
                head += "Content-Type: %s\r\n" % ctype
            parts.append(head.encode("utf-8") + b"\r\n" + value + b"\r\n")

        field("payload_json", json.dumps(payload).encode("utf-8"), ctype="application/json")
        for i, (attachment, p) in enumerate(zip(files, paths)):
            if isinstance(attachment, _PreparedAttachment):
                data = attachment.data
            else:
                with open(p, "rb") as fh:
                    data = fh.read()
            field("files[%d]" % i, data, os.path.basename(p), "application/octet-stream")
        parts.append(("--%s--\r\n" % boundary).encode())
        req = urllib.request.Request(
            url, data=b"".join(parts), method=method,
            headers={**headers, "Content-Type": "multipart/form-data; boundary=%s" % boundary})
    return req


def _post_bot(channel_id: str, content: str, files: list | None, token: str) -> bool:
    """POST as the bot to one channel, with optional file attachments.

    Multipart is assembled by hand rather than pulled from a library because this runs from
    scheduled tasks under whatever python is on PATH, and the whole Agent Center is stdlib only for
    that reason. Content is never silently truncated: an over-long body is split by
    split_for_discord into marked parts so that nothing is lost and the split is visible. It used
    to be neither truncated nor split, which meant an over-long body was lost in full; see that
    function for why that is the worse of the two failures.
    """
    if not files and (not isinstance(content, str) or not content.strip()):
        sys.stderr.write("relay: blank content cannot be delivered without attachments\n")
        return False
    parts = split_for_discord(content or "")
    if len(parts) > 1:
        # Attachments ride the first part; the rest carry text only. Sending the files with
        # every part would upload them N times.
        ok = _post_bot(channel_id, parts[0], files, token)
        for part in parts[1:]:
            if not _post_bot(channel_id, part, None, token):
                ok = False
        sys.stderr.write("relay: body was %d chars, delivered as %d parts\n"
                         % (len(content or ""), len(parts)))
        return ok
    content = parts[0]
    if os.environ.get("AGENT_CENTER_RELAY_DRYRUN"):
        sys.stdout.write("DRYRUN bot <%s> %s%s\n" % (
            channel_id, (content or "")[:80],
            (" +%d file(s)" % len(files)) if files else ""))
        return True
    machine_role.refuse_on_secondary()  # the secondary machine never sends; see machine_role.py
    req = _bot_request("%s/channels/%s/messages" % (_API, channel_id),
                       {"content": content or ""}, files, token)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status in (200, 204)
    except Exception as e:
        sys.stderr.write("relay: bot POST failed (%s)\n" % type(e).__name__)
        return False


def _open_dm(user_id: str, token: str) -> str | None:
    """The bot's DM channel with one user (Discord returns the existing one when it exists)."""
    if os.environ.get("AGENT_CENTER_RELAY_DRYRUN"):
        return "dryrun-dm"
    machine_role.refuse_on_secondary()  # the secondary machine never sends; see machine_role.py
    req = urllib.request.Request(
        "%s/users/@me/channels" % _API, method="POST",
        data=json.dumps({"recipient_id": str(user_id)}).encode("utf-8"),
        headers={"Authorization": "Bot %s" % token, "Content-Type": "application/json", "User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.loads(r.read().decode("utf-8") or "{}")
    except Exception as e:
        sys.stderr.write("relay: open DM failed (%s)\n" % type(e).__name__)
        return None
    chan = body.get("id") if isinstance(body, dict) else None
    return str(chan) if chan else None


def _big_brother(text: str, files: list | None = None) -> bool:
    """The operator's Big Brother DM (registry.big_brother.user_id), sent as the canonical bot.
    This is the phone-reaching channel: `send --dm` targets it, and an unknown-stream fallback
    lands here. Over-long bodies are split by _post_bot, never truncated."""
    if not files and (not isinstance(text, str) or not text.strip()):
        sys.stderr.write("relay: blank content cannot be delivered\n")
        return False
    reg = load_registry()
    user_id = (reg.get("big_brother") or {}).get("user_id")
    token = bot_token(reg)
    if not user_id or not token:
        sys.stderr.write("relay: no big_brother.user_id or bot token in the registry; DM not sent\n")
        return False
    chan = _open_dm(str(user_id), token)
    if not chan:
        return False
    return _post_bot(chan, text, files, token)


def deliver(stream, content, *, channel_id=None, files=None, idempotency_key=None,
            mention_user=None):
    """Deliver through the configured stream and retain confirmed Discord message IDs."""
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
    import uuid
    if not isinstance(content, str) or not content.strip():
        raise ValueError('blank content cannot produce a delivery receipt')
    reg = load_registry()
    target = (reg.get('streams') or {}).get(stream) or {}
    webhook = None if channel_id or files or mention_user else target.get('webhook')
    token = bot_token(reg)
    channel = channel_id or target.get('channel_id')
    if not webhook and not (channel and token):
        if channel_id or files or mention_user or idempotency_key is not None:
            raise ValueError('missing bot channel or credentials')
        return relay(stream, content)
    if os.environ.get('AGENT_CENTER_RELAY_DRYRUN'):
        return {'kind': 'synthetic-local', 'receipt_id': str(uuid.uuid4()),
                'delivered': True, 'exit_code': 0}
    machine_role.refuse_on_secondary()  # the secondary machine never sends; see machine_role.py
    receipts = []
    for index, part in enumerate(split_for_discord(content)):
        payload = {'content': part, 'flags': 4}
        headers = {'Content-Type': 'application/json', 'User-Agent': _UA}
        if webhook:
            parsed = urlsplit(webhook)
            query = dict(parse_qsl(parsed.query))
            query['wait'] = 'true'
            url = urlunsplit(parsed._replace(query=urlencode(query)))
            if target.get('username'):
                payload['username'] = target['username']
            request = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'),
                                             headers=headers, method='POST')
        else:
            if mention_user is not None:
                payload['allowed_mentions'] = {'parse': [], 'users': [mention_user]}
            if idempotency_key is not None:
                nonce_key = json.dumps([idempotency_key, index], ensure_ascii=False)
                payload.update(nonce=hashlib.sha256(nonce_key.encode('utf-8')).hexdigest()[:25],
                               enforce_nonce=True)
            request = _bot_request('%s/channels/%s/messages' % (_API, channel), payload,
                                   files if index == 0 else None, token)
        with urllib.request.urlopen(request, timeout=180 if files else 30) as response:
            if response.status != 200:
                raise ValueError('delivery response did not confirm a message')
            message = json.loads(response.read().decode('utf-8'))
        receipt = message.get('id') if isinstance(message, dict) else None
        if not isinstance(receipt, str) or not receipt.strip():
            raise ValueError('delivery response lacked a message ID')
        receipts.append(receipt)
    if not receipts:
        raise ValueError('delivery produced no confirmed receipt')
    return {'kind': 'discord-message', 'receipt_id': ','.join(receipts),
            'delivered': True, 'exit_code': 0}


def relay(stream: str, content: str, username: str | None = None) -> bool:
    """Deliver `content` to the named Agent Center stream. Returns True on success.

    Resolution: registry.streams[stream].webhook (per-stream identity via `username`), then a
    configured `channel_id` through the canonical Agent Center bot. The second form is useful for
    low-volume notification streams: it avoids creating another long-lived webhook secret while
    preserving the same stable `relay.py send --stream ...` contract.
    Fallback: if the stream is unknown or no registry exists, deliver to Big Brother DM so the
    message is never lost (prefixed with the stream name for context).
    """
    if not isinstance(content, str) or not content.strip():
        sys.stderr.write("relay: blank content cannot be delivered\n")
        return False
    reg = load_registry()
    s = (reg.get("streams") or {}).get(stream)
    if not s:
        sys.stderr.write("relay: stream %r not configured; using Big Brother fallback\n" % stream)
        return _big_brother("[%s] %s" % (stream, content))
    if not s.get("webhook"):
        chan = s.get("channel_id")
        token = bot_token(reg)
        if chan and token:
            return _post_bot(str(chan), content, None, token)
        sys.stderr.write("relay: stream %r has no usable webhook or bot channel; "
                         "using Big Brother fallback\n" % stream)
        return _big_brother("[%s] %s" % (stream, content))
    name = username or s.get("username") or stream
    parts = split_for_discord(content or "")
    ok = bool(parts)
    for part in parts:
        # Every part must land. Returning True after a partial delivery would report a
        # message as sent while its tail is missing, which is the failure this whole
        # function exists to avoid.
        if not _post_webhook(s["webhook"], {"content": part, "username": name}):
            ok = False
    if len(parts) > 1:
        sys.stderr.write("relay: body was %d chars, delivered as %d parts\n"
                         % (len(content or ""), len(parts)))
    return ok


def send(content: str, stream: str | None = None, channel_id: str | None = None,
         files: list | None = None, username: str | None = None, *, prepared=None) -> bool:
    """Deliver to a stream, to an explicit channel, or both, choosing the transport (see module doc).

    `stream` alone behaves exactly like relay(): it keeps the per-stream webhook identity when a
    webhook exists, otherwise a bot-backed notification stream uses its configured channel.
    `channel_id` (or any `files`) switches to the bot, because a webhook can do neither.
    Given both, `channel_id` wins for routing and `stream` is used only to resolve a channel when
    the caller passed a name instead of an id.
    """
    if prepared is not None:
        return _send_prepared(content, files, prepared)
    reg = load_registry()
    s = (reg.get("streams") or {}).get(stream) if stream else None
    chan = channel_id or (s or {}).get("channel_id")
    if not files and not channel_id:
        return relay(stream, content, username)          # the frozen caller contract
    if not chan:
        sys.stderr.write("relay: no channel for stream %r; cannot use the bot transport\n" % stream)
        return False
    token = bot_token(reg)
    if not token:
        sys.stderr.write("relay: no usable bot token (reader.bot_token_ref / reader.bot_token); "
                         "cannot use the bot transport\n")
        return False
    return _post_bot(str(chan), content, files, token)


def safe_stream_entry(entry: dict) -> dict:
    """Preserve ordinary metadata while removing explicit credential-bearing fields."""
    def scrub(value):
        if isinstance(value, dict):
            result = {}
            for key, child in value.items():
                name = str(key).casefold().replace("-", "_")
                if (name in {"webhook", "token", "password", "secret", "credentials", "authorization", "api_key"}
                        or name.endswith(("_token", "_secret", "_password", "_webhook"))):
                    continue
                result[key] = scrub(child)
            return result
        if isinstance(value, list):
            return [scrub(child) for child in value]
        return value
    return scrub(entry)


def _cmd_list() -> int:
    reg = load_registry()
    streams = reg.get("streams") or {}
    if not streams:
        print(json.dumps({"ok": False, "registry": registry_path(), "streams": []}))
        return 1
    # NEVER print webhook URLs, only safe metadata.
    out = {name: safe_stream_entry(entry) for name, entry in streams.items()}
    print(json.dumps({"ok": True, "registry": registry_path(),
                      "guild_id": reg.get("guild_id"), "streams": out}, ensure_ascii=False, indent=2))
    return 0


def _cmd_health() -> int:
    reg = load_registry()
    streams = reg.get("streams") or {}
    problems = []
    if not reg:
        problems.append("registry missing at %s" % registry_path())
    token = bot_token(reg)
    for name, s in streams.items():
        webhook_ok = s.get("webhook", "").startswith("https://")
        bot_ok = bool(s.get("channel_id") and token)
        if not webhook_ok and not bot_ok:
            problems.append("stream %s: no usable webhook or bot channel" % name)
    ok = not problems
    print(json.dumps({"ok": ok, "registry": registry_path(),
                      "stream_count": len(streams), "problems": problems}, ensure_ascii=False))
    return 0 if ok else 1


def _send_with_receipt(args, content) -> int:
    """`send --idempotency-key`: deliver through deliver() and print exactly one receipt line.

    not_applied is printed only where nothing can have been sent (checked before any request);
    a failure after the first request may have delivered part of the message, so it is uncertain.
    No fallback: a stream without a receipt-capable transport is refused instead of being sent to
    the Big Brother DM, where delivery could not be proven.
    """
    key = args.idempotency_key
    base = {"idempotency_key": key, "adapter": args.receipt_adapter}

    def emit(receipt, rc):
        print(json.dumps({**receipt, **base}, ensure_ascii=False))
        return rc

    if not isinstance(key, str) or not key.strip():
        sys.stderr.write("relay: --idempotency-key must be nonempty\n")
        return 2
    if args.dm or not (args.channel_id or args.stream):
        return emit({"status": "not_applied",
                     "evidence": "receipt mode needs --stream or --channel-id; nothing was sent"}, 2)
    if not isinstance(content, str) or not content.strip():
        return emit({"status": "not_applied", "evidence": "blank content; nothing was sent"}, 1)
    try:
        reg = load_registry()
        target = (reg.get("streams") or {}).get(args.stream) or {}
        channel = args.channel_id or target.get("channel_id")
        bot_required = args.channel_id or args.files or args.mention_user
        capable = bool((not bot_required and target.get("webhook")) or (channel and bot_token(reg)))
    except Exception as exc:
        return emit({"status": "not_applied",
                     "evidence": "registry unreadable (%s); nothing was sent" % type(exc).__name__}, 1)
    if not capable:
        return emit({"status": "not_applied",
                     "evidence": "stream %r has no webhook or bot channel; nothing was sent" % args.stream}, 1)
    try:
        delivered = deliver(args.stream, content, channel_id=args.channel_id, files=args.files,
                            idempotency_key=key, mention_user=args.mention_user)
    except Exception as exc:
        return emit({"status": "uncertain", "error": type(exc).__name__}, 1)
    receipt_id = delivered.get("receipt_id") if isinstance(delivered, dict) else None
    if not isinstance(receipt_id, str) or not receipt_id.strip():
        return emit({"status": "uncertain"}, 1)
    return emit({"status": "confirmed", "receipt_id": receipt_id, "kind": delivered.get("kind")}, 0)


def _edit(args) -> int:
    """Replace the content and attachments of one existing bot message."""
    try:
        raw = (sys.stdin.buffer.read() if args.text_file == "-"
               else Path(args.text_file).read_bytes())
        content = raw.decode("utf-8-sig")
        token = bot_token(load_registry())
        if not token:
            raise ValueError("missing bot credentials")
        request = _bot_request(
            "%s/channels/%s/messages/%s" % (_API, args.channel_id, args.message_id),
            {"content": content, "attachments": [], "allowed_mentions": {"parse": []}},
            args.files, token, method="PATCH")
        if os.environ.get("AGENT_CENTER_RELAY_DRYRUN"):
            print(json.dumps({"status": "not_applied", "evidence": "dry run; message not edited"}))
            return 1
        machine_role.refuse_on_secondary()  # the secondary machine never sends; see machine_role.py
        with urllib.request.urlopen(request, timeout=180) as response:
            if response.status != 200:
                raise ValueError("edit response did not confirm a message")
            message = json.loads(response.read().decode("utf-8"))
        if not isinstance(message, dict) or message.get("id") != args.message_id:
            raise ValueError("edit response did not confirm the requested message")
    except Exception as exc:
        print(json.dumps({"status": "uncertain", "error": type(exc).__name__}))
        return 1
    print(json.dumps({"status": "confirmed", "receipt_id": message["id"],
                      "kind": "discord-message"}))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="relay.py", description="Agent Center multi-stream Discord relay")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_send = sub.add_parser("send", help="send to a stream or to one channel")
    p_send.add_argument("--stream", default=None)
    p_send.add_argument("--channel-id", dest="channel_id", default=None,
                        help="post as the bot to this channel (answers where the user is looking)")
    p_send.add_argument("--dm", action="store_true",
                        help="send to the operator's Big Brother DM as the bot")
    p_send.add_argument("--file", dest="files", action="append", default=None,
                        help="attach a file (repeatable); forces the bot transport")
    g = p_send.add_mutually_exclusive_group(required=True)
    g.add_argument("--text")
    # Windows PowerShell 5.1 mangles a non-ASCII argv (em/CJK -> mojibake) when it invokes python.exe,
    # so a PS caller must base64-encode the UTF-8 bytes and pass them here instead of --text/--json.
    g.add_argument("--text-b64", dest="text_b64", help="base64 of UTF-8 text (PowerShell-safe)")
    g.add_argument("--text-file", dest="text_file",
                   help="read the UTF-8 text from this file ('-' = stdin)")
    g.add_argument("--json", dest="json_payload", help='{"content":..,"username":..}')
    g.add_argument("--json-b64", dest="json_b64", help="base64 of UTF-8 JSON (PowerShell-safe)")
    p_send.add_argument("--username", default=None)
    p_send.add_argument("--mention-user", default=None,
                        help="allow only this user mention; requires --idempotency-key")
    # A caller that must prove delivery (a durable action ledger) passes a key and gets one
    # JSON receipt line on stdout: confirmed with the Discord message IDs, not_applied with evidence
    # when nothing can have been sent, otherwise uncertain. Attachments and explicit channels use the bot.
    p_send.add_argument("--idempotency-key", dest="idempotency_key", default=None)
    p_send.add_argument("--receipt-adapter", dest="receipt_adapter", default="relay",
                        help="adapter name echoed in the receipt")
    p_edit = sub.add_parser("edit", help="replace one bot message and its attachments")
    p_edit.add_argument("--channel-id", required=True)
    p_edit.add_argument("--message-id", required=True)
    p_edit.add_argument("--text-file", required=True, help="UTF-8 file ('-' = stdin)")
    p_edit.add_argument("--file", dest="files", action="append", default=None)
    sub.add_parser("list", help="list configured streams (no secrets)")
    sub.add_parser("health", help="check registry health (no network, no secrets)")
    args = ap.parse_args(argv)

    def _b64(s):
        import base64
        return base64.b64decode(s).decode("utf-8")

    if args.cmd == "list":
        return _cmd_list()
    if args.cmd == "health":
        return _cmd_health()
    if args.cmd == "edit":
        return _edit(args)
    if args.cmd == "send":
        if args.mention_user and args.idempotency_key is None:
            ap.error("--mention-user requires --idempotency-key")
        if sum(bool(x) for x in (args.stream, args.channel_id, args.dm)) != 1:
            sys.stderr.write("relay: send needs exactly one of --stream, --channel-id, --dm\n")
            return 2
        payload = None
        if args.json_b64:
            payload = _b64(args.json_b64)
        elif args.json_payload:
            payload = args.json_payload
        if payload is not None:
            try:
                obj = json.loads(payload)
            except Exception as e:
                sys.stderr.write("relay: bad --json (%s)\n" % e)
                return 2
            content = obj.get("content", "")
            username = obj.get("username") or args.username
        elif args.text_b64:
            content, username = _b64(args.text_b64), args.username
        elif args.text_file:
            # Bytes, decoded here: a detached Windows child's stdin wrapper uses the ANSI code
            # page. utf-8-sig drops the BOM PowerShell 5.1 writes with -Encoding UTF8.
            raw = (sys.stdin.buffer.read() if args.text_file == "-"
                   else Path(args.text_file).read_bytes())
            content, username = raw.decode("utf-8-sig", errors="replace"), args.username
        else:
            content, username = args.text, args.username
        if args.idempotency_key is not None:
            return _send_with_receipt(args, content)
        if args.dm:
            return 0 if _big_brother(content, args.files) else 1
        return 0 if send(content, stream=args.stream, channel_id=args.channel_id,
                         files=args.files, username=username) else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
