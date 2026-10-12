"""The Agent Center bot token: one resolver for every reader of the registry.

An explicit injection wins: AGENT_CENTER_BOT_TOKEN in the process environment (what a vault's
``exec`` or a materialized dotenv provides on a machine that has no vault key, such as a container).
Otherwise the registry names the token in exactly one of two ways:

  reader.bot_token_ref   a credential reference (``cv://<record>/<credential>``) resolved through the
                         credvault Python library at the moment of use; the value never sits in the
                         registry file.
  reader.bot_token       the token inline (an install without a credential vault).

Setting both is a configuration error, not a preference order: a reference that fails must never be
papered over by an inline copy that may be stale, so there is no fallback between the two.

``resolve(reg)`` returns the token, None when neither field is set, and raises BotTokenUnavailable
when a field is set but cannot produce a value (package not importable, vault or key missing,
unknown reference). ``resolve_or_none(reg, who)`` is the degrading form for paths that can still do
useful work without the bot (webhook delivery, local checks): it writes one clear stderr line and
returns None. Neither form ever prints, logs or returns the value in an error message.
"""
import os
import sys

ENV_VAR = "AGENT_CENTER_BOT_TOKEN"
REF_FIELD = "bot_token_ref"
INLINE_FIELD = "bot_token"
REF_SCHEME = "cv://"

_CACHE = {}


class BotTokenUnavailable(RuntimeError):
    """A bot token source is configured but did not produce a value."""


def _reader(reg):
    reader = (reg or {}).get("reader") if isinstance(reg, dict) else None
    return reader if isinstance(reader, dict) else {}


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def configured(reg):
    """True when a token source is named (environment or registry; no resolution, no vault access)."""
    reader = _reader(reg)
    return bool(_text(os.environ.get(ENV_VAR)) or _text(reader.get(REF_FIELD)) or _text(reader.get(INLINE_FIELD)))


def config_errors(reg):
    """Shape problems with the reader token fields; never touches the vault."""
    reader = _reader(reg)
    ref, inline = reader.get(REF_FIELD), reader.get(INLINE_FIELD)
    errors = []
    if ref is not None and not (_text(ref) and _text(ref).startswith(REF_SCHEME)):
        errors.append("reader.bot_token_ref must be a %s<record>/<credential> reference" % REF_SCHEME)
    if _text(ref) and _text(inline):
        errors.append("reader has both bot_token_ref and an inline bot_token; remove the inline copy")
    return errors


def _import_credvault():
    try:
        import credvault  # noqa: PLC0415  optional dependency, only needed when a reference is set
    except ImportError as error:
        raise BotTokenUnavailable(
            "reader.bot_token_ref is set but the credvault package is not importable by %s (%s). "
            "Install it for this interpreter (python -m pip install --user -e <credvault>/python) "
            "or put <credvault>/python on PYTHONPATH." % (sys.executable, type(error).__name__)) from None
    return credvault


def _resolve_ref(ref):
    if ref in _CACHE:
        return _CACHE[ref]
    credvault = _import_credvault()
    try:
        value = credvault.get(ref).reveal()
    except Exception as error:  # every vault failure becomes one clear, value-free message
        raise BotTokenUnavailable(
            "reader.bot_token_ref could not be resolved from the credential vault (%s: %s)"
            % (type(error).__name__, error)) from None
    if not isinstance(value, str) or not value.strip():
        raise BotTokenUnavailable("reader.bot_token_ref resolved to an empty credential")
    _CACHE[ref] = value.strip()
    return _CACHE[ref]


def resolve(reg):
    """The bot token, or None when no source is named. Raises BotTokenUnavailable."""
    injected = _text(os.environ.get(ENV_VAR))
    if injected:
        return injected
    problems = config_errors(reg)
    if problems:
        raise BotTokenUnavailable("; ".join(problems))
    reader = _reader(reg)
    ref = _text(reader.get(REF_FIELD))
    if ref:
        return _resolve_ref(ref)
    return _text(reader.get(INLINE_FIELD))


def resolve_or_none(reg, who):
    """Degrading form: on BotTokenUnavailable write one stderr line naming the cause, return None."""
    try:
        return resolve(reg)
    except BotTokenUnavailable as error:
        sys.stderr.write("%s: bot token unavailable: %s\n" % (who, error))
        return None


def clear_cache():
    _CACHE.clear()
