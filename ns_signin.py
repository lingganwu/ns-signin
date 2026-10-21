# -*- coding: utf-8 -*-
"""NodeSeek 每日签到助手（桌面版）

登录一次（人工过 Cloudflare 验证），之后每天定时自动签到。
浏览器用持久化配置目录，session 会随每次访问滚动续期。
"""
import json
import os
import random
import re
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta

IS_WIN = sys.platform.startswith("win")
APP_NAME = "NS签到助手"
VERSION = "1.1.1"
SITE = "https://www.nodeseek.com"
API_PATH = "/api/attendance"

def app_dir():
    """可写数据目录：打包后用 EXE 同级目录，开发时用脚本目录。"""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    d = os.path.join(base, "ns_data")
    os.makedirs(d, exist_ok=True)
    return d

DATA_DIR = app_dir()
PROFILE_DIR = os.path.join(DATA_DIR, "browser_profile")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
LOG_FILE = os.path.join(DATA_DIR, "ns_signin.log")

DEFAULTS = {
    "start": "08:00",
    "end": "12:00",
    "random_mode": True,
    "proxy": "",
    "browser_exe": "",
    "enabled": True,
    "headed_fallback": True,
}

def log(msg):
    line = "%s %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        print(line, flush=True)
    except Exception:
        pass

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(default, dict) and isinstance(data, dict):
            merged = dict(default)
            merged.update(data)
            return merged
        return data
    except Exception:
        return json.loads(json.dumps(default))

def save_json(path, data):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception as e:
        log("写文件失败 %s: %s" % (path, e))
        return False

CONFIG = load_json(CONFIG_FILE, DEFAULTS)
STATE = load_json(STATE_FILE, {
    "last_date": "",
    "streak": 0,
    "last_msg": "",
    "last_result": "",
    "need_login": False,
    "last_run": "",
    "next_run": "",
    "total_ok": 0,
})

def today_str(ts=None):
    d = ts or datetime.now()
    return "%04d-%02d-%02d" % (d.year, d.month, d.day)

def yesterday_str():
    return today_str(datetime.now() - timedelta(days=1))

def hhmm(text, fallback):
    m = re.match(r"^(\d{1,2}):(\d{1,2})$", (text or "").strip())
    if not m:
        return hhmm(fallback, fallback)
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return hhmm(fallback, fallback)
    return "%02d:%02d" % (h, mi)

def parse_hhmm(text, fallback):
    s = hhmm(text, fallback)
    h, mi = s.split(":")
    return int(h), int(mi)

def next_run_at(now=None):
    """在 [start, end] 窗口内取一个随机时刻；今天已过点则排到明天。"""
    now = now or datetime.now()
    sh, sm = parse_hhmm(CONFIG.get("start", "08:00"), "08:00")
    eh, em = parse_hhmm(CONFIG.get("end", "12:00"), "12:00")
    lo = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    hi = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    if hi <= lo:
        hi = lo + timedelta(hours=1)
    if now >= hi:                       # 今天窗口已过 → 明天
        lo = lo + timedelta(days=1)
        hi = hi + timedelta(days=1)
    elif now > lo:                      # 窗口进行中但已过起点 → 从"现在+1分钟"起随机
        lo = now + timedelta(minutes=1)
    delta = int((hi - lo).total_seconds())
    return lo + timedelta(seconds=random.randint(0, max(60, delta)))


# ===================== 浏览器 / 签到核心 =====================
CHECKIN_JS = r"""
async (rv) => {
  rv = (rv === false) ? 'false' : 'true';
  const token = (() => {
    const ck = document.cookie || '';
    const names = ['csrf_token', 'XSRF-TOKEN', 'xsrf-token', 'csrftoken', 'token'];
    for (const n of names) {
      const m = ck.match(new RegExp('(?:^|;\\s*)' + n + '=([^;]+)'));
      if (m) return decodeURIComponent(m[1]);
    }
    const meta = document.querySelector('meta[name="csrf-token"]');
    if (meta && meta.content) return meta.content;
    const inp = document.querySelector('input[name="csrf_token"], input[name="_token"]');
    if (inp && inp.value) return inp.value;
    return '';
  })();
  const base = {'Accept': 'application/json, text/plain, */*', 'X-Requested-With': 'XMLHttpRequest'};
  if (token) base['X-CSRF-Token'] = token;

  const viaXhr = (url, body, ctype) => new Promise((resolve) => {
    let done = false;
    const X = new XMLHttpRequest();
    const fin = (r) => { if (!done) { done = true; resolve(r); } };
    try {
      X.open('POST', url, true); X.withCredentials = true; X.timeout = 15000;
      Object.entries({'Content-Type': ctype, ...base}).forEach(([k, v]) => { try { X.setRequestHeader(k, v); } catch (e) {} });
      X.onload = () => fin({status: X.status, text: X.responseText || ''});
      X.onerror = () => fin({status: 0, text: '', err: 'xhr blocked'});
      X.ontimeout = () => fin({status: 0, text: '', err: 'xhr timeout'});
      X.send(body);
    } catch (e) { fin({status: 0, text: '', err: String(e)}); }
  });

  const q = '/api/attendance?random=' + rv;
  const plans = [
    () => viaXhr(q, 'random=' + rv, 'application/x-www-form-urlencoded'),
    () => viaXhr(q, JSON.stringify({content: [], random: rv === 'true'}), 'application/json'),
    () => viaXhr('/api/attendance', 'random=' + rv, 'application/x-www-form-urlencoded'),
  ];
  const errs = [];
  for (const p of plans) {
    const r = await p();
    if (r.status > 0) return {...r, token: !!token};
    errs.push(r.err || 'fail');
  }
  return {status: 0, text: '', err: errs.join('; '), token: !!token};
}
"""

NEED_LOGIN_MARKERS = ("登录", "login", "sign in", "注册")

def looks_logged_out(page_html):
    """未登录判定：站点真实标志（陌生人欢迎语 / signIn.html 入口 / 密码输入框）。"""
    h = (page_html or "")
    if "陌生人" in h:
        return True
    if re.search(r'signIn\.html|/api/auth/login|input[^>]+type=["\']password', h, re.I):
        return True
    return False

def parse_checkin(status, text):
    """返回 (kind, message)，kind ∈ ok / already / needlogin / retry / fail"""
    import json as _json
    data = None
    try:
        data = _json.loads(text)
    except Exception:
        data = None
    if data and re.search(r"已完成|已签到|已经签", str(data.get("message", ""))):
        return "already", data.get("message") or "今天已经签到过了"
    if data and (data.get("success") is True or "鸡腿" in str(data.get("message", ""))):
        return "ok", data.get("message") or "签到成功"
    if status in (401, 403):
        if re.search(r"just a moment|cf_chl|attention required", text or "", re.I):
            return "retry", "被 Cloudflare 拦住"
        return "needlogin", "登录已失效，需要重新登录"
    if not data:
        if re.search(r"just a moment|cf_chl|attention required|verify you are a human", "", re.I):
            return "retry", "被 Cloudflare 拦住"
        if status == 0:
            return "retry", "请求未送达"
        return "retry", "HTTP %s 非JSON" % status
    if status == 404:
        return "fail", "接口 404，站点签到接口可能变了"
    return "fail", str((data or {}).get("message") or ("HTTP %s" % status))

def make_context(pw, headless=True):
    """启动持久化浏览器上下文。
    优先级：config 里指定的浏览器可执行文件 > 系统 Edge/Chrome > Playwright 自带 Chromium。
    """
    kw = {
        "user_data_dir": PROFILE_DIR,
        "headless": headless,
        "viewport": None,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if CONFIG.get("proxy"):
        kw["proxy"] = {"server": CONFIG["proxy"]}
    exe = CONFIG.get("browser_exe", "")
    if exe and os.path.exists(exe):
        return pw.chromium.launch_persistent_context(executable_path=exe, **kw), "custom"
    last = None
    for ch in ("msedge", "chrome"):
        try:
            return pw.chromium.launch_persistent_context(channel=ch, **kw), ch
        except Exception as e:
            last = e
    try:
        return pw.chromium.launch_persistent_context(**kw), "chromium"
    except Exception as e:
        raise RuntimeError("浏览器启动失败（找不到 Edge/Chrome）：%s" % last)

COOKIES_FILE = os.path.join(DATA_DIR, "cookies.json")

def save_cookies(ctx):
    """登录成功后把浏览器 cookie（含 HttpOnly session）快照到本地文件。"""
    try:
        cks = ctx.cookies()
        keep = ("name", "value", "domain", "path", "httpOnly", "secure", "sameSite")
        slim = []
        for c in cks:
            item = {k: c[k] for k in keep if k in c}
            if c.get("expires"):
                item["expires"] = c["expires"]
            slim.append(item)
        save_json(COOKIES_FILE, slim)
        log("已保存 %d 个 cookie 快照" % len(slim))
        return True
    except Exception as e:
        log("保存 cookie 失败: %s" % e)
        return False

def inject_cookies(ctx):
    """每次签到前把快照注入浏览器（HttpOnly session 是会话 cookie，重启会丢，靠这个补回）。"""
    data = load_json(COOKIES_FILE, [])
    if not isinstance(data, list) or not data:
        return False
    try:
        ctx.add_cookies(data)
        return True
    except Exception as e:
        log("注入 cookie 失败: %s" % e)
        return False

def do_checkin(headless=True):
    """执行一次签到。返回 dict(kind, msg)。kind ∈ ok/already/needlogin/retry/fail"""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return {"kind": "fail", "msg": "Playwright 未安装：%s" % e}
    rv = bool(CONFIG.get("random_mode", True))
    try:
        with sync_playwright() as pw:
            ctx, ch = make_context(pw, headless=headless)
            try:
                inject_cookies(ctx)
                page = ctx.new_page()
                page.goto(SITE + "/board", wait_until="domcontentloaded", timeout=45000)
                html = ""
                for _ in range(6):
                    time.sleep(2)
                    title = (page.title() or "")
                    if re.search(r"just a moment|attention required|verify you are a human", title, re.I):
                        continue  # CF 还在过
                    html = page.content()[:20000]
                    break
                if re.search(r"just a moment|attention required", (page.title() or ""), re.I):
                    return {"kind": "retry", "msg": "Cloudflare 挑战页未通过，稍后重试", "browser": ch}
                if looks_logged_out(html):
                    return {"kind": "needlogin", "msg": "浏览器里没有登录态，请点「登录/更新登录态」", "browser": ch}
                r = page.evaluate(CHECKIN_JS, bool(CONFIG.get("random_mode", True)))
                kind, msg = parse_checkin(r.get("status", 0), r.get("text", ""))
                if r.get("status", 0) == 0 and r.get("err"):
                    msg = "%s（%s）" % (msg, r["err"])
                return {"kind": kind, "msg": msg, "browser": ch, "token": r.get("token")}
            finally:
                ctx.close()
    except Exception as e:
        return {"kind": "fail", "msg": "异常：%s" % (e,)}


# ===================== 状态机 =====================
def mark_done(kind, msg):
    now = datetime.now()
    STATE["last_run"] = now.strftime("%Y-%m-%d %H:%M:%S")
    STATE["last_result"] = kind
    STATE["last_msg"] = msg
    if kind in ("ok", "already"):
        if kind == "ok":
            if STATE.get("last_date") == yesterday_str():
                STATE["streak"] = int(STATE.get("streak", 0)) + 1
            elif STATE.get("last_date") != today_str():
                STATE["streak"] = 1
            STATE["total_ok"] = int(STATE.get("total_ok", 0)) + 1
        STATE["last_date"] = today_str()
        STATE["need_login"] = False
    elif kind == "needlogin":
        STATE["need_login"] = True
    save_json(STATE_FILE, STATE)
    log("结果 %s: %s" % (kind, msg))

def run_checkin_pipeline(ui_cb=None, force=False):
    """完整签到流程：headless 两次 → 可选 headed 一次。"""
    def cb(kind, msg):
        log("pipeline: %s %s" % (kind, msg))
        if ui_cb:
            try:
                ui_cb(kind, msg)
            except Exception:
                pass

    if not force and STATE.get("last_date") == today_str():
        cb("already", "今天已签到（连续 %s 天）" % STATE.get("streak", 0))
        return {"kind": "already", "msg": "今天已签到"}

    attempts = [("headless", True), ("headless", True)]
    if CONFIG.get("headed_fallback", True):
        attempts.append(("headed", False))

    last = {"kind": "retry", "msg": "未执行"}
    for name, headless in attempts:
        r = do_checkin(headless=headless)
        last = r
        cb("try", "[%s] %s: %s" % (name, r.get("kind"), r.get("msg")))
        if r["kind"] in ("ok", "already"):
            mark_done(r["kind"], r["msg"])
            cb(r["kind"], r["msg"])
            return r
        if r["kind"] == "needlogin":
            mark_done("needlogin", r["msg"])
            cb("needlogin", r["msg"])
            return r
        if r["kind"] == "fail":
            mark_done("fail", r["msg"])
            cb("fail", r["msg"])
            return r
        time.sleep(6)
    mark_done("retry", last.get("msg", "多次重试未成功"))
    cb("retry", "多次尝试未成功：%s" % last.get("msg"))
    return last

# ===================== 登录窗口 =====================
def open_login_window(ui_cb=None):
    """打开有头浏览器让用户登录，检测到登录态后自动关闭。"""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return {"kind": "fail", "msg": "Playwright 未安装：%s" % e}
    try:
        with sync_playwright() as pw:
            ctx, ch = make_context(pw, headless=False)
            try:
                page = ctx.new_page()
                page.goto(SITE + "/signIn.html", wait_until="domcontentloaded", timeout=60000)
                try:
                    page.set_default_timeout(30000)
                except Exception:
                    pass
                if ui_cb:
                    ui_cb("login_open", "浏览器已打开（%s），请完成登录，成功后自动关闭" % ch)
                # 成功标志：浏览器里出现非空 session cookie（NS 登录成功后服务端下发）
                deadline = time.time() + 600
                while time.time() < deadline:
                    time.sleep(2)
                    try:
                        cks = ctx.cookies(SITE)
                        if any(c.get("name") == "session" and c.get("value") for c in cks):
                            time.sleep(2)
                            save_cookies(ctx)
                            if ui_cb:
                                ui_cb("login_ok", "检测到登录成功，正在关闭窗口")
                            return {"kind": "ok", "msg": "登录态已保存"}
                    except Exception:
                        pass
                return {"kind": "retry", "msg": "10 分钟内未检测到登录，窗口已关闭"}
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass
    except Exception as e:
        return {"kind": "fail", "msg": "登录窗口异常：%s" % (e,)}


# ===================== 后台调度 =="======================
class Scheduler(threading.Thread):
    def __init__(self, notify_cb):
        super().__init__(daemon=True)
        self.notify_cb = notify_cb
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.next_at = None

    def wake(self):
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def _emit(self, kind, msg):
        try:
            self.notify_cb(kind, msg)
        except Exception:
            pass

    def _wait(self, seconds):
        """可被 stop/wake 打断的等待。"""
        end = time.time() + seconds
        while not self._stop.is_set():
            remain = end - time.time()
            if remain <= 0 or self._wake.is_set():
                self._wake.clear()
                return
            self._wake.wait(min(remain, 5))
        self._wake.clear()

    def run(self):
        self._emit("waiting", "调度器已启动")
        while not self._stop.is_set():
            if not CONFIG.get("enabled", True):
                self._emit("disabled", "定时签到已暂停")
                self._wait(60)
                continue
            now = datetime.now()
            if STATE.get("last_date") == today_str():
                self._emit("waiting", "今日已完成（连续 %s 天），明早 %s 前自动补签" % (
                    STATE.get("streak", 0), CONFIG.get("start", "08:00")))
                # 直接睡到窗口结束
                self._wait(300)
                continue
            if STATE.get("need_login"):
                self._emit("needlogin", "需要重新登录，签到暂停中（每 5 分钟检查）")
                self._wait(300)
                continue
            nxt = next_run_at(now)
            wait = max(0, int((nxt - now).total_seconds()))
            if wait > 0:
                self._emit("waiting", "下次签到 %s（还有 %s）" % (nxt.strftime("%m-%d %H:%M"), fmt_dur(wait)))
                self._wait(min(wait, 300))
                continue
            # 到点了
            self._emit("running", "开始签到…")
            try:
                run_checkin_pipeline(self._emit, force=False)
            except Exception as e:
                log("pipeline 异常: %s" % e)
                self._emit("fail", "pipeline 异常：%s" % e)
            self._wait(120)

def fmt_dur(sec):
    sec = int(sec)
    h, m = sec // 3600, (sec % 3600) // 60
    if h > 0:
        return "%d小时%02d分" % (h, m)
    return "%d分%02d秒" % (m, sec % 60)


# ===================== Tkinter 界面 =====================
def build_ui():
    import queue
    import tkinter as tk
    from tkinter import ttk, messagebox

    msg_q = queue.Queue()
    root = tk.Tk()
    root.title("%s v%s" % (APP_NAME, VERSION))
    root.geometry("400x470")
    root.resizable(False, False)
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass

    status_var = tk.StringVar(value="就绪")
    result_var = tk.StringVar(value="")
    streak_var = tk.StringVar(value="")
    next_var = tk.StringVar(value="")
    state = {"btn": {}}

    def emit(kind, msg):
        """任意线程可调用的状态更新入口。"""
        msg_q.put((kind, msg))

    def pump():
        try:
            while True:
                kind, msg = msg_q.get_nowait()
                colors = {"ok": "green", "fail": "red", "needlogin": "red",
                          "retry": "orange", "waiting": "blue", "running": "blue",
                          "login_open": "purple", "login_ok": "green",
                          "disabled": "gray", "try": "gray40"}
                status_var.set(msg)
                status_label.config(foreground=colors.get(kind, "black"))
                refresh_state()
                if kind in ("ok", "already", "needlogin", "fail", "login_ok", "login_open", "login_fail"):
                    set_buttons(kind)
        except queue.Empty:
            pass
        root.after(250, pump)

    def refresh_state():
        if STATE.get("last_msg"):
            result_var.set("上次：%s（%s）" % (STATE["last_msg"], STATE.get("last_run", "")))
        else:
            result_var.set("")
        streak_var.set("连续签到 %s 天" % STATE.get("streak", 0))
        nxt = next_run_at().strftime("%m-%d %H:%M")
        next_var.set("下次执行窗口：%s" % nxt)

    def set_buttons(kind):
        busy = kind in ("running", "login_open")
        for name, b in state["btn"].items():
            try:
                b.config(state="disabled" if busy else "normal")
            except Exception:
                pass

    def worker(fn, *a):
        def run():
            try:
                fn(*a)
            except Exception as e:
                emit("fail", "异常：%s" % e)
        threading.Thread(target=run, daemon=True).start()

    def do_login():
        set_buttons("login_open")
        emit("login_open", "正在打开浏览器，请完成登录…")
        def run():
            r = open_login_window(emit)
            if r["kind"] == "ok":
                STATE["need_login"] = False
                save_json(STATE_FILE, STATE)
                emit("login_ok", "登录成功，登录态已保存 ✓")
            else:
                emit("login_fail", r["msg"])
        threading.Thread(target=run, daemon=True).start()

    def do_check():
        set_buttons("running")
        emit("running", "正在签到…")
        def run():
            r = run_checkin_pipeline(emit, force=True)
            if r["kind"] in ("ok", "already"):
                emit("ok", r["msg"])
            elif r["kind"] == "needlogin":
                emit("needlogin", r["msg"])
            else:
                emit("fail", r["msg"])
        threading.Thread(target=run, daemon=True).start()

    def start_scheduler():
        emit("running", "后台调度已启动，每天自动签到（本程序需保持运行）")
        Scheduler(emit).start()

    def open_log():
        try:
            if IS_WIN:
                os.startfile(DATA_DIR)
            else:
                import subprocess
                subprocess.Popen(["xdg-open", DATA_DIR])
        except Exception as e:
            messagebox.showinfo(APP_NAME, "日志目录：%s" % DATA_DIR)

    frame = tk.Frame(root, padx=16, pady=16)
    frame.pack(fill="both", expand=True)

    tk.Label(frame, text=APP_NAME, font=("Microsoft YaHei", 14, "bold")).pack(anchor="w")
    tk.Label(frame, text="登录一次，之后每天自动签到", foreground="gray").pack(anchor="w", pady=(0, 10))

    status_label = tk.Label(frame, textvariable=status_var, font=("Microsoft YaHei", 11),
                            wraplength=350, justify="left", anchor="w")
    status_label.pack(fill="x", pady=(0, 8))

    info = tk.LabelFrame(frame, text="状态", font=("Microsoft YaHei", 10), padx=8, pady=6)
    info.pack(fill="x", pady=(0, 10))
    tk.Label(info, textvariable=result_var, font=("Microsoft YaHei", 10), wraplength=350).pack(anchor="w")
    tk.Label(info, textvariable=streak_var, font=("Microsoft YaHei", 10)).pack(anchor="w")
    tk.Label(info, textvariable=next_var, font=("Microsoft YaHei", 10)).pack(anchor="w")

    btns = tk.Frame(frame)
    btns.pack(fill="x", pady=(6, 0))
    state["btn"]["login"] = tk.Button(btns, text="登录/更新登录态", width=14, command=do_login)
    state["btn"]["login"].grid(row=0, column=0, padx=(0, 6))
    state["btn"]["check"] = tk.Button(btns, text="立即签到", width=12, command=do_check)
    state["btn"]["check"].grid(row=0, column=1, padx=6)
    state["btn"]["sched"] = tk.Button(btns, text="启动后台调度", width=14, command=start_scheduler)
    state["btn"]["sched"].grid(row=0, column=2, padx=(6, 0))

    tk.Button(frame, text="打开数据/日志目录", fg="gray", bd=0, command=open_log).pack(anchor="e", pady=(12, 0))

    refresh_state()
    pump()
    return root

def main():
    log("启动 %s v%s (platform=%s)" % (APP_NAME, VERSION, sys.platform))
    try:
        root = build_ui()
    except Exception as e:
        log("界面构建失败: %s" % e)
        traceback.print_exc()
        root = None
    if root is None:
        log("无界面环境，直接运行调度线程")
        Scheduler(lambda k, m: log("%s: %s" % (k, m))).start()
        while True:
            time.sleep(60)
    else:
        root.mainloop()

if __name__ == "__main__":
    main()
