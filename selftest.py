#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自检：内嵌启动 mock_hub 服务，以子进程跑 fetch_usage.py，验证全链路。

覆盖：正常取数 / JSON 输出 / 错密码（exit=2）/ 断网（exit=3）。
用法：python selftest.py
"""

import json
import os
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mock_hub  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FETCH = os.path.join(HERE, "fetch_usage.py")


def run_fetch(port, password, extra=None):
    cmd = [sys.executable, FETCH,
           "--base-url", "http://127.0.0.1:%d" % port,
           "--password", password]
    if extra:
        cmd += extra
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), mock_hub.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    failures = 0

    code, out, err = run_fetch(port, "test123")
    ok = code == 0 and "tok｜" in out
    print("[1] 正常取数      %s  %s" % ("PASS" if ok else "FAIL", out or err))
    failures += 0 if ok else 1

    code, out, err = run_fetch(port, "test123", ["--json"])
    payload = {}
    try:
        payload = json.loads(out)
        ok = code == 0 and payload.get("total_tokens", 0) > 0 and payload.get("text")
    except ValueError:
        ok = False
    print("[2] JSON 输出     %s  total=%s text=%s" % (
        "PASS" if ok else "FAIL", payload.get("total_tokens"), payload.get("text")))
    failures += 0 if ok else 1

    code, out, err = run_fetch(port, "wrong-password")
    ok = code == 2
    print("[3] 错密码拦截    %s  exit=%s %s" % ("PASS" if ok else "FAIL", code, err))
    failures += 0 if ok else 1

    code, out, err = run_fetch(port, "test123",
                               ["--base-url", "http://127.0.0.1:1"])
    ok = code == 3
    print("[4] 断网报错      %s  exit=%s %s" % ("PASS" if ok else "FAIL", code, err))
    failures += 0 if ok else 1

    # 304 分支：冻结数据档位后，同一客户端的第二次请求必须命中 ETag 缓存并复用对象。
    mock_hub.bucket_key = lambda: 12345
    sys.path.insert(0, HERE)
    import fetch_usage  # noqa: E402
    client = fetch_usage.HubClient("http://127.0.0.1:%d" % port, "test123")
    first = client.usage("today")
    second = client.usage("today")
    ok = second is first and second is not None
    print("[5] 304 复用缓存  %s  etag=%s" % ("PASS" if ok else "FAIL", client._etag))
    failures += 0 if ok else 1

    server.shutdown()
    print("failures=%d" % failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
