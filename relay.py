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
  GET  /usage/analytics?realm=all&range=today|week|month -> 面板使用的日历统计

字段级失败（v0.2 起）:
  某个数据在 hub 侧取不到（字段缺失 / 数值异常）时，该字段名进
  JSON 的 "failed" 字典（值=原因），文本输出与 AOD 对应位置显示 ❌；
  模型缺价不会抹掉 hub 返回的已定价模型等价花费合计。

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
from concurrent.futures import ThreadPoolExecutor, as_completed
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
RELAY_VERSION = "0.4.1"
DEFAULT_PORT = 8790
FAIL_MARK = "❌"
USAGE_RANGES = ("today", "week", "month")
CORE_NUMERIC_FIELDS = ("total_tokens", "prompt_tokens", "completion_tokens",
                       "cached_tokens", "requests", "errors")


def _status_html(state):
    """浏览器直接访问 /aod/（无鉴权）时的状态页：只暴露版本与运行信息，不含用量数据。"""
    mock = bool(state.mock)
    mock_fail = ",".join(state.mock_fail)
    cost_ignore = bool(state.cost_ignore_missing)
    lines_proto = "v0.4 面板统计分窗与双币价格"
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
        "<tr><td>mock</td><td class=v>"
        + ("<span class=warn>开启" + ("，fail=" + mock_fail if mock_fail else "") + "</span>" if mock else "关闭")
        + "</td></tr>"
        "<tr><td>缺价容错</td><td class=v>"
        + ("<span class=ok>开启</span>" if cost_ignore else "关闭")
        + "</td></tr>"
        "</table></div></body></html>"
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
        self._etags = {}
        self._auth_lock = threading.Lock()

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

    def accounts(self, realm=None):
        """读 /accounts（面板同源账号列表：enabled/冷却/积分/realm）。"""
        from urllib.parse import urlencode
        with self._auth_lock:
            if not self.token:
                self.login()
            used_token = self.token
        query = "?" + urlencode({"realm": realm}) if realm else ""
        status, _hdrs, raw = self._request(
            "GET", "/accounts" + query, headers={"X-Panel-Token": used_token})
        if status == 401:
            with self._auth_lock:
                if self.token == used_token:
                    self.token = ""
                    self.login()
            return self.accounts(realm)
        if status != 200:
            payload = self._json_body(status, raw)
            raise HubError("data", "取账号列表失败（HTTP %s）: %s" % (status, payload))
        payload = self._json_body(status, raw)
        accounts = payload.get("accounts") if isinstance(payload, dict) else None
        if not isinstance(accounts, list):
            raise HubError("data", "hub /accounts 缺少 accounts 列表")
        return accounts

    def usage(self, range_key="today", realm="all", retry_auth=True):
        """读取 dashboard 使用的 /usage/analytics 日历窗口。"""
        from urllib.parse import urlencode
        with self._auth_lock:
            if not self.token:
                self.login()
            used_token = self.token
        cache_key = (range_key, realm)
        query = "?" + urlencode({"range": range_key, "realm": realm})
        headers = {"X-Panel-Token": used_token}
        if self._etags.get(cache_key):
            headers["If-None-Match"] = self._etags[cache_key]
        status, resp_headers, raw = self._request("GET", "/usage/analytics" + query, headers=headers)
        if status == 304:
            return None
        if status == 401:
            with self._auth_lock:
                if self.token == used_token:
                    self.token = ""
                    self.login()
            if retry_auth:
                return self.usage(range_key, realm, retry_auth=False)
            raise HubError("auth", "hub 面板会话刷新后仍未授权")
        if status != 200:
            payload = self._json_body(status, raw)
            raise HubError("data", "取用量失败（HTTP %s）: %s" % (status, payload))
        payload = self._json_body(status, raw)
        summary = payload.get("summary") if isinstance(payload, dict) else None
        snapshot = summary.get("window") if isinstance(summary, dict) else None
        if not isinstance(snapshot, dict):
            raise HubError("data", "hub /usage/analytics 缺少 summary.window")
        snapshot = dict(snapshot)
        snapshot["usd_cny"] = payload.get("usd_cny")
        snapshot["realm"] = payload.get("realm") or realm
        if payload.get("pricing_enabled") is False:
            snapshot["cost_cny"] = None
        self._etags[cache_key] = resp_headers.get("ETag") or resp_headers.get("etag") or ""
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
    """解析 dashboard 窗口统计中的金额合计。"""
    if name not in snapshot or snapshot.get(name) is None:
        failed[name] = "字段缺失"
        return None
    raw = snapshot.get(name)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        failed[name] = "数值异常: %r" % (raw,)
        return None
    return value


def collect_fields(snapshot, cost_ignore_missing=False):
    """把 hub 快照解析成 (values, failed, missing_models)。

    每个窗口前缀（today/week/month）独立解析；坏一个窗口不隐藏另外两个窗口。
    """
    failed = {}
    values = {}
    missing = snapshot.get("cost_missing")
    missing_models = len(missing) if isinstance(missing, dict) else 0
    if cost_ignore_missing:
        missing_models = 0
    for name in CORE_NUMERIC_FIELDS:
        values[name] = _numeric_field(snapshot, name, failed)
    period_fields = ("total_tokens", "prompt_tokens", "completion_tokens",
                     "cached_tokens", "requests", "errors")
    for period in ("week", "month"):
        for field in period_fields:
            name = period + "_" + field
            values[name] = _numeric_field(snapshot, name, failed)
    # 与面板行为一致：cost_missing 描述部分模型无价，但合计展示已定价模型的累加值。
    # cost_missing is per-model detail; the panel total is already the sum for priced models.
    values["cost_cny"] = _money_field(snapshot, "cost_cny", failed, 0)
    for period in ("week", "month"):
        name = period + "_cost_cny"
        values[name] = _money_field(snapshot, name, failed, 0)
    for name in ("usd_cny", "week_usd_cny", "month_usd_cny"):
        raw = snapshot.get(name) or snapshot.get("usd_cny")
        try:
            rate = float(raw)
            if rate <= 0:
                raise ValueError()
            values[name] = rate
        except (TypeError, ValueError):
            failed[name] = "美元汇率缺失或无效"
            values[name] = None
    return values, failed, missing_models


def _disp_tokens(values, failed, name):
    return FAIL_MARK if name in failed else fmt_tokens(values.get(name))


def _disp_int(values, failed, name):
    return FAIL_MARK if name in failed else str(_to_int(values.get(name)))


def _disp_money(values, failed, name, rate_name="usd_cny"):
    if name in failed:
        return "￥" + FAIL_MARK + " / $" + FAIL_MARK
    try:
        cny = float(values[name])
    except (KeyError, TypeError, ValueError):
        return "￥" + FAIL_MARK + " / $" + FAIL_MARK
    rate = values.get(rate_name) or values.get("usd_cny")
    if values.get(rate_name) is None:
        return "￥%.2f / $%s" % (cny, FAIL_MARK)
    try:
        rate = float(rate)
        if rate <= 0:
            raise ValueError()
        return "￥%.2f / $%.2f" % (cny, cny / rate)
    except (TypeError, ValueError):
        return "￥%.2f / $%s" % (cny, FAIL_MARK)


def build_lines(values, failed, range_key):
    lines = [{"id": "today", "text": "今日 %s tok｜%s" % (
        _disp_tokens(values, failed, "total_tokens"),
        _disp_money(values, failed, "cost_cny"))}]
    for key, name in (("week", "本周"), ("month", "本月")):
        total_name = key + "_total_tokens"
        cost_name = key + "_cost_cny"
        lines.append({"id": key, "text": "%s %s tok｜%s" % (
            name, _disp_tokens(values, failed, total_name),
            _disp_money(values, failed, cost_name, key + "_usd_cny"))})
    lines.append({"id": "requests", "text": "请求 今/周/月 %s/%s/%s｜错误 %s/%s/%s" % (
        _disp_int(values, failed, "requests"),
        _disp_int(values, failed, "week_requests"),
        _disp_int(values, failed, "month_requests"),
        _disp_int(values, failed, "errors"),
        _disp_int(values, failed, "week_errors"),
        _disp_int(values, failed, "month_errors"))})
    lines.append({"id": "io", "text": "输入 今/周/月 %s/%s/%s｜输出 %s/%s/%s" % (
        _disp_tokens(values, failed, "prompt_tokens"), _disp_tokens(values, failed, "completion_tokens"),
        _disp_tokens(values, failed, "week_prompt_tokens"), _disp_tokens(values, failed, "week_completion_tokens"),
        _disp_tokens(values, failed, "month_prompt_tokens"), _disp_tokens(values, failed, "month_completion_tokens"))})
    lines.append({"id": "cache", "text": "缓存命中 今/周/月 %s/%s/%s" % (
        _disp_tokens(values, failed, "cached_tokens"),
        _disp_tokens(values, failed, "week_cached_tokens"),
        _disp_tokens(values, failed, "month_cached_tokens"))})
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
        "schema_version": "2",
        "text": build_text(values, failed, range_key),
        "lines": build_lines(values, failed, range_key),
        "total_tokens": values.get("total_tokens"),
        "prompt_tokens": values.get("prompt_tokens"),
        "completion_tokens": values.get("completion_tokens"),
        "requests": values.get("requests"),
        "errors": values.get("errors"),
        "cost_cny": values.get("cost_cny"),
        "usd_cny": values.get("usd_cny"),
        "cached_tokens": values.get("cached_tokens"),
        "week_total_tokens": values.get("week_total_tokens"),
        "week_prompt_tokens": values.get("week_prompt_tokens"),
        "week_completion_tokens": values.get("week_completion_tokens"),
        "week_cached_tokens": values.get("week_cached_tokens"),
        "week_requests": values.get("week_requests"),
        "week_errors": values.get("week_errors"),
        "week_cost_cny": values.get("week_cost_cny"),
        "week_usd_cny": values.get("week_usd_cny"),
        "month_total_tokens": values.get("month_total_tokens"),
        "month_prompt_tokens": values.get("month_prompt_tokens"),
        "month_completion_tokens": values.get("month_completion_tokens"),
        "month_cached_tokens": values.get("month_cached_tokens"),
        "month_requests": values.get("month_requests"),
        "month_errors": values.get("month_errors"),
        "month_cost_cny": values.get("month_cost_cny"),
        "month_usd_cny": values.get("month_usd_cny"),
        "failed": failed,
        "cost_missing_models": missing_models,
        "realm": snapshot.get("realm", ""),
        "since": snapshot.get("since", ""),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def combine_range_snapshots(snapshots):
    """把 hub 分别按 today/week/month 返回的 analytics 窗口合成一份 AOD 数据。"""
    today = snapshots.get("today") or {}
    combined = dict(today)
    combined["realm"] = "all"
    fields = ("total_tokens", "prompt_tokens", "completion_tokens",
              "cached_tokens", "requests", "errors", "cost_cny", "usd_cny")
    for period in ("week", "month"):
        snapshot = snapshots.get(period) or {}
        for field in fields:
            combined[period + "_" + field] = snapshot.get(field)
        combined[period + "_cost_missing"] = snapshot.get("cost_missing")
    return combined


def collect_credit_totals(accounts):
    """按 realm 汇总各账号积分余额（credits.remain）；无积分数据的账号按 0 计。"""
    totals = {"cn": 0.0, "intl": 0.0}
    for account in accounts or []:
        if not isinstance(account, dict):
            continue
        realm = str(account.get("realm") or "").strip().lower()
        if realm not in totals:
            continue
        credits = account.get("credits")
        remain = credits.get("remain") if isinstance(credits, dict) else None
        try:
            totals[realm] += float(remain or 0)
        except (TypeError, ValueError):
            pass
    return {"cn": round(totals["cn"], 2), "intl": round(totals["intl"], 2)}


def collect_disabled_accounts(accounts, now):
    """提取被禁用的账号（enabled=false 或处于冷却中），供 App 侧表格展示。"""
    disabled = []
    for account in accounts or []:
        if not isinstance(account, dict):
            continue
        enabled = account.get("enabled")
        in_cooldown = bool(account.get("inCooldown"))
        if enabled and not in_cooldown:
            continue
        try:
            cooldown_for = int(account.get("cooldownFor") or 0)
        except (TypeError, ValueError):
            cooldown_for = 0
        try:
            expires_at = float(account.get("expiresAt") or 0)
        except (TypeError, ValueError):
            expires_at = 0
        models = []
        for entry in (account.get("modelCooldowns") or []):
            if not isinstance(entry, dict):
                continue
            model = str(entry.get("model") or "")
            if model:
                models.append(model)
        disabled.append({
            "uid": str(account.get("uid") or ""),
            "nickname": str(account.get("nickname") or ""),
            "realm": str(account.get("realm") or ""),
            "enabled": bool(enabled),
            "inCooldown": in_cooldown,
            "cooldownFor": cooldown_for,
            "lastError": str(account.get("lastError") or ""),
            "expiresAt": int(expires_at),
            "expiresIn": str(account.get("expiresIn") or ""),
            "cooldownModels": models,
        })
    disabled.sort(key=lambda row: (row["realm"], -row["cooldownFor"]))
    return disabled


def mock_snapshot(range_key="today"):
    """内置 mock：30 秒一档，节律与 mock_hub.py 一致，便于观察数据变化。"""
    bucket = int(time.time() // 30)
    multiplier = {"today": 1, "week": 8, "month": 27}.get(range_key, 1)
    total = (128000 + (bucket % 97) * 5000) * multiplier
    return {
        "total_tokens": total,
        "prompt_tokens": int(total * 0.72),
        "completion_tokens": int(total * 0.28),
        "cached_tokens": int(total * 0.11),
        "requests": 87 + bucket % 13,
        "errors": bucket % 3,
        "cost_cny": round(total / 1000000.0 * 3.2, 2),
        "usd_cny": 7.2,
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
        snapshot.pop(name, None)


def mock_accounts():
    """内置 mock 账号：覆盖启用/禁用/冷却/各 realm，供联调与自测。"""
    return [
        {"uid": "cn-a1", "nickname": "国内一号", "realm": "cn", "enabled": True,
         "inCooldown": False, "cooldownFor": 0, "lastError": "",
         "expiresAt": int(time.time()) + 86400, "expiresIn": "24h",
         "modelCooldowns": [], "credits": {"remain": 1280.5}},
        {"uid": "cn-a2", "nickname": "国内二号", "realm": "cn", "enabled": False,
         "inCooldown": False, "cooldownFor": 0,
         "lastError": "disabled by upstream",
         "expiresAt": int(time.time()) + 3600, "expiresIn": "1h",
         "modelCooldowns": [], "credits": None},
        {"uid": "intl-b1", "nickname": "国际一号", "realm": "intl", "enabled": True,
         "inCooldown": True, "cooldownFor": 245,
         "lastError": "429 rate limited",
         "expiresAt": int(time.time()) + 7200, "expiresIn": "2h",
         "modelCooldowns": [{"model": "deepseek-v4.1-flash", "expiresAt": 0}],
         "credits": {"remain": 320.0}},
        {"uid": "intl-b2", "nickname": "国际二号", "realm": "intl", "enabled": True,
         "inCooldown": False, "cooldownFor": 0, "lastError": "",
         "expiresAt": int(time.time()) + 172800, "expiresIn": "48h",
         "modelCooldowns": [], "credits": {"remain": 12.75}},
    ]


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
        self._cache = {}      # (realm) -> (payload, built_at)
        self._last_good = {}  # realm -> (snapshots by range, fetched_at)
        self._accounts_cache = {}  # realm -> (payload, built_at)

    def get_accounts(self, realm):
        """账号积分合计 + 禁用列表（App 内表格专用；不进 AOD 文本）。"""
        now = time.time()
        cache_key = realm or "all"
        cached = self._accounts_cache.get(cache_key)
        if cached is not None and now - cached[1] < self.cache_ttl:
            return dict(cached[0])
        if self.mock:
            accounts = mock_accounts()
        else:
            accounts = self.hub.accounts(cache_key if cache_key != "all" else None)
        payload = {
            "ok": True,
            "service": "aod-relay",
            "version": RELAY_VERSION,
            "credits": collect_credit_totals(accounts),
            "disabled_accounts": collect_disabled_accounts(accounts, now),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self._accounts_cache[cache_key] = (payload, now)
        return payload

    def get_payload(self, range_key, realm):
        now = time.time()
        if self.mock:
            snapshots = {period: mock_snapshot(period) for period in USAGE_RANGES}
            for snapshot in snapshots.values():
                apply_mock_failures(snapshot, self.mock_fail)
            snapshot = combine_range_snapshots(snapshots)
            payload = build_payload(snapshot, "all", "mock",
                                    cost_ignore_missing=self.cost_ignore_missing)
            payload["credits"] = collect_credit_totals(mock_accounts())
            payload["disabled_accounts"] = collect_disabled_accounts(
                mock_accounts(), now)
            return payload
        with self.lock:
            cache_key = realm or "all"
            if cache_key in self._cache:
                payload, built_at = self._cache[cache_key]
                if now - built_at < self.cache_ttl:
                    payload = dict(payload)
                    payload["cached"] = True
                    return payload
            try:
                previous = self._last_good.get(cache_key, ({}, 0))[0]
                snapshots = {}
                range_failures = {}
                with ThreadPoolExecutor(max_workers=len(USAGE_RANGES)) as executor:
                    futures = {executor.submit(self.hub.usage, period, cache_key): period
                               for period in USAGE_RANGES}
                    for future in as_completed(futures):
                        period = futures[future]
                        try:
                            snapshot = future.result()
                            if snapshot is None:
                                snapshot = previous.get(period)
                                if snapshot is None:
                                    raise HubError("data", "hub 对 %s 返回 304，但没有本地快照" % period)
                            snapshots[period] = snapshot
                        except HubError as period_error:
                            if period == "today":
                                raise period_error
                            range_failures[period] = str(period_error)
                            snapshots[period] = previous.get(period, {})
                combined = combine_range_snapshots(snapshots)
                accounts = None
                accounts_error = ""
                try:
                    accounts = self.hub.accounts(cache_key if cache_key != "all" else None)
                except HubError as account_error:
                    accounts_error = str(account_error)
                payload = build_payload(combined, "all", "hub",
                                        cost_ignore_missing=self.cost_ignore_missing)
                if accounts is not None:
                    payload["credits"] = collect_credit_totals(accounts)
                    payload["disabled_accounts"] = collect_disabled_accounts(accounts, now)
                else:
                    payload["credits"] = {"cn": None, "intl": None}
                    payload["disabled_accounts"] = []
                    payload["accounts_error"] = accounts_error
                if range_failures:
                    for period, reason in range_failures.items():
                        prefix = "" if period == "today" else period + "_"
                        fields = ("total_tokens", "prompt_tokens", "completion_tokens",
                                  "cached_tokens", "requests", "errors", "cost_cny")
                        for field in fields:
                            payload["failed"][prefix + field] = reason
                    values, _existing, _missing = collect_fields(combined, self.cost_ignore_missing)
                    payload["lines"] = build_lines(values, payload["failed"], "all")
                    payload["text"] = "\n".join(line["text"] for line in payload["lines"])
                self._last_good[cache_key] = (snapshots, now)
                self._cache[cache_key] = (payload, now)
                return payload
            except HubError as err:
                if cache_key in self._last_good:
                    snapshots, fetched_at = self._last_good[cache_key]
                    if now - fetched_at <= self.stale_max:
                        combined = combine_range_snapshots(snapshots)
                        payload = build_payload(
                            combined, "all", "hub", cached=True, stale=True,
                            cost_ignore_missing=self.cost_ignore_missing)
                        payload["stale_reason"] = str(err)
                        return payload
                raise
            except Exception as err:
                raise HubError("net", "并发读取 hub 窗口失败: %s" % err)


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

            if path not in ("/aod/usage", "/aod/data", "/aod/accounts"):
                return self._json(404, {"ok": False, "error": "not found"})
            if not self._token_ok(query):
                return self._json(401, {"ok": False, "error": "invalid relay token"})

            if path == "/aod/accounts":
                realm = (query.get("realm") or [""])[0].strip().lower()
                if realm not in ("", "intl", "cn", "all"):
                    return self._json(400, {"ok": False, "error": "bad realm"})
                try:
                    account_payload = state.get_accounts(realm)
                except HubError as err:
                    return self._json(502, {
                        "ok": False, "service": "aod-relay",
                        "error": str(err), "kind": err.kind})
                return self._json(200, account_payload)

            range_key = "all"
            if (query.get("range") or [""])[0].strip().lower() not in ("", "today", "week", "month", "all"):
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
            print("[relay] 拒绝启动：需要 panel 密码用于兼容方案", file=sys.stderr)
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
