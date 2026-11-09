# -*- coding: utf-8 -*-
"""routes.pages — 최상위 HTML/정적 서빙 라우트.

- `/`               : 메인 대시보드 (데스크탑 / 모바일 / Cloudflare 522 위장 분기)
- `/reset`, `/reset/`: 비전공자용 강력 초기화 페이지 (SW/caches/storage clear)
- `/sw.js`           : Service Worker 동적 서빙 (__BUILD_VERSION__ 치환)

Phase 1 2차 분할 대상. 기존 `web_lib/web_routes.py` 에서 3 라우트 이관.
"""
import hashlib
import hmac
import logging
import os
import re
import threading
import time
from collections import deque
from datetime import datetime as dt_class
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from flask import make_response, redirect, render_template, request, session

import web_lib.globals as gl
from web_lib.globals import socketio
from web_lib.bg_tasks import get_crawler
from web_lib.routes import api_bp

KST = ZoneInfo("Asia/Seoul")
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logger = logging.getLogger(__name__)

# [v6.7.733] ?login 키 실패 제한 - **프로세스 전역 창**(IP 무관). get_client_ip 는 X-Forwarded-For 를 검증 없이 믿어
#   IP 별 카운터는 헤더 한 줄로 우회된다. 그래서 키 로그인 전체를 한 카운터로 묶는다: 60초 안에 5회 틀리면
#   60초 동안 **맞는 키도** 거부한다(잠금 중 시도는 세지 않는다 - 잠금이 스스로 연장되지 않게).
#   대가: 남이 5회 틀려 주면 그 60초는 주인도 새로 못 들어온다. 이미 로그인한 세션(30일)은 무관.
_LOGIN_FAIL_MAX = 5
_LOGIN_FAIL_WINDOW_S = 60.0
_LOGIN_LOCKOUT_S = 60.0
_LOGIN_LIMIT = {'fails': deque(), 'locked_until': 0.0, 'last_digest': b'', 'last_ts': 0.0}
_LOGIN_DUP_S = 2.0   # sw.js 는 522 탐색을 800ms 뒤 한 번 재시도한다 - 같은 오답을 두 번 세지 않게
_LOGIN_LIMIT_GUARD = threading.Lock()

# [v6.7.309] 렌더 결과에서 HTML 주석 전수 제거 — 비관리자 소스 보기에 admin 기능/JS경로/내부구조
#   힌트 주석(예 "그라파나 CCU 관리자 전용. JS = ...")이 노출되던 문제. 인라인 <script>/<style>
#   내 주석 리터럴 0건 확인(안전). Jinja {# #}는 이미 렌더에서 사라지므로 대상은 남은 <!-- --> 뿐.
_HTML_COMMENT_RE = re.compile(r'<!--.*?-->', re.DOTALL)


def _strip_html_comments(html):
    return _HTML_COMMENT_RE.sub('', html)


# [v6.6.257] `/sw.js` 정적 서빙 라우트 제거 — 아래 `serve_sw_js` 가 __BUILD_VERSION__
# 플레이스홀더를 치환하는 올바른 구현. 이전엔 정적 서빙 라우트가 먼저 등록돼 치환 로직이
# 영구 미작동 상태였음(PWA 캐시 버스팅 기능 미적용). service_worker() 제거 → serve_sw_js 단독 활성.


@api_bp.route('/reset')
@api_bp.route('/reset/')
def reset_client_cache():
    """[v6.6.299] 비전공자 사용자용 긴급 초기화 페이지.

    관리자가 '데이터 안 보인다'는 사용자에게 `http://<호스트>/reset` URL 을 보내면
    SW unregister + caches.delete + localStorage.clear() + sessionStorage.clear() 자동 수행 후
    3초 카운트다운 뒤 홈(`/?_t=…`)으로 redirect. 브라우저별 "강력 새로고침" 단축키를
    숙지하지 않은 사용자도 링크 한 번으로 해결.

    페이지 자체는 no-store 로 서빙해 이 페이지도 캐시되지 않게 한다.
    """
    resp = make_response(render_template('reset.html'))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


# ─────────────────────────────────────────────────────────────────────────
# index() 헬퍼 — 143줄 index() 를 동작 불변으로 작은 함수들로 분해한 것.
#   각 헬퍼는 Flask 요청 컨텍스트의 request/session 프록시를 원본과 똑같이 직접 사용한다.
#   호출 순서·예외 처리·주석은 원본을 그대로 보존한다. 동작 계약 검사: test_pages_index_contract.py.
# ─────────────────────────────────────────────────────────────────────────


def _issue_visitor_id_if_absent():
    """[v6.7.287] 브라우저별 영구 토큰 - 같은 공유 IP(NAT) 뒤 다수 PC 구분.

    미보유 시 신규 발급값을 반환(실제 Set-Cookie 는 렌더 단계에서 수행), 보유 시 None.
    """
    if not request.cookies.get('imt_vid'):
        import uuid as _uuid
        return _uuid.uuid4().hex
    return None


def _key_eq(given, expected):
    """[v6.7.733] 상수 시간 비교 - `==` 는 처음 틀린 자리에서 멈춰 응답 시간이 일치 길이를 흘린다."""
    return bool(expected) and hmac.compare_digest(given.encode('utf-8'), expected.encode('utf-8'))


def _match_login_key(login_key):
    """키가 맞으면 'admin'/'manager', 아니면 None. 잠금 중이면 맞는 키도 None 이고 실패로 세지 않는다."""
    now = time.monotonic()
    with _LOGIN_LIMIT_GUARD:
        if now < _LOGIN_LIMIT['locked_until']:
            return None
    if _key_eq(login_key, gl.ADMIN_SECRET_KEY):
        return 'admin'
    if _key_eq(login_key, gl.MANAGER_SECRET_KEY):
        return 'manager'
    digest = hashlib.sha256(login_key.encode('utf-8', 'replace')).digest()
    with _LOGIN_LIMIT_GUARD:
        fails = _LOGIN_LIMIT['fails']
        if digest == _LOGIN_LIMIT['last_digest'] and now - _LOGIN_LIMIT['last_ts'] < _LOGIN_DUP_S:
            return None
        _LOGIN_LIMIT['last_digest'], _LOGIN_LIMIT['last_ts'] = digest, now
        fails.append(now)
        while fails and now - fails[0] > _LOGIN_FAIL_WINDOW_S:
            fails.popleft()
        if len(fails) >= _LOGIN_FAIL_MAX:
            _LOGIN_LIMIT['locked_until'] = now + _LOGIN_LOCKOUT_S
            fails.clear()
            logger.warning(f"[SECURITY] 로그인 키 실패 {_LOGIN_FAIL_MAX}회/{int(_LOGIN_FAIL_WINDOW_S)}초 - "
                           f"키 로그인 전체 {int(_LOGIN_LOCKOUT_S)}초 잠금")
    return None


def _apply_login_key():
    """[보안] 비밀 열쇠(?login=암호)가 주소에 포함되어 있으면 세션에 관리자 권한 부여.

    [v6.7.733] 키가 맞으면 login 파라미터를 뺀 같은 URL 로 302 응답을 돌려준다(주소창·Referer 에서 키 제거).
    그 밖(틀린 키·잠금 중·logout·파라미터 없음)은 None - 호출부가 계속 진행한다.
    """
    login_key = request.args.get('login')
    if login_key:
        granted = _match_login_key(login_key) if login_key != 'logout' else None
        if granted == 'admin':
            session.permanent = True  # [v6.6.142] 브라우저 종료 후에도 세션 유지
            session['role'] = 'admin'
        elif granted == 'manager':
            # [v6.7.248] 중간 관리자 — 일반 사용자 + 그라파나 CCU 탭 1개.
            session.permanent = True
            session['role'] = 'manager'
        elif login_key == 'logout':
            session.pop('role', None)
        # [v6.7.248] is_admin 은 role 에서 파생 — 기존 session['is_admin'] 서버 가드(~30곳) 무수정 호환.
        session['is_admin'] = (session.get('role') == 'admin')
        # [v6.7.733] 종전엔 "리다이렉트를 고려할 수 있으나 일단 세션 부여만" 이었다 - 키가 주소창에 남았다.
        if granted:
            _qs = urlencode([(k, v) for k, v in request.args.items(multi=True) if k != 'login'])
            _resp = redirect(request.path + ('?' + _qs if _qs else ''), code=302)
            _resp.headers['Cache-Control'] = 'no-store'
            return _resp
    return None


def _access_lock_response(ip_addr):
    """[v6.6.354] 서버 사이드 접근 락 enforcement — 비관리자에게 실제 HTTP 522 응답.

    View Source/DevTools 까지 Cloudflare 에러로 보이게 해 원본 HTML 유출 방지.
    login_key 처리 다음에 배치되어 `/?login=KEY` 로 관리자 진입은 항상 가능.
    [v6.7.263] access_lock 시 manager(중간 관리자)도 통과 — anon 만 차단 (사용자 정책 변경: 기존 "manager 도 차단" 폐기).
      role 파생 폴백 — v6.7.248 이전 legacy 세션(is_admin=True·role 미설정)도 admin 으로 정상 판정.
    락 미발동(admin/manager 이거나 access_lock!=true 이거나 조회 실패)이면 None 반환 → 호출부가 계속 진행.
    """
    _lock_role = session.get('role') or ('admin' if session.get('is_admin', False) else 'anon')
    if _lock_role in ('admin', 'manager'):
        return None
    # [v6.7.733] /api 게이트·소켓과 같은 판정 - 종전엔 조회 실패 시 'false' 로 떨어져 앱 셸이 익명에게 나갔다.
    from web_lib.routes._helpers import _access_lock_on
    if not _access_lock_on():
        return None
    import secrets as _secrets
    _ray_id = f"{_secrets.token_hex(6)}-ICN"
    _ts = dt_class.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
    _host = request.host or 'imt-tools.local'
    _resp = make_response(render_template('cloudflare_522.html',
        host=_host, ray_id=_ray_id, ts=_ts, edge_city='Seoul', client_ip=ip_addr), 522)
    _resp.headers['Content-Type'] = 'text/html; charset=UTF-8'
    _resp.headers['Server'] = 'cloudflare'
    _resp.headers['CF-RAY'] = _ray_id
    _resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    _resp.headers['Pragma'] = 'no-cache'
    _resp.headers['Expires'] = '0'
    return _resp


def _record_visit(visitor_id, ip_addr, user_agent, now_str):
    """[추가] 관리자 접속 로그 기록 (IP와 접속 시간, OS/브라우저 정보).

    [v6.6.258] 방문 로그 race 방지 — read-modify-write 를 Lock 으로 감쌈.
    반환: (is_recent, visit_count). is_recent=같은 브라우저 60초 내 재접속,
    visit_count=신규 기록 시 로그 길이(아니면 0 → 호출부가 emit 여부 판정).
    """
    _visit_count = 0
    with gl._visit_lock:
        # 중복 기록 방지 (같은 브라우저(visitor_id)가 1분 이내 재접속 시 무시)
        is_recent = False
        for log in reversed(gl.admin_visit_logs[-10:]):
            if log.get('vid') == visitor_id and (dt_class.strptime(now_str, '%Y-%m-%d %H:%M:%S') - dt_class.strptime(log['time'], '%Y-%m-%d %H:%M:%S')).total_seconds() < 60:
                is_recent = True
                break

        if not is_recent:
            # 수동 등록된 이름이 없으면 User-Agent 에서 기기 종류 자동 추출
            current_name = gl.visitor_names.get(visitor_id)
            if not current_name:
                current_name = gl.get_device_info(user_agent)

            gl.admin_visit_logs.append({
                'time': now_str,
                'vid': visitor_id,
                'ip': ip_addr,
                'ua': user_agent,
                'name': current_name
            })
            if len(gl.admin_visit_logs) > 1000:
                gl.admin_visit_logs.pop(0)
            _visit_count = len(gl.admin_visit_logs)
    return is_recent, _visit_count


def _enqueue_visitor_log(visitor_id, ip_addr, user_agent, now_str):
    """[v6.7.487] **요청 경로에서 DB write 를 걷어냈다.** 여기서 직접 UPSERT 하면 락 경합 시
      busy_timeout(30s)까지 이 스레드가 묶이고, gunicorn --threads 8 이 전부 물리면 새 요청이
      큐에 쌓여 프록시가 504 를 낸다 — 2026-08-05 오전 무응답 2구간의 직접 메커니즘
      (PATTERN-52 대응 4). 이제 큐에 넣기만 하고 `bg/visitor_log.py` 잡이 20초마다 배치로 쓴다.

    [v6.7.293] SQLite visitor_log UPSERT — 60초 dedup(is_recent) 뒤로 gate(호출부에서).
      [v6.7.287] (visitor_id, ip) 복합 PK, 영속 볼륨(imt_db_data) 누적 → 재배포/재시작 보존.
      기존엔 매 index 요청 무조건 write → 위 메모리 admin_visit_logs 의 60초 dedup 과 의미 불일치 +
      렌더 경로 중복 write(새로고침/재접속/배포-리로드 스팸). 이제 메모리 로그가 실제 기록하는 순간
      (is_recent=False)에만 SQLite 도 write → 정합(visit_count = 60초+ 간격 방문) + 2코어 포화 환경에서
      렌더 경로 동기 DB write 감소. 활성 세션 last_seen 신선도는 v6.7.290 heartbeat(touch, 90초)가
      별도 유지하므로 영향 없음. (WAL 모드라 동시 write 안전.)
    """
    try:
        device_label = gl.get_device_info(user_agent)
        resolved_name = gl.visitor_names.get(visitor_id) or device_label
        # [v6.7.304] 진단 지문 — 쿠키없음(시크릿) 접속 식별용. 유입경로/로케일/포워드체인.
        #   길이 상한으로 비정상 헤더(악의적 초장문) 저장 방지.
        _referer = (request.headers.get('Referer', '') or '')[:512]
        _accept_lang = (request.headers.get('Accept-Language', '') or '')[:256]
        _xff = (request.headers.get('X-Forwarded-For', '') or '')[:256]
        # deque.append 는 원자적 — 락 불요. 순서는 upsert_visitor_logs_batch 인자 순서와 동일.
        gl.visitor_log_queue.append((visitor_id, ip_addr, device_label, resolved_name,
                                     user_agent, now_str, _referer, _accept_lang, _xff))
    except Exception:
        # 방문 기록 실패로 index 렌더 막지 않음
        pass


def _render_index_response(is_mobile, ccu_games, night_report_games, new_vid):
    """메인 대시보드 응답 — 모바일 UA + mobile_index.html 존재 시 모바일, 아니면 데스크탑.

    [v6.6.320] HTML 응답에 no-cache 헤더 — 정적 자산은 ?v={{build_version}} 으로 무효화되지만
    HTML 자체가 브라우저 heuristic 캐시로 남으면 구버전 HTML 이 구버전 ?v= 을 물고 있어
    배포해도 재다운로드가 일어나지 않는다. no-cache 로 항상 서버 재검증 강제 → BUILD_VERSION
    갱신 즉시 모든 사용자에게 전파.
    new_vid 보유 시 imt_vid 쿠키 발급([v6.7.287]).
    """
    if is_mobile:
        template_dir = os.path.join(PROJECT_ROOT, 'templates')
        if os.path.exists(os.path.join(template_dir, 'mobile_index.html')):
            _resp = make_response(_strip_html_comments(render_template('mobile_index.html', is_mobile=is_mobile, ccu_games=ccu_games, night_report_games=night_report_games)))
            _resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            _resp.headers['Pragma'] = 'no-cache'
            _resp.headers['Expires'] = '0'
            if new_vid:
                _resp.set_cookie('imt_vid', new_vid, max_age=315360000, httponly=True, samesite='Lax')
            return _resp

    _resp = make_response(_strip_html_comments(render_template('index.html', is_mobile=is_mobile, ccu_games=ccu_games, night_report_games=night_report_games)))
    _resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    _resp.headers['Pragma'] = 'no-cache'
    _resp.headers['Expires'] = '0'
    if new_vid:
        _resp.set_cookie('imt_vid', new_vid, max_age=315360000, httponly=True, samesite='Lax')
    return _resp


@api_bp.route('/')
def index():
    user_agent = request.user_agent.string
    ip_addr = gl.get_client_ip(request)
    visitor_id = gl.peek_visitor_id(request)
    _new_vid = _issue_visitor_id_if_absent()

    _login_redirect = _apply_login_key()
    if _login_redirect is not None:
        return _login_redirect

    # [v6.6.354] 접근 락 enforcement — login_key 처리 다음에 배치되어 `/?login=KEY` 관리자 진입은 항상 가능.
    _lock_resp = _access_lock_response(ip_addr)
    if _lock_resp is not None:
        return _lock_resp

    now_str = dt_class.now(KST).strftime('%Y-%m-%d %H:%M:%S')
    is_recent, _visit_count = _record_visit(visitor_id, ip_addr, user_agent, now_str)

    # Lock 밖에서 emit (Lock 내부에서 emit 하면 블로킹 길어짐)
    if _visit_count:
        socketio.emit('admin_visit_update', {'count': _visit_count})

    if not is_recent:
        _enqueue_visitor_log(visitor_id, ip_addr, user_agent, now_str)

    user_agent_lower = user_agent.lower() if user_agent else ""
    is_mobile = 'mobile' in user_agent_lower or 'android' in user_agent_lower or 'iphone' in user_agent_lower

    crawler = get_crawler(blocking=False)
    ccu_games = crawler.get_ccu_games() if crawler else []
    night_report_games = crawler.get_night_report_games() if crawler else []

    return _render_index_response(is_mobile, ccu_games, night_report_games, _new_vid)


@api_bp.route('/sw.js', methods=['GET'])
def serve_sw_js():
    """[v6.6.227] Service Worker를 동적으로 서빙. __BUILD_VERSION__ 플레이스홀더를
    현재 BUILD_VERSION으로 치환해 캐시 키와 SHELL_ASSETS의 ?v=가 매 부팅마다 갱신되도록.

    [v6.6.265] `import app` 대신 이미 로드된 모듈을 `sys.modules` 로 조회.
    로컬 개발 환경(`python app.py`)에서 app.py 가 `__main__` 이름으로 로드된 경우
    `import app` 은 재실행을 유발해 signal.signal() 이 worker thread 에서 호출돼
    `ValueError: signal only works in main thread` 가 발생하던 문제 해결.
    """
    import sys
    from flask import current_app, Response
    _app_mod = sys.modules.get('app') or sys.modules.get('__main__')
    build_version = getattr(_app_mod, 'BUILD_VERSION', '') if _app_mod else ''
    sw_path = os.path.join(current_app.static_folder, 'js', 'sw.js')
    try:
        with open(sw_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except FileNotFoundError:
        return ('sw.js not found', 404)
    content = content.replace('__BUILD_VERSION__', build_version)
    resp = Response(content, mimetype='application/javascript')
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Service-Worker-Allowed'] = '/'
    return resp
