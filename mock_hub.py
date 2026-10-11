#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""workbuddy2api-hub 的本地 mock：只实现 AOD 链路用到的两条接口。

  POST /panel/login   {"password": ...}        -> {"ok": true, "token": ...}
  GET  /usage?range=  X-Panel-Token / ETag     -> 用量快照（30 秒一档，便于观察 304）

用途：在没有真实 hub 的环境里联调 fetch_usage.py、以及后续广播桥。
用法：python mock_hub.py [--port 8799] [--password test123]
"""

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PASSWORD = "test123"
TOKEN = "mock-token-abc"

# selftest / --fail-fields 可注入「字段读取失败」，让 relay 的 ❌ 路径可离线联调。
FAIL_FIELDS = []


def snapshot():
    """30 秒一档的假数据：同一档内 ETag 不变，命中 If-None-Match 回 304。"""
    bucket = bucket_key()
    total = 128000 + (bucket % 97) * 5000
    requests = 87 + bucket % 13
    data = {
        "requests": requests,
        "errors": bucket % 3,
        "prompt_tokens": int(total * 0.72),
        "completion_tokens": int(total * 0.28),
        "reasoning_tokens": int(total * 0.05),
        "cached_tokens": int(total * 0.11),
        "total_tokens": total,
        "credit": 0.0,
        "cost_cny": round(total / 1000000.0 * 3.2, 2),
        "cost_missing": {},
        "started": time.time(),
        "since": time.strftime("%Y-%m-%d %H:%M:%S"),
        "log_file": "usage/usage.jsonl",
        "realm": "intl",
        "usd_cny": 7.2,
        "accounts_map": {},
        "account": {"accounts": 2, "accounts_ready": 2},
        "by_model": {},
    }
    return _apply_fail_fields(data)


def _apply_fail_fields(data):
    """cost_cny 用 cost_missing 表达（数值在但价缺），其余字段直接删键（数据缺）。"""
    for name in list(FAIL_FIELDS):
        name = str(name or "").strip()
        if not name:
            continue
        if name == "cost_cny":
            data["cost_missing"] = {"mock-model": 2}
            continue
        data.pop(name, None)
    return data


def bucket_key():
    """数据档位；selftest 可将其改写成固定值以稳定测得 304 分支。"""
    return int(time.time() // 30)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[mock] %s %s" % (self.address_string(), fmt % args))

    def _json(self, code, payload, etag=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if etag:
            self.send_header("ETag", etag)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path.split("?")[0] != "/panel/login":
            return self._json(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except ValueError:
            payload = {}
        if str(payload.get("password") or "") != PASSWORD:
            return self._json(401, {"error": {"message": "invalid panel password"}})
        self._json(200, {"ok": True, "token": TOKEN, "using_default_password": False})

    def do_GET(self):
        if self.path.split("?")[0] not in ("/usage", "/v1/usage"):
            return self._json(404, {"error": "not found"})
        if (self.headers.get("X-Panel-Token") or "").strip() != TOKEN:
            return self._json(401, {"error": {"message": "panel password required"}})
        etag = '"mock-%d"' % bucket_key()
        if (self.headers.get("If-None-Match") or "") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        self._json(200, snapshot(), etag=etag)


def main():
    parser = argparse.ArgumentParser(description="workbuddy2api-hub 本地 mock")
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--fail-fields", default="",
                        help="模拟字段读取失败（逗号分隔，如 cost_cny,errors）")
    args = parser.parse_args()
    global FAIL_FIELDS
    FAIL_FIELDS[:] = [part.strip() for part in args.fail_fields.split(",") if part.strip()]
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print("[mock] listening on http://%s:%d  (password=%s)" % (args.host, args.port, PASSWORD))
    if FAIL_FIELDS:
        print("[mock] 模拟读取失败字段: %s" % ", ".join(FAIL_FIELDS))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
