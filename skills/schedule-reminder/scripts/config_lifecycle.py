"""Registry initialization and local configuration checks, without runtime side effects."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from urllib.parse import urlparse

import private_data


def authorize(path):
    source = private_data.SOURCE / "guards/tools/storage_contract.py"
    if not source.is_file():
        raise ValueError("Initialize the pinned Guards submodule before configuring storage")
    spec = importlib.util.spec_from_file_location("schedule_config_storage", source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    path = private_data.assert_writable_path(path)
    return module.authorize_artifact_write(private_data.SOURCE, path.parent, path.name,
                                           artifact_id="registry")


def template():
    """Blank setup values cannot qualify as configured delivery."""
    return {"schema_version": 1, "guild_id": "", "reader": {"bot_token": ""}, "streams": {}}


def validate(registry, capabilities="store,remind"):
    selected = {item.strip() for item in capabilities.split(",") if item.strip()}
    errors = []
    if selected - {"store", "remind", "ingest", "work"}:
        errors.append("unsupported capability selection")
    if not isinstance(registry, dict):
        return ["registry must be an object"]
    if type(registry.get("schema_version", 1)) is not int or registry.get("schema_version", 1) != 1:
        errors.append("schema_version must be 1 when present")
    reader = registry.get("reader", {})
    if not isinstance(reader, dict):
        errors.append("reader must be an object")
        reader = {}
    token = reader.get("bot_token")
    token_ready = isinstance(token, str) and bool(token.strip())
    streams = registry.get("streams")
    if not isinstance(streams, dict) or not streams:
        errors.append("streams must contain at least one configured destination")
        streams = {}
    for name, entry in streams.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(entry, dict):
            errors.append("each stream needs a nonempty name and object value")
            continue
        webhook = entry.get("webhook", "")
        parsed = urlparse(webhook) if isinstance(webhook, str) else None
        webhook_ready = bool(parsed and parsed.scheme == "https" and parsed.netloc and parsed.path)
        channel = entry.get("channel_id")
        channel_ready = isinstance(channel, str) and channel.isdecimal()
        if not webhook_ready and not (channel_ready and token_ready):
            errors.append("stream destination requires an HTTPS webhook or channel_id and reader.bot_token")
        for flag in ("listen", "inbound"):
            if flag in entry and type(entry[flag]) is not bool:
                errors.append("stream " + flag + " must be boolean")
    if selected & {"ingest", "work"}:
        guild = registry.get("guild_id")
        if not isinstance(guild, str) or not guild.isdecimal():
            errors.append("guild_id is required for ingest/work")
        if not token_ready:
            errors.append("reader.bot_token is required for ingest/work")
        owner = registry.get("big_brother", {})
        owner_id = owner.get("user_id") if isinstance(owner, dict) else None
        if not isinstance(owner_id, str) or not owner_id.isdecimal():
            errors.append("big_brother.user_id is required for ingest/work")
    return errors


def initialize(root):
    root = private_data.assert_writable_path(root)
    path = root / "registry.json"
    authorize(path)
    if path.exists():
        # Existing configuration, credentials and unknown fields remain untouched.
        return path, False
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(template(), stream, indent=2)
        stream.write("\n")
    return path, True


def doctor(path=None, capabilities="store,remind"):
    path = Path(path) if path else private_data.registry_path()
    errors = []
    try:
        authorize(path)
    except (OSError, ValueError, RuntimeError) as error:
        errors.append("storage: " + str(error))
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
        errors.extend(validate(registry, capabilities))
    except (OSError, ValueError):
        errors.append("registry.json is missing or invalid JSON")
    return {"ready": not errors, "resolved_root": str(path.absolute().parent),
            "registry": str(path.absolute()), "scope": "configuration-only",
            "runtime_ready": None, "external_delivery_verified": False,
            "problems": errors}
