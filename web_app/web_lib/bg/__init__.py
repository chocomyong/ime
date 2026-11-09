# -*- coding: utf-8 -*-
"""
web_lib.bg — Phase 2 분할 패키지.

기존 `web_lib/bg_tasks.py` 1,247 LOC 를 기능 축으로 분할한다.
`bg_tasks.py` 는 **re-export 전용 facade** 로 유지되어
`from web_lib.bg_tasks import X` 외부 호출 경로를 모두 보존한다.

분할 진행 상태:
- broadcast.py : emit_status_update, build_status_payloads, update_client_status (3개) — v6.6.375 이관 완료
- 그 외       : web_lib/bg_tasks.py 에 잔존 (후속 sub-module 에서 이관)

**새 함수를 여기에 직접 추가하지 말 것.** 반드시 기능 축에 맞는 sub-module 에 작성하고
`bg_tasks.py` 의 re-export 목록도 동시에 갱신하라.
"""
