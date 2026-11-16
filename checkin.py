#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ima 每日登录福利 —— 执行签到。

用法：
    python3 checkin.py            # 签到（幂等，已签过会如实报告）
    python3 checkin.py --status   # 只查询不签到
    python3 checkin.py --json     # 机器可读输出
    python3 checkin.py --debug    # 附带原始接口响应，便于排查改版

退出码：0=已领取或今天已领过；1=执行失败；2=凭证失效需重新扫码。
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ima_client as ic  # noqa: E402

CODE_OK, CODE_NOT_FULL_WEEK, CODE_ALREADY = 0, 1001, 1002


def find_item(info, top):
    for item in info.get("infos") or []:
        if item.get("top") == top:
            return item
    return None


def reward_of(resp):
    """奖励字段是 snake_case 的 reward_received。"""
    for key in ("reward_received", "rewardReceived", "reward"):
        v = resp.get(key)
        if v:
            return v
    return None


def week_summary(info):
    items = [i for i in (info.get("infos") or []) if i.get("top") != "满签奖励"]
    if not items:
        return "接口未返回本周条目"
    return "".join("●" if i.get("status") == ic.ST_CLAIMED else "○" for i in items)


def run(dry_run=False, debug=False):
    out = {"ok": False, "action": "none", "rewards": [], "messages": []}
    try:
        cred = ic.load_cred()
    except (FileNotFoundError, ValueError) as exc:
        out["messages"].append(str(exc))
        out["need_relogin"] = True
        return out, 2

    try:
        token = ic.get_token(cred)
    except RuntimeError as exc:
        out["messages"].append(str(exc))
        out["need_relogin"] = True
        return out, 2

    info = ic.get_info(cred, token)
    if info.get("code") != 0:
        out["messages"].append("查询签到状态失败: code=%s msg=%s"
                               % (info.get("code"), info.get("msg")))
        if debug:
            out["raw_info"] = info
        return out, 1
    if debug:
        out["raw_info"] = info

    today = find_item(info, "今日")
    out["today_status"] = today.get("status") if today else None

    # ---- 1) 每日签到 ----
    if today is not None and today.get("status") == ic.ST_AVAILABLE:
        if dry_run:
            out["action"] = "would_checkin"
            out["messages"].append("今天可签到（仅查询，未执行）")
        else:
            resp = ic.check_in(cred, token, ic.CHECKIN_DAILY)
            out["action"] = "checkin"
            if resp.get("code") == CODE_OK:
                got = reward_of(resp)
                out["ok"] = True
                if got:
                    out["rewards"].append({"type": "daily", "points": got})
                    out["messages"].append("签到成功，+%s 算力" % got)
                else:
                    out["messages"].append("签到成功")
            elif resp.get("code") == CODE_ALREADY:
                out["ok"] = True
                out["messages"].append("今天已经签过")
            elif resp.get("code") == CODE_NOT_FULL_WEEK:
                out["messages"].append("本周未满签（%s）" % resp.get("msg"))
            else:
                out["messages"].append("签到失败: code=%s msg=%s"
                                       % (resp.get("code"), resp.get("msg")))
                if debug:
                    out["raw_checkin"] = resp
            # 日签会解锁「满签奖励」，必须重新拉一次状态
            info = ic.get_info(cred, token)
            if debug:
                out["raw_info_after"] = info
    elif today is None:
        out["messages"].append("接口未返回「今日」条目，活动页可能已改版")
        return out, 1
    elif today.get("status") == ic.ST_CLAIMED:
        out["ok"] = True
        out["messages"].append("今天已经签过（本日福利已领取）")
    else:
        out["messages"].append("今天不可签到（status=%s %s）"
                               % (today.get("status"), ic.ST_TEXT.get(today.get("status"), "")))

    # ---- 2) 满签奖励：周期为周一到周日，周日结算 ----
    bonus = find_item(info, "满签奖励")
    if bonus is not None:
        out["bonus_status"] = bonus.get("status")

    if ic.now_cst().weekday() != 6:
        pass  # 非周日静默跳过，不发无效请求
    elif dry_run:
        if bonus and bonus.get("status") == ic.ST_AVAILABLE:
            out["messages"].append("满签奖励可领取（仅查询，未执行）")
        elif bonus and bonus.get("status") == ic.ST_CLAIMED:
            out["messages"].append("满签奖励本期已领")
        else:
            out["messages"].append("满签奖励未解锁（本周 %s 天）" % info.get("checkin_days"))
    elif bonus is not None and bonus.get("status") != ic.ST_CLAIMED:
        resp = ic.check_in(cred, token, ic.CHECKIN_WEEK_BONUS)
        code = resp.get("code")
        if code == CODE_OK:
            got = reward_of(resp)
            out["ok"] = True
            out["rewards"].append({"type": "weekly_bonus", "points": got})
            out["messages"].append("满签奖励已领取%s" % ("，+%s 算力" % got if got else ""))
            info = ic.get_info(cred, token)
        elif code == CODE_NOT_FULL_WEEK:
            out["messages"].append("满签奖励未达条件（本周 %s 天）" % info.get("checkin_days"))
        elif code == CODE_ALREADY:
            out["messages"].append("满签奖励今天已领过")
        else:
            out["messages"].append("满签奖励试领异常 code=%s（不影响日签结果）" % code)

    out["week"] = week_summary(info)
    out["checkin_days"] = info.get("checkin_days")
    out["total_reward_points"] = info.get("total_reward_points")
    out["week_progress"] = week_summary(info)
    return out, 0


def main():
    ap = argparse.ArgumentParser(description="ima 每日签到")
    ap.add_argument("--status", action="store_true", help="只查询，不签到")
    ap.add_argument("--json", dest="as_json", action="store_true", help="输出 JSON")
    ap.add_argument("--debug", action="store_true", help="附带原始响应")
    args = ap.parse_args()

    out, code = run(dry_run=args.status, debug=args.debug)
    out["time"] = ic.now_cst().strftime("%Y-%m-%d %H:%M:%S")

    if args.as_json:
        print(json.dumps(out, ensure_ascii=False))
        return code

    print("【ima 每日签到】%s" % out["time"])
    if out.get("week_progress"):
        print("本周进度：%s" % out["week_progress"])
    if out.get("checkin_days") is not None:
        print("累计：签到 %s 天 / %s 算力" % (out.get("checkin_days"), out.get("total_reward_points")))
    for line in out["messages"]:
        print("· %s" % line)
    return code


if __name__ == "__main__":
    sys.exit(main())
