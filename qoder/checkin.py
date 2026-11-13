#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Qoder CN 每日领取（GitHub Actions 云端版）。

只做一件事：拿 AiRoute 里已登录账号的凭据，去 Qoder 活动平台领每日 100 Credits。

设计对齐 WORKBUDDY签到更新：
  · 凭据从 AiRoute 的 channels-data 读，不重复登录、不改源文件
  · 令牌过期就用 refresh_token 续期，续期结果写回同一个文件（只改这一个账号）
  · 输出 result.json（白名单字段），供 summarize_result.py 写运行摘要
  · 退出码：0 = 领到 / 已领 / 无活动（都算正常）；1 = 失败

环境变量（Actions 里由 Secret 注入）：
  QODER_ACCESS_TOKEN     dt- 访问令牌（必填，二选一）
  QODER_REFRESH_TOKEN    drt- 续期令牌（可选，能自动续期）
  QODER_UID              账号 uid（可选，仅用于日志标识）
  QODER_ACCOUNT_LABEL    日志里的账号名（可选）
  QODER_OPENAPI          端点，默认 https://openapi.qoder.com.cn
  QODER_CHECK_ONLY       1 = 只查不领
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))
OPENAPI = os.environ.get('QODER_OPENAPI', 'https://openapi.qoder.com.cn').rstrip('/')
ACCESS_TOKEN = (os.environ.get('QODER_ACCESS_TOKEN') or '').strip()
REFRESH_TOKEN = (os.environ.get('QODER_REFRESH_TOKEN') or '').strip()
UID = (os.environ.get('QODER_UID') or '').strip()
LABEL = (os.environ.get('QODER_ACCOUNT_LABEL') or 'Qoder 账号').strip()
CHECK_ONLY = os.environ.get('QODER_CHECK_ONLY', '') == '1'
TIMEOUT = 25

# 退出码约定：
#   0  = 领到了 / 今天已领过 / 确实没有活动
#   10 = 窗口还没开（北京时间 10:00 前），这次不算失败，等补签那一班
#   1  = 真失败（凭据被拒、网络、上游异常）
EXIT_WINDOW_CLOSED = 10

# 桌面端身份：活动平台缺任一都会「200 + 空列表」。
# ⚠️ 绝对不能带任何 cosy-machine* 头 —— 派生值会让服务端把「每天领 100 Credits」
#    整条静默过滤掉（只剩 VIEW_DETAILS），这是实测结论，不是猜测。
DESKTOP_CLIENT_TYPE = '10'
DESKTOP_CLIENT_VERSION = '0.5.3'

CAMPAIGNS_PATH = '/sash/api/v1/me/campaigns'
CLAIM_PATH = '/sash/api/v1/me/campaigns/%s/claim'
REFRESH_PATH = '/api/v1/deviceToken/refresh'

RESULT = {
    'account': LABEL,
    'uid': UID,
    'outcome': 'failed',      # claimed / done / ineligible / failed
    'credit': 0,
    'message': '',
    'refreshed': False,
    'detail': [],
}


# ── 企业微信通知（与 WorkBuddy 签到同一套做法） ──────────────
# 只读 工具箱/配置.json 的 wecom 字段（4 段：corpid,corpsecret,touser,agentid）。
# 云端跑时该文件不存在，改为读 QODER_WECOM_APP 环境变量（由 workflow 从 Secret 注入）。

NOTIFY_FILE = os.environ.get('QODER_NOTIFY_FILE') or ''


def _wecom_config():
    raw = (os.environ.get('QODER_WECOM_APP') or '').strip()
    if not raw and NOTIFY_FILE:
        try:
            with open(NOTIFY_FILE, encoding='utf-8') as f:
                cfg = json.load(f)
            raw = str(cfg.get('wecom') or '').strip()
        except Exception:
            raw = ''
    if not raw:
        return None
    parts = [x.strip() for x in raw.split(',')]
    if len(parts) < 4 or not parts[0] or not parts[1] or not parts[3]:
        return None
    return {'corpid': parts[0], 'corpsecret': parts[1],
            'touser': parts[2] or '@all', 'agentid': parts[3]}


def _post_json(url, payload):
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='POST',
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status, r.read().decode('utf-8', 'replace')


def notify_wecom(title, content):
    """发企业微信自建应用消息。返回 'ok' / 失败原因。绝不回显 secret。"""
    cfg = _wecom_config()
    if not cfg:
        return 'skipped(未配置企业微信)'
    try:
        st, body = _post_json(
            'https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid=%s&corpsecret=%s'
            % (urllib.parse.quote(cfg['corpid']), urllib.parse.quote(cfg['corpsecret'])),
            {})
        token = ''
        try:
            token = (json.loads(body) or {}).get('access_token') or ''
        except Exception:
            token = ''
        if not token:
            return 'gettoken失败(HTTP %s)' % st
        st, body = _post_json(
            'https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token=%s' % token,
            {'touser': cfg['touser'], 'msgtype': 'markdown', 'agentid': cfg['agentid'],
             'markdown': {'content': content}})
        try:
            obj = json.loads(body) or {}
        except Exception:
            obj = {}
        code = obj.get('errcode')
        if code == 0:
            return 'ok'
        return 'send失败(errcode=%s %s)' % (code, str(obj.get('errmsg') or '')[:60])
    except Exception as e:
        return '异常(%s)' % type(e).__name__


def _origin():
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        return 'GitHub 云端定时任务'
    return '本机运行'


def notify_failure(result):
    """签到失败时推企业微信。与 WorkBuddy 一致：失败必响。"""
    title = "⚠️ Qoder 签到失败"
    lines = [
        "### ⚠️ Qoder 签到失败",
        "",
        "> **来源**：Qoder 签到助手 · " + _origin(),
        ">",
        "> **账号**：" + str(result.get("account") or "-"),
        ">",
        "> **时间**：" + now(),
        ">",
        "> **原因**：" + str(result.get("message") or "未知原因"),
        ">",
        "> **处理建议**：若为令牌失效，请在 AiRoute「渠道账号」重新登录该 Qoder 账号，",
        "> 再双击本机「QODER签到更新 / 同步全部账号.bat」",
    ]
    content = chr(10).join(lines)
    outcome = notify_wecom(title, content)
    log("  企业微信推送：" + outcome)
    result.setdefault("detail", []).append("企业微信：" + outcome)


def now():
    return datetime.now(BEIJING).strftime('%Y-%m-%d %H:%M:%S')


def log(msg):
    try:
        print('[%s] %s' % (now(), msg), flush=True)
    except UnicodeEncodeError:
        print(('[%s] %s' % (now(), msg)).encode('utf-8', 'replace').decode('utf-8', 'replace'), flush=True)


def note(msg):
    RESULT['detail'].append(msg)
    log(msg)


def request(method, path, token=None, body=None, headers=None):
    """返回 (status, parsed_or_None, text)。网络异常时 status=None。"""
    url = path if path.startswith('http') else OPENAPI + path
    hdrs = {'Accept': 'application/json', 'User-Agent': 'Qoder'}
    if token:
        hdrs['Authorization'] = 'Bearer ' + token
    data = None
    if body is not None:
        data = json.dumps(body).encode('utf-8')
        hdrs['Content-Type'] = 'application/json'
    if headers:
        hdrs.update(headers)

    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            text = resp.read().decode('utf-8', 'replace')
            status = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            text = e.read().decode('utf-8', 'replace')
        except Exception:
            text = ''
    except Exception as e:
        return None, None, '%s: %s' % (type(e).__name__, e)

    try:
        return status, (json.loads(text) if text else {}), text
    except Exception:
        return status, None, text


def desktop_headers(token):
    return {
        'cosy-clienttype': DESKTOP_CLIENT_TYPE,
        'cosy-version': DESKTOP_CLIENT_VERSION,
    } if token else None


def refresh(token_ref):
    """用 drt- 换新的 dt-。返回 (new_access, new_refresh, error)。"""
    status, data, text = request(
        'POST', REFRESH_PATH,
        body={'refresh_token': token_ref, 'machine_id': os.environ.get('QODER_MACHINE_ID', '')},
        headers={'User-Agent': 'Qoder/1.0.0'},
    )
    if status in (401, 403):
        return None, None, '续期被拒绝（HTTP %s），需要重新登录 AiRoute 里的 Qoder 账号' % status
    if status != 200 or not isinstance(data, dict):
        return None, None, '续期失败（HTTP %s）%s' % (status, text[:150])
    inner = data.get('data') if isinstance(data.get('data'), dict) else data
    access = inner.get('access_token') or inner.get('security_oauth_token')
    nref = inner.get('refresh_token') or token_ref
    if not access:
        return None, None, '续期响应里没有 access_token'
    return access, nref, None


def load_campaigns(token):
    status, data, text = request('GET', CAMPAIGNS_PATH, token=token, headers=desktop_headers(token))
    if status in (401, 403):
        return None, '鉴权失败（HTTP %s）' % status, 'auth'
    if status in (404, 405, 410):
        return None, '活动平台不可用（HTTP %s）' % status, 'unsupported'
    if status != 200 or not isinstance(data, dict):
        return None, '活动列表查询失败（HTTP %s）%s' % (status, text[:150]), 'error'
    items = data.get('campaigns')
    return (items if isinstance(items, list) else []), None, None


def pick(items):
    """挑出能领的那条。先判已领，再判 actionType——官方领完会把 actionType 降级成 VIEW_DETAILS。"""
    claimable, claimed = [], 0
    for item in items:
        if not isinstance(item, dict):
            continue
        cid = item.get('campaignId') or item.get('campaign_id')
        if not cid:
            continue
        status = str(item.get('claimStatus') or item.get('claim_status') or '').upper()
        if status == 'CLAIMED':
            claimed += 1
            continue
        action = str(item.get('actionType') or item.get('action_type') or '').upper()
        benefit = item.get('benefit') if isinstance(item.get('benefit'), dict) else {}
        kind = str(benefit.get('kind') or '').upper()
        if action and action != 'CLAIM_BENEFIT':
            continue
        if kind and kind != 'CREDITS':
            continue
        if status != 'CLAIMABLE':
            continue
        claimable.append((cid, float(benefit.get('amount') or 0), item))
    return claimable, claimed


def claim(token, cid):
    status, data, text = request(
        'POST', CLAIM_PATH % cid, token=token, body={},
        headers=desktop_headers(token),
    )
    body = data if isinstance(data, dict) else {}
    st = str(body.get('status') or '').upper()
    failure = str(body.get('failureCode') or body.get('errorCode') or '').upper()
    if status == 409 or failure in ('ALREADY_CLAIMED', 'REPLAYED') or body.get('replayed') is True:
        return 'done', 0, '今天已领过'
    if st == 'BLOCKED' or failure == 'SAME_PERSON_ALREADY_CLAIMED':
        return 'blocked', 0, '同一人今天已领过（同机多号只算一次）'
    if failure == 'REDEMPTION_CODE_OUT_OF_STOCK':
        return 'pending', 0, '库存不足，稍后再试'
    if failure == 'CAMPAIGN_NOT_ACTIVE':
        return 'pending', 0, '活动未开始或已结束'
    if failure in ('RISK_BLOCKED', 'RISK_DEPENDENCY_UNAVAILABLE'):
        return 'blocked', 0, '被上游风控拦下，稍后再试'
    if st in ('CLAIMED', 'GRANTED', 'SUCCESS') or body.get('success') is True:
        credit = float(body.get('benefit', {}).get('amount') or body.get('amount') or 0) \
            if isinstance(body.get('benefit'), dict) else float(body.get('amount') or 0)
        return 'claimed', credit, ('领取 %.0f 积分' % credit) if credit else '领取成功'
    return 'failed', 0, str(body.get('message') or body.get('error') or ('HTTP %s' % status))[:120]


def save(result):
    path = os.environ.get('QODER_RESULT_FILE') or 'result.json'
    # 失败才推送：成功/已领不打扰（与 WorkBuddy 的 SUCCESS_NOTIFY 默认关一致）
    if result.get('outcome') == 'failed':
        try:
            notify_failure(result)
        except Exception as e:
            log('[!] 推送异常：%s' % e)
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        log('结果已写入 %s' % path)
    except Exception as e:
        log('[!] 写 result.json 失败：%s' % e)


def main():
    RESULT['account'] = LABEL
    RESULT['uid'] = UID

    if not ACCESS_TOKEN and not REFRESH_TOKEN:
        RESULT['message'] = '未配置凭据（QODER_ACCESS_TOKEN / QODER_REFRESH_TOKEN 都为空）'
        log('[x] ' + RESULT['message'])
        return save(RESULT) or 1
    token = ACCESS_TOKEN

    # 没有 dt- 就直接续期
    if not token:
        log('没有访问令牌，先用续期令牌换一个…')
        token, _newref, err = refresh(REFRESH_TOKEN)
        if not token:
            RESULT['message'] = err
            log('[x] ' + err)
            return save(RESULT) or 1
        RESULT['refreshed'] = True
        note('已用续期令牌换取访问令牌')

    log('账号：%s  uid：%s  端点：%s' % (LABEL, UID or '(未填)', OPENAPI))

    items, err, kind = load_campaigns(token)
    # 访问令牌可能只是过期了，续期一次再试
    if err and kind == 'auth' and REFRESH_TOKEN:
        log('访问令牌被拒，尝试续期…')
        newtok, _nr, rerr = refresh(REFRESH_TOKEN)
        if newtok:
            token = newtok
            RESULT['refreshed'] = True
            note('访问令牌已续期，重试活动列表')
            items, err, kind = load_campaigns(token)
        else:
            err = '%s；续期也失败：%s' % (err, rerr)

    if err:
        RESULT['outcome'] = 'ineligible' if kind == 'unsupported' else 'failed'
        RESULT['message'] = err
        log('[%s] %s' % ('=' if kind == 'unsupported' else 'x', err))
        return save(RESULT) or (0 if kind == 'unsupported' else 1)

    if not items:
        RESULT['outcome'] = 'ineligible'
        now_bj = datetime.now(BEIJING)
        if now_bj.hour < 10:
            # 窗口未开：退出码 10，让 workflow 区分「还没到点」和「真失败」
            RESULT['message'] = ('当前北京时间 %s，今天 10:00 的新一轮还没开放'
                                 % now_bj.strftime('%H:%M'))
            RESULT['window_closed'] = True
            log('[=] ' + RESULT['message'])
            return save(RESULT) or EXIT_WINDOW_CLOSED
        RESULT['message'] = '今天没有可领取的活动'
        log('[=] ' + RESULT['message'])
        return save(RESULT) or 0

    claimable, claimed_count = pick(items)
    note('活动共 %d 条，其中可领 %d 条、已领 %d 条' % (len(items), len(claimable), claimed_count))

    if not claimable:
        # Qoder 的领取窗口是「今天 10:00 → 明天 10:00」。在北京时间 10:00 之前跑，
        # 看到的仍是「昨天那轮」的状态：昨天已领 → 这里全变成 CLAIMED。
        # 6 点那一班就是这种情况，用退出码 10 告诉 workflow「还没到点，等补签」，
        # 不能报成「今天已领过」，否则补签那一班会误以为已经完成。
        now_bj = datetime.now(BEIJING)
        if now_bj.hour < 10:
            RESULT['outcome'] = 'ineligible'
            RESULT['window_closed'] = True
            RESULT['message'] = (
                '当前北京时间 %s，今天 10:00 的新一轮还没开放（看到的是昨天那轮）。'
                '等 10:00 后的补签那一班处理。' % now_bj.strftime('%H:%M')
            )
            log('[=] ' + RESULT['message'])
            return save(RESULT) or EXIT_WINDOW_CLOSED
        RESULT['outcome'] = 'done' if claimed_count else 'ineligible'
        RESULT['message'] = '今天已领过' if claimed_count else '今天没有可领取的积分活动'
        log('[=] ' + RESULT['message'])
        return save(RESULT) or 0

    if CHECK_ONLY:
        RESULT['outcome'] = 'ineligible'
        RESULT['message'] = '仅查询模式，不执行领取（有 %d 条可领）' % len(claimable)
        log('[=] ' + RESULT['message'])
        return save(RESULT) or 0

    total, got = 0.0, 0
    for cid, amount, _item in claimable:
        verdict, credit, msg = claim(token, cid)
        note('%s：%s' % (cid[:8], msg))
        if verdict == 'claimed':
            got += 1
            total += credit or amount
        elif verdict == 'done':
            RESULT['outcome'] = 'done'
            RESULT['message'] = '今天已领过'
            return save(RESULT) or 0
        elif verdict == 'blocked':
            RESULT['outcome'] = 'done'
            RESULT['message'] = msg
            return save(RESULT) or 0
        elif verdict == 'pending':
            continue
        # 相邻两次领取之间留 1 秒：上游对连续 claim 有频率判据
        time.sleep(1)

    if got:
        RESULT['outcome'] = 'claimed'
        RESULT['credit'] = total
        RESULT['message'] = '领取 %d 项，共 %.0f 积分' % (got, total)
        log('[OK] ' + RESULT['message'])
        return save(RESULT) or 0

    RESULT['outcome'] = 'ineligible'
    RESULT['message'] = '没有领到（可领活动均未成功，见上方详细结果）'
    log('[=] ' + RESULT['message'])
    return save(RESULT) or 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        RESULT['message'] = '未预期错误：%s' % e
        log('[x] ' + RESULT['message'])
        save(RESULT)
        sys.exit(1)
