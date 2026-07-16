#!/usr/bin/env python3
"""Native Big Brother DM sender (bigbrother.py) — unit tests, no network.

This replaced the retired shell-out to the legacy DM notifier script. It reads its token/user_id
from the SAME registry every other Agent Center path uses (reader.bot_token / big_brother.user_id)
and posts via stdlib urllib. These tests cover the dryrun seam, the misconfig guards, and chunking.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

import bigbrother as bb  # noqa: E402


def test_dryrun_returns_true_without_network(monkeypatch):
    monkeypatch.setenv("AGENT_CENTER_RELAY_DRYRUN", "1")
    assert bb.send_dm("hello phone") is True


def test_missing_token_returns_false(monkeypatch):
    monkeypatch.delenv("AGENT_CENTER_RELAY_DRYRUN", raising=False)
    monkeypatch.setattr(bb, "_registry", lambda: {"big_brother": {"user_id": "123"}})  # no token
    assert bb.send_dm("hi") is False


def test_missing_user_id_returns_false(monkeypatch):
    monkeypatch.delenv("AGENT_CENTER_RELAY_DRYRUN", raising=False)
    monkeypatch.setattr(bb, "_registry", lambda: {"reader": {"bot_token": "x"}})  # no user_id
    assert bb.send_dm("hi") is False


def test_never_raises_on_registry_error(monkeypatch):
    monkeypatch.delenv("AGENT_CENTER_RELAY_DRYRUN", raising=False)

    def boom():
        raise RuntimeError("registry blew up")

    monkeypatch.setattr(bb, "_registry", boom)
    assert bb.send_dm("hi") is False  # returns False, does not propagate


def test_chunk_hard_split_preserves_content():
    text = "x" * 5000
    chunks = bb._chunk(text, limit=2000)
    assert all(len(c) <= 2000 for c in chunks)
    assert "".join(chunks) == text  # no newline to split on -> exact reassembly


def test_chunk_prefers_newline_boundary():
    text = ("a" * 1000) + "\n" + ("b" * 1000) + "\n" + ("c" * 1000)
    chunks = bb._chunk(text, limit=1990)
    assert all(len(c) <= 1990 for c in chunks)
    assert len(chunks) >= 2
    # every letter survives (only the split newlines are consumed)
    joined = "".join(chunks)
    assert joined.count("a") == 1000 and joined.count("b") == 1000 and joined.count("c") == 1000


def test_chunk_empty_is_one_empty_piece():
    assert bb._chunk("") == [""]
