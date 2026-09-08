"""任务退役:一个动作改完三处登记,或者一处都不改。

为什么不是「点一下停用」就完了:导出的任务 XML **不记录 disabled 状态**。一个只是被停用、
却仍然留在备份 allow-list 里的任务,换机还原时会被原样装回去,而且是启用的。
一份忠实还原一个已知缺陷的备份,比忘掉这个任务更糟。

所以退役 = 停用 + 退出备份 allow-list + 退出健康清单 + 把原因写进 Description。
四步缺任何一步都会留下一个以后没人敢重启用、也没人敢删的东西。

写法上抄归档器那条:**先把整个计划算完,再动手写**。中途断掉最多留下一个半成品,
而不是三处登记互相矛盾的状态。写之前每个被改的文件都留一份 .bak。

一个致命细节:健康清单必须保持**纯 ASCII**。读它的那一侧没有指定编码,一旦写进
非 ASCII 字符,它会在某个环节解码失败,而失败的表现是整份清单读不出来,
也就是「所有任务都没有被监控」,同时界面看起来完全正常。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

# 在 pythonw(GUI 子系统)下,每个控制台子程序都要新分配一个控制台。那次分配很慢,
# 而且并发时根本不成立:实测同一条 git 命令,普通 python 下几毫秒,pythonw 下单次
# 4.5 秒,四个并发全部 15 秒超时。加上这个标志之后单次降到 0.08 秒。
#
# 这个坑只在生产形态下出现,而开发期测试都是用普通 python 跑的:探针必须复现真实的
# 调用形状,否则测的是另一个程序。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# allow-list 在一个 PowerShell 脚本里,形如 $TaskNames = @( 'A', 'B' )
TASKNAMES_BLOCK = re.compile(r"(\$TaskNames\s*=\s*@\()(.*?)(\n\s*\))", re.S)


def _env_path(var: str) -> Path | None:
    v = os.environ.get(var)
    return Path(os.path.expanduser(v)) if v else None


def _powershell() -> str:
    return os.environ.get("TASK_CONSOLE_POWERSHELL") or "powershell.exe"


def _task_state(name: str) -> str | None:
    """任务此刻是什么状态。None 只表示一件事:**确认它没注册**。

    判据必须是**状态**而不是**存在**。用「存在」来决定要不要停用,会让一个已经退役的
    任务每次都再被停用一次:动作报告永远说自己改了东西,而幂等就无从谈起。

    ⚠ 这里原来把「确认它不存在」和「我查不到」编码成了同一个 None,而这两件事在退役里
    导向相反的动作。查不到状态时(任务计划服务异常、CIM 出问题、非管理员、PowerShell
    起不来)plan 会把 disable 那一步报成 not-found,apply 于是跳过它,却照常把这个任务
    从备份 allow-list 和健康清单里摘掉 :

        任务还注册着、还启用着、还在按点跑,但它已经不在备份里(换机静默丢失)
        也不在健康监控里(死了没人知道),而返回值是 ok:True。

    这是三处登记里最坏的一种错位。所以查不到就抛,不返回。
    """
    r = subprocess.run(
        [_powershell(), "-NoProfile", "-Command",
         "$t = Get-ScheduledTask -TaskName $env:TC_NAME -ErrorAction SilentlyContinue;"
         "if ($t) { Write-Output \"$($t.State)\" }"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=dict(os.environ, TC_NAME=name), timeout=90, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    if r.returncode != 0:
        from maint import Refused
        raise Refused(
            "查不到任务状态(调度器读不出来),拒绝退役: "
            + ((r.stderr or r.stdout or "").strip()[:200] or f"rc={r.returncode}"),
            "state_unreadable")
    out = (r.stdout or "").strip()
    return out or None


def plan(name: str, reason: str) -> dict:
    """算出这次退役会改什么,不写任何东西。

    每一处都单独报 state:already / will-change / missing-config / not-found。
    把它们折叠成一个布尔会让「这一处本来就不用改」和「这一处我没能力改」变成同一个答案。
    """
    from maint import Refused, SAFE_NAME
    if not SAFE_NAME.match(name or ""):
        raise Refused(f"任务名不合法: {name!r}", "bad_name")
    if not (reason or "").strip():
        # 没有原因的退役,半年后没人敢重启用,也没人敢删。这不是啰嗦,是规范里明写的。
        raise Refused("退役必须写明原因", "no_reason")

    steps = []

    state = _task_state(name)
    if state is None:
        disable_state = "not-found"
    elif state == "Disabled":
        disable_state = "already"
    else:
        disable_state = "will-change"
    steps.append({"step": "disable", "state": disable_state,
                  "detail": "停用任务并把原因写进 Description",
                  "taskState": state})

    al = _env_path("TASK_CONSOLE_ALLOWLIST")
    if not al or not al.is_file():
        steps.append({"step": "allowlist", "state": "missing-config",
                      "detail": "没有配 TASK_CONSOLE_ALLOWLIST"})
    else:
        txt = al.read_text(encoding="utf-8-sig", errors="replace")
        m = TASKNAMES_BLOCK.search(txt)
        listed = bool(m and re.search(rf"^\s*'{re.escape(name)}'", m.group(2), re.M))
        steps.append({"step": "allowlist",
                      "state": "will-change" if listed else "already",
                      "detail": str(al)})

    hp = _env_path("TASK_CONSOLE_HEALTH")
    if not hp or not hp.is_file():
        steps.append({"step": "health", "state": "missing-config",
                      "detail": "没有配 TASK_CONSOLE_HEALTH"})
    else:
        try:
            data = json.loads(hp.read_text(encoding="utf-8-sig"))
            watched = any(t.get("name") == name for t in data.get("tasks", []))
        except (OSError, ValueError):
            watched = False
        steps.append({"step": "health", "state": "will-change" if watched else "already",
                      "detail": str(hp)})

    return {"name": name, "reason": reason.strip(), "steps": steps,
            "changes": sum(1 for s in steps if s["state"] == "will-change"),
            "blocked": [s["step"] for s in steps if s["state"] == "missing-config"]}


def _rewrite_allowlist(path: Path, name: str) -> bool:
    txt = path.read_text(encoding="utf-8-sig", errors="replace")
    m = TASKNAMES_BLOCK.search(txt)
    if not m:
        return False
    body = m.group(2)
    kept = [ln for ln in body.splitlines()
            if not re.match(rf"^\s*'{re.escape(name)}'\s*,?\s*$", ln)]
    if len(kept) == len(body.splitlines()):
        return False
    new = m.group(1) + "\n".join(kept) + m.group(3)
    path.write_text(txt[:m.start()] + new + txt[m.end():], encoding="utf-8")
    return True


def _rewrite_health(path: Path, name: str) -> bool:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    tasks = data.get("tasks", [])
    kept = [t for t in tasks if t.get("name") != name]
    if len(kept) == len(tasks):
        return False
    data["tasks"] = kept
    # ensure_ascii 不是风格选择。读这份清单的那一侧没有指定编码,写进非 ASCII 会让它
    # 在某个环节整份解码失败,而那等于「所有任务都没有被监控」,同时界面看起来完全正常。
    path.write_text(json.dumps(data, ensure_ascii=True, indent=2) + "\n", encoding="ascii")
    return True


def _disable(name: str, reason: str) -> tuple[bool, str]:
    ps = ("$ErrorActionPreference='Stop';"
          "$t = Get-ScheduledTask -TaskName $env:TC_NAME;"
          "$d = $t.Description;"
          "$note = $env:TC_NOTE;"
          "if ($d -notlike \"*$note*\") { $t.Description = ($d + \"`n\" + $note).Trim() };"
          "Set-ScheduledTask -TaskName $env:TC_NAME -Description $t.Description | Out-Null;"
          "Disable-ScheduledTask -TaskName $env:TC_NAME | Out-Null;")
    note = f"[RETIRED] {reason}"
    r = subprocess.run([_powershell(), "-NoProfile", "-Command", ps],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env=dict(os.environ, TC_NAME=name, TC_NOTE=note), timeout=120, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    return r.returncode == 0, (r.stderr or r.stdout or "").strip()[:200]


def apply(name: str, reason: str) -> dict:
    """执行退役。先算完整计划,再依次写。每个被改的文件先留一份 .bak。"""
    from maint import Refused
    p = plan(name, reason)
    if p["blocked"]:
        raise Refused("这几处没有配置,拒绝只做一半: " + ", ".join(p["blocked"]), "no_config")

    done, backups = [], []
    for step in p["steps"]:
        if step["state"] != "will-change":
            continue
        if step["step"] == "disable":
            ok, msg = _disable(name, p["reason"])
            if not ok:
                raise Refused(f"停用失败: {msg}", "disable_failed")
            done.append("disable")
        else:
            path = Path(step["detail"])
            bak = path.with_suffix(path.suffix + ".bak")
            shutil.copy2(path, bak)
            backups.append(str(bak))
            changed = (_rewrite_allowlist(path, name) if step["step"] == "allowlist"
                       else _rewrite_health(path, name))
            if not changed:
                # plan 说这一处要改,写的时候却一个字没动。这只会在 plan 和 rewrite 用了
                # 两个不同的「这一行是不是它」判据时发生(实测:allow-list 里一行写两个名字,
                # plan 报 will-change 而 rewrite 的正则整块匹配不上)。
                #
                # 吞掉它的后果是本模块开头写的那个:任务被停用了,却仍然留在备份 allow-list 里,
                # 换机还原时会被原样装回去而且是启用的 :「一份忠实还原一个已知缺陷的备份,
                # 比忘掉这个任务更糟」。而界面收到的是 ok:True,唯一的线索是 done 数组少一项,
                # 没有任何地方把 plan 的 will-change 和 apply 的 done 对账。
                raise Refused(
                    f"{step['step']}: 计划说要改,实际一个字没动({path.name})。"
                    "已改的部分留在 .bak 旁边,请人工核对。",
                    "rewrite_noop")
            done.append(step["step"])
    return {"ok": True, "name": name, "done": done, "backups": backups,
            "reason": p["reason"]}
