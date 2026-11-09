# -*- coding: utf-8 -*-
import sqlite3
import os
import time
import threading
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)

# [v6.7.700] **쓰기 락을 오래 쥐는 트랜잭션을 찾는 계측** - 동작은 안 바꾸고 로그만 남긴다.
#   라이브: 배포 직후 약 10분 `database is locked`(09-28 배포 3회 모두) + 평시에도 산발(09-30 03:55 커뮤니티 글 저장 실패).
#   연결 timeout 이 30초라 "locked" = **다른 쓰기가 30초 넘게 락을 쥐었다**는 뜻인데, SQLite 는 쥔 쪽을 알려주지 않는다.
#   그래서 문장 추적 콜백으로 BEGIN~COMMIT 을 재서 (1) `_TXN_SLOW_SEC` 넘게 쥔 트랜잭션을 커밋 때 호출자와 함께 남기고
#   (2) 다른 연결이 쓰기를 시작할 때 이미 오래 열린 트랜잭션이 있으면 그 시점에 "누가 몇 초째" 를 1회 남긴다.
#   추측으로 고치지 않고 이 로그로 범인을 특정한 뒤 처방한다(루트 규율: 추측 반복보다 계측 1건).
#   끄기 = env `IMT_SQLITE_TXN_TRACE=0` + 재시작.
_TXN_TRACE = (os.environ.get('IMT_SQLITE_TXN_TRACE') or '1').strip() != '0'
_TXN_SLOW_SEC = float(os.environ.get('IMT_SQLITE_TXN_SLOW_SEC') or 10)
_OPEN_TXN: dict = {}            # key -> [t0, caller, thread, reported]
_OPEN_TXN_LOCK = threading.Lock()


def _txn_caller() -> str:
    """BEGIN 을 낸 파이썬 호출 경로 요약 - 이 모듈 안 함수 1개 + 바깥 호출자 1개."""
    import sys
    f = sys._getframe(2)
    inner = outer = ""
    while f is not None:
        fn = f.f_code.co_filename.replace('\\', '/').rsplit('/', 1)[-1]
        tag = f"{f.f_code.co_name}@{fn}:{f.f_lineno}"
        if fn == 'sqlite_handler.py':
            if f.f_code.co_name not in ('_txn_trace', '_get_connection'):
                inner = inner or tag
        elif 'sqlite3' not in fn:
            outer = tag
            break
        f = f.f_back
    return " <- ".join(x for x in (inner, outer) if x) or "?"


def instrument_txn(conn, label: str = "main"):
    """연결에 트랜잭션 보유 시간 계측을 건다. 실패해도 연결은 그대로 쓴다."""
    if not _TXN_TRACE:
        return conn
    key = (label, id(conn))

    def _txn_trace(stmt):
        try:
            head = stmt.lstrip()[:8].upper()
            now = time.monotonic()
            if head.startswith('BEGIN'):
                with _OPEN_TXN_LOCK:
                    for k, v in _OPEN_TXN.items():
                        if k != key and not v[3] and now - v[0] >= _TXN_SLOW_SEC:
                            v[3] = True
                            logger.warning(f"[SQLite] 쓰기 트랜잭션 {now - v[0]:.0f}초째 열림 - {v[1]} "
                                           f"(thread {v[2]}) · 새 쓰기가 대기 중")
                    _OPEN_TXN[key] = [now, _txn_caller(), threading.current_thread().name, False]
            elif head.startswith('COMMIT') or head.startswith('ROLLBACK') or head.startswith('END'):
                with _OPEN_TXN_LOCK:
                    v = _OPEN_TXN.pop(key, None)
                if v and now - v[0] >= _TXN_SLOW_SEC:
                    # 시간은 BEGIN~종료라 **락 대기 시간도 포함**한다 - 쥔 쪽인지 기다린 쪽인지는 위 "N초째 열림" 줄과 짝지어 읽는다.
                    logger.warning(f"[SQLite] 쓰기 트랜잭션 {now - v[0]:.1f}초(락 대기 포함) 후 {head.split()[0]} - {v[1]} "
                                   f"(thread {v[2]})")
        except Exception:
            pass
    try:
        conn.set_trace_callback(_txn_trace)
    except Exception:
        pass
    return conn

# [v6.7.437] '실 요약' 술어 - AI 요약/분석 조회의 공통 필터이자 **부분 인덱스 idx_ai_notable 의 WHERE**.
#   ai_analysis 는 대부분이 '[분석 제외]'(라이브 실측 3일 21,779행 중 실 요약 660행 = 3%)라, 이 술어로
#   걸러진 3%만 색인하면 게임별 윈도우(ROW_NUMBER)가 훑는 대상이 30배 줄어든다.
#   **주의**: 아래 문자열을 바꾸면 인덱스 WHERE 와 쿼리 WHERE 가 어긋나 SQLite 가 부분 인덱스를
#   조용히 포기한다(에러 없이 느려짐). 인덱스 생성부와 get_latest_ai_analysis 가 같은 상수를 쓰도록 유지할 것.
#   분석제외 마커가 '[분석 제외]'(대괄호)라 접두 LIKE 로는 못 걸러 부분문자열(%...%)을 쓴다 (v6.6.408 이력).
AI_NOTABLE_FILTER = (
    "summary IS NOT NULL "
    "AND TRIM(summary) != '' "
    "AND summary != 'None' "
    "AND summary != 'nan' "
    "AND summary NOT LIKE '%분석 제외%' "
    "AND summary NOT LIKE '%분석제외%'"
)

# [v6.7.461] 스키마 테이블 정본 목록 - `test_sqlite_all.py` 게이트가 이 상수를 읽는다.
#   **왜 상수인가**: 구 게이트는 검사 대상 12개를 자기 파일에 하드코딩해 두고 있었고, 그 사이
#   8종(situation_snapshots·hourly_stats·anomaly_events·sub_crawler_cache·daily_price_summary·
#   visitor_log·notification_toggles·system_settings)이 신설되며 커버리지가 60% 로 떨어졌다.
#   목록을 여기(스키마를 만드는 파일)에 두면 CREATE TABLE 을 추가하는 커밋이 게이트도 함께 넓힌다.
#   **sqlite_master 조회로 대체할 수 없다** - 그건 "있는 것" 만 알려주므로 통째로 없는 테이블은
#   목록에도 안 나온다. 부재를 잡으려면 기대 목록이 코드에 있어야 한다(PATTERN-44 "술어는 상수 1곳").
#   `visitor_log_new` 는 제외 - 마이그레이션 중간 산물이라 정상 DB 에 존재하지 않는다.
SCHEMA_TABLES = (
    'account_trades', 'ai_analysis', 'ai_feedback', 'anomaly_events', 'app_commands',
    'article_comments', 'ccu_stats', 'community_posts', 'daily_price_summary',
    'hourly_stats', 'market_data', 'market_prices', 'notification_toggles',
    'odin_global_prices', 'sub_crawler_cache', 'system_config',
    'system_health', 'system_settings', 'visitor_log',
)


# [2026-09-16] **대기열 술어 1벌.** 집는 쪽(`get_posts_pending_ai_summary`)과 세는 쪽
#   (`count_posts_pending_ai_summary`)이 **글자 단위로 같아야** 한다 - 다르면 "잔량 N" 이
#   실제 배치 대상과 다른 집합을 세고, 그 숫자를 보고 내린 판단이 전부 어긋난다
#   (같은 규율이 `idx_post_verdicts_flagged` 부분 인덱스에도 걸려 있다).
_PENDING_AI_WHERE = ("p.content IS NOT NULL AND TRIM(p.content) != '' "
                     "AND p.content NOT LIKE '[%]' AND a.link IS NULL")


class SQLiteHandler:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(SQLiteHandler, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self, db_name="crawled_data.sqlite"):
        if self._initialized:
            return

        # [수정] 스크립트 위치(utils/)에서 한 단계 상위인 web_app/ 루트의 DB 참조
        # [Docker] DB_DIR 환경변수가 설정되어 있으면 해당 경로 사용 (볼륨 마운트 지원)
        current_file = os.path.abspath(__file__)
        root_dir = os.path.dirname(os.path.dirname(current_file))
        db_dir = os.environ.get('DB_DIR', root_dir)
        if db_dir != root_dir:
            os.makedirs(db_dir, exist_ok=True)

        self.db_path = os.path.join(db_dir, db_name)
        self._wal_initialized = False
        # [v6.7.58] PRAGMA 튜닝 1회 적용 플래그 — cache_size/mmap_size/temp_store/synchronous
        self._pragma_initialized = False
        self._thread_local = threading.local()

        if not os.path.exists(self.db_path):
            self._init_db()
        # [v6.7.478] **`else` 를 걷어낸다 - 신규 DB 도 아래 보강 경로를 타야 한다.**
        #   구 구조는 `_init_db()`(신규) / 아래 블록(기존)이 배타였고 **두 경로의 테이블 목록이
        #   달랐다** - `_init_db` 는 옛 '1:1 구조' 테이블만 만들어 `anomaly_events`·`hourly_stats`·
        #   `notification_toggles`·`situation_snapshots`·`system_health`·`system_settings` **6종이
        #   빠진 채 태어났다**(2026-08-03 로컬 재현). 2회차 부팅에서 이 블록이 채우므로 무증상이었을
        #   뿐이고, 첫 부팅 동안은 그 테이블을 쓰는 기능(알림 토글·이상감지·건강도·시스템 설정)이
        #   조용히 실패한다. 같은 갈라짐이 인덱스에도 있어 v6.7.473 에서 한쪽만 고쳤다 잡았다
        #   (PATTERN-44 세 번째 교훈).
        #   이 블록은 전부 `CREATE TABLE IF NOT EXISTS` + `_ensure_column`(중복 흡수)이라
        #   **신규 DB 에서 돌아도 no-op 이거나 보강뿐**이다 - 그래서 분기를 없애는 것이 안전하다.
        # 기존 DB 구조(Migration) 보완: ai_analysis 테이블 강제 확인 및 생성
        try:
            conn = self._get_connection()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ai_analysis (
                    game TEXT,
                    source TEXT,
                    detected_keywords TEXT,
                    title TEXT,
                    content TEXT,
                    summary TEXT,
                    board_name TEXT,
                    date TEXT,
                    timestamp TEXT,
                    link TEXT PRIMARY KEY,
                    is_super_keyword INTEGER,
                    AI_Score REAL,
                    sentiment TEXT,
                    severity TEXT,
                    -- [v6.7.473] 요약 저장시각(UTC). v6.7.447 은 _migrate_schema_columns 의
                    --   ALTER 로만 추가해 **신규 DB 의 CREATE 문에는 없었다**. 그 상태에서
                    --   이 컬럼을 참조하는 인덱스를 만들면 no such column 으로 초기화가 끊긴다.
                    inserted_at TEXT
                )
            """)
            conn.commit()
        except Exception as e:
            logger.error(f"[SQLite] DB 마이그레이션 오류: {e}")

        # 추가 마이그레이션: 핵심 테이블 + 보조 테이블 누락 시 강제 생성
        try:
            conn = self._get_connection()
            # 핵심 테이블 (DB 파일은 있으나 테이블이 없는 경우 대비)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS community_posts (
                    game TEXT, source TEXT, detected_keywords TEXT,
                    title TEXT, content TEXT, board_name TEXT,
                    date TEXT, timestamp TEXT, link TEXT PRIMARY KEY,
                    is_super_keyword INTEGER, is_new INTEGER DEFAULT 0,
                    inserted_at TEXT
                )
            """)
            # [v6.7.664] 게시물 제재 판정 - 공식 홈페이지(배그/POE) 유저 게시판의 삭제 대상 판정 결과.
            #   community_posts 에 컬럼을 더하지 않고 **별도 테이블**로 둔다(PATTERN-51): writer 가
            #   갈라져 있어야 판정 잡의 실패가 크롤러의 write 를 막지 않는다. 조인 키는 link(=PK).
            #   kind: 개인정보|불법거래|홍보|욕설|비방|clean  target: 게임사|특정인|불특정|none
            #   - 개인정보/불법거래/홍보 = 예외 없는 삭제 대상 / 욕설/비방 = 사람이 맥락 판단
            #     (사용자 2026-09-15: "게임사의 잘못 등으로 삭제가 안 되는 경우도 많음")
            #   - target 은 kind 가 욕설/비방일 때만 의미를 갖는다. 그 예외를 사람이 3초에 거르는 축.
            #   judged_at 은 **UTC ISO8601** 이다 - 같은 DB 의 community_posts.timestamp 는 KST 다(9시간차).
            conn.execute("""
                CREATE TABLE IF NOT EXISTS post_verdicts (
                    link TEXT PRIMARY KEY,
                    kind TEXT, target TEXT,
                    content_hash TEXT, judged_at TEXT,
                    fail INTEGER DEFAULT 0
                )
            """)
            # 제재 목록 조회(kind <> 'clean')용 부분 인덱스. WHERE 절을 쿼리와 **글자 단위로 맞춘다** —
            #   어긋나면 SQLite 가 무경고로 인덱스를 포기한다(PATTERN-44).
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_post_verdicts_flagged
                ON post_verdicts(link) WHERE kind IS NOT NULL AND kind <> 'clean'
            """)
            # [v6.7.669] 출처 필터 인덱스. **없으면 제재 조회가 수십만 행 풀스캔이 된다.**
            #   `?source=` 만 걸면 LIMIT 이 금방 차서 체감이 없지만, `&flagged=1` 을 겹치면
            #   전 테이블에 해당 행이 190건뿐이라 LIMIT 이 영영 안 차고 `idx_posts_timestamp` 를
            #   끝까지 훑는다. 라이브 실측 콜드 **106.5초** -> 인덱스 후 0.022초.
            #   `(source, timestamp)` 순서가 중요하다 - source 로 좁힌 뒤 timestamp 정렬을
            #   인덱스가 그대로 만족시켜야 ORDER BY 가 공짜가 된다(뒤집으면 다시 스캔이다).
            #   생성 비용은 라이브에서 23초였고 IF NOT EXISTS 라 재배포 시 0 이다.
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_posts_source_ts
                ON community_posts(source, timestamp)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS article_comments (
                    comment_id TEXT PRIMARY KEY, title TEXT, comment TEXT,
                    time TEXT, timestamp TEXT, link TEXT, comment_link TEXT,
                    author TEXT, is_new INTEGER DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS account_trades (
                    game TEXT, sale_status TEXT, title TEXT, server TEXT,
                    type TEXT, price TEXT, date TEXT, time TEXT,
                    link TEXT PRIMARY KEY
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS market_prices (
                    game_name TEXT, server TEXT, date TEXT,
                    trade_count INTEGER, cash_total REAL, goods_total REAL,
                    avg_price REAL, category TEXT,
                    PRIMARY KEY (game_name, server, date, category)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS system_config (
                    config_key TEXT PRIMARY KEY,
                    config_value TEXT,
                    updated_at TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS app_commands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command TEXT,
                    params TEXT,
                    status TEXT DEFAULT 'pending', -- pending, running, done, error
                    result TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS market_data (
                    id TEXT PRIMARY KEY,
                    game TEXT,
                    source TEXT,
                    data_json TEXT,
                    timestamp TEXT
                )
            """)
            # [v6.7.629] situation_snapshots CREATE 제거 - 상황판 합성 파이프라인 은퇴(소비자 0). 라이브 DB 의
            #   기존 테이블(약 7만 행)은 그대로 두었다 - 필요하면 사람이 DROP TABLE situation_snapshots.
            # [v6.7.438] 평시 베이스라인 이상감지 - 파생 집계 + 판정 이력.
            #   hourly_stats: (시간버킷 x 게임 x 출처) 집계. 원본을 읽어 만드는 **파생**이라 언제든
            #     재생성 가능하고, 원본을 나중에 아카이브로 옮겨도 시계열 통계는 여기 남는다
            #     (보관 정책 결정을 데이터 손실과 분리하는 장치). 하루 ~770행이라 무게 없음.
            #   anomaly_events: 판정 결과. PK(bucket,game,signal) 가 곧 알림 중복 방지 - 판정 엔진은
            #     매 사이클 최근 구간을 replay 하므로 같은 버킷을 다시 계산해도 INSERT OR IGNORE 로
            #     한 번만 알린다(프로세스 재시작에도 안전한 무상태 설계).
            conn.execute("""
                CREATE TABLE IF NOT EXISTS hourly_stats (
                    bucket TEXT NOT NULL,
                    game TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    posts INTEGER DEFAULT 0,
                    notable INTEGER DEFAULT 0,
                    critical INTEGER DEFAULT 0,
                    negative INTEGER DEFAULT 0,
                    PRIMARY KEY (bucket, game, source)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_hourly_game_bucket ON hourly_stats(game, bucket DESC)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS anomaly_events (
                    bucket TEXT NOT NULL,
                    game TEXT NOT NULL,
                    signal TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    value INTEGER,
                    baseline REAL,
                    ratio REAL,
                    detected_at TEXT NOT NULL,
                    PRIMARY KEY (bucket, game, signal)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_anomaly_detected ON anomaly_events(detected_at DESC)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ccu_stats (
                    game TEXT,
                    server_name TEXT,
                    current_ccu INTEGER,
                    before_ccu INTEGER,
                    change_1m REAL,
                    change_5m REAL,
                    last_updated TEXT,
                    PRIMARY KEY (game, server_name)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS odin_global_prices (
                    id TEXT PRIMARY KEY,
                    date TEXT,
                    server_name TEXT,
                    seller_count INTEGER,
                    avg_price_1k REAL,
                    updated_at TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sub_crawler_cache (
                    crawler_key TEXT,
                    date TEXT,
                    results_json TEXT,
                    updated_at TEXT,
                    PRIMARY KEY (crawler_key, date)
                )
            """)
            # 일별 시세 요약 (트렌드 차트용, 영구 보관)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_price_summary (
                    date TEXT,
                    game TEXT,
                    category TEXT,
                    avg_price REAL,
                    total_count INTEGER,
                    min_price REAL,
                    max_price REAL,
                    PRIMARY KEY (date, game, category)
                )
            """)
            # 성능 인덱스 (IF NOT EXISTS로 중복 안전)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_timestamp ON community_posts(timestamp)")
            # [v6.7.447] AI 요약 워커의 pending 조회 전용 - 없으면 수십만 행 전수 스캔(라이브 실측
            #   1.3~2.1초/회, 60초마다). 워커는 `inserted_at >= 부팅시각` 만 보는데 그 조건에
            #   인덱스가 없어 ORDER BY 인덱스를 역순으로 끝까지 걷고 있었다(PATTERN-44 형).
            #   **한가할수록 비싸진다** - LIMIT 30 을 못 채우면 끝까지 가므로.
            conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_game ON community_posts(game)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_timestamp ON article_comments(timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_account_date ON account_trades(date)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_market_category ON market_prices(category, date)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_link ON ai_analysis(link)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_timestamp ON ai_analysis(timestamp)")
            # [v6.7.60] 부분/복합 인덱스 — cold-start 폴백 + ai_analysis 윈도우 + account_trades 게임 필터
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_game_timestamp ON ai_analysis(game, timestamp DESC)")
            # [v6.7.437] 실 요약 전용 부분 인덱스 - 게임별 윈도우가 '[분석 제외]'(전체의 ~97%)까지
            #   전부 훑던 것을 3%만 훑도록 축소. WHERE 는 AI_NOTABLE_FILTER 와 **반드시 동일**해야
            #   SQLite 가 이 인덱스를 채택한다(불일치 시 무경고 미사용). 400K행 생성 비용 실측 1.1s.
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_ai_notable ON ai_analysis(game, timestamp DESC) WHERE {AI_NOTABLE_FILTER}")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_account_game_date ON account_trades(game, date DESC, time DESC)")
            logger.info("[SQLite] 부분/복합 인덱스 적용 — idx_ai_game_timestamp, idx_account_game_date")
            # [v6.7.287] visitor_log — 브라우저 쿠키 토큰(visitor_id) 축으로 전환. PK (visitor_id, ip).
            #   공유기(NAT) 뒤 다수 PC 가 같은 공인 IP 를 공유해도 브라우저별로 행 분리.
            #   IP 는 접속 위치 식별용 컬럼으로 보존.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS visitor_log (
                    visitor_id TEXT NOT NULL,
                    ip TEXT NOT NULL,
                    device TEXT,
                    name TEXT,
                    ua TEXT,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    visit_count INTEGER DEFAULT 1,
                    referer TEXT,
                    accept_lang TEXT,
                    xff TEXT,
                    PRIMARY KEY (visitor_id, ip)
                )
            """)
            # 구 스키마(visitor_id 컬럼 부재) 감지 시 데이터 보존 재빌드 — 옛 행은 legacy:<ip>:<device> 합성 토큰.
            _vcols = [r[1] for r in conn.execute("PRAGMA table_info(visitor_log)").fetchall()]
            if 'visitor_id' not in _vcols:
                conn.execute("DROP TABLE IF EXISTS visitor_log_new")
                conn.execute("""
                    CREATE TABLE visitor_log_new (
                        visitor_id TEXT NOT NULL, ip TEXT NOT NULL, device TEXT, name TEXT, ua TEXT,
                        first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, visit_count INTEGER DEFAULT 1,
                        PRIMARY KEY (visitor_id, ip)
                    )
                """)
                conn.execute("""
                    INSERT OR IGNORE INTO visitor_log_new (visitor_id, ip, device, name, ua, first_seen, last_seen, visit_count)
                    SELECT 'legacy:'||ip||':'||device, ip, device, name, ua, first_seen, last_seen, visit_count
                    FROM visitor_log
                """)
                conn.execute("DROP TABLE visitor_log")
                conn.execute("ALTER TABLE visitor_log_new RENAME TO visitor_log")
                conn.commit()
                logger.info("[SQLite] visitor_log 마이그레이션 — visitor_id 축 전환 (구 데이터 legacy 토큰 보존)")
            # [v6.7.304] 진단 컬럼 추가(기존 DB) — 시크릿/쿠키없음 접속 식별용: 유입경로(referer)·로케일(accept_lang)·포워드체인(xff).
            _vcols_diag = [r[1] for r in conn.execute("PRAGMA table_info(visitor_log)").fetchall()]
            for _c in ('referer', 'accept_lang', 'xff'):
                if _c not in _vcols_diag:
                    conn.execute(f"ALTER TABLE visitor_log ADD COLUMN {_c} TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_visitor_last_seen ON visitor_log(last_seen)")
            # [v6.6.343] notification_toggles — 웹앱에서 발송되는 카카오워크 알림 카테고리별 ON/OFF.
            # key: 카테고리 식별자(ai_summary, keyword_alert, nightly_digest, ccu_alert,
            # crawler_healing, crawler_restart, crash_report, gsheet_write 등 확장 가능).
            # enabled: 1=ON, 0=OFF. 기본값은 코드 레벨(get_notification_toggles) 에서 미존재 시 True 반환.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS notification_toggles (
                    key TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                )
            """)
            # [v6.6.353] system_settings — 웹앱 전역 키/값 설정. 관리자 외 접근 차단 같은
            # 서버 단일 source-of-truth 플래그를 저장.
            # value 는 TEXT 로 임의값 수용 ("true"/"false"/숫자/JSON 문자열 등 호출자가 해석).
            conn.execute("""
                CREATE TABLE IF NOT EXISTS system_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TEXT NOT NULL
                )
            """)
            # [v6.7.68] ai_feedback — AI 분류/요약 오분류 운영자 피드백 저장.
            # link 가 동일 글이라도 라벨링은 여러 번 가능 (id PK). save_ai_feedback / get_recent_feedbacks 헬퍼.
            # [v6.7.459] kind/gemma_verdict/result/label 4종 추가 — 수동 AI 요약 요청을 같은 테이블에
            #   `kind='summary_request'` 로 적재한다. 테이블을 나누지 않는 이유: 프롬프트 개선은
            #   '1차가 놓친 것'(summary_request)과 '2차가 틀린 것'(misclassify)을 **한 화면에서**
            #   같이 봐야 근거가 되는데, 테이블이 둘이면 읽는 경로도 둘로 갈라져 결국 아무도 안 본다
            #   (F-3 가 라이브 0건으로 죽어 있던 것이 그 선례다).
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ai_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    link TEXT NOT NULL,
                    original_sentiment TEXT,
                    expected_sentiment TEXT,
                    original_severity TEXT,
                    expected_severity TEXT,
                    original_summary TEXT,
                    expected_summary TEXT,
                    note TEXT,
                    admin_ip TEXT,
                    ts TEXT NOT NULL,
                    kind TEXT DEFAULT 'misclassify',
                    gemma_verdict TEXT,
                    result TEXT,
                    label TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_feedback_link ON ai_feedback(link)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_feedback_ts ON ai_feedback(ts DESC)")
            # [v6.7.70] system_health — F-7 시스템 건강도 1분 sample 누적. metric=문자열 키, value=float.
            # 1주 시계열(168h) 그래프 + 임계치 알림용. purge_old_health 로 14일 보관.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS system_health (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    metric TEXT NOT NULL,
                    value REAL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_health_ts_metric ON system_health(ts DESC, metric)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_health_metric_ts ON system_health(metric, ts DESC)")
            conn.commit()
        except Exception as e:
            logger.error(f"[SQLite] 시스템 설정 및 기타 테이블 마이그레이션 오류: {e}")

        # [v6.6.219] 기존 DB의 누락 컬럼을 자동 ALTER TABLE로 추가 (idempotent)
        # 운영 중 볼륨을 날리지 않고도 새 컬럼 반영 가능하도록 안전장치
        self._migrate_schema_columns()

        # [v6.7.473] 종합 동향 리포트(bg/trend_digest.py) 구간 조회 전용 부분 인덱스.
        #   **반드시 _migrate_schema_columns() 뒤에 둔다.** 이 인덱스는 ai_analysis.inserted_at 을
        #   참조하는데 그 컬럼은 위 마이그레이션이 ALTER 로 채워주므로, 앞에 두면 옛 스키마 DB 에서
        #   `no such column: inserted_at` 이 나고 **그 예외가 같은 블록의 뒤쪽 테이블 생성까지
        #   통째로 중단시킨다**(visitor_log·system_settings·ai_feedback 등). 로컬 신규 DB 로
        #   실측 재현했고, 같은 함정이 v6.7.459 에도 있었다(community_posts.inserted_at ↔
        #   idx_posts_inserted_at). 라이브는 컬럼이 이미 있어 무증상이었을 뿐 잠복 결함이다.
        #   WHERE 는 AI_NOTABLE_FILTER - 조회 쿼리도 같은 상수를 써야 채택된다(PATTERN-44).
        #   리포트가 게시시각이 아니라 저장시각으로 구간을 자르는 이유는 그 모듈 docstring(PATTERN-46).
        #   [v6.7.478] `idx_posts_inserted_at` 도 여기로 옮겼다 - **같은 함정이 그쪽에 남아 있었다.**
        #   v6.7.459 가 이 결함을 이미 겪고 CREATE TABLE 에 컬럼을 넣어 고쳤는데 그 처방은
        #   **신규 DB 경로만** 덮었다. 옛 스키마 DB(컬럼 없음)에서는 여전히 `no such column` 이 나고
        #   그 예외가 같은 try 블록의 뒤쪽을 끊는다 - 2026-08-03 스텁 DB 로 실측 재현.
        #   v6.7.473 이 ai_analysis 쪽만 옮긴 것이 원인이다(같은 종류를 한 번에 훑지 않았다).
        try:
            conn = self._get_connection()
            with conn:
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_ai_inserted_notable "
                    f"ON ai_analysis(inserted_at) WHERE {AI_NOTABLE_FILTER}"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_posts_inserted_at ON community_posts(inserted_at)")
        except Exception as e:
            logger.error(f"[SQLite] inserted_at 인덱스 생성 실패 (해당 조회가 전수 스캔이 된다): {e}")

        self._initialized = True

    def _ensure_column(self, conn, table, column, spec):
        """테이블에 해당 컬럼이 없으면 ALTER TABLE ADD COLUMN으로 추가.

        spec 예: 'INTEGER DEFAULT 0', 'TEXT', 'REAL DEFAULT 0.0'
        SQLite는 ALTER ADD COLUMN 시 NOT NULL(DEFAULT 없음)과 PK 추가 불가 — 선택적 컬럼만 대상.
        """
        try:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if column in cols:
                return
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")
            logger.info(f"[SQLite-migrate] {table}.{column} {spec} 추가 완료")
        except Exception as e:
            logger.warning(f"[SQLite-migrate] {table}.{column} 추가 실패(무시): {e}")

    def _migrate_schema_columns(self):
        """테이블별 선택 컬럼이 없으면 자동 추가.

        새 컬럼을 추가할 때 이 리스트에 한 줄만 더 넣으면 기존 DB에도 반영됨.
        볼륨(imt_db_data) 삭제 없이 누적 데이터 보존하면서 스키마 확장 가능.
        """
        conn = self._get_connection()
        try:
            with conn:
                # community_posts — 크롤러 수집 후 추가된 메타 컬럼
                self._ensure_column(conn, 'community_posts', 'is_super_keyword', 'INTEGER DEFAULT 0')
                self._ensure_column(conn, 'community_posts', 'is_new', 'INTEGER DEFAULT 0')
                self._ensure_column(conn, 'community_posts', 'content', 'TEXT')
                self._ensure_column(conn, 'community_posts', 'board_name', 'TEXT')
                self._ensure_column(conn, 'community_posts', 'detected_keywords', 'TEXT')
                self._ensure_column(conn, 'community_posts', 'date', 'TEXT')
                self._ensure_column(conn, 'community_posts', 'timestamp', 'TEXT')
                # inserted_at: SQLite는 ALTER TABLE에 non-constant default 불허 → default 없이 추가.
                # 기존 행은 NULL로 유지(필터링에서 자동 제외), 신규 INSERT는 save_community_posts에서 명시 삽입.
                self._ensure_column(conn, 'community_posts', 'inserted_at', 'TEXT')
                # [2026-09-16] **제목이 원본에서 바뀐 사실을 남긴다.** 감시 도구에서 의미 있는
                #   제목을 `..` 같은 무의미 문자열로 바꾸는 것은 **글을 묻으려는 행동**이라
                #   그 자체가 신호다(디스코드 쪽은 이미 `edited` 를 추적하는데 커뮤니티 글만
                #   빠져 있었다). 덮어쓰기만 하면 그 사실이 사라지므로 최초 제목을 보존한다.
                #   실사고 2026-09-16: POE1 도움말 `view-thread/4005967` 이 `산성의 중재자` ->
                #   `..` 로 바뀌었는데 화면에는 옛 제목이 남아, 게시판에서 본 글을 IME 에서
                #   찾을 수 없었다(사용자 *"왜 갑자기 사라졌지"*).
                #   `title_prev` 는 **최초 제목만** 담는다 - 여러 번 바뀌어도 "원래 무엇이었나"
                #   가 알고 싶은 값이고, 전체 이력을 쌓으려면 별도 표가 필요하다(지금은 과하다).
                self._ensure_column(conn, 'community_posts', 'title_prev', 'TEXT')
                self._ensure_column(conn, 'community_posts', 'title_changed_at', 'TEXT')

                # [v6.7.447] ai_analysis.inserted_at — **요약 저장 시각**(UTC). 관측 공백 해소용.
                #   지금까지 이 테이블엔 게시 시각(timestamp)만 있어 "수집 → 요약까지 실제로 몇 분
                #   걸렸나" 를 사후에 잴 방법이 없었다. 지연 문제를 체감으로만 발견하게 되는 구조라
                #   (WORK_PROTOCOL §6-A 규율 6 '관측 공백을 의심하라'), 단계별 추정 합산 대신
                #   `ai.inserted_at - p.inserted_at` 으로 실제 분포를 바로 뽑을 수 있게 한다.
                #   기존 행은 NULL 유지(소급 불가) - 측정은 이 시점 이후분부터 유효.
                self._ensure_column(conn, 'ai_analysis', 'inserted_at', 'TEXT')

                # [v6.7.459] ai_feedback — 수동 AI 요약 요청 라벨 4종. 기존 DB(F-3 스키마)에도 반영.
                #   kind 기본값을 'misclassify' 로 두면 기존 행(라이브 0건이라 실질 무의미하나
                #   로컬/테스트 DB 에는 있을 수 있다)이 자동으로 옛 종류로 분류돼 신규 라벨과 안 섞인다.
                self._ensure_column(conn, 'ai_feedback', 'kind', "TEXT DEFAULT 'misclassify'")
                self._ensure_column(conn, 'ai_feedback', 'gemma_verdict', 'TEXT')
                self._ensure_column(conn, 'ai_feedback', 'result', 'TEXT')
                self._ensure_column(conn, 'ai_feedback', 'label', 'TEXT')

                # article_comments
                self._ensure_column(conn, 'article_comments', 'is_new', 'INTEGER DEFAULT 0')
                self._ensure_column(conn, 'article_comments', 'author', 'TEXT')
                self._ensure_column(conn, 'article_comments', 'comment_link', 'TEXT')
                self._ensure_column(conn, 'article_comments', 'time', 'TEXT')
                self._ensure_column(conn, 'article_comments', 'timestamp', 'TEXT')
                self._ensure_column(conn, 'article_comments', 'title', 'TEXT')
                # [v6.6.238] 댓글 게임 분류 필드 (comment_link grpid 파싱 기반)
                self._ensure_column(conn, 'article_comments', 'game', 'TEXT')

                # ai_analysis
                self._ensure_column(conn, 'ai_analysis', 'summary', 'TEXT')
                self._ensure_column(conn, 'ai_analysis', 'content', 'TEXT')
                self._ensure_column(conn, 'ai_analysis', 'is_super_keyword', 'INTEGER DEFAULT 0')
                self._ensure_column(conn, 'ai_analysis', 'AI_Score', 'REAL DEFAULT 0.0')
                self._ensure_column(conn, 'ai_analysis', 'detected_keywords', 'TEXT')
                self._ensure_column(conn, 'ai_analysis', 'board_name', 'TEXT')
                self._ensure_column(conn, 'ai_analysis', 'date', 'TEXT')
                self._ensure_column(conn, 'ai_analysis', 'timestamp', 'TEXT')
                # Phase A (v6.6.388): 상황판 탭용 sentiment/severity 컬럼. 기존 행은 NULL.
                self._ensure_column(conn, 'ai_analysis', 'sentiment', 'TEXT')
                self._ensure_column(conn, 'ai_analysis', 'severity', 'TEXT')
        except Exception as e:
            logger.error(f"[SQLite-migrate] 스키마 마이그레이션 실패(무시): {e}")

    def _get_connection(self):
        """스레드-로컬 커넥션을 재사용한다. 닫혔거나 아직 없으면 새로 생성.
        단일 프로세스 앱이므로 thread-local 캐시로 connection churn을 방지한다."""
        tl = self._thread_local
        conn = getattr(tl, 'conn', None)
        if conn is not None:
            try:
                # 커넥션이 살아있는지 가볍게 확인
                conn.execute("SELECT 1")
                return conn
            except Exception:
                # 죽어 있으면 버리고 새로 생성
                try:
                    conn.close()
                except Exception:
                    pass
                tl.conn = None

        conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        instrument_txn(conn)            # [v6.7.700] 락 보유 계측
        if not self._wal_initialized:
            with self._lock:
                if not self._wal_initialized:  # double-check
                    conn.execute("PRAGMA journal_mode=WAL")
                    self._wal_initialized = True
        # [v6.7.58] PRAGMA 튜닝 — 80k+ row 핫쿼리 비용 절감.
        # [v6.7.62] cache_size/mmap_size/temp_store/synchronous 는 connection-scope 이므로 매 conn 마다 적용.
        # WAL 은 DB 파일에 영구 기록되므로 1회만, PRAGMA 튜닝은 thread-local conn 재생성·신규 thread 진입 시마다 필요.
        try:
            conn.execute("PRAGMA cache_size=-20000")
            conn.execute("PRAGMA mmap_size=268435456")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA synchronous=NORMAL")
            if not self._pragma_initialized:
                with self._lock:
                    if not self._pragma_initialized:
                        self._pragma_initialized = True
                        logger.info("[SQLite] PRAGMA 튜닝 적용 — cache=20MB, mmap=256MB, temp=MEM, sync=NORMAL (per-connection)")
        except Exception as e:
            logger.warning(f"[SQLite] PRAGMA 튜닝 실패(무시): {e}")
        tl.conn = conn
        return conn

    def _init_db(self):
        """필요한 테이블들을 생성합니다. (구글 시트와 1:1 Snapshot 동기화 구조)"""
        conn = self._get_connection()
        try:
            with conn:
                # [중요] Snapshot DB: 시트 데이터는 재시작 시 재로드
                # community_posts는 내용 크롤러가 채운 content를 보존하기 위해 DROP하지 않음
                conn.execute("DROP TABLE IF EXISTS article_comments")
                conn.execute("DROP TABLE IF EXISTS account_trades")
                conn.execute("DROP TABLE IF EXISTS market_prices")

                # 1. 커뮤니티 게시글 (content 보존을 위해 CREATE IF NOT EXISTS)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS community_posts (
                        game TEXT,
                        source TEXT,
                        detected_keywords TEXT,
                        title TEXT,
                        content TEXT,
                        board_name TEXT,
                        date TEXT,
                        timestamp TEXT,
                        link TEXT PRIMARY KEY,
                        is_super_keyword INTEGER,
                        is_new INTEGER DEFAULT 0,
                        -- [v6.7.459] FIX(선재 결함): 이 컬럼이 없어 아래 idx_posts_inserted_at 생성이
                        --   'no such column' 로 터지고, **같은 try 블록의 뒤쪽 테이블(visitor_log·
                        --   ai_feedback)이 통째로 안 만들어졌다**(신규 DB 한정 - 라이브는 볼륨이
                        --   오래돼 컬럼이 이미 있어 무증상). v6.7.447 에서 인덱스만 추가하고 신규 DB
                        --   경로의 CREATE 문에는 컬럼을 안 넣은 것이 원인. `_migrate_schema_columns`
                        --   는 기존 DB 분기에서만 호출돼 신규 DB 를 못 구했다.
                        inserted_at TEXT
                    )
                """)
                # 마이그레이션: 기존 DB에 is_new 칼럼이 없으면 추가
                try:
                    conn.execute("ALTER TABLE community_posts ADD COLUMN is_new INTEGER DEFAULT 0")
                except Exception:
                    pass  # 이미 존재하면 무시

                # 1-1. AI 분석 데이터 (시트 1:1 매칭)
                conn.execute("DROP TABLE IF EXISTS ai_analysis")
                conn.execute("""
                    CREATE TABLE ai_analysis (
                        game TEXT,
                        source TEXT,
                        detected_keywords TEXT,
                        title TEXT,
                        content TEXT,
                        summary TEXT,
                        board_name TEXT,
                        date TEXT,
                        timestamp TEXT,
                        link TEXT PRIMARY KEY,
                        is_super_keyword INTEGER,
                        AI_Score REAL,
                        is_new INTEGER DEFAULT 0,
                        sentiment TEXT,
                        severity TEXT,
                        inserted_at TEXT   -- [v6.7.473] 위 CREATE TABLE IF NOT EXISTS 주석 참조
                    )
                """)

                # 2. 댓글 (Contents 시트 1:1 매칭 - 6개 컬럼 + author)
                conn.execute("""
                    CREATE TABLE article_comments (
                        comment_id TEXT PRIMARY KEY,
                        title TEXT,
                        comment TEXT,
                        time TEXT,
                        timestamp TEXT,
                        link TEXT,
                        comment_link TEXT,
                        author TEXT,
                        is_new INTEGER DEFAULT 0
                    )
                """)

                # 3. 계정 거래 (시트 1:1 매칭 - 9개 컬럼)
                conn.execute("""
                    CREATE TABLE account_trades (
                        game TEXT,
                        sale_status TEXT,
                        title TEXT,
                        server TEXT,
                        type TEXT,
                        price TEXT,
                        date TEXT,
                        time TEXT,
                        link TEXT PRIMARY KEY
                    )
                """)

                # 4. 자사/타사 시세 (시트 1:1 매칭 - 7개 컬럼)
                conn.execute("""
                    CREATE TABLE market_prices (
                        game_name TEXT,
                        server TEXT,
                        date TEXT,
                        trade_count INTEGER,
                        cash_total REAL,
                        goods_total REAL,
                        avg_price REAL,
                        category TEXT,
                        PRIMARY KEY (game_name, server, date, category)
                    )
                """)

                # 5. CCU 데이터 테이블 (정적 유지)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS ccu_stats (
                        game TEXT,
                        server_name TEXT,
                        current_ccu INTEGER,
                        before_ccu INTEGER,
                        change_1m REAL,
                        change_5m REAL,
                        last_updated TEXT,
                        PRIMARY KEY (game, server_name)
                    )
                """)

                # 6. 시스템 설정 (구글 시트의 Config/Keywords 캐시용)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS system_config (
                        config_key TEXT PRIMARY KEY,
                        config_value TEXT,
                        updated_at TEXT
                    )
                """)

                # 7. 명령 큐 (웹 뷰어 -> 터널 앱 명령 전달용)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS app_commands (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        command TEXT,
                        params TEXT,
                        status TEXT DEFAULT 'pending', -- pending, running, done, error
                        result TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    )
                """)

                # 8. 오딘 글로벌 가격 전용 테이블 (구조가 상이하므로 분리)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS odin_global_prices (
                        id TEXT PRIMARY KEY,
                        date TEXT,
                        server_name TEXT,
                        seller_count INTEGER,
                        avg_price_1k REAL,
                        updated_at TEXT
                    )
                """)

                # 9. 기타 범용 마켓 데이터 (Fallback)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS market_data (
                        id TEXT PRIMARY KEY,
                        game TEXT,
                        source TEXT,
                        data_json TEXT,
                        timestamp TEXT
                    )
                """)

                # 10. 서브 크롤러 결과 JSON 캐시 (Google Sheets 읽기 대체용, 원본 컬럼 그대로 보존)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS sub_crawler_cache (
                        crawler_key TEXT,
                        date TEXT,
                        results_json TEXT,
                        updated_at TEXT,
                        PRIMARY KEY (crawler_key, date)
                    )
                """)
                # 11. 일별 시세 요약 (트렌드 차트용, purge 대상 아님 — 영구 보관)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS daily_price_summary (
                        date TEXT,
                        game TEXT,
                        category TEXT,
                        avg_price REAL,
                        total_count INTEGER,
                        min_price REAL,
                        max_price REAL,
                        PRIMARY KEY (date, game, category)
                    )
                """)

                # 12. 성능 인덱스
                conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_timestamp ON community_posts(timestamp)")
                # [v6.7.447] AI 요약 워커의 pending 조회 전용 - 없으면 수십만 행 전수 스캔(라이브 실측
                #   1.3~2.1초/회, 60초마다). 워커는 `inserted_at >= 부팅시각` 만 보는데 그 조건에
                #   인덱스가 없어 ORDER BY 인덱스를 역순으로 끝까지 걷고 있었다(PATTERN-44 형).
                #   **한가할수록 비싸진다** - LIMIT 30 을 못 채우면 끝까지 가므로.
                conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_inserted_at ON community_posts(inserted_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_game ON community_posts(game)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_timestamp ON article_comments(timestamp)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_account_date ON account_trades(date)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_market_category ON market_prices(category, date)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_link ON ai_analysis(link)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_timestamp ON ai_analysis(timestamp)")
                # [v6.7.60] 부분/복합 인덱스 — cold-start 폴백 + ai_analysis 윈도우 + account_trades 게임 필터
                conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_game_timestamp ON ai_analysis(game, timestamp DESC)")
                # [v6.7.437] 실 요약 전용 부분 인덱스 - 게임별 윈도우가 '[분석 제외]'(전체의 ~97%)까지
                #   전부 훑던 것을 3%만 훑도록 축소. WHERE 는 AI_NOTABLE_FILTER 와 **반드시 동일**해야
                #   SQLite 가 이 인덱스를 채택한다(불일치 시 무경고 미사용). 400K행 생성 비용 실측 1.1s.
                conn.execute(f"CREATE INDEX IF NOT EXISTS idx_ai_notable ON ai_analysis(game, timestamp DESC) WHERE {AI_NOTABLE_FILTER}")
                # [v6.7.473] 동향 리포트 구간 조회용(bg/trend_digest.py). **신규 DB 경로 전용** - 기존 DB 는
                #   __init__ 의 else 분기가 _migrate_schema_columns() 뒤에서 만든다(그쪽은 ALTER 로 컬럼을
                #   채운 뒤여야 하므로 순서가 강제된다). 여기서는 바로 위 CREATE TABLE 에 inserted_at 이
                #   있으므로 안전하다. 두 경로 모두에 두지 않으면 한쪽 DB 만 전수 스캔이 된다.
                conn.execute(f"CREATE INDEX IF NOT EXISTS idx_ai_inserted_notable ON ai_analysis(inserted_at) WHERE {AI_NOTABLE_FILTER}")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_account_game_date ON account_trades(game, date DESC, time DESC)")
                logger.info("[SQLite] 부분/복합 인덱스 적용 — idx_ai_game_timestamp, idx_account_game_date")

                # 13. [v6.7.287] 관리자 기기 접속 로그 (visitor_id 쿠키 토큰 + ip 복합 PK, 최종 갱신 시간만 유지)
                #   삭제 없이 누적: 같은 (브라우저,IP) 재방문 시 last_seen 갱신 + visit_count 증가.
                #   수동 설정된 name 은 보존 (UA 자동 감지 라벨로 덮어쓰지 않음). IP 는 외부 접근 식별용 보존.
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS visitor_log (
                        visitor_id TEXT NOT NULL,
                        ip TEXT NOT NULL,
                        device TEXT,
                        name TEXT,
                        ua TEXT,
                        first_seen TEXT NOT NULL,
                        last_seen TEXT NOT NULL,
                        visit_count INTEGER DEFAULT 1,
                        referer TEXT,
                        accept_lang TEXT,
                        xff TEXT,
                        PRIMARY KEY (visitor_id, ip)
                    )
                """)
                # [v6.7.304] 진단 컬럼 추가(기존 DB) — 이 init 경로도 동일 마이그레이션 보장.
                _vcols_diag2 = [r[1] for r in conn.execute("PRAGMA table_info(visitor_log)").fetchall()]
                for _c in ('referer', 'accept_lang', 'xff'):
                    if _c not in _vcols_diag2:
                        conn.execute(f"ALTER TABLE visitor_log ADD COLUMN {_c} TEXT")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_visitor_last_seen ON visitor_log(last_seen)")

                # 14. [v6.7.68] ai_feedback — AI 분류/요약 오분류 운영자 피드백 저장 (F-3).
                #     [v6.7.459] kind/gemma_verdict/result/label 추가 (위 CREATE 와 동일 스키마 유지 — 두 곳이
                #     갈라지면 신규 DB 와 기존 DB 의 컬럼 구성이 달라진다).
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS ai_feedback (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        link TEXT NOT NULL,
                        original_sentiment TEXT,
                        expected_sentiment TEXT,
                        original_severity TEXT,
                        expected_severity TEXT,
                        original_summary TEXT,
                        expected_summary TEXT,
                        note TEXT,
                        admin_ip TEXT,
                        ts TEXT NOT NULL,
                        kind TEXT DEFAULT 'misclassify',
                        gemma_verdict TEXT,
                        result TEXT,
                        label TEXT
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_feedback_link ON ai_feedback(link)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_feedback_ts ON ai_feedback(ts DESC)")
        except Exception as e:
            logger.error(f"[SQLite] 1:1 구조 개편 초기화 실패: {e}")

    def upsert_ccu_stats(self, game, server_name, current, before, change_1m, change_5m, updated_at):
        """CCU 데이터를 업데이트하거나 삽입합니다. (소수점 2자리 반올림 적용)"""
        conn = self._get_connection()
        try:
            # 비율(0.006)을 백분율(0.61)로 변환하고 반올림
            rounded_1m = round(float(change_1m) * 100, 2) if change_1m is not None else 0
            rounded_5m = round(float(change_5m) * 100, 2) if change_5m is not None else 0
            
            with conn:
                conn.execute("""
                    INSERT OR REPLACE INTO ccu_stats 
                    (game, server_name, current_ccu, before_ccu, change_1m, change_5m, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (game, server_name, current, before, rounded_1m, rounded_5m, updated_at))
        except Exception as e:
            logger.error(f"[SQLite] CCU 저장 실패 ({game}-{server_name}): {e}")

    def save_community_posts(self, posts_list):
        """커뮤니티 게시글 목록을 저장합니다. (시트 1:1 매핑)"""
        if not posts_list:
            return
        conn = self._get_connection()
        try:
            rows = []
            for p in posts_list:
                # 시트 헤더 순서와 동일하게 매핑
                kws = p.get('detected_keywords', [])
                if isinstance(kws, list):
                    kw_str = ", ".join(map(str, kws))
                else:
                    kw_str = str(kws)

                # 만약 이미 '['로 시작하는 문자열이라면 정제 (대괄호/따옴표 제거)
                if isinstance(kw_str, str) and kw_str.startswith('[') and kw_str.endswith(']'):
                    kw_str = kw_str.strip('[]').replace("'", "").replace('"', "")

                rows.append((
                    p.get('game'), p.get('source'), kw_str,
                    p.get('title'), p.get('board_name'),
                    p.get('date'), p.get('timestamp'), p.get('link'),
                    1 if p.get('is_super_keyword') else 0
                ))

            # 이번 저장으로 바뀐 것만 고르기 위한 기준 시각(UTC). `strftime` 과 같은 축이어야 한다.
            _stamp = conn.execute("SELECT strftime('%Y-%m-%d %H:%M:%S','now')").fetchone()[0]
            with conn:
                # inserted_at: SQLite가 INSERT 시점의 UTC 타임스탬프 자동 기록 (본문 크롤러 필터용)
                # ON CONFLICT DO UPDATE는 inserted_at을 건드리지 않음 (첫 삽입 시점 유지)
                conn.executemany("""
                    INSERT INTO community_posts
                    (game, source, detected_keywords, title, content, board_name, date, timestamp, link, is_super_keyword, inserted_at)
                    VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%S','now'))
                    ON CONFLICT(link) DO UPDATE SET
                        game = excluded.game,
                        source = excluded.source,
                        detected_keywords = excluded.detected_keywords,
                        title = excluded.title,
                        -- [2026-09-16] **최초 제목을 보존한다.** `title_prev IS NULL` 조건이
                        --   "처음 바뀔 때만" 을 만든다 - 두 번째 변경이 첫 원본을 덮으면
                        --   "원래 무엇이었나" 를 잃는다(그게 이 컬럼의 존재 이유다).
                        title_prev = CASE
                            WHEN community_posts.title IS NOT excluded.title
                                 AND community_posts.title_prev IS NULL
                            THEN community_posts.title
                            ELSE community_posts.title_prev END,
                        title_changed_at = CASE
                            WHEN community_posts.title IS NOT excluded.title
                            THEN strftime('%Y-%m-%d %H:%M:%S','now')
                            ELSE community_posts.title_changed_at END,
                        board_name = excluded.board_name,
                        date = excluded.date,
                        timestamp = excluded.timestamp,
                        is_super_keyword = excluded.is_super_keyword
                """, rows)
                # [2026-09-16] **바뀐 것을 말한다.** 이 자리에 로그가 없어서 "크롤러가 새 제목을
                #   보는데 DB 가 안 바뀐다" 를 정적 독해로는 더 못 좁혔다(파서·업서트를 따로
                #   태워 둘 다 정상인 것까지 확인했다). 변경은 드물어 소음이 되지 않는다.
                try:
                    _links = [r[7] for r in rows]
                    _q = ("SELECT link, title_prev, title FROM community_posts "
                          "WHERE title_changed_at >= ? AND link IN (%s)"
                          % ",".join("?" * len(_links)))
                    for _l, _old, _new in conn.execute(_q, [_stamp] + _links).fetchall():
                        logger.info("[SQLite] 제목 변경 감지 %r -> %r (%s)", _old, _new, _l)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"[SQLite] 커뮤니티 게시글 저장 실패: {e}")

    def save_ai_analysis(self, data_list):
        """AI 분석 게시글 목록을 저장합니다.

        [v6.7.484] **반환값 = 저장 성공 여부(bool).** 종전에는 예외를 삼키고 성공·실패 모두
        `None` 을 돌려줘 **호출부가 실패를 알 수단 자체가 없었다**. 그 결과 워커는 저장이
        실패해도 `저장 N건` INFO 를 찍고 그 위에서 배지 emit·**폰 웹푸시**까지 내보냈다
        (저장 안 된 요약의 알림이 나가고, 눌러도 그 글이 없다). `analysis.py` 는 이 한계를
        알고 저장 직후 다시 읽는 우회를 쓰고 있었다 — 그쪽은 행 존재까지 확인하므로 그대로 둔다.
        """
        if not data_list:
            return True     # 저장할 것이 없는 것은 실패가 아니다
        conn = self._get_connection()
        try:
            rows = []
            for p in data_list:
                kws = p.get('detected_keywords', [])
                if isinstance(kws, list):
                    kw_str = ", ".join(map(str, kws))
                else:
                    kw_str = str(kws)

                if isinstance(kw_str, str) and kw_str.startswith('[') and kw_str.endswith(']'):
                    kw_str = kw_str.strip('[]').replace("'", "").replace('"', "")

                rows.append((
                    p.get('game'), p.get('source'), kw_str,
                    p.get('title'), p.get('content'), p.get('summary'), p.get('board_name'),
                    p.get('date'), p.get('timestamp'), p.get('link'),
                    1 if p.get('is_super_keyword') else 0,
                    float(p.get('AI_Score')) if p.get('AI_Score') else 0.0,
                    p.get('sentiment'),
                    p.get('severity')
                ))

            with conn:
                # [v6.7.447] inserted_at = 요약 저장 시각(UTC). community_posts.inserted_at 와 짝을 이뤄
                #   수집→요약 실지연을 사후 측정 가능하게 한다(관측 공백 해소).
                #   INSERT OR REPLACE 라 reanalyze 로 덮어쓰면 시각도 갱신된다 - '마지막 저장 시각'
                #   의미이며, 재분석은 드물어(admin 수동) 지연 통계 오염은 무시 가능하다.
                conn.executemany("""
                    INSERT OR REPLACE INTO ai_analysis
                    (game, source, detected_keywords, title, content, summary, board_name, date, timestamp, link, is_super_keyword, AI_Score, sentiment, severity, inserted_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%S','now'))
                """, rows)
            return True
        except Exception as e:
            logger.error(f"[SQLite] AI 분석 데이터 저장 실패: {e}")
            return False

    def get_ai_analysis_for_digest(self, start_utc, end_utc, limit=400):
        """[v6.7.472] 종합 동향 리포트용 구간 조회. `(rows, total)` 반환.

        구간 기준 = **`inserted_at`(요약 저장시각, UTC `%Y-%m-%d %H:%M:%S`)**, 반개구간 `[start, end)`.
        게시시각(`timestamp`)이 아닌 이유: 게시 09:20 / 수집·요약 09:35 인 글이 게시시각 기준으로는
        **양쪽 리포트 어디에도 안 들어간다**(조회에 안 잡히므로 로그·화면 어디에도 흔적이 없다 -
        PATTERN-46 과 같은 형태의 유실). inserted_at 기준이면 모든 실 요약이 정확히 1회 포함된다.

        `total` 은 상한 적용 **전** 건수 - 호출부가 "N건 중 limit 건만 반영" 을 로그·리포트에
        드러내기 위한 값이다(silent truncation 금지).

        정렬은 critical > warning > info → 게임 → 시각. limit 이 걸릴 때 심각한 것부터 남는다.
        v6.7.447 이전 행은 `inserted_at` 이 NULL 이라 자연 제외된다(과거 소급 없음 - 의도된 동작).
        """
        conn = self._get_connection()
        try:
            total = conn.execute(
                f"SELECT COUNT(1) FROM ai_analysis "
                f"WHERE inserted_at >= ? AND inserted_at < ? AND {AI_NOTABLE_FILTER}",
                (start_utc, end_utc),
            ).fetchone()[0]
            cur = conn.cursor()
            cur.execute(f"""
                SELECT game, summary, sentiment, severity, link, board_name, source, timestamp
                  FROM ai_analysis
                 WHERE inserted_at >= ? AND inserted_at < ?
                   AND {AI_NOTABLE_FILTER}
              ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                       game, inserted_at
                 LIMIT ?
            """, (start_utc, end_utc, limit))
            cols = ['game', 'summary', 'sentiment', 'severity', 'link', 'board_name', 'source', 'timestamp']
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            return rows, total
        except Exception as e:
            logger.error(f"[SQLite] 동향 리포트 구간 조회 실패 ({start_utc}~{end_utc}): {e}")
            return [], 0

    def get_ai_analysis_by_link(self, link):
        """[v6.7.68] 단일 링크의 AI 분석 행 조회 (재분석/피드백 reanalyze 진입점)."""
        if not link:
            return None
        conn = self._get_connection()
        cur = conn.cursor()
        cur.row_factory = sqlite3.Row
        cur.execute("SELECT * FROM ai_analysis WHERE link = ? LIMIT 1", (link,))
        row = cur.fetchone()
        return dict(row) if row else None

    def update_ai_analysis_for_link(self, link, summary, sentiment, severity):
        """[v6.7.68] 단일 링크의 ai_analysis 요약/감정/심각도 갱신 (F-3 reanalyze 결과 반영).

        존재하지 않는 링크면 no-op (False 반환). 신규 INSERT 는 save_ai_analysis 경유.
        """
        if not link:
            return False
        conn = self._get_connection()
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE ai_analysis SET summary=?, sentiment=?, severity=? WHERE link=?",
                    (summary, sentiment, severity, link)
                )
                return cur.rowcount > 0
        except Exception as e:
            logger.error(f"[SQLite] update_ai_analysis_for_link 실패: {e}")
            return False

    def save_ai_feedback(self, record):
        """[v6.7.68] AI 오분류 피드백 1건 저장. record dict 키:
        link (필수), original_sentiment, expected_sentiment, original_severity, expected_severity,
        original_summary, expected_summary, note, admin_ip.
        ts 는 호출 시점 자동 할당. 반환: 신규 row id 또는 None.

        [v6.7.459] kind / gemma_verdict / result / label 추가 (전부 선택).
        - kind: 'misclassify'(F-3 오분류 신고, 기본) | 'summary_request'(피드 수동 요약 요청)
        - gemma_verdict: 1차가 뭐라고 했나 — '분석 제외' / '미분석' / '본문없음'
        - result: 요약이 실제로 나왔는지 — 'ok' / 'budget' / 'fail'
        - label: 사용자가 고른 사유 칩 (비워도 됨)
        """
        if not record or not record.get('link'):
            return None
        conn = self._get_connection()
        ts = datetime.now().isoformat()
        try:
            with conn:
                cur = conn.execute("""
                    INSERT INTO ai_feedback
                    (link, original_sentiment, expected_sentiment,
                     original_severity, expected_severity,
                     original_summary, expected_summary,
                     note, admin_ip, ts,
                     kind, gemma_verdict, result, label)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    record.get('link'),
                    record.get('original_sentiment'),
                    record.get('expected_sentiment'),
                    record.get('original_severity'),
                    record.get('expected_severity'),
                    record.get('original_summary'),
                    record.get('expected_summary'),
                    record.get('note'),
                    record.get('admin_ip'),
                    ts,
                    record.get('kind') or 'misclassify',
                    record.get('gemma_verdict'),
                    record.get('result'),
                    record.get('label'),
                ))
                return cur.lastrowid
        except Exception as e:
            logger.error(f"[SQLite] save_ai_feedback 실패: {e}")
            return None

    def update_ai_feedback_label(self, feedback_id, label):
        """[v6.7.459] 라벨 칩 후속 선택 — 요청 시점에 이미 적재된 행에 사유만 나중에 채운다.

        칩을 필수로 만들지 않는 것이 이 기능의 설계 전제다(F-3 가 0건이 된 원인 = 입력 마찰).
        요청 자체는 클릭 즉시 라벨이 되고, 사유는 나중에 붙이거나 영영 안 붙여도 된다.
        """
        if not feedback_id:
            return False
        conn = self._get_connection()
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE ai_feedback SET label=? WHERE id=?",
                    ((label or '').strip() or None, int(feedback_id)))
                return cur.rowcount > 0
        except Exception as e:
            logger.error(f"[SQLite] update_ai_feedback_label 실패: {e}")
            return False

    def get_post_by_link(self, link):
        """[v6.7.459] community_posts 단건 조회 (수동 AI 요약 요청 대상 확보용).

        `get_ai_analysis_by_link` 의 대칭 — 요약이 **아직 없는** 글이 대상이라 ai_analysis 쪽에서는
        찾을 수 없다(PATTERN-46 고아 + '분석 제외' 판정분). link 는 PK 성격이라 LIMIT 1.
        """
        if not link:
            return None
        conn = self._get_connection()
        cur = conn.cursor()
        cur.row_factory = sqlite3.Row
        try:
            cur.execute("SELECT * FROM community_posts WHERE link = ? LIMIT 1", (link,))
            row = cur.fetchone()
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"[SQLite] get_post_by_link 실패: {e}")
            return None

    def get_recent_feedbacks(self, limit=50, kind=None):
        """[v6.7.68] 최근 피드백 N건 조회 (관리자 모니터링용). 최신순.

        [v6.7.459] kind 필터 추가 + **호출처 부활**. 이 함수는 v6.7.68 도입 이래 호출처가 0이었다 —
        읽는 경로 없이 쓰기만 만든 결과가 라이브 0건이었으므로, 라벨을 새로 쌓기 시작하는
        이번 변경에서 `/api/ai-summary/labels` 로 노출한다(같은 실패를 반복하지 않기 위해).
        """
        conn = self._get_connection()
        cur = conn.cursor()
        cur.row_factory = sqlite3.Row
        try:
            if kind:
                cur.execute(
                    "SELECT * FROM ai_feedback WHERE kind = ? ORDER BY ts DESC LIMIT ?",
                    (str(kind), int(limit))
                )
            else:
                cur.execute(
                    "SELECT * FROM ai_feedback ORDER BY ts DESC LIMIT ?",
                    (int(limit),)
                )
            return [dict(row) for row in cur.fetchall()]
        except Exception as e:
            logger.error(f"[SQLite] get_recent_feedbacks 실패: {e}")
            return []

    # ================================================================
    # [v6.7.70] F-7 시스템 건강도 — system_health 테이블 헬퍼
    # ================================================================
    def save_health_sample(self, metric, value):
        """1분 주기 sample 1건 저장. metric=짧은 키, value=float (None 가능 → 측정 불가 의미).

        호출처: web_lib/bg/health_sampler.sample_health_metrics().
        """
        if not metric:
            return False
        conn = self._get_connection()
        ts = datetime.now().isoformat()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO system_health (ts, metric, value) VALUES (?, ?, ?)",
                    (ts, metric, None if value is None else float(value))
                )
            return True
        except Exception as e:
            logger.error(f"[SQLite] save_health_sample({metric}) 실패: {e}")
            return False

    def get_health_series(self, metric, hours=168):
        """단일 metric 의 최근 N시간 시계열 반환. 오래된 → 최신 순 (그래프 X축 순방향)."""
        if not metric:
            return []
        conn = self._get_connection()
        cur = conn.cursor()
        cur.row_factory = sqlite3.Row
        try:
            cutoff = (datetime.now() - timedelta(hours=int(hours))).isoformat()
            cur.execute(
                "SELECT ts, value FROM system_health "
                "WHERE metric=? AND ts >= ? ORDER BY ts ASC",
                (metric, cutoff)
            )
            return [{'ts': r['ts'], 'value': r['value']} for r in cur.fetchall()]
        except Exception as e:
            logger.error(f"[SQLite] get_health_series({metric}) 실패: {e}")
            return []

    def get_health_latest(self, metrics):
        """metric 리스트의 각 최신값(1건씩) 일괄 조회. {metric: {ts, value}} 반환.

        admin/health/overview 라우트가 6 메트릭 한 번에 호출하는 경로.
        """
        if not metrics:
            return {}
        out = {}
        conn = self._get_connection()
        cur = conn.cursor()
        cur.row_factory = sqlite3.Row
        try:
            for m in metrics:
                cur.execute(
                    "SELECT ts, value FROM system_health WHERE metric=? "
                    "ORDER BY ts DESC LIMIT 1",
                    (m,)
                )
                row = cur.fetchone()
                out[m] = {'ts': row['ts'], 'value': row['value']} if row else None
        except Exception as e:
            logger.error(f"[SQLite] get_health_latest 실패: {e}")
        return out

    def purge_old_health(self, keep_days=14):
        """N일 이전 sample 삭제. 1일 1회 호출 권장 (cron 또는 sampler 내 카운터)."""
        conn = self._get_connection()
        cutoff = (datetime.now() - timedelta(days=int(keep_days))).isoformat()
        try:
            with conn:
                cur = conn.execute(
                    "DELETE FROM system_health WHERE ts < ?", (cutoff,)
                )
                return cur.rowcount
        except Exception as e:
            logger.error(f"[SQLite] purge_old_health 실패: {e}")
            return 0

    def save_market_data(self, crawler_key, data_list):
        """계정 거래 및 시세 데이터를 시트 구조에 맞춰 1:1 저장합니다. (오딘 글로벌 및 유연한 매핑 지원)"""
        import json
        if not data_list:
            return
        conn = self._get_connection()
        now = datetime.now().isoformat()
        try:
            account_rows = []
            market_price_rows = []
            odin_rows = []
            fallback_rows = []

            for item in data_list:
                # 1. [계정 거래] 시트인 경우 전용 테이블에 1:1 매핑 저장
                is_account_trade = any(k in item for k in ['글 제목', '판매 여부', 'sale_status', 'title'])
                if is_account_trade:
                    game = item.get('게임') or item.get('game')
                    status = item.get('판매 여부') or item.get('sale_status')
                    title = item.get('글 제목') or item.get('title')
                    server = item.get('서버') or item.get('server')
                    item_type = item.get('종류') or item.get('type') or item.get('아이템 구분')
                    price = item.get('가격') or item.get('price')
                    date = item.get('날짜') or item.get('date')
                    time_val = item.get('시간') or item.get('time') or item.get('time_')
                    link = item.get('링크') or item.get('link') or item.get('content_url')

                    if link:
                        account_rows.append((game, status, title, server, item_type, price, date, time_val, link))
                    continue

                # 2. [자사/타사 시세] 시트인 경우 전용 테이블에 1:1 매핑 저장
                is_market_price = any(k in item for k in ['평균가(1000당)', '현금거래량_합계', '평균시세', '거래완료건수'])
                if is_market_price and 'ODIN_GLOBAL' not in str(crawler_key).upper():
                    category = 'JASA' if 'JASA' in str(crawler_key).upper() else 'TASA'
                    game_name = item.get('게임명') or item.get('game')
                    date_val = next((item[k] for k in item if '날짜' in k or 'date' in k), None)
                    server_val = next((item[k] for k in item if '서버' in k or 'server' in k or '검색어' in k), '전체')
                    avg_price_val = next((item[k] for k in item if '평균' in k or '시세' in k or '가격' in k or 'price' in k), 0)
                    trade_count = next((item[k] for k in item if '거래' in k or '수량' in k or '판매' in k or 'count' in k), 0)
                    cash_total = item.get('현금거래량_합계') or item.get('현금') or 0
                    goods_total = item.get('아이템거래량_합계') or item.get('아이템') or 0

                    if game_name and date_val:
                        market_price_rows.append((str(game_name), str(server_val), str(date_val), trade_count, cash_total, goods_total, avg_price_val, category))
                    continue

                # 3. [오딘 글로벌 전용] 시트 구조 반영 (날짜, 서버명, 판매자 수, 1K당 평균 가격)
                if 'ODIN_GLOBAL' in str(crawler_key).upper():
                    date_val = next((item[k] for k in item if '날짜' in k or 'date' in k), None)
                    server_val = next((item[k] for k in item if '서버' in k or 'server' in k), 'Unknown')
                    seller_count = next((item[k] for k in item if '판매' in k or 'count' in k), 0)
                    avg_price = next((item[k] for k in item if '가격' in k or '시세' in k or '1K' in k or 'price' in k), 0.0)

                    uid = f"{date_val}_{server_val}"
                    odin_rows.append((uid, date_val, server_val, seller_count, avg_price, now))
                    continue

                # 4. [기타] 일반 JSON 저장 (Fallback)
                uid = item.get('link') or item.get('id') or f"{crawler_key}_{item.get('게임명','')} {item.get('서버명','')} {now}"
                fallback_rows.append((uid, item.get('게임명') or ("오딘 글로벌" if 'ODIN_GLOBAL' in str(crawler_key).upper() else crawler_key), crawler_key, json.dumps(item, ensure_ascii=False), now))

            with conn:
                if account_rows:
                    conn.executemany("""
                        INSERT OR REPLACE INTO account_trades
                        (game, sale_status, title, server, type, price, date, time, link)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, account_rows)
                if market_price_rows:
                    conn.executemany("""
                        INSERT OR REPLACE INTO market_prices
                        (game_name, server, date, trade_count, cash_total, goods_total, avg_price, category)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, market_price_rows)
                if odin_rows:
                    conn.executemany("""
                        INSERT OR REPLACE INTO odin_global_prices
                        (id, date, server_name, seller_count, avg_price_1k, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, odin_rows)
                if fallback_rows:
                    conn.executemany("""
                        INSERT OR REPLACE INTO market_data (id, game, source, data_json, timestamp)
                        VALUES (?, ?, ?, ?, ?)
                    """, fallback_rows)
        except Exception as e:
            logger.error(f"[SQLite] 마켓 데이터 저장 실패 ({crawler_key}): {e}")

    def save_comments(self, comment_data):
        """댓글 데이터를 저장합니다. (한글/영문 키 유연한 매핑 추가)"""
        if not comment_data:
            return
        conn = self._get_connection()
        try:
            if isinstance(comment_data, dict):
                comment_data = [comment_data]

            rows = []
            for c in comment_data:
                # [수정] 한글/영문 혼합 매핑 시도
                comment_text = c.get('내용') or c.get('본문') or c.get('comment') or c.get('content')
                post_title = c.get('제목') or c.get('title')
                author = c.get('작성자') or c.get('author') or c.get('닉네임')
                time_val = c.get('작성시간') or c.get('시간') or c.get('time')
                timestamp = c.get('타임스탬프') or c.get('timestamp') or time_val
                link = c.get('원문링크') or c.get('link') or c.get('post_url')
                comment_link = c.get('댓글링크') or c.get('comment_link')
                # [v6.6.238] 게임 필드 — 크롤러가 수집 시점에 이미 알고 있는 값
                game = c.get('game') or c.get('게임')

                # PK 생성 (댓글 고유 링크가 있으면 그것을 사용, 없으면 조합)
                uid = comment_link or f"{link}_{timestamp}_{author}"

                if not comment_text and not author:  # 무의미한 빈 행은 건너뜀
                    continue

                rows.append((uid, post_title, comment_text, time_val, timestamp, link, comment_link, author, game))

            if rows:
                with conn:
                    conn.executemany("""
                        INSERT OR REPLACE INTO article_comments
                        (comment_id, title, comment, time, timestamp, link, comment_link, author, game)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, rows)
        except Exception as e:
            logger.error(f"[SQLite] 댓글 저장 실패: {e}")

    def backfill_comment_games(self, game_resolver):
        """
        [v6.6.238] game IS NULL 인 레거시 댓글을 comment_link 파싱으로 소급 채움.
        부팅 시 1회만 호출하면 되지만, WHERE 조건이 있어 재호출해도 이미 채워진 행은 스킵됨.

        Args:
            game_resolver: callable(comment_link) -> game or None
                           (crawler.extract_game_from_comment_link 주입)

        Returns:
            (updated_count, unresolved_count)
        """
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                "SELECT comment_id, comment_link FROM article_comments "
                "WHERE (game IS NULL OR game = '') AND comment_link IS NOT NULL AND comment_link != ''"
            )
            rows = cursor.fetchall()
            updates = []
            unresolved = 0
            for comment_id, comment_link in rows:
                try:
                    game = game_resolver(comment_link)
                except Exception:
                    game = None
                if game:
                    updates.append((game, comment_id))
                else:
                    unresolved += 1
            if updates:
                with conn:
                    conn.executemany(
                        "UPDATE article_comments SET game = ? WHERE comment_id = ?",
                        updates
                    )
            return (len(updates), unresolved)
        except Exception as e:
            logger.error(f"[SQLite] backfill_comment_games 실패: {e}")
            return (0, 0)

    def get_comments(self, limit=500):
        """저장된 댓글 데이터를 반환합니다. 앱 시작 시 comment_data_status 선로드용."""
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                "SELECT comment_id, title, comment, time, timestamp, link, comment_link, author, game "
                "FROM article_comments ORDER BY timestamp DESC LIMIT ?",
                (limit,)
            )
            columns = ['comment_id', 'title', 'comment', 'time', 'timestamp', 'link', 'comment_link', 'author', 'game']
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"[SQLite] 댓글 로드 실패: {e}")
            return []

    def get_posts_without_content(self, limit=20, since=None):
        """content가 없는 게시글 목록을 최신순으로 반환합니다. (내용 크롤러용)
        NULL = 아직 미수집, '' = 레거시(구버전에서 빈 문자열로 저장된 행) 모두 대상.
        [v6.7.206] '[FAILED:N]'(일과성 실패 N회 미만 재시도분)도 재추출 대상에 포함 — 한 번의 transient
          실패로 본문이 영구 누락되지 않도록. 종료 마커 '[FAILED]'(콜론 없음)·'[DELETED]' 등은 제외(재시도 종료).
          현재 content 를 함께 반환해 호출자가 재시도 횟수를 판별한다.
        since(ISO 문자열)가 주어지면 해당 시각 이후 삽입된 글(inserted_at 기준)만 대상으로 한다."""
        conn = self._get_connection()
        try:
            if since:
                cur = conn.execute(
                    "SELECT link, source, game, title, content FROM community_posts "
                    "WHERE (content IS NULL OR content = '' OR content LIKE '[FAILED:%') AND inserted_at >= ? "
                    "ORDER BY inserted_at DESC LIMIT ?", (since, limit))
            else:
                cur = conn.execute(
                    "SELECT link, source, game, title, content FROM community_posts "
                    "WHERE content IS NULL OR content = '' OR content LIKE '[FAILED:%' "
                    "ORDER BY timestamp DESC LIMIT ?", (limit,))
            columns = ['link', 'source', 'game', 'title', 'content']
            return [dict(zip(columns, row)) for row in cur.fetchall()]
        except Exception as e:
            logger.error(f"[SQLite] 미수집 게시글 조회 실패: {e}")
            return []

    def update_post_content(self, link, content):
        """게시글 본문(content)을 업데이트합니다. (내용 크롤러용)"""
        conn = self._get_connection()
        try:
            with conn:
                conn.execute("UPDATE community_posts SET content = ? WHERE link = ?", (content, link))
        except Exception as e:
            logger.error(f"[SQLite] 본문 업데이트 실패: {e}")

    def requeue_member_only_posts(self, hours=24, limit=200):
        """[v6.7.590] `[MEMBER_ONLY]` 로 종결된 NAVER 글을 다시 본문 추출 대상(NULL)으로 되돌린다.

        네이버 회원 쿠키를 새로 저장한 직후에만 호출한다 — 그 전까지는 재시도해도 401 이라
        되돌릴 이유가 없다. 반환 = 되돌린 행 수.

        **상한 2종이 의도적이다**: ① `hours`(기본 24) — 3개월치 1,900건을 한 번에 풀면 추출기가
        한 시간 넘게 그것만 붙들고, 그 글들이 그대로 AI 요약 큐로 들어가 일일 쿼타를 태운다
        (PATTERN-43). ② `limit` — 그래도 상한을 넘길 때를 위한 하드 캡. 잘라낸 분량은 호출자가
        로그·응답으로 알린다("조용한 절단 금지").

        주의: `get_posts_without_content` 가 `inserted_at >= since`(부팅시각-30분)로 한 번 더
        거르므로, 컨테이너 부팅보다 앞서 수집된 글은 NULL 로 돌아가도 다시 집히지 않는다.
        24시간 창은 그 제약과도 맞물린 값이다.
        """
        conn = self._get_connection()
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE community_posts SET content = NULL "
                    " WHERE link IN ("
                    "   SELECT link FROM community_posts "
                    "    WHERE content = '[MEMBER_ONLY]' AND source = 'NAVER' "
                    "      AND inserted_at >= datetime('now', ?) "
                    "    ORDER BY inserted_at DESC LIMIT ?)",
                    (f'-{int(hours)} hours', int(limit)))
                return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        except Exception as e:
            logger.error(f"[SQLite] MEMBER_ONLY 재큐 실패: {e}")
            return 0

    def count_posts_pending_ai_summary(self, since=None):
        """대기열의 **실제** 건수. `get_posts_pending_ai_summary` 와 같은 술어·같은 창을 쓴다.

        [2026-09-16] 워커 로그가 `요약 대기 {len(pending)}건` 을 찍는데 그 값은 조회 상한
        (`BATCH_SIZE * BATCH_PARALLEL` = 40)에 **잘린 값**이다. 그래서 백로그가 40이든
        95든 로그에는 똑같이 `40건` 으로 보이고, **잔량이 커지는 것을 로그로 못 본다.**
        라이브 실측 2026-09-16 18:30 - 로그 40건 / 실제 95건 / 밀림 74.5분.
        그 구간의 요약 지연(수집 후 28~60분)을 로그만으로 설명할 수 없던 이유가 이것이다.

        **상한에 걸렸을 때만 부른다** - 평시(잔량 p50=2)에는 `len(pending) < limit` 이라
        이 조회 자체가 안 나간다. 즉 비용은 이미 문제인 구간에서만 낸다.
        `idx_posts_inserted_at` 범위 조회라 스캔량은 창 크기에 비례한다.
        """
        conn = self._get_connection()
        try:
            where_since, params = "", []
            if since:
                where_since = " AND p.inserted_at >= ?"
                params.append(since)
            q = ("SELECT COUNT(1) FROM community_posts p "
                 "LEFT JOIN ai_analysis a ON a.link = p.link "
                 f"WHERE {_PENDING_AI_WHERE}{where_since}")
            return int(conn.execute(q, tuple(params)).fetchone()[0])
        except Exception as e:
            logger.debug(f"대기열 건수 조회 실패(무시): {e}")
            return -1

    def get_posts_pending_ai_summary(self, limit=30, since=None):
        """Gemma4 요약 대기 게시글 조회. (Flask 내부 AI 요약 워커용)

        조건:
        - content_extractor가 본문을 채운 글 (NOT NULL + 비어있지 않음)
        - 상태 표식([DELETED], [FAILED], [DELETED_POST], [IMAGE_ONLY])은 제외
        - ai_analysis 테이블에 아직 저장되지 않은 글만
        - since(inserted_at 문자열, UTC '%Y-%m-%d %H:%M:%S')가 주어지면 수집시각(inserted_at) >= since 인 글만
          ([v6.7.205] 게시시각 p.timestamp 기준에서 변경 — 부팅 직전 게시+부팅 후 수집 글 손실 방지.
           content_extractor.get_posts_without_content 와 동일 기준으로 정합)

        [v6.7.447] 정렬 기준 `p.timestamp DESC` → `p.inserted_at DESC`.
          이유는 성능이다: `WHERE inserted_at >= since` 인데 정렬을 다른 컬럼으로 하면 SQLite 가
          `idx_posts_timestamp` 를 역순으로 **수십만 행 끝까지 걷는다**(라이브 실측 1.3~2.1초, 결과 3행).
          LIMIT 30 을 못 채우면 조기 종료가 없어 **pending 이 적을수록 더 비싸다**. 같은 컬럼으로
          정렬하면 `idx_posts_inserted_at` 하나로 범위 탐색 + 정렬이 동시에 해결된다.
          의미상으로도 이쪽이 맞다 - 워커의 처리 순서는 '수집 순'이 자연스럽다(게시 시각은 과거 글이
          뒤늦게 수집되면 순서가 뒤집힌다). 게시 시각순 처리에 의존하는 소비자는 없다(반환 후
          배치 단위로만 쓰인다).

        [v6.7.650] **꼬리 몫을 뗀다 - `DESC LIMIT n` 만으로는 잔량이 n 을 넘는 순간 꼬리가 굶는다.**
          (여기서 **꼬리 = 대기 줄의 오래된 끝**이다. 이 레포의 다른 곳 - PATTERN-47 등 - 이 쓰는
           '꼬리' 는 지연 분포의 p90/p99 를 뜻하니 섞지 말 것.)
          매 사이클이 *최신 30건*을 집으므로, 잔량이 30 을 넘으면 하위는 **한 번도 배치에 안 들어간다.**
          잔량이 30 밑으로 떨어질 때(한산한 시간)까지 영구히 앉아 있고, 그동안 밀림은 1분당 1분씩
          자란다 - 화면은 최신 요약으로 멀쩡해 보여서 **증상이 지표에만 나타난다.**

          라이브 실측 2026-09-09 (벤더 500/503 로 처리량이 유입 밑으로 떨어진 뒤):
            10:43 부터 11:37 까지 밀림이 **정확히 1:1** 로 증가(27.4 -> 87.1분, 꼬리가 한 칸도 안 움직임)
            그 순간 잔량 59건 · 굶던 것은 10:10~10:25 수집분 · `ai_analysis` 최신 저장은 11:27
          즉 워커는 멀쩡히 돌고 있었고(사이클마다 10~20건 저장) **하위 29건만 못 만나고 있었다.**

          그래서 `limit` 의 1/5(최소 1)을 **가장 오래 큐에 앉아 있던 쪽**에 고정 배정한다.
          - 여기서 '오래된' 은 **수집 순**(`inserted_at`)이지 게시 순이 아니다 - 위 v6.7.447 과 같은
            기준이다. 과거 글이 뒤늦게 수집되면 그 글은 '최신' 으로 잡힌다(의도된 성질).
          - **정렬 컬럼이 같아 위 주석의 병리는 피한다** - ASC 도 `idx_posts_inserted_at` 정방향
            탐색이라 `EXPLAIN QUERY PLAN` 에 filesort(TEMP B-TREE)가 안 뜬다. **다만 공짜는
            아니다**(2026-09-09 커밋 전 리뷰 실측 정정): ASC 는 이미 요약된 앞부분을 건너뛰며
            꼬리까지 걸어가므로 창이 클수록 비싸다 - 20k행 창에서 콜당 **약 20ms 추가**(전체 약
            59ms). 창은 `since`(= 가동 시간)로 유계이고 재시작마다 리셋되며 사이클이 20초라
            수용 가능하다. 그래도 **평시엔 아예 안 걷는다** - 아래 조기 종료를 볼 것.
          - **최신 몫이 자기 한도를 못 채우면 꼬리 조회를 건너뛴다.** 못 채웠다는 것은 잔량이
            그 한도보다 적다는 뜻이고, 그러면 꼬리는 이미 그 결과 안에 있다. 라이브 잔량은
            **p50=2 · p90=12**(14일 20,108표본, 실측 2026-09-09)라 이 분기가 평시 경로다 -
            즉 위 20ms 는 백로그일 때만 낸다.
          - 잔량이 `limit` 이하면 두 조회가 겹치므로 `link` 로 중복을 걷어낸다 - 그때 결과는
            종전과 같다(대기 전량). 즉 **평시 동작은 안 바뀌고 백로그일 때만 갈린다.**
        """
        conn = self._get_connection()
        try:
            where_since = ""
            base_params = []
            if since:
                # [v6.7.205] 컷오프 기준 = 수집시각(inserted_at). 게시시각(p.timestamp) 기준이던 것을
                #   전환 — 부팅 직전 게시되어 부팅 후 수집된 글 손실 방지. dedup(a.link IS NULL)이 있어 >= 안전.
                where_since = " AND p.inserted_at >= ?"
                base_params.append(since)

            def _fetch(direction, lim):
                query = f"""
                    SELECT p.link, p.source, p.game, p.title, p.content,
                           p.detected_keywords, p.board_name, p.date, p.timestamp,
                           p.is_super_keyword
                      FROM community_posts p
                      LEFT JOIN ai_analysis a ON a.link = p.link
                     WHERE {_PENDING_AI_WHERE}
                       {where_since}
                  ORDER BY p.inserted_at {direction}
                     LIMIT ?
                """
                return conn.execute(query, tuple(base_params + [lim])).fetchall()

            # [v6.7.650] 최신 몫 + 꼬리 몫. 근거는 docstring - 꼬리를 안 떼면 잔량 > limit 인 동안
            #   하위가 영구히 배치에 안 들어간다. 1/5 인 이유: 사이클당 6건씩이면 라이브에서 관측된
            #   백로그(59건 중 하위 29)가 5사이클에 빠지고, 그동안 최신 처리량은 80% 로 유지된다.
            tail = max(1, limit // 5) if limit > 1 else 0
            head = limit - tail
            raw = list(_fetch("DESC", head))
            # 조기 종료: 최신 몫이 한도를 못 채웠으면 잔량 <= head 라 꼬리가 이미 이 안에 있다.
            #   평시(잔량 p50=2)에 ASC 창-워크를 통째로 안 낸다 - 근거는 docstring.
            if tail and len(raw) >= head:
                raw += list(_fetch("ASC", tail))

            cols = ['link', 'source', 'game', 'title', 'content',
                    'detected_keywords', 'board_name', 'date', 'timestamp', 'is_super_keyword']
            rows, seen = [], set()
            for row in raw:
                r = dict(zip(cols, row))
                # 잔량이 limit 이하면 두 조회가 겹친다 - `link` 는 PK 성격이라 그것으로 걷어낸다.
                #   (안 걷어내면 같은 글을 한 배치에서 두 번 요약해 콜과 쿼타를 그냥 버린다.)
                if r['link'] in seen:
                    continue
                seen.add(r['link'])
                r['summary'] = ''
                r['AI_Score'] = '0.00'
                r['is_super_keyword'] = bool(r.get('is_super_keyword'))
                rows.append(r)
            return rows
        except Exception as e:
            logger.error(f"[SQLite] AI 요약 대기 조회 실패: {e}")
            return []

    def save_daily_price_summary(self, date_str, game, category, avg_price, total_count, min_price=0, max_price=0):
        """일별 시세 요약을 저장합니다. (트렌드 차트용, 영구 보관)"""
        conn = self._get_connection()
        try:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO daily_price_summary (date, game, category, avg_price, total_count, min_price, max_price) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (date_str, game, category, avg_price, total_count, min_price, max_price)
                )
        except Exception as e:
            logger.error(f"[SQLite] 일별 시세 요약 저장 실패 ({game}): {e}")

    def get_daily_price_summary(self, game=None, category=None, days=30):
        """일별 시세 요약을 조회합니다. (트렌드 차트용)"""
        conn = self._get_connection()
        try:
            query = "SELECT date, game, category, avg_price, total_count, min_price, max_price FROM daily_price_summary WHERE 1=1"
            params = []
            if game:
                query += " AND game = ?"
                params.append(game)
            if category:
                query += " AND category = ?"
                params.append(category)
            if days:
                limit_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
                query += " AND date >= ?"
                params.append(limit_date)
            query += " ORDER BY date ASC, game ASC"
            cur = conn.execute(query, params)
            columns = ['date', 'game', 'category', 'avg_price', 'total_count', 'min_price', 'max_price']
            return [dict(zip(columns, row)) for row in cur.fetchall()]
        except Exception as e:
            logger.error(f"[SQLite] 일별 시세 요약 조회 실패: {e}")
            return []

    def save_sub_crawler_cache(self, crawler_key, date_str, results):
        """서브 크롤러 결과를 JSON 캐시로 저장합니다. (Google Sheets 읽기 대체용, 원본 한글 컬럼 보존)"""
        import json
        conn = self._get_connection()
        try:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO sub_crawler_cache (crawler_key, date, results_json, updated_at) VALUES (?, ?, ?, ?)",
                    (crawler_key, date_str, json.dumps(results, ensure_ascii=False, default=str), datetime.now().isoformat())
                )
        except Exception as e:
            logger.error(f"[SQLite] 서브크롤러 캐시 저장 실패 ({crawler_key}): {e}")

    def get_sub_crawler_cache(self, crawler_key, date_str):
        """서브 크롤러 JSON 캐시를 반환합니다. 없으면 빈 리스트 반환."""
        import json
        conn = self._get_connection()
        try:
            cur = conn.execute(
                "SELECT results_json FROM sub_crawler_cache WHERE crawler_key = ? AND date = ?",
                (crawler_key, date_str)
            )
            row = cur.fetchone()
            if row and row[0]:
                return json.loads(row[0])
            return []
        except Exception as e:
            logger.error(f"[SQLite] 서브크롤러 캐시 조회 실패 ({crawler_key}): {e}")
            return []

    def purge_old_data(self, days_to_keep=7, **kwargs):
        """특정 일수 이전의 데이터를 삭제하여 Snapshot을 관리합니다."""
        # [수정] 'days' 인자가 들어올 경우 대비
        if 'days' in kwargs:
            days_to_keep = kwargs['days']
            
        conn = self._get_connection()
        # [수정] 00:00:00 기준 보관 일수 계산 (기존 2일 -> 어제 데이터부터 남김)
        limit_dt = (datetime.now() - timedelta(days=days_to_keep-1)).replace(hour=0, minute=0, second=0, microsecond=0)
        limit_str = limit_dt.isoformat()
        yesterday_str = limit_dt.strftime("%Y-%m-%d")
        
        try:
            with conn:
                conn.execute("DELETE FROM community_posts WHERE timestamp < ?", (limit_str,))
                conn.execute("DELETE FROM ai_analysis WHERE timestamp < ?", (limit_str,))
                conn.execute("DELETE FROM article_comments WHERE timestamp < ?", (limit_str,))
                conn.execute("DELETE FROM account_trades WHERE date < ?", (yesterday_str,))
                conn.execute("DELETE FROM market_prices WHERE date < ?", (yesterday_str,))
                conn.execute("DELETE FROM market_data WHERE timestamp < ?", (limit_str,))
                conn.execute("DELETE FROM odin_global_prices WHERE date < ?", (yesterday_str,))
                conn.execute("DELETE FROM sub_crawler_cache WHERE date < ?", (yesterday_str,))
                logger.debug(f"[SQLite] Snapshot Purge 완료 (기준: {limit_str})")
            # [v6.7.455] is_new 핫셋 재hydrate 제거 (컬럼 은퇴로 핫셋 자체가 사라짐)
        except Exception as e:
             logger.debug(f"[SQLite] Purge 중 경미한 이슈: {e}")

    def delete_market_data_by_category_and_date(self, category, date):
        """특정 카테고리와 날짜의 마켓 데이터를 삭제합니다. (Snapshot 동기화용)"""
        conn = self._get_connection()
        try:
            with conn:
                # category는 JASA, TASA, ACCOUNT_TRADE 등
                if category == 'ACCOUNT_TRADE':
                    conn.execute("DELETE FROM account_trades WHERE date = ?", (date,))
                elif category in ['JASA_GAMES', 'TASA_GAMES', 'JASA', 'TASA']:
                    cat_val = 'JASA' if 'JASA' in category else 'TASA'
                    conn.execute("DELETE FROM market_prices WHERE category = ? AND date = ?", (cat_val, date))
                else:
                    # 기타 market_data 테이블 (Odin Global 등)
                    # market_data는 timestamp 기반이므로 date가 포함된 건만 삭제 시도
                    conn.execute("DELETE FROM market_data WHERE source = ? AND timestamp LIKE ?", (category, f"{date}%"))
        except Exception as e:
            logger.error(f"[SQLite] 마켓 데이터 삭제 실패 ({category}, {date}): {e}")

    def delete_community_posts_by_category_and_date(self, category, date):
        """특정 카테고리와 날짜의 커뮤니티 게시글을 삭제합니다. (Snapshot 동기화용)"""
        conn = self._get_connection()
        try:
            with conn:
                # community_posts와 ai_analysis 데이터 삭제
                # timestamp가 YYYY-MM-DD로 시작하는 건 삭제
                conn.execute("DELETE FROM community_posts WHERE source = ? AND timestamp LIKE ?", (category, f"{date}%"))
                conn.execute("DELETE FROM ai_analysis WHERE source = ? AND timestamp LIKE ?", (category, f"{date}%"))
        except Exception as e:
            logger.error(f"[SQLite] 커뮤니티 데이터 삭제 실패 ({category}, {date}): {e}")

    def delete_excluded_board_posts(self, board_exclude_kw):
        """[v6.7.174] ExcludeBoards 키워드 매칭 기존 행 1회 청소 (community_posts + ai_analysis).

        ExcludeBoards 는 신규 article 만 막는 push-gate 라 '키워드 추가 전 저장분' 은 잔존한다.
        v6.7.172(피드 SQLite 직독) 이후 그 잔존분이 단일게임 필터 피드에 노출되므로 retroactive 삭제.
        crawler.py 가 board_exclude_kw diff-swap 시 1회 호출. board_name substring 매칭(LIKE %kw%).
        board_exclude_kw: {game_name: [kw, ...]}. 반환: community_posts 삭제 행 수.
        """
        if not board_exclude_kw:
            return 0
        # [v6.7.701] **이미 적용한 키워드 목록이면 건너뛴다.** 크롤러는 부팅마다 첫 설정 polling 에서 `{} -> {kw}` 를 변경으로
        #   보고 이 함수를 부른다(호출부 주석 "부팅 1회 자동 청소"). 그런데 `board_name LIKE '%kw%'` 는 인덱스를 못 타서
        #   커뮤니티 글·AI 요약 테이블을 키워드마다 통째로 훑고, 그 전부가 **트랜잭션 하나**라 HDD 에서 몇 분간 쓰기 락을 쥔다.
        #   v6.7.700 계측이 잡았다(2026-09-30 05:20 부팅: 이 함수가 59초째 열림 -> 이후 7분+ 헬스 샘플·이상감지 쓰기가 30초씩
        #   기다리다 실패). 이미 지운 것을 매번 다시 훑는 것이라 대개 0건이다. 삭제 대상·방식은 그대로 - 목록이 **실제로 바뀔 때만** 돈다.
        import json as _json
        fp = _json.dumps({g: sorted({(k or '').strip() for k in (kws or []) if (k or '').strip()})
                          for g, kws in board_exclude_kw.items()}, ensure_ascii=False, sort_keys=True)
        if self.get_system_setting('exclude_kw_applied') == fp:
            logger.info("[SQLite] ExcludeBoards retroactive 삭제 생략 - 키워드 목록이 마지막 적용분과 같다 (부팅 재청소 방지)")
            return 0
        total = 0
        conn = self._get_connection()
        try:
            with conn:
                for game, kws in board_exclude_kw.items():
                    for kw in kws:
                        kw = (kw or '').strip()
                        if not kw:
                            continue
                        like = f"%{kw}%"
                        cur = conn.execute(
                            "DELETE FROM community_posts WHERE game = ? AND board_name LIKE ?",
                            (game, like)
                        )
                        total += cur.rowcount or 0
                        conn.execute(
                            "DELETE FROM ai_analysis WHERE game = ? AND board_name LIKE ?",
                            (game, like)
                        )
            if total > 0:
                logger.info(f"[SQLite] ExcludeBoards retroactive 삭제: {total}행 (게임 {len(board_exclude_kw)}종)")
            # 성공했을 때만 적용 목록을 남긴다 - 실패하면 다음 부팅이 다시 시도한다.
            self.set_system_setting('exclude_kw_applied', fp)
        except Exception as e:
            logger.error(f"[SQLite] ExcludeBoards retroactive 삭제 실패: {e}")
        return total

    # --- [조회 메서드 추가] ---
    
    def get_odin_global_prices(self, limit=100):
        """오딘 글로벌 전용 가격 데이터를 가져옵니다."""
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT date, server_name, seller_count, avg_price_1k, updated_at 
                FROM odin_global_prices 
                ORDER BY date DESC, server_name ASC 
                LIMIT ?
            """, (limit,))
            columns = [column[0] for column in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]
        except Exception as e:
            logger.error(f"[SQLite] 오딘 글로벌 조회 실패: {e}")
            return []

    def get_all_ccu_stats(self):
        """모든 CCU 현황을 가져옵니다."""
        conn = self._get_connection()
        cur = conn.cursor()
        cur.row_factory = sqlite3.Row
        cur.execute("SELECT * FROM ccu_stats ORDER BY game, server_name")
        return [dict(row) for row in cur.fetchall()]

    def get_latest_posts(self, limit=20, games=None, source=None, flagged=False):
        """최신 커뮤니티 게시글을 가져옵니다.
        [v6.7.172] games(list) 지정 시 해당 게임들만 (WHERE game IN ...) — 글로벌 메모리(최신 합본)가
        활발한 게임에 점령돼 busy 게임의 과거 글을 못 담는 문제 회피용 per-game 직접 조회.
        [v6.7.664] source(str) / flagged(bool) 추가.
          - source: 'OFFICIAL_WEB' | 'NAVER' | 'DAUM' | 'DC'. 클라 필터로는 못 한다 - DC 가 전체
            유입의 88%(13일 실측)라 최신 N건을 받아 클라에서 거르면 공식은 500건 중 7~8건만 잡힌다.
          - flagged: post_verdicts.kind 가 clean 이 아닌 글만. 제재 토글의 서버측 구현.
          [v6.7.738] 기본 전체 보기(메모리 캐시 경로)도 이제 판정을 싣는다 - routes/articles.py 가
          get_verdicts_for_links 로 같은 필드를 붙인다. 이 함수는 SQLite 직결 경로의 조인만 맡는다."""
        conn = self._get_connection()
        cur = conn.cursor()
        cur.row_factory = sqlite3.Row
        # [v6.7.669] **느렸던 원인은 `source` 인덱스 부재다** (사용자 보고: 토글 시 글이 늦게 뜬다).
        #   라이브 실측 - 공식+제재 첫 호출 **106.5초**(콜드) / 0.43초(웜). 같은 창의 공식 단독은
        #   0.019초, DC 는 0.006초였다. 차이는 **LIMIT 이 차느냐**다: 공식 단독은 500건이 약 4,000행
        #   안에서 차지만, 제재는 전 테이블에 190건뿐이라 LIMIT 이 영영 안 차서 `idx_posts_timestamp`
        #   를 **수십만 행 끝까지 훑는다**. NAS 는 단일 HDD 라 그 풀스캔이 랜덤 읽기로 번역된다.
        #   -> `idx_posts_source_ts(source, timestamp)` 로 **공식 6,000행만** 보게 했다:
        #      SCAN -> SEARCH, 0.4327s -> 0.0224s (19배).
        #
        #   **INNER JOIN 으로 바꾸는 것만으로는 안 됐다.** 조인 방향이 원인인 줄 알고 INNER 로
        #   바꿨을 때 **실행 계획이 그대로였다** - `JOIN` 은 SQLite 에 순서를 지시하지 않아서
        #   플래너가 정렬을 피하려고 계속 p 에서 출발했다. 순서를 실제로 바꾸는 것은 아래
        #   `CROSS JOIN` 이다(의미는 INNER 와 같다 - flagged 는 판정 행이 반드시 있다).
        #   [2026-09-16] **v6.7.669 의 인덱스는 `source` 가 같이 올 때만 닿는다.** 라이브 재측정:
        #     제재 단독(출처=전체 = 기본값이자 사용자가 실제로 누르는 조합)이 **16.8초(콜드) /
        #     0.617초(웜)** 로 그대로였다. `idx_posts_source_ts(source, timestamp)` 는 source 술어가
        #     없으면 못 쓰이고, 플래너는 `ORDER BY p.timestamp DESC LIMIT` 을 공짜로 만들려고
        #     `idx_posts_timestamp` 를 타는데 제재는 수십만 행에 200건뿐이라 **LIMIT 이 영영 안 찬다.**
        #     -> 고친 축이 아니라 **조인 방향**이다: `flagged` 면 작은 쪽(post_verdicts, 707행)에서
        #     출발해 link 로 p 를 찍고 200행만 정렬한다. `CROSS JOIN` 은 SQLite 에서 "이 순서를
        #     지켜라" 는 지시어다(조합 폭발과 무관 - 결과 집합은 INNER JOIN 과 동일).
        #     라이브 실측(3회, 웜): 제재 단독 0.612 -> **0.007초** / 제재+공식 0.0155 -> 0.0069초 /
        #     제재+게임 0.445 -> 0.008초. 콜드도 16.8 -> 0.165초. 재측정 명령은 CHANGELOG v6.7.672.
        #     전제: flagged 대상이 작다(판정 창 7일 x 공식 게시물). 그 전제가 깨지면
        #     TEMP B-TREE 정렬 비용이 커지므로 여기부터 다시 잰다.
        if flagged:
            sql = ("SELECT p.*, v.kind AS verdict_kind, v.target AS verdict_target, v.judged_at AS verdict_at "
                   "FROM post_verdicts v CROSS JOIN community_posts p ON p.link = v.link")
        else:
            sql = ("SELECT p.*, v.kind AS verdict_kind, v.target AS verdict_target, v.judged_at AS verdict_at "
                   "FROM community_posts p LEFT JOIN post_verdicts v ON v.link = p.link")
        where, params = [], []
        if games:
            where.append("p.game IN (%s)" % ','.join('?' * len(games)))
            params.extend(games)
        if source:
            where.append("p.source = ?")
            params.append(source)
        if flagged:
            # 인덱스 idx_post_verdicts_flagged 의 WHERE 와 글자 단위로 같게 유지할 것 (PATTERN-44).
            where.append("v.kind IS NOT NULL AND v.kind <> 'clean'")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY p.timestamp DESC LIMIT ?"
        params.append(int(limit))
        cur.execute(sql, tuple(params))
        return [dict(row) for row in cur.fetchall()]

    def get_verdicts_for_links(self, links):
        """[v6.7.738] 주어진 link 집합의 제재 판정만 조회해 {link: {verdict_kind, verdict_target,
        verdict_at}} 로 돌려준다. 기본 피드 보기(메모리 캐시 경로, articles.py)에 SQLite 직결
        경로(get_latest_posts)와 **같은 필드·같은 의미**의 판정을 붙이기 위한 조회다 - v6.7.664 가
        문서화한 비대칭(판정이 SQLite 경로에만 실림)을 해소한다.

        link 이 post_verdicts 의 PRIMARY KEY 라 `IN (...)` 조회가 자동 인덱스를 탄다
        (`SEARCH ... USING INDEX sqlite_autoindex_post_verdicts_1` - 작은 표에서도 SCAN 이
        아니다, PATTERN-70). 판정 대상이 공식 게시물뿐이므로(post_judge) 호출부가 공식 link 만
        넘기면 IN 변수 수가 작다. 그래도 SQLITE_MAX_VARIABLE_NUMBER 를 넘지 않게 900 단위로 쪼갠다."""
        links = [l for l in (links or []) if l]
        if not links:
            return {}
        conn = self._get_connection()
        cur = conn.cursor()
        cur.row_factory = sqlite3.Row
        out = {}
        CHUNK = 900
        for i in range(0, len(links), CHUNK):
            chunk = links[i:i + CHUNK]
            ph = ','.join('?' * len(chunk))
            cur.execute(
                "SELECT link, kind, target, judged_at FROM post_verdicts WHERE link IN (%s)" % ph,
                tuple(chunk))
            for row in cur.fetchall():
                out[row['link']] = {
                    'verdict_kind': row['kind'],
                    'verdict_target': row['target'],
                    'verdict_at': row['judged_at'],
                }
        return out

    def get_latest_ai_analysis(self, limit=500, per_game_limit=50, since_today=False, since_days=None):
        """최신 AI 분석 게시글을 가져옵니다.
        [v6.6.408] 분석 제외 글 자동 필터 + 게임별 균등 LIMIT.
        - 이전: limit=500 만 적용 → 우마무스메 분석 제외 글이 최신순으로 LIMIT 차지 → 다른 게임 밀려남
        - 현재: 분석 제외 자동 제거 + 게임별 ROW_NUMBER 로 상한 (per_game_limit) 적용 후 전체 LIMIT
        [v6.6.409] since_today=True 일 때 오늘 자정~지금 글만 (상황판 ↔ AI 탭 데이터 일관성).
        [v6.7.58] IMT_DEBUG=1 시 perf 측정 로그.
        [v6.7.337] since_days=N 일 때 최근 N일 글만. ai_analysis 는 purge 잡이 없어 무한 누적 —
          바운드 없는 윈도우 쿼리가 전 행 스캔(LIKE 필터 2회/행 + TEMP B-TREE)이라 테이블 성장에
          비례해 느려진다. 호출처가 "게임별 최신 N건" 만 쓰면 시간 바운드를 걸어 스캔을 상수화할 것.
          since_today 가 True 면 since_days 는 무시.
        """
        # [v6.7.58] PERF baseline 측정 (IMT_DEBUG=1 시만)
        _perf_dbg = os.environ.get('IMT_DEBUG') == '1'
        _t0 = time.perf_counter() if _perf_dbg else 0.0
        conn = self._get_connection()
        cur = conn.cursor()
        cur.row_factory = sqlite3.Row
        # [v6.6.409] since_today 옵션 — AI 분석 탭과 상황판 합성 입력의 시간 윈도우 통일
        # [v6.7.420] 시간 필터를 컬럼-함수(datetime(timestamp)) → 문자열 비교(timestamp >= ?)로 전환.
        #   datetime() 래핑이 기존 idx_ai_timestamp 를 무효화해 매 콜드 호출이 전 행(실측 37.5만) 스캔 +
        #   행마다 datetime()·LIKE 2회 → NAS 2코어에서 수 초(= AI 요약 탭 "가장 늦게 로드"의 서버측 주범).
        #   timestamp 는 KST naive ISO('2026-07-07T00:00:45' 실측) — 컷오프를 'YYYY-MM-DD'(일 단위)로 주면
        #   'T'/' ' 어느 저장 포맷과도 사전순 비교 안전(경계일 전체 포함 — 최근 조망 목적상 무해).
        if since_today:
            cutoff = datetime.now().strftime('%Y-%m-%d')
        elif since_days:
            cutoff = (datetime.now() - timedelta(days=int(since_days))).strftime('%Y-%m-%d')
        else:
            cutoff = None
        time_clause = "AND timestamp >= ?" if cutoff else ""
        # summary 가 있는 유효한 데이터만 + 게임별 균등 분배
        # [v6.7.420] 윈도우 CTE 를 스키니(rowid+정렬키)로 — 구 SELECT * 가 content 전문(행당 수 KB)을
        #   윈도우 temp b-tree 에 통째로 적재하던 것 제거. 최종 rowid 조인으로 전체 컬럼 회수(반환 시맨틱 동일).
        params = ([cutoff] if cutoff else []) + [int(per_game_limit), int(limit)]
        # [v6.7.437] WHERE 술어를 AI_NOTABLE_FILTER 상수로 통일 - 같은 문자열이 부분 인덱스
        #   idx_ai_notable 의 WHERE 이기도 하다. SQLite 는 "인덱스 WHERE 가 쿼리 WHERE 에 포함될 때만"
        #   부분 인덱스를 쓰므로, 여기 술어를 바꾸면 인덱스가 조용히 안 쓰이고 성능만 원복된다(에러 없음).
        #   반드시 상수를 고칠 것. 실측: 이 술어로 3일 윈도우가 라이브 138ms -> 로컬벤치 713ms→44ms(16x).
        cur.execute(f"""
            WITH ranked AS (
                SELECT rowid AS rid, timestamp AS ts,
                       ROW_NUMBER() OVER (PARTITION BY game ORDER BY timestamp DESC) AS rn
                FROM ai_analysis
                WHERE {AI_NOTABLE_FILTER}
                  {time_clause}
            )
            SELECT a.* FROM ai_analysis a JOIN ranked r ON a.rowid = r.rid
            WHERE r.rn <= ?
            ORDER BY r.ts DESC
            LIMIT ?
        """, params)
        rows = [dict(row) for row in cur.fetchall()]
        if _perf_dbg:
            logger.debug(f"[PERF] get_latest_ai_analysis N={len(rows)} dt={int((time.perf_counter()-_t0)*1000)}ms since_today={since_today}")
        return rows

    # ═══════════════════ Phase B — 상황판 스냅샷 ═══════════════════

    def get_hour_summaries_for_game(self, game, window_minutes=60, limit=40, since_today=False):
        """Phase B 합성 입력용. 분석 제외·sentiment null 자동 필터.
        severity 높은 순, 최신순으로 정렬.
        [v6.6.402] since_today=True 일 때 오늘 자정~지금 윈도우 사용 (window_minutes 무시)."""
        conn = self._get_connection()
        cur = conn.cursor()
        cur.row_factory = sqlite3.Row
        if since_today:
            time_clause = "AND datetime(timestamp) >= date('now', 'localtime')"
        else:
            time_clause = f"AND datetime(timestamp) >= datetime('now', '-{int(window_minutes)} minutes', 'localtime')"
        cur.execute(f"""
            SELECT game, title, summary, sentiment, severity, timestamp, link, board_name
            FROM ai_analysis
            WHERE game = ?
              {time_clause}
              AND summary IS NOT NULL
              AND TRIM(summary) != ''
              -- [v6.7.267] '[분석 제외]'(대괄호) 마커는 접두 LIKE '분석 제외%' 가 '[' 시작이라 못 걸러냄 → 부분문자열로 전환 (get_latest_ai_analysis v6.7.259 와 정합).
              AND summary NOT LIKE '%분석 제외%'
              AND summary NOT LIKE '%분석제외%'
              -- [v6.6.406] sentiment IS NOT NULL 복원 — NULL 은 "분석 안 됨" 의미.
              -- v6.6.405 에서 잠시 허용했으나 NULL → 'neutral' 기본값 처리 시 거짓 신호 발생 (실제 분석 결과 없는데 NORMAL 표시).
              -- 의미상 정직: 분석 데이터 없으면 상황판에서 빠지는 것이 맞음. Phase A-3 (v6.6.389) 이전 레거시 글은 시간 지나면 윈도우 밖으로 자연 해소.
              AND sentiment IS NOT NULL
            ORDER BY
              CASE severity WHEN 'critical' THEN 1 WHEN 'warning' THEN 2 ELSE 3 END,
              timestamp DESC
            LIMIT ?
        """, (game, int(limit)))
        return [dict(r) for r in cur.fetchall()]

    def get_market_data(self, limit=50):
        """최신 시세/마켓 데이터를 가져옵니다."""
        import json
        conn = self._get_connection()
        cur = conn.cursor()
        cur.row_factory = sqlite3.Row
        cur.execute("SELECT * FROM market_data ORDER BY timestamp DESC LIMIT ?", (limit,))
        results = []
        for row in cur.fetchall():
            d = dict(row)
            try:
                d['data_json'] = json.loads(d['data_json'])
            except (json.JSONDecodeError, ValueError, TypeError):
                pass
            results.append(d)
        return results

    def get_latest_comments(self, limit=20):
        """최신 댓글을 가져옵니다."""
        conn = self._get_connection()
        cur = conn.cursor()
        cur.row_factory = sqlite3.Row
        cur.execute("SELECT * FROM article_comments ORDER BY timestamp DESC LIMIT ?", (limit,))
        return [dict(row) for row in cur.fetchall()]

    def get_posts_by_date(self, start_datetime_str):
        """특정 시점 이후의 게시글을 모두 가져옵니다. (YYYY-MM-DD HH:MM:SS)"""
        conn = self._get_connection()
        cur = conn.cursor()
        cur.row_factory = sqlite3.Row
        cur.execute("SELECT * FROM community_posts WHERE timestamp >= ? ORDER BY timestamp DESC", (start_datetime_str,))
        return [dict(row) for row in cur.fetchall()]

    def get_comments_by_date(self, start_datetime_str):
        """특정 시점 이후의 댓글을 모두 가져옵니다. (YYYY-MM-DD HH:MM:SS)"""
        conn = self._get_connection()
        cur = conn.cursor()
        cur.row_factory = sqlite3.Row
        cur.execute("SELECT * FROM article_comments WHERE timestamp >= ? ORDER BY timestamp DESC", (start_datetime_str,))
        return [dict(row) for row in cur.fetchall()]

    def get_recent_market_prices(self, category=None, limit=1000):
        """최신 자사/타사 시세 데이터를 가져옵니다."""
        conn = self._get_connection()
        cur = conn.cursor()
        cur.row_factory = sqlite3.Row
        if category:
            cur.execute("SELECT * FROM market_prices WHERE category = ? ORDER BY date DESC LIMIT ?", (category, limit))
        else:
            cur.execute("SELECT * FROM market_prices ORDER BY date DESC LIMIT ?", (limit,))
        return [dict(row) for row in cur.fetchall()]

    def get_recent_account_trades(self, game=None, limit=1000):
        """최신 계정 거래 데이터를 가져옵니다. (게임 필터링 지원)
        [v6.7.58] IMT_DEBUG=1 시 perf 측정 로그."""
        # [수정] 아키에이지 워 -> 아키워 매핑 보정 (DB에 아키워로 저장된 경우 대비)
        if game == '아키에이지 워': game = '아키워'

        # [v6.7.58] PERF baseline 측정 (IMT_DEBUG=1 시만)
        _perf_dbg = os.environ.get('IMT_DEBUG') == '1'
        _t0 = time.perf_counter() if _perf_dbg else 0.0
        conn = self._get_connection()
        cur = conn.cursor()
        cur.row_factory = sqlite3.Row
        if game and game not in ['all', '전체']:
            cur.execute("SELECT * FROM account_trades WHERE game = ? ORDER BY date DESC, time DESC LIMIT ?", (game, limit))
        else:
            cur.execute("SELECT * FROM account_trades ORDER BY date DESC, time DESC LIMIT ?", (limit,))
        rows = [dict(row) for row in cur.fetchall()]
        if _perf_dbg:
            logger.debug(f"[PERF] get_recent_account_trades N={len(rows)} dt={int((time.perf_counter()-_t0)*1000)}ms game={game}")
        return rows

    # --- [v6.6.308] 관리자 기기 접속 로그 (visitor_log) ---
    #   스코프: (ip, device) 복합 PK. 같은 기기 재방문 시 last_seen 갱신 + visit_count +1.
    #   삭제 API 없음 (무결 유지). name 은 관리자가 수동 지정한 값이 있으면 보존.

    # [v6.7.487] 단건 `upsert_visitor_log` 는 삭제 - 호출처가 0 이 됐다(요청 경로 write 를
    #   배치로 옮기면서). 남겨두면 "요청 경로에서 쓰면 안 되는 것" 을 다시 부르기 쉬운
    #   유혹으로 남는다(PATTERN-48: 제거 리팩토링은 죽은 코드를 남긴다). 필요하면
    #   upsert_visitor_logs_batch([row]) 로 1건만 넘기면 동일하다.
    # [v6.7.487] 위 upsert 를 N건 한 트랜잭션으로. 호출부는 요청 경로가 아니라 배치 잡이다
    #   (`web_lib/bg/visitor_log.py`). **요청 경로에서 이 write 를 하면 안 되는 이유**:
    #   락 경합 시 `busy_timeout`(30s)까지 요청 스레드가 묶이고, gunicorn `--threads 8` 이
    #   전부 물리면 새 요청이 큐에 쌓여 리버스 프록시가 504 를 낸다 — 2026-08-05 오전
    #   외부 무응답 2구간의 직접 메커니즘이 정확히 이것이었다(PATTERN-52 대응 4).
    #   배치화는 트랜잭션 수 자체도 줄여 **경합의 원인 쪽**도 함께 낮춘다.
    _VISITOR_UPSERT_SQL = """
                    INSERT INTO visitor_log (visitor_id, ip, device, name, ua, first_seen, last_seen, visit_count, referer, accept_lang, xff)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                    ON CONFLICT(visitor_id, ip) DO UPDATE SET
                        last_seen = excluded.last_seen,
                        ua = excluded.ua,
                        device = excluded.device,
                        visit_count = visit_count + 1,
                        name = CASE
                            WHEN visitor_log.name IS NULL OR visitor_log.name = ''
                                THEN excluded.name
                            ELSE visitor_log.name
                        END,
                        referer = CASE WHEN excluded.referer IS NULL OR excluded.referer = ''
                            THEN visitor_log.referer ELSE excluded.referer END,
                        accept_lang = CASE WHEN excluded.accept_lang IS NULL OR excluded.accept_lang = ''
                            THEN visitor_log.accept_lang ELSE excluded.accept_lang END,
                        xff = CASE WHEN excluded.xff IS NULL OR excluded.xff = ''
                            THEN visitor_log.xff ELSE excluded.xff END
    """

    def upsert_visitor_logs_batch(self, rows):
        """[v6.7.487] visitor_log UPSERT 배치. rows = [(visitor_id, ip, device, name, ua, now_str, referer, accept_lang, xff), ...]

        락 경합 시에만 재시도한다(PATTERN-51: 재시도는 호출부 로깅이 아니라 **write 지점**에
        있어야 한다 — 여기서 실패한 행은 큐에서 이미 빠져 다시 조회되지 않으므로 그대로 유실이다).
        sleep 은 트랜잭션 밖에서 — 그 사이 다른 write 가 지나갈 수 있게.
        반환: 저장한 행 수(실패 시 0).
        """
        if not rows:
            return 0
        payload = [(r[0], r[1], r[2] or '', r[3] or '', r[4] or '', r[5], r[5],
                    r[6] or '', r[7] or '', r[8] or '') for r in rows if r[0] and r[1]]
        if not payload:
            return 0
        conn = self._get_connection()
        for attempt in range(3):
            try:
                with conn:
                    conn.executemany(self._VISITOR_UPSERT_SQL, payload)
                return len(payload)
            except Exception as e:
                msg = str(e).lower()
                if ('locked' in msg or 'busy' in msg) and attempt < 2:
                    time.sleep(0.3 * (attempt + 1))
                    continue
                logger.error(f"[SQLite] visitor_log 배치 저장 실패 ({len(payload)}건): {e}")
                return 0
        return 0

    def touch_visitor_log(self, visitor_id, ip, now_str):
        """[v6.7.290] 현재 접속 중(admin_heartbeat) 세션의 last_seen 만 갱신.
        첫 접속(페이지 렌더)은 visitor_log 배치 flush 가 이미 기록(v6.7.487) — 여기선 기존 (visitor_id, ip)
        행의 last_seen 만 UPDATE 한다. visit_count 는 증가시키지 않음(=페이지 로드 횟수 의미 보존).
        존재하지 않는 행이면 no-op(다음 페이지 렌더가 생성). 반환=갱신된 행 수.
        now_str 은 upsert 규약과 동일하게 KST 문자열이어야 first_seen/last_seen 이 정합.
        """
        if not visitor_id or not ip:
            return 0
        conn = self._get_connection()
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE visitor_log SET last_seen = ? WHERE visitor_id = ? AND ip = ?",
                    (now_str, visitor_id, ip)
                )
                return cur.rowcount
        except Exception as e:
            logger.error(f"[SQLite] visitor_log touch 실패 ({visitor_id}/{ip}): {e}")
            return 0

    def get_visitor_logs(self, limit=1000):
        """방문 로그를 last_seen DESC 로 반환. 삭제 불가, 누적 조회용."""
        conn = self._get_connection()
        cur = conn.cursor()
        cur.row_factory = sqlite3.Row
        try:
            cur.execute(
                "SELECT visitor_id, ip, device, name, ua, first_seen, last_seen, visit_count, "
                "referer, accept_lang, xff "
                "FROM visitor_log ORDER BY last_seen DESC LIMIT ?",
                (int(limit or 1000),)
            )
            return [dict(row) for row in cur.fetchall()]
        except Exception as e:
            logger.error(f"[SQLite] visitor_log 조회 실패: {e}")
            return []

    def update_visitor_name_by_vid(self, visitor_id, new_name):
        """관리자가 특정 브라우저(visitor_id)의 기기/작업자 이름을 수동 지정.
        해당 토큰의 모든 IP 행(접속 위치별)에 동일 적용. [v6.7.287] 구 update_visitor_name_by_ip 대체."""
        if not visitor_id:
            return 0
        conn = self._get_connection()
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE visitor_log SET name = ? WHERE visitor_id = ?",
                    (new_name or '', visitor_id)
                )
                return cur.rowcount
        except Exception as e:
            logger.error(f"[SQLite] visitor_log 이름 갱신 실패 ({visitor_id}): {e}")
            return 0

    # --- [v6.6.343] 카카오워크 알림 토글 관리 ---

    def get_notification_toggles(self):
        """모든 알림 토글을 딕셔너리로 반환. 테이블에 없는 키는 호출자가 True(기본 ON) 로 해석."""
        conn = self._get_connection()
        try:
            rows = conn.execute("SELECT key, enabled FROM notification_toggles").fetchall()
            return {row[0]: bool(row[1]) for row in rows}
        except Exception as e:
            logger.error(f"[SQLite] notification_toggles 조회 실패: {e}")
            return {}

    def set_notification_toggle(self, key, enabled):
        """단일 토글을 UPSERT. `key` 는 카테고리 식별자, `enabled` 는 bool."""
        if not key:
            return False
        conn = self._get_connection()
        now = datetime.now().isoformat()
        try:
            with conn:
                conn.execute("""
                    INSERT INTO notification_toggles (key, enabled, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at
                """, (key, 1 if enabled else 0, now))
            return True
        except Exception as e:
            logger.error(f"[SQLite] notification_toggle 저장 실패 ({key}={enabled}): {e}")
            return False

    def set_notification_toggles_bulk(self, toggles: dict):
        """여러 토글을 한 번의 트랜잭션으로 UPSERT. toggles: {key: bool}."""
        if not toggles:
            return 0
        conn = self._get_connection()
        now = datetime.now().isoformat()
        updated = 0
        try:
            with conn:
                for key, enabled in toggles.items():
                    if not key:
                        continue
                    conn.execute("""
                        INSERT INTO notification_toggles (key, enabled, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(key) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at
                    """, (key, 1 if enabled else 0, now))
                    updated += 1
            return updated
        except Exception as e:
            logger.error(f"[SQLite] notification_toggles 일괄 저장 실패: {e}")
            return 0

    # --- [v6.6.353] 시스템 설정 (KV) ---

    def get_system_setting(self, key, default=None):
        """system_settings 에서 key 조회. 없거나 오류 시 default 반환."""
        if not key:
            return default
        conn = self._get_connection()
        try:
            row = conn.execute("SELECT value FROM system_settings WHERE key = ?", (key,)).fetchone()
            if row is None:
                return default
            return row[0]
        except Exception as e:
            logger.error(f"[SQLite] system_setting 조회 실패 ({key}): {e}")
            return default

    def set_system_setting(self, key, value):
        """system_settings UPSERT. value 는 TEXT 로 저장 (None 이면 빈 문자열). 성공 시 True.

        [v6.7.477] **락 경합 재시도 추가.** 이 함수는 재시도가 없어 부팅 storm 구간에서 조용히
        실패하고 있었다 - 2026-08-03 라이브 실측으로 `database is locked` **3회 연속** 확인
        (11:26:04 / 11:27:34 / 11:29:14). 호출부 대부분이 반환값을 안 보므로 **"저장했다는 로그와
        값이 없는 현실"** 이 공존했다(v6.7.474 워터마크가 그렇게 무효화됐다).
        `crawled_data.sqlite` 는 크롤러·시트 hydrate·health sampler 가 동시에 두드리는 DB 라
        경합이 상시 조건이며, 이 UPSERT 는 1행이라 **몇 초 기다리는 편이 실패보다 항상 싸다**.
        패턴은 디코 판정 `_store`(v6.7.465, 잡은 v6.7.742 삭제)와 같다 - **락일 때만** 재시도하고 다른 오류
        (스키마 오류 등)는 재시도해도 같은 결과이므로 즉시 실패로 돌린다.
        """
        if not key:
            return False
        conn = self._get_connection()
        now = datetime.now().isoformat()
        last = None
        for attempt in range(3):
            try:
                with conn:
                    conn.execute("""
                        INSERT INTO system_settings (key, value, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                    """, (key, '' if value is None else str(value), now))
                if attempt:
                    logger.info(f"[SQLite] system_setting 저장 성공 ({key}, 재시도 {attempt}회)")
                return True
            except Exception as e:
                last = e
                if 'locked' not in str(e).lower() and 'busy' not in str(e).lower():
                    break
                logger.warning(f"[SQLite] system_setting 락 경합 재시도 {attempt + 1}/2 ({key}): {e}")
                time.sleep(1.5 * (attempt + 1))
        logger.error(f"[SQLite] system_setting 저장 실패 ({key}={value}): {last}")
        return False

    # --- [웹 뷰어 연동용 신규 메서드] ---

    def add_command(self, command, params=None):
        """웹 뷰어에서 터널 앱으로 내리는 명령을 추가합니다."""
        conn = self._get_connection()
        import json
        now = datetime.now().isoformat()
        with conn:
            conn.execute("""
                INSERT INTO app_commands (command, params, status, created_at, updated_at)
                VALUES (?, ?, 'pending', ?, ?)
            """, (command, json.dumps(params or {}), now, now))

    def get_pending_commands(self):
        """아직 처리되지 않은 명령 목록을 가져옵니다."""
        conn = self._get_connection()
        import json
        cur = conn.cursor()
        cur.row_factory = sqlite3.Row
        cur.execute("SELECT * FROM app_commands WHERE status = 'pending' ORDER BY created_at ASC")
        results = []
        for row in cur.fetchall():
            d = dict(row)
            d['params'] = json.loads(d['params'])
            results.append(d)
        return results

    def update_command_status(self, cmd_id, status, result=None):
        """명령 처리 상태 및 결과를 업데이트합니다."""
        conn = self._get_connection()
        now = datetime.now().isoformat()
        with conn:
            conn.execute("""
                UPDATE app_commands
                SET status = ?, result = ?, updated_at = ?
                WHERE id = ?
            """, (status, str(result) if result else None, now, cmd_id))

    def update_system_config(self, key, value):
        """시스템 설정(Config 캐시)을 업데이트합니다."""
        conn = self._get_connection()
        import json
        now = datetime.now().isoformat()
        with conn:
            val_str = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
            conn.execute("""
                INSERT OR REPLACE INTO system_config (config_key, config_value, updated_at)
                VALUES (?, ?, ?)
            """, (key, val_str, now))

    def get_system_config(self, key):
        """시스템 설정값을 가져옵니다."""
        conn = self._get_connection()
        import json
        cur = conn.cursor()
        cur.row_factory = sqlite3.Row
        cur.execute("SELECT config_value FROM system_config WHERE config_key = ?", (key,))
        row = cur.fetchone()
        if row:
            val = row['config_value']
            try:
                return json.loads(val)
            except (json.JSONDecodeError, ValueError, TypeError):
                return val
        return None

# 전역 인스턴스
db_handler = SQLiteHandler()
