# -*- coding: utf-8 -*-
"""Command handlers: the deterministic half of the inbound bus.

THE INVARIANT UNDER TEST, stated once: every message the bus reads either gets claimed by a handler
or flows on to the judgment chain. Never neither. The bug that motivated this layer was a reader
that recognised only its own command prefix, skipped everything else, and advanced its cursor
anyway, so an instruction typed in that channel was consumed and seen by nobody. Several tests here
exist purely to make that outcome impossible to reintroduce.

Handlers are executed for real (a temp python script), not stubbed, so the stdin contract, the exit
code contract and the encoding of non-ASCII text are all actually exercised.
"""
import json
import os
import sys

import pytest

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import commands  # noqa: E402
import ingest    # noqa: E402


def _handler(tmp_path, name="h.py", body=None, rc=0):
    """A real handler script. Default: record what arrived on stdin, exit `rc`."""
    p = tmp_path / name
    p.write_text(body or (
        "import json,sys\n"
        "payload = json.load(sys.stdin)\n"
        "with open(r'%s', 'w', encoding='utf-8') as f:\n"
        "    json.dump(payload, f, ensure_ascii=False)\n"
        "sys.exit(%d)\n" % (str(tmp_path / "seen.json").replace("\\", "\\\\"), rc)
    ), encoding="utf-8")
    return str(p)


def _reg(tmp_path, trigger=r"^\s*gradient\b", rc=0, streams=None, timeout=60, body=None):
    return {
        "streams": streams or {},
        "commands": {"gradient": {"trigger": trigger, "timeout": timeout,
                                  "exec": [sys.executable, _handler(tmp_path, rc=rc, body=body)]}},
    }


def _msg(mid, text):
    return {"id": mid, "content": text, "timestamp": "2026-08-10T19:14:00Z",
            "author": {"bot": False, "id": "OWNER"}}


# --------------------------------------------------------------------------- registration
def test_bad_regex_is_skipped_loudly(capsys):
    reg = {"commands": {"broken": {"trigger": "([unclosed", "exec": ["python", "x.py"]},
                        "fine": {"trigger": "^ok", "exec": ["python", "y.py"]}}}
    got = commands.load(reg)
    err = capsys.readouterr().err
    assert [c["name"] for c in got] == ["fine"]
    assert "broken" in err, "a handler dropped for a typo must say so; silence looks like a dead bus"


def test_underscore_keys_are_comments_not_broken_handlers(capsys):
    """The registry documents itself with _note keys. Those must not be reported as broken
    handlers every single tick, or the real warnings become invisible in the noise."""
    reg = {"commands": {"_note": "how this section works",
                        "g": {"trigger": "^g", "exec": ["python", "g.py"]}}}
    assert [c["name"] for c in commands.load(reg)] == ["g"]
    assert capsys.readouterr().err == ""


def test_incomplete_registration_is_skipped_loudly(capsys):
    reg = {"commands": {"noexec": {"trigger": "^x"}, "notrigger": {"exec": ["python"]}}}
    assert commands.load(reg) == []
    err = capsys.readouterr().err
    assert "noexec" in err and "notrigger" in err


def test_bare_python_resolves_to_this_interpreter():
    """A scheduled task's PATH on Windows often holds only the WindowsApps alias for `python`: a
    stub that resolves and then runs nothing. A handler that never executes looks exactly like a
    bus that stopped reading, so the interpreter is pinned rather than looked up."""
    reg = {"commands": {"g": {"trigger": "^g", "exec": ["python", "g.py"]}}}
    assert commands.load(reg)[0]["exec"][0] == sys.executable
    reg = {"commands": {"g": {"trigger": "^g", "exec": ["node", "g.js"]}}}
    assert commands.load(reg)[0]["exec"][0] == "node", "only python is pinned, other runtimes are not"


def test_exec_expands_user_and_env(monkeypatch):
    monkeypatch.setenv("MYTOOLS", "/opt/tools")
    reg = {"commands": {"g": {"trigger": "^g", "exec": ["python", "$MYTOOLS/g.py", "~"]}}}
    argv = commands.load(reg)[0]["exec"]
    assert argv[1] == "/opt/tools/g.py"
    assert argv[2] == os.path.expanduser("~")


# --------------------------------------------------------------------------- opt out
def test_listen_false_channel_runs_no_handlers(tmp_path):
    reg = _reg(tmp_path, streams={"manual": {"channel_id": "7", "listen": False}})
    msgs = [_msg("1", "gradient og")]
    claimed, remaining, results = commands.route(msgs, "manual", "7", reg, post=False)
    assert claimed == [] and results == []
    assert remaining == msgs, "an opted-out channel still forwards its messages, it just does not run commands"


def test_unregistered_channel_listens_by_default(tmp_path):
    reg = _reg(tmp_path)
    claimed, remaining, _ = commands.route([_msg("1", "gradient")], "#常规", "99", reg, post=False)
    assert len(claimed) == 1 and remaining == []


# --------------------------------------------------------------------------- the invariant
def test_unmatched_messages_are_never_dropped(tmp_path):
    """The whole point. A message no handler wants must come back in `remaining`, not vanish."""
    reg = _reg(tmp_path)
    msgs = [_msg("1", "开一个新的频道吧，一个用来执行每天自动化输出的命令"),
            _msg("2", "gradient og"),
            _msg("3", "另一个就是纯静态备份，你不需要回复")]
    claimed, remaining, _ = commands.route(msgs, "#常规", "99", reg, post=False)
    assert [m["id"] for m in claimed] == ["2"]
    assert [m["id"] for m in remaining] == ["1", "3"], "unclaimed messages must survive routing"
    assert len(claimed) + len(remaining) == len(msgs), "no message may be lost in the split"


def test_matching_is_per_message_not_per_batch(tmp_path):
    """Matching the concatenated batch would let one command in it claim everything else too."""
    reg = _reg(tmp_path)
    msgs = [_msg("1", "gradient"), _msg("2", "顺便说一下床垫退货窗口是 90 天")]
    claimed, remaining, _ = commands.route(msgs, "s", "99", reg, post=False)
    assert [m["id"] for m in claimed] == ["1"]
    assert [m["id"] for m in remaining] == ["2"]


def test_trigger_must_anchor_not_merely_appear(tmp_path):
    """A sentence that mentions the word is not a command. The registry's own regex decides, and
    the shipped one anchors at the start; this locks that the routing does not widen it."""
    reg = _reg(tmp_path)
    claimed, remaining, _ = commands.route(
        [_msg("1", "那个 gradient 图能不能换个颜色")], "s", "99", reg, post=False)
    assert claimed == [] and len(remaining) == 1


# --------------------------------------------------------------------------- the handler contract
def test_payload_reaches_the_handler_on_stdin_in_utf8(tmp_path):
    reg = _reg(tmp_path)
    commands.route([_msg("42", "gradient 背景 x3")], "#常规", "99", reg, post=False)
    got = json.loads((tmp_path / "seen.json").read_text(encoding="utf-8"))
    assert got["text"] == "gradient 背景 x3", "non-ASCII must survive the handoff (argv would not)"
    assert got["channel_id"] == "99" and got["message_id"] == "42" and got["stream"] == "#常规"


def test_failed_handler_still_claims_and_reports(tmp_path, monkeypatch):
    """A broken command must not be re-routed into the model. Filing 'gradient x3' as a to-do is
    not a recovery from a render failure, it is a second wrong answer."""
    reg = _reg(tmp_path, rc=3)
    posted = []
    monkeypatch.setattr(commands.relay, "send",
                        lambda text, **kw: posted.append((text, kw.get("channel_id"))) or True)
    claimed, remaining, results = commands.route([_msg("1", "gradient")], "s", "99", reg)
    assert len(claimed) == 1 and remaining == []
    assert results[0]["ok"] is False
    assert posted and posted[0][1] == "99", "the failure is reported in the channel that asked"


def test_handler_timeout_is_a_failure_not_a_hang(tmp_path, monkeypatch):
    slow = "import time\ntime.sleep(30)\n"
    reg = _reg(tmp_path, timeout=1, body=slow)
    monkeypatch.setattr(commands.relay, "send", lambda text, **kw: True)
    _, _, results = commands.route([_msg("1", "gradient")], "s", "99", reg)
    assert results[0]["ok"] is False and "timed out" in results[0]["detail"]


def test_one_bad_handler_does_not_stop_the_batch(tmp_path, monkeypatch):
    reg = _reg(tmp_path, rc=1)
    reg["commands"]["gradient"]["exec"] = ["definitely-not-a-real-binary-xyz"]
    monkeypatch.setattr(commands.relay, "send", lambda text, **kw: True)
    msgs = [_msg("1", "gradient"), _msg("2", "普通消息")]
    claimed, remaining, results = commands.route(msgs, "s", "99", reg)
    assert results[0]["ok"] is False and "could not start" in results[0]["detail"]
    assert [m["id"] for m in remaining] == ["2"], "the rest of the batch still goes through"


def test_no_commands_registered_is_a_clean_passthrough(tmp_path):
    claimed, remaining, results = commands.route([_msg("1", "anything")], "s", "9", {}, post=False)
    assert claimed == [] and results == [] and len(remaining) == 1


# --------------------------------------------------------------------------- round trip with ingest
def test_remaining_messages_render_exactly_like_the_inbox(tmp_path):
    """What the chain judges and what the inbox records must be the same rendering, or the durable
    trace stops matching what actually got processed."""
    reg = _reg(tmp_path)
    msgs = [_msg("1", "gradient"), _msg("2", "记一下:床垫退货")]
    _, remaining, _ = commands.route(msgs, "s", "99", reg, post=False)
    rendered = ingest.format_messages(remaining)
    assert "床垫退货" in rendered
    assert "gradient" not in rendered, "a claimed command must not reach the chain"
    assert rendered == ingest.format_messages([msgs[1]])


@pytest.mark.parametrize("text", ["gradient", "  gradient og", "GRADIENT x2"])
def test_trigger_is_case_and_space_tolerant(tmp_path, text):
    reg = _reg(tmp_path)
    claimed, _, _ = commands.route([_msg("1", text)], "s", "99", reg, post=False)
    assert len(claimed) == 1
