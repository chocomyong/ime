# -*- coding: utf-8 -*-
"""특성화 스냅샷 검사 - `CrawlerState.__init__` 이 만드는 전역 상태가 그대로인가.

왜 있나: `web_lib/globals.py` 의 CrawlerState 는 웹앱 전역 상태의 원천이다. 다른 모듈 150여 곳이
`gl.<속성>` 으로 읽으므로, __init__ 을 리팩토링(그룹별 헬퍼 분해 등)하다 속성 하나를 빠뜨리거나,
초기값·타입을 바꾸거나, 락/이벤트/executor 객체를 잘못 공유(aliasing)하거나, 생성 순서를 뒤바꿔도
import 통과·크래시 부재로는 안 보인다(PATTERN-63). 그래서 **새 인스턴스를 만들어**(운영 싱글톤이
아니라 fresh CrawlerState()) 인스턴스의 속성 전수를 직렬화 스냅샷으로 박제하고 비교한다:
  ① 속성명 -> 타입/초기값 서술(락·이벤트·deque·executor·SocketIO 는 타입 + 핵심 인자)
  ② 속성 생성 순서(`vars()` 삽입 순서 - 순서 뒤바꿈 회귀 탐지)
  ③ 참조형 객체 aliasing(두 속성이 같은 dict/lock 객체를 가리키면 그룹으로 - 정상은 그룹 0개)

환경변수 분기: __init__ 의 유일한 외부 의존은 `IMT_ALLOWED_ORIGINS`(SocketIO cors origins)다.
미설정/커스텀/공백만(폴백) 세 시나리오로 분기를 덮는다. DB·파일·네트워크는 __init__ 에 없다.

**라이브 컨테이너에서 실행 금지** - 이 검사는 운영 프로세스의 전역 상태를 건드리지 않고 **새
CrawlerState 인스턴스만** 만들어 잰다. 그러나 `import web_lib.globals` 자체가 모듈 프록시로 인스턴스
하나를 만드므로(부작용 0: 스레드 미기동·DB 미접속·네트워크 없음), 개발 머신·CI 에서만 돌린다.

주의: `ADMIN_SECRET_KEY`/`MANAGER_SECRET_KEY` 는 __init__ 이 아니라 `__getattr__` lazy 경로라
fresh 인스턴스의 vars() 에 없다(접근 시 secrets 생성 + 경고 로그). 이 검사는 __init__ 계약만 재므로
그 둘을 일부러 접근하지 않는다.

사용:
    IMT_DISABLE_BOOTSTRAP=1 python3 test_globals_state_contract.py            # 스냅샷과 비교 (FAIL 이면 exit 1)
    IMT_DISABLE_BOOTSTRAP=1 python3 test_globals_state_contract.py --update   # 의도한 변경 후 스냅샷 갱신

속성·초기값·순서·aliasing 을 바꾸면 이 검사가 FAIL 한다 - 의도한 변경이면 --update 로 갱신하고
diff 를 커밋에서 눈으로 확인할 것(그 diff 가 곧 "무엇이 바뀌었나" 의 기록이다).
"""
import json
import os
import sys
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault('IMT_DISABLE_BOOTSTRAP', '1')
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
SNAPSHOT = os.path.join(HERE, 'tests', 'globals_state_snapshot.json')

_LOCK_TYPE = type(threading.Lock())


def _describe(v):
    """한 속성 값을 안정적인 직렬화 서술로. 락/이벤트/deque/executor/SocketIO 는
    타입 + 핵심 인자만(객체 정체성이 아니라 계약을 잰다). dict/list 는 값 그대로."""
    if v is None:
        return "None"
    if isinstance(v, bool):            # bool 은 int 하위형이라 먼저 검사
        return f"bool:{v}"
    if isinstance(v, int):
        return f"int:{v}"
    if isinstance(v, str):
        return f"str:{v!r}"
    if isinstance(v, _LOCK_TYPE):
        return "lock"
    if isinstance(v, threading.Event):
        return f"Event(set={v.is_set()})"
    if isinstance(v, deque):
        return {"kind": "deque", "maxlen": v.maxlen, "items": list(v)}
    if isinstance(v, ThreadPoolExecutor):
        return {"kind": "ThreadPoolExecutor", "max_workers": v._max_workers}
    # SocketIO: cors origins(환경변수 분기) + ping/compression 계약을 server_options 로 포착
    if type(v).__name__ == 'SocketIO':
        return {"kind": "SocketIO", "server_options": getattr(v, 'server_options', None)}
    if isinstance(v, list):
        return {"kind": "list", "value": v}
    if isinstance(v, dict):
        return {"kind": "dict", "value": v}
    return {"kind": "unknown",
            "type": f"{type(v).__module__}.{type(v).__name__}", "repr": repr(v)}


_REFY = (dict, list, _LOCK_TYPE, threading.Event, deque, ThreadPoolExecutor)


def _identity_aliases(inst):
    """참조형 속성 중 같은 객체를 가리키는 그룹(정상=없음). 락/dict 를 실수로 공유하면 잡힌다.
    None/False/0 같은 불변 싱글톤은 정체성 공유가 정상이라 제외."""
    byid = {}
    for name, val in vars(inst).items():
        if isinstance(val, _REFY) or type(val).__name__ == 'SocketIO':
            byid.setdefault(id(val), []).append(name)
    groups = [sorted(names) for names in byid.values() if len(names) > 1]
    return sorted(groups)


def _snapshot_instance(inst):
    return {
        "attributes": {name: _describe(val) for name, val in vars(inst).items()},
        "order": list(vars(inst).keys()),
        "identity_aliases": _identity_aliases(inst),
    }


# ── 시나리오: IMT_ALLOWED_ORIGINS 분기 ────────────────────────────────────────
_SCENARIOS = [
    ('default_origins_unset', None),                              # env 미설정 -> 기본 화이트리스트
    ('custom_origins', 'https://a.example, https://b.example ,https://c.example'),  # strip/필터 경로
    ('whitespace_only_falls_back', '  ,  ,'),                     # 전부 공백 -> falsy -> 기본으로 폴백
]


# [v6.7.746] 공개 주소는 비공개 값(utils/private_config.PUBLIC_ORIGINS)이라 스냅샷에 실제 값을 넣지 않는다 - 고정 가짜 값을
#   끼워 "env 미설정이면 공개 주소가 기본값 맨 앞에 온다" 를 채점한다. v6.7.745 는 이것을 빼서 라이브 소켓이 400 이 됐다.
_FAKE_PUBLIC = ['https://public.example']


def collect():
    import web_lib.globals as gl_mod
    CrawlerState = gl_mod.CrawlerState
    sys.modules['private_config'].PUBLIC_ORIGINS = list(_FAKE_PUBLIC)   # globals 가 import 한 그 모듈 객체
    out = {}
    for name, envval in _SCENARIOS:
        saved = os.environ.get('IMT_ALLOWED_ORIGINS')
        try:
            if envval is None:
                os.environ.pop('IMT_ALLOWED_ORIGINS', None)
            else:
                os.environ['IMT_ALLOWED_ORIGINS'] = envval
            inst = CrawlerState()
            out[name] = _snapshot_instance(inst)
        finally:
            if saved is None:
                os.environ.pop('IMT_ALLOWED_ORIGINS', None)
            else:
                os.environ['IMT_ALLOWED_ORIGINS'] = saved
    return out


def main():
    got = collect()
    text = json.dumps(got, ensure_ascii=False, indent=1, sort_keys=True, default=str) + "\n"
    n_attrs = len(next(iter(got.values()))['attributes']) if got else 0
    if '--update' in sys.argv:
        os.makedirs(os.path.dirname(SNAPSHOT), exist_ok=True)
        with open(SNAPSHOT, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        print(f"[UPDATED] {SNAPSHOT} (scenarios={len(got)}, attrs={n_attrs})")
        return 0
    if not os.path.exists(SNAPSHOT):
        print(f"[FAIL] 스냅샷 없음 - 먼저 --update 로 생성: {SNAPSHOT}")
        return 1
    with open(SNAPSHOT, encoding='utf-8') as f:
        want = f.read()
    if text == want:
        print(f"[PASS] CrawlerState.__init__ 상태 계약 동일 (scenarios={len(got)}, attrs={n_attrs})")
        return 0
    import difflib
    print("[FAIL] CrawlerState.__init__ 상태 계약이 스냅샷과 다르다:")
    for line in difflib.unified_diff(
            want.splitlines(), text.splitlines(), 'snapshot', 'now', lineterm='', n=1):
        print(line)
    return 1


if __name__ == '__main__':
    sys.exit(main())
