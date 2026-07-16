#!/usr/bin/env python3
"""Cost-ordered background LLM chain (the reusable primitive for all headless calls).

Feed a prompt on STDIN-equivalent (passed as a string); providers are tried in a cost-ordered
CHAIN and the first that returns non-empty text wins:

  1. codex   -- OpenAI Codex CLI (`codex exec`), spare quota / cheapest -> try first
  2. cc      -- Claude Code headless via a hosted inference gateway
  3. claude  -- plain Claude Code headless (direct Anthropic, full price) -> last resort

This mirrors email-monitor's em_agent_classify chain but is GENERIC: it returns raw model text and
leaves prompt-building + parsing to the caller. Read-only by design (codex runs `-s read-only`): use
it for JUDGMENT, then apply side effects deterministically in the caller.

  call_chain(prompt, chain=None, providers=None, timeout=180, log=None) -> str | None

Never raises: any provider failure (missing binary, timeout, non-zero exit, empty output) is skipped
and the next is tried; None means the whole chain failed (caller uses a deterministic fallback).
Stdlib only. Absolute binary paths (a scheduled task runs with a minimal PATH).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

_NOWINDOW = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
DEFAULT_CHAIN = ["codex", "cc", "claude"]

_CODEX_PATHS = [os.path.expanduser(r"~/AppData/Roaming/npm/codex.cmd"),
                os.path.expanduser(r"~/AppData/Roaming/npm/codex")]
_CC_PATHS = [os.path.expanduser(r"~/.local/bin/cc.cmd"), os.path.expanduser(r"~/.local/bin/cc")]
_CLAUDE_PATHS = [os.path.expanduser(r"~/.local/bin/claude.exe"),
                 os.path.expanduser(r"~/.local/bin/claude")]


def _find(name, explicit, candidates):
    if explicit and os.path.isfile(explicit):
        return explicit
    found = shutil.which(name)
    if found:
        return found
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def _argv(binp, *args):
    """Prefix a .cmd/.bat launcher with `cmd /c` on Windows; run other binaries directly."""
    if sys.platform == "win32" and binp.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", binp, *args]
    return [binp, *args]


def _run(cmd, prompt, timeout):
    try:
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                           encoding="utf-8", timeout=timeout, **_NOWINDOW)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if p.returncode != 0:
        return None
    return p.stdout or ""


def _unwrap_envelope(stdout):
    """Claude Code `--output-format json` wraps the model text in {result: "..."}; unwrap it."""
    if not stdout:
        return ""
    try:
        env = json.loads(stdout)
        if isinstance(env, dict) and "result" in env:
            return env.get("result") or ""
    except Exception:
        pass
    return stdout


def _call_codex(prompt, pcfg, timeout):
    binp = _find("codex", pcfg.get("bin"), _CODEX_PATHS)
    if not binp:
        return None
    model = pcfg.get("model", "gpt-5.5")
    effort = pcfg.get("reasoning", "high")
    fd, outpath = tempfile.mkstemp(prefix="sr_codex_", suffix=".txt")
    os.close(fd)
    try:
        cmd = _argv(binp, "exec", "-m", model, "-c", "model_reasoning_effort=%s" % effort,
                    "-s", "read-only", "--skip-git-repo-check", "--ephemeral",
                    "--color", "never", "-o", outpath, "-")
        if _run(cmd, prompt, timeout) is None:
            return None
        with open(outpath, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None
    finally:
        try:
            os.remove(outpath)
        except OSError:
            pass


# Both Claude Code CLIs otherwise load every configured MCP server (~26 here) and hang after the work
# is done, running out the scheduled task's time limit -> empty answer -> chain falls through for no
# reason. Disabling MCP is mandatory for a headless one-shot judge (it needs no tools).
_NO_MCP = ("--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}')


def _call_cc(prompt, pcfg, timeout):
    binp = _find("cc", pcfg.get("bin"), _CC_PATHS)
    if not binp:
        return None
    model = pcfg.get("model", "claude-opus-4-8")
    out = _run(_argv(binp, "-p", "--model", model, "--output-format", "json", *_NO_MCP), prompt, timeout)
    return _unwrap_envelope(out) if out else None


def _call_claude(prompt, pcfg, timeout):
    binp = _find("claude", pcfg.get("bin"), _CLAUDE_PATHS)
    if not binp:
        return None
    model = pcfg.get("model", "claude-opus-4-8")
    out = _run(_argv(binp, "-p", "--model", model, "--output-format", "json", *_NO_MCP), prompt, timeout)
    return _unwrap_envelope(out) if out else None


_CALLERS = {"codex": _call_codex, "cc": _call_cc, "claude": _call_claude}


def call_chain(prompt, chain=None, providers=None, timeout=180, log=None):
    """Try providers in `chain` order; return the first non-empty raw text, else None."""
    chain = chain or DEFAULT_CHAIN
    providers = providers or {}
    for name in chain:
        fn = _CALLERS.get(name)
        if not fn:
            continue
        raw = fn(prompt, providers.get(name, {}), timeout)
        if raw and raw.strip():
            if log:
                log("llm_chain: %s answered (%d chars)" % (name, len(raw)))
            return raw
        if log:
            log("llm_chain: %s unavailable/empty, trying next" % name)
    return None


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Cost-ordered background LLM chain (prompt on stdin).")
    ap.add_argument("--chain", default=",".join(DEFAULT_CHAIN))
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--codex-model", default="gpt-5.5")
    ap.add_argument("--codex-reasoning", default="high")
    ap.add_argument("--claude-model", default="claude-opus-4-8")
    a = ap.parse_args()
    providers = {"codex": {"model": a.codex_model, "reasoning": a.codex_reasoning},
                 "cc": {"model": a.claude_model}, "claude": {"model": a.claude_model}}
    prompt = sys.stdin.read()
    out = call_chain(prompt, [c.strip() for c in a.chain.split(",") if c.strip()],
                     providers, a.timeout, log=lambda m: print(m, file=sys.stderr))
    if out is None:
        return 1
    sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
