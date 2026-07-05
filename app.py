#!/usr/bin/env python3
"""
Qwen Unified OpenAPI — Render 部署版
=====================================
兼容 OpenAI /v1/chat/completions
底层: Playwright 浏览器自动化 → Qwen 网页端
部署: gunicorn app:app
端口: PORT 环境变量
"""

import asyncio
import base64
import json
import os
import re
import shutil
import threading
import time
import uuid
from io import BytesIO
from pathlib import Path
from wsgiref.simple_server import make_server

# ============ 常量 ============
BASE = "https://chat.qwen.ai"
GUEST = BASE + "/c/guest"
VERSION = "0.4.0-render"
UA = (
    "Mozilla/5.0 (Linux; Android 15; V2301A Build/AP3A.240905.015.A2; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
    "Chrome/121.0.6167.71 Mobile Safari/537.36"
)
PREFERRED_MODELS = [
    "qwen3.7-plus", "qwen3.7-max", "qwen3.6-plus",
    "qwen3.5-max", "qwen3.5-plus", "qwen3-max", "qwen3-plus",
]
BROWSER_WAIT_TIMEOUT = 30
MAX_CONCURRENT = 3

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qwen_data")
TMP_DIR = f"{DATA_DIR}/tmp_chromium_data"
TOKEN_POOL = f"{DATA_DIR}/token_pool.json"

# ============ 工具 ============

def uid(): return str(uuid.uuid4())
def ts(): return int(time.time())

def jpath(path, default=None):
    Path(os.path.dirname(path)).mkdir(parents=True, exist_ok=True)
    return path

def load_json(path, default=None):
    try: return json.loads(Path(path).read_text(encoding="utf-8"))
    except: return default or {}

def save_json(path, data):
    p = jpath(path)
    Path(p).write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))

def log(tag, msg): print(f"[{tag}] {msg}", flush=True)

# ============ 浏览器参数获取 ============

_browser_sem = asyncio.Semaphore(MAX_CONCURRENT)

def prep_uddir(bid):
    d = f"{TMP_DIR}_{bid}"
    if Path(d).exists(): shutil.rmtree(d, ignore_errors=True)
    Path(d).mkdir(parents=True, exist_ok=True)
    return d

def clean_uddir(bid):
    d = f"{TMP_DIR}_{bid}"
    if Path(d).exists(): shutil.rmtree(d, ignore_errors=True)


async def fetch_browser_params(headless=True, debug=True, browser_id=None):
    """Playwright 打开 Qwen 提取 cookies / tokens"""
    bid = browser_id or uid()[:8]
    async with _browser_sem:
        udd = prep_uddir(bid)
        try:
            async with __import__("playwright.async_api").async_api.async_playwright() as p:
                ctx = await p.chromium.launch_persistent_context(
                    user_data_dir=udd,
                    headless=headless,
                    args=[
                        "--no-sandbox", "--disable-setuid-sandbox",
                        "--disable-blink-features=AutomationControlled",
                        "--disable-dev-shm-usage",
                        "--disable-application-cache", "--disable-cache",
                        "--disk-cache-size=0", "--no-first-run",
                    ],
                    user_agent=UA,
                    viewport={"width": 390, "height": 844},
                    is_mobile=True, locale="zh-CN",
                    bypass_csp=True, ignore_https_errors=True,
                    device_scale_factor=3,
                )
                await ctx.clear_cookies()
                pg = await ctx.new_page()

                hdr_cookies = {}
                async def on_resp(r):
                    sc = r.headers.get("set-cookie") or r.headers.get("Set-Cookie") or ""
                    if sc:
                        parts = sc.split(";")[0]
                        if "=" in parts:
                            k, v = parts.split("=", 1)
                            hdr_cookies[k.strip()] = v.strip()
                pg.on("response", on_resp)

                try:
                    await pg.goto(GUEST, wait_until="domcontentloaded", timeout=60000)
                except Exception as e:
                    if debug: log("browser", f"goto: {e}")

                cookies = {}
                for i in range(BROWSER_WAIT_TIMEOUT):
                    ck = await ctx.cookies()
                    cookies = {x["name"]: x["value"] for x in ck}
                    if cookies.get("cbc") and cookies.get("tfstk"):
                        break
                    await pg.wait_for_timeout(1000)

                html = await pg.content()
                m = re.search(r"qwen-chat-fe/([0-9.]+)", html)
                fe_ver = m.group(1) if m else VERSION

                await ctx.close()
        finally:
            clean_uddir(bid)

    if not cookies.get("cbc"):
        cookies["cbc"] = hdr_cookies.get("cbc", "")

    bx = cookies.get("cbc", "")
    if not bx:
        raise RuntimeError(f"cbc cookie missing. have={list(cookies.keys())}")

    if debug: log("browser", f"bx={bx[:24]}... c={len(cookies)}")
    return {
        "version": fe_ver,
        "cookies": cookies,
        "bx_umidtoken": bx,
        "created_at": ts(),
    }


# ============ 身份池 ============

class IdentityPool:
    def __init__(self, headless=True, debug=True):
        self.headless = headless
        self.debug = debug
        self.lock = threading.Lock()
        self.identities = []
        self.idx = 0
        self.refresh_count = 0
        self._load()

    def _load(self):
        try:
            d = load_json(TOKEN_POOL)
            for item in d.get("identities", []):
                self.identities.append(item)
            if self.debug: log("pool", f"loaded {len(self.identities)}")
        except: pass

    def _save(self):
        save_json(TOKEN_POOL, {
            "identities": self.identities,
            "refresh_count": self.refresh_count,
            "updated_at": ts(),
        })

    def active(self):
        with self.lock:
            for i in range(len(self.identities)):
                ix = (self.idx + i) % len(self.identities)
                it = self.identities[ix]
                if it.get("active", True) and not it.get("rate_limited"):
                    self.idx = ix
                    return it
            return None

    def rate_limit(self, bx):
        with self.lock:
            for it in self.identities:
                if it["bx_umidtoken"] == bx:
                    it["rate_limited"] = ts()
                    it["active"] = False
                    self._save()
                    if self.debug: log("pool", f"rate-limited {bx[:24]}...")
                    return

    def bump(self, bx):
        with self.lock:
            for it in self.identities:
                if it["bx_umidtoken"] == bx:
                    it["chat_count"] = it.get("chat_count", 0) + 1
                    self._save()
                    return

    def refresh(self):
        if self.debug:
            log("pool", f"🔄 refreshing identity (have {len(self.identities)})...")
        try:
            params = asyncio.run(fetch_browser_params(
                headless=self.headless, debug=self.debug,
            ))
        except Exception as e:
            if self.debug: log("pool", f"browser error: {e}")
            raise

        identity = {
            "bx_umidtoken": params["bx_umidtoken"],
            "cookies": params["cookies"],
            "version": params["version"],
            "created_at": params["created_at"],
            "chat_count": 0,
            "active": True,
            "rate_limited": None,
        }

        with self.lock:
            self.identities.append(identity)
            self.idx = len(self.identities) - 1
            self.refresh_count += 1
            self._save()

        if self.debug: log("pool", f"✅ new identity: {identity['bx_umidtoken'][:24]}...")
        return identity

    def ensure(self):
        it = self.active()
        if it: return it
        return self.refresh()

    def stats(self):
        with self.lock:
            total = len(self.identities)
            active = sum(1 for i in self.identities if i.get("active"))
            rl = sum(1 for i in self.identities if i.get("rate_limited"))
            chats = sum(i.get("chat_count", 0) for i in self.identities)
            return {"identities_total": total, "active": active,
                    "rate_limited": rl, "total_chats": chats,
                    "refreshes": self.refresh_count}


# ============ Qwen 客户端 ============

class QwenClient:
    def __init__(self, identity):
        self.id = identity
        self.s = __import__("requests").Session()
        for k, v in identity["cookies"].items():
            self.s.cookies.set(k, v, domain="chat.qwen.ai")
        self.model = "qwen3.7-plus"
        self.chat_id = None
        self.parent_id = None

    def hdr(self):
        return {
            "User-Agent": UA, "Referer": BASE + "/", "Origin": BASE,
            "source": "h5", "Version": self.id["version"],
            "Accept": "application/json", "Content-Type": "application/json",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "X-Request-Id": uid(),
            "Timezone": time.strftime("%a %b %d %Y %H:%M:%S GMT+0800", time.localtime()),
            "bx-v": "2.5.36",
            "bx-umidtoken": self.id["bx_umidtoken"],
            "X-Accel-Buffering": "no",
            "sec-ch-ua": '"Not A(Brand";v="99", "Android WebView";v="121", "Chromium";v="121"',
            "sec-ch-ua-platform": '"Android"', "sec-ch-ua-mobile": "?1",
            "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty",
        }

    def init_api(self):
        for path, method, body in [
            ("/api/v2/models/", "GET", None),
            ("/api/v2/users/status", "POST", {}),
        ]:
            try:
                r = self.s.request(method, BASE + path, headers=self.hdr(),
                                   json=body, timeout=15)
                if path == "/api/v2/models/" and r.ok:
                    try:
                        arr = r.json().get("data", {}).get("data", [])
                        ids = [x.get("id") for x in arr if isinstance(x, dict)]
                        for m in PREFERRED_MODELS:
                            if m in ids:
                                self.model = m
                                break
                    except: pass
            except: pass

    def new_chat(self):
        body = {"title": "New Chat", "models": [self.model],
                "chat_mode": "normal", "chat_type": "t2t",
                "timestamp": int(time.time() * 1000), "project_id": ""}
        r = self.s.post(BASE + "/api/v2/chats/new", headers=self.hdr(), json=body, timeout=20)
        d = r.json()
        if not d.get("success"):
            raise RuntimeError(f"chat create failed: {(d.get('data') or {}).get('code', 'unknown')}")
        self.chat_id = d["data"]["id"]
        self.parent_id = None
        return self.chat_id

    def chat(self, text, images=None, search=False, thinking=True):
        if not self.chat_id: self.new_chat()

        url = BASE + "/api/v2/chat/completions?chat_id=" + str(self.chat_id)
        t = ts()
        files = []
        if images:
            for img in images:
                files.append({"type": "image", "file_id": img.get("file_id", ""),
                              "url": "", "name": f"img_{uid()[:6]}.png",
                              "size": 0, "status": "uploaded"})

        msg = {
            "fid": uid(), "parentId": self.parent_id, "childrenIds": [uid()],
            "role": "user", "content": text + "\n", "user_action": "chat",
            "files": files, "timestamp": t, "models": [self.model],
            "chat_type": "t2t",
            "feature_config": {
                "thinking_enabled": thinking, "output_schema": "phase",
                "research_mode": "normal", "auto_thinking": thinking,
                "thinking_mode": "Auto", "thinking_format": "summary",
                "auto_search": search,
            },
            "extra": {"meta": {"subChatType": "t2t"}},
            "sub_chat_type": "t2t", "parent_id": self.parent_id,
        }

        payload = {"stream": True, "version": "2.1", "incremental_output": True,
                   "chat_id": self.chat_id, "chat_mode": "normal",
                   "model": self.model, "parent_id": self.parent_id,
                   "messages": [msg], "timestamp": t}

        r = self.s.post(url, headers=self.hdr(), json=payload, stream=True, timeout=180)

        ct = r.headers.get("content-type", "")
        if "text/event-stream" not in ct:
            body = r.text
            rate_code = "RateLimited" if "RateLimited" in body else None
            return False, body, rate_code

        answer, thinking_parts, new_parent = [], [], None
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"): continue
            raw = line[5:].strip()
            if not raw or raw == "[DONE]": continue
            try: d = json.loads(raw)
            except: continue
            if "response.created" in d:
                new_parent = d["response.created"].get("response_id")
            if "choices" in d and d["choices"]:
                delta = d["choices"][0].get("delta", {})
                phase = delta.get("phase", "")
                content = delta.get("content", "")
                if phase == "answer" and content: answer.append(content)
                elif phase == "thinking" and content: thinking_parts.append(content)

        if new_parent: self.parent_id = new_parent
        result = "".join(answer) or "".join(thinking_parts)
        return True, result, None


# ============ 引擎 ============

class QwenEngine:
    def __init__(self, headless=True, debug=False):
        self.headless = headless
        self.debug = debug
        self.pool = IdentityPool(headless=headless, debug=debug)

    def chat(self, text, image_url=None, search=False, thinking=True, max_retries=5):
        images = None
        if image_url:
            try:
                b64, mime = _process_image(image_url)
                images = [{"base64": b64, "mime": mime, "file_id": ""}]
            except Exception as e:
                return {"success": False, "error": f"image error: {e}"}

        for attempt in range(1, max_retries + 1):
            identity = self.pool.ensure()
            client = QwenClient(identity)
            try:
                client.init_api()
                client.new_chat()
                if images:
                    for img in images:
                        if not img.get("file_id"):
                            try:
                                img["file_id"] = _upload_image(client.s, client.hdr(),
                                                               img["base64"], img["mime"]) or ""
                            except: pass
                ok, result, rate_code = client.chat(text, images=images,
                                                     search=search, thinking=thinking)
            except Exception as e:
                if self.debug: log("engine", f"attempt {attempt} err: {e}")
                try: self.pool.refresh()
                except: pass
                continue

            if ok:
                self.pool.bump(identity["bx_umidtoken"])
                return {"success": True, "result": result,
                        "identity": identity["bx_umidtoken"][:24] + "...",
                        "attempts": attempt, "chat_id": client.chat_id,
                        "response_id": client.parent_id, "model": client.model,
                        "has_images": bool(images), "search_enabled": search}

            if rate_code == "RateLimited":
                if self.debug: log("engine", "rate limited, switching identity")
                self.pool.rate_limit(identity["bx_umidtoken"])
                continue
            try: self.pool.refresh()
            except: pass

        return {"success": False, "error": f"failed after {max_retries} attempts"}

    def stats(self): return self.pool.stats()


# ============ 图片处理 ============

def _process_image(url):
    if url.startswith("data:"):
        m = re.match(r"data:(image/\w+);base64,(.+)", url, re.DOTALL)
        if m: return m.group(2), m.group(1)
        raise ValueError("bad data URL")
    if url.startswith("http"):
        import requests
        data = requests.get(url, timeout=30).content
        mime = _detect_mime(data)
        return base64.b64encode(data).decode(), mime
    if os.path.isfile(url):
        data = Path(url).read_bytes()
        return base64.b64encode(data).decode(), _detect_mime(data)
    return url, "image/png"


def _detect_mime(data):
    if data[:8] == b'\x89PNG\r\n\x1a\n': return "image/png"
    if data[:3] == b'\xff\xd8\xff': return "image/jpeg"
    if data[:4] == b'GIF8': return "image/gif"
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP': return "image/webp"
    return "image/png"


def _upload_image(session, headers, b64_data, mime):
    try:
        img_bytes = base64.b64decode(b64_data)
        ext = mime.split("/")[-1].replace("jpeg", "jpg")
        fn = f"upload_{uid()[:8]}.{ext}"
        uh = {k: v for k, v in headers.items() if k not in ("Content-Type",)}
        uh["Accept"] = "application/json"
        r = session.post(BASE + "/api/v2/files/upload", headers=uh,
                         files={"file": (fn, img_bytes, mime)}, timeout=30)
        if r.ok:
            fid = r.json().get("data", {}).get("id", "")
            if fid:
                log("image", f"uploaded: {fid}")
                return fid
    except Exception as e:
        log("image", f"upload fail: {e}")
    return None


# ============ WSGI 应用 ============

_engine = None

def _get_engine():
    global _engine
    if _engine is None:
        _engine = QwenEngine(headless=True, debug=True)
    return _engine


def _json_resp(status, data):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    return status, [("Content-Type", "application/json"),
                    ("Access-Control-Allow-Origin", "*")], body


def _stream_resp(chunks):
    def gen():
        for c in chunks:
            yield f"data: {c}\n\n".encode()
        yield b"data: [DONE]\n\n"
    return 200, [("Content-Type", "text/event-stream"),
                 ("Cache-Control", "no-cache"),
                 ("Access-Control-Allow-Origin", "*"),
                 ("X-Accel-Buffering", "no")], gen()


def _chunkify(text, sz=5):
    if not text: return [""]
    return [text[i:i+sz] for i in range(0, len(text), sz)]


def wsgi_app(environ, start_response):
    path = environ.get("PATH_INFO", "").split("?")[0]
    method = environ.get("REQUEST_METHOD", "GET")

    # CORS preflight
    if method == "OPTIONS":
        h = [("Access-Control-Allow-Origin", "*"),
             ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
             ("Access-Control-Allow-Headers", "Content-Type, Authorization")]
        start_response("200 OK", h)
        return [b""]

    st, h, b = _dispatch(method, path, environ)
    start_response(st, h)
    if hasattr(b, "__iter__"): return b
    return [b] if isinstance(b, bytes) else [str(b).encode()]


def _dispatch(method, path, env):
    engine = _get_engine()

    if method == "GET":
        if path == "/":
            return _json_resp("200 OK", {"service": "Qwen Unified API",
                                          "version": VERSION, "engine": "qwen-web"})
        if path == "/v1/models":
            models = [
                {"id": m, "object": "model", "created": ts(), "owned_by": "qwen"}
                for m in PREFERRED_MODELS
            ]
            models.append({"id": "auto", "object": "model", "created": ts(), "owned_by": "qwen"})
            return _json_resp("200 OK", {"object": "list", "data": models})
        if path == "/health":
            return _json_resp("200 OK", {"status": "ok", "timestamp": ts()})
        if path == "/v1/dashboard":
            return _json_resp("200 OK", {"status": "running", "stats": engine.stats()})
        return _json_resp("404 Not Found", {"error": "not found"})

    if method == "POST":
        if path == "/v1/chat/completions":
            length = int(env.get("CONTENT_LENGTH", 0))
            body = env["wsgi.input"].read(length).decode()
            try: req = json.loads(body)
            except: return _json_resp("400 Bad Request", {"error": "invalid JSON"})

            messages = req.get("messages", [])
            model = req.get("model", "auto")
            stream = req.get("stream", False)

            user_msg, image_url, search, thinking = "", None, False, True
            for msg in reversed(messages):
                if msg.get("role") == "user":
                    c = msg.get("content", "")
                    if isinstance(c, list):
                        parts = []
                        for p in c:
                            if isinstance(p, dict):
                                if p.get("type") == "text": parts.append(p.get("text", ""))
                                elif p.get("type") == "image_url":
                                    iu = p.get("image_url", {})
                                    image_url = iu.get("url", "") if isinstance(iu, dict) else str(iu)
                            elif isinstance(p, str): parts.append(p)
                        user_msg = " ".join(parts)
                    else: user_msg = str(c)
                    break

            for msg in messages:
                if msg.get("role") == "system":
                    sc = str(msg.get("content", ""))
                    if any(k in sc for k in ["[web_search]", "[联网]", "[search]"]):
                        search = True
                    if any(k in sc for k in ["[thinking]", "[思考]"]):
                        thinking = True

            extra = req.get("extra_body", {})
            if extra.get("web_search"): search = True
            if extra.get("thinking"): thinking = True

            for t in req.get("tools", []):
                if t.get("function", {}).get("name", "") in ("web_search", "search", "browse"):
                    search = True

            if not user_msg:
                return _json_resp("400 Bad Request", {"error": {"message": "no user message", "type": "invalid_request_error"}})

            result = engine.chat(user_msg, image_url=image_url, search=search, thinking=thinking)

            if not result["success"]:
                return _json_resp("429 Too Many Requests", {"error": {
                    "message": result.get("error", "failed"),
                    "type": "rate_limit_error"}})

            content = result["result"]
            cid = f"chatcmpl-{uid()[:12]}"
            t = ts()
            m = result.get("model", model)

            if stream:
                chunks = []
                for delta in _chunkify(content):
                    chunks.append(json.dumps({
                        "id": cid, "object": "chat.completion.chunk",
                        "created": t, "model": m,
                        "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}],
                    }, ensure_ascii=False))
                chunks.append(json.dumps({
                    "id": cid, "object": "chat.completion.chunk",
                    "created": t, "model": m,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }, ensure_ascii=False))
                return _stream_resp(chunks)

            return _json_resp("200 OK", {
                "id": cid, "object": "chat.completion", "created": t,
                "model": m,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": len(user_msg),
                          "completion_tokens": len(content),
                          "total_tokens": len(user_msg) + len(content)},
                "_extra": {"identity": result.get("identity", ""),
                           "attempts": result.get("attempts", 0),
                           "has_images": result.get("has_images", False),
                           "search_enabled": result.get("search_enabled", False)},
            })

        if path == "/v1/images/generations":
            length = int(env.get("CONTENT_LENGTH", 0))
            body = env["wsgi.input"].read(length).decode()
            try: req = json.loads(body)
            except: return _json_resp("400 Bad Request", {"error": "invalid JSON"})
            prompt = req.get("prompt", "")
            if not prompt:
                return _json_resp("400 Bad Request", {"error": "no prompt"})
            result = engine.chat(f"请生成图片：{prompt}", thinking=False)
            if not result["success"]:
                return _json_resp("500 Internal Server Error", {"error": result.get("error", "failed")})
            urls = re.findall(r'https?://[^\s\)\"\']+\.(?:png|jpg|jpeg|webp|gif)', result["result"])
            imgs = [{"url": u} for u in urls] if urls else [{"url": "", "revised_prompt": result["result"][:200]}]
            return _json_resp("200 OK", {"created": ts(), "data": imgs})

        return _json_resp("404 Not Found", {"error": "not found"})

    return _json_resp("405 Method Not Allowed", {"error": "method not allowed"})


# 给 gunicorn 用的 callable
app = wsgi_app


# ============ 直接运行（开发/测试，非 Render） ============

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)))
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    port = args.port
    global _engine
    _engine = QwenEngine(headless=True, debug=args.debug)

    print(f"\n{'='*60}")
    print(f"  Qwen Unified API — Render Deploy")
    print(f"  http://0.0.0.0:{port}")
    print(f"  DEBUG={'ON' if args.debug else 'OFF'}")
    print(f"{'='*60}")
    print(f"  端点:")
    print(f"    POST /v1/chat/completions")
    print(f"    POST /v1/images/generations")
    print(f"    GET  /v1/models")
    print(f"    GET  /v1/dashboard")
    print(f"    GET  /health")
    print(f"{'='*60}\n")

    srv = make_server("0.0.0.0", port, wsgi_app)
    try: srv.serve_forever()
    except KeyboardInterrupt:
        log("server", "stopped")
        srv.shutdown()