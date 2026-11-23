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
排版：企微 markdown，积分 / 到期日 / 剩余天数用彩色字（企微只支持 info 绿 /
      warning 橙红 / comment 灰）；通道不支持 markdown 时自动降级为纯文本。
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

# ── 企业微信 markdown 着色 ──────────────────────────────
# 企微 markdown 只认 info（绿）/ warning（橙红）/ comment（灰）三种颜色，
# 写别的色值会被当黑字，所以只暴露这三个语义名。
C_INFO, C_WARN, C_NOTE = "info", "warning", "comment"
MD_BYTE_LIMIT = 3800          # 企微 markdown 上限 4096 字节，留余量给截断提示


def c(text, color=C_INFO):
    return '<font color="%s">%s</font>' % (color, text)


def strip_color(text):
    """去掉颜色标签与加粗标记，用于纯文本降级。"""
    return re.sub(r"\*\*", "", re.sub(r"</?font[^>]*>", "", text))


def fit_markdown(text):
    """超长按行截断：整条超限企微会直接拒收。"""
    if len(text.encode("utf-8")) <= MD_BYTE_LIMIT:
        return text
    out, size = [], 0
    for line in text.split("\n"):
        n = len(line.encode("utf-8")) + 1
        if size + n > MD_BYTE_LIMIT - 80:
            break
        out.append(line)
        size += n
    out.append(c("…内容过长已截断，完整数据见运行详情", C_NOTE))
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
    """到期说明（到期日 / 剩余天数 / 这批分数上色，临期≤1 天转橙红）。"""
    if not r["has_data"]:
        if r["error"]:
            return c("积分数据未取到（%s）" % r["error"][:50], C_WARN)
        return c("积分数据未取到（本次签到可能失败，见下方告警）", C_WARN)
    if not r["expire_date"]:
        return c("当前积分未标注到期日", C_NOTE)
    urgent = isinstance(r["days_left"], (int, float)) and r["days_left"] <= 1
    color = C_WARN if urgent else C_INFO
    head = "%s %s（剩 %s 天，这批 %s）" % (
        c(r["expire_date"], color), r["label"],
        c("%s" % (r["days_left"] if r["days_left"] is not None else "?"), color),
        c("%g 分" % r["remaining"], color))
    if urgent:
        return head + "；" + c("⚠️ 今明两天就到期，今天就用掉", C_WARN)
    if r["use_by"]:
        return head + "；建议 %s 前用掉，提前一天" % c(r["use_by"], C_INFO)
    return head


def build_text(rows, title, run_url=None):
    """企微 markdown：积分、到期日、剩余天数用彩色字，其余说明文字保持黑白。"""
    today = datetime.now(BEIJING).strftime("%Y-%m-%d")
    lines = ["**%s · 每日积分播报** %s" % (title, today),
             c("每个号的当前积分，以及积分还有多久开始过期、该提前哪天用掉：", C_NOTE), ""]
    if not rows:
        lines.append(c("本次没有收到任何账号的积分数据，请查看运行详情。", C_WARN))
        if run_url:
            lines.append("运行详情：%s" % run_url)
        return fit_markdown("\n".join(lines))
    # 缺口可见：少一个号时，数量本身就能提醒（小浣熊播报同一处理）
    bad = [r for r in rows if not r["has_data"]]
    lines.append("本次收到 %d 个账号的积分数据%s" % (
        len(rows), c("，其中 %d 个未取到（见上方）" % len(bad), C_WARN) if bad else ""))
    lines.append("")
    for i, r in enumerate(rows, 1):
        lines.append("**%d. %s**" % (i, r["account"]))
        lines.append("当前积分：%s" % (
            c("%g 分" % r["credits"], C_INFO) if r["has_data"] else c("未取到", C_WARN)))
        lines.append(expiry_sentence(r))
        if r["status"] in ("error", "failed") and r["message"]:
            lines.append(c("⚠️ 签到状态：%s" % r["message"][:60], C_WARN))
        lines.append("")
    soon = [r for r in rows if r["has_data"] and r["days_left"] is not None and r["days_left"] <= 7]
    if soon:
        lines.append("建议：今天优先用 %s 跑代码，临期的先用，别拖到过期当天。"
                     % c(soon[0]["account"], C_INFO))
    else:
        lines.append(c("近 7 天没有临期积分，不用特意切号。", C_NOTE))
    lines.append(c("注：数据为云端只读余额查询的聚合结果，与 AiRoute「积分先到期先用」口径一致。",
                   C_NOTE))
    if run_url:
        lines.append("运行详情：%s" % run_url)
    return fit_markdown("\n".join(lines))


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


def push_webhook(value, content):
    url = value if value.startswith("http") else (QYAPI + "/webhook/send?key=" + value)
    r = post_json(url, {"msgtype": "markdown", "markdown": {"content": content}})
    if r.get("errcode") in (0, None):
        return "webhook:%s" % (r.get("errmsg") or "ok")
    # 通道不支持 / 拒收 markdown：降级纯文本，保证消息一定送到
    first = r.get("errmsg") or r.get("errcode")
    r = post_json(url, {"msgtype": "text", "text": {"content": strip_color(content)}})
    return "webhook:markdown失败(%s)降级text:%s" % (
        first, r.get("errmsg") or r.get("errcode"))


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
    send_url = "%s/message/send?access_token=%s" % (QYAPI, t["access_token"])
    r = post_json(send_url, {
        "touser": touser, "msgtype": "markdown", "agentid": agentid,
        "markdown": {"content": content}, "safe": 0,
    })
    downgraded = ""
    if r.get("errcode") not in (0, None):
        downgraded = "降级text(%s) " % (r.get("errmsg") or r.get("errcode"))
        r = post_json(send_url, {
            "touser": touser, "msgtype": "text", "agentid": agentid,
            "text": {"content": strip_color(content)}, "safe": 0,
        })
    bits = ["%serrcode=%s" % (downgraded, r.get("errcode")),
            "errmsg=%s" % (r.get("errmsg") or "-")]
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
