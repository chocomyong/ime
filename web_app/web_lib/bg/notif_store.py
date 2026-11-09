# -*- coding: utf-8 -*-
"""bg.notif_store — 서버측 알림 보관소 원장 (v6.7.427, 2026-07-22)

배너 3종(AI 요약/CCU 급감/워처 공지)을 서버가 발생 시점에 직접 적재하는 공유 원장.
기존 보관소(main/notif-inbox.js)는 클라 socket.js showInAppBanner 가 localStorage 에 쌓는
방식이라 **앱이 닫혀 있으면 미적재** — 설치형 PWA(폰) 푸시로만 알던 알림이 웹 보관소(상단
종)엔 남지 않았다. 이 모듈이 서버 원천에서 직접 기록해 앱 상태와 무관하게 남긴다.

설계 결정 (2026-07-22 사용자):
  - **서버 공유 원장**: 모든 admin/manager 세션이 동일 상태의 보관소를 본다(개인별 아님 —
    이 시스템의 신원 = 역할(admin/manager), 개인 계정 없음). localStorage per-device 폐기.
  - **읽음/미확인 추적 없음**: "어디까지 봤나는 안 중요, 최신 확인 가능 상태만 중요. 놓치는
    건 앱이 판단할 게 아니다." → lastSeen/미확인 배지 제거. 알림 역할은 배너(열림)/OS 푸시
    (닫힘)가 하고, 종은 순수 아카이브.
  - **적재는 구독 무관 무조건**: 보관소가 주역이고 푸시는 부산물. CCU/워처 적재를 push 구독
    유무에 묶지 않는다(web_push.run_push_watch_cycle 의 has_subs 게이트 제거). send_push 만
    구독 있을 때 발화.
  - **수동 삭제 없음**: 상한 300 FIFO 자동 롤만. "전체 삭제" 버튼 제거(공유 원장에서 한 명이
    누르면 전체 폭파 벡터 — 삭제를 없애 벡터 자체 소멸).

저장:
  - 공유 volume(/app/data)의 notif_inbox.json — web_push.py _SUBS_FILE 패턴 동형(threading.Lock
    + atomic replace). 웹앱은 gunicorn -w 1 단일 워커라 in-process 락으로 충분.
  - (title+body+url) 5분 dedup: 같은 이벤트가 여러 경로로 두 번 안 쌓이게.

생산 지점 (전부 서버측):
  1. AI 요약  : ai_gemma4_worker.py (건별)
  2. CCU 급감 : web_push.py _watch_ccu
  (3. 워처 공지 - web_push.py 의 워처 이벤트 감시는 [v6.7.741] 삭제. 워처 종료(사용자 2026-10-24).
     원장에 남은 옛 항목(src NAVER/POE · evt)은 300 FIFO 로 밀려 나간다 - 클라 배지 맵에서 POE·evt 를 뺐으므로
     남은 동안은 배지 없이 제목·본문만 보인다.)
  그 밖에 notice(AI 요약 정지 · 파이프라인 정지 · 동향 다이제스트) · anomaly · crawl(공식 게시판 연속 실패)이
  있다 - record() docstring 의 kind 목록 참조. (dc = 디코 판정은 [v6.7.742] 잡째 삭제 - 원장의 옛 dc 항목은 300 FIFO 로 밀려 나간다.)
"""
from __future__ import annotations
import json
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

_FILE = "notif_inbox.json"
_MAX = 300
_DUP_WINDOW_MS = 300_000   # (title+body+url) 5분 중복 가드
_lock = threading.Lock()


def _data_dir():
    """메인 DB 디렉토리 (= 공유 volume imt_db_data). web_push._data_dir 동형."""
    try:
        from sqlite_handler import db_handler
        return os.path.dirname(db_handler.db_path)
    except Exception:
        return "/app/data"


def _path():
    return os.path.join(_data_dir(), _FILE)


def _load():
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            v = json.load(f)
            return v if isinstance(v, list) else []
    except Exception:
        return []


def _save_atomic(items):
    p = _path()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def record(kind, title, body="", url="/", src="", game="", evt="", link="", min_role=""):
    """알림 1건 적재(최신이 맨 앞). kind ∈ {'ai','ccu','notice','anomaly','crawl'}(crawl = v6.7.739 공식 게시판
    연속 실패, crawler.py _note_official_board_result). 필드는 클라 렌더 배지와 대응
    (notif-inbox.js KIND_BADGE/SRC_LABEL/EVT_LABEL). (title+body+url) 5분 내 중복은 스킵.
    예외는 삼킨다 — 적재 실패가 발송/감시 사이클을 막지 않게.

    [v6.7.431] link = 원문 글 URL(선택). AI 요약 알림 클릭 시 종(보관소)에서 AI 요약 '탭'(admin 전용)
    대신 원문 글로 이동하기 위한 필드. AI 만 채우고 CCU/워처는 url(?tab= 딥링크) 그대로 유지 —
    notif-inbox.js 클릭 핸들러가 kind==='ai' && link 일 때만 원문(openArticle)으로 분기. 옛 record
    (link 없음)는 빈 값이라 기존 딥링크 폴백(하위호환)."""
    try:
        now_ms = int(time.time() * 1000)
        rec = {
            "ts": now_ms,
            "kind": str(kind or ""),
            "title": str(title or ""),
            "body": str(body or ""),
            "url": str(url or "/"),
            "src": str(src or ""),
            "game": str(game or ""),
            "evt": str(evt or ""),
            "link": str(link or ""),
            # [v6.7.438] min_role='admin' 이면 admin 세션에만 노출. 이 원장은 admin/manager 공유라
            #   그냥 적재하면 매니저에게도 보인다 - 필터는 서버(/api/notif-inbox)가 걸어 클라 수정 0,
            #   기존 저장분은 이 필드가 없어 빈 값 = 전원 노출(하위호환).
            "min_role": str(min_role or ""),
        }
        with _lock:
            items = _load()
            for x in items:
                # newest-first 라 dup 창을 벗어난 지점부터는 전부 더 오래됨 — 조기 종료.
                if (now_ms - (x.get("ts") or 0)) >= _DUP_WINDOW_MS:
                    break
                if (x.get("title") == rec["title"] and x.get("body") == rec["body"]
                        and x.get("url") == rec["url"]):
                    return
            items.insert(0, rec)
            if len(items) > _MAX:
                del items[_MAX:]
            _save_atomic(items)
    except Exception as e:
        logger.warning("[NOTIF-STORE] 적재 실패(무시): %s", e)


def list_recent(limit=200):
    """최신순 목록 (기본 200, 상한 300). 읽음 상태 없음 — 그대로 반환."""
    items = _load()
    if limit and len(items) > limit:
        return items[:limit]
    return items
