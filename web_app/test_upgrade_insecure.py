"""[v6.7.734] HTTPS 화면에만 upgrade-insecure-requests 를 보내는지 - 평문 :5008 에 보내면 화면이 깨진다.

    IMT_DISABLE_BOOTSTRAP=1 DB_DIR=<임시> python3 test_upgrade_insecure.py
"""
import os
import sys
import tempfile

os.environ.setdefault('IMT_DISABLE_BOOTSTRAP', '1')
os.environ.setdefault('DB_DIR', tempfile.mkdtemp(prefix='imt_uir_'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod   # noqa: E402

FAILS = []


def check(label, ok, got=None):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + ('' if ok else f'  (got {got!r})'))
    if not ok:
        FAILS.append(label)


def csp(base_url, headers=None):
    c = appmod.app.test_client()
    r = c.get('/api/health', base_url=base_url, headers=headers or {})
    return r.headers.get('Content-Security-Policy')


def main():
    want = 'upgrade-insecure-requests'
    check('Funnel 이름(포트 없음) -> 보냄', csp('https://ime.example.com') == want, csp('https://ime.example.com'))
    check('X-Forwarded-Proto: https -> 보냄', csp('http://10.0.0.20:5001', {'X-Forwarded-Proto': 'https'}) == want)
    check('평문 tailnet :5008 -> 안 보냄', csp('http://198.51.100.20:5008') is None, csp('http://198.51.100.20:5008'))
    check('평문 LAN :5008 -> 안 보냄', csp('http://10.0.0.20:5008') is None)
    check('컨테이너 헬스체크 localhost:5001 -> 안 보냄', csp('http://localhost:5001') is None)
    check('포트 없는 IP 리터럴 -> 안 보냄', csp('http://203.0.113.7') is None)
    print(f"\n{'[FAIL] ' + str(len(FAILS)) + '건' if FAILS else '[PASS] 전부 통과'}")
    return 1 if FAILS else 0


if __name__ == '__main__':
    raise SystemExit(main())
