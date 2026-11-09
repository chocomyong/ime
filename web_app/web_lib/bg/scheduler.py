# -*- coding: utf-8 -*-
"""bg.scheduler — 주기 작업·메인 크롤러 헬스체크·부트스트랩.

부트스트랩:
- `start_crawler_with_delay()` : 기동 시 SQLite 선로드 + DataManager 초기화 +
  댓글/본문 크롤러 기동 + 메인 크롤러 시작

주기 작업 (APScheduler):
- `refresh_main_viewer_data()`          : 뷰어 모드 주기 리프레시 (is_running=False 분기)
- `cleanup_visitor_status()`            : 30분 이상 비활성 방문 기록 정리
- `check_main_crawler_health()`         : 메인 크롤러 스레드 헬스체크
- `_restart_main_crawler()`             : 지연 재시작 콜백

모듈 상태:
- `_main_crawler_restart_count` : 메인 크롤러 연속 재시작 횟수 (본 모듈 소유)
"""
import time
import logging
from datetime import datetime as dt_class, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

import web_lib.globals as gl
from common import (
    CONTENTS_SHEET_URL,
    get_yesterday_str,
    normalize_timestamp,
)
from google_sheets_handler import get_handler
from contents_crawler import DataManagerAndUploader

logger = logging.getLogger(__name__)

_main_crawler_restart_count = 0
# [v6.7.288/289] hang 판정 base 임계값(초). last_successful_crawl_ts(타임아웃 없이 반환한 마지막
# 크롤)가 이보다 오래 안 갱신되면 thread.is_alive()=True 여도 hang 으로 판단. DAUM/DC 는 정상
# 시 70~120s 주기로 성공하므로 600s = 약 5회 연속 실패분 → 정상 지연과 명확히 구분.
# (v6.7.288 은 last_loop_heartbeat=루프 반복 신호였으나 240s 타임아웃 탓에 hang 중에도
#  오르내려 dedup 무력·태스크 수 의존 → v6.7.289 에서 성공 크롤 신호로 교체.)
# [v6.7.291] 레인 병렬화 이후 판정은 레인 단위(lane_last_success) — 이 상수는 base 로 쓰이고
# 실제 레인별 임계는 crawler.get_lane_hang_thresholds(base) 가 주기 반영해 산출
# (CAFE/DC: 720s, HTTP: 1080s). 전역 last_successful_crawl_ts 단독 판정은 한 레인만 hang 일 때
# 다른 레인 성공이 가려버리므로 레인 신호 부재(구버전) 시 fallback 으로만 사용.
HANG_CRAWL_STALE_SEC = 600
# [v6.7.302] 단일 boolean → 이미 알린 레인 집합. 레인 병렬화(v6.7.291) 후 hang 은 레인 단위인데
#   dedup 이 전역 boolean 이라 CAFE 알림 후 DC 가 추가로 hang 돼도 통지 안 되던 사각(부분 장애 과소평가) 해소.
#   현재 hung 레인 중 아직 안 알린 게 있으면 재발화, 전 레인 회복 시 clear. fallback(레인 미상) 은 '_global' 센티넬.
_alerted_hang_lanes: set = set()


def refresh_main_viewer_data():
    """뷰어 모드(크롤러 중지 시) 주기적 데이터 리프레시.
    Google Sheets 대신 SQLite 캐시에서 읽어 클라이언트에 전송합니다."""
    from web_lib.bg.crawler_singleton import get_crawler
    crawler = get_crawler()
    if crawler.is_running:
        return
    try:
        from sqlite_handler import db_handler as _db

        # 1. 커뮤니티 게시글 리프레시
        posts = _db.get_latest_posts(limit=2000)
        if posts:
            crawler.load_existing_data(posts)
            # [v6.7.433] 경량 신호 — 뷰어모드도 본문(무제한) broadcast 대신 핑만(PATTERN-38). 클라 자체 refetch.
            gl.socketio.emit('raw_articles_update', {})
            # [v6.7.61] 글 cycle 완료 시 article REST 캐시 무효화 — 다음 폴링에서 최신 데이터 반영.
            try:
                from web_lib.routes.articles import invalidate_article_caches
                invalidate_article_caches()
            except Exception:
                pass

        # 2. 댓글 리프레시
        _comment_limit = 500  # [v6.6.238] 서버 전송 풀 크기 고정
        comments = _db.get_comments(limit=_comment_limit)
        if comments:
            comments.sort(key=lambda x: normalize_timestamp(x.get('timestamp', '')), reverse=True)
            for c in comments:
                lnk = str(c.get('link', '') or c.get('comment_link', ''))
                c['source_community'] = 'DAUM' if 'cafe.daum.net' in lnk else 'DC'
            with gl._comment_lock:  # [v6.6.279]
                gl.comment_data_status = {
                    'latest_results': comments,
                    'last_updated': dt_class.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S"),
                }
            gl.socketio.emit('comments_update', comments)

    except Exception:
        pass  # [로그 다이어트]


def cleanup_visitor_status():
    """30분 이상 비활성 상태인 visitor_status 엔트리를 정리합니다.
    [v6.6.258] APScheduler 로 주기 호출 — visitor_status 읽기/쓰기를 Lock 으로 보호."""
    try:
        now = dt_class.now()
        stale_ips = []
        with gl._visit_lock:
            # snapshot 뜨고 판정 (dict 순회 중 다른 스레드 write 방지)
            snapshot = list(gl.visitor_status.items())
        for ip, info in snapshot:
            last_str = info.get('last_seen') or info.get('last_active', '')
            if not last_str:
                stale_ips.append(ip)
                continue
            try:
                last_dt = dt_class.strptime(last_str, '%Y-%m-%d %H:%M:%S')
                if (now - last_dt).total_seconds() > 1800:
                    stale_ips.append(ip)
            except ValueError:
                stale_ips.append(ip)
        if stale_ips:
            with gl._visit_lock:
                for ip in stale_ips:
                    gl.visitor_status.pop(ip, None)
                    # [v6.7.290] heartbeat→SQLite throttle 타임스탬프도 동반 정리 — visitor_status 와 키셋 동기(누수 방지).
                    gl.visitor_db_touch.pop(ip, None)
    except Exception:
        pass  # [로그 다이어트]


def _start_content_extractor_early(start_extractor):
    """── Phase 0.5 [v6.6.222]: 내용 크롤러 조기 기동 ──"""
    # Phase 2(Google Sheets/DataManager 초기화)가 서버 환경에서 지연되면 Phase 3까지의 대기가 길어진다.
    # 내용 크롤러는 SQLite만 있으면 동작하므로 설정 로드 없이 즉시 기동하여 본문 추출이 바로 시작되도록 한다.
    # (Phase 3에서도 호출되지만 중복 가드로 스킵됨)
    try:
        start_extractor()
    except Exception as e:
        logger.error(f"[CONTENT] 조기 자동 기동 실패(무시): {e}")


def _backfill_comment_games_if_ready(get_crawler):
    """── Phase 0.7 [v6.6.238] 댓글 game 컬럼 백필 (game IS NULL 레거시 댓글만) ──

    반환: crawler_for_backfill (get_crawler() 예외 시 None — Phase 1 game 폴백을 안전 스킵)."""
    # 크롤러 인스턴스가 아직 없을 수 있으므로 raw_config_games 없이도 동작하도록 방어.
    # [v6.6.257] try 진입 전 기본값 필수 — get_crawler() 예외 시 Phase 1 에서 NameError 로
    # SQLite 선로드 전체가 침묵 실패하던 버그 차단. Phase 1 의 외부 try/except 가 삼켜서 조용히 빈 화면이 나왔음.
    crawler_for_backfill = None
    try:
        crawler_for_backfill = get_crawler()
        if crawler_for_backfill and getattr(crawler_for_backfill, 'raw_config_games', None):
            from sqlite_handler import db_handler as _db
            updated, unresolved = _db.backfill_comment_games(
                crawler_for_backfill.extract_game_from_comment_link
            )
            if updated > 0 or unresolved > 0:
                logger.info(f"[SYNC] 댓글 game 백필: {updated}건 채움, {unresolved}건 미매칭.")
        else:
            logger.info("[SYNC] raw_config_games 아직 로드 전 — 백필 스킵 (Phase 3 이후 재시도 없음, 조회 폴백으로 커버).")
    except Exception as e:
        logger.warning(f"[SYNC] 댓글 game 백필 실패(무시): {e}")
    return crawler_for_backfill


def _preload_sqlite_cache(crawler_for_backfill, sub_targets):
    """── Phase 1: SQLite 즉시 선로드 (설정 대기 없이, 서버 기동 직후 실행) ──"""
    # 브라우저가 먼저 접속해도 빈 화면 방지
    try:
        from sqlite_handler import db_handler as _db
        logger.info("[SYNC] SQLite 캐시에서 기존 데이터 즉시 선로드...")

        # (A) 댓글 선로드 (가장 느린 탭 — 최우선)
        _comment_limit = 250
        cached_comments = _db.get_comments(limit=_comment_limit)
        if cached_comments:
            for _c in cached_comments:
                _lnk = str(_c.get('link', '') or _c.get('comment_link', ''))
                _c['source_community'] = 'DAUM' if 'cafe.daum.net' in _lnk else 'DC'
                # [v6.6.238] game 폴백: DB 에 저장됐으면 그대로, 아니면 comment_link 파싱으로 즉석 채움
                if not _c.get('game') and crawler_for_backfill:
                    try:
                        _c['game'] = crawler_for_backfill.extract_game_from_comment_link(_c.get('comment_link'))
                    except Exception:
                        pass
            with gl._comment_lock:  # [v6.6.279]
                gl.comment_data_status = {
                    'latest_results': cached_comments,
                    'last_updated': None,
                    'total_count': len(cached_comments),
                    'last_update': dt_class.now().strftime("%H:%M:%S"),
                }
            logger.info(f"[SYNC] SQLite 댓글 {len(cached_comments)}건 즉시 로드 완료.")

        # (B) 서브 크롤러 결과 선로드 (다이아 시세 / 계정 거래)
        yesterday_str = get_yesterday_str()
        for key, status in sub_targets:
            try:
                cached = _db.get_sub_crawler_cache(key, yesterday_str)
                if cached:
                    status['latest_results'] = cached
                    status['crawled_date'] = yesterday_str
                    status['gsheet_checked_date'] = yesterday_str
                    logger.info(f"[SYNC] SQLite {key} 캐시 {len(cached)}건 즉시 로드 완료.")
            except Exception:
                pass

    except Exception as e:
        logger.warning(f"[SYNC] SQLite 즉시 선로드 실패 (무시): {e}")


def _wait_for_crawler_settings(get_crawler):
    """── Phase 2: CrawlerManager 초기화 대기 (설정 로드 필요) ──

    반환: 설정이 준비된(또는 최대 30초 대기 소진한) crawler 인스턴스.
    NOTE: get_crawler() 는 원본과 동일하게 try 밖 — 여기 예외는 상위로 전파된다."""
    time.sleep(3)  # 서버 초기화 대기

    crawler = get_crawler()
    wait_count = 0
    while not crawler.settings and wait_count < 30:
        time.sleep(1)
        wait_count += 1
    return crawler


def _hydrate_memory_and_datamanager(crawler, sub_targets, ensure_sub_populated):
    """── Phase 2 (본문): 게시글 메모리 복구 + DataManager 초기화 + 서브 요약 계산 ──"""
    try:
        # (C) 커뮤니티 게시글 선로드 (crawler.load_existing_data 필요 → 설정 로드 후)
        try:
            from sqlite_handler import db_handler as _db
            # [v6.7.170] 부팅 1회 적재 2000 → 10000 (= crawler MAX_MEMORY_ARTICLES). 사용자 요청:
            #   피드가 보관된 7일치(최대 10000)를 전부 표시 — '최신 2000' 밖 과거 보관글도 메모리에 올림.
            #   주기 refresh (refresh_main_viewer_data 2000 / refresh_articles_memory_from_db 500 - 후자는 v6.7.730 삭제) 는 merge 라
            #   신규글만 누적해 10000 을 유지하므로 그대로 둠. load_existing_data 의 항목별 pd.to_datetime 락 홀드
            #   비용을 startup 1회로 국한 (운영 중 매 사이클 10000 처리 회피).
            existing_posts = _db.get_latest_posts(limit=10000)
            if existing_posts:
                added = crawler.load_existing_data(existing_posts)
                logger.info(f"[SYNC] SQLite 게시글 {added}건 메모리 복구 완료.")
            else:
                logger.info("[SYNC] SQLite에 기존 게시글 없음. 크롤링으로 누적합니다.")
        except Exception as e:
            logger.error(f"[SYNC] 게시글 선로드 실패: {e}")

        # (D) DataManagerAndUploader 초기화 (SQLite 캐시 데이터 주입)
        try:
            handler = get_handler(log_func=crawler._log)
            dynamic_url = crawler.settings.get('URL_CONTENTS', CONTENTS_SHEET_URL)
            # SQLite 선로드 데이터가 있으면 DataFrame으로 변환하여 초기 데이터로 사용
            initial_df = pd.DataFrame()
            with gl._comment_lock:  # [v6.6.279] 읽기도 lock (스냅샷 복사)
                _cached = list(gl.comment_data_status.get('latest_results', []))
            if _cached:
                initial_df = pd.DataFrame(_cached)
                logger.info(f"[SYNC] DataManager에 SQLite 캐시 {len(_cached)}건 주입.")
            gl._comment_data_manager = DataManagerAndUploader(handler, initial_df, spreadsheet_url=dynamic_url)
        except Exception as e:
            logger.error(f"[SYNC] DataManager 초기화 실패: {e}")

        # (E) 서브 크롤러 요약(summary) 계산 (설정의 게임 목록 필요)
        for key, status in sub_targets:
            if status.get('latest_results'):
                try:
                    _SM = {'ODIN_GLOBAL': ('ODIN_GLOBAL', 'G2G'), 'ACCOUNT_TRADE': ('ACCOUNT', 'ACCOUNT'),
                           'JASA_GAMES': ('JASA', 'ITEMMANIA'), 'TASA_GAMES': ('TASA', 'ITEMMANIA')}
                    sub_key, summary_type = _SM[key]
                    ensure_sub_populated(key, status, sub_key, summary_type)
                except Exception:
                    pass

    except Exception as e:
        logger.error(f"[SYNC] 통합 동기화 프로세스 오류: {e}")


def _launch_crawlers(start_extractor, run_supervised, init_async):
    """── Phase 3: 크롤러 가동 ──"""
    import threading
    logger.info("[SYNC] 데이터 동기화 완료. 크롤링 스케줄러를 가동합니다.")

    # [v6.6.341] supervised 래퍼로 변경 — 예외 발생 시 10초 후 자동 재진입 + 카카오워크 알림
    threading.Thread(target=run_supervised, kwargs={'skip_initial_load': True}, daemon=True).start()

    # 내용 크롤러 (본문 추출, 비동기)
    start_extractor()

    init_async()


def start_crawler_with_delay():
    """앱 시작 시 SQLite 캐시에서 기존 데이터를 선로드한 후 크롤링을 시작하는 통합 루틴.
    커뮤니티/댓글 데이터는 Google Sheets에서 읽지 않고, SQLite + 크롤링 누적으로 운영합니다.
    (설정(Config)만 Google Sheets에서 2분 주기 폴링)

    [v6.7.708] 리팩토링(동작 불변): 단계별 private 헬퍼로 분해. 순서·지연값·예외 경계·주석 보존.
    상단 3개 import 는 원본과 동일하게 Phase 0.5 전 uncaught 로 둔다(실패 시 조기 기동 전 전파).
    특성화 스냅샷: test_start_crawler_contract.py."""
    from web_lib.bg.crawler_singleton import get_crawler, init_crawler_async
    from web_lib.bg.contents import (
        _start_content_extractor_thread,
        run_contents_crawler_supervised,
    )
    from web_lib.bg.sub_runners import _ensure_sub_crawler_status_populated

    _start_content_extractor_early(_start_content_extractor_thread)
    crawler_for_backfill = _backfill_comment_games_if_ready(get_crawler)

    _sub_targets = [
        ('ODIN_GLOBAL', gl.odin_global_status),
        ('ACCOUNT_TRADE', gl.account_trade_status),
        ('JASA_GAMES', gl.jasa_games_status),
        ('TASA_GAMES', gl.tasa_games_status),
    ]
    _preload_sqlite_cache(crawler_for_backfill, _sub_targets)

    crawler = _wait_for_crawler_settings(get_crawler)
    _hydrate_memory_and_datamanager(crawler, _sub_targets, _ensure_sub_crawler_status_populated)

    _launch_crawlers(_start_content_extractor_thread, run_contents_crawler_supervised, init_crawler_async)


# [v6.6.281] 메인 크롤러 헬스체크 + 자동 재시작 (B4)
# 본문 크롤러(content_extractor) 는 이미 헬스체크 있으나 메인 크롤러(카페/DC) 는 미구현.
# 스레드가 크래시하면 사용자가 수동으로 "시작" 눌러야만 복구 → 장기 무인 운영 불가.


def check_main_crawler_health():
    """메인 크롤러 스레드 헬스체크. 사용자가 직접 stop() 한 상태가 아닌데 스레드가
    죽어있으면 재시작. APScheduler 1분 주기로 호출."""
    from web_lib.bg.crawler_singleton import get_crawler
    global _main_crawler_restart_count, _alerted_hang_lanes
    crawler = get_crawler(blocking=False)
    if crawler is None:
        return  # 아직 초기화 전
    # [v6.7.128] 사용자가 명시적으로 컨트롤바 "중지" 누른 경우(`user_initiated_stop=True`)에만
    # 자동 재시작 skip. SIGTERM / 내부 cleanup / 부수효과로 인한 비자발 정지는 자동복구.
    # 이전 동작은 `is_running=False` 만 봐서 silent stop 잔존 (2026-05-23 17:00~ 약 20시간 사례).
    if not crawler.is_running:
        if getattr(crawler, 'user_initiated_stop', False):
            _main_crawler_restart_count = 0
            return
        # 비자발 정지 — 자동 재시작 분기로 진입
        logger.warning(
            "[MAIN] is_running=False 이지만 user_initiated_stop=False → "
            "비자발 정지로 판단, 자동 재시작 진행"
        )
    else:
        # 실행 중이어야 하는데 스레드가 없거나 죽어있음 → 복구 필요
        thread = getattr(crawler, 'crawler_thread', None)
        if thread is not None and thread.is_alive():
            # [v6.7.288/289] thread.is_alive() 만으론 hang 을 못 잡음 — 2026-07-01 11:50~ 사고에서
            # future.result() 무한대기로 10시간+ 정지했는데도 is_alive()=True 라 여기서
            # "정상"으로 오판하고 그냥 return 했음.
            # [v6.7.289] 판정 신호를 last_loop_heartbeat(루프 반복) → last_successful_crawl_ts
            # (타임아웃 없이 반환한 마지막 크롤)로 교체. 후자는 hang 동안 갱신 안 돼 단조 증가 →
            # 감지가 태스크 수×타임아웃 산술에 안 흔들리고 dedup 도 견고(heartbeat 는 240s
            # 타임아웃 탓에 hang 중에도 오르내려 dedup 무력 + 태스크 적으면 감지 실패).
            # [v6.7.291] 레인 병렬화 — 판정을 레인 단위로. 전역 신호는 "임의 레인 성공"이라
            # 한 레인만 hang 하면 다른 레인 성공이 계속 갱신해 hang 을 가린다(병렬화가 만드는
            # 회귀 지점을 같은 커밋에서 선제 차단). 레인별 임계는 주기 반영(get_lane_hang_thresholds).
            lane_ts = getattr(crawler, 'lane_last_success', None) or {}
            if lane_ts and hasattr(crawler, 'get_lane_hang_thresholds'):
                thresholds = crawler.get_lane_hang_thresholds(HANG_CRAWL_STALE_SEC)
                now_ts = time.time()
                hung_lanes = {
                    f"레인 {lane}": now_ts - ts for lane, ts in lane_ts.items()
                    if now_ts - ts > thresholds.get(lane, HANG_CRAWL_STALE_SEC)
                }
                if hung_lanes:
                    _report_main_crawler_hang(max(hung_lanes.values()), crawler, hung_lanes=hung_lanes)
                    # is_running=True 인 좀비 레인 worker 는 crawler.start() 로도 교체 안 됨
                    # (start() 의 thread.is_alive() 가드) — 알림만 보내고 사람 개입을 기다린다.
                    return
                # [v6.7.551] **태스크 축 판정** - 레인이 정상으로 보여도 그 안의 태스크가 죽어
                #   있을 수 있다. 한 레인에 주기가 12배 차이 나는 태스크가 같이 살면(CAFE =
                #   NAVER 70~120초 + DAUM 900~1200초) 빈번한 쪽이 레인 시계를 계속 갱신하므로
                #   **느린 쪽이 통째로 죽어도 이 위 블록은 영원히 정상으로 읽는다**
                #   (2026-08-15 모니터링 검토에서 확인한 사각지대. DAUM = 오딘·아레스·아키에이지워·
                #   프렌즈타운·우마무스메 5게임의 카페 수집 경로라 유실 반경이 작지 않다).
                #   레인 판정을 대체하지 않고 **뒤에 덧붙인다** - 레인 마비가 더 큰 사건이라 먼저 알린다.
                task_ts = getattr(crawler, 'task_last_success', None) or {}
                if task_ts and hasattr(crawler, 'get_task_hang_thresholds'):
                    task_thresholds = crawler.get_task_hang_thresholds(HANG_CRAWL_STALE_SEC)
                    #   임계에 없는 key 는 **검사하지 않는다**(기본값으로 떨어뜨리지 않는다) -
                    #   `get_task_hang_thresholds` 는 enabled 태스크만 내므로, 없는 key = 비활성/제거된
                    #   태스크다. 기본값 600초를 먹이면 그 낡은 타임스탬프가 곧바로 오탐이 된다.
                    hung_tasks = {
                        f"태스크 {key}": now_ts - ts for key, ts in task_ts.items()
                        if key in task_thresholds and now_ts - ts > task_thresholds[key]
                    }
                    if hung_tasks:
                        _report_main_crawler_hang(max(hung_tasks.values()), crawler, hung_lanes=hung_tasks)
                        return
                _main_crawler_restart_count = 0
                _alerted_hang_lanes = set()  # [v6.7.302] 전 레인 회복 → dedup 집합 clear
                return
            # (fallback — 레인 신호 미시드 구버전/부팅 직후) 전역 신호로 판정
            last_ok = getattr(crawler, 'last_successful_crawl_ts', None)
            stale_sec = (time.time() - last_ok) if last_ok else 0
            if stale_sec > HANG_CRAWL_STALE_SEC:
                _report_main_crawler_hang(stale_sec, crawler)
                # is_running=True 인 좀비 스레드는 crawler.start() 로도 교체 안 됨
                # (crawler.py start() 의 thread.is_alive() 가드에 그대로 막힘) — 자동
                # 재시작을 시도해봐야 no-op 이므로 알림만 보내고 사람 개입을 기다린다.
                return
            _main_crawler_restart_count = 0
            _alerted_hang_lanes = set()  # [v6.7.302] 정상 → dedup 집합 clear
            return

    _main_crawler_restart_count += 1
    wait_sec = min(30 * _main_crawler_restart_count, 300)  # exponential backoff, max 5분
    logger.warning(
        f"[MAIN] 메인 크롤러 스레드 비정상 종료 감지 (연속 {_main_crawler_restart_count}회). "
        f"{wait_sec}초 후 재시작 시도."
    )
    # 바로 재시작하지 않고 scheduler 의 date trigger 로 지연
    scheduler = gl.scheduler
    if scheduler and scheduler.running:
        run_time = dt_class.now() + timedelta(seconds=wait_sec)
        scheduler.add_job(
            _restart_main_crawler, trigger='date', run_date=run_time,
            id='main_crawler_restart', replace_existing=True,
        )


def _report_main_crawler_hang(stale_sec, crawler=None, hung_lanes=None):
    """[v6.7.288/289] 성공 크롤 정체로 hang 감지 시 1회성 카카오워크 경보.

    [v6.7.291/550] hung_lanes({"레인 CAFE"|"태스크 DAUM": stale_sec}) 전달 시 무엇이 마비인지 병기 —
    레인 병렬 구조에서는 hang 이 레인 단위로 국소화되므로(다른 레인은 계속 수집)
    "전체 정지"가 아닌 "CAFE 레인만 정지" 같은 부분 장애 식별이 진단의 핵심.

    is_running=True + thread.is_alive()=True 인 좀비 스레드는 `_restart_main_crawler`
    (→ `crawler.start()`) 로 복구 시도해도 `start()` 의 `if self.is_running: return` 가드에
    막혀 no-op — 컨트롤바 "시작" 버튼과 동일하게 무력하다(2026-07-01 실사고에서 사용자가
    직접 중지→시작 눌러도 좀비 스레드 그대로 잔존 확인). 잘못된 자동 조치로 부작용을
    만들기보다, 사람이 컨테이너를 재시작하도록 알리는 것에 그친다.

    [v6.7.289] 판정 신호가 last_successful_crawl_ts(단조 증가)라 dedup 이 실제로 동작 —
    hang 지속 동안 dedup(_alerted_hang_lanes, v6.7.302 이전 _main_crawler_hang_alerted)이 유지되어 1회만 발화(v6.7.288
    heartbeat 방식은 진동 탓에 dedup 이 ~16분마다 풀렸음). 루프 자체가 완전히 얼었는지
    (vs 태스크만 timeout 반복) 진단용으로 last_loop_heartbeat staleness 도 병기한다.
    """
    global _alerted_hang_lanes
    stale_min = stale_sec / 60
    loop_stale_min = None
    if crawler is not None:
        hb = getattr(crawler, 'last_loop_heartbeat', None)
        if hb:
            loop_stale_min = (time.time() - hb) / 60
    loop_txt = f" / 루프 반복 정체 {loop_stale_min:.1f}분" if loop_stale_min is not None else ""
    # [v6.7.291] 마비 대상 나열 (예: "레인 CAFE(12.3분), 태스크 DAUM(48.0분)")
    # [v6.7.551] 레인과 태스크 두 축을 같은 자리에 실으므로 라벨을 "레인" 에서 "대상" 으로 넓혔다
    #   (호출측이 키에 축 이름을 붙여 넘긴다 - 여기서는 형식만 만든다).
    lane_txt = ""
    if hung_lanes:
        lane_txt = " / 마비 대상: " + ", ".join(
            f"{ln}({sec / 60:.1f}분)" for ln, sec in sorted(hung_lanes.items())
        )
    logger.error(
        f"[MAIN] 메인 크롤러 hang 의심 — 성공 크롤 {stale_min:.1f}분 정체{loop_txt}{lane_txt}. "
        f"수동 컨테이너 재시작 필요(자동복구 불가 경로)."
    )
    # [v6.7.302] 레인 단위 dedup — 현재 hung 레인 중 아직 안 알린 레인이 있으면 발화, 없으면 스팸 방지.
    #   fallback(레인 미상) 경로는 '_global' 센티넬로 기존 1회 발화 동작 유지.
    _current_lanes = set(hung_lanes.keys()) if hung_lanes else {'_global'}
    if _current_lanes <= _alerted_hang_lanes:
        return  # 이미 알린 레인만 hung — 반복 알림 억제 (새 레인 추가 시에만 재발화)
    _alerted_hang_lanes = _alerted_hang_lanes | _current_lanes
    try:
        from notification_manager import NotificationManager
        ts = dt_class.now(ZoneInfo('Asia/Seoul')).strftime('%Y-%m-%d %H:%M:%S')
        blocks = [
            {"type": "header", "text": "메인 크롤러 hang 의심 — 수동 재시작 필요", "style": "red"},
            {"type": "text", "text": (
                f"*성공 크롤 정체:* {stale_min:.1f}분{loop_txt}{lane_txt}\n*시각:* {ts}\n"
                "스레드가 죽지 않고 멈춰있어 '시작' 버튼과 자동복구 모두 무력합니다. "
                "`docker restart imt-container` 로 컨테이너 재시작이 필요합니다."
            ), "markdown": True},
        ]
        NotificationManager().send_kakaowork("메인 크롤러 hang 경보", blocks)
    except Exception as e:
        logger.warning(f"[MAIN] hang 알림 전송 실패: {e}")


def _restart_main_crawler():
    """지연 후 호출되어 메인 크롤러를 재시작합니다.

    [v6.7.128] is_running=False 라도 user_initiated_stop=False 면 비자발 정지로 간주하고
    재시작 진행. 사용자가 컨트롤바 "중지" 누른 경우(user_initiated_stop=True)만 취소.
    """
    from web_lib.bg.crawler_singleton import get_crawler
    try:
        crawler = get_crawler(blocking=False)
        if crawler is None:
            logger.warning("[MAIN] 재시작 시 크롤러 인스턴스 없음 — 스킵")
            return
        if not crawler.is_running and getattr(crawler, 'user_initiated_stop', False):
            logger.info("[MAIN] 사용자가 명시적으로 중지(user_initiated_stop=True) — 자동 재시작 취소")
            return
        crawler.start()
        logger.info("[MAIN] 메인 크롤러 자동 재시작 완료.")
    except Exception as e:
        logger.error(f"[MAIN] 자동 재시작 실패: {e}")
