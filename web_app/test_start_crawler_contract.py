# -*- coding: utf-8 -*-
"""특성화 스냅샷 검사 - `start_crawler_with_delay()` 의 관측 가능한 계약이 그대로인가.

왜 있나: 이 함수는 부팅 시 크롤러 스레드를 기동하는 고위험 경로다(부팅 계약 스냅샷
test_bootstrap_jobs.py 에서 스레드 target 으로 박제돼 있다). import 통과·크래시 부재로는
단계 하나가 빠지거나 지연값이 바뀌거나 예외 경계가 옮겨진 것을 못 본다(PATTERN-63/66).
그래서 **크롤러 싱글톤·time.sleep·threading.Thread·시트 인증·DB·DataManager 를 전부 가짜로
바꿔 끼우고** 실제 함수를 태워, ① 외부 의존 호출 시퀀스(순서·인자) ② 남긴 로그 ③ 전역 상태
변화를 시나리오별로 스냅샷과 비교한다. 운영 부작용 0(스레드 미기동, DB 미접속, 시트 미인증,
네트워크·Selenium 미기동, sleep 은 기록만 하고 즉시 반환).

**라이브 컨테이너에서 실행 금지** - 개발 머신·CI 전용. 실제 크롤러/Selenium/네트워크가 뜨면
안 되며, 이 검사는 그것을 전부 가짜로 대체해 그런 일이 없음을 전제한다.

사용:
    IMT_DISABLE_BOOTSTRAP=1 python3 test_start_crawler_contract.py            # 스냅샷과 비교 (FAIL 이면 exit 1)
    IMT_DISABLE_BOOTSTRAP=1 python3 test_start_crawler_contract.py --update   # 의도한 변경 후 스냅샷 갱신

단계 순서·지연값·예외 경계·분기를 바꾸면 이 검사가 FAIL 한다 - 의도한 변경이면 --update 로
갱신하고 diff 를 커밋에서 눈으로 확인할 것(그 diff 가 곧 "무엇이 바뀌었나" 의 기록이다).
"""
import json
import logging
import os
import sys
import types
from datetime import datetime

os.environ.setdefault('IMT_DISABLE_BOOTSTRAP', '1')
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
SNAPSHOT = os.path.join(HERE, 'tests', 'start_crawler_contract_snapshot.json')

# ── 시나리오가 참조하는 현재 컨텍스트(호출 기록기 + 설정 knob) ─────────────────
# sys.modules 에 심는 가짜 함수들은 이 전역을 통해 현재 시나리오의 기록기로 위임한다.
CTX = None


class _Raise:
    """이 knob 값이면 해당 지점에서 이 예외를 던진다는 표식."""
    def __init__(self, exc):
        self.exc = exc


class _DelayedSettings(dict):
    """crawler.settings 대역 - 지정 횟수만큼 falsy 로 보이다가 truthy 로 바뀐다.

    Phase 2 의 `while not crawler.settings and wait_count < 30` 대기 루프를
    결정론적으로 재현하기 위한 것. sleep 이 즉시 반환되므로 진짜 시간 경과 대신
    truthiness 검사 횟수로 준비 시점을 흉내낸다."""
    def __init__(self, ready_after, payload):
        super().__init__()
        self._checks = 0
        self._ready_after = ready_after
        self._payload = payload
        self._filled = False

    def __bool__(self):
        self._checks += 1
        if self._checks > self._ready_after:
            if not self._filled:
                self.update(self._payload)
                self._filled = True
            return True
        return False


class _FakeCrawler:
    def __init__(self, cfg):
        self._cfg = cfg
        self.raw_config_games = cfg.get('raw_config_games')
        settings = cfg.get('settings', {'URL_CONTENTS': 'DYNAMIC_URL'})
        self.settings = settings

    def _log(self, *a, **k):
        pass

    def extract_game_from_comment_link(self, link):
        CTX.calls.append(f"extract_game_from_comment_link({link!r})")
        if isinstance(self._cfg.get('extract_game'), _Raise):
            raise self._cfg['extract_game'].exc
        return f"GAME:{link}"

    def load_existing_data(self, posts):
        CTX.calls.append(f"load_existing_data(n={len(posts)})")
        return len(posts)


class _Ctx:
    """한 시나리오의 호출 기록 + 가짜 의존 구현."""
    def __init__(self, cfg):
        self.cfg = cfg
        self.calls = []
        self.crawler = _FakeCrawler(cfg)
        self._get_crawler_calls = 0

    # web_lib.bg.crawler_singleton
    def get_crawler(self, *a, **k):
        self._get_crawler_calls += 1
        argrepr = ', '.join(
            [repr(x) for x in a] + [f"{kk}={vv!r}" for kk, vv in k.items()]
        )
        self.calls.append(f"get_crawler({argrepr})")
        raise_on = self.cfg.get('get_crawler_raise_on_call')
        if raise_on == self._get_crawler_calls:
            raise self.cfg['get_crawler_exc']
        return self.crawler

    def init_crawler_async(self):
        self.calls.append("init_crawler_async()")

    # web_lib.bg.contents
    def start_extractor(self):
        self.calls.append("start_content_extractor_thread()")
        if isinstance(self.cfg.get('early_extractor'), _Raise) and self._first_extractor():
            raise self.cfg['early_extractor'].exc

    def _first_extractor(self):
        # Phase 0.5 의 첫 호출만 예외 대상 (Phase 3 의 두번째는 정상)
        n = sum(1 for c in self.calls if c == "start_content_extractor_thread()")
        return n == 1

    # web_lib.bg.sub_runners
    def ensure_sub(self, key, status, sub_key, summary_type):
        self.calls.append(
            f"ensure_sub_populated({key!r}, sub_key={sub_key!r}, summary_type={summary_type!r})"
        )
        if isinstance(self.cfg.get('ensure_sub'), _Raise):
            raise self.cfg['ensure_sub'].exc

    # sqlite_handler.db_handler
    def backfill_comment_games(self, cb):
        self.calls.append(f"backfill_comment_games(cb={getattr(cb, '__name__', '?')})")
        if isinstance(self.cfg.get('backfill'), _Raise):
            raise self.cfg['backfill'].exc
        return self.cfg.get('backfill', (0, 0))

    def get_comments(self, limit):
        self.calls.append(f"get_comments(limit={limit})")
        if isinstance(self.cfg.get('comments'), _Raise):
            raise self.cfg['comments'].exc
        # 픽스처 오염 방지 위해 매 시나리오 deepcopy 로 전달
        return [dict(c) for c in self.cfg.get('comments', [])]

    def get_sub_crawler_cache(self, key, day):
        self.calls.append(f"get_sub_crawler_cache({key!r}, {day!r})")
        spec = self.cfg.get('sub_cache', {}).get(key)
        if isinstance(spec, _Raise):
            raise spec.exc
        return spec

    def get_latest_posts(self, limit):
        self.calls.append(f"get_latest_posts(limit={limit})")
        if isinstance(self.cfg.get('posts'), _Raise):
            raise self.cfg['posts'].exc
        return list(self.cfg.get('posts', []))

    # google_sheets_handler.get_handler
    def get_handler(self, **kw):
        lf = kw.get('log_func')
        self.calls.append(f"get_handler(log_func={getattr(lf, '__name__', '?')})")
        if isinstance(self.cfg.get('get_handler'), _Raise):
            raise self.cfg['get_handler'].exc
        return object()  # 시트 핸들러 대역

    # contents_crawler.DataManagerAndUploader
    def data_manager(self, handler, initial_df, spreadsheet_url=None):
        self.calls.append(
            f"DataManagerAndUploader(initial_df_len={len(initial_df)}, spreadsheet_url={spreadsheet_url!r})"
        )
        if isinstance(self.cfg.get('data_manager'), _Raise):
            raise self.cfg['data_manager'].exc
        return "<DataManagerAndUploader>"


class _FakeThread:
    def __init__(self, target=None, args=(), kwargs=None, daemon=None, **extra):
        CTX.calls.append(
            f"Thread(target={getattr(target, '__name__', '?')}, kwargs={kwargs!r}, daemon={daemon!r})"
        )

    def start(self):
        CTX.calls.append("Thread.start()")


class _FixedDT:
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 1, 2, 3, 4, 5)


class _FakeTime:
    @staticmethod
    def sleep(sec):
        CTX.calls.append(f"time.sleep({sec})")

    @staticmethod
    def time():
        return 0.0


# ── sys.modules 가짜 주입 (scheduler import 전에) ────────────────────────────
def _install_fake_modules():
    def _mk(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m
        return m

    # 모듈 레벨 import 대역 (scheduler 가 import 시점에 바인딩)
    _mk('common', CONTENTS_SHEET_URL='CONTENTS_URL_DEFAULT',
        COMMUNITY_SHEET_URL_OVERRIDE=None,
        get_yesterday_str=lambda: 'YESTERDAY',
        normalize_timestamp=lambda s: s)
    _mk('google_sheets_handler', get_handler=lambda **kw: CTX.get_handler(**kw))
    _mk('contents_crawler',
        DataManagerAndUploader=lambda *a, **kw: CTX.data_manager(*a, **kw))

    class _DB:
        def backfill_comment_games(self, cb):
            return CTX.backfill_comment_games(cb)

        def get_comments(self, limit):
            return CTX.get_comments(limit)

        def get_sub_crawler_cache(self, key, day):
            return CTX.get_sub_crawler_cache(key, day)

        def get_latest_posts(self, limit):
            return CTX.get_latest_posts(limit)

    _mk('sqlite_handler', db_handler=_DB())

    # 함수 내부 lazy import 대역
    import web_lib
    import web_lib.bg  # 실제 경량 패키지 (docstring 뿐)

    def _mk_sub(name, **attrs):
        m = _mk(name, **attrs)
        parent_name, _, leaf = name.rpartition('.')
        setattr(sys.modules[parent_name], leaf, m)
        return m

    _mk_sub('web_lib.bg.crawler_singleton',
            get_crawler=lambda *a, **k: CTX.get_crawler(*a, **k),
            init_crawler_async=lambda: CTX.init_crawler_async())

    def _run_contents_crawler_supervised(*a, **k):
        # 스레드 target 이므로 실제로 호출되지 않는다 (Thread 가짜). 이름만 기록됨.
        pass

    _mk_sub('web_lib.bg.contents',
            _start_content_extractor_thread=lambda: CTX.start_extractor(),
            run_contents_crawler_supervised=_run_contents_crawler_supervised)
    _mk_sub('web_lib.bg.sub_runners',
            _ensure_sub_crawler_status_populated=lambda *a, **k: CTX.ensure_sub(*a, **k))


# ── 시나리오 정의 ────────────────────────────────────────────────────────────
_COMMENT_WITH_GAME = {'comment_link': 'http://x/1', 'link': 'http://cafe.daum.net/a', 'game': 'ODIN'}
_COMMENT_NO_GAME = {'comment_link': 'http://x/2', 'link': 'http://gall.dcinside.com/b', 'game': ''}
_FULL_SUB_CACHE = {
    'ODIN_GLOBAL': [{'row': 1}],
    'ACCOUNT_TRADE': [{'row': 2}],
    'JASA_GAMES': [{'row': 3}],
    'TASA_GAMES': [{'row': 4}],
}
_POSTS = [{'p': 1}, {'p': 2}, {'p': 3}]


def _scenarios():
    happy = {
        'raw_config_games': ['ODIN'],
        'backfill': (5, 2),
        'comments': [_COMMENT_WITH_GAME, _COMMENT_NO_GAME],
        'sub_cache': _FULL_SUB_CACHE,
        'posts': _POSTS,
        'settings': {'URL_CONTENTS': 'DYNAMIC_URL'},
    }
    return {
        'happy': happy,
        'empty_all': {
            'raw_config_games': None,
            'comments': [],
            'sub_cache': {},
            'posts': [],
            'settings': {'URL_CONTENTS': 'DYNAMIC_URL'},
        },
        'early_extractor_error': dict(happy, early_extractor=_Raise(RuntimeError('boom-extractor'))),
        'backfill_error': dict(happy, backfill=_Raise(RuntimeError('boom-backfill'))),
        'phase07_get_crawler_raises': dict(
            happy, get_crawler_raise_on_call=1, get_crawler_exc=RuntimeError('boom-get-crawler-1')),
        'preload_outer_error': dict(happy, comments=_Raise(RuntimeError('boom-comments'))),
        'sub_cache_partial_error': dict(
            happy, sub_cache=dict(_FULL_SUB_CACHE, JASA_GAMES=_Raise(RuntimeError('boom-subcache')))),
        'comment_game_fallback_error': dict(happy, extract_game=_Raise(RuntimeError('boom-extract'))),
        'phase2c_error': dict(happy, posts=_Raise(RuntimeError('boom-posts'))),
        'phase2d_error': dict(happy, get_handler=_Raise(RuntimeError('boom-handler'))),
        'phase2e_error': dict(happy, ensure_sub=_Raise(RuntimeError('boom-ensure'))),
        'settings_delayed': dict(happy, settings=_DelayedSettings(3, {'URL_CONTENTS': 'DYNAMIC_URL'})),
        'settings_never': dict(happy, settings=_DelayedSettings(9999, {})),
        'phase2_get_crawler_raises': dict(
            happy, get_crawler_raise_on_call=2, get_crawler_exc=RuntimeError('boom-get-crawler-2')),
    }


# ── 실행 + 캡처 ──────────────────────────────────────────────────────────────
def _run_one(sched, gl, name, cfg):
    global CTX
    import threading as real_threading

    CTX = _Ctx(cfg)

    # gl 베이스라인 (매 시나리오 새 dict — 함수가 여기에 쓴다)
    gl.comment_data_status = {}
    gl._comment_data_manager = None
    gl.odin_global_status = {}
    gl.account_trade_status = {}
    gl.jasa_games_status = {}
    gl.tasa_games_status = {}

    lines = []

    class _H(logging.Handler):
        def emit(self, record):
            lines.append(f"{record.levelname}: {record.getMessage()}")

    h = _H()
    h.setLevel(logging.DEBUG)
    sched.logger.addHandler(h)
    saved_level = sched.logger.level
    sched.logger.setLevel(logging.DEBUG)  # info 로그도 캡처 (로그-only 분기 채점)

    saved = {
        'time': sched.time,
        'dt': sched.dt_class,
        'get_handler': sched.get_handler,
        'dmu': sched.DataManagerAndUploader,
        'Thread': real_threading.Thread,
    }
    raised = None
    try:
        sched.time = _FakeTime
        sched.dt_class = _FixedDT
        sched.get_handler = lambda **kw: CTX.get_handler(**kw)
        sched.DataManagerAndUploader = lambda *a, **kw: CTX.data_manager(*a, **kw)
        real_threading.Thread = _FakeThread
        try:
            sched.start_crawler_with_delay()
        except Exception as e:
            raised = f"{type(e).__name__}: {e}"
    finally:
        sched.time = saved['time']
        sched.dt_class = saved['dt']
        sched.get_handler = saved['get_handler']
        sched.DataManagerAndUploader = saved['dmu']
        real_threading.Thread = saved['Thread']
        sched.logger.setLevel(saved_level)
        sched.logger.removeHandler(h)

    state = {
        'comment_data_status': gl.comment_data_status,
        'comment_data_manager': gl._comment_data_manager,
        'odin_global_status': gl.odin_global_status,
        'account_trade_status': gl.account_trade_status,
        'jasa_games_status': gl.jasa_games_status,
        'tasa_games_status': gl.tasa_games_status,
    }
    return {
        'calls': CTX.calls,
        'logs': lines,
        'state': state,
        'raised': raised,
    }


def collect():
    _install_fake_modules()
    import web_lib.globals as gl
    import web_lib.bg.scheduler as sched

    # gl 원상복구용 저장
    saved_gl = {k: getattr(gl, k) for k in (
        'comment_data_status', '_comment_data_manager', 'odin_global_status',
        'account_trade_status', 'jasa_games_status', 'tasa_games_status')}
    out = {}
    try:
        for name, cfg in _scenarios().items():
            out[name] = _run_one(sched, gl, name, cfg)
    finally:
        for k, v in saved_gl.items():
            setattr(gl, k, v)
    return out


def main():
    got = collect()
    text = json.dumps(got, ensure_ascii=False, indent=1, sort_keys=True, default=str) + "\n"
    if '--update' in sys.argv:
        os.makedirs(os.path.dirname(SNAPSHOT), exist_ok=True)
        with open(SNAPSHOT, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        print(f"[UPDATED] {SNAPSHOT} (scenarios={len(got)})")
        return 0
    if not os.path.exists(SNAPSHOT):
        print(f"[FAIL] 스냅샷 없음 - 먼저 --update 로 생성: {SNAPSHOT}")
        return 1
    with open(SNAPSHOT, encoding='utf-8') as f:
        want = f.read()
    if text == want:
        print(f"[PASS] start_crawler_with_delay 계약 동일 (scenarios={len(got)})")
        return 0
    import difflib
    print("[FAIL] start_crawler_with_delay 계약이 스냅샷과 다르다:")
    for line in difflib.unified_diff(
            want.splitlines(), text.splitlines(), 'snapshot', 'now', lineterm='', n=1):
        print(line)
    return 1


if __name__ == '__main__':
    sys.exit(main())
