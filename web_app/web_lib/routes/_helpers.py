# -*- coding: utf-8 -*-
"""routes._helpers — 여러 sub-module 이 공유하는 가드/헬퍼.

Phase 1 완료 시점 기준 수용 범위:
- `_require_role(min_role)`      : 등급 가드 (anon<manager<admin, 403 통일) — v6.7.248
- `_require_admin()`            : 관리자 가드 (`_require_role('admin')` 별칭, 403 통일)
- `_session_role()`             : 현재 세션 등급 문자열 (role 우선, 레거시 is_admin 폴백)
- `_access_lock_on()`           : access_lock ON 여부 (app.py /api 게이트 + 소켓 connect) - v6.7.733
- `create_excel_response()`     : pandas 기반 엑셀 다운로드 응답
- `_create_gsheet_export_endpoint()` : 구글 시트 비동기 export 트리거
"""
import io
import threading
from urllib.parse import quote

import pandas as pd
from flask import jsonify, make_response, session


def _make_ttl_cache(ttl_seconds):
    """TTL 캐시 (dict + Lock) 쌍 생성 헬퍼.

    반환 dict 구조: `{'data': None, 'ts': 0.0, 'ttl': ttl_seconds}`.
    표준 사용 패턴 (double-check lock):
        cache, lock = _make_ttl_cache(5.0)
        now = time.time()
        cached = cache.get('data')
        if cached is not None and (now - cache['ts']) < cache['ttl']:
            return cached
        with lock:
            now = time.time()
            cached = cache.get('data')
            if cached is not None and (now - cache['ts']) < cache['ttl']:
                return cached
            ...
            cache['data'] = result
            cache['ts'] = now
            return result
    """
    return {'data': None, 'ts': 0.0, 'ttl': ttl_seconds}, threading.Lock()


# [v6.7.248] 권한 등급 — anon < manager < admin. role 문자열(session['role'])이 source-of-truth,
#   is_admin(boolean)은 role=='admin' 파생(pages.py). manager = 일반 사용자 + 그라파나 CCU 1탭.
_ROLE_RANK = {'anon': 0, 'manager': 50, 'admin': 100}


def _session_role():
    """현재 세션 등급 문자열. role 우선, 없으면 레거시 is_admin 으로 폴백(배포 전 로그인 세션 호환)."""
    role = session.get('role')
    if role in _ROLE_RANK:
        return role
    return 'admin' if session.get('is_admin', False) else 'anon'


def _require_role(min_role='admin'):
    """등급 가드. 현재 세션 등급이 min_role 미만이면 403(admin_required).

    min_role='manager' → manager·admin 통과 / 'admin' → admin 만.
    클라이언트는 403 응답을 감지해 해당 UI 를 자동으로 감춘다 (`_revokeAdminUI`).
    """
    if _ROLE_RANK.get(_session_role(), 0) < _ROLE_RANK.get(min_role, 100):
        return jsonify({'error': 'Unauthorized', 'admin_required': True}), 403
    return None


def _require_admin():
    """관리자 전용 가드 — `_require_role('admin')` 별칭 (기존 호출처 호환)."""
    return _require_role('admin')


# [v6.7.733] 직전에 읽은 access_lock 값. get_system_setting 은 "행 없음" 과 "조회 실패" 를 같은 default 로
#   돌려줘 둘을 못 가른다. 운영은 행이 늘 있으므로 값을 못 받은 순간은 조회 실패다 - 그때 OFF 로 읽으면
#   일시 DB 오류가 곧 익명 개방이 된다. 그래서 직전 관측값을 쓴다. 한 번도 못 읽었으면(신규 DB) index() 와 같이 OFF.
_ACCESS_LOCK_LAST = {'val': None}
_LOCK_MISSING = object()


def _access_lock_on():
    """access_lock ON 여부. 원천은 index()(pages.py `_access_lock_response`) 와 같은 system_settings.access_lock."""
    try:
        from sqlite_handler import db_handler as _db
        val = _db.get_system_setting('access_lock', _LOCK_MISSING)
        if val is _LOCK_MISSING:
            # get_system_setting 은 '행 없음' 과 '조회 오류' 를 같은 default 로 돌려준다 - 여기서 다시 갈라 본다.
            row = _db._get_connection().execute(
                "SELECT value FROM system_settings WHERE key = ?", ('access_lock',)).fetchone()
            val = 'false' if row is None else row[0]   # 행 없음 = OFF (index 의 종전 기본과 같다)
    except Exception:
        val = None
    if val is None:
        # 진짜 조회 실패면 직전 값, 한 번도 못 읽었으면 ON - 재시작 직후 DB 오류가 익명에게 문을 열지 않게.
        val = _ACCESS_LOCK_LAST['val'] or 'true'
    else:
        _ACCESS_LOCK_LAST['val'] = val
    return str(val).lower() == 'true'


def create_excel_response(data_list, filename, sheet_name='Sheet1'):
    """데이터 리스트를 엑셀 파일 응답으로 변환."""
    if not data_list:
        return jsonify({'error': '내보낼 데이터 없음.'}), 404

    try:
        df = pd.DataFrame(data_list)
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name=sheet_name)

        encoded_filename = quote(filename)
        response = make_response(output.getvalue())
        response.headers['Content-Disposition'] = f'attachment; filename="{filename}"; filename*=UTF-8\'\'{encoded_filename}'
        response.headers['Content-Type'] = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        return response
    except Exception as e:
        # 로컬 import — 순환 회피 (bg_tasks 가 globals·routes 를 참조)
        from web_lib.bg_tasks import get_crawler
        get_crawler()._log(f"엑셀 내보내기 오류 ({filename}): {e}", 'error', 'EXCEL')
        return jsonify({'error': '엑셀 파일 생성 중 오류 발생.'}), 500


def _create_gsheet_export_endpoint(crawler_key, status_dict):
    """구글 시트 비동기 export 트리거.

    status_dict['latest_results'] 가 있으면 export_executor 에 제출하고 202 반환.
    비어 있으면 404.
    """
    data = status_dict.get('latest_results', [])
    if not data:
        return jsonify({'success': False, 'message': '내보낼 데이터 없음.'}), 404

    from web_lib.globals import export_executor
    from web_lib.bg_tasks import export_to_gsheet_background
    export_executor.submit(export_to_gsheet_background, crawler_key, data, overwrite=True)
    return jsonify({'success': True, 'message': 'Google 시트 내보내기 작업 시작.'}), 202
