# -*- coding: utf-8 -*-
"""ComfyUI 登录门禁（由启动器拉起，默认仅监听 127.0.0.1）。

架构：
    浏览器 -> <bind_host>:<public_port>（本文件，带登录校验）
           -> 127.0.0.1:<backend_port>（ComfyUI 本体，不对外暴露）

用法：
    python login_gate.py <public_port> <backend_port> [username] [password] [bind_host]

默认账号 admin / admin@123，可用 COMFY_ADMIN_USER / COMFY_ADMIN_PASS 环境变量覆盖。
云服务器部署时 bind_host 传 0.0.0.0，并建议在前面套 Nginx/Caddy 做 HTTPS。
"""
import asyncio
import os
import secrets
import sys
import time
import urllib.parse

from aiohttp import ClientConnectorError, ClientSession, ClientTimeout, WSMsgType, web

COOKIE_NAME = "comfy_admin"
SESSION_TTL = 7 * 24 * 3600

# 登录失败防爆破：同一 IP 在窗口期内失败过多则临时锁定
FAIL_WINDOW = 10 * 60
FAIL_LIMIT = 5
LOCK_SECONDS = 5 * 60

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
}

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ComfyUI · 登录</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:radial-gradient(1200px 600px at 20% 0%,#232838 0%,#14161c 55%,#0e0f13 100%);
       font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;color:#e8eaf0}
  .card{width:360px;padding:36px 32px;border-radius:16px;background:rgba(30,33,42,.92);
        border:1px solid rgba(255,255,255,.08);box-shadow:0 24px 64px rgba(0,0,0,.55);
        backdrop-filter:blur(8px)}
  .logo{width:44px;height:44px;border-radius:12px;margin-bottom:18px;
        background:linear-gradient(135deg,#4f7dff,#8b5cf6);display:flex;align-items:center;
        justify-content:center;font-weight:700;font-size:20px;color:#fff}
  h1{font-size:20px;font-weight:600;margin-bottom:6px}
  p.sub{font-size:13px;color:#8d93a6;margin-bottom:24px}
  label{display:block;font-size:13px;color:#aab0c0;margin:14px 0 6px}
  input{width:100%;padding:11px 12px;border-radius:9px;border:1px solid #3a3f4f;
        background:#171a21;color:#e8eaf0;font-size:14px;outline:none;transition:border .15s}
  input:focus{border-color:#4f7dff;box-shadow:0 0 0 3px rgba(79,125,255,.18)}
  button{width:100%;margin-top:24px;padding:12px;border:none;border-radius:9px;cursor:pointer;
         font-size:15px;font-weight:600;color:#fff;
         background:linear-gradient(135deg,#4f7dff,#6d5cf6);transition:filter .15s}
  button:hover{filter:brightness(1.1)}
  .err{margin-top:16px;padding:10px 12px;border-radius:8px;font-size:13px;
       background:rgba(255,77,79,.12);border:1px solid rgba(255,77,79,.35);color:#ff8f90}
  .foot{margin-top:22px;font-size:12px;color:#6d7385;text-align:center;line-height:1.7}
</style></head><body>
<form class="card" method="post" action="/login">
  <div class="logo">C</div>
  <h1>ComfyUI 本地控制台</h1>
  <p class="sub">请输入管理员账号后继续</p>
  <label for="u">账号</label>
  <input id="u" name="username" autocomplete="username" autofocus>
  <label for="p">密码</label>
  <input id="p" name="password" type="password" autocomplete="current-password">
  <input type="hidden" name="next" value="{next}">
  <button type="submit">登录</button>
  {error}
  <div class="foot">请妥善保管管理员密码<br>内置账号 admin</div>
</form></body></html>
"""

STARTING_PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="2"><title>ComfyUI 启动中</title>
<style>body{background:#14161c;color:#c9cede;font-family:"Microsoft YaHei",sans-serif;
display:flex;height:100vh;align-items:center;justify-content:center;flex-direction:column;gap:14px}
.b{width:180px;height:4px;border-radius:2px;background:#262a36;overflow:hidden;position:relative}
.b:after{content:"";position:absolute;width:60px;height:100%;background:#4f7dff;
animation:m 1.2s infinite ease-in-out}@keyframes m{0%{left:-60px}100%{left:180px}}</style>
</head><body><div>ComfyUI 正在启动，请稍候…</div><div class="b"></div>
<div style="font-size:12px;color:#6d7385">页面将自动刷新</div></body></html>
"""


def login_html(error="", next_url="/"):
    safe_next = urllib.parse.quote(next_url, safe="/?=&%")
    err = ('<div class="err">' + error + '</div>') if error else ""
    return LOGIN_PAGE.replace("{next}", safe_next).replace("{error}", err)


class Gate:
    def __init__(self, host, public_port, backend_port, username, password):
        self.host = host
        self.public_port = public_port
        self.backend_port = backend_port
        self.username = username
        self.password = password
        self.tokens = {}
        self.fails = {}
        self.session = None

    # ---------- 鉴权 ----------
    def _authed(self, request):
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            return False
        expire = self.tokens.get(token)
        if expire is None:
            return False
        if expire < time.time():
            self.tokens.pop(token, None)
            return False
        return True

    @web.middleware
    async def auth_middleware(self, request, handler):
        if request.path in ("/login", "/logout", "/favicon.ico"):
            return await handler(request)
        if self._authed(request):
            return await handler(request)
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return web.json_response({"error": "unauthorized"}, status=401)
        accept = request.headers.get("Accept", "")
        if request.method == "GET" and "text/html" in accept:
            raise web.HTTPFound("/login?next=" + urllib.parse.quote(request.path_qs, safe=""))
        return web.json_response(
            {"error": "unauthorized", "hint": "请先登录 http://%s/login" % request.host},
            status=401)

    # ---------- 登录 / 登出 ----------
    async def login_get(self, request):
        return web.Response(text=login_html("", request.query.get("next", "/")),
                            content_type="text/html", charset="utf-8")

    async def login_post(self, request):
        form = {}
        try:
            if request.content_type == "application/json":
                form = await request.json()
            else:
                form = await request.post()
        except Exception:
            form = {}
        next_url = str(form.get("next") or request.query.get("next") or "/")
        if not next_url.startswith("/") or next_url.startswith("//"):
            next_url = "/"
        username = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))

        ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
              or request.remote or "unknown")
        now = time.time()
        fail = self.fails.get(ip)
        if fail and fail.get("locked_until", 0) > now:
            wait = int(fail["locked_until"] - now)
            return web.Response(text=login_html("尝试次数过多，请 %d 秒后再试" % wait, next_url),
                                content_type="text/html", charset="utf-8", status=429)

        if username == self.username and password == self.password:
            self.fails.pop(ip, None)
            token = secrets.token_urlsafe(32)
            self.tokens[token] = now + SESSION_TTL
            response = web.HTTPFound(next_url)
            https = (request.headers.get("X-Forwarded-Proto", "").lower() == "https"
                     or request.scheme == "https")
            response.set_cookie(COOKIE_NAME, token, max_age=SESSION_TTL,
                                httponly=True, samesite="Lax", path="/", secure=https)
            raise response

        if not fail or now - fail["first"] > FAIL_WINDOW:
            fail = {"first": now, "count": 0, "locked_until": 0}
        fail["count"] += 1
        if fail["count"] >= FAIL_LIMIT:
            fail["locked_until"] = now + LOCK_SECONDS
            fail["count"] = 0
            fail["first"] = now
        self.fails[ip] = fail
        await asyncio.sleep(0.5)  # 降低暴力尝试速度
        return web.Response(text=login_html("账号或密码错误", next_url),
                            content_type="text/html", charset="utf-8", status=401)

    async def logout(self, request):
        token = request.cookies.get(COOKIE_NAME)
        if token:
            self.tokens.pop(token, None)
        response = web.HTTPFound("/login")
        response.del_cookie(COOKIE_NAME, path="/")
        return response

    # ---------- 反向代理 ----------
    async def proxy(self, request):
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self.ws_proxy(request)
        url = "http://127.0.0.1:%d%s" % (self.backend_port, request.rel_url)
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
        # 保留对外 Host：ComfyUI 会校验 Host 与 Origin 一致，改成内部端口会被判 403
        headers["Host"] = request.host
        body = await request.read() if request.can_read_body else None
        try:
            async with self.session.request(request.method, url, headers=headers,
                                            data=body, allow_redirects=False) as upstream:
                drop = HOP_BY_HOP | {"content-encoding", "content-length"}
                out_headers = [(k, v) for k, v in upstream.headers.items() if k.lower() not in drop]
                out = web.StreamResponse(status=upstream.status, headers=out_headers)
                await out.prepare(request)
                async for chunk in upstream.content.iter_chunked(65536):
                    await out.write(chunk)
                await out.write_eof()
                return out
        except (ClientConnectorError, asyncio.TimeoutError):
            return web.Response(text=STARTING_PAGE, content_type="text/html",
                                charset="utf-8", status=503)

    async def ws_proxy(self, request):
        server_ws = web.WebSocketResponse(autoping=True, compress=False, max_msg_size=0)
        await server_ws.prepare(request)
        url = "http://127.0.0.1:%d%s" % (self.backend_port, request.rel_url)
        try:
            client_ws = await self.session.ws_connect(url, compress=False, max_msg_size=0)
        except Exception:
            await server_ws.close()
            return server_ws

        async def pump(src, dst):
            try:
                async for msg in src:
                    if msg.type == WSMsgType.TEXT:
                        await dst.send_str(msg.data)
                    elif msg.type == WSMsgType.BINARY:
                        await dst.send_bytes(msg.data)
                    else:
                        break
            except Exception:
                pass
            finally:
                try:
                    await dst.close()
                except Exception:
                    pass

        await asyncio.gather(pump(server_ws, client_ws), pump(client_ws, server_ws))
        return server_ws

    # ---------- 启动 ----------
    async def run(self):
        self.session = ClientSession(timeout=ClientTimeout(total=None, sock_connect=10, sock_read=900))
        app = web.Application(middlewares=[self.auth_middleware], client_max_size=2 * 1024 ** 3)
        app.add_routes([
            web.get("/login", self.login_get),
            web.post("/login", self.login_post),
            web.get("/logout", self.logout),
            web.get("/favicon.ico", lambda r: web.Response(status=204)),
        ])
        app.router.add_route("*", "/{tail:.*}", self.proxy)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, self.host, self.public_port).start()
        print("[gate] 登录门禁已启动: http://%s:%d -> 127.0.0.1:%d"
              % (self.host, self.public_port, self.backend_port), flush=True)
        await asyncio.Event().wait()


def main():
    if len(sys.argv) < 3:
        print("usage: login_gate.py <public_port> <backend_port> [username] [password] [bind_host]")
        return 2
    public_port = int(sys.argv[1])
    backend_port = int(sys.argv[2])
    username = sys.argv[3] if len(sys.argv) > 3 else os.environ.get("COMFY_ADMIN_USER", "admin")
    password = sys.argv[4] if len(sys.argv) > 4 else os.environ.get("COMFY_ADMIN_PASS", "admin@123")
    host = sys.argv[5] if len(sys.argv) > 5 else "127.0.0.1"
    gate = Gate(host, public_port, backend_port, username, password)
    try:
        asyncio.run(gate.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
