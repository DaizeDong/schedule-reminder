#!/usr/bin/env python3
"""Back-compat shim.

The cost-ordered background LLM chain (codex -> cc -> claude) now lives in the shared `llmcall`
package (CodesSelf/llmcall), so there is exactly ONE implementation of the chain + the headless
footguns for the whole fleet. This module re-exports `call_chain` unchanged, so `dispatch.py`
(`import llm_chain; llm_chain.call_chain(...)`) and any `python llm_chain.py` caller keep working
with zero edits. Tests that monkeypatch `dispatch.llm_chain.call_chain` still patch this name.

`call_chain(prompt, chain=None, providers=None, timeout=180, log=None) -> str | None` (providers is
accepted for signature compatibility but ignored; model/effort resolve from ~/.codex/config.toml
inside llmcall). Pass `model=`/`effort=` to `llmcall.call` directly if you need an override.
"""
import sys

from llmcall import DEFAULT_CHAIN, call_chain  # noqa: F401  (re-exported; patched in tests)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Cost-ordered background LLM chain (prompt on stdin).")
    ap.add_argument("--chain", default=",".join(DEFAULT_CHAIN))
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--codex-model", default=None, help="(ignored; resolved from ~/.codex/config.toml)")
    ap.add_argument("--codex-reasoning", default=None, help="(ignored; resolved from config)")
    ap.add_argument("--claude-model", default=None, help="(ignored; llmcall default)")
    a = ap.parse_args()
    prompt = sys.stdin.read()
    out = call_chain(prompt, [c.strip() for c in a.chain.split(",") if c.strip()],
                     timeout=a.timeout, log=lambda m: print(m, file=sys.stderr))
    if out is None:
        return 1
    sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
