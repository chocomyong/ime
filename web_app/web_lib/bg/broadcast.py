# -*- coding: utf-8 -*-
"""bg.broadcast — 상태 송출 & 클라이언트 상태 업데이트.

- `build_status_payloads()` : 서브 크롤러 4종 + 메인 + 본문/댓글 상태를 dict 로 조립
- `emit_status_update()`    : WebSocket `status_update` 이벤트 발송
- `update_client_status()`  : Config_Clients 시트에 대시보드 접속 시각 기록

모듈 의존:
- `web_lib.globals`            (gl.socketio + 서브 크롤러 status dict)
- `common.format_iso_to_24h`  (시간 포맷)
- `common.CONFIG_SHEET_URL`   (Config_Clients 시트 URL)
- `google_sheets_handler.get_handler`
- `web_lib.bg_tasks.get_crawler`                 (lazy — 순환 회피)
- `web_lib.bg_tasks._contents_crawler_running`   (lazy — 댓글 크롤러 실행 상태)
"""
import socket
from copy import copy
from datetime import datetime as dt_class

import web_lib.globals as gl
from common import format_iso_to_24h, CONFIG_SHEET_URL
from google_sheets_handler import get_handler


# [v6.6.524] 헬스 스트립용 lag 계산 — gl.* 원본 dict 의 ISO timestamp 직접 사용.
# build_status_payloads 가 last_run_finish 를 'HH:MM' 으로 포맷하기 전 raw 값으로 계산.
# [v6.7.629] _calc_lag_minutes / _grade_lag / _build_health_summary 삭제 - status_update 의 health(크롤러 7종 LED)
#   소비자였던 대시보드 헬스 스트립이 v6.7.628 에서 사라졌다. 크롤러 상태는 관리자 탭 crawlers-overview 가 맡는다.


def build_status_payloads():
    """상태 업데이트 페이로드를 생성하여 반환합니다. (emit 없이 데이터만 구성)"""
    # 순환 회피 — 싱글톤/상태 플래그는 호출 시점에 해당 소유 모듈에서 직접 조회
    from web_lib.bg.crawler_singleton import get_crawler
    from web_lib.bg import contents as _cts

    status_copy = copy(gl.odin_global_status)
    status_copy['latest_results'] = list(status_copy.get('latest_results', []))
    status_copy['last_run_start'] = format_iso_to_24h(status_copy.get('last_run_start'))
    status_copy['last_run_finish'] = format_iso_to_24h(status_copy.get('last_run_finish'))

    account_trade_status_copy = copy(gl.account_trade_status)
    account_trade_status_copy['latest_results'] = list(account_trade_status_copy.get('latest_results', []))
    account_trade_status_copy['last_run_start'] = format_iso_to_24h(account_trade_status_copy.get('last_run_start'))
    account_trade_status_copy['last_run_finish'] = format_iso_to_24h(account_trade_status_copy.get('last_run_finish'))

    jasa_status_copy = copy(gl.jasa_games_status)
    jasa_status_copy['latest_results'] = list(jasa_status_copy.get('latest_results', []))
    jasa_status_copy['last_run_start'] = format_iso_to_24h(jasa_status_copy.get('last_run_start'))
    jasa_status_copy['last_run_finish'] = format_iso_to_24h(jasa_status_copy.get('last_run_finish'))

    tasa_status_copy = copy(gl.tasa_games_status)
    tasa_status_copy['latest_results'] = list(tasa_status_copy.get('latest_results', []))
    tasa_status_copy['last_run_start'] = format_iso_to_24h(tasa_status_copy.get('last_run_start'))
    tasa_status_copy['last_run_finish'] = format_iso_to_24h(tasa_status_copy.get('last_run_finish'))

    payloads = {}
    for name, original_dict in [('odin_global_crawler', status_copy), ('account_trade_crawler', account_trade_status_copy), ('jasa_games_crawler', jasa_status_copy), ('tasa_games_crawler', tasa_status_copy)]:
        payload = original_dict.copy()
        if 'latest_results' in payload:
            payload['results_count'] = len(payload['latest_results'])
        payloads[name] = payload

    crawler = get_crawler()
    payloads['main_crawler'] = {
        'is_running': crawler.is_running,
        'status_message': crawler.get_status(),
        **crawler.get_next_crawl_times()
    }

    # [v6.6.284] 본문 크롤러 / 댓글 크롤러 분리 — 둘은 완전 별개 (상시 스레드 vs 주기 잡).
    # 이전 v6.6.278 에선 하나의 content_extractor 필드로 묶어서 표시했으나,
    # 구조상 분리가 맞고 UI(헤더 pill + 관리자 탭 종합 현황)에서도 별도 행으로 표시 필요.
    ce_thread = getattr(gl, '_content_extractor_thread', None)
    ce_alive = bool(ce_thread and ce_thread.is_alive())
    payloads['content_extractor'] = {
        'alive': ce_alive,  # 본문 크롤러 (상시 스레드 생존)
        'running': ce_alive,
        'status_message': '정상' if ce_alive else '오류',
    }
    payloads['comments_crawler'] = {
        'running': bool(_cts._contents_crawler_running),  # 댓글 크롤러 (주기 실행 중인지)
        'status_message': '실행 중' if _cts._contents_crawler_running else '대기',
    }

    # [v6.7.629] payloads['health'] 제거 (위 주석 참조).

    return payloads


def emit_status_update():
    from web_lib.bg.crawler_singleton import get_crawler
    try:
        payloads = build_status_payloads()
        gl.socketio.emit('status_update', payloads)
    except Exception as e:
        get_crawler()._log(f"WebSocket 상태 업데이트 실패: {e}", 'error', 'WEBSOCKET')


def update_client_status():
    try:
        handler = get_handler(log_func=lambda m, l, t: None)
        if not handler.gc:
            return
        ws = handler.gc.open_by_url(CONFIG_SHEET_URL).worksheet("Config_Clients")
        machine_id, status, now_str = socket.gethostname(), "Web Dashboard", dt_class.now().strftime('%Y-%m-%d %H:%M:%S')
        rows = ws.get_all_values()
        # [v6.7.277 FIX] off-by-one: enumerate(rows[1:]) 는 헤더를 슬라이스로 건너뛰므로
        # 1-based 시트 행번호 = idx (start=2). 기존 i+1 은 매치된 행의 '한 칸 위'(=헤더/이웃 머신)를
        # 덮어써서 헤더 손상 + 타 머신 status 교차오염 + 중복행 폭증을 유발했음.
        row_idx = next((idx for idx, r in enumerate(rows[1:], start=2) if r and str(r[0]) == machine_id), -1)
        if row_idx != -1:
            ws.update(range_name=f"B{row_idx}:C{row_idx}", values=[[status, now_str]])
        else:
            ws.append_row([machine_id, status, now_str])
    except Exception:
        pass  # [로그 다이어트]
