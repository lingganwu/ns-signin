# -*- coding: utf-8 -*-
"""本地端到端测试：mock NodeSeek 站点 + 验证签到 pipeline / 登录窗口流程。
运行：python3 mock_test.py  （容器内，无头）"""
import http.server
import json
import os
import re
import sys
import threading
import time

PORT = 8899
BASE = "http://127.0.0.1:%d" % PORT

# ---------- mock site ----------
STATE = {"signed_today": False, "requests": 0}

BOARD_HTML = """<html><head><title>NS</title></head><body>
<div id="app">NodeSeek Mock Board</div>
<span class="user">tester</span>
</body></html>"""

SIGNIN_HTML = """<html><head><title>Sign in</title></head><body>
<h1>Sign in to NodeSeek</h1>
<button id="mock-login" onclick="document.cookie='session=mockabc123; path=/'; location.href='/board'">MOCK-LOGIN</button>
</body></html>"""

class H(http.server.BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/html"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        c = self.headers.get("Cookie", "")
        if self.path.startswith("/board"):
            if "session=mockabc123" not in c:
                self._send(200, SIGNIN_HTML)  # 未登录的 board 页（含 sign in 标记）
                return
            self._send(200, BOARD_HTML)
        elif self.path.startswith("/signin"):
            self._send(200, SIGNIN_HTML)
        elif self.path.startswith("/do-login"):
            # 模拟真实登录：服务端下发 HttpOnly session cookie
            b = "logging in".encode()
            self.send_response(302)
            self.send_header("Set-Cookie", "session=mockabc123; Path=/; HttpOnly")
            self.send_header("Location", "/board")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b)
        else:
            self._send(200, "hi")

    def do_POST(self):
        STATE["requests"] += 1
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode() if n else ""
        c = self.headers.get("Cookie", "")
        if self.path.startswith("/api/attendance"):
            if "session=mockabc123" not in c:
                self._send(401, json.dumps({"message": "请先登录"}), "application/json")
                return
            if "random=" not in self.path and "random" not in body:
                self._send(400, json.dumps({"message": "缺少 random 参数"}), "application/json")
                return
            if STATE["signed_today"]:
                self._send(200, json.dumps({"success": True, "message": "今日已完成签到"}), "application/json")
            STATE["signed_today"] = True
            self._send(200, json.dumps({"success": True, "message": "获得了 7 个鸡腿"}), "application/json")
        else:
            self._send(404, "not found")

    def log_message(self, *a):
        pass

def main():
    srv = http.server.HTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # 让主程序指向 mock
    import importlib.util
    spec = importlib.util.spec_from_file_location("ns_main", os.path.join(os.path.dirname(__file__), "ns_signin.py"))
    ns = importlib.util.module_from_spec(spec)
    sys.modules["ns_main"] = ns
    spec.loader.exec_module(ns)
    ns.SITE = BASE
    ns.API_PATH = "/api/attendance"
    ns.PROFILE_DIR = os.path.join(os.path.dirname(__file__), "test_profile")
    ns.COOKIES_FILE = os.path.join(os.path.dirname(__file__), "test_profile", "cookies.json")
    if os.path.exists("/opt/hermes/tools/chromium-1208/chrome-linux64/chrome"):
        ns.CONFIG["browser_exe"] = "/opt/hermes/tools/chromium-1208/chrome-linux64/chrome"

    # 1) 未登录 → 应判定 needlogin
    r = ns.do_checkin(headless=True)
    print("T1 未登录:", r["kind"], r["msg"])
    assert r["kind"] in ("needlogin", "retry"), r

    # 2) 模拟登录：走真实登录页，点按钮让浏览器自己写 cookie 到 profile
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        ctx, ch = ns.make_context(pw, headless=True)
        p = ctx.new_page()
        p.goto(BASE + "/do-login", wait_until="networkidle")  # 服务端 Set-Cookie 下发 HttpOnly session
        time.sleep(0.5)
        ns.save_cookies(ctx)     # 模拟登录窗口成功后的快照
        ctx.close()

    r = ns.do_checkin(headless=True)
    print("T2 登录+签到:", r["kind"], r["msg"])
    assert r["kind"] == "ok" and "鸡腿" in r["msg"], r

    # 3) 同日重复 → already
    r = ns.do_checkin(headless=True)
    print("T3 重复签到:", r["kind"], r["msg"])
    assert r["kind"] == "already", r

    # 4) pipeline 全流程
    ns.STATE["last_date"] = ""
    ns.STATE["last_run"] = ""
    logs = []
    def cb(k, m): logs.append((k, m))
    # 注：T2 已在 mock 服务端签到，今天服务端必回 already，所以 pipeline 结果应为 ok/already
    res = ns.run_checkin_pipeline(cb, force=True)
    print("T4 pipeline:", res["kind"], res["msg"])
    assert res["kind"] in ("ok", "already"), res
    assert ns.STATE["last_date"] == ns.today_str(), ns.STATE

    # 5) 调度器随机时刻合理性
    now = ns.datetime.now()
    for _ in range(20):
        nra = ns.next_run_at(now)
        assert nra > now or nra.day != now.day, nra
    print("T5 随机时刻 OK")

    srv.shutdown()
    print("ALL TESTS PASSED")

if __name__ == "__main__":
    main()
