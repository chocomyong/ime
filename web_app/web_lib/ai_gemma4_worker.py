# -*- coding: utf-8 -*-
"""
Gemma4 요약 APScheduler 워커 (Flask 웹앱 내부 전용)

[역할]
- 20초 주기(v6.7.447~, 구 60초)로 SQLite `community_posts`에서 본문이 채워졌으나 아직 `ai_analysis`에 없는 글을 찾아
  Gemma4로 배치 요약을 수행하고 결과를 SQLite `ai_analysis` 테이블에 Upsert한다.

[설계 포인트]
- Gemma4 동기 HTTP 호출이 APScheduler 스레드를 장시간 점유하지 않도록 전용
  ThreadPoolExecutor(max_workers=1)에 fire-and-forget으로 submit한다.
- 잡 중복 실행 방지: `_running_flag` + APScheduler `max_instances=1` 이중 방어.
- 429 쿼타 연속 5회 실패 시 잡을 5분 pause 후 자동 resume.
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_core_instance = None
_core_lock = threading.Lock()
_summary_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gemma4-summary")
_running_flag = threading.Event()
# [v6.7.205] 워치독 — executor 가 hang(genai 무한 block 등)하면 _running_flag 가 영구 set 되어
#   재시작 전까지 AI 요약이 영영 멈춘다(REMOTE_KILL 아닌 silent-death). 마지막 사이클 시작 후
#   _CYCLE_HANG_TIMEOUT 초가 지나도 안 끝나면 hang 으로 간주 → executor 재생성 + flag 강제 복구.
#   (google-genai 1.63 HttpOptions(timeout) 은 #911/#681 미적용 버그라 SDK timeout 에 의존 불가 → 워치독이 정본 방어.)
_last_cycle_start = 0.0
# [v6.7.679] **워치독은 '느림' 이 아니라 '멈춤' 을 재야 한다.** 종전 상수 주석은 *"정상 사이클은
#   수 초"* 였는데 그 전제가 낡았다 - 캡이 `BATCH_SIZE * BATCH_PARALLEL`(=40)로 커지고(v6.7.652)
#   벤더가 나쁘면 콜당 60~104초 + 429 키교체(retryDelay 15~49초) + 503 모델 쿨다운(120초)이 붙어
#   **정상적으로 일하는 사이클이 1,468~2,214초** 다(2026-09-18 라이브 실측). 600초로는 그 사이클을
#   전부 hang 으로 오판했고, 파이썬은 스레드를 못 죽이므로(아래 abandon) **옛 사이클이 살아 있는
#   채로 새 사이클이 떠서** 동시 콜이 2~3배가 됐다. 그 동시성이 분당 입력토큰 쿼타
#   (`GenerateContentInputTokensPerModelPerMinute`)를 더 먹어 429 를 늘리는 **되먹임**이었다.
#   그래서 두 가지를 바꾼다: ① 기준을 사이클 시작이 아니라 **마지막 진척 신호**(`_last_progress`)로
#   ② 임계를 실측 최악(2,214초) 위로. 이러면 '느리지만 일하는' 사이클은 안 건드리고
#   genai 무한 block(원래 방어 대상, v6.7.205)은 그대로 잡는다.
_last_progress = 0.0        # 사이클 시작 + 배치 1개 끝날 때마다 갱신 = "살아 있다" 신호
_CYCLE_HANG_TIMEOUT = 2700  # 45분간 **진척이 0** 이면 hang. 느린 사이클은 배치 완료로 계속 갱신된다.
_cycle_seq = 0              # 사이클 일련번호 - 버려진 사이클의 콜백을 식별한다
_current_seq = 0
_abandoned_running = 0      # 워치독이 버렸지만 아직 스레드가 살아 있는 사이클 수

_QUOTA_BACKOFF_THRESHOLD = 5
_consecutive_quota_failures = 0
_quota_lock = threading.Lock()

# [v6.6.228] 컷오프 복구 — 웹서버 가동 이후 수집된 글만 요약 (v6.6.220의 원래 정책).
# v6.6.224에서 운영 서버 462건 일회성 백필 위해 잠시 None으로 비활성했으나, 백필 완료 후 복구.
# [v6.7.203→205] 컷오프 = 워커 부팅(= 모듈 import) 이후 '수집된'(inserted_at) 글만 요약.
#   v203/204 는 게시판 표시시각(community_posts.timestamp) 기준이라 '부팅 직전 게시 + 부팅 후 수집'된
#   글을 놓쳤다(게시시각 < 부팅 < 수집시각). v205 부터 content_extractor 와 동일하게 수집시각(inserted_at)
#   기준으로 정합 — inserted_at 는 SQLite strftime('%Y-%m-%d %H:%M:%S','now')(UTC, 공백 19자)로 저장되므로
#   _WORKER_START_TIME 도 동일 포맷(UTC strftime)으로 맞춰 raw 문자열 비교(get_posts_pending_ai_summary)를 결정적으로 만든다.
#   (v203 tz-aware +09:00 / v204 마이크로초 정합은 이 inserted_at 전환으로 자연 흡수 — 게시시각 비교 자체를 폐기.)
# [v6.7.452] 컷오프를 **부팅 시각 - BOOT_LOOKBACK_MIN** 으로. 부팅 시각 그대로면 재시작 순간
#   '수집됐고 본문도 있는데 아직 요약 안 된' 글이 **영구 스킵**된다(조회가 `p.inserted_at >= since`).
#   같은 커밋에서 content_extractor 도 같은 이유로 고쳤다 - 두 파이프라인이 같은 결함을 공유해
#   재시작 한 번에 in-flight 글이 **양쪽에서 동시에** 버려지고 있었다.
#   30분인 이유: 재시작 창만 덮으면 되고, 조회가 `a.link IS NULL` 로 이미 미요약분만 거르므로
#   이 창 안에서도 중복 요약이 생기지 않는다. 창을 길게 잡으면 부팅 직후 쿼타 소모가 튄다.
# [v6.7.477] 30 → 360(6시간). **평시엔 이 창이 남아돈다** - 라이브 실측(2026-08-03) 미요약 잔량
#   0건, 유입 최대 5.1건/분 대 처리량 25.8건/분(가동률 0.2). 즉 이 상향은 관측된 유실의 수습이
#   아니라 **조건부 위험의 선제 차단**이다: 크롤 백로그 폭주(2026-07-25 1,400건/시)나 CPU 포화
#   (PATTERN-31)로 처리량이 급락하면 큐가 30분을 넘길 수 있고, 그 상태에서 배포하면 초과분이
#   컷오프 밖으로 밀려 **영구 유실**된다(디코에서 실측된 경로 - 그쪽은 가동률>1 이라 상시 조건이었다).
#   비용은 사실상 0 - 조회가 `a.link IS NULL` 로 이미 미처리분만 거르므로 창을 넓혀도 중복 요약이
#   생기지 않고, `idx_posts_inserted_at` 범위 탐색이라 스캔량도 큐 크기에 비례한다.
#   큐가 실제로 쌓이는지는 같은 커밋의 `queue_summary_pending` 지표가 말해준다.
# [v6.7.656] **360 -> 30 으로 되돌린다**(사용자 결정 2026-09-10 *"IME 소급 필요없어"*).
#   같은 날 IMT 에 세운 규약과 맞춘다: **켠 뒤 들어온 글만 본다**(`AME-C v0.1.66~69`).
#   위 v6.7.477 의 논리는 *"조건부 위험의 선제 차단"* 이었고 그 전제(잔량 0 · 가동률 0.2)는
#   지금도 유효하다 - 즉 **이 값을 줄여서 잃는 것은 그 조건부 보험뿐**이고, 사용자는 그 보험을
#   원하지 않는다고 명시했다(*"과거 ai요약 대상은 필요없어 다 버려도돼"*). 오래 죽어 있다 살아난
#   컨테이너가 6시간치를 한꺼번에 태우는 쪽이 이제는 손해다.
#
#   **0 으로 두지 않는 이유는 v6.7.452 가 실측으로 남겨 뒀다** - 컷오프를 부팅 시각 그대로 쓰면
#   재시작 순간 *"수집됐고 본문도 있는데 아직 요약 안 된"* 글이 **영구 스킵**된다. 배포가 잦은
#   앱이라 그 유실은 일회성이 아니라 **배포마다 반복**된다. 30분은 그 재시작 창을 덮는 값이지
#   소급이 아니다 - 평시 잔량 0 · 처리량 25.8건/분이라 30분이면 큐는 사실상 비어 있다.
#   **되돌리는 것은 477 의 확대분뿐이고 452 의 수리는 그대로 산다.**
_BOOT_LOOKBACK_MIN = 30
_skip_logged_for = None   # [v6.7.453] 같은 사이클에 대한 '틱 스킵' 로그 1회 제한
# [v6.7.484] 저장 실패 후 LLM 재지출 유예. 저장이 실패하면 그 글들은 pending 으로 남아
#   다음 사이클이 **같은 글을 다시 LLM 에 태운다** — 락 경합처럼 원인이 몇 분 지속되는
#   상황에서는 그게 20초마다 반복되며 쿼타만 태운다(2026-08-05 실측: 부팅 폭풍 중 같은
#   3건이 9회 반복, Gemini 콜 12회). 대기는 공짜지만 재요약은 비싸므로 잠시 쉰다.
_SAVE_FAIL_COOLDOWN = 120
_save_fail_until = 0.0
_save_fail_logged = False
_WORKER_START_TIME = (datetime.now(timezone.utc)
                      - timedelta(minutes=_BOOT_LOOKBACK_MIN)).strftime('%Y-%m-%d %H:%M:%S')
logger.info(f"[GEMMA4] 요약 컷오프 = {_WORKER_START_TIME} (이 수집시각[inserted_at, UTC] 이후 글만 요약, "
            f"부팅 -{_BOOT_LOOKBACK_MIN}분 - 재시작 시 in-flight 글 유실 방지)")


def _get_core():
    """Gemma4SummaryCore 싱글턴 지연 생성."""
    global _core_instance
    with _core_lock:
        if _core_instance is None:
            try:
                from ai_lib.gemma4_summary_core import Gemma4SummaryCore
                _core_instance = Gemma4SummaryCore()
            except Exception as e:
                logger.error(f"[GEMMA4] Core 초기화 실패: {e}")
                return None
        return _core_instance


def _is_quota_error(exc: Exception) -> bool:
    msg = str(exc)
    return "429" in msg or "quota" in msg.lower()


def _handle_quota_failure():
    """쿼타 연속 실패 카운트를 증가시키고 임계치 도달 시 잡을 5분 pause."""
    global _consecutive_quota_failures
    with _quota_lock:
        _consecutive_quota_failures += 1
        if _consecutive_quota_failures < _QUOTA_BACKOFF_THRESHOLD:
            return
        _consecutive_quota_failures = 0

    try:
        import web_lib.globals as gl  # 지연 import (순환 방지)
        scheduler = getattr(gl, 'scheduler', None)
        if scheduler is None:
            return
        # [v6.7.205] FIX: resume 예약을 pause 보다 먼저 — add_job 이 실패해도 pause 가 적용 안 돼
        #   잡이 영구 정지(silent-death)하는 것을 방지. (resume_job 은 비-pause 잡엔 no-op 이라 순서 역전 안전.)
        resume_at = datetime.now() + timedelta(minutes=5)
        scheduler.add_job(
            lambda: scheduler.resume_job('gemma4_summary_worker'),
            'date',
            run_date=resume_at,
            id='gemma4_summary_resume',
            replace_existing=True,
        )
        scheduler.pause_job('gemma4_summary_worker')
        logger.warning(f"[GEMMA4] 쿼타 연속 실패 — 5분 pause (재개: {resume_at:%H:%M:%S})")
    except Exception as e:
        logger.error(f"[GEMMA4] pause/resume 예약 실패: {e}")


def _reset_quota_failures():
    global _consecutive_quota_failures
    with _quota_lock:
        _consecutive_quota_failures = 0


_quota_notified_date = None


def _notify_quota_exhausted(n):
    """[v6.7.436] 일일 Gemini 예산 소진을 알림 보관소(종)에 1일 1회 적재.

    2026-07-25 사고의 본질은 '멈춘 것' 자체보다 **멈춘 걸 알 수 없었던 것**이다 - 카카오워크 AI 요약
    알림은 실 요약이 있을 때만 발화하므로, 요약이 0건이 되면 알림도 함께 사라져 정상과 구분되지 않는다.
    (KST 자정 카운터 리셋 전까지 자동 회복이 안 되므로) 소진 사실만이라도 웹 종에 남긴다.
    """
    global _quota_notified_date
    try:
        today = datetime.now(ZoneInfo('Asia/Seoul')).date()
        if _quota_notified_date == today:
            return
        _quota_notified_date = today
        from web_lib.bg.notif_store import record
        record('notice', 'AI 요약 일시 중단',
               f'오늘 Gemini 요약 호출 예산이 소진돼 {n}건이 대기 중입니다. 자정(KST) 카운터 리셋 후 자동 재개됩니다.',
               '/?tab=ai-summary')
        logger.warning(f"[GEMMA4] 일일 Gemini 예산 소진 - 알림 적재 (대기 {n}건, 자정 리셋까지 요약 정지)")
    except Exception as e:
        logger.warning(f"[GEMMA4] 예산 소진 알림 실패 (무시): {e}")


# ─────────────────────────────────────────────────────────────────────────────
# [v6.7.593] 제보 게시판 개인 신고 글 — 요약 전 결정적 제외
# ─────────────────────────────────────────────────────────────────────────────
# 배그 공식카페 '🚨┃제보 게시판'은 v6.7.590 의 회원 쿠키로 본문이 열리면서 요약 대상이 됐는데,
# **글의 92%가 특정 이용자 신고**다(전수 1,935건: 팀킬티밍 671 · 불법이용의심 416 · 기타위반 314 ·
# 말머리없음 377[표본 18건 중 17건이 신고] vs 버그오류 152). 이 글들을 요약하면:
#
#   1. **정보가 0 추가된다** — "[기타 위반 제보]욕설 제보" → "특정 이용자(X)의 욕설 사용 현상 제보".
#      제목의 재진술이고, 강제 종결어미가 "욕설 제보" 를 "욕설 사용 현상 제보" 로 부풀린다.
#   2. **등급이 오염된다** — 8/19 이후 요약 24건 중 warning 12 · critical 1. 개인 신고 하나가
#      critical 을 받았다(서버 다운급 등급). 진짜 경보가 그 사이에 묻힌다.
#   3. **미확인 혐의와 개인 식별자가 박제된다** — 요약문에 게이머 닉네임이 그대로 들어가
#      AI 요약 탭·카카오워크에 남는다. 제보는 주장이지 확정이 아니다.
#   4. **판정이 흔들린다** — 같은 성격의 '[불법 이용 의심]' 글이 회차마다 critical / [분석 제외]
#      로 갈린다(PATTERN-49). LLM 분류에 맡길 게 아니라 규칙으로 못 박아야 하는 자리다.
#
# **수집은 그대로 둔다** — 제보 *건수 급증* 자체는 핵 유행 같은 신호이고 이상감지가 건수로 본다.
# 자르는 것은 "요약·알림"이지 "수집"이 아니다. 건의·자유·질문 게시판은 손대지 않는다(사용자:
# "건의나 게임 내용 관련 동향으로 취합할 필요가 있는 건 요약해도 된다").
#
# 규칙(배그 제보 게시판 한정): 말머리가 신고 3종이면 제외. 말머리가 없으면 제목의 신고성 토큰으로
# 판정 — 그래야 "네트워크 지연 무엇?" 같은 **장애성 글은 살아남는다**(표본 18건에서 16건 제외,
# 장애글 1건 통과, 1건 놓침[닉네임 나열형]. 놓치면 요약될 뿐이라 손실이 작다).
_REPORT_BOARD_GAME = '배틀그라운드'
_REPORT_BOARD_HINT = '제보'
_REPORT_EXCLUDE_HEADS = ('[팀킬티밍', '[불법 이용', '[기타 위반')
_REPORT_EXCLUDE_TOKENS = ('신고', '팀킬', '티밍', '핵쟁이', '핵버스', '욕설', '어뷰저',
                          'esp', '에임봇', '오토에임', '에임핵')
_REPORT_EXCLUDE_SUMMARY = '[분석 제외] 개인 신고 글'

# [v6.7.596] 제재·정지 동향은 **카카오 건일 때만** 요약한다 (사용자 2026-08-20 지시).
#   이 게시판의 제재 글(제목 기준 138/1,935건)은 대부분 **우리 소관이 아니다** —
#   같은 날 옆 세션의 무고밴 조사가 그 구조를 실측했다
#   (`_workspace/2026-08-20_pubg_mugoban_trend_investigation.md` §3-2):
#     - **제보 게시판 양식에 "스팀 이용자 전용" 이 명시**돼 있고 카카오 이용자는 다른 창구를 쓴다
#     - 그래서 표본이 "스팀 33 : 카카오 1" 로 나오는데 **그건 실태가 아니라 창구 구조의 결과**다
#   즉 여기서 제재 동향을 통째로 요약하면 **남의 플랫폼 민원을 우리 리포트에 싣는 것**이 된다.
#   카카오 건이면 남긴다 — 그건 실제로 우리 운영 신호다(무고밴·기기제재 오탐 등).
#
#   **판별은 제목만으로 불가능하다**: 제재 글 138건 중 **제목에 '카카오' 가 있는 건 0건**이었다
#   (2026-08-20 전수 실측). 그래서 제목+본문을 함께 본다. 게이트는 **제목의 제재 토큰**으로 열어
#   오탐을 줄인다(본문에 '정지' 가 스치는 버그 글이 끌려 들어오지 않게).
_REPORT_BAN_TOKENS = ('정지', '제재', '밴', '복구', '이용제한', '차단')
_REPORT_KAKAO_TOKENS = ('카카오', '카겜')


def _is_personal_report(article):
    """배그 제보 게시판에서 **요약하지 않을 글**인가.

    ① 신고 말머리 3종 = 개인 신고 → 제외 (카카오든 아니든 우리가 조치할 게 없다)
    ② 제재·정지 주제 → **카카오 건일 때만 요약**, 아니면 제외 (위 주석의 창구 구조)
    ③ 말머리 없는 신고성 제목 → 제외
    ④ 나머지(버그·오류·장애·동향·건의) → 요약
    """
    if (article.get('game') or '') != _REPORT_BOARD_GAME:
        return False
    if _REPORT_BOARD_HINT not in (article.get('board_name') or ''):
        return False
    title = (article.get('title') or '').strip()
    if title.startswith(_REPORT_EXCLUDE_HEADS):
        return True
    if any(b in title for b in _REPORT_BAN_TOKENS):
        blob = f"{title} {article.get('content') or ''}"
        return not any(k in blob for k in _REPORT_KAKAO_TOKENS)
    if title.startswith('['):
        return False        # `[버그오류 제보]` 등 — 신고 말머리가 아니면 통과
    low = title.lower()
    return any(t in low for t in _REPORT_EXCLUDE_TOKENS)


def _split_personal_reports(pending):
    """요약 대상 pending 을 (요약할 것, 결정적 제외할 것) 으로 가른다."""
    keep, drop = [], []
    for a in pending:
        (drop if _is_personal_report(a) else keep).append(a)
    return keep, drop


def run_gemma4_summary_cycle():
    """APScheduler가 20초 주기로 호출하는 엔트리 (fire-and-forget). 주기는 app.py 잡 등록부가 정본."""
    global _last_cycle_start, _summary_executor, _cycle_seq, _current_seq, _abandoned_running, _last_progress
    if _running_flag.is_set():
        # [v6.7.205] 워치독 — executor hang 으로 flag 가 영구 set 되었는지 검사.
        #   _CYCLE_HANG_TIMEOUT 초 초과 시 hang 으로 간주 → executor 재생성(hang 스레드 abandon: Python
        #   은 thread kill 불가, 새 executor 가 후속 처리) + flag 강제 clear 후 아래로 흘러 새 사이클 제출.
        # [v6.7.679] 기준을 `_last_cycle_start` -> `_last_progress` 로. 위 상수 주석 참조.
        if _last_progress and (time.monotonic() - _last_progress) > _CYCLE_HANG_TIMEOUT:
            _stuck = int(time.monotonic() - _last_progress)
            _abandoned_running += 1
            logger.error(f"[GEMMA4] 사이클 hang 감지 (진척 없이 {_stuck}s) - executor 재생성 + flag 복구 "
                         f"(버려진 사이클 {_abandoned_running}개가 아직 살아 있을 수 있다)")
            try:
                _summary_executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
            _summary_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gemma4-summary")
            _running_flag.clear()
        else:
            # [v6.7.453] 스킵을 DEBUG 로만 남기면 **긴 사이클이 로그에서 통째로 사라진다** - 실제로
            #   18:46:35 → 18:51:17 사이 4분 42초 공백이 관측됐는데 그 사이 무슨 일이 있었는지
            #   추적할 근거가 0이었다. 60초를 넘긴 사이클만 INFO 로 1회 올린다(20초마다 스팸 방지).
            _el = (time.monotonic() - _last_cycle_start) if _last_cycle_start else 0
            global _skip_logged_for
            if _el > 60 and _skip_logged_for != _last_cycle_start:
                _skip_logged_for = _last_cycle_start
                logger.info(f"[GEMMA4] 사이클 {int(_el)}초 진행 중 — 틱 스킵 (처리 시간이 주기를 초과)")
            else:
                logger.debug("[GEMMA4] 이전 사이클 진행 중 — 스킵")
            return
    _running_flag.set()
    _last_cycle_start = time.monotonic()
    _last_progress = _last_cycle_start
    _cycle_seq += 1
    _current_seq = _cycle_seq
    try:
        future = _summary_executor.submit(_cycle_impl)
        future.add_done_callback(lambda _f, _s=_current_seq: _on_cycle_done(_s))
    except Exception as e:
        _running_flag.clear()
        logger.error(f"[GEMMA4] 사이클 제출 실패: {e}")


def _on_cycle_done(seq):
    """[v6.7.679] **버려진 사이클의 콜백이 남의 flag 를 지우지 못하게 한다.**

    워치독이 flag 를 강제 복구해도 옛 스레드는 계속 산다(파이썬은 스레드 kill 불가 - 위 abandon).
    종전 콜백은 `lambda _f: _running_flag.clear()` 라 **그 유령이 끝나는 순간 새 사이클의 flag 를
    지웠다.** 그러면 20초 뒤 틱이 또 하나를 띄워 겹침이 누적된다 - 2026-09-18 라이브에서 사이클
    2개가 **51초 간격으로 완료**된 것이 그 흔적이다(각각 2,037초·1,468초짜리).
    일련번호가 현재 사이클일 때만 flag 를 내린다.
    """
    global _abandoned_running
    if seq == _current_seq:
        _running_flag.clear()
    else:
        _abandoned_running = max(0, _abandoned_running - 1)
        logger.info(f"[GEMMA4] 버려진 사이클 #{seq} 뒤늦게 종료 - flag 는 건드리지 않는다 "
                    f"(남은 유령 {_abandoned_running}개)")


def _in_save_fail_cooldown():
    """저장 실패 직후 쿨다운 창이면 True (그 사이 LLM 재호출을 막는다)."""
    # [v6.7.484] 직전 사이클이 저장에 실패했으면 잠시 쉰다. 대상 글은 pending 으로 남아 있으므로
    #   유예가 끝나면 그대로 재시도된다(유실 0) — 잃는 것은 지연뿐이고 아끼는 것은 LLM 쿼타다.
    global _save_fail_logged
    if _save_fail_until and time.monotonic() < _save_fail_until:
        if not _save_fail_logged:
            _save_fail_logged = True
            logger.info(
                f"[GEMMA4] 직전 저장 실패로 {_SAVE_FAIL_COOLDOWN}초 유예 — LLM 호출 스킵 "
                f"(대상 글은 pending 유지, 유예 후 재시도)"
            )
        return True
    return False


def _exclude_personal_reports(db_handler, pending):
    """제보 게시판 개인 신고 글을 LLM 전에 결정적으로 제외하고, 남은 요약 대상을 돌려준다."""
    # [v6.7.593] 제보 게시판 개인 신고 글 — LLM 에 보내기 **전에** 결정적으로 제외한다(위 주석의 근거).
    #   기존 '[분석 제외]' 규약을 그대로 쓴다: 알림·배지 술어가 이미 이 문자열을 거르므로 새 경로가
    #   필요 없고(계단 2), ai_analysis 에 남기므로 pending 에서 빠져 매 사이클 재조회되지 않으며,
    #   화면에도 "왜 요약이 없는지" 가 보인다. 저장 실패는 삼킨다 — 다음 사이클에 다시 만난다.
    pending, _personal = _split_personal_reports(pending)
    if _personal:
        for r in _personal:
            r['summary'] = _REPORT_EXCLUDE_SUMMARY
            r['sentiment'] = 'neutral'
            r['severity'] = 'info'
            r['AI_Score'] = 0.0
        if db_handler.save_ai_analysis(_personal):
            logger.info(f"[GEMMA4] 제보 게시판 개인 신고 {len(_personal)}건 — 요약 제외 (LLM 호출 0)")
        else:
            logger.warning(f"[GEMMA4] 제보 제외 {len(_personal)}건 저장 실패 — 다음 사이클 재시도")
    return pending


def _log_queue_depth(db_handler, pending, _lim):
    """요약 대기 로그. 상한에 걸렸을 때만 진짜 잔량을 세서 함께 찍는다(평시 비용 0)."""
    # [2026-09-16] **상한에 걸렸으면 진짜 잔량을 같이 찍는다.** `len(pending)` 은 조회 상한
    #   (`BATCH_SIZE * BATCH_PARALLEL`)에 잘린 값이라, 백로그가 40이든 95든 로그에는 똑같이
    #   `40건` 으로 보인다 - **잔량이 커지는 것을 로그로 못 본다.** 라이브 실측 2026-09-16
    #   18:00~18:30: 로그는 `40건` 으로 30분간 평평했는데 실제는 95건 · 밀림 74.5분이었고,
    #   그 구간의 요약 지연(수집 후 28~60분)을 로그만으로는 설명할 수 없었다.
    #   **평시엔 이 조회가 아예 안 나간다** - 잔량이 상한 밑이면 `len(pending) < _lim` 이다.
    _depth = db_handler.count_posts_pending_ai_summary(since=_WORKER_START_TIME) \
        if len(pending) >= _lim else len(pending)
    logger.info(f"[GEMMA4] 요약 대기 {len(pending)}건"
                + (f" (조회 상한 {_lim} - 실제 잔량 {_depth}건)" if _depth > len(pending) else "")
                + f" - 배치 시작 (since={_WORKER_START_TIME})")


def _run_batches(core, batches, batch_parallel):
    """배치들을 병렬(또는 순차)로 실행하고 (결과 목록, 사용 워커 수) 를 돌려준다."""
    def _run_batch(batch):
        """배치 1개 실행. 반환 = (batch, 예외 or None). 예외는 여기서 삼키지 않고 올려서
        호출부가 기존과 **동일한 쿼타 판정 경로**(_is_quota_error → _handle_quota_failure)를 타게 한다."""
        global _last_progress
        try:
            core._generate_summary_batch(batch, defer_second=True)
            return batch, None
        except Exception as e:      # noqa: BLE001 - 판정은 아래에서
            return batch, e
        finally:
            # [v6.7.679] 워치독의 '살아 있다' 신호. 실패해도 갱신한다 - 우리가 재는 것은
            #   **성공 여부가 아니라 멈춤 여부**다(실패도 벤더 응답을 받았다는 뜻이다).
            _last_progress = time.monotonic()

    # [v6.7.454] 배치 병렬 실행. 순차였을 때 피크 가동률이 0.98 이었다(측정:
    #   _workspace/2026-07-28_ai_summary_capacity_model.md). 1차 콜은 원격 API 대기라 스레드가
    #   GIL 을 놓고 잠들므로 2코어 NAS 에서도 CPU 부담이 낮다(PATTERN-31 의 Selenium 부하와 다름).
    #   BATCH_PARALLEL=1 이면 이 경로도 배치를 하나씩 돌려 v6.7.453 이전과 동일 동작이 된다.
    #   `with` 블록은 전 배치 완료까지 블록한다 - 한 배치가 hang 하면 사이클이 늘어지지만, 그건
    #   순차였을 때와 같고 최종 방어선은 기존 워치독(_CYCLE_HANG_TIMEOUT 600s)이 그대로 맡는다.
    _workers = max(1, min(batch_parallel, len(batches)))
    if _workers > 1:
        with ThreadPoolExecutor(max_workers=_workers,
                                thread_name_prefix="gemma4-batch") as _bex:
            _results = list(_bex.map(_run_batch, batches))
    else:
        _results = [_run_batch(b) for b in batches]
    return _results, _workers


def _flush_second_stage(core, _batch_n):
    """2차 통합은 사이클당 1콜 - 저장·알림보다 먼저 불러 summary 를 붙인다."""
    # [v6.7.680] **2차는 사이클당 1콜.** 배치는 1차만 하고 대상을 모아 두었다가 여기서 한 번에
    #   보낸다 - 2차 flash 는 RPD 20/키/모델 이라 콜 수가 곧 일일 천장이고, 그 콜들은
    #   `_gemini_lock` 으로 어차피 직렬이라 합쳐도 벽시계 손해가 없다(품질 변화 0).
    #   **저장·알림보다 먼저** 불러야 한다 - 여기서 summary 가 붙는다.
    try:
        _merged = core.flush_deferred_second()
        if _merged:
            logger.info(f"[GEMMA4] 2차 통합 {_merged}건 - 콜 1회 (종전 배치 {_batch_n}회)")
    except Exception as _e:
        logger.error(f"[GEMMA4] 2차 통합 호출 실패: {_e}")


def _collect_results(_results):
    """배치 결과를 (저장할 성공분, 대기 건수, 소진 건수) 로 분류한다."""
    success_records = []
    deferred_n = 0        # [v6.7.436] 이번 사이클에서 2차 요약을 못 받고 대기로 밀린 글 수
    exhausted_n = 0       # [v6.7.436] 그 중 '전 모델 RPD 소진' 사유
    for batch, err in _results:
        if err is not None:
            if _is_quota_error(err):
                _handle_quota_failure()
            else:
                logger.error(f"[GEMMA4] 배치 실패: {err}")
            continue
        _reset_quota_failures()

        for r in batch:
            s = (r.get('summary') or '').strip()
            # [v6.7.205] FIX: 일과성 실패 placeholder('(...)' 형 — _stamp_failure)는 저장하지 않는다.
            #   ai_analysis 에 link 로 박히면 a.link IS NULL dedup 가드로 쿼타 자정 회복 후에도 영구
            #   재요약이 차단됨. 저장 생략 → 다음 사이클 자연 재시도. '분석 제외'(영구 제외 의도)는 보존.
            if s and not (s.startswith('(') and s.endswith(')')):
                success_records.append(r)
            elif s:
                # [v6.7.436] '(...)' 스탬프 = 저장 생략(= 다음 사이클 재시도)된 글. 그동안 이 상태는
                #   로그 한 줄 없이 조용해서, 2026-07-25 처럼 17시간 요약 0건이어도 운영자가 알 길이
                #   없었다(사용자: "어제 새벽부터 ai 요약이 멈췄는데 몰랐다"). 카운트해서 아래에서 발화.
                deferred_n += 1
                if 'RPD 소진' in s:
                    exhausted_n += 1
    return success_records, deferred_n, exhausted_n


def _emit_deferred_alerts(deferred_n, exhausted_n):
    """대기·소진을 로그/알림으로 가시화한다 (침묵이 사고를 오래 끌던 지점)."""
    # [v6.7.436] 대기/소진 가시화 - 저장 0건인데 대기만 쌓이는 상태(= 화면상 'AI 요약 정지')를 로그로
    #   드러내고, 일일 예산 소진은 알림 보관소(종)로 1일 1회 올린다. 침묵이 사고를 17시간 끌었던 지점.
    if deferred_n:
        # [v6.7.610] "2차 요약 대기" 였는데, 이제 **1차 응답 누락분도 같은 괄호형 스탬프로**
        #   여기 들어온다(그쪽도 저장 생략 = 다음 사이클 재시도라 처리가 같다). 문구가 사유를
        #   하나로 단정하면 로그만 보고 원인을 오판한다 - 세부 사유는 `[Gemma4Core]` 줄에 있다.
        # [v6.7.618] "그 외" 에 **시간당 페이싱/429 쿨다운**을 명시한다. 2026-08-28 에 이 줄이
        #   `일일예산 소진 11건` 이라고 했는데 그때 일일 잔여는 130콜(36%)이었다 - 진짜 차단자는
        #   페이싱이었고, 이 줄만 본 사람은 키 증설로 갔을 것이다. 코어가 이제 사유별로 다른
        #   스탬프를 찍으므로(`(전 모델 RPD 소진 ...)` vs `(2차 대기: 페이싱/쿨다운 ...)`)
        #   `exhausted_n` 은 **일일 예산이 실제로 0 일 때만** 오른다.
        logger.warning(f"[GEMMA4] 요약 대기 {deferred_n}건 (일일예산 소진 {exhausted_n}건, "
                       f"그 외 시간당 페이싱·429 쿨다운·1차 응답 누락·2차 미수신) "
                       f"- 저장 생략, 다음 사이클 재시도")
    if exhausted_n:
        _notify_quota_exhausted(exhausted_n)


def _persist_and_notify(db_handler, success_records):
    """저장이 성공했을 때만 캐시 refresh·소켓 emit·웹푸시로 이어간다 (실패면 후속 전부 생략)."""
    global _save_fail_until, _save_fail_logged
    # [v6.7.484] 저장 성공 여부를 실제로 본다. 종전에는 반환값을 안 보고 무조건 성공 로그를
    #   찍은 뒤 그 위에서 캐시 리프레시·배지 emit·**폰 웹푸시**·상황판 합성까지 이어갔다 —
    #   저장이 실패해도 알림은 나가고, 눌러도 그 글이 없다. 실패면 후속을 전부 건너뛴다.
    if not db_handler.save_ai_analysis(success_records):
        _save_fail_until = time.monotonic() + _SAVE_FAIL_COOLDOWN
        _save_fail_logged = False
        logger.error(
            f"[GEMMA4] ai_analysis 저장 실패 {len(success_records)}건 — "
            f"후속(캐시/소켓/웹푸시/상황판) 생략, {_SAVE_FAIL_COOLDOWN}초 뒤 재시도"
        )
        return
    _save_fail_until = 0.0
    logger.info(f"[GEMMA4] ai_analysis 저장 {len(success_records)}건")

    # [v6.7.381] '실 요약'(분석 제외/빈/placeholder 제외) 레코드만 추림. 배지·인앱배너·폰푸시 전부
    #   이것만 대상 → 분석 제외만 있는 배치는 알림/카운트 자체를 안 만든다 (사용자: "분석 제외만
    #   세면 오히려 필요없는 알림"). 카카오 발송 게이트(gemma4_summary_core 3차 순회)와 동일 술어.
    def _notable(_sm):
        _sm = (_sm or '').strip()
        return bool(_sm) \
            and not (_sm.startswith('[') and _sm.endswith(']')) \
            and not (_sm.startswith('(') and _sm.endswith(')')) \
            and '분석 제외' not in _sm and '분석제외' not in _sm
    _notable_records = [r for r in success_records if _notable(r.get('summary'))]

    # [v6.7.328] 저장 → 응답 캐시 무효화(전건 - 디버그 ai-analysis 탭도 포함) + 클라 소켓 신호.
    #   클라('ai_analysis_update')는 배지 증가 + 활성 탭 refetch. broadcast 엔 count 신호만(본문은 admin API).
    try:
        # [v6.7.437] invalidate -> refresh(웜 리필). 저장 직후 소켓 푸시를 받은 클라의 refetch 가
        #   비워진 캐시를 맞아 매번 콜드 윈도우 쿼리를 물던 것(= AI 요약 탭만 늦게 뜨던 체감)을 제거 -
        #   무거운 계산은 이 워커 스레드(요청 경로 밖)에서 끝내고 결과만 swap 한다. 실패 시 내부에서
        #   invalidate 폴백 후 예외 → 아래 except 가 흡수(사이클 진행 무영향).
        from web_lib.routes.analysis import refresh_ai_caches
        refresh_ai_caches()
    except Exception as cache_err:
        logger.warning(f"[GEMMA4] AI 응답 캐시 갱신 실패 (무시): {cache_err}")
    # [v6.7.381] 실 요약 0(분석 제외만) 이면 emit 생략 → 배지/배너/refetch 트리거 안 됨. new_count=실요약수.
    try:
        import web_lib.globals as gl
        if getattr(gl, 'socketio', None) and _notable_records:
            gl.socketio.emit('ai_analysis_update', {'new_count': len(_notable_records)})
    except Exception as emit_err:
        logger.warning(f"[GEMMA4] ai_analysis_update 푸시 실패 (무시): {emit_err}")

    # [v6.7.360/367/374] PWA Web Push 미러 - 카카오워크 AI 알림 형식·단위.
    #   [v6.7.374] 제목 "[게임] AI 요약" + 본문 = 요약문(길면 폰이 여러 줄 wrap - 카카오 표시감).
    #   글 제목(article title)은 제거(사용자 2026-07-18: "글 제목 필요없고 [게임명] ai요약 정도면").
    #   건별 발송 + 클릭 시 원문 링크. 제외 술어 = 카카오 발송 게이트(gemma4_summary_core 3차 순회)
    #   그대로: sentinel([..]) / placeholder((..)) / '분석 제외' 포함 3중. 구독 0 이면 send_push 즉시 반환.
    try:
        from web_lib.bg.web_push import send_push
        from web_lib.bg.notif_store import record   # [v6.7.427] 서버 보관소 적재(구독 무관)
        for _r in _notable_records:   # [v6.7.381] 실 요약만 (분석 제외 등은 위에서 이미 제외)
            _g = _r.get('game') or ''
            _s = (_r.get('summary') or '').strip()
            # [v6.7.427] 보관소 적재 = 구독 무관 무조건(웹 종 아카이브), 아래 send_push = 구독 있을 때만(폰).
            #   title 빈값 - 게임은 배지가, 내용은 body(요약문)가 표시하므로 "[게임]" 중복 제거.
            # [v6.7.431] link=원문 URL(p.link) 전달 → 종 클릭 시 AI 요약 '탭'(admin 전용) 대신 원문 글로
            #   이동(매니저도 글 열람). url(?tab= 딥링크)은 그대로 두고 link 만 추가 — CCU/워처는 link 없이
            #   기존 딥링크 유지. _r 은 get_posts_pending_ai_summary 레코드라 'link'(=p.link) 보유.
            record('ai', '', _s, '/?tab=ai-summary', game=_g, link=_r.get('link') or '')
            # [v6.7.378] 제목=[게임], 본문=요약문. 요약을 본문에 두는 이유: 안드로이드는 제목을 1줄만
            #   표시(잘림)라 요약이 제목이면 1줄뿐 - 본문은 접힌 상태에서도 보통 2줄 노출돼 더보기 없이
            #   읽힌다(사용자 요청). 정확한 줄 수는 OS 재량이라 강제 불가, 본문 배치가 최선의 지렛대.
            #   game 전달 → 구독별 visibility 필터(v6.7.377) / 클릭=IME 앱 / tag=트레이 1개 갱신.
            send_push(f"[{_g or 'IME'}]",
                      _s,
                      '/?tab=ai-summary',
                      tag='imt-ai',
                      game=_g)
    except Exception as push_err:
        logger.warning(f"[GEMMA4] 웹푸시 발송 실패 (무시): {push_err}")

    # [v6.7.629] 상황판 합성 트리거(trigger_synthesis_throttled) 제거 - 요약 저장 때마다 게임별 Gemma 합성을
    #   불렀는데 그 스냅샷을 읽는 화면이 v6.7.628 에서 사라졌다. 저장 -> 캐시/소켓/웹푸시 흐름은 그대로.


def _cycle_impl():
    """실제 배치 요약 실행 (전용 스레드에서 실행)."""
    from sqlite_handler import db_handler
    from ai_lib.gemma4_summary_core import BATCH_SIZE, BATCH_PARALLEL

    core = _get_core()
    if core is None:
        return

    core.maybe_reload_config()
    if core.should_kill():
        logger.warning("[GEMMA4] REMOTE_KILL 감지 — 이번 사이클 스킵")
        return

    if _in_save_fail_cooldown():
        return

    # [v6.7.652] **캡을 병렬도에 묶는다** (`* 3` -> `* BATCH_PARALLEL`). 종전 30 은 배치 3개인데
    #   워커가 2개라 **뒷 웨이브의 절반이 놀았다** - 콜 3회에 웨이브 2개분 시간이 들어 처리량이
    #   천장(`BATCH_SIZE * BATCH_PARALLEL`)의 75% 였다. 가속은 워커 수가 아니라
    #   `배치수 / ceil(배치수/워커수)` 라는 PATTERN-47 의 공식이 그대로 걸린 자리다.
    #   `BATCH_SIZE * BATCH_PARALLEL` 로 두면 배치 수 = 워커 수라 **웨이브가 항상 꽉 찬다.**
    #   P 를 env 로 바꿔도 자동으로 따라오므로 둘이 어긋날 수 없다(30 은 P=2 에도 P=4 에도 안 맞았다).
    #   실측 2026-09-09: 한 사이클 30건 / 배치 3개 / 병렬 2 / 60~168초.
    #   **사이클이 짧아지는 것은 대가가 아니라 이득이다** - 틱이 20초라 곧바로 다음 사이클이
    #   붙고(백로그 구간에서는 이미 back-to-back), 워치독(`_CYCLE_HANG_TIMEOUT` 600초) 여유가
    #   커지며, 꼬리 몫(`limit // 5`)은 사이클이 잦아진 만큼 시간당 배출이 같다.
    pending = db_handler.get_posts_pending_ai_summary(
        limit=BATCH_SIZE * BATCH_PARALLEL, since=_WORKER_START_TIME
    )
    if not pending:
        return

    pending = _exclude_personal_reports(db_handler, pending)
    if not pending:
        return

    _log_queue_depth(db_handler, pending, BATCH_SIZE * BATCH_PARALLEL)

    _cycle_t0 = time.monotonic()   # [v6.7.453] 사이클 총 소요 계측 (처방 판단 근거 + 스톨 구간 식별)
    batches = [pending[i:i + BATCH_SIZE] for i in range(0, len(pending), BATCH_SIZE)]
    _batch_n = len(batches)

    _results, _workers = _run_batches(core, batches, BATCH_PARALLEL)
    _flush_second_stage(core, _batch_n)
    success_records, deferred_n, exhausted_n = _collect_results(_results)

    # [v6.7.453] 사이클 총 소요 - 배치 수와 함께 남긴다. 이 한 줄로 (a) 배치 크기 대 처리 시간
    #   관계(처방 순이득 계산의 전제) (b) 사이클이 20초 주기를 얼마나 초과하는지(= 틱 스킵 빈도)
    #   (c) 로그에 관측된 4분 42초 공백 같은 스톨 구간이 어디서 생기는지가 한꺼번에 보인다.
    #   [v6.7.454] 병렬도를 함께 남긴다 - 이 값 없이는 사이클 소요를 배치 수로 나눠 해석할 수 없고,
    #   병렬화 전후 회귀 판정(같은 방법으로 30분)도 불가능해진다.
    _cycle_el = time.monotonic() - _cycle_t0
    logger.info(f"[GEMMA4] 사이클 완료 {len(pending)}건 / 배치 {_batch_n}개 / 병렬 {_workers} / {_cycle_el:.1f}s")

    _emit_deferred_alerts(deferred_n, exhausted_n)

    if success_records:
        _persist_and_notify(db_handler, success_records)


def shutdown_worker():
    """Flask 종료 시 호출 — ThreadPoolExecutor 정리."""
    try:
        _summary_executor.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass
