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
from http.server import ThreadingHTTPServer

import pytest

HERE = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(HERE, "scripts", "task_console"))

if os.name != "nt":
    pytest.skip("task_console 只在 Windows 上有意义", allow_module_level=True)

import server as S  # noqa: E402

TOKEN = "test-token-not-a-real-one"


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
