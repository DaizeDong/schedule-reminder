#!/usr/bin/env python3
"""The bot token resolver (bot_credential.py): reference via a credential vault, or inline.

A fake ``credvault`` module stands in for the real library, so these tests never touch a vault, a
key or the network. Every value here is synthetic.
"""
import os
import sys
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

import bot_credential  # noqa: E402
import bigbrother  # noqa: E402
import config_lifecycle  # noqa: E402
import ingest  # noqa: E402
import relay  # noqa: E402

REF = "cv://synthetic-record/bot_token"
VAULT_VALUE = "SYNTHETIC_VAULT_TOKEN_0123456789"
INLINE_VALUE = "SYNTHETIC_INLINE_TOKEN"


class _Secret:
    def __init__(self, value):
        self._value = value

    def reveal(self):
        return self._value


def _fake_vault(monkeypatch, values=None, error=None):
    calls = []

    def get(ref):
        calls.append(ref)
        if error is not None:
            raise error
        return _Secret((values or {REF: VAULT_VALUE})[ref])

    monkeypatch.setitem(sys.modules, "credvault", types.SimpleNamespace(get=get))
    return calls


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.delenv(bot_credential.ENV_VAR, raising=False)
    bot_credential.clear_cache()
    yield
    bot_credential.clear_cache()


def test_reference_resolves_through_the_vault(monkeypatch):
    calls = _fake_vault(monkeypatch)
    reg = {"reader": {"bot_token_ref": REF}}
    assert bot_credential.resolve(reg) == VAULT_VALUE
    assert ingest.bot_token(reg) == VAULT_VALUE
    assert relay.bot_token(reg) == VAULT_VALUE
    assert calls == [REF]  # cached per process: one decryption for the many readers of a tick


def test_inline_token_still_works_without_a_vault(monkeypatch):
    monkeypatch.setitem(sys.modules, "credvault", None)  # import would fail; must not be needed
    reg = {"reader": {"bot_token": INLINE_VALUE}}
    assert bot_credential.resolve(reg) == INLINE_VALUE
    assert relay.bot_token(reg) == INLINE_VALUE


def test_nothing_configured_is_none():
    assert bot_credential.resolve({}) is None
    assert bot_credential.resolve({"reader": {}}) is None
    assert bot_credential.configured({"reader": {"bot_token": ""}}) is False


def test_missing_package_fails_with_install_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "credvault", None)
    with pytest.raises(bot_credential.BotTokenUnavailable) as caught:
        bot_credential.resolve({"reader": {"bot_token_ref": REF}})
    assert "not importable" in str(caught.value) and sys.executable in str(caught.value)


def test_vault_failure_never_falls_back_and_never_leaks(monkeypatch):
    class KeyMissing(Exception):
        pass

    _fake_vault(monkeypatch, error=KeyMissing("no key file"))
    reg = {"reader": {"bot_token_ref": REF}}
    with pytest.raises(bot_credential.BotTokenUnavailable) as caught:
        ingest.bot_token(reg)
    assert "KeyMissing" in str(caught.value)
    assert VAULT_VALUE not in str(caught.value)


def test_both_fields_is_a_configuration_error(monkeypatch):
    calls = _fake_vault(monkeypatch)
    reg = {"reader": {"bot_token_ref": REF, "bot_token": INLINE_VALUE}}
    with pytest.raises(bot_credential.BotTokenUnavailable):
        bot_credential.resolve(reg)
    assert calls == []  # the stale inline copy is neither used nor silently preferred
    assert any("inline" in e for e in config_lifecycle.validate(
        {**reg, "streams": {"a": {"webhook": "https://example.com/hook"}}}))


def test_malformed_reference_is_rejected(monkeypatch):
    _fake_vault(monkeypatch)
    with pytest.raises(bot_credential.BotTokenUnavailable):
        bot_credential.resolve({"reader": {"bot_token_ref": "synthetic-record/bot_token"}})


def test_relay_degrades_with_a_clear_line(monkeypatch, capsys):
    _fake_vault(monkeypatch, error=RuntimeError("vault offline"))
    assert relay.bot_token({"reader": {"bot_token_ref": REF}}) is None
    err = capsys.readouterr().err
    assert "relay: bot token unavailable" in err and "vault offline" in err


def test_bigbrother_degrades_to_false(monkeypatch):
    monkeypatch.delenv("AGENT_CENTER_RELAY_DRYRUN", raising=False)
    _fake_vault(monkeypatch, error=RuntimeError("vault offline"))
    monkeypatch.setattr(bigbrother, "_registry",
                        lambda: {"reader": {"bot_token_ref": REF}, "big_brother": {"user_id": "1"}})
    assert bigbrother.send_dm("hi") is False


def test_lifecycle_accepts_a_reference_for_ingest():
    reg = {"guild_id": "1", "reader": {"bot_token_ref": REF},
           "big_brother": {"user_id": "2"}, "streams": {"a": {"channel_id": "3"}}}
    assert config_lifecycle.validate(reg, "store,remind,ingest,work") == []


def test_explicit_environment_injection_wins_without_touching_the_vault(monkeypatch):
    calls = _fake_vault(monkeypatch)
    monkeypatch.setenv(bot_credential.ENV_VAR, "SYNTHETIC_INJECTED_TOKEN")
    assert bot_credential.resolve({"reader": {"bot_token_ref": REF}}) == "SYNTHETIC_INJECTED_TOKEN"
    assert calls == []
    assert bot_credential.configured({}) is True
