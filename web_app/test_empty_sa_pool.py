# -*- coding: utf-8 -*-
"""[v6.7.747] 서비스 계정 풀이 비면 설정 시트 로드가 0 으로 나누지 않고, 멈추지도 않고 바로 돌아오는가.

왜: 키 파일이 없으면 `utils/service_accounts.py` 가 [FAIL] 을 남기고 빈 목록을 준다. 그때
`CrawlerManager._load_config_from_sheet_inner` 의 except 가 `% len(SERVICE_ACCOUNTS)` 에서 ZeroDivisionError 로 터져
원인을 가렸다. 나눗셈만 막으면 '성공할 때까지 무한 재시도' 라 영원히 멈춘다 - 루프 전에 돌아와야 한다.
발견: 포폴 빌드(키 파일을 넣지 않은 사본)에서 `test_bootstrap_jobs.py` 가 이 경로로 죽었다(2026-10-26).
되돌려 넣기: crawler.py 의 가드를 지우면 [1] 이 FAIL 한다(ZeroDivisionError).

실행: cd web_app && IMT_DISABLE_BOOTSTRAP=1 python test_empty_sa_pool.py
"""
import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
for p in (HERE, os.path.join(HERE, 'crawlers'), os.path.join(HERE, 'utils')):
    sys.path.insert(0, p)
os.environ.setdefault('IMT_DISABLE_BOOTSTRAP', '1')

import crawlers.crawler as C   # noqa: E402

fails = []


def check(name, cond, detail=''):
    print(('  PASS  ' if cond else '  FAIL  ') + name + ('' if cond else f'  -> {detail}'))
    if not cond:
        fails.append(name)


print('[1] 서비스 계정 0개')
C.SERVICE_ACCOUNTS = []   # crawler 가 import 한 이름을 비운다(키 파일이 없을 때와 같은 값)
m = C.CrawlerManager.__new__(C.CrawlerManager)   # __init__ 은 시트를 실제로 부른다 - 이 경로만 태운다
m.current_account_index = 0
logs = []
m._log = lambda msg, level='info', tag='': logs.append((level, tag, msg))
box = {}


def _run():
    try:
        box['ret'] = m._load_config_from_sheet_inner()
    except Exception as e:   # 결함이면 여기로 온다 - 종류를 기록한다
        box['err'] = type(e).__name__


t = threading.Thread(target=_run, daemon=True)
t.start()
t.join(10)   # 무한 재시도면 10초 안에 안 끝난다
check('10초 안에 돌아온다(무한 재시도 아님)', not t.is_alive())
check('예외 없이 돌아온다', 'err' not in box, box.get('err'))
check('변경 없음 = 빈 목록', box.get('ret') == [], box.get('ret'))
check('원인을 error 로 남긴다', any(lv == 'error' and '서비스 계정 0개' in msg for lv, _, msg in logs), logs)

print('\n' + ('[PASS] 전 항목 통과' if not fails else f'[FAIL] {len(fails)}건'))
sys.exit(1 if fails else 0)
