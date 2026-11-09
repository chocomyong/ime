# -*- coding: utf-8 -*-
"""bg.contents — 댓글 크롤러 + 본문 크롤러 + 크래시 알림.

댓글 크롤러 (contents_crawler.py 래퍼):
- `on_comment_received(data)` : 댓글 수신 콜백 (SQLite 저장 + 실시간 UI 갱신 + 주기 완료 처리)
- `run_contents_crawler(skip_initial_load=False)` : 댓글 크롤러 진입점
- `run_contents_crawler_supervised(skip_initial_load=False)` : 예외 시 자동 재진입 래퍼

본문 크롤러 (content_extractor.py 래퍼):
- `_content_log_func(msg, level, tag)` : 로그 래퍼 (crawler 초기화 전에도 안전)
- `_start_content_extractor_thread()` : 스레드 생성 + gl 참조 저장
- `check_content_extractor_health()` : 헬스체크 + 재시작 예약
- `_restart_content_extractor()` : 지연 재시작 콜백

공통:
- `_send_crawler_crash_alert(crawler_name, error, action)` : 카카오워크 알림 (silent failure)

모듈 상태 (본 모듈 소유):
- `_contents_crawler_running`, `_contents_crawler_lock`, `_generic_crawler_lock`

외부 reader (broadcast.py / admin.py) 는 `web_lib.bg.contents._contents_crawler_running`
로 직접 접근하도록 업데이트됨.
"""
import threading
import time
import logging
from datetime import datetime as dt_class, timedelta
from zoneinfo import ZoneInfo

import web_lib.globals as gl
from common import CONTENTS_SHEET_URL, normalize_timestamp
from google_sheets_handler import get_handler
from contents_crawler import crawl_comments, DataManagerAndUploader, load_initial_data

logger = logging.getLogger(__name__)


# 댓글 크롤러 상태
_contents_crawler_running = False
_contents_crawler_lock = threading.Lock()
# `_generic_crawler_lock` 은 `_run_crawler_generic` 전용이므로 `bg/sub_runners.py` 로 이관됨.

# ====================================================================
# [v6.7.485] 댓글 크롤러 on/off — `system_settings.comments_crawler_enabled`
# ====================================================================
# 왜 필요한가: 이 크롤러는 **전용 Chrome + 카카오 로그인 세션**을 상시 물고 있다
#   (PATTERN-31 실측 약 953MB). 담당이 아닌 기간에는 그 비용을 낼 이유가 없는데
#   **끄는 수단이 코드에 아예 없었다** — 주기(`COMMENTS_CRAWL_INTERVAL_*`)를 늘려도
#   `comment_trigger_event`(메인 크롤러 DAUM/DC 사이클 종료마다 발화, `crawler.py:1663`)가
#   대기를 깨우므로 **DC 주기(30~60초)로 계속 돈다**. 즉 주기로는 못 멈춘다
#   (2026-08-05 실측: 46/56 → 1800/3600 으로 바꿔도 webapp CPU 변화 0 — 실험이 무효였다).
#
# 왜 `REMOTE_KILL_*` 을 안 쓰나: 그 규약은 은퇴한 앱(터미널·AI 프로세서) 시절 것이라
#   신규 확장 대상이 아니다(사용자 지시 2026-08-05). 현행 전역 토글 패턴은
#   `system_settings` + admin API 이며 `access_lock`(v6.6.353)이 그 선례다.
#
# 왜 `contents_stop_event` 를 직접 안 쓰나: 그것은 **정상 셧다운** 신호라 set 하면
#   supervised 루프까지 종료된다(다시 켤 수 없음). 그래서 별도 이벤트를 두고
#   `crawl_comments` 에는 두 신호를 합친 shim 을 넘긴다 — 그러면 기존 종료 경로
#   (`finally`: 쿠키 저장 → `driver.quit()`)를 그대로 타서 **드라이버가 실제로 죽는다**.
#   유휴 드라이버는 메모리를 안 놓는다는 것이 PATTERN-31 의 `about:blank` 실험으로
#   확정돼 있으므로, 메모리를 회수하려면 반드시 quit 여야 한다.
_comments_pause_event = threading.Event()
# [v6.7.598] **기본 pause 상태로 시작한다.** threading.Event() 의 기본값은 clear(= 안 멈춤)라
#   부팅 직후 supervised 루프가 pause 가드를 그냥 통과해 `run_contents_crawler` 에 진입했고,
#   감시 스레드의 첫 조회(최대 15초 뒤)가 pause 를 걸어도 **이미 사이클 안이라 못 막았다.**
#   2026-08-21 14:21:32 실측 — `토글 OFF` 로그 **2ms 뒤**에 `댓글 크롤링 시작` 이 찍혔고
#   그대로 로그인 50초 → 실패 → Chrome 사망까지 갔다. 게다가 로그인 단계는 `_StopOrPause` 를
#   보지 않으므로(그 shim 은 크롤링 루프의 `stop_event.is_set()` 호출에만 반응) 중간에 끊을
#   수단도 없다. v6.7.597 의 기본값 False 만으로는 이 레이스가 안 막힌다 — 조회 전에 이미
#   돌기 때문이다. **확인되기 전까지 멈춰 있는 것**이 맞는 초기 상태다.
_comments_pause_event.set()
_COMMENTS_ENABLED_KEY = 'comments_crawler_enabled'
_COMMENTS_TOGGLE_POLL_SEC = 15
# [v6.7.485] 감시 스레드 1회 기동 가드. `run_contents_crawler_supervised` 는 **두 곳**에서
#   스레드로 뜬다 — `bg/scheduler.py:389`(부팅) 과 `routes/crawler_control.py:46`
#   (`/api/crawler/start`, 뷰어→가동 모드 전환). 가드가 없으면 모드 전환마다 감시 스레드가
#   하나씩 쌓인다(동작은 같지만 스레드 누수).
_toggle_watcher_started = False
_toggle_watcher_lock = threading.Lock()


def is_comments_crawler_enabled() -> bool:
    """`system_settings` 의 토글 조회. 미설정/조회 실패 시 **False**(기본 OFF).

    [v6.7.597] 기본값을 True → False 로 뒤집는다. 종전 기본 True 는 **배포·재시작마다 크롤러를
    자동으로 켰고**, 2026-08-05 로그인이 깨진 뒤 16일간(08-06~08-21) 2분 주기 실패 루프가 돌았다.
    그 사이 카카오 계정이 임시 잠금에 걸렸는데 잠금은 2시간이면 풀리는 것이라, **재시도가 잠금을
    계속 갱신해 복구 가능한 장애를 영구화**했다. 로그인 없이 도는 본문 크롤러와 달리 이 크롤러는
    외부 계정에 의존하므로 "꺼져 있는 것" 이 안전한 기본값이다.

    판정도 `!= 'false'`(오타·이상값도 켜짐) → `== 'true'`(명시적으로 켤 때만 켜짐) 로 바꿔
    fail-safe 방향을 맞춘다. 켜려면 admin 화면에서 명시적으로 ON 해야 한다.
    """
    try:
        from sqlite_handler import db_handler as _db
        return (_db.get_system_setting(_COMMENTS_ENABLED_KEY, 'false') or 'false').lower() == 'true'
    except Exception:
        return False


class _StopOrPause:
    """`crawl_comments` 는 `stop_event.is_set()` 만 호출하므로 이 shim 으로 충분하다.

    전역 종료(`contents_stop_event`) 또는 토글 OFF 중 하나면 True → 크롤러가 정상 종료 경로로
    빠져나가며 드라이버를 quit 한다. 토글 OFF 였다면 supervised 루프가 대기 후 재진입한다.
    """
    def is_set(self):
        return gl.contents_stop_event.is_set() or _comments_pause_event.is_set()


def _comments_toggle_watcher():
    """토글을 주기적으로 읽어 `_comments_pause_event` 에 반영하는 감시 스레드.

    별도 스레드인 이유: supervised 루프는 `run_contents_crawler` 안에서 블록돼 있어
    **실행 중에는 토글을 볼 수 없다.** 이 스레드가 대신 보고 신호만 세운다.
    """
    while not gl.contents_stop_event.is_set():
        try:
            if is_comments_crawler_enabled():
                if _comments_pause_event.is_set():
                    _comments_pause_event.clear()
                    logger.info("[COMMENTS] 토글 ON — 다음 사이클부터 재개")
            else:
                if not _comments_pause_event.is_set():
                    _comments_pause_event.set()
                    logger.info("[COMMENTS] 토글 OFF — 진행 중 사이클 종료 후 드라이버 quit")
        except Exception:
            pass
        time.sleep(_COMMENTS_TOGGLE_POLL_SEC)


def _send_crawler_crash_alert(crawler_name: str, error: Exception, action: str = "재시작"):
    """[v6.6.341] 크롤러 예외/재시작 시 카카오워크 알림 — 조용히 실패.

    - `_monitoring_loop` (메인) / `run_contents_crawler` (댓글) / `check_content_extractor_health` (본문)
      모두 이 헬퍼를 공유해 일관된 알림 포맷 사용.
    - 알림 전송 실패는 앱 안정성 우선 원칙에 따라 silent log only.
    """
    try:
        from notification_manager import NotificationManager
        kst = ZoneInfo("Asia/Seoul")
        ts = dt_class.now(kst).strftime('%Y-%m-%d %H:%M:%S')
        err_excerpt = str(error)[:300] if error else "알 수 없음"
        blocks = [
            {"type": "header", "text": f"크롤러 {action} — {crawler_name}", "style": "yellow"},
            {"type": "text", "text": f"*예외:* `{err_excerpt}`\n*시각:* {ts}", "markdown": True},
        ]
        # [v6.6.343] crawler_restart 카테고리 가드 — 관리자 토글 OFF 시 스킵.
        NotificationManager().send_crawler_restart(f"크롤러 {action} — {crawler_name}", blocks)
    except Exception as _alert_err:
        logger.warning(f"[CRASH_ALERT] 알림 전송 실패({crawler_name}): {_alert_err}")


# [v6.7.597] 댓글 크롤러 실패 알림 쿨다운 — 같은 오류가 반복될 때 알림 폭주를 막는다.
#   2분 주기 재시도 × 16일이면 알림도 1만 건이 되므로, 첫 발생만 즉시 알리고 이후는 억제한다.
_COMMENTS_ALERT_COOLDOWN_SEC = 1800
_last_comments_alert = {'sig': '', 'ts': 0.0}


def _notify_comments_failure(error: Exception):
    """댓글 크롤러 실패 알림 — 첫 발생·오류 변경 시 즉시, 같은 오류 반복은 30분 억제.

    [v6.7.597] 종전에는 `run_contents_crawler` 의 `except` 가 **로그만 찍고 예외를 삼켜서**
    supervised 래퍼의 `except` 까지 올라가지 못했고, 그 결과 `_send_crawler_crash_alert` 가
    한 번도 불리지 않았다 — 2026-08-05~21 **16일 무중단 실패에 카카오워크 알림 0건**.
    `crawler_restart` 토글은 내내 ON 이었으므로 알림 인프라가 아니라 호출 경로가 없던 것이다.
    """
    try:
        sig = f"{type(error).__name__}:{str(error)[:80]}"
        now = time.time()
        if sig == _last_comments_alert['sig'] and (now - _last_comments_alert['ts']) < _COMMENTS_ALERT_COOLDOWN_SEC:
            return
        _last_comments_alert['sig'] = sig
        _last_comments_alert['ts'] = now
        _send_crawler_crash_alert("댓글 크롤러", error, action="실패")
    except Exception as _e:
        logger.warning(f"[CRASH_ALERT] 댓글 크롤러 실패 알림 처리 중 오류: {_e}")


def on_comment_received(data):
    has_manager = bool(gl._comment_data_manager)

    # DataManager가 있으면 메모리 DataFrame에 추가
    if has_manager:
        gl._comment_data_manager.add_data(data)

    # SQLite 저장은 DataManager 유무와 무관하게 항상 수행
    if data.get('comment_link'):
        try:
            from sqlite_handler import db_handler as _db
            _db.save_comments(data)
        except Exception:
            pass

    # 실시간 UI 갱신
    if data.get('comment_link'):
        try:
            from sqlite_handler import db_handler as _db
            _comment_limit = 250  # [v6.6.326] 500 → 250 (게임 5개 × 50 기준). 초기 페이로드/DOM 비용 감소.
            current_records = _db.get_comments(limit=_comment_limit)

            current_records.sort(key=lambda x: normalize_timestamp(x.get('timestamp', '')), reverse=True)

            # [v6.7.455] is_new 주입 제거 (컬럼 은퇴) — 댓글 탭의 신규 표시는 도착 잔광이 담당.
            for r in current_records:
                lnk = str(r.get('link', '') or r.get('comment_link', ''))
                r['source_community'] = 'DAUM' if 'cafe.daum.net' in lnk else 'DC'

            # [v6.6.279] Lock 으로 atomic swap 보호 (읽기-수정-쓰기 race 방지)
            with gl._comment_lock:
                gl.comment_data_status = {
                    'latest_results': current_records,
                    'last_updated': gl.comment_data_status.get('last_updated'),
                }
            gl.socketio.emit('comments_update', current_records)
        except Exception as e:
            logger.debug(f"실시간 댓글 UI 업데이트 실패: {e}")

    # [v6.7.455] 주기 완료 훅 — 구 is_new 마킹은 컬럼 은퇴로 제거됐고, 그 마킹을 위해
    #   댓글 uid 를 모으던 `_pending_new_comment_ids`/`_pending_comments_lock` 도 함께 제거했다
    #   (읽는 곳이 없어져 write-only 누적만 남았기 때문). 아래 캐시 무효화는 계속 필요하다.
    if data.get('meta_type') == 'cycle_finish':
        # [v6.7.61] 댓글 cycle 완료 시 article 캐시 무효화 — by-community 등 최신 데이터 즉시 반영.
        try:
            from web_lib.routes.articles import invalidate_article_caches
            invalidate_article_caches()
        except Exception:
            pass
        try:
            from sqlite_handler import db_handler as _db
            _comment_limit = 250
            records = _db.get_comments(limit=_comment_limit)

            records.sort(key=lambda x: normalize_timestamp(x.get('timestamp', '')), reverse=True)

            # [v6.7.455] is_new 주입 제거 (컬럼 은퇴)
            for r in records:
                lnk = str(r.get('link', '') or r.get('comment_link', ''))
                r['source_community'] = 'DAUM' if 'cafe.daum.net' in lnk else 'DC'

            # [v6.6.279] Lock 으로 atomic swap 보호
            with gl._comment_lock:
                gl.comment_data_status = {
                    'latest_results': records,
                    'last_updated': dt_class.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S"),
                }
            gl.socketio.emit('comments_update', records)
        except Exception as e:
            logger.error(f"주기 완료 댓글 갱신 실패: {e}")


def run_contents_crawler_supervised(skip_initial_load=False):
    """[v6.6.341] `run_contents_crawler` self-healing 래퍼.

    예외 발생 시 카카오워크 알림 + 10초 대기 후 자동 재진입.
    `gl.contents_stop_event` 가 set 되면 루프 종료 (정상 셧다운).
    """
    cooldown = 10
    # [v6.7.485] 토글 감시 스레드 1회만 기동 (실행 중에도 토글을 보려면 별도 스레드가 필요하다).
    #   이 함수가 두 경로에서 호출되므로 가드 필수 — 위 `_toggle_watcher_started` 주석 참조.
    global _toggle_watcher_started
    with _toggle_watcher_lock:
        if not _toggle_watcher_started:
            _toggle_watcher_started = True
            threading.Thread(target=_comments_toggle_watcher, name="comments-toggle-watcher",
                             daemon=True).start()
    while not gl.contents_stop_event.is_set():
        try:
            # [v6.7.485] 토글 OFF 면 아예 시작하지 않는다 (드라이버도 안 뜬다).
            if _comments_pause_event.is_set():
                time.sleep(_COMMENTS_TOGGLE_POLL_SEC)
                skip_initial_load = True
                continue
            run_contents_crawler(skip_initial_load=skip_initial_load)
            # 예외 없이 종료 — `targets` 비었거나 stop_event 의해 자연 종료.
            if gl.contents_stop_event.is_set():
                break
            # [v6.7.485] 토글 OFF 로 빠져나온 경우 60초 대기 없이 위 가드로 즉시 복귀
            if _comments_pause_event.is_set():
                skip_initial_load = True
                continue
            # targets 변경 대기 + 재진입
            time.sleep(60)
            skip_initial_load = True
        except Exception as e:
            logger.error(f"[COMMENTS] supervised 예외 → {cooldown}s 후 재진입: {e}", exc_info=True)
            _send_crawler_crash_alert("댓글 크롤러", e)
            time.sleep(cooldown)
            skip_initial_load = True


def run_contents_crawler(skip_initial_load=False):
    global _contents_crawler_running
    from web_lib.bg_tasks import get_crawler  # 순환 회피 — 런타임 참조

    with _contents_crawler_lock:
        if _contents_crawler_running:
            logger.info("[COMMENTS] 댓글 크롤러가 이미 실행 중입니다.")
            return
        _contents_crawler_running = True

    try:
        crawler = get_crawler()
        # [수정] 설정이 로드될 때까지 대기 상황을 로그로 출력
        wait_start = time.time()
        while not crawler.settings:
            if time.time() - wait_start > 5:
                crawler._log("댓글 크롤러: 구글 시트 설정을 대기 중입니다...", 'info', 'COMMENTS')
                wait_start = time.time()
            time.sleep(1)

        targets = crawler.comment_targets
        if not targets:
            crawler._log("댓글 크롤러: Config_Contents에 수집 대상이 없어 수집을 진행하지 않습니다.", 'warning', 'COMMENTS')
            with _contents_crawler_lock:
                _contents_crawler_running = False
            return

        # [FIX] SQLite 캐시에서 즉시 선로드 — 구글 시트 로드 완료 전까지 댓글 탭 공백 방지
        try:
            from sqlite_handler import db_handler as _db
            _comment_limit = 250
            _cached = _db.get_comments(limit=_comment_limit)
            if _cached:
                for _c in _cached:
                    _lnk = str(_c.get('link', '') or _c.get('comment_link', ''))
                    _c['source_community'] = 'DAUM' if 'cafe.daum.net' in _lnk else 'DC'
                with gl._comment_lock:
                    gl.comment_data_status = {
                        'latest_results': _cached,
                        'last_updated': gl.comment_data_status.get('last_updated'),
                    }
                crawler._log(f"댓글 크롤러: SQLite 캐시 {len(_cached)}건 선로드 완료.", 'info', 'COMMENTS')
        except Exception as _e:
            crawler._log(f"댓글 SQLite 선로드 실패 (무시): {_e}", 'warning', 'COMMENTS')

        # [수정] skip_initial_load가 False일 때만 초기 데이터 로드 수행
        if not skip_initial_load:
            try:
                handler = get_handler(log_func=crawler._log)
                if handler.gc:
                    dynamic_url = crawler.settings.get('URL_CONTENTS', CONTENTS_SHEET_URL)
                    initial_df = load_initial_data(handler, spreadsheet_url=dynamic_url)
                    gl._comment_data_manager = DataManagerAndUploader(handler, initial_df, spreadsheet_url=dynamic_url)

                    if not initial_df.empty:
                        # DataManager에 시트 데이터 유지 (업로드용)
                        # 표시용은 SQLite에서 읽기 (날짜 무관 전체 누적)
                        from sqlite_handler import db_handler as _db_init
                        _comment_limit = 250
                        _cached = _db_init.get_comments(limit=_comment_limit)
                        for r in _cached:
                            lnk = str(r.get('link', '') or r.get('comment_link', ''))
                            r['source_community'] = 'DAUM' if 'cafe.daum.net' in lnk else 'DC'

                        with gl._comment_lock:
                            gl.comment_data_status = {
                                'latest_results': _cached,
                                'last_updated': dt_class.now().strftime("%Y-%m-%d %H:%M:%S"),
                            }
                        crawler._log(f"댓글 크롤러: 초기 데이터 {len(_cached)}건 로드 완료 (SQLite).", 'info', 'COMMENTS')
            except Exception as e:
                crawler._log(f"댓글 크롤러: 초기 데이터 로드 중 오류: {e}", 'error', 'COMMENTS')
        else:
            crawler._log("댓글 크롤러: 초기 데이터 로드를 완료한 상태로 시작합니다.", 'info', 'COMMENTS')

        crawler._log(f"[COMMENTS] 댓글 크롤링 시작 (대상: {len(targets)}개)", 'info', 'COMMENTS')

        # [수정] 시트에서 설정된 인터벌 가져오기 (없으면 4~8분 기본값)
        min_int = int(crawler.settings.get('COMMENTS_CRAWL_INTERVAL_MIN', 240))
        max_int = int(crawler.settings.get('COMMENTS_CRAWL_INTERVAL_MAX', 480))

        # [수정] 현재 메모리에 로드된 데이터(링크)를 추출하여 크롤러에 넘김 (중복 수집 로깅 방지)
        initial_links = set()
        if hasattr(gl, '_comment_data_manager') and gl._comment_data_manager:
            with gl._comment_data_manager.lock:
                if not gl._comment_data_manager.df.empty:
                    # comment_link가 있는 경우만 중복 체크용으로 활용
                    initial_links = set(gl._comment_data_manager.df['comment_link'].dropna().unique().tolist())

        crawl_comments(
            callback=on_comment_received,
            # [v6.7.485] 전역 종료 OR 토글 OFF — 후자면 크롤러가 정상 종료 경로로 빠져나가
            #   `finally` 에서 쿠키 저장 + `driver.quit()` 을 수행한다(메모리 실제 회수).
            stop_event=_StopOrPause(),
            trigger_event=gl.comment_trigger_event,
            log_func=crawler._log,
            targets=targets,
            min_interval=min_int,
            max_interval=max_int,
            initial_seen_entries=initial_links
        )
    except Exception as e:
        crawler._log(f"댓글 크롤러 실행 중 오류가 발생했습니다: {e}", 'error', 'COMMENTS')
        # [v6.7.597] 여기서 예외를 삼키면 supervised 래퍼의 except 까지 못 가서 알림이 영영 안 나간다.
        #   실제로 16일간 그랬다. 삼키는 동작(재진입 보장)은 유지하되 알림만 직접 발화한다.
        _notify_comments_failure(e)
    finally:
        with _contents_crawler_lock:
            _contents_crawler_running = False
        crawler._log("댓글 크롤러가 종료되었습니다.", 'info', 'COMMENTS')


# ─────────────────────────────────────────────────────────────────────────────
# 내용 크롤러 (content_extractor) 헬스체크 및 자동 재시작
# ─────────────────────────────────────────────────────────────────────────────

def _content_log_func(msg, level='info', tag=''):
    """내용 크롤러 전용 로그 래퍼 — crawler 초기화 전(Phase 0.5)에도 안전하게 동작.

    crawler가 준비됐으면 crawler._log로 위임(웹 UI 로그 탭에 표시),
    아직 초기화 중이면 Python logger로 fallback.
    """
    from web_lib.bg_tasks import get_crawler  # 순환 회피
    c = get_crawler(blocking=False)
    if c is not None:
        c._log(msg, level, tag)
        return
    log_level = getattr(logging, level.upper(), logging.INFO)
    logger.log(log_level, f"[{tag}] {msg}" if tag else msg)


def _start_content_extractor_thread():
    """내용 크롤러 스레드를 생성하고 gl에 참조를 저장합니다.

    웹앱 기동 시각을 기준으로 이후 수집된 글만 본문 추출 대상으로 제한한다.
    (Gemma4와 동일하게 구동 이후 데이터만 처리)
    """
    # [중복 호출 방지] 이미 실행 중이면 스킵
    existing = getattr(gl, '_content_extractor_thread', None)
    if existing is not None and existing.is_alive():
        logger.info("[CONTENT] 내용 크롤러 스레드가 이미 실행 중. 재기동 스킵.")
        return

    from content_extractor import run_content_extractor
    from datetime import datetime, timezone

    # SQLite의 strftime('%Y-%m-%d %H:%M:%S','now')는 UTC 기준 → UTC로 맞춤
    # [v6.7.452] 컷오프를 **부팅 시각 - BOOT_LOOKBACK_MIN** 으로. 부팅 시각 그대로 쓰면 **크롤은 됐지만
    #   본문 추출 전에 컨테이너가 재시작된 글이 영구 유실**된다 - `get_posts_without_content` 가
    #   `inserted_at >= since` 로 거르므로 그 글들은 다시는 추출 대상이 되지 않고, 본문이 없으면
    #   AI 요약 대상(`get_posts_pending_ai_summary` 는 content 필수)에서도 영영 빠진다.
    #   **라이브 실측(2026-07-28)**: 부팅 08:37:36 UTC, 컷오프 08:37:35 → 08:36:45 수집분 11건(DC POE1 6 ·
    #   우마무스메 4 · 네이버 배그 1)이 85분째 방치. 조용한 유실이라 화면·로그 어디에도 안 나타난다.
    #   배포 1회당 그 순간의 in-flight 분량이 통째로 사라지는 구조였다(이날 배포 6회).
    #   되돌아보는 창을 두는 이유 = 재시작 창만 덮으면 되므로 짧게. 30분이면 크롤 주기(52~64초)의
    #   30배라 충분하고, 옛 백로그(수십만 행)를 끌어오지 않는다. 쿼리가 `content IS NULL...` 로 이미
    #   거르므로 이 창 안에서도 **진짜 미추출분만** 잡힌다(중복 추출 없음).
    #   부팅 직후 최대 유입량 = 30분 × 실측 5.6건/분 ≈ 170건, 추출 처리량 17.6건/분이라 10분 내 소화.
    if not getattr(gl, '_content_extractor_since', None):
        from datetime import timedelta as _td
        # [v6.7.477] 30 → 360(6시간). 근거는 ai_gemma4_worker 의 같은 상수 주석과 동일 -
        #   평시 잔량 0건(실측)이라 관측된 유실의 수습이 아니라 폭주·CPU 포화 시의 선제 차단이다.
        #   쿼리가 `content IS NULL` 로 이미 미추출분만 거르므로 창 확대 비용은 사실상 0.
        _BOOT_LOOKBACK_MIN = 360
        gl._content_extractor_since = (datetime.now(timezone.utc) - _td(minutes=_BOOT_LOOKBACK_MIN)
                                       ).strftime('%Y-%m-%d %H:%M:%S')
        logger.info(f"[CONTENT] 내용 크롤러 기준 시각: {gl._content_extractor_since} (UTC, "
                    f"부팅 -{_BOOT_LOOKBACK_MIN}분 - 재시작 시 in-flight 글 유실 방지)")

    t = threading.Thread(target=run_content_extractor, kwargs={
        'stop_event': gl.contents_stop_event,
        'log_func': _content_log_func,
        'interval': 5,
        'batch_size': 20,
        'since': gl._content_extractor_since,
    }, daemon=True)
    gl._content_extractor_thread = t
    # [v6.7.302] 기동마다 카운터 리셋 제거 — 지속 실패(드라이버 기동 실패 등) 시 재시작→여기서 0 리셋→
    #   다음 헬스체크 count 항상 0→1→항상 30초라 180/300초 escalation 에 도달 못 하던 버그.
    #   정상 복구 시 리셋은 헬스체크 alive 분기(check_content_extractor_health)가 담당.
    t.start()
    logger.info("[CONTENT] 내용 크롤러 스레드 시작.")


# [v6.7.206] 본문 추출 진행도 stall 경고 임계(초) — alive 지만 이 시간 이상 추출 0 + 대기 글 존재 시 경고.
_CONTENT_STALL_WARN_SEC = 600

# [v6.7.302] 연속 실패 중 카카오 알림 dedup — 사망 감지마다 발화하면 ~90초마다 스팸.
#   첫 사망 시 1회 발화 → 이후 억제, alive 복구 시 리셋(다음 새 실패 streak 는 다시 1회 발화).
_content_extractor_alerted = False


def check_content_extractor_health():
    """내용 크롤러 헬스체크: 스레드가 죽었으면 자동 재시작합니다.

    [v6.7.206] alive 한 경우에도 진행도(content_extractor._LAST_PROGRESS_TS)를 확인해
    'alive 지만 N분간 추출 0 + 대기 글 존재' 면 드라이버 stall 의심 경고를 남긴다(자가복구는 루프 내 self-heal).
    """
    global _content_extractor_alerted
    if gl.contents_stop_event.is_set():
        return

    if gl._content_extractor_thread is not None and gl._content_extractor_thread.is_alive():
        gl._content_extractor_restart_count = 0
        _content_extractor_alerted = False  # [v6.7.302] 복구 → 알림 dedup 리셋

        # [v6.7.206] 진행도 stall 가시화 — alive 지만 N분간 추출 0 + 대기(pending)>0 이면 경고.
        try:
            import content_extractor as _ce
            last = getattr(_ce, '_LAST_PROGRESS_TS', 0.0)
            if last > 0 and (time.monotonic() - last) > _CONTENT_STALL_WARN_SEC:
                from sqlite_handler import db_handler as _db
                if _db.get_posts_without_content(limit=1, since=getattr(gl, '_content_extractor_since', None)):
                    mins = int((time.monotonic() - last) / 60)
                    logger.warning(f"[CONTENT] 본문 추출 {mins}분간 0건(스레드 alive, 대기 글 존재) — 드라이버 stall 의심")
        except Exception:
            pass
        return

    # 스레드가 죽어있음 -> 재시작
    gl._content_extractor_restart_count += 1
    count = gl._content_extractor_restart_count

    if count == 1:
        wait_sec = 30
    elif count == 2:
        wait_sec = 180
    else:
        wait_sec = 300

    wait_label = f"{wait_sec // 60}분" if wait_sec >= 60 else f"{wait_sec}초"
    logger.warning(f"[CONTENT] 비정상 종료 감지 (연속 {count}회). {wait_label} 후 재시작합니다.")
    # [v6.6.341] 본문 크롤러 비정상 종료 감지 시 카카오워크 알림 (재시작은 기존 scheduler 경로)
    # [v6.7.302] 연속 실패 streak 당 1회만 발화 — 사망 감지마다(~90초) 스팸 방지. alive 복구 시 리셋.
    if not _content_extractor_alerted:
        _content_extractor_alerted = True
        _send_crawler_crash_alert(
            "본문 크롤러",
            Exception(f"스레드 비정상 종료 감지 (연속 {count}회). {wait_label} 후 재시작."),
            action=f"재시작 예약({wait_label})",
        )

    scheduler = gl.scheduler
    if scheduler and scheduler.running:
        run_time = dt_class.now() + timedelta(seconds=wait_sec)
        scheduler.add_job(
            _restart_content_extractor,
            trigger='date',
            run_date=run_time,
            id='content_extractor_restart',
            replace_existing=True,
        )


def _restart_content_extractor():
    """지연 후 호출되어 내용 크롤러를 재시작합니다."""
    if gl.contents_stop_event.is_set():
        return
    logger.info("[CONTENT] 내용 크롤러 재시작 시도...")
    _start_content_extractor_thread()
