import asyncio
import json
import os
import re
import secrets
import sys
import threading
import time
import uuid
import webbrowser
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, quote, urlparse

import httpx
import websockets
from mcp.server.fastmcp import FastMCP

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
import report as report_module
import report_templates
import interpret as interpret_module

CONFIG_PATH = BASE_DIR / "config.json"
ENDPOINTS_PATH = BASE_DIR / "endpoints.json"
SETTINGS_HTML_PATH = BASE_DIR / "settings.html"
UI_TOKEN = secrets.token_urlsafe(24)

DEFAULT_CONFIG: dict[str, Any] = {
    "base_url": "",
    "token": "",
    "result_mode": "websocket",
    "websocket": {
        "host": "",
        "port": 6001,
        "key": "b3CnDSGvJUDhqqEJekBddzUh",
        "cluster": "ap3",
        "tls": False,
    },
    "webhook": {
        "host": "127.0.0.1",
        "port": 8766,
        "public_base_url": "",
    },
    "defaults": {
        "channel_prefix": "wb-",
        "timeout_seconds": 120,
        "subscribe_delay_seconds": 0.4,
        "img_width": 1521,
        "img_height": 900,
        "webhook": "",
    },
    "settings_ui": {
        "host": "127.0.0.1",
        "port": 8765,
    },
    "report": {
        "template": "aurora",
    },
}

WEBHOOK_RESULTS: dict[str, Any] = {}
WEBHOOK_LOCK = threading.Lock()
_settings_server: Optional[ThreadingHTTPServer] = None
_settings_thread: Optional[threading.Thread] = None
_webhook_server: Optional[ThreadingHTTPServer] = None
_webhook_thread: Optional[threading.Thread] = None
_LAST_REPORT: Optional[str] = None
_LAST_BROADCAST: Optional[dict[str, Any]] = None

mcp = FastMCP("bql")


def deep_merge(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(base)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return deepcopy(DEFAULT_CONFIG)
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    return deep_merge(DEFAULT_CONFIG, raw)


def save_config(config: dict[str, Any]) -> None:
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp, CONFIG_PATH)
    os.chmod(CONFIG_PATH, 0o600)


def masked_config(config: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(config)
    if out.get("token"):
        out["token"] = "******"
    return out


_ENDPOINTS_CACHE: dict[str, Any] = {"mtime": None, "data": {}}


def load_endpoints() -> dict[str, Any]:
    """接口注册表带 mtime 缓存，避免每次调用都读盘解析 JSON。"""
    try:
        mtime = ENDPOINTS_PATH.stat().st_mtime
    except OSError:
        return {}
    if _ENDPOINTS_CACHE["mtime"] != mtime:
        with ENDPOINTS_PATH.open("r", encoding="utf-8") as handle:
            _ENDPOINTS_CACHE["data"] = json.load(handle)
        _ENDPOINTS_CACHE["mtime"] = mtime
    return _ENDPOINTS_CACHE["data"]


_HTTP_CLIENTS: dict[int, tuple[Any, httpx.AsyncClient]] = {}


def shared_client() -> httpx.AsyncClient:
    """按事件循环复用 HTTP 连接池，省掉每次调用的 TLS 握手。"""
    loop = asyncio.get_running_loop()
    key = id(loop)
    cached = _HTTP_CLIENTS.get(key)
    if cached and cached[0] is loop and not cached[1].is_closed:
        return cached[1]
    client = httpx.AsyncClient(
        timeout=30,
        limits=httpx.Limits(max_keepalive_connections=8, keepalive_expiry=180),
        follow_redirects=True,
    )
    _HTTP_CLIENTS[key] = (loop, client)
    return client


def derive_host(base_url: str) -> str:
    if not base_url:
        return ""
    candidate = base_url if "://" in base_url else "https://" + base_url
    return urlparse(candidate).hostname or ""


def derive_tls(base_url: str) -> bool:
    if not base_url:
        return False
    candidate = base_url if "://" in base_url else "https://" + base_url
    return urlparse(candidate).scheme.lower() == "https"


def resolve_websocket(config: dict[str, Any]) -> dict[str, Any]:
    ws = deepcopy(config.get("websocket") or {})
    base = config.get("base_url", "")
    if not ws.get("host"):
        ws["host"] = derive_host(base)
    if ws.get("tls") is None:
        ws["tls"] = derive_tls(base)
    if not ws.get("port"):
        ws["port"] = 443 if ws.get("tls") else 6001
    return ws


def websocket_uri(ws: dict[str, Any]) -> str:
    scheme = "wss" if ws.get("tls") else "ws"
    host = str(ws.get("host") or "").strip()
    port = int(ws.get("port") or 6001)
    key = str(ws.get("key") or "").strip()
    return f"{scheme}://{host}:{port}/app/{key}?protocol=7&client=js&version=7.0.0&flash=false"


_BROADCAST_LABELS = ("广播地址", "wsHost", "host")
_PORT_LABELS = ("广播端口", "port")


def _strip_label(value: Any, labels: tuple[str, ...]) -> str:
    """从 '广播地址：47.100.178.195' 这类文本里取出真实值。"""
    text = str(value or "").strip()
    for label in labels:
        match = re.search(label + r"\s*[:：]\s*(.+)", text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return text


def parse_broadcast(ack: Any) -> dict[str, Any]:
    """从 POST 的 202 响应里解析服务端给出的广播地址/端口/channel。"""
    out: dict[str, Any] = {}
    if not isinstance(ack, dict):
        return out
    data = ack.get("data")
    if not isinstance(data, dict):
        return out
    if "wsHost" in data:
        out["host"] = _strip_label(data.get("wsHost"), _BROADCAST_LABELS)
    if "port" in data:
        raw_port = _strip_label(data.get("port"), _PORT_LABELS)
        digits = re.sub(r"[^0-9]", "", raw_port)
        if digits:
            out["port"] = int(digits)
    if "channel" in data:
        raw_channel = _strip_label(data.get("channel"), ("广播频道名称", "channel"))
        if raw_channel:
            out["channel"] = raw_channel
    return out


def apply_broadcast(config: dict[str, Any], broadcast: dict[str, Any]) -> dict[str, Any]:
    """用服务端返回的广播地址覆盖本地 WS 配置（可选，失败则回退到本地配置）。"""
    ws = resolve_websocket(config)
    if broadcast.get("host"):
        ws["host"] = str(broadcast["host"])
    if broadcast.get("port"):
        ws["port"] = int(broadcast["port"])
    return ws


def build_webhook_url(config: dict[str, Any], channel: str) -> str:
    override = str((config.get("defaults") or {}).get("webhook") or "").strip()
    if override:
        return override.replace("{channel}", quote(channel))
    webhook = config.get("webhook") or {}
    base = str(webhook.get("public_base_url") or "").strip()
    if not base:
        host = str(webhook.get("host") or "127.0.0.1").strip()
        port = int(webhook.get("port") or 8766)
        base = f"http://{host}:{port}"
    return base.rstrip("/") + "/webhook/" + quote(channel)


def require_runtime_config(config: dict[str, Any]) -> None:
    if not str(config.get("base_url") or "").strip():
        raise ValueError("BASE_URL 未配置，请先打开 BQL 设置页填写。")
    if not str(config.get("token") or "").strip():
        raise ValueError("TOKEN 未配置，请先打开 BQL 设置页填写。")
    if str(config.get("result_mode") or "auto") not in {"auto", "webhook", "websocket"}:
        raise ValueError("result_mode 只能是 auto、webhook 或 websocket。")


def validate_payload(spec: dict[str, Any], payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("payload 必须是 JSON 对象。")
    body = deepcopy(payload)
    required = spec.get("required") or []
    missing = [key for key in required if key not in body or body[key] is None]
    if missing:
        raise ValueError("缺少必填字段：" + ", ".join(missing))
    data = body.get("data")
    if not isinstance(data, list) or not data:
        raise ValueError("data 必须是非空数组。")
    variables = body.get("variables")
    if isinstance(variables, str):
        body["variables"] = [variables]
    elif not isinstance(variables, list) or not variables:
        raise ValueError("variables 必须是非空数组或字段名字符串。")
    for key, allowed in (spec.get("enums") or {}).items():
        if key in body and body[key] not in allowed:
            raise ValueError(f"{key} 必须是以下值之一：{', '.join(map(str, allowed))}")
    return body


def build_summary(charts: list[dict[str, Any]], raw: dict[str, Any]) -> str:
    lines: list[str] = []
    status = raw.get("status", raw.get("code"))
    message = raw.get("msg") or raw.get("message") or ""
    if status is not None or message:
        lines.append(f"状态：{status} {message}".strip())
    for index, chart in enumerate(charts, 1):
        chart_type = chart.get("chart_type") or "分析"
        lines.append(f"[{index}] {chart_type}")
        values = chart.get("values") or {}
        if isinstance(values, dict):
            for key, value in list(values.items())[:30]:
                lines.append(f"  {key}: {value}")
        minitab = chart.get("minitab") or {}
        if isinstance(minitab, dict):
            for key in ("OTGraph", "mtbOutDoc", "data_file_path", "worksheet", "Exception"):
                if minitab.get(key):
                    lines.append(f"  {key}: {minitab[key]}")
    if not lines:
        lines.append("已收到结果，但未能解析出指标字段。")
    return "\n".join(lines)


def normalize_result(result: Any, ack: Any, channel: str, source: str) -> dict[str, Any]:
    raw = result if isinstance(result, dict) else {"raw": result}
    data = raw.get("data", raw)
    items = data if isinstance(data, list) else [data]
    charts: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        values = item.get("values")
        if values is None:
            values = item.get("jsonObject") or {}
        minitab = item.get("minitab")
        if minitab is None and ("OTGraph" in item or "mtbOutDoc" in item or "jsonObject" in item):
            minitab = item
        charts.append(
            {
                "chart_type": item.get("chartType"),
                "values": values,
                "minitab": minitab or {},
            }
        )
    return {
        "summary": build_summary(charts, raw),
        "source": source,
        "channel": channel,
        "charts": charts,
        "http_ack": ack,
        "raw": raw,
    }


async def wait_webhook(channel: str, timeout: float) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        with WEBHOOK_LOCK:
            if channel in WEBHOOK_RESULTS:
                return WEBHOOK_RESULTS.pop(channel)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"等待 webhook 结果超时（channel={channel}）。")
        await asyncio.sleep(min(0.2, remaining))


def is_result_event(event: str) -> bool:
    """结果事件可能带 PHP 命名空间前缀，如 App\\Events\\BroadcastAnalysisResults。"""
    return event.endswith("BroadcastAnalysisResults")


async def wait_websocket(
    config: dict[str, Any], channel: str, timeout: float, ws_override: Optional[dict[str, Any]] = None
) -> Any:
    ws = apply_broadcast(config, ws_override or {})
    if not ws.get("host") or not ws.get("key"):
        raise ValueError("WebSocket 的 host 或 key 未配置。")
    uri = websocket_uri(ws)
    deadline = time.monotonic() + timeout

    async with websockets.connect(uri, ping_interval=20, ping_timeout=20) as connection:
        await connection.send(
            json.dumps({"event": "pusher:subscribe", "data": {"channel": channel}})
        )
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"等待 WebSocket 结果超时（channel={channel}）。")
            raw = await asyncio.wait_for(connection.recv(), remaining)
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                continue
            event = str(message.get("event", ""))
            if event == "pusher:ping":
                await connection.send(json.dumps({"event": "pusher:pong"}))
                continue
            if event in {"pusher_internal:subscription_error", "pusher:error"}:
                raise RuntimeError(f"WebSocket 订阅失败：{message.get('data')}")
            if not is_result_event(event):
                continue
            payload = message.get("data")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except ValueError:
                    continue
            if isinstance(payload, dict):
                return payload


def webhook_is_public(config: dict[str, Any]) -> bool:
    """本地 127.0.0.1 / localhost 的 webhook 外网服务回调不到，视为未配置。"""
    override = str((config.get("defaults") or {}).get("webhook") or "").strip()
    base = override or str((config.get("webhook") or {}).get("public_base_url") or "").strip()
    if not base:
        return False
    host = urlparse(base if "://" in base else "https://" + base).hostname or ""
    return host not in {"127.0.0.1", "localhost", "::1", "0.0.0.0", ""}


async def collect_result(
    config: dict[str, Any],
    channel: str,
    timeout: float,
    broadcast: Optional[dict[str, Any]] = None,
) -> tuple[Any, str]:
    mode = str(config.get("result_mode") or "websocket")
    tasks: dict[asyncio.Task, str] = {}
    if mode in {"auto", "webhook"} and webhook_is_public(config):
        tasks[asyncio.create_task(wait_webhook(channel, timeout))] = "webhook"
    if mode in {"auto", "websocket"} or (mode == "webhook" and not webhook_is_public(config)):
        tasks[asyncio.create_task(wait_websocket(config, channel, timeout, broadcast))] = "websocket"
    if not tasks:
        raise ValueError("没有可用的结果接收通道。")

    pending = set(tasks)
    failures: list[tuple[str, BaseException]] = []
    deadline = time.monotonic() + timeout
    try:
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"等待分析结果超时（channel={channel}）。")
            done, pending = await asyncio.wait(
                pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                try:
                    result = task.result()
                except BaseException as exc:
                    failures.append((tasks[task], exc))
                    continue
                for other in pending:
                    other.cancel()
                return result, tasks[task]
        if failures:
            raise failures[0][1]
        raise TimeoutError(f"等待分析结果超时（channel={channel}）。")
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()


async def pick_result(
    ws_task: asyncio.Task,
    webhook_task: Optional[asyncio.Task],
    timeout: float,
    channel: str,
) -> tuple[Any, str]:
    """等待已并行的结果任务，谁先返回用谁。"""
    tasks: dict[asyncio.Task, str] = {ws_task: "websocket"}
    if webhook_task is not None:
        tasks[webhook_task] = "webhook"
    pending = set(tasks)
    failures: list[tuple[str, BaseException]] = []
    deadline = time.monotonic() + timeout
    try:
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"等待分析结果超时（channel={channel}）。")
            done, pending = await asyncio.wait(
                pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                try:
                    result = task.result()
                except BaseException as exc:
                    failures.append((tasks[task], exc))
                    continue
                for other in pending:
                    other.cancel()
                return result, tasks[task]
        if failures:
            raise failures[0][1]
        raise TimeoutError(f"等待分析结果超时（channel={channel}）。")
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()


async def post_analysis(config: dict[str, Any], spec: dict[str, Any], body: dict[str, Any], channel: str) -> Any:
    base_url = str(config.get("base_url") or "").rstrip("/")
    path = str(spec.get("path") or "")
    defaults = config.get("defaults") or {}
    params: dict[str, Any] = dict(spec.get("query_defaults") or {})
    params["channel"] = channel
    if defaults.get("img_width") is not None:
        params["img_width"] = defaults["img_width"]
    if defaults.get("img_height") is not None:
        params["img_height"] = defaults["img_height"]
    if webhook_is_public(config) and str(config.get("result_mode") or "websocket") in {"auto", "webhook"}:
        params["webhook"] = build_webhook_url(config, channel)

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": "Bearer " + str(config.get("token") or ""),
    }
    timeout = float(defaults.get("timeout_seconds") or 120)
    client = shared_client()
    response = await client.post(base_url + path, params=params, json=body, headers=headers, timeout=timeout)
    response.raise_for_status()
    try:
        return response.json()
    except ValueError:
        return {"text": response.text[:2000]}


async def probe_http(config: dict[str, Any]) -> dict[str, Any]:
    base_url = str(config.get("base_url") or "").strip()
    if not base_url:
        return {"ok": False, "error": "BASE_URL 未配置"}
    headers = {"Authorization": "Bearer " + str(config.get("token") or "")}
    try:
        response = await shared_client().get(base_url, headers=headers, timeout=8)
        return {"ok": response.status_code < 500, "status": response.status_code}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def probe_webhook(config: dict[str, Any]) -> dict[str, Any]:
    try:
        ensure_webhook_server()
        webhook = config.get("webhook") or {}
        host = str(webhook.get("host") or "127.0.0.1")
        port = int(webhook.get("port") or 8766)
        channel = "selftest-" + uuid.uuid4().hex[:10]
        url = f"http://{host}:{port}/webhook/{channel}"
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(url, json={"selftest": True, "channel": channel})
            response.raise_for_status()
        received = await wait_webhook(channel, 3)
        return {"ok": received.get("selftest") is True, "url": url}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def probe_websocket(config: dict[str, Any]) -> dict[str, Any]:
    try:
        ws = resolve_websocket(config)
        if not ws.get("host") or not ws.get("key"):
            return {"ok": False, "error": "WebSocket host 或 key 未配置"}
        channel = "selftest-" + uuid.uuid4().hex[:10]
        uri = websocket_uri(ws)
        async with websockets.connect(uri, ping_interval=20, ping_timeout=20) as connection:
            await connection.send(
                json.dumps({"event": "pusher:subscribe", "data": {"channel": channel}})
            )
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                message = json.loads(await asyncio.wait_for(connection.recv(), 4))
                if message.get("event") in {
                    "pusher_internal:subscription_succeeded",
                    "pusher:connection_established",
                }:
                    return {"ok": True, "uri": uri}
        return {"ok": False, "error": "未收到订阅成功回执", "uri": uri}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def test_connection(config: dict[str, Any]) -> dict[str, Any]:
    mode = str(config.get("result_mode") or "websocket")
    result: dict[str, Any] = {
        "ok": False,
        "result_mode": mode,
        "http": await probe_http(config),
        "webhook": None,
        "websocket": None,
    }
    if mode in {"auto", "webhook"} and webhook_is_public(config):
        result["webhook"] = await probe_webhook(config)
    if mode in {"auto", "websocket"} or (mode == "webhook" and not webhook_is_public(config)):
        result["websocket"] = await probe_websocket(config)
    checks = [check for check in (result["http"], result["webhook"], result["websocket"]) if check]
    result["ok"] = all(bool(check.get("ok")) for check in checks)
    return result


async def execute_analysis(
    endpoint: str,
    payload: dict[str, Any],
    channel: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
    include_raw: bool = False,
) -> dict[str, Any]:
    config = load_config()
    require_runtime_config(config)
    endpoints = load_endpoints()
    if endpoint not in endpoints:
        raise ValueError("未知接口：" + endpoint + "；可用接口：" + ", ".join(endpoints))
    spec = endpoints[endpoint]
    body = validate_payload(spec, payload)

    defaults = config.get("defaults") or {}
    prefix = str(defaults.get("channel_prefix") or "wb-")
    channel = channel or prefix + uuid.uuid4().hex[:12]
    timeout = float(timeout_seconds or defaults.get("timeout_seconds") or 120)

    mode = str(config.get("result_mode") or "websocket")
    webhook_public = mode in {"auto", "webhook"} and webhook_is_public(config)
    if webhook_public:
        ensure_webhook_server()

    # 并行：提交任务的同时，用上一次的广播地址预先订阅 WebSocket。
    # 首次调用没有缓存时退化为串行；缓存地址与服务端返回不一致会自动切换。
    global _LAST_BROADCAST
    post_task = asyncio.create_task(post_analysis(config, spec, body, channel))
    ws_task: Optional[asyncio.Task] = None
    if _LAST_BROADCAST:
        ws_task = asyncio.create_task(wait_websocket(config, channel, timeout, _LAST_BROADCAST))
    webhook_task = asyncio.create_task(wait_webhook(channel, timeout)) if webhook_public else None

    previous_broadcast = _LAST_BROADCAST
    ack = await post_task
    broadcast = parse_broadcast(ack)
    if broadcast.get("host"):
        _LAST_BROADCAST = broadcast

    if ws_task is None:
        ws_task = asyncio.create_task(wait_websocket(config, channel, timeout, broadcast))
    elif ws_task.done() and ws_task.exception() is not None:
        ws_task = asyncio.create_task(wait_websocket(config, channel, timeout, broadcast))
    elif (
        previous_broadcast
        and broadcast.get("host")
        and str(broadcast.get("host")) != str(previous_broadcast.get("host"))
    ):
        ws_task.cancel()
        ws_task = asyncio.create_task(wait_websocket(config, channel, timeout, broadcast))

    result, source = await pick_result(ws_task, webhook_task, timeout, channel)
    normalized = normalize_result(result, ack, channel, source)
    normalized["broadcast"] = broadcast
    try:
        normalized["interpretation"] = interpret_module.from_result(normalized, body)
    except Exception as exc:
        normalized["interpret_error"] = f"解读生成失败：{exc}"
    try:
        template_name = str((config.get("report") or {}).get("template") or "aurora")
        report_path = report_module.render_report(
            endpoint, body, normalized, channel, source, broadcast, template=template_name
        )
        normalized["report_template"] = template_name
        global _LAST_REPORT
        _LAST_REPORT = str(report_path)
        normalized["report_path"] = str(report_path)
        normalized["summary"] += f"\n可视化报告：{report_path}"
        # 图片内嵌放到后台，报告先用远程地址秒开，不阻塞分析返回
        report_module.embed_minitab_image_async(report_path, normalized)
    except Exception as exc:
        normalized["report_error"] = f"报告生成失败：{exc}"
    try:
        if normalized.get("report_path"):
            json_path = Path(normalized["report_path"]).with_suffix(".json")
            json_path.write_text(
                json.dumps(
                    {
                        "endpoint": endpoint,
                        "channel": channel,
                        "source": source,
                        "report_path": normalized["report_path"],
                        "summary": normalized.get("summary"),
                        "interpretation": normalized.get("interpretation"),
                        "charts": normalized.get("charts"),
                        "broadcast": broadcast,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            normalized["metrics_uri"] = f"bql://metrics/{json_path.name}"
    except Exception as exc:
        normalized["metrics_save_error"] = f"指标保存失败：{exc}"
    if not include_raw:
        normalized.pop("raw", None)
    return normalized


class SettingsHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return

    def _reply(self, status: int, payload: Any, content_type: str = "application/json") -> None:
        if content_type.startswith("application/json"):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        else:
            body = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        token = self.headers.get("X-Settings-Token") or (query.get("token") or [None])[0]
        return token == UI_TOKEN

    def _json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            value = json.loads(raw)
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    def do_GET(self) -> None:
        if not self._authorized():
            self._reply(404, {"error": "not found"})
            return
        path = urlparse(self.path).path
        if path in {"/", "/settings"}:
            html = SETTINGS_HTML_PATH.read_text(encoding="utf-8").replace("__UI_TOKEN__", UI_TOKEN)
            self._reply(200, html, "text/html")
        elif path == "/api/config":
            self._reply(200, {"config": masked_config(load_config())})
        elif path == "/api/endpoints":
            self._reply(200, {"endpoints": load_endpoints()})
        elif path == "/api/templates":
            config = load_config()
            current = str((config.get("report") or {}).get("template") or "aurora")
            self._reply(200, {
                "templates": report_module.list_templates(),
                "current": current,
                "user_dir": str(report_templates.USER_DIR),
            })
        elif path == "/api/report":
            report_dir = report_module.REPORTS_DIR
            files = sorted(report_dir.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True) if report_dir.exists() else []
            self._reply(200, {"ok": bool(files), "path": str(files[0]) if files else "", "count": len(files)})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._authorized():
            self._reply(404, {"error": "not found"})
            return
        path = urlparse(self.path).path
        if path == "/api/config":
            updates = self._json_body()
            config = deep_merge(load_config(), updates)
            save_config(config)
            self._reply(200, {"ok": True, "config": masked_config(config)})
        elif path == "/api/test":
            result = asyncio.run(test_connection(load_config()))
            self._reply(200, result)
        elif path == "/api/analyze":
            body = self._json_body()
            try:
                result = asyncio.run(
                    execute_analysis(
                        str(body.get("endpoint") or "capa"),
                        body.get("payload") or {},
                        body.get("channel"),
                        body.get("timeout_seconds"),
                    )
                )
                self._reply(200, {"ok": True, "result": result})
            except Exception as exc:
                self._reply(200, {"ok": False, "error": str(exc)})
        else:
            self._reply(404, {"error": "not found"})


class WebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not path.startswith("/webhook/"):
            self.send_response(404)
            self.end_headers()
            return
        channel = path[len("/webhook/") :]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"raw": raw[:4000]}
        with WEBHOOK_LOCK:
            WEBHOOK_RESULTS[channel] = payload
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def ensure_settings_server() -> str:
    global _settings_server, _settings_thread
    if _settings_server is not None:
        return settings_url()
    config = load_config()
    ui = config.get("settings_ui") or {}
    host = str(ui.get("host") or "127.0.0.1")
    port = int(ui.get("port") or 8765)
    try:
        server = ThreadingHTTPServer((host, port), SettingsHandler)
    except OSError:
        server = ThreadingHTTPServer((host, 0), SettingsHandler)
    _settings_server = server
    _settings_thread = threading.Thread(target=server.serve_forever, daemon=True)
    _settings_thread.start()
    return settings_url()


def settings_url() -> str:
    if _settings_server is None:
        return ""
    host, port = _settings_server.server_address[:2]
    return f"http://{host}:{port}/settings?token={UI_TOKEN}"


def ensure_webhook_server() -> None:
    global _webhook_server, _webhook_thread
    if _webhook_server is not None:
        return
    config = load_config()
    webhook = config.get("webhook") or {}
    host = str(webhook.get("host") or "127.0.0.1")
    port = int(webhook.get("port") or 8766)
    server = ThreadingHTTPServer((host, port), WebhookHandler)
    _webhook_server = server
    _webhook_thread = threading.Thread(target=server.serve_forever, daemon=True)
    _webhook_thread.start()


def _report_files() -> list[Path]:
    report_dir = report_module.REPORTS_DIR
    if not report_dir.exists():
        return []
    return sorted(report_dir.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True)


@mcp.resource("bql://reports")
def res_reports() -> str:
    """已生成的可视化分析报告列表（最新在前）。"""
    files = _report_files()
    if not files:
        return "# 分析报告\n\n暂无报告。先调用 bql_call 运行一次分析。"
    lines = ["# 分析报告", ""]
    for path in files:
        lines.append(f"- `{path.name}` — {path.stat().st_mtime:.0f}")
    lines.append("")
    lines.append("读取单份报告：`bql://report/{文件名}`；读取结构化指标：`bql://metrics/{文件名}`。")
    return "\n".join(lines)


@mcp.resource("bql://report/{name}")
def res_report(name: str) -> str:
    """读取一份可视化报告的 HTML 内容。name 为 reports 目录下的文件名。"""
    path = (report_module.REPORTS_DIR / name).resolve()
    if not path.exists() or report_module.REPORTS_DIR.resolve() not in path.parents:
        raise ValueError(f"报告不存在：{name}")
    return path.read_text(encoding="utf-8")


@mcp.resource("bql://metrics/{name}")
def res_metrics(name: str) -> str:
    """读取一份分析的结构化指标 JSON（供 Agent 纳入上下文做对比）。"""
    json_name = name.replace(".html", "") + ".json"
    path = (report_module.REPORTS_DIR / json_name).resolve()
    if not path.exists() or report_module.REPORTS_DIR.resolve() not in path.parents:
        raise ValueError(f"指标记录不存在：{json_name}")
    return path.read_text(encoding="utf-8")


@mcp.resource("bql://endpoint/{name}")
def res_endpoint(name: str) -> str:
    """读取某个 BQL 接口的字段定义。"""
    endpoints = load_endpoints()
    if name not in endpoints:
        raise ValueError(f"未知接口：{name}")
    return json.dumps({"name": name, **endpoints[name]}, ensure_ascii=False, indent=2)


def _endpoint_category(name: str, spec: dict[str, Any]) -> str:
    """根据接口 key/名称/路径推断分析类别，便于在大量接口中定位。"""
    text = (name + " " + str(spec.get("name", "")) + " " + str(spec.get("path", ""))).lower()
    rules = [
        ("能力分析", ("capa", "能力", "sixpack", "六合一")),
        ("量具与测量系统", ("gage", "量具", "rn", "linearity", "bias", "attribute", "一致性")),
        ("控制图", ("chart", "控制图", "imr", "xbar", "p图", "np", "cchart", "uchart")),
        ("假设检验", ("test", "检验", "anova", "oneway", "方差", "mann", "kruskal",
                    "mood", "median", "paired", "运行图", "run", "游程", "onet", "twot",
                    "onez", "twoz", "oulier", "outlier", "equalvar", "normality")),
        ("回归与建模", ("regress", "回归", "fit", "模型", "cart", "mass", "响应", "design", "设计",
                      "因子", "混料", "田口", "筛选", "曲面", "优化")),
        ("分布与可靠性", ("dist", "分布", "reliab", "寿命", "可靠性", "growth", "增长", "cox", "weibull")),
        ("图形与可视化", ("plot", "图", "histogram", "boxplot", "dotplot", "matrix", "pie", "ecdf",
                    "probability", "等高", "等值", "cube", "气泡", "bubbl")),
    ]
    for label, keywords in rules:
        if any(k in text for k in keywords):
            return label
    return "其他"


CATEGORY_KEYWORDS = {
    "能力分析": ("capa", "能力", "sixpack", "六合一"),
    "量具与测量系统": ("gage", "量具", "量测", "msa"),
    "控制图": ("chart", "控制图", "control"),
    "假设检验": ("test", "检验", "anova", "方差", "oneway", "mann", "kruskal", "onet", "twot"),
    "回归与建模": ("regress", "回归", "design", "设计", "因子", "响应", "cart", "mass", "田口", "混料"),
    "分布与可靠性": ("dist", "分布", "reliab", "寿命", "growth", "cox"),
    "图形与可视化": ("plot", "图", "histogram", "boxplot", "dotplot"),
}


@mcp.tool()
def bql_list_endpoints(category: Optional[str] = None, keyword: Optional[str] = None) -> dict[str, Any]:
    """列出已注册的分析接口（110+ 个）。可按 category 分组、按关键词筛选。

    category 可选值：能力分析 / 量具与测量系统 / 控制图 / 假设检验 / 回归与建模 /
    分布与可靠性 / 图形与可视化 / 其他。keyword 对接口名、中文名、路径做子串匹配。
    """
    endpoints = load_endpoints()
    items = []
    for name, spec in endpoints.items():
        cat = _endpoint_category(name, spec)
        items.append({
            "name": name,
            "display_name": spec.get("name"),
            "path": spec.get("path"),
            "category": cat,
            "description": (spec.get("description") or "")[:120],
        })
    if category:
        cat_kw = CATEGORY_KEYWORDS.get(category, ())
        items = [i for i in items
                 if i["category"] == category
                 or (cat_kw and any(k in (i["name"] + str(i["display_name"]) + str(i["path"])).lower()
                                     for k in cat_kw))]
    if keyword:
        kw = keyword.lower()
        items = [i for i in items
                 if kw in i["name"].lower()
                 or kw in str(i["display_name"]).lower()
                 or kw in str(i["path"]).lower()]
    groups: dict[str, list[dict[str, Any]]] = {}
    for i in items:
        groups.setdefault(i["category"], []).append(i)
    return {"total": len(items), "groups": groups, "items": items}


@mcp.tool()
def bql_describe_endpoint(endpoint: str) -> dict[str, Any]:
    """查看某个 BQL 接口的路径、必填字段、选填字段和说明。"""
    endpoints = load_endpoints()
    if endpoint not in endpoints:
        raise ValueError("未知接口：" + endpoint + "；可用接口：" + ", ".join(endpoints))
    spec = endpoints[endpoint]
    return {"name": endpoint, **spec}


def _numeric_series(data: Any) -> list[float]:
    """把一列数字、逗号分隔字符串或 [{x:1},...] 统一成 float 列表。"""
    values: list[float] = []
    if isinstance(data, str):
        tokens = [t for t in re.split(r"[,;\s，、]+", data.strip()) if t]
        for token in tokens:
            try:
                values.append(float(token))
            except ValueError:
                raise ValueError(f"无法解析数值：{token}") from None
    elif isinstance(data, (list, tuple)):
        for item in data:
            if isinstance(item, dict):
                numbers = [v for v in item.values() if isinstance(v, (int, float))]
                if not numbers:
                    raise ValueError("data 元素里没有数值字段")
                values.append(float(numbers[0]))
            else:
                try:
                    values.append(float(item))
                except (TypeError, ValueError):
                    raise ValueError(f"无法解析数值：{item}") from None
    else:
        raise ValueError("data 必须是数字数组或逗号分隔的字符串")
    if not values:
        raise ValueError("data 不能为空")
    return values


@mcp.tool()
async def bql_quick_analysis(
    data: Any,
    lspec: float,
    uspec: float,
    target: Optional[float] = None,
    group_size: int = 1,
    analysis_type: str = "CStat",
    variable: str = "测量值",
    endpoint: str = "capa",
    timeout_seconds: Optional[int] = None,
) -> dict[str, Any]:
    """一步完成过程能力分析：直接传一列测量值即可，无需先查接口字段。

    data 支持 [32,53,57] 或 "32,53,57" 或 [{"x":32},{"x":53}]；
    lspec/uspec 为规格下限/上限，target 可选。
    返回精简结论（Cpk/Ppk/判定/报告路径/图表链接），适合对话里最快拿到结果。
    """
    started = time.perf_counter()
    values = _numeric_series(data)
    payload: dict[str, Any] = {
        "data": [{variable: value} for value in values],
        "variables": [variable],
        "type": analysis_type,
        "lspec": lspec,
        "uspec": uspec,
        "group_size": group_size,
    }
    if target is not None:
        payload["target"] = target
    result = await execute_analysis(endpoint, payload, None, timeout_seconds)
    metrics = report_module.extract_metrics(result, payload)
    status = report_module.capability_status(metrics["cpk"])
    charts = result.get("charts") or [{}]
    minitab = charts[0].get("minitab") if isinstance(charts[0], dict) else {}
    minitab = minitab if isinstance(minitab, dict) else {}
    out_of_spec = (report_module._num(metrics["out_usl"]) or 0) + (report_module._num(metrics["out_lsl"]) or 0)
    return {
        "ok": True,
        "verdict": status["label"],
        "grade": status["grade"],
        "cpk": report_module._fmt(metrics["cpk"], 3),
        "ppk": report_module._fmt(metrics["ppk"], 3),
        "mean": report_module._fmt(metrics["mean"], 3),
        "std_within": report_module._fmt(metrics["wstd"], 3),
        "std_overall": report_module._fmt(metrics["ostd"], 3),
        "n": len(values),
        "out_of_spec": out_of_spec,
        "report_path": result.get("report_path"),
        "chart_url": report_module._graph_url(result),
        "minitab_doc": minitab.get("mtbOutDoc"),
        "interpretation": result.get("interpretation"),
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }


@mcp.tool()
async def bql_call(
    endpoint: str,
    payload: dict[str, Any],
    channel: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
) -> dict[str, Any]:
    """调用一个 BQL 分析接口，通过 WebSocket 等待完整分析结果（服务端会返回广播地址）。

    返回 summary(指标摘要)、charts(指标/图表/Minitab 链接)、source、channel 等。
    例：bql_call(endpoint="capa", payload={"data":[{"x":32},{"x":53}],"variables":["x"],
    "type":"CStat","lspec":0,"uspec":100,"group_size":1})。
    """
    return await execute_analysis(endpoint, payload, channel, timeout_seconds)


@mcp.tool()
def bql_configure(
    base_url: Optional[str] = None,
    token: Optional[str] = None,
    result_mode: Optional[str] = None,
    ws_host: Optional[str] = None,
    ws_port: Optional[int] = None,
    ws_key: Optional[str] = None,
    ws_cluster: Optional[str] = None,
    ws_tls: Optional[bool] = None,
    timeout_seconds: Optional[int] = None,
) -> dict[str, Any]:
    """在对话中直接配置 BQL 连接器（无需打开网页）。只需传要修改的字段。

    常用：bql_configure(base_url="https://...", token="...")。
    结果通道默认 websocket（推荐）；服务端会在 POST 响应里返回广播地址，通常无需手动配 ws_host。
    """
    config = load_config()
    updates: dict[str, Any] = {}
    if base_url is not None:
        updates["base_url"] = str(base_url).strip().rstrip("/")
    if token is not None:
        updates["token"] = str(token).strip()
    if result_mode is not None:
        if str(result_mode) not in {"auto", "webhook", "websocket"}:
            raise ValueError("result_mode 只能是 auto、webhook 或 websocket。")
        updates["result_mode"] = str(result_mode)
    ws_updates: dict[str, Any] = {}
    if ws_host is not None:
        ws_updates["host"] = str(ws_host).strip()
    if ws_port is not None:
        ws_updates["port"] = int(ws_port)
    if ws_key is not None:
        ws_updates["key"] = str(ws_key).strip()
    if ws_cluster is not None:
        ws_updates["cluster"] = str(ws_cluster).strip()
    if ws_tls is not None:
        ws_updates["tls"] = bool(ws_tls)
    if ws_updates:
        updates["websocket"] = ws_updates
    if timeout_seconds is not None:
        updates["defaults"] = {"timeout_seconds": int(timeout_seconds)}
    if updates:
        config = deep_merge(config, updates)
        save_config(config)
    masked = masked_config(config)
    return {
        "ok": True,
        "message": "配置已保存。结果通道使用 websocket 即可，服务端会返回广播地址，无需配置本地 webhook。",
        "config": masked,
    }


@mcp.tool()
def bql_list_templates() -> dict[str, Any]:
    """列出所有可用的可视化报告模板（内置 + 用户自定义），并指出当前使用的模板。"""
    config = load_config()
    current = str((config.get("report") or {}).get("template") or "aurora")
    return {
        "current": current,
        "templates": report_module.list_templates(),
        "custom_dir": str(report_templates.USER_DIR),
        "hint": "把自定义 HTML 放进 custom_dir 即可作为新模板使用；同名会覆盖内置模板。",
    }


@mcp.tool()
def bql_set_template(name: str) -> dict[str, Any]:
    """切换可视化报告模板，之后的分析会按新模板生成报告。name 来自 bql_list_templates。"""
    available = {item["name"] for item in report_module.list_templates()}
    if name not in available:
        raise ValueError(f"未知模板：{name}；可用模板：{', '.join(sorted(available))}。请用 bql_list_templates 查看")
    config = load_config()
    report_cfg = config.setdefault("report", {})
    report_cfg["template"] = name
    save_config(config)
    return {
        "ok": True,
        "template": name,
        "message": f"报告模板已切换为 {name}。下一次分析生成的报告将采用该模板；已有报告可用 bql_export 重新导出。",
    }


@mcp.tool()
def bql_export(name: str, format: str = "docx") -> dict[str, Any]:
    """导出一份分析报告。name 为报告文件名（可从 bql://reports 获取），format 支持 docx/pptx/html/md。

    导出结果保存到 reports/exports/ 目录，返回文件路径。
    """
    import exporter as exporter_module  # 延迟导入，减少 MCP 启动时间

    path = exporter_module.export(name, format)
    return {
        "ok": True,
        "format": format.lower().lstrip("."),
        "path": str(path),
        "uri": path.as_uri(),
        "message": f"已导出 {format.upper()} 报告：{path}",
    }


@mcp.tool()
def bql_open_report(path: Optional[str] = None) -> str:
    """打开最近一次分析的可视化报告（HTML）。传 path 可打开指定报告。"""
    global _LAST_REPORT
    target = path or _LAST_REPORT
    if not target:
        report_dir = report_module.REPORTS_DIR
        files = sorted(report_dir.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True) if report_dir.exists() else []
        target = str(files[0]) if files else ""
    if not target or not Path(target).exists():
        raise ValueError("没有可打开的分析报告，请先调用 bql_call 运行一次分析。")
    _LAST_REPORT = target
    url = Path(target).as_uri()
    try:
        webbrowser.open(url)
    except Exception:
        pass
    return url


@mcp.tool()
def bql_open_settings() -> str:
    """打开本地 BQL 连接器配置页（可选）。也可直接用 bql_configure 在对话里配置。"""
    url = ensure_settings_server()
    try:
        webbrowser.open(url)
    except Exception:
        pass
    return url


@mcp.prompt()
def bql_quality_interpret(channel: str = "", industry: str = "通用制造") -> str:
    """以质量工程专家身份解读一份 BQL 分析结果。channel 留空则解读最近一次。

    industry 可选：通用制造 / 汽车 IATF / 航空 AS9100 / 医疗 FDA，阈值与措辞按行业调整。
    """
    target = channel or (_LAST_REPORT.split("-")[-1].replace(".html", "") if _LAST_REPORT else "")
    return (
        "你是资深质量工程师与六西格玛黑带。请基于 BQL 分析结果，输出一份可直接用于汇报的质量解读。\n\n"
        f"目标 channel：{target or '最近一次分析'}\n"
        f"行业背景：{industry}\n\n"
        "步骤：\n"
        "1. 用 bql_list_endpoints / resources/read 读取 bql://reports 找到报告，"
        "再用 bql://metrics/{文件名} 读取结构化指标。\n"
        "2. 指标里的 interpretation 是服务端确定性规则生成的结论，作为事实基准，不要推翻其数值判定。\n"
        "3. 结合行业背景补充：该行业对 Cpk 的常见要求、当前差距、潜在审核风险。\n"
        "4. 输出四段：结论摘要 / 关键发现 / 风险与假设 / 下一步行动（按优先级，含负责角色建议）。\n"
        "5. 不要编造指标里没有的数字；不确定处明确标注“需确认”。\n"
    )


@mcp.prompt()
def bql_analyze_workflow(industry: str = "通用制造") -> str:
    """完整的 BQL 分析工作流：配置检查 → 接口确认 → 调用 → 解读 → 导出。"""
    return (
        "使用 BQL MCP Connector 完成一次质量分析。全程少废话，优先最快拿到结果：\n\n"
        "1. 若未配置，先用 bql_configure(base_url=..., token=...) 配置（Token 不要回显给用户）。\n"
        "2. 用户已给数据 + 规格限时，**直接调用 bql_quick_analysis(data=..., lspec=..., uspec=...)**，"
        "不要先 list/describe 接口，也不要重复确认。\n"
        "3. 只有用户要跑非能力分析接口时，才用 bql_describe_endpoint → bql_call。\n"
        "4. 拿到结果后直接给结论：判定等级、Cpk/Ppk、主要风险、报告路径；不要复述全部原始字段。\n"
        f"5. 用行业背景「{industry}」补充解读，输出结论摘要与行动建议。\n"
        "6. 用户要文件时再用 bql_export 导出。\n"
    )


if __name__ == "__main__":
    save_config(load_config())
    mcp.run(transport="stdio")
