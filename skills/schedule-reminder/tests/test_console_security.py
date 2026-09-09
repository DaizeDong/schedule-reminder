#!/usr/bin/env python3
"""task_console 的四道安全控制,现在有了会失败的证据。

这四条以前只有一次人工实测的记录。一个能改系统状态的 HTTP 端点,它的防线不该只存在于
某人的记忆里:所以每条控制都配一条**正对照**(正常请求必须通过),否则一个把所有请求
都拒掉的服务器也能让「拒绝」那半边全绿。

Host 白名单是新加的。绑 127.0.0.1 挡得住网段,挡不住 DNS rebinding:token 是被替换进
'/' 那张页面的,任何能同源请求 '/' 的东西直接把 token 读走。rebinding 之后 Host 头带的是
攻击者的域名,和三个回环字面量对不上,这就是它被挡住的地方。
"""
import http.client
import json
import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

HERE = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(HERE, "scripts", "task_console"))

if os.name != "nt":
    pytest.skip("task_console 只在 Windows 上有意义", allow_module_level=True)

import server as S  # noqa: E402

TOKEN = "test-token-not-a-real-one"

# ⚠ 这一行必须跑在任何 fixture 之前:它拍下 Handler **出厂时**的 token 默认值。
# 下面那条用例要钉的正是「出厂默认值不能通过鉴权」,而如果用例自己去 setattr 一个哨兵,
# 它测的就变成了「我刚设的那个值不能通过」—— 把默认值改回 "" 照样绿。
# (投毒时实测过:第一版就是那个形状,四条投毒里唯独这条没红。)
DEFAULT_TOKEN = S.Handler.token


@pytest.fixture(scope="module")
def srv():
    S.Handler.token = TOKEN
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), S.Handler)
    port = httpd.server_address[1]
    S.Handler.allowed_hosts = {f"localhost:{port}", f"127.0.0.1:{port}", f"[::1]:{port}"}
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield port
    httpd.shutdown()


def call(port, method, path, host=None, token=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Host": host if host is not None else f"127.0.0.1:{port}"}
    if token is not None:
        headers["X-Console-Token"] = token
    payload = json.dumps(body).encode() if body is not None else None
    if payload:
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(payload))
    c.request(method, path, body=payload, headers=headers)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


# ---------- 令牌 ----------

def test_api_without_token_is_403(srv):
    st, _ = call(srv, "GET", "/api/hours?from=2026-01-01&to=2026-01-02")
    assert st == 403


def test_api_with_wrong_token_is_403(srv):
    st, _ = call(srv, "GET", "/api/hours?from=2026-01-01&to=2026-01-02", token="wrong")
    assert st == 403


def test_correct_token_is_not_403(srv):
    # 正对照。没有它,一个把每个请求都拒掉的服务器会让上面两条全绿。
    st, _ = call(srv, "GET", "/api/hours?from=2026-01-01&to=2026-01-02", token=TOKEN)
    assert st != 403


# ---------- 动词白名单 ----------

def test_unknown_verb_is_400(srv):
    st, body = call(srv, "POST", "/api/act", token=TOKEN,
                    body={"verb": "delete", "name": "Whatever"})
    assert st == 400
    assert b"verb" in body


def test_missing_task_name_is_400(srv):
    st, _ = call(srv, "POST", "/api/act", token=TOKEN, body={"verb": "run", "name": ""})
    assert st == 400


def test_act_without_token_is_403_before_any_verb_check(srv):
    # 顺序很重要:一个先校验动词再校验令牌的端点,会把「这个动词存在吗」告诉未鉴权的调用方。
    st, _ = call(srv, "POST", "/api/act", body={"verb": "delete", "name": "X"})
    assert st == 403


def test_allowed_verbs_are_exactly_four():
    # 这条钉的是集合本身。加动词是一个需要有人明确改这行的动作,不是顺手就能滑进去的。
    assert set(S.VERBS) == {"enable", "disable", "run", "stop"}


# ---------- Host 白名单(新加) ----------

def test_rebinding_host_is_rejected(srv):
    st, body = call(srv, "GET", "/", host="evil.example.com")
    assert st == 400
    assert b"host" in body.lower()


def test_missing_host_is_rejected(srv):
    st, _ = call(srv, "GET", "/", host="")
    assert st == 400


def test_loopback_hosts_are_accepted(srv):
    # 正对照。三个回环写法浏览器都可能发,一个把它们也挡掉的白名单等于把控制台锁死。
    for h in (f"127.0.0.1:{srv}", f"localhost:{srv}"):
        st, _ = call(srv, "GET", "/", host=h)
        assert st == 200, h


def test_host_check_also_guards_the_page_that_carries_the_token(srv):
    # 这才是这道闸真正防的东西:token 在 '/' 的 HTML 里,所以 '/' 必须和 /api/ 一样受保护。
    st, body = call(srv, "GET", "/", host="attacker.test")
    assert st == 400
    assert TOKEN.encode() not in body


def test_star_is_a_separate_branch_not_a_hostname():
    # '*' 不能是名单里的一项,否则任何能写进名单的名字都可能把整道闸关掉。
    h = S.Handler
    saved = h.allowed_hosts
    try:
        h.allowed_hosts = {"*"}
        inst = object.__new__(h)
        inst.headers = {"Host": "evil.example.com"}
        assert h._host_ok(inst) is False
    finally:
        h.allowed_hosts = saved


def test_default_allowed_hosts_is_fail_closed():
    """忘了设置和明确允许一切必须是两件事。

    断言的是**源码里声明的默认值**而不是运行时的类属性:上面那个 fixture 会把类属性改掉,
    读运行时值的断言只能证明「某个测试设过它」,证明不了「没人设的时候是什么」。
    """
    src = open(os.path.join(HERE, "scripts", "task_console", "server.py"),
                  encoding="utf-8").read()
    assert "allowed_hosts: object = frozenset()" in src


# ---------- 维护端点(新):它自己的表,不蹭任务那张 ----------

def test_maint_read_without_token_is_403(srv):
    st, _ = call(srv, "GET", "/api/maint")
    assert st == 403


def test_maint_act_without_token_is_403(srv):
    st, _ = call(srv, "POST", "/api/maint/act",
                 body={"action": "skill.archive", "name": "x"})
    assert st == 403


def test_maint_act_rejects_a_host_it_does_not_know(srv):
    st, _ = call(srv, "POST", "/api/maint/act", host="evil.example.com", token=TOKEN,
                 body={"action": "skill.archive", "name": "x"})
    assert st == 400


def test_maint_act_refuses_an_action_outside_its_table(srv):
    st, body = call(srv, "POST", "/api/maint/act", token=TOKEN,
                    body={"action": "skill.delete", "name": "x"})
    assert st == 400
    assert b"bad_action" in body


def test_task_verbs_are_not_reachable_through_the_maint_endpoint(srv):
    # 两张表必须是两张表。任务动词从维护端点进来必须被拒,否则「分开」只是文档上的说法。
    st, body = call(srv, "POST", "/api/maint/act", token=TOKEN,
                    body={"action": "run", "name": "SomeTask"})
    assert st == 400
    assert b"bad_action" in body


def test_maint_verbs_are_not_reachable_through_the_task_endpoint(srv):
    # 反方向同理。
    st, body = call(srv, "POST", "/api/act", token=TOKEN,
                    body={"verb": "skill.archive", "name": "x"})
    assert st == 400
    assert b"verb" in body


def test_selfcheck_without_token_is_403(srv):
    st, _ = call(srv, "GET", "/api/selfcheck")
    assert st == 403


def test_selfcheck_with_token_answers(srv):
    # 正对照。自检端点自己要是 403 了,页面上那条会显示「自检本身失败」而不是假绿。
    st, body = call(srv, "GET", "/api/selfcheck", token=TOKEN)
    assert st == 200
    assert b'"rows"' in body and b'"probed"' in body


def test_repos_read_without_token_is_403(srv):
    st, _ = call(srv, "GET", "/api/repos")
    assert st == 403


def test_repo_push_is_not_an_action(srv):
    # 动作表里没有 push,从端点也进不去。
    st, body = call(srv, "POST", "/api/maint/act", token=TOKEN,
                    body={"action": "repo.push", "name": "x"})
    assert st == 400
    assert b"bad_action" in body


def test_sys_read_without_token_is_403(srv):
    st, _ = call(srv, "GET", "/api/sys")
    assert st == 403


def test_clean_action_cannot_be_pointed_at_a_path(srv, tmp_path, monkeypatch):
    """删除动作不收路径:喂一条路径进去,它只清自己那批,绝不去动别的地方。

    ⚠ 这条用例原来什么都没断言。它写的是 `assert st in (200, 400)`,再在 400 分支里
    `assert no_config in body or missing_src in body` —— 两条通路全接受,而且这台机器上
    根本没配 TASK_CONSOLE_PLUGIN_CACHE,于是它每次都走 no_config 那条,
    **删除逻辑一次都没有被执行过**。把 clean_temp_git 改成 `shutil.rmtree(name)` 它照样绿。
    一条名字里写着「不能被指到别的路径」的用例,从来没有验证过任何路径。

    现在把环境搭成确定的:缓存目录里放一个够旧、够格被删的暂存目录,**外面**放一个哨兵。
    正对照是「它真的删掉了自己那一个」(否则「什么都没删」也能让哨兵活下来),
    负对照是「哨兵还在」。
    """
    import os
    import time

    cache = tmp_path / "cache"
    cache.mkdir()
    victim = cache / "temp_git_123_abc"          # 名字形状与年龄都够格
    victim.mkdir()
    (victim / "f.txt").write_text("x", encoding="utf-8")
    old = time.time() - 10 * 3600
    os.utime(victim, (old, old))

    sentinel = tmp_path / "DO-NOT-TOUCH"          # 在缓存目录之外
    sentinel.mkdir()
    (sentinel / "keep.txt").write_text("keep", encoding="utf-8")

    monkeypatch.setenv("TASK_CONSOLE_PLUGIN_CACHE", str(cache))
    st, body = call(srv, "POST", "/api/maint/act", token=TOKEN,
                    body={"action": "clean.tempgit", "name": "../DO-NOT-TOUCH"})

    assert st == 200, body[:300]
    # 正对照:它确实动手了。少了这一条,一个什么都不做的实现也能通过下面那条。
    assert not victim.exists(), "自己那批没被清掉,这条用例根本没执行到删除逻辑"
    assert b'"removed": 1' in body.replace(b" ", b"") or b'"removed":1' in body.replace(b" ", b"")
    # 真正要钉的那条:name 里的路径没有被当成删除目标。
    assert sentinel.exists() and (sentinel / "keep.txt").exists(), \
        "name 里的相对路径被当成了删除目标"


def test_retire_plan_without_token_is_403(srv):
    st, _ = call(srv, "POST", "/api/retire/plan", body={"name": "X"})
    assert st == 403


def test_retire_without_a_reason_is_refused(srv):
    """退役必须带原因。

    ⚠ 原来断言的是 `no_reason 或 bad_name 或 no_config`。三选一意味着这条用例
    **分不出「因为没写原因被挡」和「因为任务名不合法被挡」** —— 把 no_reason 那道闸
    整个删掉,它照样绿(名字或配置那两条会接住)。一个能被三种不同原因满足的断言,
    钉不住其中任何一种。
    """
    st, body = call(srv, "POST", "/api/maint/act", token=TOKEN,
                    body={"action": "task.retire", "name": "SomeTask", "arg": ""})
    assert st == 400
    assert b"no_reason" in body, body[:300]


def test_retire_with_a_bad_name_is_refused_for_a_different_reason(srv):
    """负对照:换成不合法的名字,理由必须变成 bad_name。

    有了这一条,上面那条才是在钉「没写原因」而不是在钉「反正会被挡」。
    两条用例的输入只差一个字段,输出必须是两个不同的拒绝理由。
    """
    st, body = call(srv, "POST", "/api/maint/act", token=TOKEN,
                    body={"action": "task.retire", "name": "../evil", "arg": ""})
    assert st == 400
    assert b"bad_name" in body, body[:300]


def test_an_uninitialised_handler_refuses_everything(srv):
    """令牌没被设过时,鉴权必须拒绝,而不是放行。

    ⚠ `Handler.token` 以前的类默认值是 `""`,而 `_authed` 用的是
    `compare_digest(请求头 or "", self.token)` —— 于是一个**根本不带这个头**的请求
    会得到 compare_digest("", "") → True,直接过鉴权。它没被利用只是因为 Host 闸
    恰好先开火;也就是说令牌这道控制在「没初始化」状态下靠的是另一道控制兜底,
    而紧挨着它的 allowed_hosts 有四行注释专门解释自己为什么 fail-closed。
    """
    # ⚠ 这里曾经有一句 `assert not DEFAULT_TOKEN`。它对 "" 恒成立,而 "" 正是当年那个
    # 出问题的默认值 —— 一句在它要防的那个值上恒真的断言,正是这一轮在清理的那一类。
    # 真正能失败的判据只有行为:**把 Handler 退回出厂状态,它必须拒绝一切**。
    # 两层控制哪一层还在都能让它成立(None 哨兵、或 _authed 里的 `if not self.token`),
    # 而两层同时退回当年那个形态时它会红 —— 投毒验过。
    saved = S.Handler.token
    try:
        # 把 Handler 退回出厂状态,而不是退回一个我自己挑的哨兵。
        S.Handler.token = DEFAULT_TOKEN
        st, _ = call(srv, "GET", "/api/tasks")            # 不带令牌
        assert st == 403, "未初始化的 Handler 放行了一个不带令牌的请求"
        st, _ = call(srv, "GET", "/api/tasks", token="")   # 带一个空令牌
        assert st == 403, "未初始化的 Handler 放行了一个空令牌"
    finally:
        S.Handler.token = saved
    # 正对照:恢复之后正常的令牌仍然能用,证明上面拒的不是「服务器本来就坏了」。
    st, _ = call(srv, "GET", "/api/tasks", token=TOKEN)
    assert st == 200


def test_convos_read_without_token_is_403(srv):
    st, _ = call(srv, "GET", "/api/convos")
    assert st == 403


# ---------- vendor 静态资产 ----------
# vendor 下是随仓发的第三方资产(Tabler),刻意免令牌:<link> 和 <script> 标签发不了
# 自定义请求头,而这里面没有任何秘密。免令牌就意味着这条路径唯一的控制是「不许爬出
# vendor 目录」,所以它必须被单独测,并且要有一个证明它不是空拦的正对照。

def test_vendor_serves_a_real_asset(srv):
    st, body = call(srv, "GET", "/vendor/tabler/tabler.min.css")
    assert st == 200, st
    assert b"Tabler" in body[:400], body[:120]


def test_vendor_asset_needs_no_token(srv):
    # 正对照:证明上面那条 200 不是因为测试恰好带了令牌。
    st, _ = call(srv, "GET", "/vendor/tabler/tabler.min.css", token=None)
    assert st == 200, st


@pytest.mark.parametrize("path", [
    "/vendor/../server.py",
    "/vendor/tabler/../../server.py",
    "/vendor/%2e%2e/server.py",          # 百分号编码:前缀检查看不出来
    "/vendor/tabler/%2e%2e%2f%2e%2e%2fserver.py",
])
def test_vendor_refuses_to_climb_out(srv, path):
    st, _ = call(srv, "GET", path)
    assert st == 404, f"{path} 爬出去了: {st}"


def test_vendor_traversal_target_actually_exists(srv):
    # 负对照:上面那些 404 必须是被闸挡的,不能是「文件本来就不在」。
    # server.py 就在 vendor 的上两级,真的存在;直接确认一下,
    # 否则那四条断言全都可以在闸门被拆掉之后照样通过。
    import server as S2
    from pathlib import Path
    assert (Path(S2.VENDOR).parent / "server.py").is_file()


def test_vendor_missing_file_is_404(srv):
    st, _ = call(srv, "GET", "/vendor/tabler/nope.css")
    assert st == 404, st

# ---------- vendor:形状拒绝必须发生在碰文件系统之前 ----------
# 第一版这道闸只有「解析之后确认仍在 vendor 里」。它确实拦得住,文件一个字节都没泄。
# 但 "//host/share/x" 会让 Path.resolve() 在 Windows 上按 UNC 去**连那台主机**,
# 而那一步发生在归属检查之前:实测对一个不可路由地址(RFC 5737 的 192.0.2.1)
# 耗时 21.07 秒,对一台可达的攻击者主机则是一次自动 NTLM 协商。
# 免令牌 + 任意主机 + 每请求二十多秒,凑起来是一个不用登录的外联与阻塞原语,
# 而返回码从头到尾都是干净的 404。
#
# 所以下面这组用例分两种:一种断言仍然 404(结果对),
# 另一种断言**耗时**(证明它没有先去连网络)。只测前者的话,这个洞原封不动。

@pytest.mark.parametrize("path", [
    "/vendor///192.0.2.1/share/x",              # 浏览器不会折叠 path 里的连续斜杠
    "/vendor/%2F%2F192.0.2.1%2Fshare%2Fx",      # 百分号编码同形
    "/vendor/" + chr(92) + chr(92) + "192.0.2.1" + chr(92) + "share",   # 反斜杠 UNC
    "/vendor/C:/Windows/win.ini",               # 带盘符的绝对路径
    "/vendor/tabler/../../../server.py",        # 多级穿越
    "/vendor/./tabler/tabler.min.css",          # 单点段
    "/vendor/",                                 # 空 rel
])
def test_vendor_rejects_by_shape(srv, path):
    st, _ = call(srv, "GET", path)
    assert st == 404, f"{path} 没被挡: {st}"


def test_vendor_unc_is_refused_without_touching_the_network(srv):
    """UNC 那条必须**立刻**返回,不能先去连主机。

    这是本条唯一能分辨「修好了」和「没修但恰好也 404」的判据:两种实现的状态码一样,
    只有耗时不一样。192.0.2.1 是 RFC 5737 的 TEST-NET-1,保证不可路由,
    所以未修的实现会在这里卡二十秒以上。
    """
    # 刻意用一个**上面那组用例没碰过**的地址。第一版这里和 test_vendor_rejects_by_shape
    # 共用 192.0.2.1,于是形状用例先跑并阻塞了 21 秒之后,Windows 缓存了那次失败的解析,
    # 轮到这条时秒返,投毒状态下它照样打印绿色 : 一个只在自己是第一个碰那台主机时
    # 才有效的耗时判据,和没有判据差不多。
    t0 = time.monotonic()
    st, _ = call(srv, "GET", "/vendor///192.0.2.77/share/x")
    dt = time.monotonic() - t0
    assert st == 404, st
    assert dt < 2.0, f"返回是 404 但花了 {dt:.1f}s,说明形状检查跑在了 resolve() 之后"


def test_vendor_still_serves_the_real_asset(srv):
    """正对照:上面那一串 404 不能是因为把整条路由拒死了。"""
    st, body = call(srv, "GET", "/vendor/tabler/tabler.min.css")
    assert st == 200, st
    assert b"Tabler" in body[:400]


# ---------- 不许被 iframe 进去 ----------
# Host 白名单防的是 DNS rebinding。iframe 走另一条路:它发的 Host 就是 127.0.0.1:8787,
# 白名单原样放行,页面带着一枚有效令牌正常渲染。攻击者不需要读到任何东西,
# 只要骗一次点击落在他知道位置的按钮上,而这一页上的按钮会真删目录、真跑计划任务。

def _headers(port, path="/"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", path, headers={"Host": f"127.0.0.1:{port}"})
    r = c.getresponse()
    r.read()
    h = {k.lower(): v for k, v in r.getheaders()}
    c.close()
    return h


def test_the_page_refuses_to_be_framed(srv):
    h = _headers(srv, "/")
    assert "frame-ancestors 'none'" in h.get("content-security-policy", ""), h.get("content-security-policy")
    assert h.get("x-frame-options", "").upper() == "DENY", h.get("x-frame-options")


def test_every_response_carries_the_frame_ban_not_just_the_page(srv):
    """API 和静态资产也要带。只给 / 加,等于把「以后新增的路由」全漏掉。"""
    for p in ("/favicon.svg", "/vendor/tabler/tabler.min.css", "/api/selfcheck"):
        h = _headers(srv, p)
        assert "frame-ancestors 'none'" in h.get("content-security-policy", ""), p


# ---------- 「未检查」不能被压成「零」 ----------
# build_freshness 在没有健康清单时仍然返回一个 summary(total/bad 全是 0),
# 前端「有没有 summary」那个条件因此永远成立,于是概览最显眼的那块板子印出绿色的 0。
# 后端这一半的责任是:把 load_health 给出的**具体**原因带上去。
# 「清单 JSON 坏了」和「压根没设环境变量」是两件事,前者要修,后者是没启用;
# 压成同一句之后,页面上没有任何办法把它们分开。

def test_build_freshness_passes_the_specific_reason_through():
    got = S.build_freshness({}, {}, "健康监控清单解析失败(/x/y.json): Expecting value")
    assert got["summary"]["bad"] == 0
    assert "解析失败" in got["reason"], got["reason"]


def test_build_freshness_still_says_something_when_no_reason_is_given():
    """正对照:调用方没给原因时不能变成空字符串或 None,
    否则前端那句 `if(reason)` 会走回「有 summary 就当成功」的老路。"""
    got = S.build_freshness({}, {}, None)
    assert got["reason"], got["reason"]


def test_two_different_failures_do_not_produce_the_same_sentence():
    """这条才是这一组的重点:两种故障必须在页面上长得不一样。"""
    a = S.build_freshness({}, {}, "没有设 TASK_CONSOLE_HEALTH")["reason"]
    b = S.build_freshness({}, {}, "健康监控清单解析失败(/x/y.json)")["reason"]
    assert a != b


# ---------- 同一条规则只能有一份实现 ----------
# server.py 曾经 `from rcnorm import norm_rc as _norm_rc_unused`,然后在下面自己抄了一份
# 同名实现。渲染侧和写入侧从此是两份代码:当时逐位等价,但任何一侧改了边界条件另一侧不会
# 跟着变,而症状是**同一个退出码在两个面板上一个算成功一个算失败**,没有任何东西会报警。
# 别名里的 unused 还让读代码的人以为共享那份已经在用了。

def test_server_uses_the_shared_norm_rc_not_a_copy():
    import rcnorm
    assert S.norm_rc is rcnorm.norm_rc, (
        "server.py 又有自己的 norm_rc 了。同一条规则两份实现,漂了不报警。")


def test_norm_rc_still_unwraps_the_hresult_form():
    """正对照:上面那条 is 断言对着两个都坏掉的实现也会通过,
    所以这里钉住这条规则本身的行为(实测换来的那两条)。"""
    assert S.norm_rc(2147942402) == 2, "事件日志把 Win32 码包成 HRESULT,必须解包"
    assert S.norm_rc(-2147024891) == 5, "带符号 int32 的同一个值也要认"
    assert S.norm_rc(None) is None, "没有返回码就是 None,不是 0"
    assert S.norm_rc("") is None


# ---------- 导出字段必须显式两分 ----------
# 这层白名单已经吃掉过三次字段:reason(空库那条判定生效了,而页面上只有一个没有原因的
# available=False)、lastIngest(区分「摄入器挂了」和「本来就没跑过」的唯一信号)、
# countScope(那个数字属于哪个时间窗)。每一次的表现都一样:
# **后端算对了,页面上什么都没有,而没有任何东西报错。**
#
# 所以现在要求显式两分:导出的(*_OUT)加上刻意不导出的(*_DROP),
# 两边必须覆盖生产者写下的每一个键。忘了归类会让这条测试变红,
# 而不是让那个字段安静地到不了页面。

import inspect  # noqa: E402
import re as _re  # noqa: E402


def _producer_keys(varname):
    """从 server.py 源码里读出 `varname = {...}` 这个字面量写下的键。

    读源码而不是调 build_payload:后者要真跑 PowerShell。
    读源码的坏处是形状一变就失效,所以下面立刻断言读到的数量下限。
    """
    src = inspect.getsource(S)
    keys = set()
    for m in _re.finditer(r"^\s*" + varname + r"\s*=\s*\{", src, _re.M):
        i = src.index("{", m.start())
        depth, j = 0, i
        while j < len(src):
            if src[j] == "{":
                depth += 1
            elif src[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        keys |= set(_re.findall(r'"([a-zA-Z][a-zA-Z0-9_]*)"\s*:', src[i:j]))
    assert len(keys) >= 5, f"只从 {varname} 读到 {keys},源码形状可能变了"
    return keys


@pytest.mark.parametrize("var,out,drop", [
    ("hist", "HISTORY_OUT", "HISTORY_DROP"),
    ("runs", "RUNLOG_OUT", "RUNLOG_DROP"),
])
def test_every_produced_field_is_either_exported_or_deliberately_dropped(var, out, drop):
    produced = _producer_keys(var)
    classified = set(getattr(S, out)) | set(getattr(S, drop))
    missing = produced - classified
    assert not missing, (
        f"{var} 里这些键既没导出也没写进 {drop},它们会安静地到不了页面: {sorted(missing)}")


def test_the_reason_field_is_exported():
    """单独钉住 reason:它是三次事故里的第一次,也是最贵的一次。"""
    assert "reason" in S.HISTORY_OUT and "reason" in S.RUNLOG_OUT


def test_the_producer_reader_can_actually_fail():
    """负对照:上面几条全靠 _producer_keys 真的读到了东西。
    读一个不存在的变量必须炸,而不是返回空集合让断言恰好通过。"""
    with pytest.raises(AssertionError):
        _producer_keys("nosuchvariable")
