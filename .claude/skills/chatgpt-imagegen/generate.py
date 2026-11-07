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

    # Edit or use reference image(s):
    uv run --no-project --with curl_cffi python generate.py \
        --prompt "..." --image "/abs/path/input.png" --out "/abs/path/out.png"

The account credential defaults to ~/.codex/auth.json and can be overridden
with --auth-file or CHATGPT_AUTH_FILE. Expired access tokens are refreshed with
the stored refresh token and the file is updated atomically. Proxy settings
come from HTTPS_PROXY or the Windows system proxy. Timezone settings default to
the local timezone and can be overridden with --timezone/--timezone-offset-min.
Successful conversations are archived after download; use --no-archive to keep
one active. Nothing is printed except one JSON line on success (or an error on
stderr).
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
from urllib.parse import parse_qs, urlsplit

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


def warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr)


def fail(message: str, code: int = 1) -> "None":
    print(json.dumps({"ok": False, "error": message}, ensure_ascii=False), file=sys.stderr)
    raise SystemExit(code)


# ---------------------------------------------------------------- reference images

MAX_IMAGE_BYTES = 20 * 1024 * 1024
SUPPORTED_IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp"}


def system_proxy_url() -> str | None:
    """Return an explicit proxy for curl_cffi, or None to let libcurl decide.

    curl_cffi does not read the Windows system proxy. Without this, the
    chatgpt.com requests may work while the Azure upload host fails.
    """
    if any(os.environ.get(key) for key in
           ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")):
        return None
    if os.name != "nt":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            enabled = winreg.QueryValueEx(key, "ProxyEnable")[0]
            server = str(winreg.QueryValueEx(key, "ProxyServer")[0] or "")
    except (OSError, ImportError, ValueError):
        return None
    if not enabled or not server:
        return None
    entries = {}
    for item in server.split(";"):
        if "=" in item:
            name, value = item.split("=", 1)
            entries[name.strip().lower()] = value.strip()
    value = entries.get("https") or entries.get("http") or server
    if not value:
        return None
    return value if "://" in value else "http://" + value


def local_timezone() -> tuple[int, str]:
    """Return the JavaScript-style UTC offset and a local timezone name."""
    now = datetime.now().astimezone()
    offset = now.utcoffset() or timedelta(0)
    minutes = -int(offset.total_seconds() // 60)
    name = os.environ.get("TZ") or now.tzname() or "UTC"
    return minutes, name


def sniff_image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def image_dimensions(data: bytes, mime: str) -> tuple[int, int]:
    """Return (width, height) without adding Pillow as a dependency."""
    try:
        if mime == "image/png" and len(data) >= 24:
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        if mime == "image/webp" and len(data) >= 30:
            chunk = data[12:16]
            if chunk == b"VP8X":
                return (1 + int.from_bytes(data[24:27], "little"),
                        1 + int.from_bytes(data[27:30], "little"))
            if chunk == b"VP8L" and data[20] == 0x2F:
                bits = int.from_bytes(data[21:25], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            if chunk == b"VP8 " and len(data) >= 30:
                return (int.from_bytes(data[26:28], "little") & 0x3FFF,
                        int.from_bytes(data[28:30], "little") & 0x3FFF)
        if mime == "image/jpeg":
            i = 2
            sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                   0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                length = int.from_bytes(data[i + 2:i + 4], "big")
                if length < 2:
                    break
                if marker in sof:
                    return (int.from_bytes(data[i + 7:i + 9], "big"),
                            int.from_bytes(data[i + 5:i + 7], "big"))
                i += 2 + length
    except (IndexError, ValueError):
        pass
    return 1, 1


def load_reference_images(paths: list[str]) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = path.resolve()
        if not path.is_file():
            fail(f"reference image not found: {path}")
        try:
            data = path.read_bytes()
        except OSError as exc:
            fail(f"cannot read reference image {path}: {type(exc).__name__}")
        if not data:
            fail(f"reference image is empty: {path}")
        if len(data) > MAX_IMAGE_BYTES:
            fail(f"reference image is larger than 20 MiB: {path.name}")
        mime = sniff_image_mime(data)
        if mime not in SUPPORTED_IMAGE_MIMES:
            fail(f"reference image must be PNG, JPEG, or WebP: {path.name}")
        width, height = image_dimensions(data, mime)
        images.append({
            "path": path,
            "file_name": path.name,
            "data": data,
            "mime_type": mime,
            "size": len(data),
            "width": width,
            "height": height,
        })
    return images


def image_asset_pointer(image: dict[str, Any]) -> dict[str, Any]:
    file_id = image["file_id"]
    scheme = "sediment" if file_id.startswith("file_") else "file-service"
    return {
        "asset_pointer": f"{scheme}://{file_id}",
        "content_type": "image_asset_pointer",
        "height": image.get("height") or 1,
        "size_bytes": image.get("size") or 0,
        "width": image.get("width") or 1,
    }


def attachment_metadata(image: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": image["file_id"],
        "name": image.get("file_name") or "",
        "size": image.get("size") or 0,
        "mime_type": image.get("mime_type") or "",
        "width": image.get("width") or 1,
        "height": image.get("height") or 1,
    }


def build_user_message(prompt: str, images: list[dict[str, Any]]) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "developer_mode_connector_ids": [],
        "selected_github_repos": [],
        "selected_all_github_repos": False,
        "system_hints": ["picture_v2"],
        "serialization_metadata": {"custom_symbol_offsets": []},
    }
    content: dict[str, Any] = {"content_type": "text", "parts": [prompt]}
    if images:
        content = {
            "content_type": "multimodal_text",
            "parts": [*[image_asset_pointer(image) for image in images], prompt],
        }
        metadata["attachments"] = [attachment_metadata(image) for image in images]
    return {
        "id": str(uuid.uuid4()),
        "author": {"role": "user"},
        "create_time": time.time(),
        "content": content,
        "metadata": metadata,
    }


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


def load_access_token(verbose: bool = False, auth_file: str = "") -> str:
    configured = auth_file or os.environ.get("CHATGPT_AUTH_FILE") or ""
    path = (Path(configured).expanduser() if configured
            else Path.home() / ".codex" / "auth.json")
    if not path.exists():
        fail(f"ChatGPT auth file not found: {path}; run `codex login` or set --auth-file/CHATGPT_AUTH_FILE")
    try:
        auth = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(f"cannot read ChatGPT auth file {path}: {type(exc).__name__}")
    token = (auth.get("tokens") or {}).get("access_token") or ""
    if not token:
        fail(f"no ChatGPT access_token in {path}; run `codex login` or set --auth-file/CHATGPT_AUTH_FILE")
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
    def __init__(self, token: str, timezone_name: str = "",
                 timezone_offset_min: int | None = None) -> None:
        from curl_cffi import requests

        local_offset, local_name = local_timezone()
        self.token = token
        self.timezone_offset_min = (timezone_offset_min
                                    if timezone_offset_min is not None else local_offset)
        self.timezone_name = timezone_name or local_name
        self.session = requests.Session(
            impersonate="edge101", timeout=60, proxy=system_proxy_url())
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

    def _request_retry(self, method: str, url: str, attempts: int = 3, **kwargs: Any) -> Any:
        last: Exception | None = None
        for index in range(attempts):
            try:
                return self.session.request(method, url, **kwargs)
            except Exception as exc:
                last = exc
                if index + 1 < attempts:
                    time.sleep(0.8 * (index + 1))
        if last:
            raise last
        raise RuntimeError(f"{method} {url} failed without an exception")

    def bootstrap(self, verbose: bool = False) -> None:
        r = self._request_retry("GET", BASE + "/", headers={
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
        r = self._request_retry("POST", BASE + path,
                                headers=self.headers(path, {"Content-Type": "application/json"}),
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

    def _upload_blob(self, upload_url: str, image: dict[str, Any],
                     upload_headers: dict[str, str] | None) -> None:
        from curl_cffi import requests

        parts = urlsplit(upload_url)
        mime = image["mime_type"]
        data = image["data"]
        last: Exception | None = None
        for index, impersonate in enumerate(("edge101", "chrome124", "safari17_0")):
            session = self.session
            if index:
                session = requests.Session(
                    impersonate=impersonate, timeout=120, proxy=system_proxy_url())
            try:
                if parts.path.endswith("/estuary/upload_content_bytes"):
                    embedded = (parse_qs(parts.query).get("upload_url") or [""])[0]
                    if not embedded:
                        raise RuntimeError("combined upload URL has no embedded upload_url")
                    r = session.post(
                        upload_url,
                        data={"upload_url": embedded},
                        files={"file": (image["file_name"], data, mime)},
                        headers={"Origin": BASE, "Referer": BASE + "/"},
                        timeout=120,
                    )
                else:
                    headers = {"Content-Type": mime}
                    if "x-amz-algorithm=" not in parts.query.lower():
                        headers.update({
                            "x-ms-blob-type": "BlockBlob",
                            "x-ms-version": "2020-04-08",
                            "x-ms-blob-content-type": mime,
                        })
                    if upload_headers:
                        headers.update({str(k): str(v) for k, v in upload_headers.items()})
                    r = session.put(upload_url, data=data, headers=headers, timeout=120)
                if 200 <= r.status_code < 300:
                    return
                last = RuntimeError(f"blob upload failed: HTTP {r.status_code}")
                if 400 <= r.status_code < 500:
                    break
            except Exception as exc:
                last = exc
            if index + 1 < 3:
                time.sleep(0.8 * (index + 1))
        if last:
            raise last
        raise RuntimeError("blob upload failed without an exception")

    def _confirm_upload(self, image: dict[str, Any], req: dict[str, str]) -> tuple[bool, str]:
        path = "/backend-api/files/process_upload_stream"
        payload = {
            "entry_surface": "chat_composer",
            "file_id": image["file_id"],
            "file_name": image["file_name"],
            "index_for_retrieval": False,
            "metadata": {},
            "mime_type": image["mime_type"],
            "use_case": "multimodal",
        }
        r = self._request_retry(
            "POST", BASE + path, attempts=2,
            headers=self.conv_headers(path, req, accept="text/event-stream"),
            json=payload,
            stream=True,
            timeout=120,
        )
        if not 200 <= r.status_code < 300:
            detail = f"HTTP {r.status_code}"
            r.close()
            return False, detail
        event_name = ""
        completed = False
        error_name = ""
        try:
            for line in r.iter_lines():
                if not line:
                    continue
                if isinstance(line, bytes):
                    line = line.decode("utf-8", "replace")
                line = line.strip()
                if not line or line == "[DONE]":
                    continue
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                name = str(obj.get("event") or obj.get("name") or event_name or "")
                if obj.get("error"):
                    error_name = name or "upload processing error"
                    break
                if name.endswith(".error") or name.endswith(".failed") or name.endswith(".cancelled"):
                    error_name = name
                    break
                if name in ("file.processing.file_ready", "file.processing.completed"):
                    completed = True
                    if name == "file.processing.completed":
                        break
        finally:
            r.close()
        if error_name:
            return False, error_name
        return (True, "" if completed else "stream ended")

    def upload_image(self, image: dict[str, Any], req: dict[str, str],
                     verbose: bool = False) -> dict[str, Any]:
        path = "/backend-api/files"
        payload = {
            "client_resolved_mime_type": image["mime_type"],
            "entry_surface": "chat_composer",
            "file_name": image["file_name"],
            "file_size": image["size"],
            "reset_rate_limits": False,
            "timezone_offset_min": self.timezone_offset_min,
            "use_case": "multimodal",
        }
        r = self._request_retry("POST", BASE + path,
                                headers=self.conv_headers(path, req), json=payload)
        if not 200 <= r.status_code < 300:
            fail(f"file registration failed: HTTP {r.status_code} {r.text[:200]}")
        try:
            created = r.json()
        except Exception:
            fail("file registration returned invalid JSON")
        file_id = str(created.get("file_id") or "")
        upload_url = str(created.get("upload_url") or "")
        if not file_id or not upload_url:
            fail("file registration returned no file_id/upload_url")
        upload_headers = created.get("upload_headers")
        if not isinstance(upload_headers, dict):
            upload_headers = None
        uploaded = {**image, "file_id": file_id}
        log(f"uploading reference {image['file_name']} ({image['size']} bytes, "
            f"{image['width']}x{image['height']})", verbose)
        try:
            self._upload_blob(upload_url, uploaded, upload_headers)
        except Exception as exc:
            fail(f"reference image upload failed: {str(exc)[:200]}")
        uploaded.pop("data", None)
        ok, detail = self._confirm_upload(uploaded, req)
        if not ok:
            # Older web builds used this endpoint; keep it as a compatibility
            # fallback when process_upload_stream is unavailable.
            old_path = f"/backend-api/files/{file_id}/uploaded"
            old = self._request_retry(
                "POST", BASE + old_path, headers=self.conv_headers(old_path, req), json={})
            if not 200 <= old.status_code < 300:
                fail(f"reference image processing failed: {detail}; "
                     f"fallback HTTP {old.status_code}")
            log("reference image confirmed through legacy uploaded endpoint", verbose)
        else:
            log(f"reference image ready: {image['file_name']}", verbose)
        return uploaded

    def prepare(self, prompt: str, req: dict[str, str], model: str,
                images: list[dict[str, Any]] | None = None) -> str:
        path = "/backend-api/f/conversation/prepare"
        images = images or []
        message = build_user_message(prompt, images)
        partial_query = {
            "id": message["id"],
            "author": message["author"],
            "content": message["content"],
        }
        if images:
            partial_query["metadata"] = {"attachments": message["metadata"]["attachments"]}
        payload = {
            "action": "next", "fork_from_shared_post": False,
            "parent_message_id": str(uuid.uuid4()), "model": model,
            "client_prepare_state": "success",
            "timezone_offset_min": self.timezone_offset_min,
            "timezone": self.timezone_name,
            "conversation_mode": {"kind": "primary_assistant"},
            "system_hints": ["picture_v2"],
            "partial_query": partial_query,
            "supports_buffering": True, "supported_encodings": ["v1"],
            "client_contextual_info": {"app_name": "chatgpt.com"},
        }
        r = self._request_retry("POST", BASE + path,
                                headers=self.conv_headers(path, req), json=payload)
        if r.status_code != 200:
            fail(f"prepare failed: HTTP {r.status_code} {r.text[:300]}")
        return r.json().get("conduit_token", "")

    def generate(self, prompt: str, req: dict[str, str], conduit: str,
                 model: str, images: list[dict[str, Any]] | None = None
                 ) -> tuple[str, list[str], list[str]]:
        path = "/backend-api/f/conversation"
        payload = {
            "action": "next",
            "messages": [build_user_message(prompt, images or [])],
            "parent_message_id": str(uuid.uuid4()), "model": model,
            "client_prepare_state": "sent",
            "timezone_offset_min": self.timezone_offset_min,
            "timezone": self.timezone_name,
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
        r = self._request_retry(
            "POST", BASE + path, attempts=3,
            headers=self.conv_headers(path, req, conduit, "text/event-stream"),
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
            r = self._request_retry("GET", BASE + path,
                                    headers=self.headers(path, {"Accept": "application/json"}))
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
            r = self._request_retry("GET", BASE + path,
                                    headers=self.headers(path, {"Accept": "application/json"}))
            if r.status_code == 200:
                d = r.json()
                if d.get("download_url") or d.get("url"):
                    urls.append(d.get("download_url") or d.get("url"))
        if not urls:
            for sid in sed_ids:
                path = f"/backend-api/conversation/{conv_id}/attachment/{sid}/download"
                r = self._request_retry("GET", BASE + path,
                                        headers=self.headers(path, {"Accept": "application/json"}))
                if r.status_code == 200:
                    d = r.json()
                    if d.get("download_url") or d.get("url"):
                        urls.append(d.get("download_url") or d.get("url"))
        if not urls:
            fail("image was generated but no download URL was returned")
        r = self._request_retry("GET", urls[0], timeout=120)
        if r.status_code != 200 or not r.content:
            fail(f"image download failed: HTTP {r.status_code}")
        return r.content

    def archive_conversation(self, conv_id: str, req: dict[str, str],
                             verbose: bool = False) -> bool:
        path = f"/backend-api/conversation/{conv_id}"
        try:
            r = self._request_retry(
                "PATCH", BASE + path, attempts=2,
                headers=self.conv_headers(path, req), json={"is_archived": True})
        except Exception as exc:
            warn(f"could not archive conversation: {type(exc).__name__}")
            return False
        if not 200 <= r.status_code < 300:
            warn(f"could not archive conversation: HTTP {r.status_code}")
            return False
        log("conversation archived", verbose)
        return True


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate or edit one image through the ChatGPT web backend")
    ap.add_argument("--prompt", help="image prompt or edit instruction")
    ap.add_argument("--out", help="absolute path to the output image")
    ap.add_argument("--image", action="append", default=[],
                    help="reference image path for editing; repeatable")
    ap.add_argument("--auth-file", default="",
                    help="ChatGPT auth JSON; default: CHATGPT_AUTH_FILE or ~/.codex/auth.json")
    ap.add_argument("--model", default="auto", help="web model slug (default: auto)")
    ap.add_argument("--timeout", type=float, default=300.0, help="seconds to wait for the image")
    ap.add_argument("--timezone", default="",
                    help="timezone name; default: local timezone or TZ")
    ap.add_argument("--timezone-offset-min", type=int, default=None,
                    help="minutes behind UTC; default: local offset")
    ap.add_argument("--check", action="store_true", help="only verify auth and the sentinel handshake")
    ap.add_argument("--no-archive", action="store_true",
                    help="keep the ChatGPT conversation active after downloading the image")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    out = None
    references: list[dict[str, Any]] = []
    if not args.check:
        if not args.prompt or not args.out:
            ap.error("--prompt and --out are required unless --check is used")
        out = Path(args.out).expanduser()
        if not out.is_absolute():
            fail("--out must be an absolute path")
        if out.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
            fail("--out must end with .png/.jpg/.jpeg/.webp")
        if any(out.resolve() == Path(raw).expanduser().resolve() for raw in args.image):
            fail("--out must not overwrite a reference image")
        references = load_reference_images(args.image)
        out.parent.mkdir(parents=True, exist_ok=True)

    token = load_access_token(args.verbose, args.auth_file)
    client = Client(token, timezone_name=args.timezone,
                    timezone_offset_min=args.timezone_offset_min)
    client.bootstrap(args.verbose)
    req = client.requirements(args.verbose)
    if args.check:
        print(json.dumps({"ok": True, "check": "sentinel", "persona": req.get("persona", "")},
                         ensure_ascii=False))
        return
    uploads = [client.upload_image(image, req, args.verbose) for image in references]
    conduit = client.prepare(args.prompt, req, args.model, uploads)
    conv_id, file_ids, sed_ids = client.generate(args.prompt, req, conduit, args.model, uploads)
    if not conv_id:
        fail("conversation id was not returned; the turn may have been rejected")
    reference_ids = {image["file_id"] for image in uploads}
    file_ids = [x for x in file_ids if x not in reference_ids]
    sed_ids = [x for x in sed_ids if x not in reference_ids]
    if not file_ids and not sed_ids:
        log("no generated image id in stream; polling conversation", args.verbose)
        file_ids, sed_ids = client.poll_image(conv_id, args.timeout)
        file_ids = [x for x in file_ids if x not in reference_ids]
        sed_ids = [x for x in sed_ids if x not in reference_ids]
    if not file_ids and not sed_ids:
        fail(f"no image received within {args.timeout:g}s (conversation {conv_id})")
    data = client.download_image(conv_id, file_ids, sed_ids)
    out.write_bytes(data)
    archived = False
    if not args.no_archive:
        archived = client.archive_conversation(conv_id, req, args.verbose)
    print(json.dumps({"ok": True, "path": str(out), "bytes": len(data),
                      "conversation_id": conv_id, "model": args.model,
                      "references": len(uploads), "archived": archived},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
