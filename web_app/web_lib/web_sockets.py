# -*- coding: utf-8 -*-
"""
웹 애플리케이션 웹소켓 핸들러 (web_sockets.py)
클라이언트의 실시간 양방향 통신(연결/해제 및 초기 데이터 전송)을 전담합니다.
"""
import logging
import sys
import time
from web_lib.globals import socketio
from flask import request, session
from flask_socketio import emit, join_room
from datetime import datetime
import web_lib.globals as gl

def _get_build_version():
    """[v6.6.320] app.py 의 BUILD_VERSION 을 안전하게 조회.
    순환 import 유발하지 않도록 sys.modules 에서 직접 가져오며, 없으면 '0' 반환.
    """
    _app_mod = sys.modules.get('app') or sys.modules.get('__main__')
    return str(getattr(_app_mod, 'BUILD_VERSION', '0'))
from web_lib.bg_tasks import (
    get_crawler, build_status_payloads,
    _ensure_odin_global_status_populated, _ensure_account_trade_status_populated,
    _ensure_jasa_games_status_populated, _ensure_tasa_games_status_populated
)
from common import get_yesterday_str
from zoneinfo import ZoneInfo

# [추가] 한국 표준시 존 정의
KST = ZoneInfo("Asia/Seoul")

# [v6.7.290] admin_heartbeat 발 SQLite visitor_log.last_seen 갱신 throttle(초). heartbeat 는 30초 주기지만
#   현재 접속 중 로그는 1~2분 주기 갱신이면 충분 — 90초로 write 증폭·visit_count 왜곡 억제(사용자 요청 "1~2분마다").
_DB_TOUCH_INTERVAL = 90

logger = logging.getLogger(__name__)

def _send_initial_payload(sid, is_admin=False):
    """[v6.6.319] 초기 데이터 전송을 gunicorn 워커가 아닌 background thread 에서 처리.

    `handle_connect()` 가 워커 쓰레드를 수 초간 점유하면 gunicorn(--threads 8) 풀이 고갈돼
    같은 시점에 들어오는 `/api/status` 등 HTTP 요청이 프록시 타임아웃(60s) 까지 대기 → 504.
    이 함수를 `socketio.start_background_task()` 로 호출하면 워커는 즉시 해제되고
    실제 emit 은 별도 스레드에서 진행된다. 각 emit 은 `to=sid` 로 연결된 클라이언트에만 전송.
    """
    try:
        # [v6.6.320] 가장 먼저 서버 BUILD_VERSION 전송 — 재연결 시 구버전 HTML 사용자 자동 reload.
        socketio.emit('server_version', {'build_version': _get_build_version()}, to=sid)

        crawler = get_crawler(blocking=False)
        if crawler is None:
            return

        # 캐시 히트 시에만 _ensure_* 호출 (동시 접속 시 Google Sheets 블로킹 방지)
        yesterday = get_yesterday_str()
        for ensure_fn, status_dict in [
            (_ensure_odin_global_status_populated, gl.odin_global_status),
            (_ensure_account_trade_status_populated, gl.account_trade_status),
            (_ensure_jasa_games_status_populated, gl.jasa_games_status),
            (_ensure_tasa_games_status_populated, gl.tasa_games_status),
        ]:
            if status_dict.get('summary') and status_dict.get('gsheet_checked_date') == yesterday:
                continue
            ensure_fn()

        # [v6.6.321] 초기 로그 전송량 축소 (500 → 100). 로그 탭을 실제로 열면 `/api/logs?limit=10000`
        # 으로 풀 로드하므로 일반 사용자에겐 100개로 충분. JSON 직렬화 + 네트워크 전송 + 메인 스레드
        # 점유 시간을 줄여 초기 연결 직후 다른 emit 이 빠르게 전달되도록 한다.
        # [v6.7.731] 운영 로그는 admin 만 (/api/logs admin 가드와 정합) - 익명·일반 세션엔 초기 로그 미전송.
        if is_admin:
            logs = crawler.get_logs(limit=100)
            socketio.emit('logs_update', {'logs': logs, 'count': len(logs)}, to=sid)
        socketio.emit('toast', {'message': '데이터 가져오는 중...'}, to=sid)

        # [v6.7.432] 초기 raw_articles_update emit 제거 — 로딩 순단(freeze)의 진원.
        #   과거: get_all_latest_articles()(무제한) → _enrich → ~1.22MB JSON 을 connect 직후 emit.
        #   이 payload 는 어떤 뷰에서도 고유 소비처가 없다:
        #     - 기본 뷰(feed/comments/dashboard): feed 는 payload 무시 후 자체 /api/articles/all 재fetch
        #       (socket.js raw_articles_update 핸들러), comments/dashboard 는 미소비.
        #     - total/games/community: 활성화 시 loadPaneData → loadXxx() 가 self-fetch(actions.js).
        #     - boot.js(v6.7.100)가 로딩 즉시 /articles/all 를 Promise.all 로 선발사해 피드를 hydrate.
        #   즉 무제한 리스트 순회 + 1.22MB 직렬화(서버 GIL, HTTP 워커 latency 폭증 — v6.7.75 와 동일
        #   메커니즘) + 클라 메인스레드 JSON 파싱(첫 paint 경합)을 로딩 순간에 때린 뒤 폐기했다.
        #   v6.7.75 는 가동모드 2분 주기 emit 만 제거하고 이 connect 초기 emit 을 남긴 미완결분.
        #   뷰어모드 주기 갱신(scheduler.py refresh_main_viewer_data)의 emit 은 존치(유일 갱신 경로).
        socketio.emit('status_update', build_status_payloads(), to=sid)

        # 댓글 초기 전송
        # [v6.6.279] 스냅샷 복사
        with gl._comment_lock:
            cached_comments = list(gl.comment_data_status.get('latest_results', []))
        if cached_comments:
            socketio.emit('comments_update', cached_comments, to=sid)
    except Exception as e:
        logger.error(f"WebSocket 초기 데이터 전송 실패: {e}")

@socketio.on('connect')
def handle_connect():
    """클라이언트 연결 시 초기 데이터 전송.

    [v6.6.319] 실제 전송 로직은 `_send_initial_payload()` 를 `start_background_task()` 로
    호출해 별도 스레드에서 처리. 핸들러 자체는 즉시 리턴하여 gunicorn --threads 풀을
    점유하지 않는다 (동시 접속 시 /api/status 504 방지).
    크롤러 초기화 중이면 캐시 데이터만 즉시 선전송하고 종료.
    """
    # [v6.7.733] access_lock ON + admin/manager 아님 -> 연결 거부. **어떤 emit 보다 먼저** 둔다 - 종전엔
    #   익명 소켓이 server_version·status_update·comments_update 를 받고 room 없는 broadcast 도 계속 받았다.
    #   판정은 app.py /api 게이트와 같은 원천(_access_lock_on). 판정 자체가 터지면 거부한다(보안 경계).
    try:
        from web_lib.routes._helpers import _session_role, _access_lock_on
        if _session_role() not in ('admin', 'manager') and _access_lock_on():
            return False
    except Exception as e:
        logger.error(f"WebSocket connect 게이트 판정 실패 - 거부: {e}")
        return False
    logger.info("WebSocket 클라이언트 연결")
    try:
        # [v6.6.320] 크롤러 준비 여부와 무관하게 버전부터 전달 — 구버전 HTML 사용자는 이 시점에 reload.
        emit('server_version', {'build_version': _get_build_version()})

        # [v6.7.731] admin 세션만 'admin' room 에 가입 - 운영 로그(logs_update)는 이 room 으로만 간다.
        #   판정 기준은 /api/logs 가드와 동일한 session['is_admin'] (role 파생). anon/일반은 미가입.
        is_admin = bool(session.get('is_admin', False))
        if is_admin:
            join_room('admin')

        crawler = get_crawler(blocking=False)
        if crawler is None:
            # 크롤러 초기화 중이라도 SQLite 캐시에서 선로드된 데이터가 있으면 전송
            logger.info("크롤러 초기화 중 — 캐시 데이터 선전송")
            # [v6.6.279] 스냅샷 복사 (순회 중 mutate 방지)
            with gl._comment_lock:
                cached_comments = list(gl.comment_data_status.get('latest_results', []))
            if cached_comments:
                emit('comments_update', cached_comments)
            emit('toast', {'message': '서버 초기화 중입니다. 잠시 후 데이터가 자동으로 갱신됩니다.'})
            return

        # 무거운 초기 payload 전송을 background 로 offload — 워커 즉시 해제
        sid = request.sid
        socketio.start_background_task(_send_initial_payload, sid, is_admin)
    except Exception as e:
        logger.error(f"WebSocket connect 처리 실패: {e}")

@socketio.on('disconnect')
def handle_disconnect():
    logger.info("WebSocket 클라이언트 연결 해제")

@socketio.on('admin_heartbeat')
def handle_admin_heartbeat():
    """관리자용 실시간 접속 상태 갱신"""
    # [v6.7.733] 락 ON 이면 admin/manager 만 - 종전 무가드라 아무 소켓이나 visitor_status 를 쓰고 DB touch 를 일으켰다.
    #   락 OFF 는 종전대로 익명 방문자도 '현재 접속중' 에 잡힌다(connect 와 같은 조건).
    from web_lib.routes._helpers import _session_role, _access_lock_on
    if _session_role() not in ('admin', 'manager') and _access_lock_on():
        return
    # [프록시 대응]
    ip_addr = gl.get_client_ip(request)
    # [v6.7.287] 브라우저별 토큰으로 현재 접속중 식별 - 같은 공유 IP(NAT) 뒤 다수 PC 구분.
    visitor_id = gl.peek_visitor_id(request)
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # [자동 기기명] 수동 등록된 이름이 없으면 User-Agent에서 기기 종류를 추출
    user_agent_str = request.user_agent.string
    now_mono = time.monotonic()
    _do_db_touch = False
    # [v6.6.258] Lock 으로 visitor_names 읽기 + visitor_status 쓰기 보호
    with gl._visit_lock:
        current_name = gl.visitor_names.get(visitor_id)
        if not current_name:
            current_name = gl.get_device_info(user_agent_str)
        gl.visitor_status[visitor_id] = {
            'last_active': now_str,
            'name': current_name,
            'ua': user_agent_str,
            'ip': ip_addr
        }
        log_count = len(gl.admin_visit_logs)
        # [v6.7.290] 현재 접속 중 세션의 SQLite last_seen 을 1~2분 주기로 갱신(첫 접속은 index 라우트가 이미 기록).
        #   heartbeat 는 30초마다 오지만 DB write 는 _DB_TOUCH_INTERVAL 로 throttle → write 증폭/visit_count 왜곡 방지.
        if now_mono - gl.visitor_db_touch.get(visitor_id, 0) >= _DB_TOUCH_INTERVAL:
            gl.visitor_db_touch[visitor_id] = now_mono
            _do_db_touch = True
    # emit 은 Lock 밖
    socketio.emit('admin_visit_update', {'count': log_count})
    # [v6.7.290] SQLite last_seen 갱신도 Lock 밖(스레드-로컬 conn + WAL). KST 로 기록 — upsert 의 first_seen/last_seen 규약과 정합.
    if _do_db_touch:
        try:
            kst_now = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
            from sqlite_handler import db_handler as _dbh
            _dbh.touch_visitor_log(visitor_id, ip_addr, kst_now)
        except Exception:
            pass