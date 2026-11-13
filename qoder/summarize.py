#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 qoder_checkin.py 的 result.json 汇总成 Actions 运行摘要。

只输出白名单字段 —— 摘要会长期留在 Actions 页面，不能把接口原文整段写进去。
"""
from __future__ import annotations

import json
import sys

OUTCOME = {
    'claimed': '✅ 领取成功',
    'done': '☑️ 今天已领过',
    'ineligible': '⏸ 暂无活动 / 不适用',
    'failed': '❌ 失败',
}


def load(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def main():
    data = load(sys.argv[1] if len(sys.argv) > 1 else 'result.json')
    if not data:
        print('\n> 没有读到 result.json（脚本可能在写结果前就退出了）')
        return 0

    w = sys.stdout.write
    w('\n| 项目 | 结果 |\n|---|---|\n')
    w('| 账号 | %s |\n' % (data.get('account') or '-'))
    if data.get('uid'):
        w('| UID | `%s` |\n' % data['uid'])
    w('| 结论 | %s |\n' % OUTCOME.get(data.get('outcome'), data.get('outcome') or '-'))
    if data.get('credit'):
        w('| 本次积分 | %.0f |\n' % float(data['credit']))
    w('| 说明 | %s |\n' % (data.get('message') or '-'))
    if data.get('refreshed'):
        w('| 令牌 | 本次已自动续期 |\n')

    detail = data.get('detail') or []
    if detail:
        w('\n<details><summary>详细过程（%d 条）</summary>\n\n' % len(detail))
        for line in detail:
            w('- %s\n' % line)
        w('\n</details>\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
