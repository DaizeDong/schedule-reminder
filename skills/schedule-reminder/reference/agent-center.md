# Agent Center — unified relay + daily digest (frozen surface)

schedule-reminder is the **single backend** every other skill routes through: state (the reminder
contract), **outbound Discord** (`relay.py`), and the **daily 当日总结** (`digest.py`). Downstream
skills call these via subprocess and never re-implement transport or scheduling.

> Design law (same as the base): **the contract is the surface, not the transport.** Skills depend on
> `relay.py <stream>` / `digest.py`, never on webhooks, bots, or the registry file directly — so the
> Discord wiring can change forever without touching any skill.

## Topology

```
each skill  --(relay.py send --stream X)-->  Agent Center #X channel  (per-stream webhook + identity)
each skill  --(digest section contributor)-->  digest.py  --(one summary)-->  Big Brother DM
schedule-reminder tick  --(relay.py send --stream reminders)-->  #reminders
```

Streams (Agent Center server): `mail · hotspots · demand · promotion · support · crypto · infra ·
reminders`. The aggregated daily summary goes to **Big Brother DM**, not a channel.

## relay.py — the single Discord egress

```
python relay.py send   --stream <name> (--text T | --json '{"content":..,"username":..}')
python relay.py digest --text T        # aggregated summary -> Big Brother DM
python relay.py list                   # configured streams (NEVER prints webhook URLs)
python relay.py health                 # registry sane? (no network, no secrets)
```

- **Registry (secret, never committed)**: discovery = env `AGENT_CENTER_CONFIG`, else
  `~/.agent-center/registry.json`. Shape: `{"streams":{"<name>":{"webhook":"...","username":"..."}},
  "big_brother":{...}}`.
- **Per-stream identity**: each message sets `username` so a stream shows its own name/avatar.
- **Fallback**: unknown stream / missing registry → delivered to Big Brother DM (prefixed
  `[stream]`) so nothing is ever silently lost.
- **Gotcha (encoded in code)**: Discord/Cloudflare 403s the default urllib User-Agent — `relay.py`
  always sends a real `User-Agent`.
- **Test seam**: `AGENT_CENTER_RELAY_DRYRUN=1` skips the network.

## digest.py — the one daily 当日总结

One daily task aggregates every *installed* skill's section into a single summary.

```
python digest.py run [--now ISO] [--dry-run]
python digest.py register --name N --title T --cmd 'argv...' [--timeout S] [--disabled]
python digest.py unregister --name N
python digest.py list
```

- **Contributors file**: discovery = env `AGENT_CENTER_DIGEST`, else `~/.agent-center/digest.json`.
- **A contributor** is a command that prints its 当日总结段 (markdown) to stdout and exits 0. Empty
  stdout → section skipped. Failure/timeout/nonzero → section skipped and reported to `#infra`
  (never aborts the whole digest). Child stdio is forced to UTF-8 (Windows GBG hosts otherwise
  mangle emoji/Chinese).
- **Pluggable**: a skill registers its contributor at install time; uninstalled skills are simply
  absent. This is exactly skill todo.md's "如果这个 skill 安装了，则联动每日的固定定时任务".

## How a downstream skill integrates (copy-paste)

```python
import subprocess, sys, os
REMINDER_DIR = os.path.expanduser("~/.claude/skills/schedule-reminder/scripts")  # or probe
def push(stream, text, username=None):
    cmd = [sys.executable, os.path.join(REMINDER_DIR, "relay.py"), "send", "--stream", stream, "--text", text]
    if username: cmd += ["--username", username]
    return subprocess.run(cmd).returncode == 0
```

Register a daily section at install:

```
python <reminder>/scripts/digest.py register --name hotspots --title "💡 当日商机" \
    --cmd "python <skill>/scripts/digest.py --section"
```
