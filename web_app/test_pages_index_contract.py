# -*- coding: utf-8 -*-
"""index() 동작 계약 스냅샷 검사 - routes/pages.py `index()` 리팩토링 안전망.

왜 있나: `index()`(약 143줄)는 세션 role · 쿠키 · 쿼리(?login) · 헤더 · access_lock · 방문로그
dedup · 모바일 분기 · 응답 헤더/쿠키를 한 함수에서 처리한다. 이걸 작은 함수로 분해할 때
'동작 불변'을 증명하려면 import 통과·크래시 부재로는 부족하다(PATTERN-63/66). 그래서 코드가
실제로 분기하는 조건(anon/manager/admin · ?login 4종 · access_lock · vid 쿠키 유무 · 60초 dedup ·
모바일 UA · 진단 지문)마다 test_client 로 `/` 를 태워, **응답(상태·주요 헤더·Set-Cookie·렌더 HTML)과
관측 가능한 부작용(socketio emit · visitor_log 큐 적재 · 방문로그 증가 · 세션 변경)** 을 스냅샷과
비교한다. 분기 하나만 바뀌어도 FAIL 한다(뮤테이션으로 검증됨).

**라이브 컨테이너에서 실행 금지** - import app 이 운영 DB 에 sqlite 초기화를 건드린다(개발 머신·CI 전용).

운영 부작용 0: get_crawler 는 None 으로, socketio.emit 은 기록기로, access_lock DB read 는 가짜로
대체한다(외부 네트워크·시트·소켓 브로드캐스트·DB write 없음). ADMIN/MANAGER 비밀키는 알려진 값을
env 로 주입해 ?login 분기를 결정적으로 만든다.

사용:
    IMT_DISABLE_BOOTSTRAP=1 python test_pages_index_contract.py            # 스냅샷과 비교 (FAIL 이면 exit 1)
    IMT_DISABLE_BOOTSTRAP=1 python test_pages_index_contract.py --update   # 리팩토링 전/의도된 변경 후 스냅샷 갱신

--update 는 각 시나리오를 2회 렌더해 정규화 후 동일한지(노이즈 바닥 0) 먼저 확인하고,
흔들리면 스냅샷을 쓰지 않고 그 자리를 보고한다(WORK_PROTOCOL 4-B).
"""
import json
import os
import re
import sys
from datetime import datetime

# ASSERT-01 - 크롤러/스케줄러 실기동 차단. env 는 import 전에 고정해야 한다.
os.environ.setdefault('IMT_DISABLE_BOOTSTRAP', '1')
os.environ.setdefault('IMT_ADMIN_SECRET_KEY', 'contract_admin_key')
os.environ.setdefault('IMT_MANAGER_SECRET_KEY', 'contract_manager_key')

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)
SNAPSHOT = os.path.join(HERE, 'tests', 'pages_index_contract_snapshot.json')

ADMIN_KEY = os.environ['IMT_ADMIN_SECRET_KEY']
MANAGER_KEY = os.environ['IMT_MANAGER_SECRET_KEY']

import app as appmod          # noqa: E402
import web_lib.globals as gl  # noqa: E402
import web_lib.routes.pages as pages  # noqa: E402
import sqlite_handler         # noqa: E402

UA_DESKTOP = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
UA_MOBILE = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)'

# 응답에서 index()/reset 라우트가 실제로 세팅하는 헤더만 고정 순서로 캡처한다.
HEADER_KEYS = ['Content-Type', 'Cache-Control', 'Pragma', 'Expires', 'Server', 'CF-RAY']

# ── 외부 부작용 대체용 홀더 ──
_ACCESS_LOCK = {'val': 'false'}
_EMIT_CALLS = []


def _norm_body(text):
    """실행마다 달라지는 값만 지운다: build_version(시각 폴백) · 522 ray_id · 522 ts."""
    text = text.replace(appmod.BUILD_VERSION, '<BV>')
    text = re.sub(r'[0-9a-f]{12}-ICN', '<RAY>-ICN', text)
    text = re.sub(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC', '<TS> UTC', text)
    return text


def _norm_cookies(cookies):
    out = []
    for ck in cookies:
        ck = re.sub(r'imt_vid=[0-9a-f]+', 'imt_vid=<VID>', ck)
        ck = re.sub(r'session=[^;]+', 'session=<SESSION>', ck)
        ck = re.sub(r'Expires=[^;]+GMT', 'Expires=<EXP>', ck)
        out.append(ck)
    return sorted(out)


def _norm_ray(val):
    return re.sub(r'[0-9a-f]{12}-ICN', '<RAY>-ICN', val) if val else val


def _norm_queue(items):
    """visitor_log_queue 튜플에서 now_str(index 5, 시각)만 <NOW> 로."""
    out = []
    for t in items:
        row = list(t)
        if len(row) > 5:
            row[5] = '<NOW>'
        out.append(row)
    return out


def _install_patches():
    saved = {
        'get_crawler': pages.get_crawler,
        'socketio': pages.socketio,
        'get_system_setting': sqlite_handler.db_handler.get_system_setting,
    }
    import types as _t
    pages.get_crawler = lambda blocking=False: None  # ccu/night 리스트 = [] 로 고정
    pages.socketio = _t.SimpleNamespace(emit=lambda ev, data=None, **k: _EMIT_CALLS.append((ev, data)))
    _orig_setting = saved['get_system_setting']
    sqlite_handler.db_handler.get_system_setting = (
        lambda key, default=None: (_ACCESS_LOCK['val'] if key == 'access_lock' else _orig_setting(key, default))
    )
    return saved


def _restore_patches(saved):
    pages.get_crawler = saved['get_crawler']
    pages.socketio = saved['socketio']
    sqlite_handler.db_handler.get_system_setting = saved['get_system_setting']


def _reset_state():
    gl.admin_visit_logs.clear()
    gl.visitor_names.clear()
    gl.visitor_log_queue.clear()
    _EMIT_CALLS.clear()
    # [v6.7.733] 로그인 실패 제한은 프로세스 전역이다 - login_wrong_key 가 회차마다 쌓여 잠기면 login_* 결과가 흔들린다.
    pages._LOGIN_LIMIT['fails'].clear()
    pages._LOGIN_LIMIT['locked_until'] = 0.0


# 시나리오: 코드가 분기하는 모든 조건. (name, kwargs)
#   ua · headers(추가) · query · session(사전세팅) · access_lock · vid(imt_vid 쿠키 주입) ·
#   seed_recent(60초 dedup 대상 방문로그 선주입)
# 쿠키는 반드시 test_client.set_cookie 로 주입한다 - 수동 Cookie 헤더는 test_client 가 무시한다(실측).
SCENARIOS = [
    ('anon_desktop_unlocked', dict(ua=UA_DESKTOP)),
    ('anon_desktop_locked', dict(ua=UA_DESKTOP, access_lock='true')),
    ('manager_desktop_locked', dict(ua=UA_DESKTOP, access_lock='true',
                                    session={'role': 'manager', 'is_admin': False})),
    ('admin_desktop_locked', dict(ua=UA_DESKTOP, access_lock='true',
                                  session={'role': 'admin', 'is_admin': True})),
    ('legacy_isadmin_locked', dict(ua=UA_DESKTOP, access_lock='true',
                                   session={'is_admin': True})),  # role 미설정 legacy 폴백
    ('anon_mobile_unlocked', dict(ua=UA_MOBILE)),  # mobile_index.html 부재 -> index.html fall-through
    # mobile_index.html 이 존재하는 배포 상태를 흉내낸다(레포엔 없다): 존재 검사만 참으로 바꾸고
    # render_template 은 이름·인자를 기록하는 대역으로 - 모바일 분기와 그 안의 imt_vid 쿠키 발급을 채점한다.
    ('mobile_template_exists', dict(ua=UA_MOBILE, mobile_tpl=True)),
    ('mobile_template_exists_with_vid', dict(ua=UA_MOBILE, mobile_tpl=True, vid='fixedvid00000001')),
    # 크롤러가 있을 때 ccu_games / night_report_games 가 렌더까지 전달되는지.
    ('crawler_present', dict(ua=UA_DESKTOP, crawler=True)),
    ('login_admin', dict(ua=UA_DESKTOP, query='login=' + ADMIN_KEY)),
    ('login_manager', dict(ua=UA_DESKTOP, query='login=' + MANAGER_KEY)),
    ('logout_from_admin', dict(ua=UA_DESKTOP, query='login=logout',
                               session={'role': 'admin', 'is_admin': True})),
    ('login_wrong_key', dict(ua=UA_DESKTOP, query='login=bogus_not_a_key')),
    ('anon_with_vid_cookie', dict(ua=UA_DESKTOP, vid='fixedvid00000001')),
    ('dedup_recent_second', dict(ua=UA_DESKTOP, seed_recent=True)),
    ('fingerprint_headers', dict(ua=UA_DESKTOP,
                                 headers={'Referer': 'https://ref.example/path',
                                          'Accept-Language': 'ko-KR,ko;q=0.9',
                                          'X-Forwarded-For': '203.0.113.7, 10.0.0.1'})),
]


def _do_request(sc):
    """한 시나리오를 실행하고 측정 대상 요청의 스냅샷 dict 를 반환."""
    _ACCESS_LOCK['val'] = sc.get('access_lock', 'false')
    _reset_state()
    client = appmod.app.test_client()
    if sc.get('session'):
        with client.session_transaction() as s:
            for k, v in sc['session'].items():
                s[k] = v
    if sc.get('vid'):
        client.set_cookie('imt_vid', sc['vid'], domain='localhost')
    if sc.get('seed_recent'):
        # 쿠키 없는 접속의 visitor_id 는 'ip:127.0.0.1'. 그 id 로 방금(60초 내) 방문 기록을 선주입해
        # 측정 요청이 실제 dedup 로직으로 is_recent=True 를 판정하게 한다(경로를 끄지 않는다).
        _now = datetime.now(pages.KST).strftime('%Y-%m-%d %H:%M:%S')
        gl.admin_visit_logs.append({'time': _now, 'vid': 'ip:127.0.0.1',
                                    'ip': '127.0.0.1', 'ua': sc['ua'], 'name': 'Windows PC'})
    headers = dict(sc.get('headers', {}))
    headers['User-Agent'] = sc['ua']
    _tpl_calls = []
    _undo = []
    if sc.get('crawler'):
        import types as _t2
        _fake = _t2.SimpleNamespace(get_ccu_games=lambda: [{'game': 'CCU_MARK'}],
                                    get_night_report_games=lambda: ['NIGHT_MARK'])
        _prev = pages.get_crawler
        pages.get_crawler = lambda blocking=False: _fake
        _undo.append(lambda: setattr(pages, 'get_crawler', _prev))
    if sc.get('mobile_tpl'):
        import types as _t3
        _prev_os, _prev_rt = pages.os, pages.render_template
        pages.os = _t3.SimpleNamespace(path=_t3.SimpleNamespace(
            join=os.path.join,
            exists=lambda q: True if q.endswith('mobile_index.html') else os.path.exists(q)))
        def _rt(name, **kw):
            _tpl_calls.append((name, sorted(kw.items(), key=lambda kv: kv[0])))
            return '<html>MOBILE_STUB</html>'
        pages.render_template = _rt
        _undo.append(lambda: (setattr(pages, 'os', _prev_os), setattr(pages, 'render_template', _prev_rt)))
    path = '/'
    if sc.get('query'):
        path = '/?' + sc['query']

    # 측정 요청 직전에 부작용 기록기 리셋
    _EMIT_CALLS.clear()
    gl.visitor_log_queue.clear()
    logs_before = len(gl.admin_visit_logs)

    try:
        resp = client.get(path, headers=headers)
    finally:
        for _u in _undo:
            _u()

    logs_added = len(gl.admin_visit_logs) - logs_before
    with client.session_transaction() as s:
        sess = {'role': s.get('role'), 'is_admin': s.get('is_admin')}

    hdrs = {k: resp.headers.get(k) for k in HEADER_KEYS}
    hdrs['CF-RAY'] = _norm_ray(hdrs.get('CF-RAY'))
    return {
        'status': resp.status_code,
        'headers': hdrs,
        'set_cookie': _norm_cookies(resp.headers.getlist('Set-Cookie')),
        'body': _norm_body(resp.get_data(as_text=True)),
        'emit': list(_EMIT_CALLS),
        'queue': _norm_queue(list(gl.visitor_log_queue)),
        'logs_added': logs_added,
        'session': sess,
        'template_calls': [repr(c) for c in _tpl_calls],
    }


def collect():
    saved = _install_patches()
    try:
        appmod.app.config['TESTING'] = True
        return {name: _do_request(sc) for name, sc in SCENARIOS}
    finally:
        _restore_patches(saved)


def _noise_floor_ok():
    """각 시나리오를 2회 돌려 정규화 후 동일한지. 흔들리면 (False, name) 반환."""
    saved = _install_patches()
    try:
        appmod.app.config['TESTING'] = True
        for name, sc in SCENARIOS:
            a = json.dumps(_do_request(sc), ensure_ascii=False, sort_keys=True)
            b = json.dumps(_do_request(sc), ensure_ascii=False, sort_keys=True)
            if a != b:
                return False, name
    finally:
        _restore_patches(saved)
    return True, None


def _dump(obj):
    return json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True, default=str) + "\n"


def main():
    if '--update' in sys.argv:
        ok, bad = _noise_floor_ok()
        if not ok:
            print(f"[ABORT] 시나리오 '{bad}' 가 2회 렌더에서 흔들린다 - 정규화 누락. 스냅샷 미기록.")
            return 1
        got = collect()
        os.makedirs(os.path.dirname(SNAPSHOT), exist_ok=True)
        with open(SNAPSHOT, 'w', encoding='utf-8', newline='\n') as f:
            f.write(_dump(got))
        print(f"[UPDATED] {SNAPSHOT} (시나리오 {len(got)}종)")
        return 0

    got = _dump(collect())
    with open(SNAPSHOT, encoding='utf-8') as f:
        want = f.read()
    if got == want:
        print(f"[PASS] index() 동작 계약 동일 (시나리오 {len(SCENARIOS)}종)")
        return 0
    import difflib
    print("[FAIL] index() 동작 계약이 스냅샷과 다르다:")
    for line in difflib.unified_diff(want.splitlines(), got.splitlines(),
                                     'snapshot', 'now', lineterm='', n=1):
        print(line)
    return 1


if __name__ == '__main__':
    sys.exit(main())
