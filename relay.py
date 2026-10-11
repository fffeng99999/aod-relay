#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""aod-relay —— AOD 取数中转服务（单文件，零依赖）。

位置：部署在 hub 所在服务器（Docker 原样不动）。手机只面对本服务一条极简
只读接口；hub 的面板凭据与登录细节全部留在服务器本地。

对外（手机侧）:
  GET /aod/usage             -> 精简 JSON（默认）
  GET /aod/usage?format=text -> text/plain 一行短文本（便于手机浏览器/curl 直测）
  GET /aod/health            -> 存活探测（无鉴权）
  鉴权: Authorization: Bearer <relay_token> 或 ?token=<relay_token>
        （手机浏览器不便带自定义头，故两者都支持；正式 App 用 Bearer）

上游（workbuddy2api-hub）:
  POST /panel/login  {"password": ...} -> token（内存缓存；401 自动重登）
  GET  /usage?range=... 、Realm 过滤、ETag/If-None-Match -> 用量快照

字段级失败（v0.2 起）:
  某个数据在 hub 侧取不到（字段缺失 / 数值异常 / 有模型缺少价格）时，该字段名进
  JSON 的 "failed" 字典（值=原因），文本输出与 AOD 对应位置显示 ❌；
  模型缺价默认算「金额读取失败」（显示 ¥❌），--cost-ignore-missing 可关掉这条。

占位凭据（部署时替换，或用环境变量注入）:
  AOD_HUB_URL=http://127.0.0.1:8788
  WB_PANEL_PASSWORD=__REPLACE_ME__   （hub 面板密码；默认 admin，务必已改）
  AOD_RELAY_TOKEN=__REPLACE_ME__     （手机侧令牌，自定义长随机串）

用法:
  python relay.py --mock                                  # 联调：内置 mock 数据
  python relay.py --mock --mock-fail cost_cny,errors      # 联调：模拟字段读取失败（AOD 显示 ❌）
  python relay.py --panel-password '<面板密码>' --relay-token '<令牌>'
  python relay.py --host 0.0.0.0 --port 8790              # 局域网给手机用

容器部署见同目录 Dockerfile / docker-compose.yml（中转在服务器上，向 hub 走本地链接）。
"""

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime

PLACEHOLDER = "__REPLACE_ME__"
RELAY_VERSION = "0.3.1"
DEFAULT_PORT = 8790
FAIL_MARK = "❌"
CORE_NUMERIC_FIELDS = ("total_tokens", "prompt_tokens", "completion_tokens",
                       "cached_tokens", "requests", "errors")


def _status_html(state):
    """浏览器直接访问 /aod/（无鉴权）时的状态页：只暴露版本与运行信息，不含用量数据。"""
    mock = bool(state.mock)
    mock_fail = ",".join(state.mock_fail)
    cost_ignore = bool(state.cost_ignore_missing)
    lines_proto = "v0.3（lines=[{id,text},...]）"
    return (
        "<!doctype html><html lang=zh><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>aod-relay " + RELAY_VERSION + "</title>"
        "<style>body{font-family:system-ui,'Segoe UI',sans-serif;background:#0d1117;color:#e6edf3;"
        "margin:0;display:flex;align-items:center;justify-content:center;min-height:100vh}"
        ".card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:28px 36px;"
        "min-width:320px;box-shadow:0 4px 16px rgba(0,0,0,.4)}"
        "h1{margin:0 0 4px;font-size:22px;color:#58a6ff}"
        "h1 small{font-size:14px;color:#8b949e;font-weight:400}"
        "table{margin-top:14px;border-collapse:collapse;font-size:14px}"
        "td{padding:5px 14px 5px 0;color:#8b949e}"
        "td.v{color:#e6edf3;font-family:ui-monospace,Consolas,monospace}"
        ".ok{color:#3fb950}.warn{color:#d29922}"
        "</style></head><body><div class=card>"
        "<h1>aod-relay <small>" + ("（mock 模式）" if mock else "") + "</small></h1>"
        "<table>"
        "<tr><td>版本</td><td class=v>" + RELAY_VERSION + "</td></tr>"
        "<tr><td>lines 协议</td><td class=v>" + lines_proto + "</td></tr>"
        "<tr><td>启动时间</td><td class=v>" + state.started_at + "</td></tr>"
        "<tr><td>监听端口</td><td class=v>" + str(state.port) + "</td></tr>"
        "<tr><td>hub 地址</td><td class=v>" + (state.hub_url if not mock else "—（mock）") + "</td></tr>"
        "<tr><td>mock</td><td class=v>"
        + ("<span class=warn>开启" + ("，fail=" + mock_fail if mock_fail else "") + "</span>" if mock else "关闭")
        + "</td></tr>"
        "<tr><td>缺价容错</td><td class=v>"
        + ("<span class=ok>开启</span>" if cost_ignore else "关闭")
        + "</td></tr>"
        "</table>"
        "<p style='margin-top:16px;font-size:12px;color:#8b949e'>"
        "接口：<code>GET /aod/usage?range=today|week|month</code>（Bearer 令牌）· "
        "<code>GET /aod/health</code>（无鉴权）</p>"
        "</div></body></html>"
    )


class HubError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind  # "auth" | "net" | "data"


def _to_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


class HubClient(object):
    """hub 的普通客户端：登录换 token、拉 /usage，其余细节全在这层消化。"""

    def __init__(self, base_url, password, timeout=10):
        self.base = base_url.rstrip("/")
        self.password = password
        self.timeout = timeout
        self.token = ""
        self._etag = ""

    def _request(self, method, path, body=None, headers=None):
        url = self.base + path
        data = None
        hdrs = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as err:
            return err.code, dict(err.headers), err.read()
        except OSError as err:
            raise HubError("net", "无法连接 hub %s: %s" % (url, err))

    @staticmethod
    def _json_body(status, raw):
        try:
            return json.loads(raw.decode("utf-8", "replace") or "{}")
        except ValueError:
            raise HubError("data", "hub 返回不是 JSON（HTTP %s）" % status)

    def login(self):
        status, _hdrs, raw = self._request(
            "POST", "/panel/login", body={"password": self.password})
        payload = self._json_body(status, raw)
        if status == 429:
            raise HubError("auth", "面板登录被限流（>5 次/分钟），稍后重试")
        if status != 200 or not payload.get("token"):
            detail = payload.get("error", {}).get("message") if isinstance(
                payload.get("error"), dict) else payload.get("error")
            raise HubError("auth", "面板登录失败（HTTP %s）: %s" % (status, detail or "密码错误？"))
        self.token = payload["token"]

    def usage(self, range_key="today", realm=""):
        """取用量快照；304 时返回 None（调用方复用旧数据）。"""
        if not self.token:
            self.login()
        query = "?range=" + range_key
        if realm:
            query += "&realm=" + realm
        headers = {"X-Panel-Token": self.token}
        if self._etag:
            headers["If-None-Match"] = self._etag
        status, resp_headers, raw = self._request("GET", "/usage" + query, headers=headers)
        if status == 304:
            return None
        if status == 401:
            self.token = ""
            self.login()
            return self.usage(range_key, realm)
        if status != 200:
            payload = self._json_body(status, raw)
            raise HubError("data", "取用量失败（HTTP %s）: %s" % (status, payload))
        snapshot = self._json_body(status, raw)
        self._etag = resp_headers.get("ETag") or resp_headers.get("etag") or ""
        return snapshot


def fmt_tokens(n):
    n = _to_int(n)
    if n < 10000:
        return str(n)
    if n < 100000000:
        value = n / 10000.0
        return ("%.1f" % value).rstrip("0").rstrip(".") + "万"
    value = n / 100000000.0
    return ("%.2f" % value).rstrip("0").rstrip(".") + "亿"


def fmt_money(cny):
    try:
        return "¥%.2f" % float(cny)
    except (TypeError, ValueError):
        return "¥?"


RANGE_LABEL = {
    "today": "今日", "day": "今日", "1d": "今日",
    "week": "本周", "w": "本周",
    "month": "本月", "m": "本月",
    "all": "累计",
}


def _numeric_field(snapshot, name, failed):
    """取一个「数字型」字段；缺失 / null / 非数字 -> 记入 failed 并返回 None。"""
    if name not in snapshot or snapshot.get(name) is None:
        failed[name] = "字段缺失"
        return None
    raw = snapshot.get(name)
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        failed[name] = "数值异常: %r" % (raw,)
        return None


def _money_field(snapshot, name, failed, missing_models):
    """取「金额型」字段；missing_models > 0 表示有模型没取到价格，一并算读取失败。"""
    if name not in snapshot or snapshot.get(name) is None:
        failed[name] = "字段缺失"
        return None
    raw = snapshot.get(name)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        failed[name] = "数值异常: %r" % (raw,)
        return None
    if missing_models:
        failed[name] = "%d 个模型缺少价格数据" % missing_models
        return None
    return value


def collect_fields(snapshot, cost_ignore_missing=False):
    """把 hub 快照解析成 (values, failed, missing_models)。

    某个数据在 hub 侧取不到就进 failed —— 手机端对应位置显示 ❌，而不是把
    0 / 缺字段静默当成真实数字。周 / 月聚合仅当快照里带了才解析（真实 hub
    按 range 返回单窗口数据，不带这两个字段，不算失败）。
    """
    failed = {}
    values = {}
    missing = snapshot.get("cost_missing")
    missing_models = len(missing) if isinstance(missing, dict) else 0
    if cost_ignore_missing:
        missing_models = 0
    for name in CORE_NUMERIC_FIELDS:
        values[name] = _numeric_field(snapshot, name, failed)
    values["cost_cny"] = _money_field(snapshot, "cost_cny", failed, missing_models)
    for name in ("week_total_tokens", "month_total_tokens"):
        if name in snapshot:
            values[name] = _numeric_field(snapshot, name, failed)
    for name in ("week_cost_cny", "month_cost_cny"):
        if name in snapshot:
            values[name] = _money_field(snapshot, name, failed, missing_models)
    return values, failed, missing_models


def _disp_tokens(values, failed, name):
    return FAIL_MARK if name in failed else fmt_tokens(values.get(name))


def _disp_int(values, failed, name):
    return FAIL_MARK if name in failed else str(_to_int(values.get(name)))


def _disp_money(values, failed, name):
    return "¥" + FAIL_MARK if name in failed else fmt_money(values.get(name))


def build_lines(values, failed, range_key):
    label = RANGE_LABEL.get(str(range_key or "").strip().lower(), "用量")
    lines = [{"id": "today", "text": "%s %s tok｜%s" % (
        label, _disp_tokens(values, failed, "total_tokens"),
        _disp_money(values, failed, "cost_cny"))}]
    # 本周 / 本月：快照里带了才输出（mock 会带）；读取失败时也送出 ❌ 行
    for key, name in (("week", "本周"), ("month", "本月")):
        total_name = key + "_total_tokens"
        cost_name = key + "_cost_cny"
        if total_name not in values and cost_name not in values:
            continue
        total = values.get(total_name) or 0
        if total > 0 or total_name in failed or cost_name in failed:
            lines.append({"id": key, "text": "%s %s tok｜%s" % (
                name, _disp_tokens(values, failed, total_name),
                _disp_money(values, failed, cost_name))})
    lines.append({"id": "requests", "text": "请求 %s｜错误 %s" % (
        _disp_int(values, failed, "requests"), _disp_int(values, failed, "errors"))})
    prompt = values.get("prompt_tokens") or 0
    completion = values.get("completion_tokens") or 0
    if (prompt > 0 or completion > 0
            or "prompt_tokens" in failed or "completion_tokens" in failed):
        lines.append({"id": "io", "text": "输入 %s｜输出 %s" % (
            _disp_tokens(values, failed, "prompt_tokens"),
            _disp_tokens(values, failed, "completion_tokens"))})
    cached = values.get("cached_tokens") or 0
    if cached > 0 or "cached_tokens" in failed:
        lines.append({"id": "cache", "text": "缓存命中 %s"
                      % _disp_tokens(values, failed, "cached_tokens")})
    return lines


def build_text(values, failed, range_key):
    return "\n".join(str(line["text"]) for line in build_lines(values, failed, range_key))


def build_payload(snapshot, range_key, source, cached=False, stale=False,
                  cost_ignore_missing=False):
    values, failed, missing_models = collect_fields(snapshot, cost_ignore_missing)
    return {
        "ok": True,
        "service": "aod-relay",
        "version": RELAY_VERSION,
        "source": source,
        "cached": cached,
        "stale": stale,
        "range": range_key,
        "text": build_text(values, failed, range_key),
        "lines": build_lines(values, failed, range_key),
        "total_tokens": values.get("total_tokens"),
        "prompt_tokens": values.get("prompt_tokens"),
        "completion_tokens": values.get("completion_tokens"),
        "requests": values.get("requests"),
        "errors": values.get("errors"),
        "cost_cny": values.get("cost_cny"),
        "cached_tokens": values.get("cached_tokens"),
        "week_total_tokens": values.get("week_total_tokens"),
        "week_cost_cny": values.get("week_cost_cny"),
        "month_total_tokens": values.get("month_total_tokens"),
        "month_cost_cny": values.get("month_cost_cny"),
        "failed": failed,
        "cost_missing_models": missing_models,
        "realm": snapshot.get("realm", ""),
        "since": snapshot.get("since", ""),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def mock_snapshot():
    """内置 mock：30 秒一档，节律与 mock_hub.py 一致，便于观察数据变化。"""
    bucket = int(time.time() // 30)
    total = 128000 + (bucket % 97) * 5000
    week = int(total * 5.4) + 310000
    month = int(total * 21.7) + 1250000
    return {
        "total_tokens": total,
        "prompt_tokens": int(total * 0.72),
        "completion_tokens": int(total * 0.28),
        "cached_tokens": int(total * 0.11),
        "requests": 87 + bucket % 13,
        "errors": bucket % 3,
        "cost_cny": round(total / 1000000.0 * 3.2, 2),
        "week_total_tokens": week,
        "week_cost_cny": round(week / 1000000.0 * 3.2, 2),
        "month_total_tokens": month,
        "month_cost_cny": round(month / 1000000.0 * 3.2, 2),
        "realm": "mock",
        "since": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def apply_mock_failures(snapshot, names):
    """mock 模式模拟 hub 侧字段读取失败：cost_cny 用 cost_missing 表达（数据在但价缺），
    其余字段直接抹掉键（数据缺）。"""
    for name in names:
        name = name.strip()
        if not name:
            continue
        if name == "cost_cny":
            snapshot["cost_missing"] = {"mock-model": 3}
            continue
        snapshot.pop(name, None)


class RelayState(object):
    """缓存放这里：手机分钟级轮询命中缓存就不打 hub；hub 挂了还能发旧数据兜底。"""

    def __init__(self, hub, mock=False, cache_ttl=30, stale_max=600, timeout=10,
                 cost_ignore_missing=False, mock_fail=()):
        self.hub = hub
        self.hub_url = hub.base if hub is not None else ""
        self.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.port = None
        self.mock = mock
        self.cache_ttl = cache_ttl
        self.stale_max = stale_max
        self.timeout = timeout
        self.cost_ignore_missing = cost_ignore_missing
        self.mock_fail = tuple(mock_fail)
        self.lock = threading.Lock()
        self._cache = None      # (payload, built_at)
        self._last_good = None  # (snapshot, range_key, fetched_at)

    def get_payload(self, range_key, realm):
        now = time.time()
        if self.mock:
            snapshot = mock_snapshot()
            apply_mock_failures(snapshot, self.mock_fail)
            return build_payload(snapshot, range_key, "mock",
                                 cost_ignore_missing=self.cost_ignore_missing)
        with self.lock:
            if self._cache is not None:
                payload, built_at = self._cache
                if now - built_at < self.cache_ttl and payload.get("range") == range_key:
                    payload = dict(payload)
                    payload["cached"] = True
                    return payload
            try:
                snapshot = None
                if self.hub is not None:
                    snapshot = self.hub.usage(range_key, realm)
                if snapshot is None:
                    # 304：数据未变，复用上次快照
                    if self._last_good is None:
                        raise HubError("data", "hub 返回 304 但本服务尚无缓存快照")
                    snapshot = self._last_good[0]
                payload = build_payload(snapshot, range_key, "hub",
                                        cost_ignore_missing=self.cost_ignore_missing)
                self._last_good = (snapshot, range_key, now)
                self._cache = (payload, now)
                return payload
            except HubError as err:
                if self._last_good is not None:
                    snapshot, last_range, fetched_at = self._last_good
                    if now - fetched_at <= self.stale_max:
                        payload = build_payload(
                            snapshot, range_key, "hub", cached=True, stale=True,
                            cost_ignore_missing=self.cost_ignore_missing)
                        payload["stale_reason"] = str(err)
                        return payload
                raise


def create_handler(state, relay_token, handler_cls=BaseHTTPRequestHandler):
    class Handler(handler_cls):
        server_version = "aod-relay/" + RELAY_VERSION

        def log_message(self, fmt, *args):
            sys.stderr.write("[relay] %s %s\n" % (self.address_string(), fmt % args))

        def _json(self, code, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _text(self, code, text):
            body = (text + "\n").encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _token_ok(self, query):
            header = self.headers.get("Authorization") or ""
            if header.startswith("Bearer "):
                return header[7:].strip() == relay_token
            supplied = (query.get("token") or [""])[0]
            return supplied == relay_token

        def do_GET(self):
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)

            if path in ("/", "/aod", "/aod/"):
                html = _status_html(state).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(html)
                return

            if path == "/aod/health":
                return self._json(200, {
                    "ok": True, "service": "aod-relay",
                    "version": RELAY_VERSION, "mock": state.mock,
                    "mock_fail": list(state.mock_fail),
                    "cost_ignore_missing": state.cost_ignore_missing})

            if path != "/aod/usage":
                return self._json(404, {"ok": False, "error": "not found"})
            if not self._token_ok(query):
                return self._json(401, {"ok": False, "error": "invalid relay token"})

            range_key = (query.get("range") or ["today"])[0].strip().lower() or "today"
            if range_key not in ("today", "week", "month", "all"):
                return self._json(400, {"ok": False, "error": "bad range"})
            realm = (query.get("realm") or [""])[0].strip().lower()
            if realm not in ("", "intl", "cn", "all"):
                return self._json(400, {"ok": False, "error": "bad realm"})
            want_text = (query.get("format") or [""])[0].strip().lower() == "text"

            try:
                payload = state.get_payload(range_key, realm)
            except HubError as err:
                return self._json(502, {
                    "ok": False, "service": "aod-relay",
                    "error": str(err), "kind": err.kind})
            if want_text:
                return self._text(200, payload["text"])
            return self._json(200, payload)

    return Handler


def parse_args(argv):
    parser = argparse.ArgumentParser(description="AOD 取数中转服务")
    parser.add_argument("--host", default=os.environ.get("AOD_RELAY_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("AOD_RELAY_PORT", DEFAULT_PORT)))
    parser.add_argument("--hub-url", default=os.environ.get("AOD_HUB_URL", "http://127.0.0.1:8788"))
    parser.add_argument("--panel-password", default=os.environ.get("WB_PANEL_PASSWORD", PLACEHOLDER))
    parser.add_argument("--relay-token", default=os.environ.get("AOD_RELAY_TOKEN", PLACEHOLDER))
    parser.add_argument("--mock", action="store_true", help="内置 mock 数据，不连 hub")
    parser.add_argument("--mock-fail", default=os.environ.get("AOD_MOCK_FAIL", ""),
                        help="mock 模式下模拟读取失败的字段（逗号分隔，如 cost_cny,errors）")
    parser.add_argument("--cost-ignore-missing", action="store_true",
                        default=str(os.environ.get("AOD_COST_IGNORE_MISSING", "")).lower()
                        in ("1", "true", "yes", "on"),
                        help="有模型缺价时不把金额算作读取失败（默认算失败，显示 ¥❌）")
    parser.add_argument("--cache-ttl", type=float, default=30.0)
    parser.add_argument("--stale-max", type=float, default=600.0)
    parser.add_argument("--timeout", type=float, default=10.0)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    relay_token = args.relay_token
    if relay_token == PLACEHOLDER and args.mock:
        import secrets
        relay_token = secrets.token_urlsafe(12)
        print("[relay] mock 模式未提供 token，临时生成: %s" % relay_token)
    if not args.mock:
        if args.panel_password in ("", PLACEHOLDER):
            print("[relay] 拒绝启动：请用 --panel-password 或 WB_PANEL_PASSWORD 填入 hub 面板密码"
                  "（占位值 %s 不能用于真实模式）" % PLACEHOLDER, file=sys.stderr)
            return 2
        if relay_token in ("", PLACEHOLDER):
            print("[relay] 拒绝启动：请用 --relay-token 或 AOD_RELAY_TOKEN 设置手机侧令牌",
                  file=sys.stderr)
            return 2

    hub = None if args.mock else HubClient(args.hub_url, args.panel_password, timeout=args.timeout)
    state = RelayState(hub, mock=args.mock, cache_ttl=args.cache_ttl,
                       stale_max=args.stale_max, timeout=args.timeout,
                       cost_ignore_missing=args.cost_ignore_missing,
                       mock_fail=[part for part in args.mock_fail.split(",") if part.strip()])
    handler = create_handler(state, relay_token)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    state.port = args.port
    print("[relay] listening on http://%s:%d  mode=%s  hub=%s" % (
        args.host, args.port, "mock" if args.mock else "hub", args.hub_url))
    if args.mock and state.mock_fail:
        print("[relay] mock 模拟读取失败字段: %s" % ", ".join(state.mock_fail))
    if state.cost_ignore_missing:
        print("[relay] 已开启 --cost-ignore-missing：模型缺价不再算金额读取失败")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
