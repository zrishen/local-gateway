#!/usr/bin/env python3
"""
Compatible AI client -> local schema proxy -> configurable OpenAI-compatible upstream.

干三件事
--------
1) 模型名映射（替代 cc-switch 的映射）
   Claude Code 发的是 claude-sonnet-5[1M] 这类名字，网关只认 google/gemini-3.8-flash。
   按 MODEL_MAP 前缀匹配替换；不认识的名字原样放行。
2) 去掉 [1m] / [1M] 后缀
   那是 Claude Code 用来标记 1M 上下文窗口的，上游不认。
3) 只在「google/gemini 模型」上清洗工具 schema
   Claude Code 的 schema 是 Zod 生成的 draft-2020-12，带 propertyNames / const / allOf 等，
   Google 的 Schema proto 只支持子集，原样发会 400。非 Google 模型（deepseek/glm…）原样透传。
   规则：删 $schema/$ref/allOf/propertyNames/…；const:X -> enum:[X]；oneOf -> anyOf；
        修掉 required 里指向不存在属性的条目；null 分支 -> nullable:true。

用法
----
    python sanitize_proxy.py [port]          默认 127.0.0.1:8799

环境变量
    SANITIZE_UPSTREAM   upstream URL; falls back to settings.local.json AiUpstream
    SANITIZE_PORT       监听端口，默认 8799
    SANITIZE_DUMP=1     把清洗后的 body 落到 ./dumps/
    SANITIZE_FORCE_CLEAN=1  不管什么模型都清洗

健康检查： GET http://127.0.0.1:8799/__health
"""

import http.client
import http.server
import ipaddress
import json
import os
from pathlib import Path
import re
import socketserver
import sys
import time
import urllib.parse

from settings_config import load_settings
from upstream_routing import load_dns_overrides
from upstream_transport import (CONNECT_TIMEOUT, MAX_ATTEMPTS, UpstreamUnavailable,
                                error_payload, record_event, request_upstream)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_SETTINGS = load_settings(PROJECT_ROOT)


def _seconds(key, default):
    try:
        return max(1.0, float(PROJECT_SETTINGS.get(key, default)))
    except (TypeError, ValueError):
        return float(default)


HEADERS_TIMEOUT = _seconds('AiUpstreamHeadersTimeoutSeconds', 360)
STREAM_TIMEOUT = _seconds('AiUpstreamStreamIdleSeconds', 360)
UPLOAD_TIMEOUT = _seconds('AiUpstreamUploadSeconds', 180)
UPSTREAM = (os.environ.get("SANITIZE_UPSTREAM") or PROJECT_SETTINGS.get("AiUpstream") or "").rstrip("/")
PORT = int(os.environ.get("SANITIZE_PORT") or (sys.argv[1] if __name__ == "__main__" and len(sys.argv) > 1 else PROJECT_SETTINGS.get("PublicPort", 8799)))
DUMP = os.environ.get("SANITIZE_DUMP") == "1"
FORCE_CLEAN = os.environ.get("SANITIZE_FORCE_CLEAN") == "1"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DUMP_DIR = os.path.join(BASE_DIR, "dumps")
UPSTREAM_DNS_OVERRIDES = load_dns_overrides(PROJECT_ROOT)


def create_upstream_connection(up):
    connection_class = http.client.HTTPSConnection if up.scheme == "https" else http.client.HTTPConnection
    conn = connection_class(up.netloc, timeout=UPLOAD_TIMEOUT)
    override = UPSTREAM_DNS_OVERRIDES.get(up.hostname)
    if not override:
        return conn
    ipaddress.ip_address(override)
    original_create_connection = conn._create_connection

    def connect_socket(address, *args, **kwargs):
        # An explicit machine-local override is authoritative, even when DNS
        # returns a real (but unreachable) address. Only change the TCP target;
        # HTTPSConnection still verifies the certificate/SNI for the hostname.
        return original_create_connection((override, address[1]), *args, **kwargs)

    conn._create_connection = connect_socket
    return conn

# 模型名映射：前缀 -> 网关真实模型名（按顺序匹配）
MODEL_MAP = [
    ("claude-opus", "google/gemini-3.8-flash"),
    ("claude-sonnet", "google/gemini-3.8-flash"),
    ("claude-haiku", "google/gemini-3.8-flash"),
    ("claude-fable", "google/gemini-3.8-flash"),
    ("opus", "google/gemini-3.8-flash"),
    ("sonnet", "google/gemini-3.8-flash"),
    ("haiku", "google/gemini-3.8-flash"),
]

GOOGLE_HINTS = ("google/", "gemini", "gemma")

# Google Gemini Schema proto 不接受的 JSON Schema 关键字（直接删除）
DROP_KEYS = {
    "$schema", "$id", "$comment", "$anchor", "$dynamicAnchor", "$dynamicRef", "$vocabulary",
    "$defs", "definitions", "id",
    "propertyNames", "patternProperties", "dependentSchemas", "dependentRequired", "dependencies",
    "if", "then", "else", "allOf", "not",
    "unevaluatedItems", "unevaluatedProperties", "prefixItems", "additionalItems",
    "contains", "minContains", "maxContains",
    "contentMediaType", "contentEncoding", "contentSchema",
    "examples", "example", "deprecated", "readOnly", "writeOnly",
}

SUFFIX_RE = re.compile(r"\[1m\]$", re.IGNORECASE)
STATS = {"requests": 0, "cleaned_nodes": 0, "mapped": 0}


def strip_1m(name: str) -> str:
    return SUFFIX_RE.sub("", name)


def map_model(name: str):
    """返回 (最终模型名, 是否发生了映射)。"""
    if not name:
        return name, False
    bare = strip_1m(name)
    low = bare.lower()
    for prefix, target in MODEL_MAP:
        if low.startswith(prefix):
            return target, (target != bare)
    return bare, (bare != name)


def is_google_model(name: str) -> bool:
    low = (name or "").lower()
    return any(h in low for h in GOOGLE_HINTS)


def clean_schema(node, counter):
    if isinstance(node, list):
        return [clean_schema(x, counter) for x in node]
    if not isinstance(node, dict):
        return node

    out = {}
    for k, v in node.items():
        if k in DROP_KEYS:
            counter[0] += 1
            continue
        if k == "const":
            out["enum"] = [v]
            counter[0] += 1
            continue
        if k == "oneOf":
            out["anyOf"] = [clean_schema(x, counter) for x in v]
            counter[0] += 1
            continue
        out[k] = clean_schema(v, counter)

    t = out.get("type")
    if isinstance(t, list) and "null" in t:
        rest = [x for x in t if x != "null"]
        counter[0] += 1
        if len(rest) == 1:
            out["type"] = rest[0]
        elif rest:
            out["type"] = rest
        else:
            out.pop("type", None)
        out["nullable"] = True

    branches = out.get("anyOf")
    if isinstance(branches, list) and any(
        isinstance(b, dict) and (b.get("type") == "null" or (isinstance(b.get("type"), list) and "null" in b["type"]))
        for b in branches
    ):
        keep = [b for b in branches
                if not (isinstance(b, dict) and (b.get("type") == "null" or (isinstance(b.get("type"), list) and "null" in b["type"])))]
        counter[0] += 1
        out["nullable"] = True
        if len(keep) == 1 and isinstance(keep[0], dict):
            merged = dict(keep[0])
            merged.update({k: v for k, v in out.items() if k != "anyOf"})
            out = merged
        else:
            out["anyOf"] = keep

    req = out.get("required")
    if isinstance(req, list):
        props = out.get("properties")
        if isinstance(props, dict):
            kept = [r for r in req if r in props]
            if len(kept) != len(req):
                counter[0] += 1
            if kept:
                out["required"] = kept
            else:
                out.pop("required", None)
        else:
            out.pop("required", None)
            counter[0] += 1

    return out


def process_body(body: bytes):
    """返回 (新 body 或 None, 信息)。None 表示原样转发。"""
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:
        return None, {}
    if not isinstance(data, dict):
        return None, {}

    raw_model = data.get("model") or ""
    mapped, changed = map_model(raw_model)
    google = is_google_model(mapped)

    counter = [0]
    if google or FORCE_CLEAN:
        tools = data.get("tools")
        if isinstance(tools, list):
            for t in tools:
                if not isinstance(t, dict):
                    continue
                if isinstance(t.get("input_schema"), dict):
                    t["input_schema"] = clean_schema(t["input_schema"], counter)
                fn = t.get("function")
                if isinstance(fn, dict) and isinstance(fn.get("parameters"), dict):
                    fn["parameters"] = clean_schema(fn["parameters"], counter)
                if isinstance(t.get("parameters"), dict):
                    t["parameters"] = clean_schema(t["parameters"], counter)

    if mapped and mapped != raw_model:
        data["model"] = mapped
        changed = True

    th = data.get("thinking")
    if isinstance(th, dict):
        thinking = th.get("type", "") + ("/" + str(th.get("budget_tokens")) if th.get("budget_tokens") else "")
    else:
        thinking = str(th) if th is not None else "-"
    oc = data.get("output_config")
    effort = oc.get("effort") if isinstance(oc, dict) else None
    info = {
        "model_in": raw_model,
        "model_out": mapped,
        "mapped": mapped != raw_model,
        "google": google,
        "tools": len(data.get("tools") or []) if isinstance(data.get("tools"), list) else 0,
        "stream": bool(data.get("stream")),
        "cleaned": counter[0],
        "thinking": thinking,
        "effort": effort or "-",
        "mt": data.get("max_tokens"),
    }
    if not changed and counter[0] == 0:
        return None, info
    return json.dumps(data, ensure_ascii=False).encode("utf-8"), info


def log(msg):
    print("[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "schema-sanitizer/1.1"

    def log_message(self, *args):
        pass

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _upstream_headers(self):
        hop = {"host", "content-length", "connection", "transfer-encoding", "keep-alive",
               "proxy-connection", "upgrade"}
        return {k: v for k, v in self.headers.items() if k.lower() not in hop}

    def _upstream_error(self, status, payload):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _relay(self, method, body):
        if not UPSTREAM:
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            payload = b'{"error":"AI upstream is not configured"}'
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            return
        up = urllib.parse.urlsplit(UPSTREAM)
        target = up.path.rstrip("/") + self.path
        started = time.time()
        conn = None
        try:
            conn, resp, request_id, attempts = request_upstream(
                lambda: create_upstream_connection(up), method, target, body,
                self._upstream_headers(), headers_timeout=HEADERS_TIMEOUT,
                stream_timeout=STREAM_TIMEOUT)
        except UpstreamUnavailable as error:
            self._upstream_error(502, error.payload())
            return

        first = b''
        if resp.status in (502, 503, 504):
            try:
                first = resp.read1(8192)
            except (OSError, http.client.HTTPException) as error:
                conn.close()
                record_event({'request_id': request_id, 'phase': 'error_body',
                              'cause': type(error).__name__, 'retry': False})
                self._upstream_error(resp.status, error_payload(resp.status, request_id, attempts))
                return
            if not first:
                conn.close()
                self._upstream_error(resp.status, error_payload(resp.status, request_id, attempts))
                return

        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() in ("content-length", "transfer-encoding", "connection", "keep-alive"):
                continue
            self.send_header(k, v)
        self.send_header("Connection", "close")
        self.send_header('X-Local-Gateway-Request-ID', request_id)
        self.end_headers()

        total = 0
        try:
            if first:
                self.wfile.write(first)
                self.wfile.flush()
                total += len(first)
            while True:
                chunk = resp.read1(8192)
                if not chunk:
                    break
                total += len(chunk)
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, http.client.HTTPException) as error:
            record_event({'request_id': request_id, 'phase': 'stream',
                          'cause': type(error).__name__, 'bytes': total, 'retry': False})
        finally:
            conn.close()
        log("  <- %s  %d bytes  %.2fs" % (resp.status, total, time.time() - started))

    def do_GET(self):
        if self.path.startswith("/__health"):
            payload = json.dumps({
                "ok": True, "configured": bool(UPSTREAM), "port": PORT,
                "upstream_dns_override_count": len(UPSTREAM_DNS_OVERRIDES),
                "upstream_dns_override_mode": "explicit",
                "upstream_connect_timeout_seconds": CONNECT_TIMEOUT,
                "upstream_headers_timeout_seconds": HEADERS_TIMEOUT,
                "upstream_stream_idle_seconds": STREAM_TIMEOUT,
                "upstream_upload_timeout_seconds": UPLOAD_TIMEOUT,
                "upstream_max_attempts": MAX_ATTEMPTS,
                "requests": STATS["requests"], "cleaned_nodes": STATS["cleaned_nodes"],
                "mapped_requests": STATS["mapped"], "model_map": MODEL_MAP,
            }, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        log("%s (GET)" % self.path)
        self._relay("GET", None)

    def do_HEAD(self):
        self._relay("HEAD", None)

    def do_DELETE(self):
        self._relay("DELETE", None)

    def _bridge_responses(self, data):
        """Translate Responses requests to Chat Completions for Gemini-compatible upstreams."""
        if not UPSTREAM:
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            return
        import responses_bridge as rb
        chat = rb.build_chat_request(data, clean_schema=clean_schema)
        up = urllib.parse.urlsplit(UPSTREAM)
        conn = None
        try:
            headers = self._upstream_headers()
            headers["Content-Type"] = "application/json"
            target = up.path.rstrip("/") + "/v1/chat/completions"
            conn, resp, request_id, attempts = request_upstream(
                lambda: create_upstream_connection(up), 'POST', target,
                json.dumps(chat, ensure_ascii=False).encode('utf-8'), headers,
                headers_timeout=HEADERS_TIMEOUT, stream_timeout=STREAM_TIMEOUT)
        except UpstreamUnavailable as error:
            self._upstream_error(502, error.payload())
            return

        if resp.status != 200:
            try:
                err = resp.read()
            except (OSError, http.client.HTTPException) as error:
                record_event({'request_id': request_id, 'phase': 'error_body',
                              'cause': type(error).__name__, 'retry': False})
                err = json.dumps(error_payload(resp.status, request_id, attempts)).encode('utf-8')
            finally:
                conn.close()
            log("bridge upstream HTTP %s (request_id=%s)" % (resp.status, request_id))
            if not err and resp.status in (502, 503, 504):
                err = json.dumps(error_payload(resp.status, request_id, attempts)).encode('utf-8')
            self.send_response(resp.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(err)
            return

        def chat_chunks():
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload == "[DONE]":
                    return
                try:
                    yield json.loads(payload)
                except Exception:
                    continue

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header('X-Local-Gateway-Request-ID', request_id)
        self.end_headers()
        total = 0
        try:
            for ev in rb.stream_events(chat_chunks(), chat["model"], data.get("instructions")):
                self.wfile.write(ev)
                self.wfile.flush()
                total += len(ev)
        except (OSError, http.client.HTTPException) as error:
            record_event({'request_id': request_id, 'phase': 'stream',
                          'cause': type(error).__name__, 'bytes': total, 'retry': False})
        finally:
            conn.close()
        log("  <- bridge 200  %d bytes" % total)

    def do_POST(self):
        body = self._read_body()
        # Codex 的 /v1/responses + google/gemini 模型 -> 走协议转换（兼容上游的 Responses 路径不支持 Gemini 时）
        if self.path.split("?")[0].rstrip("/") == "/v1/responses":
            try:
                _d = json.loads(body.decode("utf-8"))
            except Exception:
                _d = None
            if isinstance(_d, dict) and is_google_model(map_model(_d.get("model") or "")[0]):
                return self._bridge_responses(_d)
        new_body, info = process_body(body)
        STATS["requests"] += 1
        if info:
            if info.get("mapped"):
                STATS["mapped"] += 1
            STATS["cleaned_nodes"] += int(info.get("cleaned", 0))
        if new_body is not None:
            body = new_body
        if info:
            log("%s model=%s -> %s%s tools=%s stream=%s max_tokens=%s thinking=%s effort=%s 清洗=%s处" % (
                self.path, info.get("model_in"), info.get("model_out"),
                " (google)" if info.get("google") else "", info.get("tools"),
                info.get("stream"), info.get("mt"), info.get("thinking"), info.get("effort"),
                info.get("cleaned", 0)))
        else:
            log("%s (非 JSON 请求体，原样转发)" % self.path)
        if DUMP:
            os.makedirs(DUMP_DIR, exist_ok=True)
            with open(os.path.join(DUMP_DIR, time.strftime("%H%M%S") + "-" + str(STATS["requests"]) + ".json"), "wb") as f:
                f.write(body)
        self._relay("POST", body)


class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    log("AI proxy: http://127.0.0.1:%d -> configured upstream" % PORT)
    log("模型映射: " + ", ".join("%s* -> %s" % (p, t) for p, t in MODEL_MAP))
    ThreadedServer(("127.0.0.1", PORT), Handler).serve_forever()


