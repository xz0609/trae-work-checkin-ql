#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cron "0 8,20 * * *" script-path=trae_work_checkin.py,tag=TRAE签到
new Env('Trae Work 签到')

Trae Work（字节，trae.cn 国内版）每日积分自动签到 · 单文件 · 零依赖（仅 Python 标准库）。

用法：
  python trae_work_checkin.py login    交互式登录（Windows 本地执行），凭证保存到 auths/auth-<uid>.json
  python trae_work_checkin.py checkin   执行签到：检查状态 -> 领取（无参数等同）
  python trae_work_checkin.py           同上，执行签到

凭证：仅读取脚本同目录 auths/auth-<uid>.json 文件（本地登录生成；青龙部署时把该文件
     上传到容器脚本目录即可）。凭证不读环境变量；
     可选环境变量 RANDOM_SIGNIN / MAX_RANDOM_DELAY 控制签到前的随机延时（见下方常量区）。

可选通知：脚本目录放置 notify.py（青龙面板自带）后自动发送签到结果。

免责声明：本脚本为第三方逆向脚本，与官方无关，接口随时可能失效，仅供个人学习研究。
"""
import base64
import json
import logging
import os
import random
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
AUTH_DIR = BASE_DIR / "auths"

API_BASE = "https://api.trae.cn"
AUTH_BASE = "https://api.trae.com.cn"
CLIENT_ID = "en1oxy7wnw8j9n"
IDE_VERSION = "0.1.52"
UA = f"Trae/{IDE_VERSION}"

REFRESH_MARGIN = 24 * 3600      # 提前 24 小时判定 token 即将过期
LOGIN_TIMEOUT = 300             # 登录会话最长 5 分钟

# 随机延时（青龙面板友好）：签到前先睡一个随机时长，避免大量账号同时刻打卡。
#   RANDOM_SIGNIN  是否启用随机延时（默认 true）
#   MAX_RANDOM_DELAY  随机延时的上限秒数（默认 3600，即最多 1 小时）
RANDOM_SIGNIN = os.getenv("RANDOM_SIGNIN", "true").lower() == "true"
MAX_RANDOM_DELAY = int(os.getenv("MAX_RANDOM_DELAY", "3600"))

log = logging.getLogger("trae_checkin")


# ---------------------------------------------------------------------------
# 通知（青龙面板 notify.py 可选）
# ---------------------------------------------------------------------------
try:
    from notify import send as _ql_send
    _HAS_NOTIFY = True
except Exception:
    _HAS_NOTIFY = False


def notify(title: str, content: str) -> None:
    """发送青龙面板通知；无 notify.py 时仅打印。"""
    if _HAS_NOTIFY:
        try:
            _ql_send(title, content)
            log.info("通知已发送: %s", title)
        except Exception as e:
            log.warning("通知发送失败: %s", e)
    else:
        print(f"\n📢 {title}\n📄 {content}")


# ---------------------------------------------------------------------------
# HTTP 小工具（仅标准库 urllib）
# ---------------------------------------------------------------------------
def http_request(method: str, url: str, headers: dict, body: dict | None = None,
                 timeout: int = 30) -> dict:
    data = None
    hdrs = dict(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
        hdrs.setdefault("Content-Length", str(len(data)))

    req = urllib.request.Request(url, data=data, method=method)
    for k, v in hdrs.items():
        req.add_header(k, v)

    if os.environ.get("DEBUG_HTTP") == "1":
        print(f"[http] {method} {url}")
        if body is not None:
            print(f"[http]   body: {body}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            if os.environ.get("DEBUG_HTTP") == "1":
                print(f"[http]   status: {resp.status}  resp: {raw[:500]}")
            try:
                return json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return {"_raw": raw, "_status": resp.status}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
        if os.environ.get("DEBUG_HTTP") == "1":
            print(f"[http]   HTTPError {e.code}: {raw[:500]}")
        try:
            return json.loads(raw) if raw else {"_status": e.code}
        except json.JSONDecodeError:
            return {"_raw": raw, "_status": e.code}
    except urllib.error.URLError as e:
        raise RuntimeError(f"网络请求失败: {e.reason}") from e


def decode_jwt_payload(token: str) -> dict:
    """解析 JWT payload 段（不校验签名，用于提取 uid 等展示字段）。"""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Trae 设备指纹（rockswang/workbuddy-wild 逆向结论）
#   9074「当前参与用户太多」实测不是随机限流，而是设备指纹校验失败：
#   - x-device-id 必须用官方客户端注册的真实 device_id，随机 hex 过不了校验
#   - 还需补齐 x-device-brand / x-device-type / x-os-version / x-app-version
#   - 同一真实设备一天只能签一个账号（否则 9095）
# ---------------------------------------------------------------------------
def parse_tt_net_config(raw: str) -> str:
    m = re.search(r"device_id&#\*([^@$]*)@\$\*", raw)
    return m.group(1).strip() if m else ""


def trae_device_pool() -> list:
    """扫描本机字节系客户端注册的真实设备 ID，去重保序。青龙容器内通常为空。"""
    bases = [
        Path.home() / "Library" / "Application Support",   # macOS
        Path.home() / "AppData" / "Roaming",               # Windows
    ]
    pool = []
    for base in bases:
        try:
            configs = sorted(base.glob("*/ahanet/tt_net_config.config"))
        except OSError:
            continue
        for p in configs:
            try:
                dev = parse_tt_net_config(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            if dev and dev not in pool:
                pool.append(dev)
    return pool


def resolve_trae_device_id(fallback: str = "") -> str:
    candidates = [
        Path.home() / "AppData" / "Roaming" / "TRAE SOLO CN" / "machineid",
        Path.home() / "AppData" / "Roaming" / "Trae CN" / "machineid",
        Path.home() / "Library" / "Application Support" / "TRAE SOLO CN" / "machineid",
        Path.home() / "Library" / "Application Support" / "Trae CN" / "machineid",
    ]
    for p in candidates:
        try:
            if p.is_file():
                v = p.read_text(encoding="utf-8").strip()
                if v:
                    return v
        except OSError:
            continue
    return fallback


_FINGERPRINT_CACHE: dict = {}


def trae_fingerprint() -> tuple:
    """返回 (device_brand, device_type, os_version)，尽量模仿官方客户端。"""
    if _FINGERPRINT_CACHE:
        return _FINGERPRINT_CACHE["v"]

    import platform
    import subprocess

    system = platform.system()
    if system == "Darwin":
        brand = "Apple"
        osver = ""
        try:
            brand = subprocess.run(["sysctl", "-n", "hw.model"], capture_output=True,
                                   text=True, timeout=5).stdout.strip() or "Apple"
        except Exception:
            pass
        try:
            osver = "macOS " + subprocess.run(["sw_vers", "-productVersion"],
                                              capture_output=True, text=True,
                                              timeout=5).stdout.strip()
        except Exception:
            osver = f"macOS {platform.mac_ver()[0]}"
        result = (brand, "mac", osver)
    else:
        result = ("Unknown", "windows", platform.platform())

    _FINGERPRINT_CACHE["v"] = result
    return result


# ---------------------------------------------------------------------------
# 凭证读写
# ---------------------------------------------------------------------------
def uid_of(uid: str) -> str:
    uid = str(uid or "").strip()
    return uid or f"trae-{uuid.uuid4().hex[:6]}"


def cred_path(uid: str) -> Path:
    return AUTH_DIR / f"auth-{uid}.json"


def persist_cred(uid: str, cred: dict) -> Path:
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    p = cred_path(uid)
    p.write_text(json.dumps(cred, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def load_creds() -> list:
    """读取 auths/auth-<uid>.json 全部凭证，按 userId 去重。返回 [(source, uid, cred)]。"""
    items: dict = {}   # key = userId or uid
    try:
        files = sorted(AUTH_DIR.glob("auth-*.json"))
    except OSError:
        files = []
    for p in files:
        try:
            cred = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            print(f"⚠️  跳过无法解析的凭证 {p.name}")
            continue
        uid = p.stem[len("auth-"):]
        key = str(cred.get("userId") or uid)
        items.setdefault(key, ("file", uid, cred))
    return list(items.values())


# ---------------------------------------------------------------------------
# Trae 签到
# ---------------------------------------------------------------------------
class Trae:
    def __init__(self, cred: dict, uid: str):
        self.uid = uid
        self.user_id = cred.get("userId", "")
        self.screen_name = cred.get("screenName", "")
        self.access_token = cred["accessToken"]
        self.refresh_token = cred.get("refreshToken", "")
        self.expires_at = cred.get("expiresAt", 0)
        self.pool = trae_device_pool()
        # 设备优先级：登录时绑定的 claimDeviceId > 本机真实设备池 > 旧 deviceId
        self.device_id = (cred.get("claimDeviceId")
                          or (self.pool[0] if self.pool else None)
                          or resolve_trae_device_id(cred.get("deviceId", "")))
        # 指纹与 claimDeviceId 绑定：优先用登录时随凭证保存的属性，保证与设备一致
        fp = cred.get("fingerprint") or {}
        self.brand = fp.get("brand", "")
        self.dev_type = fp.get("type", "")
        self.os_version = fp.get("os", "")

    @property
    def display(self) -> str:
        return self.screen_name or self.user_id or self.uid

    # --- 凭证 ----------------------------------------------------------
    def needs_refresh(self) -> bool:
        return self.expires_at - time.time() < REFRESH_MARGIN

    def to_cred(self) -> dict:
        return {
            "userId": self.user_id,
            "screenName": self.screen_name,
            "accessToken": self.access_token,
            "refreshToken": self.refresh_token,
            "expiresAt": self.expires_at,
            "deviceId": self.device_id,
            "claimDeviceId": self.device_id,
            "fingerprint": {"brand": self.brand, "type": self.dev_type, "os": self.os_version},
        }

    # --- token 刷新 ----------------------------------------------------
    def refresh(self) -> bool:
        body = {
            "ClientID": CLIENT_ID,
            "RefreshToken": self.refresh_token,
            "ClientSecret": "-",
            "UserID": self.user_id or "",
        }
        try:
            data = http_request("POST", f"{AUTH_BASE}/cloudide/api/v3/trae/oauth/ExchangeToken",
                                {"Content-Type": "application/json"}, body)
        except RuntimeError as e:
            log.warning("[Trae/%s] 刷新请求失败: %s", self.display, e)
            return False
        result = data.get("Result") or data.get("data") or {}
        new_access = result.get("Token") or result.get("AccessToken") or result.get("access_token")
        new_refresh = result.get("RefreshToken") or result.get("refresh_token") or self.refresh_token
        expire_at = result.get("TokenExpireAt") or result.get("expiresAt")
        if not new_access:
            log.warning("[Trae/%s] 刷新返回无 token", self.display)
            return False
        self.access_token = new_access
        self.refresh_token = new_refresh
        # TokenExpireAt 为毫秒时间戳，兼容秒
        if expire_at:
            self.expires_at = expire_at / 1000 if expire_at > 1e12 else expire_at
        else:
            self.expires_at = time.time() + 3600
        return True

    # --- 业务接口 ------------------------------------------------------
    def _resolve_fingerprint(self) -> tuple:
        """指纹优先用凭证里随 claimDeviceId 保存的属性，缺失时回退本机探测。"""
        if self.brand and self.dev_type and self.os_version:
            return self.brand, self.dev_type, self.os_version
        return trae_fingerprint()

    def _auth_headers(self) -> dict:
        brand, dev_type, osver = self._resolve_fingerprint()
        return {
            "Content-Type": "application/json",
            "Authorization": f"Cloud-IDE-JWT {self.access_token}",   # 注意前缀
            "x-device-id": self.device_id,
            "X-Device-Id": self.device_id,
            "X-User-Region": "CN",
            "x-device-brand": brand,
            "x-device-type": dev_type,
            "x-os-version": osver,
            "x-app-version": IDE_VERSION,
            "User-Agent": UA,
        }

    def check_status(self) -> dict:
        return http_request("POST", f"{API_BASE}/trae/api/v2/ug/checkin_credits/status",
                            self._auth_headers(), body={})

    def do_claim(self) -> dict:
        return http_request("POST", f"{API_BASE}/trae/api/v2/ug/checkin_credits/claim",
                            self._auth_headers(), body={})

    def query_credits(self) -> dict:
        return http_request("POST", f"{API_BASE}/trae/api/v2/pay/ide_user_ent_usage",
                            self._auth_headers(), body={})

    # --- 主流程 --------------------------------------------------------
    def run(self) -> dict:
        result = {"display": self.display, "uid": self.uid}

        if self.needs_refresh():
            log.info("[Trae/%s] token 即将过期，尝试刷新...", self.display)
            if not self.refresh():
                result["status"] = "token 失效，请重新 login"
                result["ok"] = False
                return result
            persist_cred(uid_of(self.user_id or self.uid), self.to_cred())
        elif self.device_id:
            persist_cred(uid_of(self.user_id or self.uid), self.to_cred())

        # 先查状态
        try:
            self.check_status()
        except RuntimeError as e:
            result["status"] = f"查询状态失败: {e}"
            result["ok"] = False
            return result

        # 领取积分（服务端可能返回 9074，重试几次）
        claim_retries = 3
        r = None
        auth_retried = False
        for attempt in range(1, claim_retries + 1):
            try:
                r = self.do_claim()
                code = r.get("code")
                if code == 9074 and attempt < claim_retries:
                    wait = 20 * attempt
                    log.info("[Trae/%s] 服务端限流(9074)，%ds 后重试(%d/%d)",
                             self.display, wait, attempt, claim_retries)
                    time.sleep(wait)
                    continue
                if code == 1001 and not auth_retried:
                    log.info("[Trae/%s] 认证失败(1001)，刷新 token 后重试", self.display)
                    auth_retried = True
                    if self.refresh():
                        persist_cred(uid_of(self.user_id or self.uid), self.to_cred())
                        continue
                break
            except RuntimeError as e:
                result["status"] = f"领取请求失败: {e}"
                result["ok"] = False
                return result

        # 9074 兜底：当前设备过不了指纹校验时，轮换本机真实设备池其他设备各试一次
        if isinstance(r, dict) and r.get("code") == 9074:
            for alt in self.pool:
                if alt == self.device_id:
                    continue
                log.info("[Trae/%s] 9074，轮换真实设备 %s... 再试", self.display, alt[:6])
                self.device_id = alt
                try:
                    r2 = self.do_claim()
                except RuntimeError:
                    continue
                if r2.get("code") in (0, 200) or (r2.get("data") or {}).get("success"):
                    r = r2
                    persist_cred(uid_of(self.user_id or self.uid), self.to_cred())
                    break
                if r2.get("code") == 9095:
                    log.info("[Trae/%s] 设备 %s... 今日已签到(9095)，继续轮换", self.display, alt[:6])

        result["status"] = self._parse_claim_result(r)

        # 查积分
        try:
            result["credits"] = self._parse_credits(self.query_credits())
        except RuntimeError as e:
            result["credits"] = f"查询积分失败: {e}"

        result["ok"] = self._status_ok(result["status"])
        return result

    # --- 结果解析 ------------------------------------------------------
    @staticmethod
    def _status_ok(status: str) -> bool:
        return not any(k in str(status) for k in (
            "token 失效", "认证失败", "查询状态失败", "领取请求失败", "领取结果: code="))

    @staticmethod
    def _parse_claim_result(r: dict) -> str:
        if not isinstance(r, dict):
            return f"领取结果: code=网络异常"
        code = r.get("code")
        msg = r.get("message") or r.get("Message") or ""
        data = r.get("data") or r.get("Result") or {}
        if code in (0, 200) or r.get("success") or data.get("success"):
            return "领取成功"
        if code == 9074:
            return f"服务端限流，领取失败 ({msg})"
        for kw in ("已签到", "已领取", "明日再来", "already", "checked", "claimed"):
            if kw in str(msg) or kw in str(r):
                return f"今日已领取 ({msg})"
        if code == 1001:
            return f"认证失败，无法确认领取状态 ({msg})"
        return f"领取结果: code={code} msg={msg}"

    @staticmethod
    def _parse_credits(r: dict) -> str:
        def fmt(v) -> str:
            f = float(v)
            return str(int(f)) if f == int(f) else f"{round(f, 2):g}"

        # 真实剩余积分 = usage_summary 的 total_amount - consumed_amount
        summary = r.get("usage_summary") or {}
        total = summary.get("total_amount")
        consumed = summary.get("consumed_amount")
        if total is not None and consumed is not None:
            return f"当前积分: {fmt(total - consumed)} (总额 {fmt(total)} / 已用 {fmt(consumed)})"

        packs = r.get("user_entitlement_pack_list") or []
        if packs:
            s = 0.0
            parts = []
            for pk in packs:
                quota = (pk.get("entitlement_base_info") or {}).get("quota") or pk.get("quota") or {}
                limit = quota.get("credits_limit") or 0
                if not limit:
                    continue
                used = (pk.get("usage") or {}).get("credits_amount") or 0
                s += limit - used
                name = pk.get("group_name") or pk.get("display_desc") or ""
                parts.append(f"{name}={fmt(limit - used)}")
            if parts:
                return f"当前积分: {fmt(s)} ({', '.join(parts[:3])})"
            return json.dumps(packs[0], ensure_ascii=False)[:120]

        data = r.get("data") or r.get("Result") or {}
        credits = (data.get("credits") or data.get("balance")
                   or data.get("totalCredits") or data.get("workCredits"))
        if credits is None:
            return json.dumps(r, ensure_ascii=False)[:120]
        return f"当前积分: {credits}"


# ---------------------------------------------------------------------------
# 交互式登录（Windows 本地执行）
# ---------------------------------------------------------------------------
class _CallbackHandler(BaseHTTPRequestHandler):
    """接收 OAuth 回调，把 query 存到 server.auth_result。"""

    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        self.server.auth_result = {k: (v[0] if v else "") for k, v in query.items()}  # type: ignore
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            "<html><body style='font-family:sans-serif;text-align:center;padding:40px'>"
            "<h2>登录成功</h2><p>凭证已捕获，现在可以关闭此页面，回到命令行查看结果。</p>"
            "</body></html>".encode("utf-8")
        )

    def log_message(self, fmt, *args):
        log.debug("callback http: %s", fmt % args)


def _bind_callback_server() -> HTTPServer:
    for port in range(18080, 18100):
        try:
            server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
            server.auth_result = None  # type: ignore
            server.timeout = 1
            return server
        except OSError:
            continue
    raise RuntimeError("18080-18099 端口均被占用，无法启动登录回调服务")


def _parse_user_identity(access_token: str) -> tuple:
    """从 accessToken 的 JWT payload.data.id 解析 user_id / screen_name。"""
    jwt_data = core_like_decode_data(access_token)
    user_id = str(jwt_data.get("id") or jwt_data.get("UserID")
                  or jwt_data.get("uid") or "")
    screen_name = str(jwt_data.get("screen_name") or jwt_data.get("ScreenName")
                      or jwt_data.get("name") or "")
    return user_id, screen_name


def core_like_decode_data(access_token: str) -> dict:
    """取 JWT payload 的 data 段（兼容 dict / str 两种形态）。"""
    data = decode_jwt_payload(access_token).get("data") or {}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            data = {}
    return data if isinstance(data, dict) else {}


def _assign_claim_device() -> str:
    """分配一个本机真实注册设备 ID，尽量避免与已存账号重复（同设备一天一账号）。"""
    pool = trae_device_pool()
    used = set()
    for _src, _u, cred in load_creds():
        if cred.get("claimDeviceId"):
            used.add(cred["claimDeviceId"])
    for dev in pool:
        if dev not in used:
            return dev
    return pool[0] if pool else ""


def do_login() -> int:
    """交互式登录：打开浏览器 -> 捕获回调 -> 换 token -> 存 auths/auth-<uid>.json。"""
    print("== Trae Work 登录 ==")
    device_id = secrets.token_hex(16)
    machine_id = secrets.token_hex(16)
    server = None
    try:
        server = _bind_callback_server()
    except RuntimeError as e:
        print(f"❌ {e}")
        return 1
    port = server.server_address[1]

    params = {
        "client_id": CLIENT_ID,
        "auth_from": "solo",
        "login_channel": "native_ide",
        "plugin_version": "2.3.62834",
        "auth_callback_url": f"http://127.0.0.1:{port}/authorize",
        "x_app_version": IDE_VERSION,
        "x_app_type": "stable",
        "device_id": device_id,
        "machine_id": machine_id,
    }
    auth_url = "https://www.trae.cn/authorization?" + urllib.parse.urlencode(params)

    captured: dict = {}

    def _wait():
        start = time.time()
        try:
            while time.time() - start < LOGIN_TIMEOUT:
                server.handle_request()
                if getattr(server, "auth_result", None):
                    captured.update(server.auth_result)
                    return
        finally:
            server.server_close()

    threading.Thread(target=_wait, daemon=True).start()

    print("正在打开浏览器登录页，请用手机号 + 验证码登录...")
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass
    print(f"如果未自动打开，请手动访问：\n{auth_url}")
    print(f"\n回调服务器监听 http://127.0.0.1:{port}/authorize")
    print("若浏览器自动跳转到 127.0.0.1 页面但打不开，请把地址栏里那条完整地址粘贴到下面。\n")

    answer = input("完成登录后回车继续；或粘贴回调地址再回车: ").strip()
    if answer.startswith("http"):
        parsed = urllib.parse.urlparse(answer)
        captured.update({k: (v[0] if v else "") for k, v in
                         urllib.parse.parse_qs(parsed.query).items()})

    if not captured:
        print("❌ 未捕获到回调参数，请重试。")
        return 1

    refresh_token = (captured.get("refreshToken") or captured.get("refresh_token")
                     or captured.get("data"))
    if not refresh_token:
        print(f"❌ 回调中没有 refreshToken: {captured}")
        return 1

    # userInfo / userJwt 部分版本会带，统一解析
    def _parse_json_param(key: str) -> dict:
        if key not in captured:
            return {}
        try:
            v = json.loads(urllib.parse.unquote(captured[key]))
            return v if isinstance(v, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    user_info = _parse_json_param("userInfo")
    user_jwt = _parse_json_param("userJwt")
    access_token = (user_jwt.get("Token") or user_jwt.get("token")
                    or user_jwt.get("AccessToken"))
    user_id = str(user_info.get("UserID") or user_jwt.get("UserID") or "")
    screen_name = str(user_info.get("ScreenName") or user_info.get("screenName") or "")
    bound_device_id = device_id
    expire_at = None

    if not access_token:
        body = {
            "ClientID": CLIENT_ID,
            "RefreshToken": refresh_token,
            "ClientSecret": "-",
            "UserID": user_id or "",
        }
        try:
            exch = http_request("POST", f"{AUTH_BASE}/cloudide/api/v3/trae/oauth/ExchangeToken",
                                {"Content-Type": "application/json"}, body)
        except RuntimeError as e:
            print(f"❌ ExchangeToken 请求失败: {e}")
            return 1
        result = exch.get("Result") or exch.get("data") or {}
        access_token = result.get("Token") or result.get("AccessToken")
        new_refresh = result.get("RefreshToken")
        if new_refresh:
            refresh_token = new_refresh
        bound_device_id = result.get("BoundDeviceID") or device_id
        expire_at = result.get("TokenExpireAt")
        if not access_token:
            print(f"❌ ExchangeToken 未返回 Token: {json.dumps(exch, ensure_ascii=False)[:200]}")
            return 1
    else:
        expire_at = user_jwt.get("TokenExpireAt") or captured.get("refreshExpireAt")

    # 回调本身不带用户信息，身份藏在 accessToken 的 JWT 里（payload.data.id）
    jwt_data = core_like_decode_data(access_token)
    user_id = user_id or str(jwt_data.get("id") or jwt_data.get("UserID")
                             or jwt_data.get("uid") or "")
    screen_name = screen_name or str(jwt_data.get("screen_name")
                                     or jwt_data.get("ScreenName") or "")

    expires_at = time.time() + 3600
    try:
        expire_at = float(expire_at) if expire_at else None
    except (TypeError, ValueError):
        expire_at = None
    if expire_at:
        expires_at = expire_at / 1000 if expire_at > 1e12 else expire_at

    cred = {
        "userId": user_id,
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "expiresAt": expires_at,
        "deviceId": bound_device_id,
        "screenName": screen_name,
        "machineId": machine_id,
        "claimDeviceId": _assign_claim_device(),
        # 保存登录机的设备属性，使签到侧能发出与 claimDeviceId 一致、可过校验的指纹头
        "fingerprint": dict(zip(
            ("brand", "type", "os"), trae_fingerprint())),
    }

    uid = uid_of(user_id or screen_name)
    path = persist_cred(uid, cred)
    print(f"\n✅ 登录成功")
    print(f"   用户: {screen_name or user_id or uid}")
    print(f"   凭证已保存: {path}")
    print(f"   积分领取设备: {cred['claimDeviceId'] or '无（需本机字节客户端）'}")
    return 0


# ---------------------------------------------------------------------------
# 签到（青龙模式）
# ---------------------------------------------------------------------------
def _fmt_seconds(seconds: int) -> str:
    """把秒数格式化为时/分/秒，便于倒计时展示。"""
    if seconds <= 0:
        return "立即执行"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}小时{m}分{s}秒"
    if m > 0:
        return f"{m}分{s}秒"
    return f"{s}秒"


def _random_delay(label: str = "") -> None:
    """每个账号签到前独立随机延时，带倒计时打印，避免青龙面板判定任务卡死。"""
    if not RANDOM_SIGNIN or MAX_RANDOM_DELAY <= 0:
        return
    who = f"[{label}] " if label else ""
    delay = random.randint(0, MAX_RANDOM_DELAY)
    print(f"{who}⏳ 随机延时 {_fmt_seconds(delay)} 后开始签到")
    remaining = delay
    while remaining > 0:
        if remaining <= 10 or remaining % 10 == 0:
            print(f"   倒计时: {_fmt_seconds(remaining)}")
        step = 1 if remaining <= 10 else min(10, remaining)
        time.sleep(step)
        remaining -= step
    print(f"{who}随机延时结束，开始签到")


def do_checkin() -> int:
    creds = load_creds()
    if not creds:
        msg = "未找到任何凭证。请先执行 login 登录（生成 auths/auth-<uid>.json）。"
        print(f"⚠️  {msg}")
        notify("Trae Work 签到", f"⚠️ {msg}")
        return 1

    print(f"== Trae Work 签到  共 {len(creds)} 个账号 ==")
    lines = []
    all_ok = True
    for _src, uid, cred in creds:
        _random_delay(uid)
        try:
            res = Trae(cred, uid).run()
        except Exception as e:
            log.exception("签到异常 %s", uid)
            res = {"status": f"发生异常: {e}", "ok": False, "display": uid, "uid": uid, "credits": ""}
        print(f"\n[{res['display']}]")
        print(f"  状态: {res['status']}")
        if res.get("credits"):
            print(f"  积分: {res['credits']}")
        lines.append(f"[{res['display']}] {res['status']} {res.get('credits', '')}")
        if not res["ok"]:
            all_ok = False

    content = "\n".join(lines)
    notify("Trae Work 签到完成", content)
    print("\n== 完成 ==")
    return 0 if all_ok else 2


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def usage() -> int:
    print(__doc__)
    print("用法:")
    print("  python trae_work_checkin.py login     交互式登录并保存凭证")
    print("  python trae_work_checkin.py checkin   执行签到（默认，可不带参数）")
    return 1


def main() -> int:
    if len(sys.argv) > 2:
        print("多余的参数，忽略。")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "checkin"
    if cmd == "login":
        return do_login()
    if cmd == "checkin":
        return do_checkin()
    return usage()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")
    sys.exit(main())