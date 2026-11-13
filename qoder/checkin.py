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
        RESULT['message'] = '今天没有可领取的活动（活动每天 10:00 刷新）'
        log('[=] ' + RESULT['message'])
        return save(RESULT) or 0

    claimable, claimed_count = pick(items)
    note('活动共 %d 条，其中可领 %d 条、已领 %d 条' % (len(items), len(claimable), claimed_count))

    if not claimable:
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
