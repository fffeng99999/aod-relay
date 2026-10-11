#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自检：relay.py 全链路（对 fake hub / 鉴权 / 缓存 / 文本格式 / mock 模式）。

用法：python selftest_relay.py
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mock_hub  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
RELAY = os.path.join(HERE, "relay.py")
RELAY_TOKEN = "test-relay-token"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http_get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read().decode("utf-8", "replace")
    except OSError as err:
        return 0, {}, str(err)


def start_relay(args):
    proc = subprocess.Popen(
        [sys.executable, RELAY, "--host", "127.0.0.1"] + args,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return proc


def wait_ready(port, proc, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError("relay 提前退出: %s" % out)
        status, _h, _b = http_get("http://127.0.0.1:%d/aod/health" % port)
        if status == 200:
            return True
        time.sleep(0.2)
    raise RuntimeError("relay 启动超时")


def main():
    failures = 0
    procs = []

    # fake hub（模拟真实 workbuddy2api-hub）
    hub = ThreadingHTTPServer(("127.0.0.1", 0), mock_hub.Handler)
    threading.Thread(target=hub.serve_forever, daemon=True).start()
    hub_port = hub.server_address[1]

    # relay 实例 1：真实模式，指向 fake hub
    port1 = free_port()
    p1 = start_relay(["--port", str(port1),
                      "--hub-url", "http://127.0.0.1:%d" % hub_port,
                      "--panel-password", "test123",
                      "--relay-token", RELAY_TOKEN])
    procs.append(p1)
    wait_ready(port1, p1)

    base1 = "http://127.0.0.1:%d/aod/usage" % port1

    status, _h, body = http_get(base1, {"Authorization": "Bearer " + RELAY_TOKEN})
    payload = json.loads(body) if status == 200 else {}
    ok = status == 200 and payload.get("ok") and payload.get("source") == "hub" and payload.get("text")
    print("[1] 真实模式取数    %s  %s" % ("PASS" if ok else "FAIL", payload.get("text") or body))
    failures += 0 if ok else 1

    status, _h, body = http_get(base1, {"Authorization": "Bearer " + RELAY_TOKEN})
    payload2 = json.loads(body) if status == 200 else {}
    ok = status == 200 and payload2.get("cached") is True
    print("[2] 命中缓存        %s  cached=%s" % ("PASS" if ok else "FAIL", payload2.get("cached")))
    failures += 0 if ok else 1

    status, _h, body = http_get(base1, {"Authorization": "Bearer wrong-token"})
    ok = status == 401
    print("[3] 错误令牌拦截    %s  exit=%s" % ("PASS" if ok else "FAIL", status))
    failures += 0 if ok else 1

    status, headers, body = http_get(base1 + "?format=text&token=" + RELAY_TOKEN)
    ok = status == 200 and "tok｜" in body and headers.get("Content-Type", "").startswith("text/plain")
    print("[4] 文本格式输出    %s  %r" % ("PASS" if ok else "FAIL", body.strip()))
    failures += 0 if ok else 1

    # relay 实例 2：密码错误 -> 502 auth
    port2 = free_port()
    p2 = start_relay(["--port", str(port2),
                      "--hub-url", "http://127.0.0.1:%d" % hub_port,
                      "--panel-password", "wrong-password",
                      "--relay-token", RELAY_TOKEN])
    procs.append(p2)
    wait_ready(port2, p2)
    status, _h, body = http_get("http://127.0.0.1:%d/aod/usage" % port2,
                                {"Authorization": "Bearer " + RELAY_TOKEN})
    payload = json.loads(body) if body else {}
    ok = status == 502 and payload.get("kind") == "auth"
    print("[5] 上游密码错误    %s  status=%s kind=%s" % (
        "PASS" if ok else "FAIL", status, payload.get("kind")))
    failures += 0 if ok else 1

    # relay 实例 3：mock 模式
    port3 = free_port()
    p3 = start_relay(["--port", str(port3), "--mock", "--relay-token", RELAY_TOKEN])
    procs.append(p3)
    wait_ready(port3, p3)
    status, _h, body = http_get("http://127.0.0.1:%d/aod/usage" % port3,
                                {"Authorization": "Bearer " + RELAY_TOKEN})
    payload = json.loads(body) if body else {}
    ok = status == 200 and payload.get("source") == "mock" and payload.get("text")
    print("[6] mock 模式取数   %s  %s" % ("PASS" if ok else "FAIL", payload.get("text") or body))
    failures += 0 if ok else 1

    # fetch_usage.py 以 relay 模式跑通（PC 端全链路验证）
    fetch = os.path.join(HERE, "fetch_usage.py")
    proc = subprocess.run(
        [sys.executable, fetch, "--base-url", "http://127.0.0.1:%d" % port1,
         "--relay-token", RELAY_TOKEN],
        capture_output=True, text=True, timeout=30)
    ok = proc.returncode == 0 and "tok｜" in proc.stdout
    print("[7] fetch 走 relay   %s  %s" % (
        "PASS" if ok else "FAIL", proc.stdout.strip() or proc.stderr.strip()))
    failures += 0 if ok else 1

    # relay 实例 4：缺价（cost_missing）-> 金额位置显示 ¥❌
    mock_hub.FAIL_FIELDS[:] = ["cost_cny"]
    port4 = free_port()
    p4 = start_relay(["--port", str(port4),
                      "--hub-url", "http://127.0.0.1:%d" % hub_port,
                      "--panel-password", "test123",
                      "--relay-token", RELAY_TOKEN])
    procs.append(p4)
    wait_ready(port4, p4)
    status, _h, body = http_get("http://127.0.0.1:%d/aod/usage" % port4,
                                {"Authorization": "Bearer " + RELAY_TOKEN})
    payload = json.loads(body) if body else {}
    failed = payload.get("failed") or {}
    ok = (status == 200 and "¥❌" in (payload.get("text") or "")
          and "cost_cny" in failed)
    print("[8] 缺价->¥❌        %s  failed=%s" % (
        "PASS" if ok else "FAIL", failed.get("cost_cny")))
    failures += 0 if ok else 1

    # relay 实例 5：字段缺失 -> 对应位置显示 ❌
    mock_hub.FAIL_FIELDS[:] = ["errors"]
    port5 = free_port()
    p5 = start_relay(["--port", str(port5),
                      "--hub-url", "http://127.0.0.1:%d" % hub_port,
                      "--panel-password", "test123",
                      "--relay-token", RELAY_TOKEN])
    procs.append(p5)
    wait_ready(port5, p5)
    status, _h, body = http_get("http://127.0.0.1:%d/aod/usage" % port5,
                                {"Authorization": "Bearer " + RELAY_TOKEN})
    payload = json.loads(body) if body else {}
    failed = payload.get("failed") or {}
    ok = (status == 200 and "错误 ❌" in (payload.get("text") or "")
          and "errors" in failed)
    print("[9] 缺字段->❌       %s  %s" % (
        "PASS" if ok else "FAIL", (payload.get("text") or "").replace("\n", " | ")))
    failures += 0 if ok else 1
    mock_hub.FAIL_FIELDS[:] = []

    # relay 实例 6（mock）：lines 数组存在且与 text 一致（v0.3 协议）
    port6 = free_port()
    p6 = start_relay(["--port", str(port6), "--mock", "--relay-token", RELAY_TOKEN])
    procs.append(p6)
    wait_ready(port6, p6)
    status, _h, body = http_get("http://127.0.0.1:%d/aod/usage" % port6,
                                {"Authorization": "Bearer " + RELAY_TOKEN})
    payload = json.loads(body) if body else {}
    lines = payload.get("lines") or []
    ids = [l.get("id") for l in lines]
    joined = "\n".join(l.get("text", "") for l in lines)
    ok = (status == 200
          and "today" in ids and "requests" in ids
          and joined == (payload.get("text") or ""))
    print("[10] lines 协议      %s  ids=%s" % ("PASS" if ok else "FAIL", ids))
    failures += 0 if ok else 1

    for proc in procs:
        proc.terminate()
    for proc in procs:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    hub.shutdown()

    print("failures=%d" % failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
