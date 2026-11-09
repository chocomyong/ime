# -*- coding: utf-8 -*-
"""bg.crawler_singleton — CrawlerManager 싱글톤 관리.

- `_kill_zombie_chrome()` : 비활성화된 스텁 (호출 즉시 반환). v6.6.346 이전 좀비 정리 기능.
- `get_crawler(blocking=True)` : `gl._crawler_manager` 싱글톤 접근자 + 지연 초기화
- `init_crawler_async()` : 첫 주기 시작 + CRAWLER_EXECUTIONS['main'] 등록

싱글톤 잠금은 `gl._init_lock` 이 담당. 본 모듈은 순수 함수만 보유.

의존 관계:
- `web_lib.globals` (gl)
- `crawlers.crawler.CrawlerManager` (지연 import — 초기화 시점에만)
- `emit_status_update` / `main_crawler_export_callback` — 함수 내부 lazy import (순환 회피)
"""
import web_lib.globals as gl


def _kill_zombie_chrome():
    """[v6.6.346] 비활성화된 스텁 — 호출 즉시 반환.

    이전 동작: `pkill -f chromedriver` + `pkill -f 'chrome.*headless'` 로
    잔류 Chrome 프로세스를 일괄 종료해 NAS 메모리 확보.

    문제: **살아있는** 댓글/본문 크롤러 chromedriver 프로세스까지 함께 죽여버려,
    서브 크롤러 "데이터 일괄 추출" 시작 직후 댓글 크롤러가
    `Connection refused (chromedriver port)` 로 전 타겟 실패하는 증상 발생.
    (예: 2026-04-22 로그 — 00:06:54 BATCH 시작 → 동 시각 COMMENTS 우마무스메/프렌즈 타운 실패)

    사용자 정책 "서브 크롤러 실행 시 메인/댓글/본문은 중단하지 말 것"과 정면 충돌하므로
    호출처(`_trigger_batch_run` 시작부 + 재시도 경로)에서 남겨둔 호출들도 이 스텁을 만나
    아무 동작도 하지 않게 된다.

    좀비 정리가 정말 필요해지면 살아있는 driver PID 를 추적해 그것만 제외하고 죽이는
    방식으로 재설계 필요. 현재는 컨테이너 재시작 시 clean 상태로 돌아가고,
    v6.6.341 self-healing 루프가 누적 좀비로 인한 실패를 자동 복구하므로 영향 제한적.
    """
    return


def get_crawler(blocking=True):
    """CrawlerManager 인스턴스를 반환합니다.

    Args:
        blocking: True(기본값)이면 초기화 완료까지 락 대기.
                  False이면 초기화 중일 때 None을 즉시 반환하여 스레드 풀 고갈을 방지.
    """
    # 이미 초기화 완료된 경우 즉시 반환
    if gl._crawler_manager is not None and gl._crawler_manager != "INITIALIZING":
        return gl._crawler_manager

    # [비차단 경로] 초기화 중이면 None 즉시 반환 (WebSocket 연결 등에서 스레드 풀 고갈 방지)
    if not blocking and gl._crawler_manager == "INITIALIZING":
        return None

    # [차단 경로] 락을 사용하여 초기화 중 중복 생성을 방지하고 재귀 호출 안전성 확보
    with gl._init_lock:
        if gl._crawler_manager is None:
            # 초기화 중임을 표시하기 위해 임시 할당 (재귀 방지)
            gl._crawler_manager = "INITIALIZING"
            try:
                # 순환 회피 — 초기화 시점에만 bg 서브 모듈 참조
                from crawlers.crawler import CrawlerManager
                from web_lib.bg.broadcast import emit_status_update
                from web_lib.bg.export import main_crawler_export_callback
                gl._crawler_manager = CrawlerManager(
                    socketio=gl.socketio, status_emitter=emit_status_update,
                    executor=gl.main_crawler_executor, data_export_callback=main_crawler_export_callback,
                    app_name="W", mode_name="Web Dashboard"
                )
            except Exception as e:
                gl._crawler_manager = None
                print(f"[ERROR] CrawlerManager 초기화 실패: {e}")
                raise
    return gl._crawler_manager


def init_crawler_async():
    with gl._init_lock:
        if gl._crawler_started:
            return
        get_crawler().start()
        gl.CRAWLER_EXECUTIONS['main'] = {
            'execution': get_crawler().crawler_thread,
            'info': {'game': 'Community', 'server': 'All', 'worker': 'MainThread', 'type': 'monitoring'}
        }
        gl._crawler_started = True
