#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ima 每日登录福利 —— 公共客户端模块（仅标准库，Python 3.8+）。

职责：
  1. 读写凭证（默认 /sandbox/workspace/.ima_cred/credentials.json，可用环境变量
     IMA_CRED_FILE 覆盖；GitHub Actions 里设为 ./credentials.json）
  2. 用长期 refresh_token 换 2 小时有效的 access token
  3. 查询本周签到状态 / 提交签到

为什么凭证单独放：凭证等于账号登录态，技能包会被上传到平台共享，绝不能
把 credentials.json 放在技能目录内。
"""
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = "https://ima.qq.com"
REFRESH_URL = BASE + "/cgi-bin/auth_login/refresh"
LOGIN_URL = BASE + "/cgi-bin/auth_login/login"
INFO_URL = BASE + "/cgi-bin/daily_login_activity/get_activity_info"
CHECKIN_URL = BASE + "/cgi-bin/daily_login_activity/check_in"

QR_CONNECT = "https://open.weixin.qq.com/connect/qrconnect"
QR_IMAGE = "https://open.weixin.qq.com/connect/qrcode/"
QR_POLL = "https://long.open.weixin.qq.com/connect/l/qrconnect?uuid="
WX_APPID = "wx0d63f5de059f1d52"
WX_AUTH = BASE + "/wx-auth/"
CHECKIN_PAGE = BASE + "/copilot-token-daily-checkin/pc"

DEFAULT_CRED = Path("/sandbox/workspace/.ima_cred/credentials.json")

BASE_HEADERS = {
    "Content-Type": "application/json",
    "from_browser_ima": "1",
    "extension_version": "5.17.2",
}
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    )
}

# 服务端状态: 1=已过期 2=已领取 3=可领取 4=未解锁
ST_EXPIRED, ST_CLAIMED, ST_AVAILABLE, ST_LOCKED = 1, 2, 3, 4
ST_TEXT = {0: "未知", 1: "已过期", 2: "已领取", 3: "可领取", 4: "未解锁"}

CHECKIN_DAILY = 1      # 每日签到
CHECKIN_WEEK_BONUS = 2  # 满签奖励（周日结算后解锁）

CST = timezone(timedelta(hours=8))


def now_cst():
    return datetime.now(CST)


def cred_path():
    env = os.environ.get("IMA_CRED_FILE")
    return Path(env) if env else DEFAULT_CRED


def load_cred():
    p = cred_path()
    if not p.exists():
        raise FileNotFoundError(
            "未找到凭证文件 %s。请先运行 scripts/login.py 微信扫码绑定。" % p
        )
    with open(p, "r", encoding="utf-8") as fp:
        cred = json.load(fp)
    missing = [k for k in ("ima_uid", "ima_guid", "refresh_token") if not cred.get(k)]
    if missing:
        raise ValueError("凭证文件缺少字段 %s，请重新扫码绑定。" % ", ".join(missing))
    cred.setdefault("uid_type", "2")
    return cred


def save_cred(cred):
    p = cred_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        p.replace(p.with_suffix(".json.bak"))
    with open(p, "w", encoding="utf-8") as fp:
        json.dump(cred, fp, ensure_ascii=False, indent=2)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def _int32(v):
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v >= 0x80000000 else v


def bkn(token):
    """复刻签到页前端 JS 的 djb2 签名，服务端要求 x-ima-bkn。"""
    r = 5381
    for ch in token:
        r = _int32(_int32(r) << 5) + ord(ch)
    return _int32(r) & 0x7FFFFFFF


def _cookie(cred, token=None):
    parts = [
        "IMA-UID=%s" % cred["ima_uid"],
        "UID-TYPE=%s" % cred.get("uid_type", "2"),
        "IMA-GUID=%s" % cred["ima_guid"],
        "PLATFORM=H5",
    ]
    if token:
        parts.insert(1, "IMA-TOKEN=%s" % token)
    if cred.get("refresh_token"):
        parts.insert(2, "IMA-REFRESH-TOKEN=%s" % cred["refresh_token"])
    return "; ".join(parts)


def post_json(url, body, cookie, token=None, timeout=25):
    """POST JSON。服务端被拒多次后会回 HTTP 403 + 空 body，这里统一兜住。"""
    headers = dict(BASE_HEADERS)
    headers["x-ima-cookie"] = cookie
    if token:
        headers["x-ima-bkn"] = str(bkn(token))
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw, status = resp.read().decode("utf-8", "replace"), resp.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read().decode("utf-8", "replace"), exc.code
    except urllib.error.URLError as exc:
        return {"code": -1, "msg": "网络错误: %s" % exc, "http_status": 0}
    if not raw.strip():
        return {"code": -1, "msg": "空响应（通常是被限流，稍后重试）", "http_status": status}
    try:
        data = json.loads(raw)
    except ValueError:
        return {"code": -1, "msg": "非 JSON 响应: %s" % raw[:200], "http_status": status}
    if isinstance(data, dict):
        data.setdefault("http_status", status)
    return data


def get_token(cred):
    """refresh_token → access token。失败说明凭证彻底失效，需要重新扫码。"""
    data = post_json(
        REFRESH_URL,
        {
            "userId": cred["ima_uid"],
            "refreshToken": cred["refresh_token"],
            "tokenType": -1,
        },
        "IMA-GUID=%s; PLATFORM=H5" % cred["ima_guid"],
    )
    token = data.get("token")
    if data.get("code") != 0 or not token:
        raise RuntimeError(
            "登录态刷新失败（code=%s msg=%s）。refresh_token 已失效，"
            "需要重新扫码绑定：python scripts/login.py" % (data.get("code"), data.get("msg"))
        )
    return token


def get_info(cred, token):
    return post_json(INFO_URL, {}, _cookie(cred, token), token)


def check_in(cred, token, kind):
    return post_json(CHECKIN_URL, {"type": kind}, _cookie(cred, token), token)
