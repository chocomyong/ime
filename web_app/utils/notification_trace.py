# -*- coding: utf-8 -*-
"""utils.notification_trace — 알림 결정 흐름 in-memory 트레이스.

[v6.6.527] Config_Keywords 매칭 → dedupe → 카카오워크 응답코드 까지
운영자가 5초 안에 "왜 안 갔는지/누가 받았는지" 추적할 수 있도록 ring buffer 에 박제.

이벤트 종류:
- type='match'  : 매칭 결정 시점. result ∈ {'queued', 'dedupe', 'initial_skip'}
- type='send'   : 카카오워크 발송 응답 도착 시점. http_status / ok / error_msg

저장은 in-memory deque(200) — 별도 DB 없이 시작. 부팅 시 비어 있음.
별 카테고리 구분 없이 한 ring 으로 — 클라가 type 으로 필터.
"""
import threading
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

_MAX = 200
_TRACE = deque(maxlen=_MAX)
_LOCK = threading.Lock()
_SEQ = 0


def record(event_type: str, **fields) -> dict:
    """이벤트 1건 기록. 호출자는 일반적으로 반환값 무시.
    fields 는 free-form — 알려진 키:
      kind   : 'SUPER' | 'OFFICIAL'
      game   : str
      source : 'DAUM' | 'DC' | ...
      title  : str
      link   : str
      keywords : list[str]
      result : 'queued' | 'dedupe' | 'initial_skip'   (match 전용)
      http_status : int (send 전용)
      ok     : bool (send 전용)
      error_msg : str (send 전용)
    """
    global _SEQ
    with _LOCK:
        _SEQ += 1
        entry = {
            'id': _SEQ,
            'ts': datetime.now(tz=ZoneInfo("Asia/Seoul")).isoformat(timespec='seconds'),
            'type': event_type,
        }
        entry.update(fields)
        _TRACE.append(entry)
        return entry


def snapshot(limit: int = 200) -> list:
    """최근 limit 건 사본 반환 (newest last)."""
    with _LOCK:
        if limit >= len(_TRACE):
            return list(_TRACE)
        return list(_TRACE)[-limit:]


def clear() -> int:
    """전체 비우기. 운영자 수동 리셋용."""
    with _LOCK:
        n = len(_TRACE)
        _TRACE.clear()
        return n
