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
排版：应用消息固定发 text（微信端只渲染 text / image，markdown 会显示
      「暂不支持此消息类型」）；群机器人 webhook 保留 markdown 彩色。
      两个通道渲染同一份内容，颜色只在企业微信群里有意义。
"""

import glob
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

QYAPI = "https://qyapi.weixin.qq.com/cgi-bin"
BEIJING = timezone(timedelta(hours=8))

TEXT_BYTE_LIMIT = 1900        # 企微 text 上限 2048 字节，留余量给截断提示


def fit_text(text, limit=TEXT_BYTE_LIMIT):
    """超长按行截断：整条超限企微会直接拒收，比截断更糟。"""
    if len(text.encode("utf-8")) <= limit:
        return text
    out, size = [], 0
    for line in text.split("\n"):
        n = len(line.encode("utf-8")) + 1
        if size + n > limit - 80:
            break
        out.append(line)
        size += n
    out.append("…内容过长已截断，完整数据见运行详情")
    return "\n".join(out)


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
        # 优先用脚本写好的账号标签；没有则退回 artifact 目录名，并去掉
        # 「wb-expiry- / qoder-expiry- / expiry-」前缀，避免显示成 wb-expiry-9653
        dir_name = os.path.basename(os.path.dirname(path))
        fallback = re.sub(r"^(?:wb|qoder|raccoon)?-?expiry-", "", dir_name)
        account = str(data.get("account") or fallback or dir_name or "未命名账号")
        status = str(data.get("status") or data.get("outcome") or "unknown")
        message = str(data.get("message") or data.get("msg") or "")
        err = str(exp.get("error") or "")
        credits = num(exp.get("credits")) if not err else 0.0
        days = exp.get("days_left")
        batches = []
        for b in (exp.get("batches") or []):
            if not isinstance(b, dict):
                continue
            try:
                exp_ms = int(float(b.get("expireAt") or 0))
            except (TypeError, ValueError):
                exp_ms = 0
            leftover = num(b.get("remaining"))
            if exp_ms > 0 and leftover > 0:
                batches.append({"remaining": leftover, "expireAt": exp_ms})
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
            "batches": batches,
            "error": err,
        })
    # 与 AiRoute「积分先到期先用」一致：到期日近的在前；同日则临期待用积分
    # 多的在前（最该先消耗）；再按总积分、账号名稳定排序。
    rows.sort(key=lambda r: (r["days_left"] if r["days_left"] is not None else 999,
                             -r["remaining"], -r["credits"], r["account"]))
    return rows


def account_label(name):
    """播报统一写「尾号 xxxx」；已经是尾号就归一化，不重复加。"""
    text = str(name or "").strip()
    if text.startswith("尾号"):
        digits = re.sub(r"\D", "", text)
        return "尾号" + digits[-4:] if len(digits) >= 4 else text
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 4:
        return "尾号" + digits[-4:]
    return text or "未命名账号"


def three_day_amount(r):
    """从最早到期日开始的三天（含当天）要过期多少分。

    有批次明细就按日期求和；老结果没有 batches，退回「最早那一批」的剩余。
    """
    if not r["batches"] or not r["expire_date"]:
        return r["remaining"]
    start = date.fromisoformat(r["expire_date"])
    total = 0.0
    for b in r["batches"]:
        try:
            day = datetime.fromtimestamp(b["expireAt"] / 1000.0, BEIJING).date()
        except (OverflowError, OSError, ValueError):
            continue
        if start <= day <= start + timedelta(days=2):
            total += b["remaining"]
    return round(total, 2) if total > 0 else r["remaining"]


def expiry_lines(r):
    """一个账号的到期说明：几天后开始过期、三天内合计多少、建议哪天前用掉。"""
    if not r["has_data"]:
        return ["⚪ 积分数据未取到"]
    if not r["expire_date"]:
        return ["⚪ 当前积分未标注到期日"]
    days = r["days_left"]
    if isinstance(days, (int, float)):
        when = "今天" if days <= 0 else "明天" if days == 1 else "%s 天后" % days
    else:
        when = "?"
    three = three_day_amount(r)
    lines = ["📅 %s，%s 开始，三天内过期 %g 分" % (when, r["expire_date"], three)]
    if isinstance(days, (int, float)) and days <= 0:
        lines.append("⏰ 今天就到期，尽快用掉")
    elif isinstance(days, (int, float)) and days == 1:
        lines.append("⏰ 明天就到期，今天用掉")
    elif r["use_by"]:
        lines.append("⏰ 建议 %s 前用掉（提前一天）" % r["use_by"])
    return lines


def build_text(rows, title, run_url=None):
    """手机播报：纯文本 + emoji 色标（微信端 markdown/图片都不可靠，只有 emoji 有颜色）。"""
    today = datetime.now(BEIJING).strftime("%Y-%m-%d")
    lines = ["📌 %s · 每日积分播报" % title, "📅 %s" % today]
    if not rows:
        lines.append("⚠️ 本次没有收到任何账号的积分数据，请查看运行详情")
        if run_url:
            lines.append("🔗 运行详情：%s" % run_url)
        return fit_text("\n".join(lines))
    # 缺口可见：少一个号时，数量本身就能提醒（小浣熊播报同一处理）
    bad = [r for r in rows if not r["has_data"]]
    lines.append("📣 收到 %d 个账号的积分数据%s" % (
        len(rows), "，%d 个未取到" % len(bad) if bad else "，全部正常"))
    for r in rows:
        lines.append("")
        lines.append(("🟩 " if r["has_data"] else "🟥 ") + account_label(r["account"]))
        if r["has_data"]:
            lines.append("💰 当前积分 %g 分" % r["credits"])
            for part in expiry_lines(r):
                lines.append(part)
        else:
            reason = (r["message"] or r["error"] or "未知原因")[:60]
            lines.append("❗ 积分未取到：%s" % reason)
        if r["has_data"] and r["status"] in ("error", "failed") and r["message"]:
            lines.append("❗ 签到异常：%s" % r["message"][:60])
    soon = [r for r in rows if r["has_data"] and r["days_left"] is not None and r["days_left"] <= 7]
    lines.append("")
    if soon:
        lines.append("📣 今天优先用 %s 跑代码，临期的先用，别拖到过期当天"
                     % account_label(soon[0]["account"]))
    else:
        lines.append("⚪ 近 7 天没有临期积分，不用特意切号")
    lines.append("⚪ 注：数据取云端只读余额查询，与 AiRoute「积分先到期先用」口径一致")
    if run_url:
        lines.append("🔗 运行详情：%s" % run_url)
    return fit_text("\n".join(lines))


def build_report(rows, title):
    """Markdown 榜单，写进 Actions Summary。"""
    lines = ["#### %s · 每日积分播报（按最早到期日排序）" % title, "",
             "| 排名 | 账号 | 当前积分 | 到期日 | 剩几天 | 三天内过期 | 建议用掉日 | 签到状态 |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for i, r in enumerate(rows, 1):
        lines.append("| %d | %s | %s | %s | %s | %s | %s | %s |" % (
            i, account_label(r["account"]),
            "**%g**" % r["credits"] if r["has_data"] else "未取到",
            r["expire_date"] or "—",
            r["days_left"] if r["days_left"] is not None else "—",
            "%g 分" % three_day_amount(r) if r["has_data"] else "—",
            r["use_by"] or "—",
            "失败" if r["status"] in ("error", "failed") else "成功"))
    lines.append("")
    lines.append("> 当前积分取云端只读余额接口；到期日取「还没用掉且最早到期」的那一批，"
                 "三天内过期 = 该日起 3 天内到期的批次合计。")
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


def push_webhook(value, content):
    url = value if value.startswith("http") else (QYAPI + "/webhook/send?key=" + value)
    r = post_json(url, {"msgtype": "text", "text": {"content": content}})
    return "webhook:%s" % (r.get("errmsg") or r.get("errcode") or "ok")


def push_app(value, content):
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
        "text": {"content": content}, "safe": 0,
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
