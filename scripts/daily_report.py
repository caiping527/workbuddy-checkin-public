#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日积分播报：汇总各账号 result.json，生成榜单并推一条到企业微信。

与小浣熊「每日积分播报」同一套形态，但不依赖 AiRoute、不改动小浣熊：
  · 各 checkin job 把该账号的 result.json 上传为 artifact
  · 本脚本在独立的 report job 里下载全部 artifact，逐个账号列出
    「当前积分」「还有多久开始过期」「建议提前一天用掉」，推一条到手机
  · 只读白名单字段，绝不输出令牌 / uid / 接口原始响应
  · 任何异常都吞掉并返回 0：播报失败不影响签到结果，也不影响心跳

result.json 里的积分字段（由各签到脚本写入，二选一，本脚本都认）：
  detail.expiry = {credits, earliest_expire_date, days_left,
                   earliest_remaining, use_by, label, error}
  或顶层 expiry = {...}

用法：
  python daily_report.py _report --title WorkBuddy
  python daily_report.py _report --title Qoder
开关：RECOMMEND_NOTIFY=0 / off / false / no / none / disabled 关闭推送
通道：WECOM_WEBHOOK（群机器人完整 webhook 或仅 key）、WECOM_APP（corpid,corpsecret,touser,agentid）
"""

import glob
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

QYAPI = "https://qyapi.weixin.qq.com/cgi-bin"
BEIJING = timezone(timedelta(hours=8))


def truthy(v):
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def num(v):
    try:
        return round(float(v or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def load_rows(root):
    """读取所有 result.json，一个账号一行。缺数据的账号也保留一行。"""
    rows = []
    for path in sorted(glob.glob(os.path.join(root, "**", "result.json"), recursive=True)):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        detail = data.get("detail")
        exp = {}
        if isinstance(detail, dict) and isinstance(detail.get("expiry"), dict):
            exp = detail["expiry"]
        elif isinstance(data.get("expiry"), dict):
            exp = data["expiry"]
        account = str(data.get("account") or os.path.basename(os.path.dirname(path)) or "未命名账号")
        status = str(data.get("status") or data.get("outcome") or "unknown")
        message = str(data.get("message") or data.get("msg") or "")
        err = str(exp.get("error") or "")
        credits = num(exp.get("credits")) if not err else 0.0
        days = exp.get("days_left")
        rows.append({
            "account": account,
            "status": status,
            "message": message,
            "has_data": bool(exp) and not err and credits > 0,
            "credits": credits,
            "expire_date": str(exp.get("earliest_expire_date") or ""),
            "days_left": days if isinstance(days, (int, float)) else None,
            "remaining": num(exp.get("earliest_remaining")),
            "use_by": str(exp.get("use_by") or ""),
            "label": str(exp.get("label") or "开始过期"),
            "error": err,
        })
    # 与 AiRoute「积分先到期先用」一致：到期日近的在前；同日则临期待用积分
    # 多的在前（最该先消耗）；再按总积分、账号名稳定排序。
    rows.sort(key=lambda r: (r["days_left"] if r["days_left"] is not None else 999,
                             -r["remaining"], -r["credits"], r["account"]))
    return rows


def expiry_sentence(r):
    if not r["has_data"]:
        if r["error"]:
            return "积分数据未取到（%s）" % r["error"][:50]
        return "积分数据未取到（本次签到可能失败，见下方告警）"
    if not r["expire_date"]:
        return "当前积分未标注到期日"
    head = "%s %s（剩 %s 天，这批 %g 分）" % (
        r["expire_date"], r["label"],
        r["days_left"] if r["days_left"] is not None else "?", r["remaining"])
    if isinstance(r["days_left"], (int, float)) and r["days_left"] <= 1:
        return head + "；⚠️ 今明两天就到期，今天就用掉"
    if r["use_by"]:
        return head + "；建议 %s 前用掉，提前一天" % r["use_by"]
    return head


def build_text(rows, title, run_url=None):
    """企微纯文本，不用 Markdown 符号，手机上看得清。"""
    today = datetime.now(BEIJING).strftime("%Y-%m-%d")
    lines = ["【%s · 每日积分播报】%s" % (title, today),
             "每个号的当前积分，以及积分还有多久开始过期、该提前哪天用掉：", ""]
    if not rows:
        lines.append("本次没有收到任何账号的积分数据，请查看运行详情。")
        if run_url:
            lines.append("运行详情：%s" % run_url)
        return "\n".join(lines)
    # 缺口可见：少一个号时，数量本身就能提醒（小浣熊播报同一处理）
    bad = [r for r in rows if not r["has_data"]]
    lines.append("本次收到 %d 个账号的积分数据%s" % (
        len(rows), ("，其中 %d 个未取到（见上方）" % len(bad)) if bad else ""))
    lines.append("")
    for i, r in enumerate(rows, 1):
        lines.append("%d. %s" % (i, r["account"]))
        lines.append("   当前积分：%s" % ("%g 分" % r["credits"] if r["has_data"] else "未取到"))
        lines.append("   %s" % expiry_sentence(r))
        if r["status"] in ("error", "failed") and r["message"]:
            lines.append("   ⚠️ 签到状态：%s" % r["message"][:60])
        lines.append("")
    soon = [r for r in rows if r["has_data"] and r["days_left"] is not None and r["days_left"] <= 7]
    if soon:
        lines.append("建议：今天优先用 %s 跑代码，临期的先用，别拖到过期当天。" % soon[0]["account"])
    else:
        lines.append("近 7 天没有临期积分，不用特意切号。")
    lines.append("注：数据为云端只读余额查询的聚合结果，与 AiRoute「积分先到期先用」口径一致。")
    if run_url:
        lines.append("运行详情：%s" % run_url)
    return "\n".join(lines)


def build_report(rows, title):
    """Markdown 榜单，写进 Actions Summary。"""
    lines = ["#### %s · 每日积分播报（按最早到期日排序）" % title, "",
             "| 排名 | 账号 | 当前积分 | 到期日 | 剩几天 | 该批积分 | 建议用掉日 | 签到状态 |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for i, r in enumerate(rows, 1):
        lines.append("| %d | %s | %s | %s | %s | %s | %s | %s |" % (
            i, r["account"],
            "**%g**" % r["credits"] if r["has_data"] else "未取到",
            r["expire_date"] or "—",
            r["days_left"] if r["days_left"] is not None else "—",
            "%g 分" % r["remaining"] if r["has_data"] else "—",
            r["use_by"] or "—",
            "失败" if r["status"] in ("error", "failed") else "成功"))
    lines.append("")
    lines.append("> 当前积分取云端只读余额接口；到期日取「还没用掉且最早到期」的那一批积分。")
    if not rows:
        lines.append("")
        lines.append("- 本次没有收到任何账号的积分数据。")
    return "\n".join(lines) + "\n"


def post_json(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def push_webhook(value, text):
    url = value if value.startswith("http") else (QYAPI + "/webhook/send?key=" + value)
    r = post_json(url, {"msgtype": "text", "text": {"content": text}})
    return "webhook:%s" % (r.get("errmsg") or r.get("errcode"))


def push_app(value, text):
    parts = [p.strip() for p in value.split(",")]
    if len(parts) < 4:
        return "app:配置格式错误（应为 corpid,corpsecret,touser,agentid）"
    corpid, secret, touser, agentid = parts[0], parts[1], parts[2] or "@all", parts[3]
    token_url = "%s/gettoken?corpid=%s&corpsecret=%s" % (
        QYAPI, urllib.parse.quote(corpid, safe=""), urllib.parse.quote(secret, safe=""))
    with urllib.request.urlopen(token_url, timeout=15) as resp:
        t = json.loads(resp.read().decode("utf-8", "replace"))
    if t.get("errcode") != 0:
        return "app:gettoken失败 %s" % t.get("errmsg")
    r = post_json("%s/message/send?access_token=%s" % (QYAPI, t["access_token"]), {
        "touser": touser, "msgtype": "text", "agentid": agentid,
        "text": {"content": text}, "safe": 0,
    })
    bits = ["errcode=%s" % r.get("errcode"), "errmsg=%s" % (r.get("errmsg") or "-")]
    if r.get("invaliduser"):
        bits.append("invaliduser=%s" % r["invaliduser"])
    if r.get("msgid"):
        bits.append("msgid=%s" % r["msgid"])
    return "app:" + " ".join(bits)


def push_all(text):
    channels = []
    webhook = (os.environ.get("WECOM_WEBHOOK") or "").strip()
    app = (os.environ.get("WECOM_APP") or "").strip()
    if webhook:
        try:
            channels.append(push_webhook(webhook, text))
        except Exception as e:
            channels.append("webhook异常:%s" % type(e).__name__)
    if app:
        try:
            channels.append(push_app(app, text))
        except Exception as e:
            channels.append("app异常:%s" % type(e).__name__)
    if not channels:
        channels.append("未配置通知通道，仅打印：\n" + text)
    return channels


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "_report"
    title = "签到"
    if "--title" in sys.argv:
        i = sys.argv.index("--title")
        if i + 1 < len(sys.argv):
            title = sys.argv[i + 1]
    rows = load_rows(root)
    print(build_report(rows, title))
    policy = str(os.environ.get("RECOMMEND_NOTIFY") or "daily").strip().lower()
    if policy in ("0", "off", "false", "no", "none", "disabled"):
        print("每日积分播报：已关闭（RECOMMEND_NOTIFY=%s）" % policy)
        return 0
    text = build_text(rows, title, os.environ.get("REPORT_RUN_URL"))
    try:
        print("每日积分播报：%s" % " | ".join(push_all(text)))
    except Exception as e:
        # 播报失败绝不影响签到结果与心跳
        print("每日积分播报推送异常（已忽略）：%s" % type(e).__name__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
