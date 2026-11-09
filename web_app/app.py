# -*- coding: utf-8 -*-
"""
[INTERFACE] 이 파일의 상세 통신 명세 및 데이터 스키마는 프로젝트 루트의 
=== 운용 카드 (RUNTIME) - "지금 어떻게 도는가" 만 적는다. 능력·이력은 아래 본문과 CHANGELOG. ===
  검증: `python3 scripts/check_state_drift.py`
  인스턴스 1개 - 컨테이너 `imt-container` (홈 서버). 웹앱 = IME = 통합 모니터링 환경.
     포트   = 5008 -> 5001. gunicorn gthread `-w 1 --threads 8 --timeout 240`.
              외부 공개 주소는 Tailscale Funnel(443) -> 호스트 127.0.0.1:5008. 주소 값은 `.env` 의 IMT_ALLOWED_ORIGINS.
              **`:5008` 직통으로 로그인 금지** (admin 키가 평문으로 나간다).
     env    = `IMT_ADMIN_SECRET_KEY` · `IMT_MANAGER_SECRET_KEY` · `IMT_ALLOWED_ORIGINS` ·
              `FLASK_SECRET_KEY` · `DB_DIR`=/app/data. 값은 `web_app/.env`(**git 밖**)이고
              `.dockerignore` 가 이미지에 굽는 것도 막는다(레이어에 굳으면 로테이트해도 남는다).
     데이터 = named volume `imt_db_data`. `crawled_data.sqlite` + 판정 사이드카 `fp.sqlite` 가 **동거**한다
              (워처 3종은 v6.7.743~744 에 지웠다 - 볼륨에 남은 옛 워처 DB 파일은 사용자 결정 대기).
     버전   = `BUILD_VERSION` 은 코드 상수가 아니라 빌드 때 기록되는 `.build_version` 파일이다.
     시트   = "Data HUB" 의 `Config_*` 탭들. 인증은 SA 풀 2개를 요청마다 로테이션하는데
              **둘 다 개인 계정 소유**라 승계 리스크가 여기 있다(docs/DOC_DRIFT.md 6).
     배포   = `sudo imt-deploy webapp`. exit 0/3/4/5 로 판정하고 **Stage 4 는 사람 몫**이다
              (사이클이 돌아야 터지는 결함은 부팅 시 조용하다).
     ⚠ `access_lock` **상시 ON 이 정상**이다. 익명 `/` 의 522 는 위장 응답이지 장애가 아니다.
              락 ON 이면 익명 `/api/*` 는 403(`/api/health` 만 예외), 소켓 연결은 거부된다(v6.7.733).
====================================================================================================

[INTERFACE_REGISTRY.md](INTERFACE_REGISTRY.md)를 반드시 참고하십시오.
"""
"""
Flask 웹 애플리케이션 - 통합 크롤러 (Daum Cafe + DC Gallery)
"""

import os
import re
import sys

# 모듈 경로 추가 (폴더 구조 변1경 대응)
_base = os.path.dirname(os.path.abspath(__file__))
for _sub in ('', 'crawlers', 'utils', 'ai_lib'):
    sys.path.append(os.path.join(_base, _sub) if _sub else _base)

# GUI 모드(No Console) 또는 백그라운드 실행 시 sys.stdout/stderr가 None일 때 발생하는 라이브러리 오류 방지
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

from flask import Flask, jsonify, request, Response, session
from flask_cors import CORS
# [v6.6.470] 응답 gzip 압축 — /api/articles/all 1.2MB 등 JSON 대용량 응답이 LCP 직격(3.56s).
#   text 압축비 5x+ → 200~250KB 수준으로 단축. flask-compress 미설치 환경 대응 위해 try/except.
try:
    from flask_compress import Compress
    _HAS_COMPRESS = True
except ImportError:
    _HAS_COMPRESS = False
import atexit
import signal
import threading
import logging
import posixpath
from urllib.parse import unquote
from apscheduler.schedulers.background import BackgroundScheduler

# [리팩토링] 전역 경로 상수 정의 (중복 제거)
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

from common import setup_logging

# 로깅 설정
setup_logging()
# [v6.7.336] 'app' 이 아닌 전용 이름 사용 필수 — __name__ 은 'app' 이라
#   logging.getLogger(__name__) 이 Flask 의 app.logger(=getLogger('app')) 와 **동일 객체**가 된다.
#   그 상태에서 아래 app.logger.setLevel(ERROR)(werkzeug 소음 차단용)가 이 모듈 로거까지 함께 낮춰
#   [BOOTSTRAP] info 로그가 구조적으로 출력 불가였다(배포 검증 신호 상실 — CLAUDE.md 배포 섹션 참조).
#   이름을 분리해 Flask 로거는 ERROR 로 두고 모듈 로거만 root(INFO) 를 상속한다.
logger = logging.getLogger('imt.app')

# [리팩토링] 설정 클래스 정의 (환경 변수 지원 및 설정 캡슐화)
class Config:
    JSON_AS_ASCII = False
    TEMPLATES_AUTO_RELOAD = True
    SEND_FILE_MAX_AGE_DEFAULT = 0 # 정적 파일(CSS 등) 캐싱 방지
    # [v6.7.733] 세션 쿠키 SameSite=Lax - 타 사이트발 POST(재시작·access-lock·설정 저장)에 쿠키가 안 실린다(CSRF 토큰 부재 보완).
    SESSION_COOKIE_SAMESITE = 'Lax'
    # SESSION_COOKIE_SECURE 는 켜지 않는다 - LAN/tailnet :5008 평문 http 접속이 아직 있다. 그 경로를 닫으면 True 로.

# Flask 앱 초기화
app = Flask(__name__, 
            static_url_path='/static',
            static_folder=os.path.join(PROJECT_ROOT, 'static'),
            template_folder=os.path.join(PROJECT_ROOT, 'templates'))
app.config.from_object(Config) # [적용] Config 클래스 로드

# Werkzeug 로거 비활성화 또는 레벨 설정
logging.getLogger('werkzeug').setLevel(logging.ERROR) # Silences most Werkzeug info logs
app.logger.setLevel(logging.ERROR) # Also silence default Flask app logger
# 세션 및 보안용 비밀 키 설정 (도커 환경 대응)
# [v6.6.258] 하드코딩 리터럴 제거 — 환경변수 미설정 시 기동 시마다 랜덤 secret 생성.
# 단점: 컨테이너 재시작 시 기존 세션 무효화 → 관리자 재로그인 필요 (보안 트레이드오프 감수).
# 프로덕션에선 FLASK_SECRET_KEY 환경변수 필수 설정 권장.
app.secret_key = os.environ.get('FLASK_SECRET_KEY') or os.urandom(32).hex()
from datetime import timedelta
app.permanent_session_lifetime = timedelta(days=30)  # [v6.6.142] 세션 유효기간 30일

# [v6.6.279] CORS 화이트리스트 — 이전엔 CORS(app) 로 모든 origin 허용(공격 표면 큼).
# IMT_ALLOWED_ORIGINS 환경변수(콤마 구분)로 오버라이드 가능. 기본: 공개 주소(Tailscale Funnel) + 로컬 개발.
# [v6.7.746] 공개 주소 값은 비공개 자리 `utils/private_config.py` 의 PUBLIC_ORIGINS 에서 온다.
#   v6.7.745 는 이것을 기본값에서 빼고 운영 .env 를 정본으로 봤지만, 운영 .env 에 그 값이 없어 공개 주소의 소켓이
#   400 이 됐다(2026-10-26 배포 직후 실측). 문서의 "env 에 들어 있다" 를 라이브로 재지 않고 믿은 것이 원인(PATTERN-84).
#   HTTPS 는 443 디폴트라 Origin 헤더에 포트가 없다 → 값에도 포트 없이 적는다.
from private_config import PUBLIC_ORIGINS  # noqa: E402 - 위에서 utils 를 sys.path 에 넣었다
_default_origins = list(PUBLIC_ORIGINS) + [
    "http://localhost:5008",
    "http://127.0.0.1:5008",
    "http://localhost:5001",  # 컨테이너 내부 포트
]
_allowed_origins_env = os.environ.get('IMT_ALLOWED_ORIGINS', '').strip()
_allowed_origins = [o.strip() for o in _allowed_origins_env.split(',') if o.strip()] or _default_origins
CORS(app, origins=_allowed_origins, supports_credentials=True)

# [v6.6.470] gzip 압축 활성화 — JSON/HTML/CSS/JS 응답 자동 압축. 폰트(woff2)는 이미 압축본이라 자동 제외.
#   COMPRESS_LEVEL 6 = balanced (속도/압축비). 500 bytes 미만은 압축 비용 > 이득이라 스킵.
if _HAS_COMPRESS:
    app.config['COMPRESS_MIN_SIZE'] = 500
    app.config['COMPRESS_LEVEL'] = 6
    Compress(app)
    logger.info("[BOOTSTRAP] flask-compress 활성화 — JSON/HTML/CSS/JS gzip 압축")
else:
    logger.warning("[BOOTSTRAP] flask-compress 미설치 — 'pip install flask-compress' 권장 (LCP 단축)")
app.logger.setLevel(logging.INFO)  # 경고 출력용 잠시 상향
app.logger.warning(f"[SECURITY] CORS allowed origins: {_allowed_origins}")
app.logger.setLevel(logging.ERROR)

# [v6.6.227] 정적 자산 자동 cache busting — 모든 ?v=, SW CACHE_VERSION/SHELL_ASSETS, server_version emit 의 단일 소스.
# [v6.7.261] BUILD_VERSION 을 "부팅 시각 timestamp" → "이미지-불변 baked 값" 으로 전환 (false "서버 업데이트 중" 근절).
#   기존: str(int(time.time())) = 프로세스 부팅마다 새 값 → autoheal restart / 워커 SIGKILL 재기동 / 컨테이너
#         재시작 (PATTERN-29/30/31) 처럼 "코드 배포가 아닌" 재시작에서도 값이 바뀐다. 그러면 열린 탭의
#         meta(옛 값) vs server_version(새 값) mismatch → socket.js _maybeAutoReload / boot.js
#         _checkStaleSwCacheOnBoot 가 "서버 업데이트 중" 오버레이 + 강제 reload 를 발화 (사용자 보고
#         "빌드 아닌데도 가끔 뜸"의 구조적 원인).
#   변경: Dockerfile 빌드 단계가 /app/.build_version 에 빌드 시각을 1회 굽고(COPY 직후 RUN date), 런타임은
#         그 파일을 읽는다. 같은 이미지 재시작 = 동일 값 → mismatch 0 → false 오버레이/reload 소멸. 진짜
#         docker-compose up --build = 새 이미지 = 새 값 → 정당한 자동 reload 는 그대로 유지. 파일 부재(로컬
#         python app.py)는 timestamp 폴백으로 기존 동작 보존. PATTERN-22(호스트 000 권한) 무관 — 호스트 COPY
#         파일이 아니라 컨테이너 내부 RUN 산출 파일이라 btrfs/ACL 000 전파 경로를 타지 않는다.
import time as _time_mod
def _resolve_build_version():
    try:
        _bv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.build_version')
        with open(_bv_path, 'r', encoding='utf-8') as _f:
            _bv = _f.read().strip()
        if _bv:
            return _bv
    except Exception:
        pass
    # 폴백 — 로컬 개발(파일 부재) 또는 읽기 실패 시 부팅 timestamp (기존 동작 보존)
    return str(int(_time_mod.time()))
BUILD_VERSION = _resolve_build_version()

# [v6.7.9] 알림 출처 — send_kakaowork 블록 알림에 "출처: IME" 자동 삽입
try:
    from notification_manager import set_app_source as _set_app_source
    _set_app_source('IME')
except Exception:
    pass

# [v6.7.638] 탭 은닉 스위치. [v6.7.726] 정본은 web_lib/hidden_apps.py 로 옮겼다(백그라운드 잡도 읽어야 해서) - 경위는 그 docstring.
from web_lib.hidden_apps import HIDDEN_APP_IDS  # noqa: E402

@app.context_processor
def _inject_build_version():
    # [v6.7.254] is_admin/is_manager 전역 주입 — 템플릿에서 admin 마크업을 서버 조건부 렌더링하여
    #   비관리자에게는 admin UI 구조를 HTML 소스(View Source)에서도 은닉. (CSS/JS 숨김만으론 소스에 노출됨)
    try:
        _role = session.get('role') or ('admin' if session.get('is_admin') else 'anon')
        _is_admin = bool(session.get('is_admin', False))
    except Exception:
        _role, _is_admin = 'anon', False
    return {
        'build_version': BUILD_VERSION,
        'hidden_apps': HIDDEN_APP_IDS,
        'is_admin': _is_admin,
        'is_manager': _role == 'manager',
        'user_role': _role,
    }

# ====================================================================
# [v6.7.44] 스캐너 봇 차단 + robots.txt — wpbot/forms.gle 위시 sourced 역옵트아웃 폼 운영자 데이터 수집 회피.
# 차단 정책:
#   1. UA substring 매치 (대소문자 무시) — wpbot, forms.gle 두 키워드 충분 (wpbot 가 forms.gle 를 contact 로 박는 결합).
#   2. 매치 시 403 즉시 반환 + 1회 warning 로그 (재방문 시 spam 방지 위해 IP+UA 조합 dedupe).
#   3. robots.txt 도 추가 — 정상 봇 표준 채널. wpbot 가 안 지키더라도 formal record.
# 향후 패턴 추가는 BLOCKED_UA_PATTERNS 만 갱신.
# ====================================================================
BLOCKED_UA_PATTERNS = ('wpbot', 'forms.gle')
_BLOCKED_UA_LOGGED = set()  # (ip, ua) dedupe — 로그 spam 방지

@app.before_request
def _health_count_request():
    """[v6.7.70] F-7 요청 카운터. 1분 누적 → health_sampler 가 sample 시 reset."""
    try:
        from web_lib.bg.health_sampler import record_request
        record_request()
    except Exception:
        pass


@app.before_request
def _block_scanner_bots():
    ua = (request.headers.get('User-Agent') or '').lower()
    for pattern in BLOCKED_UA_PATTERNS:
        if pattern in ua:
            ip = request.headers.get('X-Forwarded-For', request.remote_addr or '?').split(',')[0].strip()
            key = (ip, ua[:120])
            if key not in _BLOCKED_UA_LOGGED:
                _BLOCKED_UA_LOGGED.add(key)
                if len(_BLOCKED_UA_LOGGED) > 500:
                    _BLOCKED_UA_LOGGED.clear()  # 메모리 누적 방지 (단순 lru 대신 임계 reset)
                app.logger.warning(f"[BOT-BLOCK] {pattern} 매치 — IP={ip} UA={ua[:200]}")
            return Response('', status=403, mimetype='text/plain')
    return None


# ====================================================================
# [v6.7.305] admin 전용 static JS 서버 게이트 — 비관리자에게 admin/ 하위 JS 403.
#   배경: index.html 이 admin JS 를 로드해도(Layer 2 조건부 렌더로 익명엔 미로드) URL 을
#   아는(유출본) 사람이 /static/js/tabs/admin/*.js 를 직접 GET 하면 admin 동작 로직이
#   그대로 노출된다. static 서빙은 index() 라우트의 access_lock(522) 게이트를 안 거치므로
#   별도 서버 가드가 유일한 우회불가 차단. ui-control.js 는 게이팅 로직 본체(checkAdminStatus/
#   _revokeAdminUI)라 익명도 실행돼야 admin UI 정리가 되므로 예외 허용.
# ====================================================================
# [v6.7.309] admin 전용 기능 JS — tabs/admin/ 폴더 외에 tabs/·main/ 직속에 흩어진 admin 기능 파일.
#   basename 기준 매칭(각 파일명 유일). ccu-grafana 만 manager 개방(MANAGER_EXTRA 정합).
#   [v6.7.313] ai-summary-tab.js — manager 개방 회수(사용자 결정 2026-07-11), admin 전용 이동.
#   [v6.7.500] ai-summary-tab.js — manager 재개방(사용자 결정 2026-08-11)으로 _ADMIN_MANAGER_JS 로 이동.
_ADMIN_ONLY_JS = frozenset({
    'ai-tab.js', 'search-tab.js', 'diamond-tab.js', 'account-tab.js', 'trend-tab.js',
    'log-tab.js',
    'anomaly-tab.js',     # [v6.7.439] 이상감지 - 기능 전 표면 admin 한정
})
# [v6.7.383] 알림 탭(naver-watcher)·워처 상태등/배너(topbar-status)·알림 보관소(notif-inbox) 를 manager 개방으로 이동.
# [v6.7.428] discord-tab.js — manager 개방(naver-watcher v6.7.383 선례 동형)으로 _ADMIN_ONLY_JS 에서 이동.
# [v6.7.500] ai-summary-tab.js — manager 개방. 데이터 API `/api/ai-summary` 는 v6.7.386 부터 이미
#   `_require_role('manager')` 였고(배너·보관소 개방분), 탭 UI 만 admin 에 남아 있던 비대칭을 해소.
# [v6.7.741] naver-watcher-tab.js · discord-tab.js 삭제(알림 탭 · 디스코드 탭 제거, 워처 종료). topbar-status.js 는
#   그라파나 CCU 상태등만 남아 그대로 둔다.
_ADMIN_MANAGER_JS = frozenset({
    'ccu-grafana-tab.js', 'topbar-status.js', 'notif-inbox.js',
    'ai-summary-tab.js',  # [v6.7.500] AI 요약 — manager 개방
    'situation-tab.js', 'graph-tab.js', 'pass-rate-graph.js',  # [v6.7.711] 대시보드 — manager 개방(사용자 2026-10-01)
    'verdict-tab.js',  # [v6.7.725] 판정 - manager 개방(사용자 2026-10-11). 사람 판정 쓰기만 admin(routes/verdict.py)
})


def _normalized_path(raw):
    """[v6.7.733] 게이트 판정용 경로 - // 병합, . / .. 해석, 끝 / 제거.

    라우팅·정적 서빙은 원래 경로로 그대로 간다. 이 값은 '막을지' 판정에만 쓴다.
    request.path 는 WSGI 가 이미 한 번 디코딩한 값이고 safe_join 도 이 값을 푼다 - 여기서 더 unquote 하면
    리터럴 `%2e%2e` 세그먼트를 `..` 로 읽어 접두를 하나 더 벗겨 내 오히려 통과시킨다(교차 검토가 재현).
    남은 '%' 는 정적 게이트가 따로 닫는다(_gate_admin_static).
    정적 서빙(werkzeug safe_join)이 posixpath.normpath 로 같은 해석을 하므로, 원래 경로로 판정하던 때는
    끝 '/'(`ai-tab.js/`)와 '../'(`tabs/foo/../admin/db-console.js`) 변형이 basename 매칭을 비켜 200 이었다.
    """
    p = raw or '/'
    return posixpath.normpath('/' + p.lstrip('/'))


@app.before_request
def _gate_admin_static():
    p = _normalized_path(request.path)
    if not p.startswith('/static/js/'):
        return None
    _role = session.get('role') or ('admin' if session.get('is_admin') else 'anon')
    if '%' in (request.path or '') and _role != 'admin':
        return Response('', status=403, mimetype='text/plain')
    base = p.rsplit('/', 1)[-1]
    # tabs/admin/ 폴더 전체 (ui-control.js 게이팅 로직만 예외 — 익명도 실행돼야 admin UI 정리됨)
    if p.startswith('/static/js/tabs/admin/') and base != 'ui-control.js':
        if _role != 'admin':
            return Response('', status=403, mimetype='text/plain')
    elif base in _ADMIN_ONLY_JS:
        if _role != 'admin':
            return Response('', status=403, mimetype='text/plain')
    elif base in _ADMIN_MANAGER_JS:
        if _role not in ('admin', 'manager'):
            return Response('', status=403, mimetype='text/plain')
    return None


# ====================================================================
# [v6.7.733] access_lock 서버 전역 게이트 - /api/* 전부.
#   배경: 락은 index()(pages.py) 한 곳에서만 집행됐다. 익명 `/` 는 522 였지만 /api/articles/all ·
#   /api/comments · /api/games · /api/admin/check(락 상태·client_ip 까지) 등이 익명에게 데이터를 그대로
#   돌려줬다(라이브 실측 2026-10-21). 락 ON + admin/manager 아님 -> 403. 락 OFF 면 종전 동작(주인 스위치).
#   락 원천은 index() 와 같은 system_settings.access_lock(`_access_lock_on`). 소켓은 web_sockets.py connect 가 같은 판정.
#   익명 허용 목록 - 근거(2026-10-21 grep):
#     /api/health  docker HEALTHCHECK(docker-compose.yml test -> healthcheck.sh 의 curl). imt-deploy Stage 3 은
#                  이 healthy 상태를 기다린다(scripts/imt-deploy.sh). docker-deploy-verifier 외부 진단도 이 URL.
#   그 밖의 익명 경로는 /api 를 안 부른다: 522 위장 페이지(templates/cloudflare_522.html) · /reset · /sw.js · 정적 자산.
#   허용은 정규화 전 경로가 정확히 같을 때만 - 인코딩 변형으로 허용을 얻지 못하게 한다. 차단 판정은 정규화 경로로.
# ====================================================================
_API_ANON_ALLOW = frozenset({'/api/health'})


@app.before_request
def _gate_api_access_lock():
    p = _normalized_path(request.path)
    if p != '/api' and not p.startswith('/api/'):
        return None
    if request.path in _API_ANON_ALLOW:
        return None
    from web_lib.routes._helpers import _session_role, _access_lock_on
    if _session_role() in ('admin', 'manager'):
        return None
    if not _access_lock_on():
        return None
    return jsonify({'error': 'forbidden'}), 403

@app.route('/robots.txt')
def robots_txt():
    """[v6.7.44] 봇 표준 채널 — 정상 봇이 자율적으로 따르는 exclusion. wpbot 류는 안 지킴."""
    body = (
        "User-agent: wpbot\n"
        "Disallow: /\n"
        "\n"
        "User-agent: *\n"
        "Disallow: /api/\n"
        "Disallow: /admin/\n"
    )
    return Response(body, mimetype='text/plain')

# 분리된 전역 상태 및 인스턴스 로드
from web_lib.globals import socketio, executor, contents_stop_event
import web_lib.globals as gl
from web_lib.bg_tasks import (
    get_crawler,
    update_client_status,
    start_crawler_with_delay, _trigger_batch_run
)

# SocketIO 초기화 바인딩
socketio.init_app(app)

# 웹소켓 이벤트 핸들러 모듈 등록 (임포트 즉시 데코레이터가 실행되어 socketio에 바인딩됨)
import web_lib.web_sockets  # noqa: F401 - 부수효과 임포트

# 스케줄러 초기화
scheduler = BackgroundScheduler()
gl.scheduler = scheduler  # 순환 참조 없이 web_routes.py에서 gl.scheduler로 접근 가능

# Blueprint 라우터 등록
from web_lib.web_routes import api_bp
app.register_blueprint(api_bp)

# ====================================================================
# [v6.7.182] PERF-01 — 정적 자산 장기 캐시 (재방문 49자산 재요청 제거 → LCP↓)
#   SEND_FILE_MAX_AGE_DEFAULT=0 의 no-cache 를 /static/ 에 한해 덮어씀.
#   안전 변형: ?v=BUILD_VERSION 으로 버전 분리된 자산만 immutable(1년).
#     ?v= 없는 자산(icon.svg/manifest/offline)은 1h 단기캐시 → 갱신 막힘 방지.
#   - /sw.js·/·/reset 은 /static/ 경로 밖이라 자동 제외 (각자 no-store 유지).
#   - Vary/Content-Encoding 절대 미터치 → flask-compress gzip 정합 보존.
#   - Compress(app) 보다 나중 등록 → after_request LIFO 상 압축 전 실행(Vary 보존).
# ====================================================================
@app.after_request
def _static_cache_headers(resp):
    try:
        p = request.path or ''
        if p.startswith('/static/'):
            if p == '/static/manifest.json':
                # [v6.7.390] manifest 는 ?v= 가 붙어도 no-cache(재검증) — 설치형 PWA(WebAPK)가 설치 시점 URL(?v=구버전)을
                #   immutable(1yr)로 물고 있으면 scope/shortcuts 변경이 재설치 전까지 전달 안 됨. ETag/Last-Modified 로 304 값이라 대역 부담 미미.
                resp.headers['Cache-Control'] = 'no-cache'
            elif request.args.get('v'):
                resp.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
            else:
                resp.headers['Cache-Control'] = 'public, max-age=3600'
            resp.headers.pop('Expires', None)   # 기본 핸들러 Expires:0 잔존 제거 (immutable 모순 차단)
            resp.headers.pop('Pragma', None)
    except Exception:
        pass
    return resp


# [v6.7.734] HTTPS 로 열린 화면의 http:// 하위 자원(수집 글 썸네일 등)을 브라우저가 https 로 받게 한다 -
#   크롬이 페이지 전체를 '주의 요함'(혼합 콘텐츠)으로 표시하던 것. HTTPS 판단은 프록시(Tailscale Funnel/serve,
#   호스트 역방향 프록시)가 주는 X-Forwarded-Proto, 없으면 '포트 없는 호스트 이름'(두 프록시 경로 모두 443 전용이다).
#   평문 :5008(LAN/tailnet 직통)에는 보내지 않는다 - 보내면 같은 출처 자원까지 https://...:5008 로 바뀌어 화면이 깨진다.
#   ponytail: https 를 못 주는 썸네일 호스트는 그림이 빠진다 - 문제가 되면 이미지 프록시로.
def _is_https_request():
    if (request.headers.get('X-Forwarded-Proto') or '').split(',')[0].strip().lower() == 'https':
        return True
    host = request.host or ''
    return ':' not in host and not re.fullmatch(r'[0-9.]+', host)


@app.after_request
def _upgrade_insecure_subresources(resp):
    try:
        if _is_https_request():
            resp.headers.setdefault('Content-Security-Policy', 'upgrade-insecure-requests')
    except Exception:
        pass
    return resp

# ====================================================================
# 에러 핸들링
# ====================================================================
@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Not Found'}), 404

@app.errorhandler(500)
def internal_error(error):
    try:
        crawler = get_crawler()
        if crawler:
            crawler._log(f"Internal Server Error: {error}", 'error', 'SERVER')
    except Exception:
        pass
    return jsonify({'error': 'Internal Server Error'}), 500

# ====================================================================
# 앱 초기화 및 백그라운드 작업 시작
# ====================================================================
# 모듈 레벨에서의 스레드 시작 제거 (main 블록으로 이동)

# [수정] 아래의 초기화 및 실행 로직은 web_lib.bg_tasks에서 독립적으로 관리됩니다.



# [제거] 댓글 캐시 갱신 스케줄 - 메모리 기반(gl.comment_data_status)으로 통합됨
# (기존의 comments_cache.json 파일 생성 로직 삭제)


# 앱 종료 시 크롤러 리소스 정리
_cleanup_started = False

def cleanup_resources(signum=None, frame=None):
    """애플리케이션 종료 시 호출되어 모든 백그라운드 리소스를 정리합니다."""

    # [v6.7.130] code-reviewer P1-1 보강 — 명시적 재진입 플래그.
    # SIGTERM → cleanup_resources(signum=15) → sys.exit(0) → atexit → cleanup_resources(None)
    # 경로에서 첫 호출의 sleep(5) 도중 두 번째 호출 들어오면 sleep 또 5초 추가 가능.
    # scheduler.running / executor._shutdown 가드만으로는 비동기 shutdown 윈도우에 통과 못 함.
    global _cleanup_started
    if _cleanup_started:
        return
    _cleanup_started = True

    # 중복 호출 방지 (이미 정리 중이면 스킵) — 이전 가드 보존 (downgrade safety)
    if not scheduler.running and getattr(executor, '_shutdown', False):
        return
    sig_name = f" ({signal.Signals(signum).name})" if signum else ""
    logger.info(f"애플리케이션 종료 신호 수신{sig_name}, 리소스 정리 시작...")
    
    # [신규] 댓글 크롤러 중단 신호
    contents_stop_event.set()

    # [v6.7.129] 댓글 크롤러가 contents_stop_event 받고 finally 블록의
    # _save_cookies + driver.quit 완료할 시간을 보장. 이전엔 set 후 즉시 다음
    # cleanup 단계로 진행해 댓글 크롤러 driver 가 비정상 종료될 가능성 있었음.
    # gunicorn --graceful-timeout 30s 안에 충분 (보통 1~3초면 완료).
    import time as _time
    _time.sleep(5)

    crawler = get_crawler()
    # 크롤러 스레드를 먼저 중지 신호를 보냅니다.
    if crawler and crawler.is_running:
        logger.info("메인 크롤러 중지 요청...")
        crawler.stop()
        # 스레드가 종료될 때까지 잠시 대기 (최대 5초)
        if crawler.crawler_thread and crawler.crawler_thread.is_alive():
            crawler.crawler_thread.join(timeout=5.0)
            if crawler.crawler_thread.is_alive():
                logger.warning("메인 크롤러 스레드가 5초 내에 종료되지 않았습니다.")
    
    # 스케줄러와 스레드 풀 종료 (시작된 경우에만)
    # [v6.6.215] wait=False -- REMOTE_KILL 잡 자신이 scheduler.shutdown을 호출하는 경우
    # 자기 자신의 완료를 기다리다 cannot join current thread 에러 발생을 방지
    if scheduler.running:
        try:
            scheduler.shutdown(wait=False)
        except Exception as e:
            logger.warning(f"[CLEANUP] scheduler.shutdown 예외: {e}")
    gl.executor.shutdown(wait=False, cancel_futures=True)
    gl.main_crawler_executor.shutdown(wait=False, cancel_futures=True)
    gl.export_executor.shutdown(wait=False, cancel_futures=True)
    # [v6.7.291] 레인 전용 executor 들도 동일 정리 (CrawlerManager._lane_executors)
    if crawler is not None:
        for _ex in getattr(crawler, '_lane_executors', {}).values():
            try:
                _ex.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
    # [v6.6.208] Gemma4 요약 워커의 전용 executor 정리
    try:
        from web_lib.ai_gemma4_worker import shutdown_worker as _gemma4_shutdown
        _gemma4_shutdown()
    except Exception:
        pass
    logger.info("모든 백그라운드 작업 정리 완료. 프로세스를 종료합니다.")
    
    if signum:
        sys.exit(0)

atexit.register(cleanup_resources)
# 시그널 핸들러 등록 (Docker/Gunicorn 환경 대응)
# [v6.6.265] 메인 스레드에서만 등록 — 로컬(`python app.py`)에서 app 모듈이 __main__ 으로
# 로드된 상태에서 다른 모듈이 `import app` 을 하면 본 파일이 재실행되는데, 그 재실행이
# Flask worker thread 컨텍스트일 경우 signal.signal() 이 `ValueError: signal only works
# in main thread of the main interpreter` 로 터짐. main-thread 가드로 무해화.
import threading as _threading
if _threading.current_thread() is _threading.main_thread():
    try:
        signal.signal(signal.SIGTERM, cleanup_resources)
        signal.signal(signal.SIGINT, cleanup_resources)
        logger.info("atexit: 리소스 정리 핸들러 등록")
    except (ValueError, OSError) as _e:
        logger.warning(f"signal 핸들러 등록 스킵: {_e}")
else:
    logger.debug("signal 핸들러 등록 스킵 (non-main thread 에서 import 됨)")


# [v6.6.214] 웹앱 전용 원격 킬 스위치
def check_remote_kill_web():
    """2분 주기 REMOTE_KILL / REMOTE_KILL_WEB 체크 → KILL 시 Flask 프로세스 종료.

    APScheduler 잡 내부에서 sys.exit(0)은 SystemExit로 잡혀 잡만 죽으므로,
    반드시 os._exit(0)으로 인터프리터를 즉시 종료해야 한다.
    """
    try:
        from notification_manager import NotificationManager
        if NotificationManager().check_remote_kill('WEB'):
            logger.warning("[REMOTE_KILL] 웹앱 종료 신호 수신 — 리소스 정리 후 강제 종료")
            try:
                cleanup_resources()
            except Exception as ce:
                logger.error(f"[REMOTE_KILL] cleanup 중 오류: {ce}")
            os._exit(0)
    except Exception as e:
        logger.error(f"[REMOTE_KILL] 체크 중 오류: {e}")

# [v6.6.224 FIX] Gunicorn/uWSGI 배포 시 __main__ 블록이 실행되지 않아 크롤러 스레드와
# 모든 스케줄러 잡(GEMMA4 워커 포함)이 영원히 등록되지 않던 버그 수정. 백그라운드 초기화
# 전체를 모듈 레벨로 끌어올려 import 시점에 한 번만 실행되도록 가드.
# docker-compose가 -w 1로 단일 워커를 강제하므로 안전. 멀티 워커 확장 시 gunicorn post_fork 훅으로 전환 필요.
# 주기 정본 - add_job 인자와 부팅 로그의 "파이프라인 이론 상한" 계산이 같은 값을 쓴다
# (구 코드는 로그 쪽에 20/30/120 을 따로 적어 두고 "바꾸면 여기도 바꿔야 한다" 로 버텼다).
_AI_SUMMARY_CYCLE_S = 20
_POST_JUDGE_CYCLE_S = 120


def _register_scheduler_jobs():
    """상시·일회성 스케줄러 잡을 등록한다(시작은 호출자가). 잡 목록의 계약은 test_bootstrap_jobs.py 스냅샷."""
    # [v6.5.56] 00시 일괄추출 — 시트 미반영 모드
    # [v6.6.268] 스케줄 비활성화. import(`_trigger_batch_run`)·비상용 API
    #   (`/api/crawler/batch-run`)·모바일 버튼은 수동 경로로 그대로 유지.
    # [v6.7.334 / 2026-07-15] 비활성 근거 정정 — 아래 3줄을 되살리지 말 것.
    #   구 근거였던 "터미널앱(app_terminal.py)이 00:01/22:01 에 독립 운영 중" 은 **무효**다.
    #   터미널앱은 2026-07-05 은퇴했다(imt_tools_legacy 아카이브). 그럼에도 스케줄을 켜지
    #   않는 이유는 대체자가 필요해서가 아니라 **대상 기능 자체가 중단**됐기 때문이다 —
    #   계정거래(바로템)·다이아 시세(아이템매니아 자사/타사) 크롤링은 현재 미운영
    #   (사용자 확인 2026-07-15). 크롤러 코드(`crawlers/account_trade.py`,
    #   `crawlers/itemmania_crawler.py`)는 재개 대비로 보존만 하는 상태.
    #   ※ POE1/POE2 아이템매니아 크롤러는 이 배치 경로와 무관한 별도 운영 — 혼동 금지.
    # scheduler.add_job(lambda: _trigger_batch_run(trigger_type='scheduled_00', export_gsheet=False),
    #                   'cron', hour=0, minute=1, jitter=60, id='batch_run_daily_0001', replace_existing=True)
    # [v6.6.226] ai_analysis_loader 제거 — /api/ai-analysis가 SQLite를 직접 읽음.
    # [v6.6.230] 자정 데이터 롤오버 제거 — 글 전체 누적 유지 (특정 커뮤니티 글만 남던 버그 해소)
    # 클라이언트 상태 업데이트 (3분 주기)
    scheduler.add_job(update_client_status, 'interval', minutes=3,
                      id='client_status_update', replace_existing=True)
    # 본문 크롤러 헬스체크 (60초 주기)
    # [v6.7.302] misfire_grace_time/coalesce 추가 — 다른 주기 잡과 정합. GIL 포화(PATTERN-29/31) 구간에서
    #   기본 grace 1s 로 hang 감지 잡 자체가 misfire·skip 되어 감지 지연되던 문제 방지.
    from web_lib.bg_tasks import check_content_extractor_health
    scheduler.add_job(check_content_extractor_health, 'interval', seconds=60,
                      id='content_extractor_healthcheck', replace_existing=True,
                      max_instances=1, misfire_grace_time=30, coalesce=True)
    # [v6.6.281] 메인 크롤러 헬스체크 (60초 주기) — is_running 인데 스레드 죽으면 자동 재시작
    from web_lib.bg_tasks import check_main_crawler_health
    scheduler.add_job(check_main_crawler_health, 'interval', seconds=60,
                      id='main_crawler_healthcheck', replace_existing=True,
                      max_instances=1, misfire_grace_time=30, coalesce=True)
    # [v6.6.208] Gemma4 요약 워커 — 60초 주기에서 시작.
    # [v6.7.83] 60s → 180s - 홈 서버(2-6GB RAM) 환경 OOM 회피.
    # [v6.7.107] 180s → 60s 복원 — 사용자 보고 (2026-05-19): 글 작성 → AI 알림 12분 worst.
    #   Gemma4 cycle 단축으로 worst case 2분 단축 (Gemma4 대기 0~3분 → 0~1분).
    #   본질 lag (메인 크롤러 폴링 3~5분 / 시트 hydrate 1분) 은 별도 — 본 변경은 부분 fix.
    #   회귀 위험: v6.7.83 의 OOM SIGKILL 패턴 + PATTERN-31 의 Selenium CPU 부담 합산 시
    #   워커 hang 재발 가능. autoheal 사이드카 (v6.7.99) + healthcheck 완화 (v6.7.102) 가
    #   안전망 — 재발 시 autoheal restart 로 자가복구. 1~2일 모니터링 후 안 풀리면 롤백.
    from web_lib.ai_gemma4_worker import run_gemma4_summary_cycle
    # [v6.7.447] 60초 → 20초. AI 요약 도착이 5~10분 걸린다는 사용자 체감의 두 축 중 하나가
    #   이 픽업 대기였다(다른 축은 크롤 주기, 이쪽은 봇 차단 위험이라 안 건드린다).
    # [v6.7.571 주석 정정] 위 줄은 크롤 주기를 "52~64초" 라고 적고 있었다 - **측정된 값이 아니라
    #   시트 시드값**(utils/setup_config_sheet.py:618-621)을 인용한 것이었고, 라이브는 그때도
    #   지금도 다르다. 2026-08-17 실측(지연 분포에서 역산): NAVER 70~120초(crawler.py:141-142
    #   모듈 기본값 - 시트 hot reload 대상이 아니다) / DAUM 900~1,200초(2026-08-05 시트 변경) /
    #   OFFICIAL 180~300초(crawler.py:346-347). PATTERN-58 사례 - 상수 주석의 근거는 썩는다.
    #   근거: _workspace/2026-08-17_ime_latency_budget.md §1-3.
    #   실측 지연 예산: 게시→수집 중앙 0.8분 + 본문추출 ~10초 + **픽업 0~60초** + 처리 12~30초.
    #   20초로 내리면 픽업 평균 30초 → 10초, 전체 중앙 ~2분 → ~1.3분.
    #   비용 검토: (a) 2차 Gemini 콜은 **불변** - 요약 대상이 희소(2.5%)해 이미 1건/콜이라
    #   주기가 아니라 요약 건수에 비례한다(~120콜/일). (b) 1차 gemma4 콜은 배치가 작아져 약 3배
    #   (~1,200 → ~3,600콜/일). [2026-10-01 정정] 한도는 RPD 1,440/키/모델(구 14,400 은 틀린 값) -
    #   6키 8,640 대비 약 40%(주력 26b · 폴백 31b 는 버킷이 따로라 실제 여유는 더 크다). (c) pending 조회는
    #   같은 커밋의 idx_posts_inserted_at + ORDER BY 교체로 1.3~2.1초 → 밀리초라, **주기를 3배로
    #   해도 이 잡의 총 CPU 부담은 오히려 줄어든다**(그 수정이 이 변경의 전제였다 - PATTERN-31).
    #   max_instances=1 + _running_flag 는 그대로라 처리가 20초를 넘으면 다음 틱은 스킵된다
    #   (자기 조절 - 바쁠 땐 처리 시간이 실효 주기가 되고 한가할 때만 20초로 촘촘해진다).
    scheduler.add_job(
        run_gemma4_summary_cycle, 'interval', seconds=_AI_SUMMARY_CYCLE_S,
        id='gemma4_summary_worker', replace_existing=True,
        max_instances=1, misfire_grace_time=60, coalesce=True,
    )
    # [v6.7.742] 디스코드 욕설·비방 판정 잡(`discord_judge`, 30초 · v6.7.462~) 삭제 - 디스코드 워처가 멈춰 입력이
    #   없다(사용자 결정 2026-10-24). 되살릴 일이 생기면 git show v6.7.741 시점 web_lib/bg/discord_judge.py.
    # [v6.7.665] 공식 홈페이지 게시물 제재 판정 (배그팀 요구 2026-09-15).
    #   주기 120초 근거: 공식 유저 게시판 유입이 하루 약 64건(30일 실측)이라 디코(하루 2만건)와
    #   자릿수가 다르다. 30초로 돌리면 대기 0건 사이클만 늘고, 콜 자체가 60~120초라 주기를
    #   줄여도 실효 지연을 못 줄인다. 대기 0건이면 콜 없이 return 한다.
    #   컷오프는 **최근 3일**이지 부팅 시각이 아니다 - 디코 판정의 부팅 컷오프를 그대로 쓰면
    #   배포 때마다 미판정분이 영구 유실된다(v6.7.488 에서 실측 3,500건).
    from web_lib.bg.post_judge import run_post_judge_cycle
    scheduler.add_job(
        run_post_judge_cycle, 'interval', seconds=_POST_JUDGE_CYCLE_S,
        id='post_judge', replace_existing=True,
        max_instances=1, misfire_grace_time=120, coalesce=True,
    )
    # [v6.7.629] 상황판 quiet checker(situation_quiet_checker, 5분) 은퇴 - 대시보드 카드가 v6.7.628 에서
    #   사라져 합성 스냅샷의 소비자가 0 이었다. bg/situation_aggregator.py 삭제와 함께 제거.
    # [v6.6.166] visitor_status 메모리 정리 (10분 주기)
    from web_lib.bg_tasks import cleanup_visitor_status
    scheduler.add_job(cleanup_visitor_status, 'interval', minutes=10,
                      id='visitor_status_cleanup', replace_existing=True)
    # [v6.7.487] visitor_log 배치 flush (20초 주기) — 요청 경로의 DB write 를 여기로 옮겼다.
    #   근거·유실 규약은 web_lib/bg/visitor_log.py docstring (PATTERN-52 대응 4).
    from web_lib.bg.visitor_log import flush_visitor_logs
    scheduler.add_job(
        flush_visitor_logs, 'interval', seconds=20,
        id='visitor_log_flush', replace_existing=True,
        max_instances=1, misfire_grace_time=15, coalesce=True,
    )
    # [v6.6.214] 웹앱 원격 킬 스위치 (2분 주기)
    scheduler.add_job(
        check_remote_kill_web, 'interval', minutes=2,
        id='remote_kill_web_check', replace_existing=True, max_instances=1,
    )
    # [v6.7.70] F-7 시스템 건강도 sample (1분 주기) — system_health 테이블 누적.
    from web_lib.bg.health_sampler import sample_health_metrics
    scheduler.add_job(
        sample_health_metrics, 'interval', minutes=1,
        id='health_sampler', replace_existing=True,
        max_instances=1, misfire_grace_time=30, coalesce=True,
    )
    # [v6.7.209 B2] status_update(health strip) 주기 송출 — 기존엔 이벤트 시에만 발화해 strip 이 멈춤.
    #   crawler lag/LED 신선값 유지(메인 크롤러 정지 적시 인지). 부팅게이트 이후 등록이라 안전, emit_status_update 자체 try/except 보유.
    from web_lib.bg.broadcast import emit_status_update
    scheduler.add_job(
        emit_status_update, 'interval', seconds=30,
        id='status_update_emit', replace_existing=True,
        max_instances=1, misfire_grace_time=15, coalesce=True,
    )
    # [v6.7.744] 시트 오버레이 릴레이 잡(`alert_overlay_sync`, 2분 · v6.7.353~) 삭제 - Config_alert 탭을 읽어 워처
    #   (imt-poe/imt-ncw) 감시 대상 JSON 을 볼륨에 쓰던 잡인데, 그 JSON 을 읽는 쪽이 워처 둘뿐이었고 워처 앱을 지웠다
    #   (사용자 2026-10-24 워처 종료). 되살릴 일이 생기면 git show cb0080cd:web_app/web_lib/bg/alert_overlay_sync.py.
    # [v6.7.354] imt_db_data 볼륨 주간 백업 (대상 = bg/volume_backup.py `_TARGETS` → host bind /app/backups).
    #   6h 체크 + 내부 "7일 경과 시에만 실행" due 판정 — misfire/재부팅 catch-up 자연 흡수.
    #   down -v·볼륨 삭제 유실 방어 (레포 백로그 2026-07-16). 상세 bg/volume_backup.py docstring.
    from web_lib.bg.volume_backup import run_volume_backup_cycle
    scheduler.add_job(
        run_volume_backup_cycle, 'interval', hours=6,
        id='volume_backup_weekly', replace_existing=True,
        max_instances=1, misfire_grace_time=3600, coalesce=True,
    )
    # [v6.7.360] PWA 푸시 감시 (CCU 급감 red 전이 + 워처 공지 diff → Web Push).
    #   구독 기기 0 이면 완전 no-op(시트 read 0 - 쿼터 무부담). AI 요약 푸시는 잡이 아니라
    #   gemma4 워커 저장 지점 piggyback. 상세 bg/web_push.py docstring.
    # [v6.7.564] 120초 → 30초. **워처 알림이 늦는 원인이 워처가 아니라 이 잡이었다** -
    #   ncw/poe 는 40~70초마다 폴링해 감지 즉시 카카오워크로 쏘는데, IME 종·폰 웹푸시는
    #   이 잡이 워처 DB 를 diff 할 때까지 기다린다. 보관소 실측(2026-08-17, n=7):
    #   감지 → 적재가 +27 ~ +118초(평균 75초)로 `[0,120]` 균등분포 그대로였고, 같은 공지에서
    #   **AI 요약 알림이 워처 공지 알림보다 76초 먼저** 도착했다(원천이 더 빠른 쪽이 화면엔 늦게).
    #   30초로 내리면 평균 75초 → 15초. 비용이 붙지 않는 이유는 **비싼 감시만 분리**했기 때문 -
    #   워처 diff 는 로컬 sqlite 2건 `LIMIT 5` 읽기고, 시트를 읽는 CCU 는 web_push.py 안에서
    #   `_CCU_MIN_INTERVAL_S=120` 으로 스로틀해 쿼터가 그대로다(하루 720 read 유지).
    #   부수 효과: 그 `LIMIT 5` 는 사이클 사이에 6건 이상 들어오면 오래된 것을 놓치는데,
    #   창이 1/4 로 좁아져 그 위험도 함께 줄었다.
    #   근거: _workspace/2026-08-17_ime_latency_budget.md §3.
    # [v6.7.741] 워처 공지 diff 삭제(워처 종료) - 이 잡에는 CCU 급감 감시만 남았다(그라파나 은닉 중엔 no-op).
    #   잡 id 는 그대로 둔다 - 부팅 마커(잡 id 목록)와 tests/bootstrap_jobs_snapshot.json 이 이 이름을 본다.
    from web_lib.bg.web_push import run_push_watch_cycle
    scheduler.add_job(
        run_push_watch_cycle, 'interval', seconds=30,
        id='web_push_watch', replace_existing=True,
        max_instances=1, misfire_grace_time=60, coalesce=True,
    )
    # [v6.7.438] 평시 베이스라인 이상감지 - 매시 05분에 직전 완결 버킷 집계 + 판정.
    #   05분인 이유: 정각 직후엔 직전 시간 글이 아직 다 수집되지 않아 과소평가된다.
    #   최초 1회는 hourly_stats 백필(120일, 실측 수 초)을 겸한다. 룰/임계치 근거는
    #   bg/anomaly.py docstring + tools/anomaly_backtest.py (백테스트 하루 0.66건).
    from web_lib.bg.anomaly import run_anomaly_cycle
    scheduler.add_job(
        run_anomaly_cycle, 'cron', minute=5,
        id='anomaly_watch', replace_existing=True,
        max_instances=1, misfire_grace_time=600, coalesce=True,
    )
    # [v6.7.440] 부팅 후 1회 즉시 실행 (일회성 date 잡 - 위 13개 상시 잡 카운트에 미포함).
    #   cron(매시 05분)만 두면 배포 직후 최대 59분간 화면이 '집계 대기 중'이라 방금 배포한 기능이
    #   동작하는지 확인할 수가 없다(첫 배포 때 실측된 마찰). 사이클 자체가 멱등이라
    #   (집계=upsert, 판정=INSERT OR IGNORE) 재시작마다 돌아도 중복 알림이 없다.
    # [v6.7.441] 90초 → 300초. 90초는 **부팅 storm 한복판**이었다 - 실측(01:53 부팅) 결과 그 창에서
    #   `database is locked` 4건(ExcludeBoards 삭제/visitor_log upsert x2/health 샘플)이 났고,
    #   그중 하나가 이 잡 시작 시각(01:55:03)과 겹쳤다. 크롤러 스레드 + 시트 hydrate + Selenium 이
    #   동시에 SQLite 를 두드리는 구간(PATTERN-31)에 쓰기를 하나 더 얹을 이유가 없다.
    #   확인이 1분 반 → 5분으로 늦어질 뿐, 최대 59분 대기보다는 여전히 압도적으로 낫다.
    from datetime import datetime as _dt_now
    scheduler.add_job(
        run_anomaly_cycle, 'date',
        run_date=_dt_now.now() + timedelta(seconds=300),
        id='anomaly_bootstrap_run', replace_existing=True,
        max_instances=1, misfire_grace_time=600,
    )
    # [v6.7.627] 보관 삭제 - 기본 OFF (IMT_RETENTION_DAYS 미설정/0 이면 사이클이 로그 1줄 남기고 끝).
    #   켜져 있으면 6시간마다 rowid 2,000개씩 조각 삭제 + 부팅 15분 뒤 1회 선행(배포로 켠 값이
    #   6시간을 안 기다리게. 부팅 폭풍 5분을 피한 자리 - PATTERN-31/52). 왜 purge_old_data 를 안 부르는지,
    #   대상 밖 테이블은 bg/retention.py docstring.
    from web_lib.bg.retention import run_retention_cycle
    scheduler.add_job(
        run_retention_cycle, 'interval', hours=6,
        id='retention_purge', replace_existing=True,
        max_instances=1, misfire_grace_time=3600, coalesce=True,
    )
    scheduler.add_job(
        run_retention_cycle, 'date',
        run_date=_dt_now.now() + timedelta(seconds=900),
        id='retention_bootstrap_run', replace_existing=True,
        max_instances=1, misfire_grace_time=1800,
    )
    # [v6.7.473] 종합 동향 리포트 - 근무 교대 시각에 직전 12시간의 AI 요약을 병합해 카카오 발송.
    #   시각은 **코드 상수 고정**(SLOTS, 사용자 결정) - 시트 REPORT_SCHEDULE_TIMES 는 은퇴한 AI
    #   프로세서의 잔재이며 이 잡은 그 키를 읽지 않는다. 설계·구간 기준은 bg/trend_digest.py docstring.
    #   `timezone` 명시 이유: compose 의 TZ=Asia/Seoul 에 의존하면 그 환경변수를 건드리는 순간
    #   발송 시각이 조용히 어긋난다(잡은 그대로 도니 증상이 '시간만 이상함'으로만 보인다).
    #   `args=[slot]` 로 넘기는 이유: 루프 변수를 lambda 로 캡처하면 마지막 값(21:30)이 두 잡에
    #   모두 박힌다(late binding) - APScheduler 가 args 를 지원하므로 클로저를 만들지 않는다.
    #   misfire 1800s: 배포·재시작이 발송 시각과 겹쳐도 30분 내면 따라잡는다. 중복은 잡이 아니라
    #   내부 슬롯 멱등 가드(system_settings)가 막는다.
    from web_lib.bg.trend_digest import run_trend_digest, SLOTS as _TREND_SLOTS
    for _slot in _TREND_SLOTS:
        _th, _tm = (int(_x) for _x in _slot.split(':'))
        scheduler.add_job(
            run_trend_digest, 'cron', hour=_th, minute=_tm, args=[_slot],
            id=f"trend_digest_{_slot.replace(':', '')}", replace_existing=True,
            max_instances=1, misfire_grace_time=1800, coalesce=True,
            timezone='Asia/Seoul',
        )
    # [v6.7.78] v6.7.74 의 articles_memory_refresh 스케줄러 등록 일시 제거.
    # 사유: load_existing_data 가 crawler.lock 안에서 pd.to_datetime × N posts 를 수행 →
    # 2.5~5s lock hold × 2분 주기 → gunicorn 1w×8t 풀 saturation → LCP 60s.
    # 함수 자체 (refresh_articles_memory_from_db) 는 web_lib/bg/scheduler.py 에 보존.
    # 추후 pd.to_datetime 을 lock 밖으로 분리하는 batching 수정 후 재활성 예정.
    # 그 동안 메모리 stale 문제는 v6.7.74 이전 운영 방식 (재시작 시 SQLite preload) 으로 회귀.
    # [v6.7.730] 위 '보존' 함수를 삭제했다 - 재활성 전제가 사라졌다. 이 함수의 존재 이유는 데몬(app_terminal.py)이
    #   별도 프로세스로 SQLite 에 쓰는 글을 웹앱 메모리로 끌어오는 것이었는데, 터미널앱은 2026-07-05 은퇴했고
    #   지금은 메인 크롤러가 이 프로세스 안에서 돈다. 되살릴 일이 생기면 git show v6.7.729 시점 bg/scheduler.py.


def _load_notification_toggles():
    """카카오워크 알림 토글을 SQLite 에서 메모리로 로드한다(실패해도 부팅은 계속)."""
    # [v6.6.343] 카카오워크 알림 토글을 SQLite 에서 메모리로 로드 (관리자 UI 와 동기)
    try:
        from sqlite_handler import db_handler as _db
        _saved_toggles = _db.get_notification_toggles()
        if _saved_toggles:
            with gl.notification_toggles_lock:
                for key, enabled in _saved_toggles.items():
                    gl.notification_toggles[key] = bool(enabled)
            logger.info(f"[BOOTSTRAP] 카카오워크 알림 토글 {len(_saved_toggles)}건 로드 완료")
        else:
            logger.info("[BOOTSTRAP] 저장된 알림 토글 없음 — 전체 ON(기본값) 유지")

        # [v6.6.492] keyword_alert 는 웹앱 기본 OFF (터미널앱과 알림 중복 방지). SQLite 미저장 시 1회 시드.
        # [v6.6.521] OFFICIAL(다음 카페 공지) 알림도 동일 keyword_alert 토글로 함께 제어 — 별도 시드 불필요.
        if 'keyword_alert' not in (_saved_toggles or {}):
            _db.set_notification_toggles_bulk({'keyword_alert': False})
            with gl.notification_toggles_lock:
                gl.notification_toggles['keyword_alert'] = False
            logger.info("[BOOTSTRAP] keyword_alert 미저장 — 웹앱 기본 OFF 시드 완료")
    except Exception as _toggle_err:
        logger.warning(f"[BOOTSTRAP] 알림 토글 로드 실패 (기본값 ON 유지): {_toggle_err}")


def _log_bootstrap_summary():
    """부팅 요약 로그. 아래 마지막 줄은 imt-deploy 배포 후 게이트가 읽는 마커라 절대 죽지 않아야 한다."""
    # [v6.7.461] 개수를 **스케줄러에게 물어서** 찍는다 - 손으로 쓴 상수를 없앤다.
    #   구 코드는 문자열 리터럴 "13개" 였고, 그 숫자가 CLAUDE.md 배포 섹션 + portfolio/07 md/html
    #   까지 4곳에 복제돼 있었다. 즉 잡을 하나 추가할 때마다 4곳을 손으로 맞춰야 하고, 한 곳만
    #   놓치면 배포 검증이 정상 배포를 리그레션으로(또는 그 반대로) 읽는다 - 실제로 v6.7.353/354/
    #   360/438 마다 이 동기화를 했다(=드리프트 벡터가 상시 열려 있었다는 뜻).
    #   개수를 세는 주체를 코드로 옮기면 그 벡터 자체가 소멸한다. **잡 id 목록을 같이 찍는 것이
    #   핵심** - 판정이 "13이 맞나"(산수)에서 "무엇이 빠졌나"(이름)로 바뀌어, 문서가 기대값을
    #   들고 있을 이유가 없어진다. 하나 빠졌을 때 어느 기능인지도 즉시 드러난다.
    #   이 줄은 imt-deploy 배포 후 게이트가 읽는 마커이므로(scripts/imt-deploy.sh) 절대 죽지 않게
    #   방어한다 - id 수집이 실패해도 마커 자체는 반드시 출력한다.
    # [v6.7.478] 파이프라인 **처리 상한**을 계산해 찍는다 (리팩토링 제안서 P1-2).
    #   상한 = 배치크기 / 사이클주기. 그런데 그 두 값이 **다른 파일에** 있다 - 주기는 여기,
    #   배치는 각 모듈(`BATCH_SIZE`·`BATCH_CAP`·본문 `batch_size`). 2026-08-03 디코 지연을
    #   진단할 때 상한을 알려면 **두 파일을 오가며 곱해야** 했고, 그 곱이 유입보다 낮다는
    #   사실이 문제의 전부였다. 코드가 대신 곱해서 한 줄로 보여주면 다음엔 로그만 보고 판단한다.
    #   (v6.7.461 이 잡 개수를 문서에서 코드로 옮긴 것과 같은 처방 - 사람이 계산을 맡으면 드리프트한다.)
    try:
        from ai_lib.gemma4_summary_core import BATCH_SIZE as _AI_BATCH, BATCH_PARALLEL as _AI_PAR
        from web_lib.bg.post_judge import BATCH_CAP as _PJ_BATCH
        # 주기는 add_job 인자와 같은 상수(_*_CYCLE_S)를 쓴다 - 한 곳만 바꿔도 로그가 따라간다.
        # [v6.7.742] dc_judge 줄 삭제 - 디코 판정 잡을 지웠다.
        _caps = [
            ("ai_summary", _AI_BATCH * _AI_PAR, _AI_SUMMARY_CYCLE_S),
            ("post_judge", _PJ_BATCH, _POST_JUDGE_CYCLE_S),   # [v6.7.665] 공식 게시물 제재 판정
            ("content", 20, 5),      # web_lib/bg/contents.py 스레드 인자 (batch_size / interval)
        ]
        #   **이론 상한이다** - 사이클이 주기 안에 끝난다는 가정이고, LLM 콜이 주기를 넘으면
        #   실효는 더 낮다(디코 배치25 = 이론 50건/분인데 콜 46초라 실효 32건/분). 그래도 유용하다:
        #   유입이 이 값을 넘으면 **확실히** 밀리고, 안 넘는데 밀리면 콜 소요가 범인이라는 뜻이다.
        logger.info("[BOOTSTRAP] 파이프라인 이론 상한(주기 기준, 콜이 주기 초과 시 실효는 더 낮음) - "
                    + " · ".join("%s %.0f건/분(배치%d/%ds)" % (n, b * 60.0 / p, b, p)
                                 for n, b, p in _caps))
    except Exception as _cap_err:
        logger.info(f"[BOOTSTRAP] 파이프라인 상한 계산 실패(무시): {_cap_err}")

    try:
        _jobs = scheduler.get_jobs()
        _job_ids = ", ".join(sorted(j.id for j in _jobs))
        logger.info(
            f"[BOOTSTRAP] 크롤러 스레드 + {len(_jobs)}개 스케줄러 잡 등록, 스케줄러 시작 완료 "
            f"(ids: {_job_ids})"
        )
    except Exception as _jobs_err:
        logger.info("[BOOTSTRAP] 크롤러 스레드 + 스케줄러 잡 등록, 스케줄러 시작 완료 "
                    f"(잡 목록 조회 실패: {_jobs_err})")


def _bootstrap_background_jobs():
    """크롤러 스레드 + 모든 스케줄러 잡 + scheduler.start()를 1회만 실행.
    Gunicorn import 또는 python app.py 실행 모두에서 호출되며, _done 플래그와
    scheduler.running 가드로 중복 시작 방지."""
    if getattr(_bootstrap_background_jobs, '_done', False):
        return
    _bootstrap_background_jobs._done = True

    # [v6.7.627] 워커 생존 신호 - 나머지 초기화보다 먼저, 아래 try 밖(다른 초기화가 실패해도 돈다).
    #   healthcheck.sh 가 HTTP 실패 시 이 파일의 신선도로 "밀림" 과 "죽음" 을 가른다 (bg/heartbeat.py).
    try:
        from web_lib.bg.heartbeat import start_heartbeat
        start_heartbeat()
    except Exception as _hb_err:
        logger.warning(f"[HEARTBEAT] 기동 실패 - healthcheck 는 HTTP 만으로 판정한다: {_hb_err}")

    try:
        # [v6.6.215] 기동 시점 KILL 체크 — 모듈 레벨에서는 sys.exit 대신 조용히 스킵
        try:
            from notification_manager import NotificationManager
            if NotificationManager().check_remote_kill('WEB'):
                logger.warning("[REMOTE_KILL] 기동 시점 KILL 감지 — 백그라운드 잡 등록 스킵")
                return
        except Exception as e:
            logger.error(f"[REMOTE_KILL] 기동 시 체크 오류(무시): {e}")

        # 메인 크롤러 + 본문 크롤러 자동 기동 (Phase 0~3)
        threading.Thread(target=start_crawler_with_delay, daemon=True).start()

        _register_scheduler_jobs()
        if not scheduler.running:
            scheduler.start()

        _load_notification_toggles()
        _log_bootstrap_summary()
    except Exception as e:
        logger.error(f"[BOOTSTRAP] 백그라운드 초기화 실패: {e}", exc_info=True)

# 모듈 레벨 호출: Gunicorn import 시점에 즉시 실행
# IMT_DISABLE_BOOTSTRAP=1 환경변수로 스킵 가능 (테스트·리팩토링 baseline 스냅샷·도구 스크립트용).
# 기본값(미지정) 은 기존 동작 유지 — Gunicorn/직접 실행 시 자동 부팅.
if os.environ.get('IMT_DISABLE_BOOTSTRAP', '').strip().lower() in ('1', 'true', 'yes'):
    logger.info("[BOOTSTRAP] IMT_DISABLE_BOOTSTRAP 감지 — 백그라운드 초기화 스킵 (도구 모드)")
else:
    _bootstrap_background_jobs()

if __name__ == '__main__':
    from utils.common import install_crash_hook
    install_crash_hook("웹앱")

    # python app.py 직접 실행 시에는 위 _bootstrap_background_jobs()가 이미 호출된 상태.
    # KILL 시 sys.exit으로 즉시 종료할 수 있도록 메인 스레드 전용 체크는 여기에 한 번 더.
    try:
        from notification_manager import NotificationManager
        if NotificationManager().check_remote_kill('WEB'):
            logger.warning("[REMOTE_KILL] 기동 시점 KILL 감지 — 직접 실행 모드에서 즉시 종료")
            sys.exit(0)
    except SystemExit:
        raise
    except Exception as e:
        logger.error(f"[REMOTE_KILL] 기동 시 체크 오류(무시): {e}")

    try:
        host = os.environ.get('HOST', '0.0.0.0')
        port = int(os.environ.get('PORT', 5008))
        print(f"Starting Flask server on {host}:{port}")
        # Eventlet/SocketIO 충돌을 피하고 안정적 로컬 접속을 위해 app.run 사용
        debug_mode = os.environ.get('FLASK_DEBUG', 'false').lower() in ('true', '1', 'yes')
        app.run(host=host, port=port, debug=debug_mode, use_reloader=False, threaded=True)
    except Exception as e:
        print(f"Server startup failed: {e}")

