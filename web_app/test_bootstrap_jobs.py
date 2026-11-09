# -*- coding: utf-8 -*-
"""부팅 계약 스냅샷 검사 - `_bootstrap_background_jobs()` 가 등록하는 것이 그대로인가.

왜 있나: 이 함수는 import 시점에 도는 부팅 경로라 리팩토링이 조용히 잡 하나를 빠뜨리거나 주기를
바꿔도 import 통과 · 크래시 부재로는 안 보인다(PATTERN-63). 잡 id 목록은 imt-deploy 배포 후 게이트가
읽는 마커이기도 하다. 그래서 **스케줄러·스레드·DB 를 가짜로 바꿔 끼우고** 실제 함수를 태워, 등록된
add_job 호출(함수·트리거·인자)과 [BOOTSTRAP] 로그 줄을 스냅샷과 비교한다. 운영 부작용 0
(스레드 미기동, 스케줄러 미기동, 알림 토글 DB 미기록).

**라이브 컨테이너에서 실행 금지** - import app 이 운영 DB 에 sqlite 초기화를 건드린다(개발 머신·CI 전용).

사용:
    IMT_DISABLE_BOOTSTRAP=1 python test_bootstrap_jobs.py            # 스냅샷과 비교 (FAIL 이면 exit 1)
    IMT_DISABLE_BOOTSTRAP=1 python test_bootstrap_jobs.py --update   # 의도한 변경 후 스냅샷 갱신

잡을 추가·삭제·주기 변경하면 이 검사가 FAIL 한다 - 의도한 변경이면 --update 로 갱신하고 diff 를
커밋에서 눈으로 확인할 것(그 diff 가 곧 "무엇이 바뀌었나" 의 기록이다).
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
SNAPSHOT = os.path.join(HERE, 'tests', 'bootstrap_jobs_snapshot.json')


def _fn_name(f):
    return f"{getattr(f, '__module__', '?')}.{getattr(f, '__qualname__', repr(f))}"


class _Job:
    def __init__(self, id_):
        self.id = id_


class _RecScheduler:
    """BackgroundScheduler 대역 - 호출만 기록한다."""
    def __init__(self, t0):
        self.running = False
        self.calls = []
        self._t0 = t0

    def add_job(self, func, trigger=None, **kw):
        rec = {'func': _fn_name(func), 'trigger': trigger}
        for k, v in kw.items():
            if isinstance(v, datetime):  # run_date = now + N초 -> 상대 초(10초 단위 반올림)
                rec[k] = f"t0+{round((v - self._t0).total_seconds() / 10) * 10}s"
            else:
                rec[k] = v
        self.calls.append(rec)

    def start(self):
        self.running = True

    def get_jobs(self):
        return [_Job(c['id']) for c in self.calls if 'id' in c]


def collect():
    import app as appmod
    import web_lib.bg.heartbeat as heartbeat
    import notification_manager
    import sqlite_handler

    t0 = datetime.now()
    sched = _RecScheduler(t0)
    threads, side = [], []

    class _FakeThread:
        def __init__(self, target=None, **kw):
            threads.append(_fn_name(target))
        def start(self):
            pass

    saved = {
        'scheduler': appmod.scheduler,
        'threading': appmod.threading,
        'hb': heartbeat.start_heartbeat,
        'kill': notification_manager.NotificationManager.check_remote_kill,
        'get_t': sqlite_handler.db_handler.get_notification_toggles,
        'set_t': sqlite_handler.db_handler.set_notification_toggles_bulk,
    }
    lines = []

    class _H(logging.Handler):
        def emit(self, record):
            m = record.getMessage()
            if '[BOOTSTRAP]' in m:
                lines.append(m)

    h = _H()
    appmod.logger.addHandler(h)
    try:
        appmod.scheduler = sched
        appmod.threading = types.SimpleNamespace(Thread=_FakeThread)
        heartbeat.start_heartbeat = lambda: side.append('heartbeat')
        notification_manager.NotificationManager.check_remote_kill = lambda self, *a, **k: False
        sqlite_handler.db_handler.get_notification_toggles = lambda: {}
        sqlite_handler.db_handler.set_notification_toggles_bulk = lambda d: side.append(('toggles', d))
        fn = appmod._bootstrap_background_jobs
        fn._done = False
        fn()
    finally:
        appmod.scheduler = saved['scheduler']
        appmod.threading = saved['threading']
        heartbeat.start_heartbeat = saved['hb']
        notification_manager.NotificationManager.check_remote_kill = saved['kill']
        sqlite_handler.db_handler.get_notification_toggles = saved['get_t']
        sqlite_handler.db_handler.set_notification_toggles_bulk = saved['set_t']
        appmod.logger.removeHandler(h)
        appmod._bootstrap_background_jobs._done = False
    return {
        'threads': threads,
        'side_effects': side,
        'scheduler_started': sched.running,
        'jobs': sched.calls,
        'bootstrap_log': lines,
    }


def main():
    got = collect()
    text = json.dumps(got, ensure_ascii=False, indent=1, sort_keys=True, default=str) + "\n"
    if '--update' in sys.argv:
        os.makedirs(os.path.dirname(SNAPSHOT), exist_ok=True)
        with open(SNAPSHOT, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        print(f"[UPDATED] {SNAPSHOT} (jobs={len(got['jobs'])})")
        return 0
    with open(SNAPSHOT, encoding='utf-8') as f:
        want = f.read()
    if text == want:
        print(f"[PASS] 부팅 계약 동일 (jobs={len(got['jobs'])})")
        return 0
    import difflib
    print("[FAIL] 부팅 계약이 스냅샷과 다르다:")
    for line in difflib.unified_diff(want.splitlines(), text.splitlines(), 'snapshot', 'now', lineterm='', n=1):
        print(line)
    return 1


if __name__ == '__main__':
    sys.exit(main())
