# -*- coding: utf-8 -*-
"""routes.analysis — AI 분석 + 날짜별 히스토리 조회.

- `/api/ai-analysis`    : AI 분석 리스트 (SQLite 직접 조회, '분석 제외' 필터링)
- `/api/data/history`   : 과거 날짜 Google Sheets 데이터 (서브 크롤러 탭별 조회)

Phase 1 7차 분할 대상. 기존 `web_lib/web_routes.py` 에서 2 라우트 + 히스토리 헬퍼 이관.
"""
import time
from datetime import datetime as dt_class
from zoneinfo import ZoneInfo

import pandas as pd
from flask import jsonify, request

from web_lib.bg_tasks import get_crawler
from web_lib.routes import api_bp
from web_lib.routes._helpers import _make_ttl_cache, _require_admin, _require_role

KST = ZoneInfo("Asia/Seoul")

# [v6.7.61] /api/ai-analysis 응답 캐시 — Gemma4 INSERT 주기(~3분) 대비 30초 TTL 로 과도한 윈도우 쿼리 절감.
# [v6.7.63] _make_ttl_cache factory 일관 적용 (code-reviewer P2-1 해소).
_ai_analysis_cache, _ai_analysis_cache_lock = _make_ttl_cache(30.0)

# /api/ai-summary 전용 응답 캐시 — get_ai_analysis 와 동일 30초 TTL (관리자 전용 AI 요약 탭).
_ai_summary_cache, _ai_summary_cache_lock = _make_ttl_cache(30.0)


def _build_ai_summary_payload():
    """[v6.7.437] /api/ai-summary 응답 본문 계산 (라우트와 워커 웜리필의 공용 경로).

    라우트 안에 인라인이던 쿼리+필터+content 캡을 그대로 추출 - 동작 무변경.
    """
    from sqlite_handler import db_handler as _db
    # [v6.7.658] per_game 50 -> 500 · limit 500 -> 2000 (사용자 2026-09-10 *"어제/오늘 데이터를
    #   볼 수 있으면 좋은데 ... 200도 적은거같음"*).
    #
    #   **캡이 스파이크가 아니라 평시에 자르고 있었다.** 라이브 실측 2026-09-10(게임x날짜):
    #   POE2 는 이슈가 없는 날에도 하루 **102~174건**이라 `per_game=50` 은 어제/오늘의 절반
    #   이상을 매일 버렸다. 그날 체감된 "엄청나게 생성" 은 배그 한 게임(96 vs 평시 35~60)이고
    #   **전체 일합은 204 로 6일 범위(159~244) 안**이었다 - 즉 이슈를 해소해도 이 절단은 남는다.
    #   어제+오늘 전량 보장선은 **per_game 178 / limit 400**(같은 날 실측)이라 500/2000 은
    #   그 2.8배 여유다.
    #
    #   **비용은 사실상 0이다.** 아래 쿼리의 윈도우 CTE 는 `per_game_limit` 과 무관하게 창
    #   전체를 훑고 `rn <= ?` 를 그 뒤에 적용한다 - 비싼 스캔은 50에서도 이미 다 치른다.
    #   라이브 A/B(2026-09-10): per_game 100/200/400 이 각각 170·182·155ms 로 **차이 없음**,
    #   payload 는 400 에서 789행 0.4MB(본문 1,200자 캡 덕). 지금 값은 3일 창 861건 전량이다.
    #
    #   **이 레포는 같은 결함을 피드에서 이미 고쳤다** - `routes/articles.py` v6.7.169:
    #   *"PER_GAME_LIMIT(10) override - 메모리 보유분 전량(<=10000)을 클라로 전송, 표시 개수는
    #   games-tab.js 의 displayLimit(사용자 limits[게임])이 제어. **라우트가 10/소스 로 막던
    #   구조 해소**"*. 즉 **표시 개수는 화면이 정할 일이지 라우트가 막을 일이 아니다.**
    #   그 판정이 이 탭에는 이식되지 않아 여기만 서버가 막고 있었다(형제 자리 부분 적용).
    #   여기 값은 이제 **표시 정책이 아니라 안전판**이다 - 평시엔 안 걸리고 폭주 때만 문다.
    #   (참고: 피드 payload 는 500건 **1.22MB** 인데 이 탭은 861행에도 약 0.45MB 다 - 본문
    #    1,200자 캡 때문에 행이 더 가볍다. "무거워서 막아 뒀다" 는 이 탭에 성립하지 않는다.)
    #
    #   **`since_days=3` 은 건드리지 않는다** - 그쪽이 성능 절벽이다(v6.7.421 실측:
    #   14일 18.5s / 3일 0.17s). 늘려야 할 축은 게임별 캡이지 시간 창이 아니다.
    #   **게임별 캡 자체는 남긴다** - 한 게임이 3일에 500을 넘어야 발동하므로 평시엔 안 걸리고,
    #   진짜 폭주 때만 독식을 막는다(v6.6.408 이 이 캡을 만든 이유가 그것이다).
    rows = _db.get_latest_ai_analysis(limit=2000, per_game_limit=500, since_today=False, since_days=3)
    out = []
    for item in rows:
        s = str(item.get('summary') or '').strip()
        if s in ('', 'None', 'nan'):
            continue
        if '분석 제외' in s or '분석제외' in s:
            continue
        c = item.get('content')
        if isinstance(c, str) and len(c) > 1200:
            item['content'] = c[:1200]
        out.append(item)
    return out


def refresh_ai_caches():
    """[v6.7.437] 저장 직후 **미리 계산해 채우는** 캐시 갱신 (gemma4 워커 전용).

    왜 invalidate 가 아니라 refresh 인가: 워커가 요약을 저장할 때마다(1~2분 주기) 캐시를 비우고
    같은 순간 소켓 푸시(ai_analysis_update)를 쏘면, 그 신호를 받은 클라의 refetch 가 **항상 콜드 쿼리**를
    맞는다 - 사용자가 체감한 "AI 요약 탭만 늦게 뜨는" 구간이 이 자리다(피드 쿼리 2.6ms vs 여기 138ms 실측).
    무거운 계산을 워커 스레드(요청 경로 밖)에서 끝내고 결과만 swap 하면 클라 요청은 항상 캐시 히트가 된다.
    계산 실패 시엔 기존 정책대로 invalidate 로 폴백(다음 요청이 직접 계산).
    """
    try:
        data = _build_ai_summary_payload()
    except Exception:
        invalidate_ai_caches()
        raise
    with _ai_summary_cache_lock:
        _ai_summary_cache['data'] = data
        _ai_summary_cache['ts'] = time.time()
    with _ai_analysis_cache_lock:
        _ai_analysis_cache['data'] = None   # 디버그 탭(14일·per_game 10)은 별 쿼리라 지연 계산 유지
    return len(data)


def invalidate_ai_caches():
    """[v6.7.328] gemma4 워커가 신규 요약 저장 직후 호출 — /api/ai-analysis·/api/ai-summary 응답 캐시 즉시 무효화.

    소켓 푸시(ai_analysis_update)를 받은 클라이언트가 refetch 했을 때 30초 TTL 잔여분의
    stale 캐시를 받아 "푸시는 왔는데 새 글이 안 보이는" 레이스를 차단한다. (-w 1 단일 워커라
    워커 스레드의 무효화가 서빙 프로세스에 그대로 반영됨 — Dockerfile CMD 참조.)
    """
    with _ai_analysis_cache_lock:
        _ai_analysis_cache['data'] = None
    with _ai_summary_cache_lock:
        _ai_summary_cache['data'] = None


@api_bp.route('/api/ai-analysis', methods=['GET'])
def get_ai_analysis():
    # [v6.6.226] GEMMA4 워커가 SQLite ai_analysis에 직접 INSERT하는 구조이므로
    # 3분 주기 캐시 잡 우회하고 매 요청마다 SQLite 직접 조회.
    # [v6.7.731] 구 `crawler.get_ai_analysis()` 분기 제거 - CrawlerManager 에 그 메서드가 없어(webview_app
    #   의 동명 API 와 혼동한 잔재) 항상 SQLite else 로만 떨어지던 죽은 분기였다. hasattr 평가가
    #   get_crawler(blocking) 를 호출해 부팅 중 크롤러 준비까지 이 요청을 막던 대기도 함께 사라진다
    #   (응답·동작 불변 - 아래 SQLite 경로가 운영에서 실제로 돌던 유일한 경로다).
    # [v6.6.412] since_today=True → False, per_game_limit=10 — 자정 직후 초기화되지 않도록 게임별 최신 N개 누적.
    # 어제/오늘 구분은 클라이언트 formatTime (오늘 HH:MM / 이전 MM.DD) 으로 표시.
    # [v6.7.61] 30초 TTL 캐시 — ROW_NUMBER 윈도우 쿼리 반복 비용 절감.
    # [v6.7.338] admin 가드 — 소비처가 AI 분석 탭(ADMIN_ONLY_APP_IDS) 단일인데 무가드라
    #   비로그인 URL 직접 호출로 게임별 요약/감정/본문이 노출되던 결함 봉합(/api/ai-summary v6.7.313 동형).
    guard = _require_role('admin')
    if guard is not None:
        return guard
    now = time.time()
    cached = _ai_analysis_cache.get('data')
    if cached is not None and (now - _ai_analysis_cache['ts']) < _ai_analysis_cache['ttl']:
        return jsonify(cached)
    with _ai_analysis_cache_lock:
        now = time.time()
        cached = _ai_analysis_cache.get('data')
        if cached is not None and (now - _ai_analysis_cache['ts']) < _ai_analysis_cache['ttl']:
            return jsonify(cached)
        from sqlite_handler import db_handler as _db
        # [v6.7.421] since_days=14 바운드 - 무바운드는 37.5만행 전 행 필터 평가(콜드 150s급, v6.7.420 실측 계열).
        rows = _db.get_latest_ai_analysis(limit=200, per_game_limit=10, since_today=False, since_days=14)
        out = []
        for item in rows:
            s = str(item.get('summary') or '').strip()
            if s in ('', 'None', 'nan'):
                continue
            if '분석 제외' in s or '분석제외' in s:
                continue
            out.append(item)
        data = out
        _ai_analysis_cache['data'] = data
        _ai_analysis_cache['ts'] = now
    return jsonify(data)


@api_bp.route('/api/ai-summary', methods=['GET'])
def get_ai_summary():
    # "AI 요약" 데이터 - [v6.7.386] manager 개방 (사용자 결정 2026-07-18: AI 배너/보관소를 매니저 제공 →
    #   배너가 이 API 를 클라 fetch 하므로 manager 접근 필수).
    #   [v6.7.386~499] 당시엔 탭 UI 가 admin 전용이라 매니저는 배너/보관소 스니펫만 봤다.
    #   [v6.7.731 주석 정정] 위 서술은 스테일 - v6.7.500 에 탭 UI 도 manager 재개방됐다(app.py _ADMIN_MANAGER_JS
    #   + ui-control.js MANAGER_EXTRA_APP_IDS). 이 탭이 쓰는 API 는 이것 하나뿐이라 매니저가 403 밟는 경로 없음.
    #   [v6.7.313] 원래 admin 전용이었음(회수 이력). 자매 /api/ai-analysis 는 admin 유지(별개 디버그 탭).
    # [v6.7.751] 일반 공개(사용자 2026-11-14) - 지원서 평가용으로 AI 요약 · 대시보드 · 이상감지 탭을 익명에게 연다. 쓰기 API 는 그대로 admin.
    now = time.time()
    cached = _ai_summary_cache.get('data')
    if cached is not None and (now - _ai_summary_cache['ts']) < _ai_summary_cache['ttl']:
        return jsonify(cached)
    with _ai_summary_cache_lock:
        now = time.time()
        cached = _ai_summary_cache.get('data')
        if cached is not None and (now - _ai_summary_cache['ts']) < _ai_summary_cache['ttl']:
            return jsonify(cached)
        # [v6.7.437] 본문 계산은 _build_ai_summary_payload 단일 경로 (워커 웜리필과 공용).
        #   아래 옛 인라인 주석은 그 함수가 계승 - 파라미터/캡 값의 근거 이력이라 보존.
        # per_game_limit/limit 상향 — AI 요약 탭은 게임별 누적 조망용(디버그용 ai-analysis 10건/게임보다 넓게).
        # [v6.7.337] since_days=14 — ai_analysis 는 purge 잡이 없어 무한 누적, 바운드 없는 윈도우
        #   쿼리가 전 행 스캔이라 탭 로딩이 테이블 성장에 비례해 느려지던 것 차단(최근 조망 목적과 정합).
        # [v6.7.421] since_days 14→3 — 서버 실측(2026-07-21): 구쿼리 153s / v6.7.420 인덱스 후에도 14일=18.5s
        #   (summary 필터 평가가 content 동거 페이지를 6.7만행 랜덤 I/O, NAS 2GB RAM 캐시 초과) / 3일=0.17s.
        #   대가 = 저볼륨 게임(아레스·프렌즈타운) 조망이 3일치로 축소(사용자 수락). 고볼륨은 per_game 50 캡이라 무변.
        # [v6.7.337] content 1200자 캡 — SELECT * 가 본문 전문을 실어 500행 payload 가 수 MB 로
        #   비대(직렬화+gzip+전송이 로딩 지연 주범). 클라(ai-summary-tab.js)는 800자 캡 +
        #   "이하 생략" 접미인데, 접미 분기(>800 판정)를 살리려 800 이 아니라 1200 에서 자른다.
        data = _build_ai_summary_payload()
        _ai_summary_cache['data'] = data
        _ai_summary_cache['ts'] = now
    return jsonify(data)


@api_bp.route('/api/ai-summary/backlog', methods=['GET'])
def get_ai_summary_backlog():
    """[v6.7.686] AI 요약 **대기(밀림) 현황** - 요약 워커가 아직 못 집은 건수 + 가장 오래된 대기 나이.

    무거운 조회를 새로 만들지 않는다(PATTERN-70 - NAS 단일 HDD 에서 함수 씌운 컬럼/전 행 스캔은
    콜드 캐시를 랜덤 I/O 로 묶는다). `health_sampler._queue_lags` 가 1분마다 **요약 워커와 같은
    술어**(content 비어있지 않음 · `NOT LIKE '[%]'` · ai_analysis 미존재 · 워커 컷오프 창)로 이미
    재서 `system_health` 에 넣어 둔 값을 그대로 읽기만 한다 - 이 라우트는 인덱스 1행 조회 2건뿐이다.
    그래서 창·술어가 워커와 어긋나지 않는다(PATTERN-73). 대신 표시 값은 최대 1분 늦다 - `sample_age_s`
    로 신선도를 함께 내려 클라가 오래된 표본임을 표기할 수 있게 한다.

    게이트는 `/api/ai-summary` 와 동일한 `_require_role('manager')` - 이 탭이 치는 다른 API 와 같은
    등급이라 매니저 동선에 구멍이 안 생긴다(응답 형태는 **리스트가 아니라 객체**라 배너/보관소/hydrate
    가 쓰는 `/api/ai-summary` 형태와 섞이지 않는다).
    """
    # [v6.7.751] 일반 공개(사용자 2026-11-14) - 지원서 평가용으로 AI 요약 · 대시보드 · 이상감지 탭을 익명에게 연다. 쓰기 API 는 그대로 admin.
    from sqlite_handler import db_handler as _db
    latest = _db.get_health_latest(['queue_summary_backlog', 'queue_summary_lag_s'])

    def _val(entry):
        return entry['value'] if (entry and entry.get('value') is not None) else None

    backlog_e = latest.get('queue_summary_backlog')
    lag_e = latest.get('queue_summary_lag_s')
    pending = _val(backlog_e)
    lag_s = _val(lag_e)

    # 표본 나이 - 서버에서 계산한다(클라 TZ 개입 없이). save_health_sample 은 `datetime.now().isoformat()`
    #   (naive 로컬)이라 같은 축의 now 와 뺀다. 두 지표는 같은 sample 사이클에 쓰이므로 ts 가 같다.
    ts = (backlog_e or {}).get('ts') or (lag_e or {}).get('ts')
    sample_age_s = None
    if ts:
        try:
            sample_age_s = max(0.0, (dt_class.now() - dt_class.fromisoformat(ts)).total_seconds())
        except Exception:
            sample_age_s = None

    # 경보 임계와 같은 값을 쓴다(단일 소스) - 클라의 경고색 판정과 서버 정지 경보가 어긋나지 않게.
    try:
        from web_lib.bg.health_sampler import _STALL_THRESHOLD_S as _thr
    except Exception:
        _thr = 1800

    return jsonify({
        'pending': int(pending) if pending is not None else None,
        'oldest_lag_s': lag_s,
        'sample_age_s': sample_age_s,
        'threshold_s': _thr,
    })


# ====================================================================
# 날짜 조회 API — 설정 기반 리팩토링
# ====================================================================
# 탭별 설정 레지스트리: url_source, ws_suffix, 후처리 옵션
_HISTORY_TAB_CONFIG = {
    # 서브 크롤러 탭만 날짜별 히스토리 조회 지원 (커뮤니티/댓글/AI는 SQLite 실시간 전용)
    'account_trade': {'url_source': 'setting:URL_ACCOUNT_TRADE',   'ws_suffix': 'Account_trade'},
    'odin_global':   {'url_source': 'setting:URL_ODIN_GLOBAL',     'ws_suffix': 'Odin_global'},
    'jasa':          {'url_source': 'setting:URL_JASA_GAMES',      'ws_suffix': 'Jasa'},
    'tasa':          {'url_source': 'setting:URL_TASA_GAMES',      'ws_suffix': 'Tasa'},
}


def _resolve_sheet_url(crawler, cfg):
    """탭 설정에서 Google Sheets URL을 해석합니다."""
    import utils.common as _common
    src = cfg['url_source']
    if src.startswith('setting:'):
        url = crawler.settings.get(src.split(':', 1)[1])
        if not url and 'fallback_url' in cfg:
            fb = cfg['fallback_url']
            url = getattr(_common, fb.split(':', 1)[1]) if fb.startswith('const:') else fb
        return url
    elif src.startswith('const:'):
        return getattr(_common, src.split(':', 1)[1], None)
    return None


def _postprocess_history(records, tab, cfg):
    """서브 크롤러 탭 히스토리 후처리."""
    return records


@api_bp.route('/api/data/history', methods=['GET'])
def get_history_data():
    """과거 날짜 Google Sheets 데이터 조회 (설정 기반)"""
    # [v6.7.731] admin 가드 추가 - 무가드로 비로그인 URL 직접 호출 시 거래/시세 과거 시트가 노출되던
    #   결함 봉합(/api/ai-analysis v6.7.338 동형). 소비처는 loadHistoryData 뿐이고([v6.7.741] updates-history.js -> tabs/admin/history-data.js 로 이사)
    #   그 호출은 account-trade·diamond-trade 탭에서만 나는데, 두 탭 다 ADMIN_ONLY_APP_IDS 이고
    #   MANAGER_EXTRA_APP_IDS 엔 없다(admin 전용). 그래서 가드 추가가 기존 화면을 깨지 않는다.
    guard = _require_admin()
    if guard is not None:
        return guard
    tab = request.args.get('tab', '').strip()
    date_str = request.args.get('date', '').strip()

    if not date_str:
        return jsonify({'error': 'date 파라미터가 필요합니다.'}), 400
    if len(date_str) > 10:
        return jsonify({'error': 'date 형식이 올바르지 않습니다. (YYYY-MM-DD)'}), 400
    try:
        req_date = dt_class.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'error': 'date 형식이 올바르지 않습니다. (YYYY-MM-DD)'}), 400
    if req_date >= dt_class.now(KST).date():
        return jsonify({'error': '오늘 이후 날짜는 조회할 수 없습니다.'}), 400

    cfg = _HISTORY_TAB_CONFIG.get(tab)
    if not cfg:
        return jsonify({'error': f'tab 값이 올바르지 않습니다. ({", ".join(_HISTORY_TAB_CONFIG)})'}), 400

    crawler = get_crawler()
    try:
        from google_sheets_handler import get_handler
        handler = get_handler(log_func=lambda m, l, t: None)
        if not handler.gc:
            return jsonify({'data': [], 'message': '데이터 로드 실패'}), 200

        spreadsheet_url = _resolve_sheet_url(crawler, cfg)
        if not spreadsheet_url:
            return jsonify({'data': [], 'message': '해당 날짜의 데이터가 없습니다.'}), 200

        ws_name = f"{date_str}_{cfg['ws_suffix']}"
        results = handler.read_multiple_worksheets(spreadsheet_url, [ws_name])
        df = results.get(ws_name)
        if df is None or df.empty:
            return jsonify({'data': [], 'message': '해당 날짜의 데이터가 없습니다.'}), 200

        records = df.where(pd.notnull(df), None).to_dict('records')
        return jsonify({'data': _postprocess_history(records, tab, cfg)})

    except Exception as e:
        crawler._log(f"날짜 조회 오류 ({tab}, {date_str}): {e}", 'error', 'HISTORY')
        return jsonify({'data': [], 'message': '데이터 로드 실패'}), 200


# ====================================================================
# [v6.7.68] F-3 — AI 오분류 피드백 루프 (관리자 전용)
# ====================================================================
# POST /api/ai-feedback           : 운영자가 카드 단위로 expected sentiment/severity/note 저장
# POST /api/ai-feedback/reanalyze : 해당 link 의 글을 Gemini 2차 요약기로 재분석 → ai_analysis 갱신
# 두 라우트 모두 _require_admin 가드. SQLite ai_feedback 테이블에 누적, ai_analysis 는 in-place UPDATE.

_SENTIMENT_ENUM = {'positive', 'negative', 'neutral', 'mixed'}
_SEVERITY_ENUM = {'critical', 'warning', 'info'}

# ====================================================================
# [v6.7.459] 수동 AI 요약 요청 — 예산 가드
# ====================================================================
# 왜 별도 상한이 필요한가: 수동 경로는 `bypass_pace=True` 로 시간당 페이싱을 면제받으므로,
# 자동 파이프라인을 지키던 PATTERN-43 방어선이 이 경로에는 걸리지 않는다. 지금까지 reanalyze
# 하나뿐이었을 때는 AI 분석 탭 모달 깊숙이 있어 발화 빈도가 무시할 수준이었지만, 피드 카드마다
# 버튼이 달리면 클릭 빈도가 자릿수로 올라간다 → 여기서 막지 않으면 사람이 하루 예산을 태울 수 있다.
#
# 이중 상한인 이유: 시간당 10만 걸면 최악 240콜/일 = 전체 예산(6키 x 체인 RPD 합 = 360)의 67%라
# 이론상 자동 파이프라인을 굶길 수 있다. 일일 40 을 함께 걸면 11% 로 묶인다 - 사람이 하루에
# 40건을 누를 일은 사실상 없으므로 실사용 마찰은 0 이면서 최악만 잘라낸다.
_MANUAL_SUMMARY_HOURLY_CAP = 10
_MANUAL_SUMMARY_DAILY_CAP = 40
# 프로세스 로컬 카운터 (gunicorn -w 1 단일 워커라 이걸로 충분 — Dockerfile CMD 참조).
# 재시작 시 리셋되지만, 일일 RPD 카운터(`gemini_counts`)도 같은 성질이라 정합이 깨지지 않는다.
_manual_quota = {'hour_key': None, 'hour_calls': 0, 'day_key': None, 'day_calls': 0}


def _manual_quota_check():
    """수동 요약 요청 1건을 예약한다. 반환: (허용여부, 사유문자열 또는 None).

    호출 **직전**에 세는 이유는 페이싱 카운터와 같다 - 실패(429/네트워크)도 쿼타를 소비하므로
    성공분만 세면 상한이 샌다(PATTERN-43 의 '시도 기준' 규율).
    """
    now_kst = dt_class.now(KST)
    hour_key = (now_kst.date(), now_kst.hour)
    day_key = now_kst.date()
    if _manual_quota['hour_key'] != hour_key:
        _manual_quota['hour_key'] = hour_key
        _manual_quota['hour_calls'] = 0
    if _manual_quota['day_key'] != day_key:
        _manual_quota['day_key'] = day_key
        _manual_quota['day_calls'] = 0

    if _manual_quota['day_calls'] >= _MANUAL_SUMMARY_DAILY_CAP:
        return False, (f'오늘 수동 요약 한도 소진 ({_MANUAL_SUMMARY_DAILY_CAP}건) — '
                       f'자정(KST) 이후 재개됩니다.')
    if _manual_quota['hour_calls'] >= _MANUAL_SUMMARY_HOURLY_CAP:
        return False, (f'이번 시간 수동 요약 한도 소진 ({_MANUAL_SUMMARY_HOURLY_CAP}건) — '
                       f'다음 시간에 재개됩니다. (오늘 {_manual_quota["day_calls"]}/'
                       f'{_MANUAL_SUMMARY_DAILY_CAP}건 사용)')
    _manual_quota['hour_calls'] += 1
    _manual_quota['day_calls'] += 1
    return True, None


def _post_ai_state(post, ai_row):
    """피드 글 1건의 AI 요약 상태를 판정한다. 반환: (state, gemma_verdict).

    state 는 클라 렌더 분기 키이고 gemma_verdict 는 라벨에 박제할 '1차가 뭐라고 했나' 값이다.
    - `summarized`  : 실 요약 있음
    - `excluded`    : Gemma4 1차가 '분석 제외' 판정 (요청 시 이 판정을 뒤집는다)
    - `unanalyzed`  : ai_analysis 행 자체 없음 (PATTERN-46 고아 등)
    - `no_content`  : 본문 미수집 — 요약 불가 (원문 재크롤은 별건, 여기서 하지 않는다)
    """
    content = str((post or {}).get('content') or '').strip()
    if not content or (content.startswith('[') and content.endswith(']')):
        return 'no_content', '본문없음'
    if not ai_row:
        return 'unanalyzed', '미분석'
    summary = str(ai_row.get('summary') or '').strip()
    if not summary or (summary.startswith('(') and summary.endswith(')')):
        # 대기 스탬프('(...)')는 워커가 저장을 생략하는 값이라 정상 경로에선 안 보이지만,
        # 과거 빌드가 남긴 잔재가 있을 수 있어 미분석과 같이 취급한다(요청 시 UPDATE 로 덮인다).
        return 'unanalyzed', '미분석'
    if '분석 제외' in summary or '분석제외' in summary:
        return 'excluded', '분석 제외'
    return 'summarized', '요약됨'


@api_bp.route('/api/ai-feedback', methods=['POST'])
def post_ai_feedback():
    guard = _require_admin()
    if guard is not None:
        return guard
    payload = request.get_json(silent=True) or {}
    link = (payload.get('link') or '').strip()
    if not link:
        return jsonify({'error': 'link 필수'}), 400

    expected_sentiment = (payload.get('expected_sentiment') or '').strip().lower()
    expected_severity = (payload.get('expected_severity') or '').strip().lower()
    if expected_sentiment and expected_sentiment not in _SENTIMENT_ENUM:
        return jsonify({'error': f'expected_sentiment enum 위반 ({_SENTIMENT_ENUM})'}), 400
    if expected_severity and expected_severity not in _SEVERITY_ENUM:
        return jsonify({'error': f'expected_severity enum 위반 ({_SEVERITY_ENUM})'}), 400
    if not (expected_sentiment or expected_severity or (payload.get('expected_summary') or '').strip() or (payload.get('note') or '').strip()):
        return jsonify({'error': '최소 한 항목 (sentiment/severity/summary/note) 입력 필수'}), 400

    from sqlite_handler import db_handler as _db
    original = _db.get_ai_analysis_by_link(link) or {}
    record = {
        'link': link,
        'original_sentiment': original.get('sentiment'),
        'expected_sentiment': expected_sentiment or None,
        'original_severity': original.get('severity'),
        'expected_severity': expected_severity or None,
        'original_summary': original.get('summary'),
        'expected_summary': (payload.get('expected_summary') or '').strip() or None,
        'note': (payload.get('note') or '').strip() or None,
        'admin_ip': request.headers.get('X-Forwarded-For', request.remote_addr or '')[:64],
    }
    new_id = _db.save_ai_feedback(record)
    if new_id is None:
        return jsonify({'error': '피드백 저장 실패'}), 500
    return jsonify({'success': True, 'id': new_id})


@api_bp.route('/api/ai-feedback/reanalyze', methods=['POST'])
def post_ai_feedback_reanalyze():
    """[v6.7.68] 단일 link 재분석. Gemma4 분류 단계 우회 — 운영자가 명시적으로 재분석 요청 = analyze=true 가정.
    Gemini 2차 요약기 (`_generate_gemini_summaries`) 단발 호출 후 ai_analysis 갱신.
    응답: 새 summary/sentiment/severity. 차단: admin 가드.
    """
    guard = _require_admin()
    if guard is not None:
        return guard
    payload = request.get_json(silent=True) or {}
    link = (payload.get('link') or '').strip()
    if not link:
        return jsonify({'error': 'link 필수'}), 400

    from sqlite_handler import db_handler as _db
    row = _db.get_ai_analysis_by_link(link)
    if not row:
        return jsonify({'error': '해당 link 의 ai_analysis 행 없음 — 재분석 대상 아님'}), 404

    # Gemma4 1차 분류 우회 — 단발 글 단위로 Gemini 2차 요약기 직접 호출
    try:
        from web_lib.ai_gemma4_worker import _get_core
        core = _get_core()
    except Exception as e:
        return jsonify({'error': f'Gemma4SummaryCore 로드 실패: {e}'}), 500
    if core is None:
        return jsonify({'error': 'Gemma4SummaryCore 미초기화'}), 500

    # [v6.7.459] 수동 경로 예산 규율을 요약 요청과 **통일**. 두 경로가 서로 다른 규율을 가지면
    #   한쪽만 조이는 순간 다른 쪽이 우회로가 된다.
    _ok, _reason = _manual_quota_check()
    if not _ok:
        return jsonify({'error': _reason}), 429

    article = {
        'game': row.get('game') or '',
        'board_name': row.get('board_name') or '',
        'title': row.get('title') or '',
        'content': row.get('content') or '',
        'link': link,
    }
    try:
        core._generate_gemini_summaries(
            [article],
            getattr(core, 'system_instruction', None) or '',
            '',
            bypass_pace=True
        )
    except Exception as e:
        return jsonify({'error': f'재분석 호출 실패: {e}'}), 500

    new_summary = article.get('summary') or ''
    # [v6.7.436] 실패/대기 placeholder('(...)' 형: 쿼타 소진·예산 페이싱 대기·호출 실패)는 저장 금지.
    #   이 경로는 워커와 달리 결과를 무조건 UPDATE 하므로, 그대로 두면 **멀쩡한 기존 요약이 placeholder 로
    #   덮여** 영구 손실된다(워커의 v6.7.205 저장 생략 규율이 여기엔 없던 비대칭). 원인 불문 재시도 안내로 종료.
    if new_summary.startswith('(') and new_summary.endswith(')'):
        return jsonify({'error': f'재분석 결과 없음 {new_summary} - 기존 요약을 유지했습니다. 잠시 후 다시 시도하세요.'}), 503
    new_sentiment = article.get('sentiment') or row.get('sentiment')
    new_severity = article.get('severity') or row.get('severity')
    updated = _db.update_ai_analysis_for_link(link, new_summary, new_sentiment, new_severity)
    if not updated:
        return jsonify({'error': 'ai_analysis 갱신 실패'}), 500

    # 캐시 무효화 — TTL 30s 라 즉시 재반영 위해 강제 invalidate.
    # [v6.7.437] 옛 코드는 _ai_analysis_cache 만 비워 **AI 요약 탭(/api/ai-summary)은 최대 30초간 옛 요약을
    #   그대로 보여줬다**(재분석 결과가 안 보이는 비대칭). 공용 invalidate 로 양쪽 동시 무효화.
    try:
        invalidate_ai_caches()
    except Exception:
        pass

    return jsonify({
        'success': True,
        'link': link,
        'summary': new_summary,
        'sentiment': new_sentiment,
        'severity': new_severity,
    })


# ====================================================================
# [v6.7.459] 피드 탭 수동 AI 요약 요청 (관리자 전용)
# ====================================================================
# GET  /api/ai-summary/for-link : 상세 펼침 시 1건 상태 조회 (4상태 판정 + 기존 요약)
# POST /api/ai-summary/request  : Gemma4 1차 우회 + Gemini 2차 강제 → ai_analysis INSERT/UPDATE
# POST /api/ai-summary/label    : 요청 후 사유 칩 (선택)
# GET  /api/ai-summary/labels   : 쌓인 라벨 읽기 — 프롬프트(Config_LLM A2) 개선 근거
#
# 설계 근거 3가지:
# (1) for-link 를 따로 두는 이유 — 피드 payload(/api/articles/all, 500건 1.22MB)에 요약을 조인하면
#     PATTERN-29/41 경로를 다시 연다. 상세는 사용자가 명시적으로 펼치는 것이라 온디맨드 1건이 맞다.
# (2) 알림 0 — 카카오 발송은 `_generate_summary_batch`, 웹푸시/종은 워커 `_cycle_impl` 안에만 있어
#     이 경로는 자연히 안 탄다. 과거 글을 요약했다고 폰이 울리면 그 자체가 사고이므로, 이 성질은
#     **의도된 것**이고 여기에 알림/소켓 emit 을 추가하지 말 것.
# (3) 라벨은 요청 시점에 남긴다(요약 성공 여부와 무관) — 신호는 "사람이 요약 가치가 있다고
#     판단했다" 이고 그건 Gemini 호출 성공과 독립이다. 예산 소진 503 으로 실패해도 라벨은 남아야
#     프롬프트 개선 데이터가 된다("요약은 안 됐지만 요약이 요구되던 글" 이 정확히 그 케이스).

# 사유 칩 — 프롬프트 개선 시 "1차가 어떤 종류를 놓치는가" 를 묶어 보기 위한 최소 분류.
# 필수 입력이 아니다(F-3 가 라이브 0건이 된 원인이 입력 마찰이므로 어떤 항목도 강제하지 않는다).
_SUMMARY_LABEL_ENUM = {'patch', 'maintenance', 'issue', 'sentiment', 'etc'}


@api_bp.route('/api/ai-summary/for-link', methods=['GET'])
def get_ai_summary_for_link():
    """피드 상세 펼침 시 글 1건의 AI 상태 조회."""
    guard = _require_admin()
    if guard is not None:
        return guard
    link = (request.args.get('link') or '').strip()
    if not link:
        return jsonify({'error': 'link 필수'}), 400

    from sqlite_handler import db_handler as _db
    post = _db.get_post_by_link(link)
    if not post:
        return jsonify({'error': '해당 link 의 글 없음'}), 404
    ai_row = _db.get_ai_analysis_by_link(link)
    state, verdict = _post_ai_state(post, ai_row)

    summary = ''
    if state == 'summarized':
        summary = str((ai_row or {}).get('summary') or '').strip()
    return jsonify({
        'link': link,
        'state': state,
        'gemma_verdict': verdict,
        'summary': summary,
        'sentiment': (ai_row or {}).get('sentiment') or '',
        'severity': (ai_row or {}).get('severity') or '',
    })


def _summary_request_context(_db, link):
    """수동 요약 요청의 대상 글·기존 분석·상태를 해석한다. 반환: (ctx, err).

    ctx = {'post', 'ai_row', 'state', 'verdict'} (전제 통과) / err = (Response, status) (전제 실패).
    `reanalyze` 와 달리 `community_posts` 를 기준으로 삼아 행이 없으면 404 가 아니라 INSERT 로 간다
    (호출부 참조) - 여기서는 글 자체 부재(404)와 본문 미수집(422)만 거른다.
    """
    post = _db.get_post_by_link(link)
    if not post:
        return None, (jsonify({'error': '해당 link 의 글 없음 — 요약 대상 아님'}), 404)

    ai_row = _db.get_ai_analysis_by_link(link)
    state, verdict = _post_ai_state(post, ai_row)
    if state == 'no_content':
        # 원문 재크롤은 하지 않는다: 원문이 이미 삭제됐을 가능성이 높고(DC/카페), Selenium 단건
        # 기동은 2코어 NAS 에서 PATTERN-31 부하 경로다. 본문 백필은 PATTERN-46 별건 과제.
        return None, (jsonify({'error': '본문 미수집 — 요약 불가', 'state': state}), 422)
    return {'post': post, 'ai_row': ai_row, 'state': state, 'verdict': verdict}, None


def _summary_label_recorder(_db, link, verdict, ai_row, admin_ip):
    """라벨 적재 클로저를 만든다 — 요약 성공/실패와 무관하게 남긴다 (위 설계 근거 (3))."""
    def _record_label(result):
        try:
            return _db.save_ai_feedback({
                'link': link,
                'kind': 'summary_request',
                'gemma_verdict': verdict,
                'result': result,
                'original_summary': str((ai_row or {}).get('summary') or '').strip() or None,
                'original_sentiment': (ai_row or {}).get('sentiment'),
                'original_severity': (ai_row or {}).get('severity'),
                'admin_ip': admin_ip,
            })
        except Exception:
            return None
    return _record_label


def _build_summary_article(post, link):
    """Gemini 2차에 넘길 글 1건 구성 (워커 배치와 같은 5필드 형태)."""
    return {
        'game': post.get('game') or '',
        'board_name': post.get('board_name') or '',
        'title': post.get('title') or '',
        'content': post.get('content') or '',
        'link': link,
    }


def _persist_manual_summary(_db, ai_row, post, link, new_summary, new_sentiment, new_severity):
    """새 요약 3필드를 ai_analysis 에 반영한다. 반환: 저장 성공 여부.

    기존 행이 있으면 3필드만 UPDATE, 없으면 워커 배치 레코드 형태로 INSERT 후 행 존재를 재확인한다.
    """
    if ai_row:
        # 기존 행이 있으면 요약 3필드만 갱신한다 — save_ai_analysis 는 INSERT OR REPLACE 라
        # AI_Score·is_super_keyword 등 기존 값을 통째로 덮어쓴다.
        return _db.update_ai_analysis_for_link(link, new_summary, new_sentiment, new_severity)
    # 신규 INSERT — save_ai_analysis 는 워커 배치와 같은 레코드 형태를 받는다.
    _db.save_ai_analysis([{
        'link': link,
        'game': post.get('game') or '',
        'source': post.get('source') or '',
        'board_name': post.get('board_name') or '',
        'title': post.get('title') or '',
        'content': post.get('content') or '',
        'summary': new_summary,
        'sentiment': new_sentiment,
        'severity': new_severity,
        'timestamp': post.get('timestamp') or '',
        'date': post.get('date') or '',
        'detected_keywords': post.get('detected_keywords') or '',
        'is_super_keyword': post.get('is_super_keyword'),
        'AI_Score': '0.00',
    }])
    # [v6.7.484] save_ai_analysis 는 이제 성공 여부(bool)를 반환하지만, 여기서는 **행 존재까지**
    # 다시 읽어 확인한다 — 반환값은 "예외 없이 executemany 가 끝났다" 까지만 보장하고, 이 경로는
    # 사용자에게 요약을 즉시 보여주므로 한 단계 더 강한 근거가 맞다(link 인덱스 1행, 비용 무시 가능).
    # (구 주석은 "항상 None 을 반환한다" 였다 — 그 한계 때문에 생긴 우회이나, 우회 쪽이 더 엄밀해 존치.)
    # 이 확인이 없으면 저장 실패 시에도 사용자에겐 요약이 보이고 다음 조회에서 사라진다.
    return bool(_db.get_ai_analysis_by_link(link))


@api_bp.route('/api/ai-summary/request', methods=['POST'])
def post_ai_summary_request():
    """수동 AI 요약 요청 — Gemma4 1차 판정을 뒤집고 Gemini 2차를 강제 실행한다.

    `reanalyze` 와의 차이: 저 쪽은 `ai_analysis` 행이 있어야만 동작한다(없으면 404). 이 라우트는
    `community_posts` 를 기준으로 삼아 행이 없으면 INSERT 한다 — 라이브 실측상 최근 7일 미분석
    글 중 본문이 있는 1,314건이 정확히 저 404 에 막혀 있던 구간이다.
    """
    guard = _require_admin()
    if guard is not None:
        return guard
    payload = request.get_json(silent=True) or {}
    link = (payload.get('link') or '').strip()
    if not link:
        return jsonify({'error': 'link 필수'}), 400

    from sqlite_handler import db_handler as _db
    ctx, err = _summary_request_context(_db, link)
    if err is not None:
        return err
    post, ai_row, verdict = ctx['post'], ctx['ai_row'], ctx['verdict']

    _ok, _reason = _manual_quota_check()
    if not _ok:
        return jsonify({'error': _reason}), 429

    _admin_ip = request.headers.get('X-Forwarded-For', request.remote_addr or '')[:64]
    _record_label = _summary_label_recorder(_db, link, verdict, ai_row, _admin_ip)

    try:
        from web_lib.ai_gemma4_worker import _get_core
        core = _get_core()
    except Exception as e:
        return jsonify({'error': f'Gemma4SummaryCore 로드 실패: {e}'}), 500
    if core is None:
        return jsonify({'error': 'Gemma4SummaryCore 미초기화'}), 500

    article = _build_summary_article(post, link)
    try:
        core._generate_gemini_summaries(
            [article],
            getattr(core, 'system_instruction', None) or '',
            '',
            bypass_pace=True,
            # [v6.7.460] 2차의 거절 경로까지 닫는다. v459 는 1차만 우회해, 라이브 첫 시험에서
            #   '분석 제외(사유: ...)' 가 돌아왔다 - 버튼이 겨냥하는 87% 가 이미 제외 판정된 글이라
            #   2차도 같은 판단을 하면 눌러도 아무것도 안 나오고 쿼타만 나간다.
            force_summary=True
        )
    except Exception as e:
        _record_label('fail')
        return jsonify({'error': f'요약 호출 실패: {e}'}), 500

    new_summary = (article.get('summary') or '').strip()
    # 실패/대기 placeholder('(...)')는 저장하지 않는다 — reanalyze v6.7.436 과 동일 규율.
    #   저장하면 `a.link IS NULL` dedup 가드에 걸려 자동 워커가 이 글을 영영 다시 안 집는다.
    if new_summary.startswith('(') and new_summary.endswith(')'):
        _fid = _record_label('budget')
        return jsonify({
            'error': f'요약 결과 없음 {new_summary} — 잠시 후 다시 시도하세요.',
            'feedback_id': _fid,   # 실패해도 라벨은 남는다 → 칩 선택은 계속 가능
        }), 503

    new_sentiment = article.get('sentiment') or (ai_row or {}).get('sentiment') or 'neutral'
    new_severity = article.get('severity') or (ai_row or {}).get('severity') or 'info'

    saved = _persist_manual_summary(_db, ai_row, post, link, new_summary, new_sentiment, new_severity)
    if not saved:
        _record_label('fail')
        return jsonify({'error': 'ai_analysis 저장 실패'}), 500

    # [v6.7.460] result 를 실제 결과로 가른다. force_summary 로도 모델이 '분석 제외' 를 고집할 수
    #   있는데, 그걸 'ok' 로 적으면 **프롬프트 개선 데이터가 오염된다** - "사람은 요약을 원했는데
    #   모델이 끝내 거절한 글" 이야말로 Config_LLM A2 를 고칠 때 가장 봐야 할 표본이다.
    _still_excluded = ('분석 제외' in new_summary) or ('분석제외' in new_summary)
    _fid = _record_label('excluded' if _still_excluded else 'ok')

    # 캐시만 무효화한다. 소켓 emit(ai_analysis_update)은 **의도적으로 생략** — 수동 1건에 전 클라
    # 배지를 올리면 알림이 소음이 된다(요청자 화면은 아래 응답값으로 그 자리에서 갱신된다).
    try:
        invalidate_ai_caches()
    except Exception:
        pass

    return jsonify({
        'success': True,
        'link': link,
        'summary': new_summary,
        'sentiment': new_sentiment,
        'severity': new_severity,
        'gemma_verdict': verdict,
        'feedback_id': _fid,
        # [v6.7.460] 클라가 결과를 추정하지 않도록 상태를 서버가 확정해 내려준다. 'excluded' 면
        #   클라는 '요청' 버튼으로 되돌리지 않고 거절 사실을 표시한다(같은 요청 반복 = 쿼타 낭비).
        'state': 'excluded' if _still_excluded else 'summarized',
    })


@api_bp.route('/api/ai-summary/label', methods=['POST'])
def post_ai_summary_label():
    """요청 후 사유 칩 선택 (선택 사항). feedback_id 는 request 응답이 돌려준 값."""
    guard = _require_admin()
    if guard is not None:
        return guard
    payload = request.get_json(silent=True) or {}
    fid = payload.get('feedback_id')
    label = (payload.get('label') or '').strip().lower()
    if not fid:
        return jsonify({'error': 'feedback_id 필수'}), 400
    if label and label not in _SUMMARY_LABEL_ENUM:
        return jsonify({'error': f'label enum 위반 ({_SUMMARY_LABEL_ENUM})'}), 400

    from sqlite_handler import db_handler as _db
    if not _db.update_ai_feedback_label(fid, label):
        return jsonify({'error': '라벨 갱신 실패 (없는 feedback_id)'}), 404
    return jsonify({'success': True, 'feedback_id': fid, 'label': label})


@api_bp.route('/api/ai-summary/labels', methods=['GET'])
def get_ai_summary_labels():
    """쌓인 라벨 조회 — Config_LLM A2 프롬프트 개선의 근거 목록.

    **이 라우트가 이 기능의 절반이다.** v6.7.68 의 F-3 는 쓰기 UI 와 조회 함수까지 만들어 놓고
    호출처를 안 만들어서 라이브 0건인 채로 죽었다. 라벨을 새로 쌓기 시작하는 이상 읽는 경로를
    같은 커밋에 넣는다(전용 화면은 실제로 쌓이는 것을 보고 판단 — 지금 만들면 빈 화면이 된다).
    """
    guard = _require_admin()
    if guard is not None:
        return guard
    try:
        limit = max(1, min(500, int(request.args.get('limit', 100))))
    except Exception:
        limit = 100
    kind = (request.args.get('kind') or '').strip() or None

    from sqlite_handler import db_handler as _db
    rows = _db.get_recent_feedbacks(limit=limit, kind=kind)
    return jsonify({
        'count': len(rows),
        'quota': {
            'hour_used': _manual_quota['hour_calls'], 'hour_cap': _MANUAL_SUMMARY_HOURLY_CAP,
            'day_used': _manual_quota['day_calls'], 'day_cap': _MANUAL_SUMMARY_DAILY_CAP,
        },
        'items': rows,
    })
