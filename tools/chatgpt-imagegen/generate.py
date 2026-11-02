#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["curl_cffi"]
# ///
"""Generate one image through the ChatGPT web backend using ChatGPT OAuth.

This is the independent ChatGPT web path. It does not invoke Codex CLI and is
not a fallback for tools/codex-imagegen/generate.py.

Usage:
    uv run --no-project --with curl_cffi python generate.py \
        --prompt "..." --out "/abs/path/out.png"

The account credential is read from ~/.codex/auth.json. Expired access tokens
are refreshed with the stored refresh token and the file is updated atomically.
Nothing is printed except one JSON line on success (or an error on stderr).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

BASE = "https://chatgpt.com"
AUTH_URL = "https://auth.openai.com/oauth/token"
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CLIENT_VERSION = "prod-be885abbfcfe7b1f511e88b3003d9ee44757fbad"
CLIENT_BUILD = "5955942"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0")
CORES = [8, 16, 24, 32]
DOCUMENT_KEYS = ["_reactListeningo743lnnpvdg", "location"]
DEFAULT_POW_SCRIPT = BASE + "/backend-api/sentinel/sdk.js"


def log(msg: str, verbose: bool = False) -> None:
    if verbose:
        print(msg, file=sys.stderr)


def fail(message: str, code: int = 1) -> "None":
    print(json.dumps({"ok": False, "error": message}, ensure_ascii=False), file=sys.stderr)
    raise SystemExit(code)


# ---------------------------------------------------------------- auth

def _jwt_exp(token: str) -> int:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload)).get("exp") or 0)
    except Exception:
        return 0


def _save_auth(path: Path, auth: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(auth, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def refresh_auth(auth: dict[str, Any], path: Path, verbose: bool = False) -> dict[str, Any]:
    from curl_cffi import requests

    refresh_token = (auth.get("tokens") or {}).get("refresh_token")
    if not refresh_token:
        fail("ChatGPT refresh token missing; run `codex login` first")
    log("refreshing ChatGPT access token", verbose)
    try:
        r = requests.post(
            AUTH_URL,
            headers={"Content-Type": "application/json", "User-Agent": UA},
            json={"client_id": OAUTH_CLIENT_ID, "grant_type": "refresh_token",
                  "refresh_token": refresh_token, "scope": "openid profile email"},
            impersonate="chrome",
            timeout=30,
        )
    except Exception as exc:
        fail(f"token refresh request failed: {type(exc).__name__}")
    if r.status_code != 200:
        fail(f"token refresh failed ({r.status_code}); run `codex login`")
    data = r.json()
    access = data.get("access_token") or ""
    if not access:
        fail("token refresh returned no access_token; run `codex login`")
    tokens = auth.setdefault("tokens", {})
    tokens["access_token"] = access
    if data.get("refresh_token"):
        tokens["refresh_token"] = data["refresh_token"]
    if data.get("id_token"):
        tokens["id_token"] = data["id_token"]
    auth["last_refresh"] = datetime.now(timezone.utc).isoformat()
    try:
        _save_auth(path, auth)
    except Exception as exc:
        log(f"warning: could not persist refreshed token: {type(exc).__name__}", True)
    return auth


def load_access_token(verbose: bool = False) -> str:
    path = Path.home() / ".codex" / "auth.json"
    if not path.exists():
        fail("~/.codex/auth.json not found; run `codex login` first")
    try:
        auth = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(f"cannot read ~/.codex/auth.json: {type(exc).__name__}")
    token = (auth.get("tokens") or {}).get("access_token") or ""
    if not token:
        fail("no ChatGPT access_token in ~/.codex/auth.json; run `codex login`")
    if _jwt_exp(token) and _jwt_exp(token) <= time.time() + 60:
        auth = refresh_auth(auth, path, verbose)
        token = auth["tokens"]["access_token"]
    return token


# ---------------------------------------------------------------- proof of work

class ScriptSrcParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.script_sources: list[str] = []
        self.data_build = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        attrs_dict = dict(attrs)
        src = attrs_dict.get("src")
        if not src:
            return
        self.script_sources.append(src)
        match = re.search(r"c/[^/]*/_", src)
        if match:
            self.data_build = match.group(0)


def parse_pow_resources(html_content: str) -> tuple[list[str], str]:
    parser = ScriptSrcParser()
    parser.feed(html_content)
    script_sources = parser.script_sources or [DEFAULT_POW_SCRIPT]
    data_build = parser.data_build
    if not data_build:
        match = re.search(r'<html[^>]*data-build="([^"]*)"', html_content)
        if match:
            data_build = match.group(1)
    return script_sources, data_build


def _legacy_parse_time() -> str:
    now = datetime.now(timezone(timedelta(hours=-5)))
    return now.strftime("%a %b %d %Y %H:%M:%S") + " GMT-0500 (Eastern Standard Time)"


def build_pow_config(user_agent: str, script_sources: list[str], data_build: str) -> list[Any]:
    navigator_key = random.choice([
        "registerProtocolHandler−function registerProtocolHandler() { [native code] }",
        "storage−[object StorageManager]", "locks−[object LockManager]",
        "appCodeName−Mozilla", "permissions−[object Permissions]",
        "share−function share() { [native code] }", "webdriver−false",
        "vendor−Google Inc.", "mediaDevices−[object MediaDevices]",
        "vibrate−function vibrate() { [native code] }", "cookieEnabled−true",
        "product−Gecko", "onLine−true", "mimeTypes−[object MimeTypeArray]",
        "credentials−[object CredentialsContainer]", "keyboard−[object Keyboard]",
        "gpu−[object GPU]", "pdfViewerEnabled−true", "language−zh-CN",
        "hardwareConcurrency−32", "userAgentData−[object NavigatorUAData]",
    ])
    window_key = random.choice([
        "0", "window", "self", "document", "name", "location", "customElements",
        "history", "navigation", "innerWidth", "innerHeight", "scrollX", "scrollY",
        "visualViewport", "screenX", "screenY", "outerWidth", "outerHeight",
        "devicePixelRatio", "screen", "chrome", "navigator", "onresize",
        "performance", "crypto", "indexedDB", "sessionStorage", "localStorage",
        "scheduler", "alert", "atob", "btoa", "fetch", "matchMedia",
        "postMessage", "queueMicrotask", "requestAnimationFrame", "setInterval",
        "setTimeout", "caches", "__NEXT_DATA__", "__BUILD_MANIFEST",
    ])
    script_source = random.choice(script_sources) if script_sources else DEFAULT_POW_SCRIPT
    return [
        random.choice([3000, 4000, 5000]),
        _legacy_parse_time(),
        4294705152,
        0,
        user_agent,
        script_source,
        data_build,
        "en-US",
        "en-US,es-US,en,es",
        0,
        navigator_key,
        random.choice(DOCUMENT_KEYS),
        window_key,
        time.perf_counter() * 1000,
        str(uuid.uuid4()),
        "",
        random.choice(CORES),
        time.time() * 1000 - (time.perf_counter() * 1000),
    ]


def _pow_generate(seed: str, difficulty: str, config: list[Any], limit: int = 2_000_000) -> tuple[str, bool]:
    target = bytes.fromhex(difficulty)
    diff_len = len(difficulty) // 2
    seed_bytes = seed.encode()
    static_1 = (json.dumps(config[:3], separators=(",", ":"), ensure_ascii=False)[:-1] + ",").encode()
    static_2 = ("," + json.dumps(config[4:9], separators=(",", ":"), ensure_ascii=False)[1:-1] + ",").encode()
    static_3 = ("," + json.dumps(config[10:], separators=(",", ":"), ensure_ascii=False)[1:]).encode()
    for i in range(limit):
        final_json = static_1 + str(i).encode() + static_2 + str(i >> 1).encode() + static_3
        encoded = base64.b64encode(final_json)
        if hashlib.sha3_512(seed_bytes + encoded).digest()[:diff_len] <= target:
            return encoded.decode(), True
    fallback = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D" + base64.b64encode(f'"{seed}"'.encode()).decode()
    return fallback, False


def build_legacy_requirements_token(ua: str, scripts: list[str], data_build: str) -> str:
    seed = format(random.random())
    config = build_pow_config(ua, scripts, data_build)
    answer, _ = _pow_generate(seed, "0fffff", config)
    return "gAAAAAC" + answer


def build_proof_token(seed: str, difficulty: str, ua: str, scripts: list[str], data_build: str) -> str:
    config = build_pow_config(ua, scripts, data_build)
    answer, solved = _pow_generate(seed, difficulty, config)
    if not solved:
        raise RuntimeError(f"failed to solve proof token: difficulty={difficulty}")
    return "gAAAAAB" + answer


# ---------------------------------------------------------------- turnstile VM
# Ported from pansx/chatgpt2api (MIT). Used only when the server supplies a
# decodable `turnstile.dx` payload; the current web build may use a different
# encoding, in which case the token is empty and the server decides.

class OrderedMap:
    def __init__(self) -> None:
        self.keys: list[str] = []
        self.values: dict[str, Any] = {}

    def add(self, key: str, value: Any) -> None:
        if key not in self.values:
            self.keys.append(key)
        self.values[key] = value


def _ts(value: Any) -> str:
    if value is None:
        return "undefined"
    if isinstance(value, float):
        return str(value)
    if isinstance(value, str):
        special = {
            "window.Math": "[object Math]", "window.Reflect": "[object Reflect]",
            "window.performance": "[object Performance]",
            "window.localStorage": "[object Storage]",
            "window.Object": "function Object() { [native code] }",
            "window.Reflect.set": "function set() { [native code] }",
            "window.performance.now": "function () { [native code] }",
            "window.Object.create": "function create() { [native code] }",
            "window.Object.keys": "function keys() { [native code] }",
            "window.Math.random": "function random() { [native code] }",
        }
        return special.get(value, value)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return ",".join(value)
    return str(value)


def _xor(text: str, key: str) -> str:
    if not key:
        return text
    return "".join(chr(ord(ch) ^ ord(key[i % len(key)])) for i, ch in enumerate(text))


def solve_turnstile_token(dx: str, p: str) -> str | None:
    try:
        token_list = json.loads(_xor(base64.b64decode(dx).decode(), p))
    except Exception:
        return None
    if not isinstance(token_list, list):
        return None
    m: dict[Any, Any] = {}
    start_time = time.time()
    result = ""

    def f1(e, t): m[e] = _xor(_ts(m[e]), _ts(m[t]))
    def f2(e, t): m[e] = t
    def f3(e):
        nonlocal result
        result = base64.b64encode(e.encode()).decode()
    def f5(e, t):
        cur, inc = m[e], m[t]
        if isinstance(cur, (list, tuple)):
            m[e] = list(cur) + [inc]
        elif isinstance(cur, (str, float)) or isinstance(inc, (str, float)):
            m[e] = _ts(cur) + _ts(inc)
        else:
            m[e] = "NaN"
    def f6(e, t, n):
        tv, nv = m[t], m[n]
        if isinstance(tv, str) and isinstance(nv, str):
            v = f"{tv}.{nv}"
            m[e] = "https://chatgpt.com/" if v == "window.document.location" else v
    def f7(e, *args):
        target = m[e]; values = [m[a] for a in args]
        if isinstance(target, str) and target == "window.Reflect.set":
            obj, key_name, val = values
            obj.add(str(key_name), val)
        elif callable(target):
            target(*values)
    def f8(e, t): m[e] = m[t]
    def f14(e, t): m[e] = json.loads(m[t])
    def f15(e, t): m[e] = json.dumps(m[t])
    def f17(e, t, *args):
        call_args = [m[a] for a in args]; target = m[t]
        if target == "window.performance.now":
            elapsed_ns = time.time_ns() - int(start_time * 1e9)
            m[e] = (elapsed_ns + random.random()) / 1e6
        elif target == "window.Object.create":
            m[e] = OrderedMap()
        elif target == "window.Object.keys":
            if call_args and call_args[0] == "window.localStorage":
                m[e] = ["STATSIG_LOCAL_STORAGE_INTERNAL_STORE_V4",
                        "STATSIG_LOCAL_STORAGE_STABLE_ID", "client-correlated-secret",
                        "oai/apps/capExpiresAt", "oai-did",
                        "STATSIG_LOCAL_STORAGE_LOGGING_REQUEST",
                        "UiState.isNavigationCollapsed.1"]
        elif target == "window.Math.random":
            m[e] = random.random()
        elif callable(target):
            m[e] = target(*call_args)
    def f18(e): m[e] = base64.b64decode(_ts(m[e])).decode()
    def f19(e): m[e] = base64.b64encode(_ts(m[e]).encode()).decode()
    def f20(e, t, n, *args):
        if m[e] == m[t]:
            target = m[n]
            if callable(target):
                target(*[m[a] for a in args])
    def f21(*_): return
    def f23(e, t, *args):
        if m[e] is not None and callable(m[t]):
            m[t](*args)
    def f24(e, t, n):
        tv, nv = m[t], m[n]
        if isinstance(tv, str) and isinstance(nv, str):
            m[e] = f"{tv}.{nv}"

    m.update({1: f1, 2: f2, 3: f3, 5: f5, 6: f6, 7: f7, 8: f8, 9: token_list,
              10: "window", 14: f14, 15: f15, 16: p, 17: f17, 18: f18, 19: f19,
              20: f20, 21: f21, 23: f23, 24: f24})
    for token in token_list:
        try:
            fn = m.get(token[0])
            if callable(fn):
                fn(*token[1:])
        except Exception:
            continue
    return result or None


# ---------------------------------------------------------------- client

class Client:
    def __init__(self, token: str) -> None:
        from curl_cffi import requests

        self.token = token
        self.session = requests.Session(impersonate="edge101", timeout=60)
        self.session.headers.update({
            "User-Agent": UA,
            "Origin": BASE,
            "Referer": BASE + "/",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-US;q=0.7",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Priority": "u=1, i",
            "Sec-Ch-Ua": '"Microsoft Edge";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
            "Sec-Ch-Ua-Arch": '"x86"',
            "Sec-Ch-Ua-Bitness": '"64"',
            "Sec-Ch-Ua-Full-Version": '"143.0.3650.96"',
            "Sec-Ch-Ua-Full-Version-List": '"Microsoft Edge";v="143.0.3650.96", "Chromium";v="143.0.7499.147", "Not A(Brand";v="24.0.0.0"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Model": '""',
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Ch-Ua-Platform-Version": '"19.0.0"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "OAI-Device-Id": str(uuid.uuid4()),
            "OAI-Session-Id": str(uuid.uuid4()),
            "OAI-Language": "zh-CN",
            "OAI-Client-Version": CLIENT_VERSION,
            "OAI-Client-Build-Number": CLIENT_BUILD,
            "Authorization": "Bearer " + token,
        })
        self.scripts: list[str] = []
        self.data_build = ""

    def headers(self, path: str, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = dict(self.session.headers)
        h["X-OpenAI-Target-Path"] = path
        h["X-OpenAI-Target-Route"] = path
        if extra:
            h.update(extra)
        return h

    def bootstrap(self, verbose: bool = False) -> None:
        r = self.session.get(BASE + "/", headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        })
        if r.status_code != 200:
            fail(f"bootstrap failed: HTTP {r.status_code}")
        self.scripts, self.data_build = parse_pow_resources(r.text)
        log(f"bootstrap ok ({len(self.scripts)} scripts, build={self.data_build[:32]})", verbose)

    def requirements(self, verbose: bool = False) -> dict[str, str]:
        path = "/backend-api/sentinel/chat-requirements"
        p = build_legacy_requirements_token(UA, self.scripts, self.data_build)
        r = self.session.post(BASE + path, headers=self.headers(path, {"Content-Type": "application/json"}),
                              json={"p": p})
        if r.status_code != 200:
            fail(f"chat-requirements failed: HTTP {r.status_code} {r.text[:200]}")
        data = r.json()
        proof = data.get("proofofwork") or {}
        turnstile = data.get("turnstile") or {}
        proof_token = ""
        if proof.get("required"):
            proof_token = build_proof_token(proof.get("seed", ""), proof.get("difficulty", ""),
                                            UA, self.scripts, self.data_build)
        turnstile_token = ""
        if turnstile.get("required") and turnstile.get("dx"):
            turnstile_token = solve_turnstile_token(turnstile["dx"], "") or ""
        log(f"requirements: persona={data.get('persona')} pow={bool(proof.get('required'))} "
            f"turnstile={bool(turnstile.get('required'))} solved={bool(turnstile_token)}", verbose)
        if not data.get("token"):
            fail("chat requirements returned no token")
        return {"token": data["token"], "proof": proof_token,
                "turnstile": turnstile_token, "so": data.get("so_token", ""),
                "persona": data.get("persona", "")}

    def conv_headers(self, path: str, req: dict[str, str], conduit: str = "",
                     accept: str = "*/*") -> dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": accept,
             "OpenAI-Sentinel-Chat-Requirements-Token": req["token"]}
        if req["proof"]:
            h["OpenAI-Sentinel-Proof-Token"] = req["proof"]
        if req["turnstile"]:
            h["OpenAI-Sentinel-Turnstile-Token"] = req["turnstile"]
        if conduit:
            h["X-Conduit-Token"] = conduit
        if accept == "text/event-stream":
            h["X-Oai-Turn-Trace-Id"] = str(uuid.uuid4())
        return self.headers(path, h)

    def prepare(self, prompt: str, req: dict[str, str], model: str) -> str:
        path = "/backend-api/f/conversation/prepare"
        payload = {
            "action": "next", "fork_from_shared_post": False,
            "parent_message_id": str(uuid.uuid4()), "model": model,
            "client_prepare_state": "success", "timezone_offset_min": -480,
            "timezone": "Asia/Shanghai",
            "conversation_mode": {"kind": "primary_assistant"},
            "system_hints": ["picture_v2"],
            "partial_query": {"id": str(uuid.uuid4()), "author": {"role": "user"},
                              "content": {"content_type": "text", "parts": [prompt]}},
            "supports_buffering": True, "supported_encodings": ["v1"],
            "client_contextual_info": {"app_name": "chatgpt.com"},
        }
        r = self.session.post(BASE + path, headers=self.conv_headers(path, req), json=payload)
        if r.status_code != 200:
            fail(f"prepare failed: HTTP {r.status_code} {r.text[:300]}")
        return r.json().get("conduit_token", "")

    def generate(self, prompt: str, req: dict[str, str], conduit: str,
                 model: str) -> tuple[str, list[str], list[str]]:
        path = "/backend-api/f/conversation"
        payload = {
            "action": "next",
            "messages": [{"id": str(uuid.uuid4()), "author": {"role": "user"},
                          "create_time": time.time(),
                          "content": {"content_type": "text", "parts": [prompt]},
                          "metadata": {"developer_mode_connector_ids": [],
                                       "selected_github_repos": [],
                                       "selected_all_github_repos": False,
                                       "system_hints": ["picture_v2"],
                                       "serialization_metadata": {"custom_symbol_offsets": []}}}],
            "parent_message_id": str(uuid.uuid4()), "model": model,
            "client_prepare_state": "sent", "timezone_offset_min": -480,
            "timezone": "Asia/Shanghai",
            "conversation_mode": {"kind": "primary_assistant"},
            "enable_message_followups": True, "system_hints": ["picture_v2"],
            "supports_buffering": True, "supported_encodings": ["v1"],
            "client_contextual_info": {"is_dark_mode": False, "time_since_loaded": 1200,
                                       "page_height": 1072, "page_width": 1724,
                                       "pixel_ratio": 1.2, "screen_height": 1440,
                                       "screen_width": 2560, "app_name": "chatgpt.com"},
            "paragen_cot_summary_display_override": "allow",
            "force_parallel_switch": "auto",
        }
        r = self.session.post(BASE + path, headers=self.conv_headers(path, req, conduit, "text/event-stream"),
                              json=payload, stream=True)
        if r.status_code != 200:
            detail = r.text[:300]
            fail(f"conversation failed: HTTP {r.status_code} {detail}")
        conv_id = ""
        file_ids: list[str] = []
        sed_ids: list[str] = []
        file_pat = re.compile(r"file-service://([A-Za-z0-9_-]+)")
        sed_pat = re.compile(r"sediment://([A-Za-z0-9_-]+)")
        for line in r.iter_lines():
            if not line:
                continue
            if isinstance(line, bytes):
                line = line.decode("utf-8", "replace")
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if raw == "[DONE]":
                break
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            conv_id = obj.get("conversation_id") or conv_id
            v = obj.get("v")
            msg = obj.get("message") or (v.get("message") if isinstance(v, dict) else None) or {}
            if not isinstance(msg, dict):
                msg = {}
            conv_id = msg.get("conversation_id") or conv_id
            content = msg.get("content") or {}
            parts = content.get("parts") or []
            joined = " ".join(p.get("asset_pointer", "") if isinstance(p, dict) else
                              (p if isinstance(p, str) else "") for p in parts)
            file_ids += [x for x in file_pat.findall(joined) if x not in file_ids]
            sed_ids += [x for x in sed_pat.findall(joined) if x not in sed_ids]
        r.close()
        return conv_id, file_ids, sed_ids

    def poll_image(self, conv_id: str, timeout: float) -> tuple[list[str], list[str]]:
        file_pat = re.compile(r"file-service://([A-Za-z0-9_-]+)")
        sed_pat = re.compile(r"sediment://([A-Za-z0-9_-]+)")
        start = time.time()
        while time.time() - start < timeout:
            path = f"/backend-api/conversation/{conv_id}"
            r = self.session.get(BASE + path, headers=self.headers(path, {"Accept": "application/json"}))
            if r.status_code == 200:
                file_ids: list[str] = []
                sed_ids: list[str] = []
                for node in (r.json().get("mapping") or {}).values():
                    msg = (node or {}).get("message") or {}
                    if (msg.get("author") or {}).get("role") != "tool":
                        continue
                    for part in (msg.get("content") or {}).get("parts") or []:
                        text = part.get("asset_pointer", "") if isinstance(part, dict) else (
                            part if isinstance(part, str) else "")
                        file_ids += [x for x in file_pat.findall(text) if x not in file_ids]
                        sed_ids += [x for x in sed_pat.findall(text) if x not in sed_ids]
                if file_ids or sed_ids:
                    return file_ids, sed_ids
            time.sleep(4)
        return [], []

    def download_image(self, conv_id: str, file_ids: list[str], sed_ids: list[str]) -> bytes:
        urls: list[str] = []
        for fid in file_ids:
            if fid == "file_upload":
                continue
            path = f"/backend-api/files/{fid}/download"
            r = self.session.get(BASE + path, headers=self.headers(path, {"Accept": "application/json"}))
            if r.status_code == 200:
                d = r.json()
                if d.get("download_url") or d.get("url"):
                    urls.append(d.get("download_url") or d.get("url"))
        if not urls:
            for sid in sed_ids:
                path = f"/backend-api/conversation/{conv_id}/attachment/{sid}/download"
                r = self.session.get(BASE + path, headers=self.headers(path, {"Accept": "application/json"}))
                if r.status_code == 200:
                    d = r.json()
                    if d.get("download_url") or d.get("url"):
                        urls.append(d.get("download_url") or d.get("url"))
        if not urls:
            fail("image was generated but no download URL was returned")
        r = self.session.get(urls[0], timeout=120)
        if r.status_code != 200 or not r.content:
            fail(f"image download failed: HTTP {r.status_code}")
        return r.content


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate one image through the ChatGPT web backend")
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--out", required=True, help="absolute path to the output .png")
    ap.add_argument("--model", default="auto", help="web model slug (default: auto)")
    ap.add_argument("--timeout", type=float, default=300.0, help="seconds to wait for the image")
    ap.add_argument("--check", action="store_true", help="only verify auth and the sentinel handshake")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    out = Path(args.out).expanduser()
    if not out.is_absolute():
        fail("--out must be an absolute path")
    if out.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
        fail("--out must end with .png/.jpg/.jpeg/.webp")
    out.parent.mkdir(parents=True, exist_ok=True)

    token = load_access_token(args.verbose)
    client = Client(token)
    client.bootstrap(args.verbose)
    req = client.requirements(args.verbose)
    if args.check:
        print(json.dumps({"ok": True, "check": "sentinel", "persona": req.get("persona", "")},
                         ensure_ascii=False))
        return
    conduit = client.prepare(args.prompt, req, args.model)
    conv_id, file_ids, sed_ids = client.generate(args.prompt, req, conduit, args.model)
    if not conv_id:
        fail("conversation id was not returned; the turn may have been rejected")
    if not file_ids and not sed_ids:
        log("no image id in stream; polling conversation", args.verbose)
        file_ids, sed_ids = client.poll_image(conv_id, args.timeout)
    if not file_ids and not sed_ids:
        fail(f"no image received within {args.timeout:g}s (conversation {conv_id})")
    data = client.download_image(conv_id, file_ids, sed_ids)
    out.write_bytes(data)
    print(json.dumps({"ok": True, "path": str(out), "bytes": len(data),
                      "conversation_id": conv_id, "model": args.model}, ensure_ascii=False))


if __name__ == "__main__":
    main()
