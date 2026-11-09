# -*- coding: utf-8 -*-
"""
전역 상태 및 공통 인스턴스 관리 모듈 (web_lib/globals.py)

모든 전역 상태는 CrawlerState 클래스 인스턴스(_state)에 보관됩니다.
기존 코드(bg_tasks.py, web_routes.py 등)에서 `gl.xxx` 형태로 접근하면
모듈 레벨 __getattr__/__setattr__ 프록시를 통해 _state 를 투명하게 읽고 씁니다.
따라서 기존 참조처(153개)를 수정하지 않아도 됩니다.
"""
import logging
import os
import secrets
import sys
import types
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from flask_socketio import SocketIO

# [v6.7.746] 소켓 허용 출처의 공개 주소는 비공개 자리 utils/private_config.py 에 있다. 앱(app.py)은 utils 를 sys.path 에
#   넣지만 이 모듈은 단독으로도 import 된다(계약 시험) - 같은 폴더를 직접 넣는다. 못 찾으면 ImportError 로 바로 드러난다.
_UTILS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'utils')
if _UTILS_DIR not in sys.path:
    sys.path.append(_UTILS_DIR)
import private_config as _private_config  # noqa: E402

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────
# 헬퍼: 크롤러 상태 딕셔너리 초기값 생성
# (CrawlerState.__init__ 보다 먼저 정의되어야 함)
# ─────────────────────────────────────────────────
def create_crawler_status(game_list=None):
    status = {
        'running': False,
        'last_run_start': None,
        'last_run_finish': None,
        'status_message': '대기',
        'error': None,
        'latest_results': [],
        'crawled_date': None,
        'gsheet_checked_date': None,
        'last_crawl_duration_seconds': None,
        'trigger_type': None
    }
    if game_list:
        status['summary'] = {game: {'price_per_1k': '-', 'total_count': 0} for game in game_list}
    return status


# ─────────────────────────────────────────────────
# 전역 상태 캡슐화 클래스
# ─────────────────────────────────────────────────
class CrawlerState:
    """웹 서버의 모든 런타임 상태를 단일 객체로 관리합니다."""

    def __init__(self):
        # 그룹별 초기화 헬퍼로 분해(v7.0 트랙 리팩토링). 각 헬퍼의 대입 순서와
        # 아래 호출 순서가 곧 원래 __init__ 의 속성 생성 순서다 — 순서·초기값·주석 보존.
        self._init_socketio()
        self._init_crawler_runtime()
        self._init_notification_toggles()
        self._init_crawler_manager()
        self._init_executors()
        self._init_sub_crawler_status()
        self._init_comment_data()
        self._init_scheduler_and_display()
        self._init_admin_access()

    def _init_socketio(self):
        # Flask-SocketIO 인스턴스 (app.py에서 init_app으로 연결)
        # [v6.6.279] cors_allowed_origins="*" → 화이트리스트. IMT_ALLOWED_ORIGINS 로 오버라이드.
        # [v6.7.746] 공개 주소는 private_config.PUBLIC_ORIGINS(app.py CORS 와 같은 값). 운영 .env 에는 이 값이 없다 -
        #   v6.7.745 가 기본값에서 뺐다가 공개 주소 소켓이 400 이 됐다. 생성 시점에 모듈 속성으로 읽는다(시험이 바꿔 끼울 수 있게).
        _default_so_origins = list(_private_config.PUBLIC_ORIGINS) + [
            "http://localhost:5008",
            "http://127.0.0.1:5008",
            "http://localhost:5001",
        ]
        _env = os.environ.get('IMT_ALLOWED_ORIGINS', '').strip()
        _so_origins = [o.strip() for o in _env.split(',') if o.strip()] or _default_so_origins
        # [v6.7.236] ping_timeout 20s(기본) → 60s. 단일 워커(-w 1) GIL 경합(Gemma4/situation CTE/Selenium)으로
        #   짧은 stall 발생 시 기본 관용폭(interval 25 + timeout 20 = 45s)을 넘으면 클라가 끊고 재연결 → 체감 '순단'.
        #   timeout 60 으로 클라 grace 25+60=85s 확대 → 일시 stall 이 WS disconnect 로 안 번지게(blip 억제).
        #   대시보드 admin 소수 사용이라 죽은 연결 지연 정리(최대 ~85s) 자원 영향 무시 가능.
        self.socketio = SocketIO(cors_allowed_origins=_so_origins, async_mode='threading',
                                 compression_threshold=1024, ping_interval=25, ping_timeout=60)

    def _init_crawler_runtime(self):
        # 댓글 크롤러
        self.contents_stop_event = threading.Event()
        self.comment_trigger_event = threading.Event()
        self._comment_data_manager = None

        # 내용 크롤러 (본문 추출)
        self._content_extractor_thread = None
        self._content_extractor_restart_count = 0

    def _init_notification_toggles(self):
        # Google Sheets 쓰기 토글 (기본 OFF: SQLite에만 저장, 시트 읽기는 유지)
        self.gsheet_write_enabled = False

        # [v6.6.343] 카카오워크 알림 카테고리별 토글 — 웹앱(app.py) 프로세스 내부에서 발송되는 것만.
        # `ai_processor.py` / `ai_processor_gemma4.py` / CCU 크롤러는 독립 데몬 또는 터미널앱 전용이라 제외.
        # 앱 기동 시 SQLite notification_toggles 테이블에서 덮어씀. 관리자 API 로 갱신.
        self.notification_toggles = {
            'ai_summary': True,       # AI 요약 (Gemma4 웹앱 워커 — ai_lib/gemma4_summary_core.py)
            'crawler_restart': True,  # 메인/댓글/본문 크롤러 재시작 (web_lib/bg_tasks.py, v6.6.341)
            'crawler_healing': True,  # 서브 크롤러 자가 치유 요약 (crawlers/account_trade.py 등 웹앱 batch-run 경로)
            'crash_report': True,     # 치명 오류 크래시 리포트 (utils/notification_manager.send_crash_report)
            'keyword_alert': True,    # [v6.6.492] SUPER 키워드 매칭 (crawlers/crawler.py). default True — 터미널앱은 그대로 ON, 웹앱은 app.py 부팅 시드로 False 강제 (알림 중복 방지).
            'trend_digest': True,     # [v6.7.472] 종합 동향 리포트 (09:30/21:30, web_lib/bg/trend_digest.py). 죽은 'nightly_digest' 와 별개 키 - 그쪽은 '야간' 의미라 09:30 발송과 어긋난다.
            'official_alert': True,   # [v6.7.7] 다음 카페 OFFICIAL 공지 알림. keyword_alert 와 독립 - 웹앱 기본 ON (홈 서버 24/7 백업 발송). 터미널앱은 web_lib.globals import 실패 → True 폴백 유지.
        }
        self.notification_toggles_lock = threading.Lock()

    def _init_crawler_manager(self):
        # 크롤러 매니저 및 초기화 락
        self._crawler_manager = None
        self.crawler_init_thread = None
        self._crawler_started = False
        self._init_lock = threading.Lock()

    def _init_executors(self):
        # 작업 큐 스레드 풀 (NAS 환경 안정성을 위해 단일 스레드로 제한)
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.main_crawler_executor = ThreadPoolExecutor(max_workers=1)
        self.export_executor = ThreadPoolExecutor(max_workers=1)

    def _init_sub_crawler_status(self):
        # 서브 크롤러 상태 딕셔너리
        self.odin_global_status = create_crawler_status()
        self.account_trade_status = create_crawler_status()
        self.jasa_games_status = create_crawler_status()
        self.tasa_games_status = create_crawler_status()

        # 크롤러 실행 객체 및 메타데이터 관리
        self.CRAWLER_EXECUTIONS = {
            'main': {'execution': None, 'info': {}},
            'odin_global': {'execution': None, 'info': {}},
            'account_trade': {'execution': None, 'info': {}},
            'jasa_games': {'execution': None, 'info': {}},
            'tasa_games': {'execution': None, 'info': {}}
        }

        # [v6.6.226] ai_analysis_status 제거 — /api/ai-analysis가 SQLite 직접 조회로 전환되어
        # 메모리 캐시 불필요.

    def _init_comment_data(self):
        # 댓글 데이터 상태 (메모리 캐시)
        # [v6.6.279] _comment_lock 추가 — APScheduler 스레드(쓰기) + Flask route(읽기/쓰기) +
        # WebSocket(읽기) 3경로 동시 접근. 이전엔 atomic dict swap 패턴으로 부분 보호했으나
        # 일부 key 재할당 경로(`['latest_results'] = ...`) 가 남아있어 순회 중 RuntimeError 위험.
        # 이 lock 은 읽기/쓰기 모두 감싸서 일관성 보장. 스냅샷 복사 후 lock 해제하는 짧은 critical section.
        self._comment_lock = threading.Lock()
        self.comment_data_status = {
            'latest_results': [],
            'last_updated': None
        }

    def _init_scheduler_and_display(self):
        # APScheduler 인스턴스 (app.py에서 생성 후 주입)
        self.scheduler = None

        # 화면 표시 설정
        self.display_settings = {
            'limits': {},       # {game: int} 0=무제한
            'visibility': {},   # {game: bool}
            'comment_limit': 100
        }

    def _init_admin_access(self):
        # 관리자 접속 설정
        # [v6.7.745] ADMIN_IPS(집 공인 IP 코드값) 삭제 - 읽는 곳이 0 이었다(web_app 전체 grep, 2026-10-26).
        # [v6.6.291] ADMIN_SECRET_KEY 는 lazy 초기화 (`__getattr__` 참조).
        # 이전엔 __init__ 에서 즉시 env var 체크 + 미설정 시 경고 로그 출력했으나,
        # 터미널앱(`app_terminal.py`)이 `gsheet_write_enabled` 토글 켜려고
        # `import web_lib.globals` 만 해도 CrawlerState() 가 즉시 인스턴스화되며
        # 경고 로그가 출력되는 부작용 발생. 터미널은 ADMIN_SECRET_KEY 를 사용하지
        # 않으므로 (Flask 관리자 라우트 전용) 첫 실제 접근 시점까지 초기화 지연.
        self.admin_visit_logs = []   # [{time, name, ip, ua}]
        self.visitor_names = {}      # {visitor_id: name}  — 수동 지정 이름 (브라우저 토큰 키)
        self.visitor_status = {}     # {visitor_id: {last_active, name, ua, ip}}  — 현재 접속중
        # [v6.7.290] {visitor_id: monotonic epoch} — heartbeat→SQLite last_seen 쓰기 throttle(1~2분 주기). visitor_status 와 동일 lock.
        self.visitor_db_touch = {}
        # [v6.6.258] 방문 로그/상태 동시성 보호 — Flask route / Socket.IO / APScheduler 3경로 접근
        self._visit_lock = threading.Lock()
        # [v6.7.487] visitor_log SQLite write 를 요청 경로 밖으로 — 라우트는 여기 append 만 하고
        #   `bg/visitor_log.py` 잡이 20초마다 배치로 쓴다. 요청 경로 write 는 락 경합 시
        #   busy_timeout(30s)까지 gunicorn 스레드를 묶어 504 를 만든다(PATTERN-52 대응 4).
        #   maxlen — 배치 잡이 멎어도 메모리가 무한히 늘지 않게. 넘치면 오래된 것부터 버린다
        #   (방문 기록은 분석용이라 유실 허용, 대신 서비스는 절대 멈추지 않는다).
        self.visitor_log_queue = deque(maxlen=2000)

    def __getattr__(self, name):
        """[v6.6.291] ADMIN_SECRET_KEY 첫 접근 시점에 env var 검사 + 캐시.

        __getattr__ 은 일반 attribute lookup 실패 시에만 호출됨. 캐시 후엔
        instance dict 에 저장되므로 다음 호출부터는 호출되지 않음.
        """
        # [v6.7.483] 평문 fallback 제거 (사용자 결정 2026-08-04). 종전에는 env 미설정 시
        #   소스에 박아둔 고정 키를 썼다(v6.7.249, "다른 clone/재시작 시 키가 바뀌면 불편"이라는
        #   2026-06-20 지시). 그 값이 git tracked 라 **레포를 읽을 수 있으면 곧 admin 권한**이었고,
        #   private 레포라는 전제 하나에 인증이 통째로 기대고 있었다. 이제 미설정 시 매 기동
        #   랜덤 생성 + 경고 로그를 남긴다 — 레포에 유효 키가 존재하지 않게 된다.
        #   운영(홈 서버)은 compose 가 .env 를 environment 로 주입하므로 이 경로를 타지 않는다.
        #   env 없는 로컬/다른 clone 은 기동 로그의 경고에 찍힌 키를 그때그때 쓸 것(재기동마다 변경).
        if name in ('ADMIN_SECRET_KEY', 'MANAGER_SECRET_KEY'):
            env_name = 'IMT_ADMIN_SECRET_KEY' if name == 'ADMIN_SECRET_KEY' else 'IMT_MANAGER_SECRET_KEY'
            value = os.environ.get(env_name, '').strip()
            if not value:
                value = secrets.token_urlsafe(16)
                # 값을 찍는 이유: 이 경로는 env 미설정 = 로컬 개발 환경 전용이고, 안 찍으면
                # 로그인 자체가 불가능해진다. 운영은 위 주석대로 여기 도달하지 않는다.
                logger.warning(
                    "[SECURITY] %s 미설정 — 임시 키를 생성했다 (이번 기동 한정): ?login=%s",
                    env_name, value,
                )
            object.__setattr__(self, name, value)
            return value
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")


# ─────────────────────────────────────────────────
# 유틸리티 함수 (모듈 레벨 공개 API)
# ─────────────────────────────────────────────────
def get_client_ip(request):
    """모든 가능한 프록시 헤더를 확인하여 실제 클라이언트 아이피를 반환합니다."""
    xff = request.headers.get('X-Forwarded-For')
    if xff:
        return xff.split(',')[0].strip()
    xri = request.headers.get('X-Real-IP')
    if xri:
        return xri
    for header in ['Client-IP', 'X-Client-IP', 'X-Cluster-Client-IP']:
        val = request.headers.get(header)
        if val:
            return val
    return request.remote_addr


def peek_visitor_id(request):
    """브라우저별 영구 토큰(쿠키 imt_vid)을 읽는다. 없으면 IP 폴백 키('ip:<ip>').

    공유기(NAT) 뒤 다수 PC 가 같은 공인 IP 를 공유해도 브라우저(쿠키)별로 구분하기 위한 식별자.
    쿠키 발급(Set-Cookie)은 index() 라우트가 담당 — 여기선 읽기만 한다(소켓 핸들러도 공용 사용).
    """
    vid = request.cookies.get('imt_vid')
    if vid and 0 < len(vid) <= 64:
        return vid
    return f'ip:{get_client_ip(request)}'


def get_device_info(ua_string):
    """User-Agent 문자열에서 기기 종류를 추출합니다."""
    ua = ua_string.lower() if ua_string else ""
    if 'iphone' in ua: return 'iPhone'
    elif 'ipad' in ua: return 'iPad'
    elif 'android' in ua:
        if 'mobile' in ua: return 'Android Mobile'
        return 'Android Tablet'
    elif 'macintosh' in ua or 'mac os x' in ua: return 'Mac'
    elif 'windows' in ua: return 'Windows PC'
    elif 'linux' in ua: return 'Linux PC'
    return 'Unknown Device'


# ─────────────────────────────────────────────────
# 모듈 프록시 — gl.xxx 접근을 CrawlerState로 투명하게 라우팅
# 기존 153개 참조처를 수정하지 않아도 됩니다.
# ─────────────────────────────────────────────────
_MODULE_LEVEL_NAMES = frozenset({
    'CrawlerState', 'create_crawler_status', 'get_client_ip', 'get_device_info', 'peek_visitor_id',
    '__name__', '__doc__', '__package__', '__loader__', '__spec__',
    '__file__', '__cached__', '__builtins__',
})


class _ProxyModule(types.ModuleType):
    """CrawlerState 인스턴스를 투명하게 노출하는 모듈 래퍼."""

    __slots__ = ('_state',)

    def __init__(self, original_module):
        super().__init__(original_module.__name__, original_module.__doc__)
        object.__setattr__(self, '_state', CrawlerState())
        # 모듈 레벨 함수/클래스는 __dict__에 직접 보관
        for name in _MODULE_LEVEL_NAMES:
            if name in original_module.__dict__:
                self.__dict__[name] = original_module.__dict__[name]
        # 나머지 dunder 메타데이터 복사
        for attr in ('__file__', '__loader__', '__package__', '__spec__', '__path__'):
            if hasattr(original_module, attr):
                self.__dict__[attr] = getattr(original_module, attr)

    def __getattr__(self, name):
        state = object.__getattribute__(self, '_state')
        try:
            return getattr(state, name)
        except AttributeError:
            raise AttributeError(f"module {self.__name__!r} has no attribute {name!r}")

    def __setattr__(self, name, value):
        if name == '_state':
            object.__setattr__(self, name, value)
            return
        state = object.__getattribute__(self, '_state')
        if hasattr(state, name):
            setattr(state, name, value)
        else:
            # 새 속성은 state에 동적으로 추가
            setattr(state, name, value)


sys.modules[__name__] = _ProxyModule(sys.modules[__name__])
