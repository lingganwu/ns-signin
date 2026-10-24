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
VERSION = "1.2.1"
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
    "close_action": "tray",      # 右上角×：tray=最小化到托盘 / exit=直接退出
    "auto_start": True,          # 程序启动即自动开启后台调度
    "hidden_headed": True,       # 有头兜底窗口挪到屏幕外：能过 Cloudflare 又不抢焦点
    "headless_tries": 1,         # 无头尝试次数，用完直接换真实浏览器
    "retry_minutes": 30,         # 一次流程失败后隔多久再试
    "max_attempts": 6,           # 当天最多重试次数，超过就等明天窗口
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
    "attempts_today": 0,
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

def _window_bounds(now):
    """今天的 [lo, hi] 时间窗。"""
    sh, sm = parse_hhmm(CONFIG.get("start", "08:00"), "08:00")
    eh, em = parse_hhmm(CONFIG.get("end", "12:00"), "12:00")
    lo = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    hi = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    if hi <= lo:
        hi = lo + timedelta(hours=1)
    return lo, hi


def _rand_in(lo, hi):
    delta = int((hi - lo).total_seconds())
    return lo + timedelta(seconds=random.randint(0, max(60, delta)))


def next_run_at(now=None, catchup=True):
    """下一次签到时刻。
    catchup=True 时：今天窗口已过而今天还没签 → 立刻补签（返回 now），不再白等一整天。
    """
    now = now or datetime.now()
    lo, hi = _window_bounds(now)
    if now >= hi:
        if catchup:
            return now
        return _rand_in(lo + timedelta(days=1), hi + timedelta(days=1))
    if now > lo:                        # 窗口进行中但已过起点 → 从"现在+1分钟"起随机
        lo = now + timedelta(minutes=1)
    return _rand_in(lo, hi)


def next_window_start(now=None):
    """下一个还没用完的窗口起点（今天已签完时用）。"""
    now = now or datetime.now()
    lo, hi = _window_bounds(now)
    return lo if now < hi else lo + timedelta(days=1)


def tomorrow_slot(now=None):
    now = now or datetime.now()
    lo, hi = _window_bounds(now)
    return _rand_in(lo + timedelta(days=1), hi + timedelta(days=1))


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
        if re.search(r"just a moment|cf_chl|attention required|verify you are a human", text or "", re.I):
            return "retry", "被 Cloudflare 拦住"
        if status == 0:
            return "retry", "请求未送达"
        return "retry", "HTTP %s 非JSON" % status
    if status == 404:
        return "fail", "接口 404，站点签到接口可能变了"
    return "fail", str((data or {}).get("message") or ("HTTP %s" % status))

def make_context(pw, headless=True, hidden=False):
    """启动持久化浏览器上下文。
    优先级：config 里指定的浏览器可执行文件 > 系统 Edge/Chrome > Playwright 自带 Chromium。
    hidden=True 表示有头但窗口挪到屏幕外（真浏览器指纹，且不抢焦点）。
    """
    args = ["--disable-blink-features=AutomationControlled",
            "--disable-features=Translate"]
    if not headless and hidden:
        # 有头但窗口移到屏幕外：Cloudflare 看到的是真浏览器，用户不会被抢焦点
        args += ["--window-position=-32000,-32000", "--window-size=1280,800"]
    kw = {
        "user_data_dir": PROFILE_DIR,
        "headless": headless,
        "viewport": None,
        "args": args,
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

def login_snapshot_state():
    """检查本地登录态快照。返回 (kind, text, color)。"""
    if STATE.get("need_login"):
        return "expired", "登录已失效，请重新点登录", "red"
    cks = load_json(COOKIES_FILE, [])
    if isinstance(cks, list) and cks:
        if any(c.get("name") == "session" and c.get("value") for c in cks):
            return "saved", "已保存登录态", "green"
        return "partial", "已存 cookie 但缺 session，建议重新登录", "orange"
    return "none", "未登录", "gray"

def do_checkin(headless=True, hidden=False):
    """执行一次签到。返回 dict(kind, msg)。kind ∈ ok/already/needlogin/retry/fail"""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return {"kind": "fail", "msg": "Playwright 未安装：%s" % e}
    rv = bool(CONFIG.get("random_mode", True))
    try:
        with sync_playwright() as pw:
            ctx, ch = make_context(pw, headless=headless, hidden=hidden)
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
        if kind == "already" and int(STATE.get("streak", 0)) == 0:
            STATE["streak"] = 1
        if kind == "ok":
            if STATE.get("last_date") == yesterday_str():
                STATE["streak"] = int(STATE.get("streak", 0)) + 1
            elif STATE.get("last_date") != today_str():
                STATE["streak"] = 1
            STATE["total_ok"] = int(STATE.get("total_ok", 0)) + 1
        STATE["last_date"] = today_str()
        STATE["need_login"] = False
        STATE["attempts_today"] = 0
        STATE["attempt_date"] = today_str()
    elif kind == "needlogin":
        STATE["need_login"] = True
    save_json(STATE_FILE, STATE)
    log("结果 %s: %s" % (kind, msg))

def build_attempts():
    """尝试序列：无头试探 → 隐身有头（屏幕外）→ 可见有头兜底。"""
    tries = max(1, int(CONFIG.get("headless_tries", 1) or 1))
    attempts = [("无头浏览器", True, False)] * tries
    if CONFIG.get("headed_fallback", True):
        if CONFIG.get("hidden_headed", True):
            attempts.append(("后台浏览器", False, True))
        attempts.append(("可见浏览器", False, False))
    return attempts


def run_checkin_pipeline(ui_cb=None, force=False):
    """完整签到流程：无头 → 隐身有头 → 可见有头。"""
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

    attempts = build_attempts()
    total = len(attempts)

    last = {"kind": "retry", "msg": "未执行"}
    for idx, (name, headless, hidden) in enumerate(attempts, 1):
        cb("try", "第 %d/%d 次尝试：%s…" % (idx, total, name))
        r = do_checkin(headless=headless, hidden=hidden)
        last = r
        if r["kind"] not in ("ok", "already"):
            cb("try", "第 %d/%d 次（%s）未成功：%s" % (idx, total, name, r.get("msg")))
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

    def _reset_day_counters(self, now):
        if STATE.get("attempt_date") != today_str(now):
            STATE["attempt_date"] = today_str(now)
            STATE["attempts_today"] = 0
            save_json(STATE_FILE, STATE)

    def run(self):
        self._emit("waiting", "调度器已启动")
        while not self._stop.is_set():
            if not CONFIG.get("enabled", True):
                self._emit("disabled", "定时签到已暂停")
                self._wait(60)
                continue
            now = datetime.now()
            self._reset_day_counters(now)

            if STATE.get("last_date") == today_str(now):
                nxt = next_window_start(now)
                self.next_at = nxt
                self._emit("waiting", "今日已完成（连续 %s 天），下次 %s" % (
                    STATE.get("streak", 0), nxt.strftime("%m-%d %H:%M")))
                self._wait(300)
                continue

            if STATE.get("need_login"):
                self._emit("needlogin", "需要重新登录，签到暂停中（每 5 分钟检查）")
                self._wait(300)
                continue

            tries = int(STATE.get("attempts_today", 0) or 0)
            cap = max(1, int(CONFIG.get("max_attempts", 6) or 6))
            if tries >= cap:
                nxt = tomorrow_slot(now)
                self.next_at = nxt
                self._emit("retry", "今天已试 %d 次没成功，明天 %s 再试" % (
                    tries, nxt.strftime("%m-%d %H:%M")))
                self._wait(min(max(0, int((nxt - now).total_seconds())), 300))
                continue

            # 窗口内随机时刻；窗口已过而今天没签 → 立刻补签
            nxt = next_run_at(now, catchup=True)
            self.next_at = nxt
            wait = max(0, int((nxt - now).total_seconds()))
            if wait > 0:
                self._emit("waiting", "下次签到 %s（还有 %s）" % (
                    nxt.strftime("%m-%d %H:%M"), fmt_dur(wait)))
                self._wait(min(wait, 300))
                continue

            STATE["attempts_today"] = tries + 1
            save_json(STATE_FILE, STATE)
            self._emit("running", "开始签到…（今天第 %d 轮）" % (tries + 1))
            try:
                res = run_checkin_pipeline(self._emit, force=False)
            except Exception as e:
                log("pipeline 异常: %s" % e)
                self._emit("fail", "pipeline 异常：%s" % e)
                res = {"kind": "fail", "msg": str(e)}

            if (res or {}).get("kind") in ("ok", "already"):
                self._wait(300)
                continue
            gap = max(1, int(CONFIG.get("retry_minutes", 30) or 30)) * 60
            left = max(0, cap - (tries + 1))
            if left:
                self._emit("retry", "这轮没成功，%d 分钟后重试（今天还剩 %d 次机会）" % (
                    CONFIG.get("retry_minutes", 30), left))
            self._wait(min(gap, 300))
            if left:
                # 等待剩余间隔（_wait 最多睡 300 秒，用循环补满）
                remain = gap - 300
                while remain > 0 and not self._stop.is_set():
                    step = min(remain, 300)
                    self._wait(step)
                    remain -= step

def fmt_dur(sec):
    sec = int(sec)
    h, m = sec // 3600, (sec % 3600) // 60
    if h > 0:
        return "%d小时%02d分" % (h, m)
    return "%d分%02d秒" % (m, sec % 60)


# ===================== Tkinter 界面 =====================
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

def autostart_get():
    if not IS_WIN:
        return False
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY)
        v, _t = winreg.QueryValueEx(k, "NSSignIn")
        winreg.CloseKey(k)
        return bool(v)
    except Exception:
        return False

def autostart_set(on):
    """返回 None=成功，否则错误信息。"""
    if not IS_WIN:
        return "开机自启仅支持 Windows"
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
        if getattr(sys, "frozen", False):
            cmd = '"%s"' % sys.executable
        else:
            cmd = '\"%s\" \"%s\"' % (sys.executable, os.path.abspath(__file__))
        if on:
            winreg.SetValueEx(k, "NSSignIn", 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(k, "NSSignIn")
            except FileNotFoundError:
                pass
        winreg.CloseKey(k)
        return None
    except Exception as e:
        return str(e)

def build_settings_dialog(parent, on_saved):
    """设置面板：时间窗、自动签到开关、×行为、开机自启等。"""
    import tkinter as tk
    from tkinter import messagebox
    top = tk.Toplevel(parent)
    top.title("设置")
    top.geometry("380x360")
    top.resizable(False, False)
    top.transient(parent)
    top.grab_set()

    v_start = tk.StringVar(value=CONFIG.get("start", "08:00"))
    v_end = tk.StringVar(value=CONFIG.get("end", "12:00"))
    v_enabled = tk.BooleanVar(value=bool(CONFIG.get("enabled", True)))
    v_sched = tk.BooleanVar(value=bool(CONFIG.get("auto_start", True)))
    v_close = tk.StringVar(value=CONFIG.get("close_action", "tray"))
    v_random = tk.BooleanVar(value=bool(CONFIG.get("random_mode", True)))
    v_hidden = tk.BooleanVar(value=bool(CONFIG.get("hidden_headed", True)))
    v_retry = tk.StringVar(value=str(CONFIG.get("retry_minutes", 30)))
    v_boot = tk.BooleanVar(value=autostart_get())

    f = tk.Frame(top, padx=16, pady=10)
    f.pack(fill="both", expand=True)
    F = ("Microsoft YaHei", 10)

    r0 = tk.Frame(f); r0.pack(fill="x", pady=2)
    tk.Label(r0, text="每日签到时间窗", font=F).pack(side="left")
    tk.Entry(r0, textvariable=v_start, width=6, justify="center").pack(side="left", padx=(8, 2))
    tk.Label(r0, text="—", font=F).pack(side="left")
    tk.Entry(r0, textvariable=v_end, width=6, justify="center").pack(side="left", padx=(2, 8))
    tk.Label(r0, text="窗口内随机执行", fg="gray", font=F).pack(side="left")

    tk.Checkbutton(f, text="启用每日自动签到", variable=v_enabled, font=F).pack(anchor="w", pady=(6, 0))
    tk.Checkbutton(f, text="程序启动时自动开启后台调度", variable=v_sched, font=F).pack(anchor="w")
    tk.Checkbutton(f, text="随机鸡腿（0~10），不选为固定", variable=v_random, font=F).pack(anchor="w")
    tk.Checkbutton(f, text="后台自动签到时隐藏浏览器窗口（推荐）", variable=v_hidden, font=F).pack(anchor="w")
    row2 = tk.Frame(f); row2.pack(fill="x", pady=2)
    tk.Label(row2, text="失败后间隔", font=F).pack(side="left")
    tk.Entry(row2, textvariable=v_retry, width=5, justify="center").pack(side="left", padx=4)
    tk.Label(row2, text="分钟重试一次", font=F).pack(side="left")

    box = tk.LabelFrame(f, text="点击右上角 × 时", font=F, padx=8, pady=2)
    box.pack(fill="x", pady=8)
    tk.Radiobutton(box, text="最小化到托盘（后台继续签到）", variable=v_close, value="tray", font=F).pack(anchor="w")
    tk.Radiobutton(box, text="直接退出程序", variable=v_close, value="exit", font=F).pack(anchor="w")

    tk.Checkbutton(f, text="开机自动运行", variable=v_boot, font=F).pack(anchor="w", pady=(4, 0))

    def save():
        s, e = hhmm(v_start.get(), ""), hhmm(v_end.get(), "")
        if not s or not e:
            messagebox.showwarning("设置", "时间格式应为 HH:MM，例如 08:30", parent=top)
            return
        if (int(s[:2]) * 60 + int(s[3:])) >= (int(e[:2]) * 60 + int(e[3:])):
            e = "%02d:%02d" % (min(23, int(s[:2]) + 1), int(s[3:]))
        CONFIG["start"], CONFIG["end"] = s, e
        CONFIG["enabled"] = bool(v_enabled.get())
        CONFIG["auto_start"] = bool(v_sched.get())
        CONFIG["close_action"] = v_close.get()
        CONFIG["random_mode"] = bool(v_random.get())
        CONFIG["hidden_headed"] = bool(v_hidden.get())
        try:
            CONFIG["retry_minutes"] = min(720, max(1, int(v_retry.get())))
        except Exception:
            pass
        err = None
        if v_boot.get() != autostart_get():
            err = autostart_set(bool(v_boot.get()))
        save_json(CONFIG_FILE, CONFIG)
        if err:
            messagebox.showwarning("设置", "其余已保存，开机自启设置失败：%s" % err, parent=top)
        top.destroy()
        on_saved()

    btns = tk.Frame(f)
    btns.pack(pady=(12, 0))
    tk.Button(btns, text="保存", width=10, command=save).pack(side="left", padx=6)
    tk.Button(btns, text="取消", width=10, command=top.destroy).pack(side="left", padx=6)

def build_ui():
    import queue
    import tkinter as tk
    from tkinter import ttk, messagebox

    msg_q = queue.Queue()
    ui_q = queue.Queue()          # 托盘线程 → 主线程 的指令
    tray = {"icon": None}
    sched = {"thread": None}
    root = tk.Tk()
    root.title("%s v%s" % (APP_NAME, VERSION))
    root.geometry("400x470")
    root.resizable(False, False)
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass

    status_var = tk.StringVar(value="正在检查登录态…")
    login_var = tk.StringVar(value="…")
    result_var = tk.StringVar(value="")
    streak_var = tk.StringVar(value="")
    next_var = tk.StringVar(value="")
    state = {"btn": {}}

    def emit(kind, msg):
        """任意线程可调用的状态更新入口。"""
        msg_q.put((kind, msg))

    def real_quit():
        try:
            if tray["icon"]:
                tray["icon"].stop()
        except Exception:
            pass
        root.quit()
        root.destroy()

    def minimize_to_tray():
        root.withdraw()
        if tray["icon"] is None and not start_tray():
            root.deiconify()
            status_var.set("托盘不可用，已改为最小化到任务栏")
            return
        try:
            tray["icon"].notify("已最小化到托盘，双击图标恢复", APP_NAME)
        except Exception:
            pass

    def start_tray():
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception as e:
            log("托盘组件缺失: %s" % e)
            return False
        try:
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.ellipse([4, 4, 60, 60], fill=(214, 122, 34, 255))
            try:
                d.text((19, 22), "NS", fill=(255, 255, 255, 255))
            except Exception:
                pass
            menu = pystray.Menu(
                pystray.MenuItem("显示主窗口", lambda i, it: ui_q.put(("show",))),
                pystray.MenuItem("立即签到", lambda i, it: ui_q.put(("check",))),
                pystray.MenuItem("退出", lambda i, it: ui_q.put(("quit",))),
            )
            ic = pystray.Icon("NSSignIn", img, APP_NAME, menu)
            threading.Thread(target=ic.run, daemon=True).start()
            tray["icon"] = ic
            return True
        except Exception as e:
            log("托盘启动失败: %s" % e)
            return False

    def pump():
        try:
            while True:
                cmd = ui_q.get_nowait()[0]
                if cmd == "show":
                    root.deiconify()
                    root.lift()
                    root.focus_force()
                elif cmd == "check":
                    root.deiconify()
                    do_check()
                elif cmd == "quit":
                    real_quit()
                    return
        except queue.Empty:
            pass
        try:
            while True:
                kind, msg = msg_q.get_nowait()
                colors = {"ok": "#1a7f37", "already": "#1a7f37", "fail": "#c00000",
                          "needlogin": "#c00000", "login_ok": "#1a7f37",
                          "retry": "#b45309", "waiting": "#1d4ed8", "running": "#1d4ed8",
                          "login_open": "#7c3aed", "disabled": "gray", "try": "gray40"}
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
        _kind, _txt, _color = login_snapshot_state()
        login_var.set("登录状态：" + _txt)
        try:
            login_label.config(foreground=_color)
        except Exception:
            pass
        streak_var.set("连续签到 %s 天" % STATE.get("streak", 0))
        try:
            th = sched.get("thread")
        except Exception:
            th = None
        if th is not None and getattr(th, "next_at", None):
            nxt = th.next_at
        elif STATE.get("last_date") == today_str():
            nxt = next_window_start()
        else:
            nxt = next_run_at()
        next_var.set("下次签到：%s" % nxt.strftime("%m-%d %H:%M"))

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
        if sched["thread"] is not None and sched["thread"].is_alive():
            emit("waiting", "后台调度已在运行中")
            return
        emit("running", "后台调度已启动，每天自动签到")
        sched["thread"] = Scheduler(emit)
        sched["thread"].start()

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
    login_label = tk.Label(info, textvariable=login_var, font=("Microsoft YaHei", 10, "bold"))
    login_label.pack(anchor="w")
    tk.Label(info, textvariable=streak_var, font=("Microsoft YaHei", 10)).pack(anchor="w")
    tk.Label(info, textvariable=next_var, font=("Microsoft YaHei", 10)).pack(anchor="w")

    btns = tk.Frame(frame)
    btns.pack(fill="x", pady=(6, 0))
    state["btn"]["login"] = tk.Button(btns, text="① 登录/更新登录态", width=16, command=do_login)
    state["btn"]["login"].grid(row=0, column=0, padx=(0, 6))
    state["btn"]["check"] = tk.Button(btns, text="② 立即签到", width=13, command=do_check)
    state["btn"]["check"].grid(row=0, column=1, padx=6)
    state["btn"]["sched"] = tk.Button(btns, text="③ 启动后台调度", width=16, command=start_scheduler)
    state["btn"]["sched"].grid(row=0, column=2, padx=(6, 0))

    def open_settings():
        build_settings_dialog(root, lambda: (refresh_state(),
                                             emit("waiting", "设置已保存")))

    btns2 = tk.Frame(frame)
    btns2.pack(fill="x", pady=(6, 0))
    b_set = tk.Button(btns2, text="⚙ 设置", width=10, command=open_settings)
    b_set.pack(side="left")
    tk.Button(btns2, text="打开数据/日志目录", fg="gray", bd=0, command=open_log).pack(side="right")

    tk.Label(frame, text="提示：首次使用按 ①→③；最小化到托盘后仍会自动签到",
             fg="gray40", font=("Microsoft YaHei", 9)).pack(anchor="w", pady=(8, 0))

    def tip(widget, text):
        holder = {"w": None}
        def enter(_e):
            t = tk.Toplevel(widget)
            t.wm_overrideredirect(True)
            tk.Label(t, text=text, background="#ffffe1", relief="solid", bd=1,
                     font=("Microsoft YaHei", 9)).pack()
            t.wm_geometry("+%d+%d" % (widget.winfo_rootx() + 12,
                                      widget.winfo_rooty() + widget.winfo_height() + 6))
            holder["w"] = t
        def leave(_e):
            if holder["w"]:
                holder["w"].destroy()
                holder["w"] = None
        widget.bind("<Enter>", enter)
        widget.bind("<Leave>", leave)

    tip(state["btn"]["login"], "弹出真实浏览器窗口登录 NS\n（含人机验证，成功后自动关闭并保存登录态）")
    tip(state["btn"]["check"], "马上尝试签到一次\n用于验证登录态是否正常")
    tip(state["btn"]["sched"], "开启后按设置的时间窗每天自动签到\n（本程序/托盘需保持运行，可配合开机自启）")
    tip(b_set, "签到时间窗、自动签到开关、\n关闭×的行为、开机自启")

    def on_close():
        if CONFIG.get("close_action", "tray") == "exit":
            real_quit()
        else:
            minimize_to_tray()
    root.protocol("WM_DELETE_WINDOW", on_close)

    refresh_state()
    if CONFIG.get("auto_start", True) and CONFIG.get("enabled", True):
        root.after(1200, start_scheduler)
    status_var.set("当前：" + login_snapshot_state()[1] + "。流程：登录 → 启动后台调度")
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
