#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""workbuddy2api-hub / aod-relay -> AOD 取数层原型（只用标准库）。

两种模式：
  * 直连 hub：POST /panel/login 换面板 token -> GET /usage 取快照
  * 经中转：GET /aod/usage（Authorization: Bearer <relay_token>）
输出均为一行适合 AOD 显示的短文本（亦可 --json 输出结构化结果）。

设计约束（见工作区《ColorOS16-AOD 自定义》文档）：
  * 取数在独立进程做，绝不放进 AOD / SystemUI 进程；
  * AOD 每分钟刷新一次即可，--watch 即以该节奏轮询；
  * 面板 token 存内存（服务端重启即失效），401 时自动重登一次；
  * /usage 支持 ETag：客户端带 If-None-Match，命中 304 时复用上次数据。

用法示例：
  python fetch_usage.py --base-url http://192.168.1.10:8788 --password '<面板密码>'
  python fetch_usage.py --base-url http://192.168.1.10:8790 --relay-token '<中转令牌>'
  python fetch_usage.py --relay-token '<中转令牌>' --watch 60 --json
环境变量：WB_HUB_URL / WB_PANEL_PASSWORD / AOD_RELAY_TOKEN 可代替命令行参数。
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

EXIT_OK = 0
EXIT_AUTH = 2
EXIT_NET = 3
EXIT_DATA = 4


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
    def __init__(self, base_url, password, timeout=10):
        self.base = base_url.rstrip("/")
        self.password = password
        self.timeout = timeout
        self.token = ""
        self._etag = ""
        self._last_snapshot = None

    # ---- 低层请求 ----
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
            raise HubError("net", "无法连接 %s：%s" % (url, err))

    @staticmethod
    def _json_body(status, raw):
        try:
            return json.loads(raw.decode("utf-8", "replace") or "{}")
        except ValueError:
            raise HubError("data", "服务端返回不是 JSON（HTTP %s）" % status)

    # ---- 面板会话 ----
    def login(self):
        status, _hdrs, raw = self._request(
            "POST", "/panel/login", body={"password": self.password})
        payload = self._json_body(status, raw)
        if status == 429:
            raise HubError("auth", "登录被限流（>5 次/分钟），稍后再试")
        if status != 200 or not payload.get("token"):
            detail = payload.get("error", {}).get("message") if isinstance(
                payload.get("error"), dict) else payload.get("error")
            raise HubError("auth", "面板登录失败（HTTP %s）：%s" % (status, detail or "密码错误？"))
        self.token = payload["token"]
        return payload

    # ---- 用量快照 ----
    def usage(self, range_key="today", realm=""):
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
            return self._last_snapshot  # 数据未变，复用缓存
        if status == 401:
            # 面板 token 过期 / 服务端重启：重登一次再试。
            self.token = ""
            self.login()
            return self.usage(range_key, realm)
        if status != 200:
            payload = self._json_body(status, raw)
            raise HubError("data", "取用量失败（HTTP %s）：%s" % (status, payload))
        snapshot = self._json_body(status, raw)
        etag = resp_headers.get("ETag") or resp_headers.get("etag")
        self._etag = etag or ""
        self._last_snapshot = snapshot
        return snapshot


class RelayClient(HubClient):
    """经 aod-relay 取数：只面对一条精简只读接口，无需知道 hub 细节。"""

    def __init__(self, base_url, relay_token, timeout=10):
        super().__init__(base_url, relay_token, timeout=timeout)

    def usage(self, range_key="today", realm=""):
        query = "/aod/usage?range=" + range_key
        if realm:
            query += "&realm=" + realm
        status, _hdrs, raw = self._request(
            "GET", query, headers={"Authorization": "Bearer " + self.password})
        payload = self._json_body(status, raw)
        if status == 401:
            raise HubError("auth", "relay 令牌无效（HTTP 401）")
        if status != 200 or not payload.get("ok"):
            raise HubError("data", "relay 取数失败（HTTP %s）：%s" % (
                status, payload.get("error") or payload))
        return payload


def fmt_tokens(n):
    """大数字压成 AOD 能放下的短文本（中文习惯：万/亿）。"""
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
        value = float(cny)
    except (TypeError, ValueError):
        return "¥?"
    return "¥%.2f" % value


RANGE_LABEL = {
    "today": "今日", "day": "今日", "1d": "今日",
    "week": "本周", "w": "本周",
    "month": "本月", "m": "本月",
    "all": "累计", "": "累计",
}


def build_line(snapshot, range_key="today"):
    """拼一行 AOD 文本：如「今日 128万 tok｜¥3.45」。

    AOD 防烧屏与低刷约束下建议保持 < 20 字符、无长串高亮。
    """
    label = RANGE_LABEL.get(str(range_key or "").strip().lower(), "用量")
    return "%s %s tok｜%s" % (
        label, fmt_tokens(snapshot.get("total_tokens")), fmt_money(snapshot.get("cost_cny")))


def summarize(snapshot, range_key):
    return {
        "text": snapshot.get("text") or build_line(snapshot, range_key),
        "range": range_key,
        "realm": snapshot.get("realm", ""),
        "total_tokens": _to_int(snapshot.get("total_tokens")),
        "prompt_tokens": _to_int(snapshot.get("prompt_tokens")),
        "completion_tokens": _to_int(snapshot.get("completion_tokens")),
        "requests": _to_int(snapshot.get("requests")),
        "errors": _to_int(snapshot.get("errors")),
        "cost_cny": snapshot.get("cost_cny"),
        "since": snapshot.get("since", ""),
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def parse_args(argv):
    parser = argparse.ArgumentParser(description="workbuddy2api-hub 用量取数（AOD 原型）")
    parser.add_argument("--base-url", default=os.environ.get("WB_HUB_URL", "http://127.0.0.1:8788"),
                        help="hub 地址，默认 http://127.0.0.1:8788（可用 WB_HUB_URL）")
    parser.add_argument("--password", default=os.environ.get("WB_PANEL_PASSWORD", ""),
                        help="面板密码（可用 WB_PANEL_PASSWORD；默认密码 admin，建议已改）")
    parser.add_argument("--relay-token", default=os.environ.get("AOD_RELAY_TOKEN", ""),
                        help="经 aod-relay 取数时的中转令牌（可用 AOD_RELAY_TOKEN）；给了则走 relay 模式")
    parser.add_argument("--realm", default="", choices=["", "intl", "cn", "all"],
                        help="国际版/国内版/全部，默认跟随服务端当前出口")
    parser.add_argument("--range", dest="range_key", default="today",
                        choices=["today", "week", "month", "all"],
                        help="统计窗口，默认今日")
    parser.add_argument("--watch", type=int, default=0, metavar="SECONDS",
                        help="循环模式：每 SECONDS 秒刷新一次（AOD 建议 60），0 为单次")
    parser.add_argument("--json", action="store_true",
                        help="输出 JSON（供广播桥 / 脚本管道消费）")
    parser.add_argument("--timeout", type=float, default=10, help="单次请求超时秒数")
    return parser.parse_args(argv)


def emit(summary, as_json):
    if as_json:
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    else:
        print("%s  [%s] requests=%d errors=%d" % (
            summary["text"], summary["fetched_at"],
            summary["requests"], summary["errors"]), flush=True)


def run_once(client, args):
    snapshot = client.usage(args.range_key, args.realm)
    if snapshot is None:
        raise HubError("data", "尚未取到过任何数据（304 无缓存）")
    emit(summarize(snapshot, args.range_key), args.json)


def run_watch(client, args):
    """分钟级轮询：文本没变就不重复打印（之后替换为广播推送）。"""
    last_text = None
    while True:
        try:
            snapshot = client.usage(args.range_key, args.realm)
            if snapshot is not None:
                summary = summarize(snapshot, args.range_key)
                if summary["text"] != last_text or args.json:
                    emit(summary, args.json)
                    last_text = summary["text"]
        except HubError as err:
            print("[warn] %s" % err, file=sys.stderr, flush=True)
        time.sleep(max(5, args.watch))


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if args.relay_token:
        client = RelayClient(args.base_url, args.relay_token, timeout=args.timeout)
    else:
        if not args.password:
            print("缺少凭据：--password（直连 hub）或 --relay-token（经中转）", file=sys.stderr)
            return EXIT_AUTH
        client = HubClient(args.base_url, args.password, timeout=args.timeout)
    try:
        if args.watch > 0:
            run_watch(client, args)
        else:
            run_once(client, args)
    except HubError as err:
        print("[error] %s" % err, file=sys.stderr)
        return {"auth": EXIT_AUTH, "net": EXIT_NET, "data": EXIT_DATA}[err.kind]
    except KeyboardInterrupt:
        return EXIT_OK
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
