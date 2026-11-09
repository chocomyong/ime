# -*- coding: utf-8 -*-
"""특성화(characterization) 스냅샷 - `ai_gemma4_worker._cycle_impl` 동작 고정.

## 왜 있나

`_cycle_impl` 은 AI 요약 파이프라인의 20초 주기 핵심이라 위험도가 가장 높다. 리팩토링이
분기 하나·호출 순서·저장 게이트·락/플래그 범위를 조용히 바꿔도 `import` 통과·크래시 부재로는
안 보인다(PATTERN-63/66). 그래서 Gemini/Gemma 코어·시트·소켓·웹푸시·DB·시간을 **전부 가짜로**
끼우고 실제 `_cycle_impl` 을 분기별로 태워, **호출 시퀀스·저장 대상·반환값·로그 마커·전역
상태**를 스냅샷과 비교한다. 운영 부작용 0 (실 API·실 DB·실 소켓·실 시트 미접촉).

이 검사가 잠그는 것(PATTERN-35/43/46/83/88 의 침묵 실패가 걸리는 자리):
  - 대기 0건·REMOTE_KILL·저장실패 쿨다운의 조기 반환 경로
  - 제보 게시판 개인 신고 글의 LLM-전 결정적 제외(저장 성공/실패 양쪽)
  - 1차 실패(429=쿼타 vs 5xx/기타=배치결함)의 서로 다른 처리 경로
  - 괄호형 placeholder('(...)') = 저장 생략(다음 사이클 재시도) vs '분석 제외' 영구 제외
  - 2차 통합 flush 가 저장·알림보다 **먼저** 불리는 순서
  - 저장 성공 후에만 캐시 refresh·소켓 emit·웹푸시가 돈다(저장 실패면 후속 전부 생략)

**라이브 컨테이너에서 실행 금지** - 개발 머신·CI 전용(가짜만 쓰므로 운영 DB 미접촉이지만
관행을 맞춘다). `import app` 을 하지 않으므로 `IMT_DISABLE_BOOTSTRAP` 도 불필요.

사용:
    python test_gemma4_cycle_contract.py            # 스냅샷과 비교 (FAIL 이면 exit 1)
    python test_gemma4_cycle_contract.py --update   # 의도한 변경 후 스냅샷 갱신

리팩토링으로 분기·순서·저장 게이트가 바뀌면 이 검사가 FAIL 한다 - 의도한 변경이면 --update 로
갱신하고 diff 를 커밋에서 눈으로 확인할 것(그 diff 가 곧 "무엇이 바뀌었나" 의 기록이다).
"""
import json
import logging
import os
import re
import sys
import threading
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.join(HERE, 'utils')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

SNAPSHOT = os.path.join(HERE, 'tests', 'gemma4_cycle_snapshot.json')
FIXED_CUTOFF = '2026-01-01 00:00:00'      # _WORKER_START_TIME 을 고정 - 로그의 since= 를 결정적으로
_DUR = re.compile(r'\d+\.\d+s')           # 사이클 소요(_cycle_el)만 로그에 뜨는 유일한 실수(nondeterministic)


def _norm(msg):
    """로그의 유일한 비결정 값(사이클 소요초)만 마스킹한다. 건수·사유·마커는 그대로 채점한다."""
    return _DUR.sub('<t>s', msg)


# ─────────────────────────────────────────────────────────────────────────────
# 대역(fake) - 각 경계에서 호출만 기록하고 시나리오가 지정한 동작을 흉내낸다
# ─────────────────────────────────────────────────────────────────────────────
class Recorder:
    def __init__(self):
        self._lock = threading.Lock()
        self.events = []        # 메인 스레드 호출(결정적 순서)
        self.gen_batches = []   # 워커 스레드 병렬 호출 - 순서 대신 집합으로 (정렬해 비교)
        self.logs = []          # [levelname, normalized_message]

    def ev(self, s):
        with self._lock:
            self.events.append(s)

    def gen(self, ids):
        with self._lock:
            self.gen_batches.append(sorted(ids))


class FakeDB:
    """sqlite_handler.db_handler 대역. save_ai_analysis 반환값은 호출 순서대로 소비한다."""
    def __init__(self, rec, pending, count_value, save_results):
        self._rec = rec
        self._pending = pending
        self._count = count_value
        self._save_results = list(save_results)

    def get_posts_pending_ai_summary(self, limit, since):
        self._rec.ev(f"db.get_pending(limit={limit},since={since})")
        return self._pending

    def count_posts_pending_ai_summary(self, since):
        self._rec.ev("db.count_pending")
        return self._count

    def save_ai_analysis(self, records):
        ok = self._save_results.pop(0) if self._save_results else True
        links = [r.get('link') for r in records]
        self._rec.ev(f"db.save(n={len(records)},ok={ok},links={links})")
        return ok


class FakeCore:
    def __init__(self, rec, should_kill, gen_fn, flush_fn):
        self._rec = rec
        self._should_kill = should_kill
        self._gen_fn = gen_fn
        self._flush_fn = flush_fn
        self._seen = []               # 1차가 모아 둔 레코드 - 실 코어의 defer_second 대응

    def maybe_reload_config(self):
        self._rec.ev("core.maybe_reload_config")

    def should_kill(self):
        self._rec.ev("core.should_kill")
        return self._should_kill

    def _generate_summary_batch(self, batch, defer_second=False):
        self._rec.gen([r.get('id') for r in batch])
        self._seen.extend(batch)
        self._gen_fn(batch)          # 1차 summary 를 채우거나 예외를 올린다

    def flush_deferred_second(self):
        # 실 코어는 여기서 2차 summary 를 레코드에 붙인다 - 그래서 저장·수집보다 먼저 불려야 한다.
        self._rec.ev("core.flush")
        return self._flush_fn(self._seen)


class SocketIOSpy:
    def __init__(self, rec, raises=False):
        self._rec = rec
        self._raises = raises

    def emit(self, event, data):
        self._rec.ev(f"socketio.emit({event},{data})")
        if self._raises:
            raise RuntimeError("emit boom")


# ── gen_fn / flush_fn 팩토리 ─────────────────────────────────────────────────
def gen_normal(batch):
    for r in batch:
        r['summary'] = f"요약{r['id']}"
        r['sentiment'] = 'neutral'
        r['severity'] = 'info'
        r['AI_Score'] = 0.5


def gen_stamp(text):
    def _f(batch):
        for r in batch:
            r['summary'] = text
    return _f


def gen_raise(exc_factory):
    def _f(batch):
        raise exc_factory()
    return _f


def flush_ret(n):
    return lambda seen: n


def flush_raise(seen):
    raise RuntimeError("flush boom")


def flush_finalize(text):
    """실 코어처럼 2차 flush 가 모인 레코드에 최종 summary 를 붙인다. 반환 = 처리 건수."""
    def _f(seen):
        for r in seen:
            r['summary'] = f"{text}{r['id']}"
        return len(seen)
    return _f


# ── pending 레코드 팩토리 ────────────────────────────────────────────────────
def post(i, game='POE', board='자유게시판', title='제목', content='본문'):
    return {'id': i, 'game': game, 'board_name': board, 'title': title,
            'content': content, 'link': f'L{i}'}


def personal(i):
    # _is_personal_report 가 True 를 주는 최소 조건: 배그 제보 게시판 + 신고 말머리
    return post(i, game='배틀그라운드', board='🚨┃제보 게시판', title='[팀킬티밍] 신고합니다')


# ─────────────────────────────────────────────────────────────────────────────
# 시나리오 정의 - _cycle_impl 에서 발견되는 분기 전부
# ─────────────────────────────────────────────────────────────────────────────
class World:
    def __init__(self, *, pending=None, count_value=0, save_results=(True,),
                 should_kill=False, gen_fn=gen_normal, flush_fn=flush_ret(0),
                 core_none=False, socketio=True, batch_size=2, batch_parallel=3,
                 init_save_fail_until=0.0, init_save_fail_logged=False,
                 refresh_raises=False, emit_raises=False,
                 send_push_raises=False, record_raises=False):
        self.pending = pending if pending is not None else []
        self.count_value = count_value
        self.save_results = save_results
        self.should_kill = should_kill
        self.gen_fn = gen_fn
        self.flush_fn = flush_fn
        self.core_none = core_none
        self.socketio = socketio
        self.batch_size = batch_size
        self.batch_parallel = batch_parallel
        self.init_save_fail_until = init_save_fail_until
        self.init_save_fail_logged = init_save_fail_logged
        self.refresh_raises = refresh_raises
        self.emit_raises = emit_raises
        self.send_push_raises = send_push_raises
        self.record_raises = record_raises


def scenarios():
    S = {}
    S['core_none'] = World(core_none=True)
    S['remote_kill'] = World(should_kill=True)
    S['save_fail_cooldown_first'] = World(
        init_save_fail_until='FUTURE', init_save_fail_logged=False)
    S['save_fail_cooldown_repeat'] = World(
        init_save_fail_until='FUTURE', init_save_fail_logged=True)
    # 쿨다운이 만료된 채(양수 값 잔존) 사이클이 돌아 저장에 성공하면 쿨다운 값이 0 으로 해제돼야 한다.
    S['cooldown_expired_then_save_ok'] = World(
        init_save_fail_until='PAST', pending=[post(0), post(1)])
    S['empty_pending'] = World(pending=[])
    S['all_personal'] = World(pending=[personal(0), personal(1)], save_results=(True,))
    S['personal_save_fail_then_continue'] = World(
        pending=[personal(0), personal(1), post(2), post(3)],
        save_results=(False, True))            # 개인신고 저장 실패, 이후 성공 저장은 OK
    S['normal_single_batch'] = World(pending=[post(0), post(1)])          # 1배치 순차
    S['normal_parallel'] = World(pending=[post(i) for i in range(5)])     # 3배치 병렬
    S['depth_exceeds_cap'] = World(
        pending=[post(i) for i in range(6)], count_value=9)               # len>=_lim -> count
    S['batch_error_quota'] = World(
        pending=[post(0), post(1)], gen_fn=gen_raise(lambda: Exception("429 RESOURCE_EXHAUSTED")))
    S['batch_error_generic'] = World(
        pending=[post(0), post(1)], gen_fn=gen_raise(lambda: Exception("500 INTERNAL")))
    S['deferred_placeholder'] = World(
        pending=[post(0), post(1)], gen_fn=gen_stamp('(2차 대기: 페이싱/쿨다운)'))
    S['deferred_exhausted'] = World(
        pending=[post(0), post(1)], gen_fn=gen_stamp('(전 모델 RPD 소진, 자정 리셋 대기)'))
    S['success_not_notable'] = World(
        pending=[post(0), post(1)], gen_fn=gen_stamp('[분석 제외]'))       # 저장O 알림X
    S['save_success_fail'] = World(pending=[post(0), post(1)], save_results=(False,))
    S['flush_raises'] = World(pending=[post(0), post(1)], flush_fn=flush_raise)
    S['flush_returns_merged'] = World(pending=[post(0), post(1)], flush_fn=flush_ret(2))
    # 1차는 summary 를 비워 두고(2차 대기), flush 가 최종 summary 를 붙인다 - flush 가 수집·저장보다
    #   **먼저** 불려야만 성공분이 생긴다(reorder 시 s='' 라 저장 0건 -> 스냅샷이 갈린다).
    S['flush_finalizes_summary'] = World(
        pending=[post(0), post(1)], gen_fn=gen_stamp(''), flush_fn=flush_finalize('완성'))
    S['refresh_raises'] = World(pending=[post(0), post(1)], refresh_raises=True)
    S['emit_raises'] = World(pending=[post(0), post(1)], emit_raises=True)
    S['push_send_raises'] = World(pending=[post(0), post(1)], send_push_raises=True)
    S['push_record_raises'] = World(pending=[post(0), post(1)], record_raises=True)
    S['socketio_none'] = World(pending=[post(0), post(1)], socketio=False)
    return S


# ─────────────────────────────────────────────────────────────────────────────
def _install_fakes(w, world, rec):
    """lazy import 되는 경계 모듈을 sys.modules 에 가짜로 끼운다."""
    # sqlite_handler.db_handler
    db = FakeDB(rec, world.pending, world.count_value, world.save_results)
    m_sql = types.ModuleType('sqlite_handler')
    m_sql.db_handler = db
    sys.modules['sqlite_handler'] = m_sql

    # ai_lib.gemma4_summary_core -> BATCH_SIZE / BATCH_PARALLEL 만 필요
    m_ai_pkg = types.ModuleType('ai_lib')
    m_core = types.ModuleType('ai_lib.gemma4_summary_core')
    m_core.BATCH_SIZE = world.batch_size
    m_core.BATCH_PARALLEL = world.batch_parallel
    sys.modules['ai_lib'] = m_ai_pkg
    sys.modules['ai_lib.gemma4_summary_core'] = m_core

    # web_lib.globals -> socketio (그리고 _handle_quota_failure 가 보는 scheduler=None)
    m_gl = types.ModuleType('web_lib.globals')
    m_gl.socketio = SocketIOSpy(rec, raises=world.emit_raises) if world.socketio else None
    m_gl.scheduler = None
    sys.modules['web_lib.globals'] = m_gl

    # web_lib.routes.analysis.refresh_ai_caches
    m_routes = types.ModuleType('web_lib.routes')
    m_analysis = types.ModuleType('web_lib.routes.analysis')

    def _refresh():
        rec.ev("routes.refresh_ai_caches")
        if world.refresh_raises:
            raise RuntimeError("refresh boom")
    m_analysis.refresh_ai_caches = _refresh
    sys.modules['web_lib.routes'] = m_routes
    sys.modules['web_lib.routes.analysis'] = m_analysis

    # web_lib.bg.web_push.send_push + web_lib.bg.notif_store.record
    m_bg = types.ModuleType('web_lib.bg')
    m_push = types.ModuleType('web_lib.bg.web_push')
    m_notif = types.ModuleType('web_lib.bg.notif_store')

    def _send_push(title, body, url, tag=None, game=None):
        rec.ev(f"push.send(title={title},game={game},tag={tag})")
        if world.send_push_raises:
            raise RuntimeError("push boom")

    def _record(kind, title, body, url, game=None, link=None):
        rec.ev(f"notif.record(kind={kind},game={game},link={link})")
        if world.record_raises:
            raise RuntimeError("record boom")
    m_push.send_push = _send_push
    m_notif.record = _record
    sys.modules['web_lib.bg'] = m_bg
    sys.modules['web_lib.bg.web_push'] = m_push
    sys.modules['web_lib.bg.notif_store'] = m_notif


def run_scenario(name, world):
    import web_lib.ai_gemma4_worker as w

    rec = Recorder()
    _install_fakes(w, world, rec)

    # 모듈 전역 초기화 (시나리오 간 누출 차단)
    w._save_fail_until = (time.monotonic() + 1000.0 if world.init_save_fail_until == 'FUTURE'
                          else time.monotonic() - 1.0 if world.init_save_fail_until == 'PAST'
                          else 0.0)
    w._save_fail_logged = world.init_save_fail_logged
    w._consecutive_quota_failures = 0
    w._quota_notified_date = None
    w._last_progress = 0.0
    w._WORKER_START_TIME = FIXED_CUTOFF

    core = None if world.core_none else FakeCore(
        rec, world.should_kill, world.gen_fn, world.flush_fn)
    _orig_get_core = w._get_core
    w._get_core = lambda: core

    cap = _Cap(rec)
    w.logger.addHandler(cap)
    _orig_level = w.logger.level
    w.logger.setLevel(logging.DEBUG)
    _orig_prop = w.logger.propagate
    w.logger.propagate = False
    try:
        ret = w._cycle_impl()
    finally:
        w.logger.removeHandler(cap)
        w.logger.setLevel(_orig_level)
        w.logger.propagate = _orig_prop
        w._get_core = _orig_get_core

    return {
        'return': repr(ret),
        'events': rec.events,
        'gen_batches': sorted(rec.gen_batches),
        'logs': rec.logs,
        'state': {
            'save_fail_cooldown_set': w._save_fail_until > 0.0,
            'save_fail_logged': bool(w._save_fail_logged),
            'consecutive_quota_failures': w._consecutive_quota_failures,
            'last_progress_advanced': w._last_progress > 0.0,
        },
    }


class _Cap(logging.Handler):
    def __init__(self, rec):
        super().__init__()
        self._rec = rec

    def emit(self, record):
        self._rec.logs.append([record.levelname, _norm(record.getMessage())])


def collect():
    out = {}
    for name, world in scenarios().items():
        out[name] = run_scenario(name, world)
    return out


def main():
    got = collect()
    text = json.dumps(got, ensure_ascii=False, indent=1, sort_keys=True, default=str) + "\n"
    if '--update' in sys.argv:
        os.makedirs(os.path.dirname(SNAPSHOT), exist_ok=True)
        with open(SNAPSHOT, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        print(f"[UPDATED] {SNAPSHOT} (시나리오 {len(got)}개)")
        return 0
    if not os.path.exists(SNAPSHOT):
        print(f"[FAIL] 스냅샷이 없다: {SNAPSHOT} (먼저 --update 로 생성)")
        return 1
    with open(SNAPSHOT, encoding='utf-8') as f:
        want = f.read()
    if text == want:
        print(f"[PASS] _cycle_impl 계약 동일 (시나리오 {len(got)}개)")
        return 0
    import difflib
    print("[FAIL] _cycle_impl 계약이 스냅샷과 다르다:")
    for line in difflib.unified_diff(want.splitlines(), text.splitlines(),
                                     'snapshot', 'now', lineterm='', n=1):
        print(line)
    return 1


if __name__ == '__main__':
    sys.exit(main())
