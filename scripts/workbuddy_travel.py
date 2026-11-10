#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy「派猫猫旅行」全自动巡检（纯标准库，零第三方依赖）。

与每日签到的关系
----------------
本脚本 **只负责派猫猫旅行**，不碰签到接口，也不触发签到心跳，
因此不会掩盖「签到没跑」这类故障。账号凭据与每日签到完全共用：
复用 workbuddy_checkin.resolve_credential() 从环境变量取长效令牌并刷新，
无需新增任何 Secret，也无需浏览器 Cookie。

接口（已用真实账号实测，2026-11-10）
-----------------------------------
旅行接口固定走 https://www.workbuddy.cn，**不**使用签到域名 auth.domain；
只需 Authorization: Bearer <accessToken>，实测返回 code=0。

  GET  /activity/growth/buddy/travel/status    旅行状态（只读）
  GET  /activity/growth/buddy/travel/config    地点配置（只读）
  POST /activity/growth/buddy/travel/claim     领取已到达的奖励，body {}
  POST /activity/growth/buddy/travel/depart    派出，body {"location_id": 1~4}

状态机（以服务端 state 为唯一权威）
-----------------------------------
  arrived    → claim 领取积分（哪怕 daily_limit_reached=true 也必须领）
  traveling  → 什么都不做，打印到达倒计时，等下一轮巡检
  idle + 今日未派过 → depart 派出（地点随机，四个地点收益无差异）
  idle + 今日已派过 → 今日已完成

⚠️ daily_limit_reached 的语义是「今天已经派过一次」，**不是**「奖励已领」。
   绝不能用它短路掉 arrived，否则奖励会永远挂在门口领不到。

为什么一天要跑很多轮
--------------------
旅行时长随机 1~4 小时，且每天只能派一次。所以 GitHub Actions 用多个
错峰 cron 定时巡检：每轮「先领后派」，到达后最近一轮就会把积分领走，
领完当天的额度也就用满了。GitHub 定时偶尔延迟 / 漏跑，下一轮会自动补上。

三个账号错峰 + 随机
-------------------
--offset 是账号固定偏移、--jitter 是每轮额外随机抖动上限，
travel.yml 给 6717/7100/动态账号分别填 0/120/240 秒 + 随机 0~120 秒，
派出和领取窗口互不重叠，也不会落在整齐的固定时刻。
⚠️ 只在真的要写接口（派出 / 领取）时才等待；查状态、旅行中、今日已完成
   零等待。等待结束后**重新查一次状态**，避免等待期间状态已变还去重复写。

用法
----
  python workbuddy_travel.py                # 跑一轮状态机（自动领取 / 派出）
  python workbuddy_travel.py --status       # 只读，不写任何接口
  python workbuddy_travel.py --location 2   # 指定地点（1咖啡馆 2商场 3健身房 4古镇客栈）
  python workbuddy_travel.py --json         # 只输出一行脱敏 JSON，便于 CI 解析
  python workbuddy_travel.py --offset 120 --jitter 120   # 错峰 + 随机（CI 用）

退出码：0 = 本轮正常（含「旅行中」「今日已完成」）；1 = 真失败（会推微信）。
输出严格白名单：绝不打印令牌、手机号、userId 或完整接口响应。
"""

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import workbuddy_checkin as wb
except Exception as exc:  # noqa: BLE001
    print("[旅行] 无法加载同目录的 workbuddy_checkin.py：%s" % str(exc)[:160])
    raise SystemExit(1)

TRAVEL_BASE = "https://www.workbuddy.cn/activity/growth/buddy/travel"
WEB_BASE = "https://www.workbuddy.cn"
HTTP_TIMEOUT = 15
LOCATIONS = {1: "咖啡馆", 2: "商场店铺", 3: "健身房", 4: "古镇客栈"}
UA = "WorkBuddy-Checkin-Script/2.3"

# 服务端可能用不同措辞表示「这次不用做」，这些都不算失败（幂等）
BENIGN = ("已领", "已领取", "无待领", "没有可领", "already", "no unclaimed",
          "旅行中", "已在旅行", "traveling", "已派", "今日已", "次数已达",
          "limit", "上限", "未到达", "not arrived")


def _brief(body, limit=140):
    """只保留 code/msg，绝不带出 data（可能含用户资料）。"""
    if not isinstance(body, dict):
        return str(body)[:limit]
    return "code=%s msg=%s" % (body.get("code"),
                               str(body.get("msg") or body.get("message") or "")[:80])


def _headers(token):
    return {
        "Authorization": "Bearer " + token,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "User-Agent": UA,
        "x-client-platform": "web",
        "Origin": WEB_BASE,
        "Referer": WEB_BASE + "/profile/growth-center",
    }


def api(token, path, method="GET", payload=None):
    """返回 (http_status, body_dict)。网络异常返回 (-1, {"err": 中文})。"""
    data = None
    if method == "POST":
        data = json.dumps(payload if payload is not None else {}).encode("utf-8")
    req = urllib.request.Request(TRAVEL_BASE + path, data=data, method=method)
    for k, v in _headers(token).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
    except urllib.error.URLError as exc:
        return -1, {"err": "网络不通或 DNS 失败：%s" % str(exc.reason)[:110]}
    except Exception as exc:  # noqa: BLE001
        return -1, {"err": "请求异常：%s" % str(exc)[:110]}
    try:
        return status, json.loads(raw)
    except Exception:  # noqa: BLE001
        return status, {"err": "响应不是 JSON（前 80 字）：%s" % raw[:80]}


def _ok(http, body):
    return http == 200 and isinstance(body, dict) and body.get("code") == 0


def _benign(body):
    text = (str(body.get("msg") or "") + str(body.get("message") or "")).lower()
    return any(k.lower() in text for k in BENIGN)


def get_status(token):
    """返回 (data, error)。error 为中文原因。"""
    http, body = api(token, "/status")
    if http in (401, 403):
        return None, "登录态失效（HTTP %s）：WB_REFRESH_TOKEN 可能过期，请在本机重新导出长效令牌" % http
    if http == -1:
        return None, body.get("err", "网络错误")
    if not _ok(http, body):
        return None, "查询旅行状态失败：HTTP %s %s" % (http, _brief(body))
    data = body.get("data")
    if not isinstance(data, dict):
        return None, "查询旅行状态失败：响应缺少 data"
    return data, None


def do_claim(token, record_id):
    """领取奖励。返回 (ok, data, 中文说明)。"""
    # 官方客户端发空 body，服务端按权威 state 判断领哪一次；
    # 少数版本要求 record_id，失败时再退到带 id 的变体。
    attempts = [{}, {"record_id": record_id}, {"recordId": record_id}] if record_id else [{}]
    last = ""
    for pl in attempts:
        http, body = api(token, "/claim", "POST", pl)
        if _ok(http, body):
            d = body.get("data") or {}
            got = d.get("reward_credit")
            return True, d, ("领取成功 +%s 积分" % got) if got is not None else "领取成功"
        last = "HTTP %s %s" % (http, _brief(body))
        if _benign(body):
            return True, {}, "无需领取（%s）" % str(body.get("msg") or "服务端确认已领")[:40]
        if http in (401, 403):
            break
    return False, {}, "领取失败：%s" % last


def do_depart(token, loc_id):
    """派出。返回 (ok, data, 中文说明)。"""
    http, body = api(token, "/depart", "POST", {"location_id": loc_id})
    if _ok(http, body):
        d = body.get("data") or {}
        loc = (d.get("location") or {}).get("name") or LOCATIONS.get(loc_id, str(loc_id))
        arrive = d.get("arrive_at")
        extra = ""
        if isinstance(arrive, (int, float)) and arrive > 0:
            mins = max(0, int((arrive - int(time.time())) / 60))
            extra = "，约 %d 小时 %d 分后回来" % (mins // 60, mins % 60)
        return True, d, "已派出 → %s%s" % (loc, extra)
    if _benign(body):
        return True, {}, "无需派出（%s）" % str(body.get("msg") or "服务端确认已派")[:40]
    return False, {}, "派出失败：HTTP %s %s" % (http, _brief(body))


def _countdown(data):
    arrive = data.get("arrive_at") or 0
    now = data.get("server_now") or int(time.time())
    try:
        secs = max(0, int(arrive) - int(now))
    except (TypeError, ValueError):
        return ""
    if not secs:
        return ""
    return "，还有约 %d 小时 %d 分回来" % (secs // 3600, (secs % 3600) // 60)


def needs_write(data):
    """本轮是否真的要写接口（派出 / 领取）。只有这两种才需要错峰随机等待。"""
    state = str(data.get("state") or "").lower()
    return state == "arrived" or (
        state == "idle" and not bool(data.get("daily_limit_reached")))


def jitter_seconds(offset, jitter):
    """账号固定偏移 + 每轮随机抖动。三个账号偏移不同 → 不会同时派。"""
    return max(0, int(offset)) + (random.randint(0, int(jitter)) if jitter > 0 else 0)


def run_once(token, label, status_only=False, loc_id=None, offset=0, jitter=0):
    """跑一轮状态机，返回 (结果字典, 是否失败)。

    offset / jitter：三个账号用不同偏移拉开，再叠加每轮随机秒数，
    让「派出」「领取」不落在整齐的固定时刻。延迟后**重新查一次状态**，
    避免等待期间猫猫状态变了还去重复写接口。
    """
    out = {
        "account": label or "未命名账号",
        "ok": True,
        "action": "none",
        "state": "",
        "location": "",
        "reward": None,
        "message": "",
    }
    data, err = get_status(token)
    if err:
        out.update(ok=False, message=err)
        return out, True

    if not status_only and (offset or jitter) and needs_write(data):
        delay = jitter_seconds(offset, jitter)
        if delay > 0:
            print("[旅行] 错峰随机等待 %d 分 %d 秒（账号偏移 %d 秒 + 随机 0~%d 秒）"
                  % (delay // 60, delay % 60, offset, jitter))
            time.sleep(delay)
            data, err = get_status(token)
            if err:
                out.update(ok=False, message=err)
                return out, True

    state = str(data.get("state") or "").lower()
    limit = bool(data.get("daily_limit_reached"))
    loc = (data.get("location") or {}).get("name") or ""
    reward = data.get("reward_credit")
    record_id = data.get("record_id")
    out.update(state=state, location=loc, reward=reward)

    head = "状态=%s 地点=%s 今日已派=%s" % (state or "未知", loc or "-", "是" if limit else "否")
    print("[旅行] %s ｜ %s" % (out["account"], head))

    if status_only:
        out.update(action="status", message=head + "（只读，未派出/未领取）")
        print("[旅行] " + out["message"])
        return out, False

    if state == "arrived":
        # 权威状态优先：哪怕 limit=true 也要领，否则积分永远挂在门口
        ok, d, msg = do_claim(token, record_id)
        out.update(action="claim", ok=ok, message=msg)
        if ok and isinstance(d, dict) and d.get("reward_credit") is not None:
            out["reward"] = d.get("reward_credit")
        print("[旅行] " + msg)
        return out, not ok

    if state == "traveling":
        out.update(action="traveling", message="旅行中，等待到达后自动领取" + _countdown(data))
        print("[旅行] " + out["message"])
        return out, False

    if state == "idle" and not limit:
        pick = loc_id if loc_id in LOCATIONS else random.choice(list(LOCATIONS))
        ok, d, msg = do_depart(token, pick)
        out.update(action="depart", ok=ok, message=msg)
        if ok and isinstance(d, dict):
            out["state"] = str(d.get("state") or "traveling").lower()
            name = (d.get("location") or {}).get("name")
            if name:
                out["location"] = name
        print("[旅行] " + msg)
        return out, not ok

    if state == "idle" and limit:
        out.update(action="done", message="今日已完成（已派出且无待领奖励）")
        print("[旅行] " + out["message"])
        return out, False

    out.update(ok=False, action="unknown",
               message="遇到未知状态 %r，未做任何写操作，请查看官方页面" % state)
    print("[旅行] " + out["message"])
    return out, True


def notify_failure(label, message):
    """仅在真失败时推微信「东东推送」。成功 / 正常跳过一律静默。"""
    raw = (os.environ.get("WECOM_APP") or "").strip()
    if not raw:
        print("[旅行] 未配置 WECOM_APP，跳过失败告警")
        return
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) < 4 or not (parts[0] and parts[1] and parts[3]):
        print("[旅行] WECOM_APP 格式不对（应为 corpid,corpsecret,touser,agentid），跳过告警")
        return
    corpid, corpsecret, touser, agentid = parts[:4]
    try:
        errcode, errmsg = wb.notify_via_wecom_app(
            corpid, corpsecret, agentid, touser or "@all",
            "⚠️ WorkBuddy 派猫猫旅行失败",
            "账号：%s\n问题：%s\n时间：%s（北京时间）\n\n"
            "不影响每日签到；下一轮定时巡检会自动重试。\n"
            "若连续多轮失败，多半是长效令牌过期，请在本机重新导出 WB_REFRESH_TOKEN。"
            % (label, message, time.strftime("%Y-%m-%d %H:%M:%S",
                                             time.localtime(time.time() + 8 * 3600))))
        print("[旅行] 失败告警推送：errcode=%s %s" % (errcode, errmsg))
    except Exception as exc:  # noqa: BLE001
        print("[旅行] 失败告警推送异常（不影响主流程）：%s" % str(exc)[:110])


def main():
    ap = argparse.ArgumentParser(description="WorkBuddy 派猫猫旅行 · 全自动巡检")
    ap.add_argument("--status", "--check-only", dest="status_only", action="store_true",
                    help="只读查询，不派出、不领取")
    ap.add_argument("--location", type=int, choices=sorted(LOCATIONS),
                    help="指定旅行地点 1~4，缺省随机（四地收益无差异）")
    ap.add_argument("--json", action="store_true", help="只输出一行脱敏 JSON")
    ap.add_argument("--no-notify", action="store_true", help="失败时不推微信")
    ap.add_argument("--offset", type=int, default=0,
                    help="账号固定错峰偏移（秒），三个账号填不同值")
    ap.add_argument("--jitter", type=int, default=0,
                    help="每轮额外随机抖动上限（秒），0 表示准时执行")
    args = ap.parse_args()
    offset = max(0, args.offset)
    jitter = max(0, args.jitter)

    label = (os.environ.get("WB_ACCOUNT_LABEL") or "").strip() or "未命名账号"
    holder = {"detail": {}}
    try:
        token, domain, err = wb.resolve_credential(holder)
    except Exception as exc:  # noqa: BLE001
        token, domain, err = None, "", "读取凭据异常：%s" % str(exc)[:120]
    if err or not token:
        msg = err or "未取得接口令牌"
        print("[旅行] 凭据失败：%s" % msg)
        if not args.status_only and not args.no_notify:
            notify_failure(label, msg)
        if args.json:
            print(json.dumps({"account": label, "ok": False, "action": "auth",
                              "message": msg}, ensure_ascii=False))
        return 1

    try:
        out, failed = run_once(token, label, args.status_only, args.location,
                               offset=offset, jitter=jitter)
    except Exception as exc:  # noqa: BLE001
        out = {"account": label, "ok": False, "action": "exception",
               "state": "", "location": "", "reward": None,
               "message": "脚本异常：%s" % str(exc)[:160]}
        failed = True

    if args.json:
        print(json.dumps(out, ensure_ascii=False))
    elif not args.status_only:
        print("[旅行] 本轮结论：%s" % ("成功 · " + out["action"] if out["ok"] else "失败 · " + out["message"]))

    if failed and not args.status_only and not args.no_notify:
        notify_failure(label, out.get("message") or "未知错误")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
