#!/usr/bin/env python3
"""Check every `llmcall.call(...)` in this repo against the INSTALLED llmcall signature.

Why a static check: the runtime that ran these scripts for weeks carried an older llmcall whose
`call()` accepted cwd=, cancel=, env=, policy=, selection= and requirements=. The current package
(0.3.0) accepts none of them, and a stale keyword is a TypeError only on the day that path runs,
which for a work order can be hours into a detached runner. Reading the call sites is cheap and
fails at test time instead.

Keyword sources understood: literal keywords, and `**name` where `name` is built in the same
function from dict literals (including conditional expressions of dict literals) and
`name["key"] = ...` assignments with constant keys. Anything else behind `**` cannot be proven
and is reported, never assumed fine.

The allowed names come from `inspect.signature(llmcall.call)` in a CLEAN child interpreter, so a
test-time stub of `llmcall` can never be the thing that approves the call sites.

Usage:
    python tools/llmcall_contract.py [--python EXE] [FILE ...]
Exit 0 when every call site is proven, 1 with one line per finding otherwise.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "schedule-reminder" / "scripts"

_PROBE = r"""
import dataclasses, inspect, json
import llmcall
from llmcall import core
params = list(inspect.signature(llmcall.call).parameters)
print(json.dumps({
    "parameters": params,
    "result_fields": [f.name for f in dataclasses.fields(llmcall.Result)],
    "attempt_fields": [f.name for f in dataclasses.fields(llmcall.Attempt)],
    "reasons": {name: getattr(core, name) for name in dir(core) if name.startswith("REASON_")},
    "rung_group": callable(getattr(llmcall, "rung_group", None)),
    "file": llmcall.__file__,
}))
"""


def installed_contract(python=None):
    """Describe the installed llmcall from a child interpreter that has no test stubs."""
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    flags = {"creationflags": 0x08000000} if os.name == "nt" else {}
    result = subprocess.run([python or sys.executable, "-B", "-c", _PROBE], capture_output=True,
                            text=True, encoding="utf-8", timeout=60, env=env,
                            cwd=str(Path.home()), **flags)
    if result.returncode:
        raise RuntimeError("installed llmcall is unavailable: " + (result.stderr or "").strip()[-400:])
    return json.loads(result.stdout.strip().splitlines()[-1])


def _call_names(tree):
    """Names bound to llmcall.call by `from llmcall import call [as x]`."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "llmcall":
            for alias in node.names:
                if alias.name == "call":
                    names.add(alias.asname or "call")
    return names


def _is_llmcall_call(node, direct):
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "call":
        return isinstance(func.value, ast.Name) and func.value.id == "llmcall"
    return isinstance(func, ast.Name) and func.id in direct


def _dict_keys(expr):
    """Constant string keys of a dict literal or conditional of dict literals, else None."""
    if isinstance(expr, ast.Dict):
        keys = set()
        for key in expr.keys:
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                return None
            keys.add(key.value)
        return keys
    if isinstance(expr, ast.IfExp):
        body, orelse = _dict_keys(expr.body), _dict_keys(expr.orelse)
        return None if body is None or orelse is None else body | orelse
    return None


def _starred_keys(name, scope):
    """Every key that may reach `**name` inside `scope`, or None when one cannot be proven."""
    keys, seen = set(), False
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    found = _dict_keys(node.value)
                    if found is None:
                        return None
                    keys |= found
                    seen = True
                elif (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                      and target.value.id == name):
                    key = target.slice
                    if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                        return None
                    keys.add(key.value)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            target = node.target
            if isinstance(target, ast.Name) and target.id == name:
                return None
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and isinstance(node.func.value, ast.Name) and node.func.value.id == name
              and node.func.attr in ("update", "setdefault")):
            return None
    return keys if seen else None


def count_calls(source, filename="<source>"):
    """How many llmcall.call sites the scanner recognises, so a vacuous pass is visible."""
    tree = ast.parse(source, filename=filename)
    direct = _call_names(tree)
    return sum(1 for node in ast.walk(tree)
               if isinstance(node, ast.Call) and _is_llmcall_call(node, direct))


def check_source(source, allowed, filename="<source>"):
    """Return (line, message) findings for every unprovable or unsupported keyword."""
    tree = ast.parse(source, filename=filename)
    direct = _call_names(tree)
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    findings = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _is_llmcall_call(node, direct)):
            continue
        scope = node
        while scope in parents and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = parents[scope]
        for keyword in node.keywords:
            if keyword.arg is not None:
                names = {keyword.arg}
            elif isinstance(keyword.value, ast.Name):
                names = _starred_keys(keyword.value.id, scope)
                if names is None:
                    findings.append((node.lineno, "**%s cannot be proven" % keyword.value.id))
                    continue
            else:
                names = _dict_keys(keyword.value)
                if names is None:
                    findings.append((node.lineno, "** expression cannot be proven"))
                    continue
            for name in sorted(names - set(allowed)):
                findings.append((node.lineno, "unsupported keyword %s=" % name))
    return findings


def check_files(paths, allowed):
    findings = []
    for path in paths:
        source = Path(path).read_text(encoding="utf-8-sig")
        for line, message in check_source(source, allowed, str(path)):
            findings.append("%s:%d: %s" % (path, line, message))
    return findings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--python", help="interpreter whose installed llmcall is the contract")
    parser.add_argument("files", nargs="*")
    args = parser.parse_args(argv)
    contract = installed_contract(args.python)
    files = args.files or sorted(str(p) for p in SCRIPTS.glob("*.py"))
    findings = check_files(files, contract["parameters"])
    for finding in findings:
        print(finding)
    print("checked %d file(s) against llmcall.call%s from %s" % (
        len(files), tuple(contract["parameters"]), contract["file"]), file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
