# -*- coding: utf-8 -*-
"""bg.export — Google Sheets 비동기 쓰기 (데이터/설정).

- `main_crawler_export_callback(crawler_key, data)` : CrawlerManager 완료 콜백
  - MAIN_CRAWLER 완료 시 SQLite 저장 + 댓글 크롤러 트리거
  - 디바운싱(`_export_version`) — 같은 key 누적 시 최신 버전만 실행
- `_debounced_export(crawler_key, data, submitted_version)` : 큐 실행 시점 최신성 검사
- `export_to_gsheet_background(crawler_key, data_list, overwrite, target_date)` : 실제 시트 쓰기
- `upload_config_to_gsheet_background(data_map)` : Config_* 시트 일괄 업로드

디바운싱 상태 (`_export_version`, `_export_version_lock`) 는 본 모듈 소유.
외부 모듈에서 쓰기 없음(grep 확인) → 안전하게 이관.
"""
import threading
from datetime import datetime as dt_class
from zoneinfo import ZoneInfo
import logging

import pandas as pd

import web_lib.globals as gl
from common import (
    CONFIG_SHEET_URL, SHEET_NAME_MAP,
    COMMUNITY_SHEET_URL_OVERRIDE,
    get_yesterday_str,
    prepare_community_upload_df,
    finalize_community_group_df,
)
from google_sheets_handler import get_handler

logger = logging.getLogger(__name__)

# Google Sheets 쓰기 디바운싱: 같은 crawler_key 작업이 큐에 쌓이면 최신 것만 실행
_export_version = {}  # {crawler_key: int} — 제출 시 버전 증가, 실행 시 최신 버전만 처리
_export_version_lock = threading.Lock()


def main_crawler_export_callback(crawler_key, data):
    from web_lib.bg_tasks import get_crawler  # noqa — 순환 회피
    if crawler_key == "MAIN_CRAWLER":
        crawler_key = "COMMUNITY"
        # [신규] 메인 크롤러 주기 완료 시 댓글 크롤러 즉시 트리거
        if hasattr(gl, 'comment_trigger_event') and gl.comment_trigger_event:
            gl.comment_trigger_event.set()
        # SQLite에도 커뮤니티 게시글 저장 (재시작 시 선로드용)
        try:
            from sqlite_handler import db_handler as _db
            _db.save_community_posts(data)
        except Exception:
            pass
    # Google Sheets 쓰기 토글 OFF면 시트 업로드 생략 (SQLite 저장은 위에서 완료)
    if not gl.gsheet_write_enabled:
        return
    # 디바운싱: 버전 증가 후 제출, 실행 시점에 최신 버전이 아니면 스킵
    with _export_version_lock:
        ver = _export_version.get(crawler_key, 0) + 1
        _export_version[crawler_key] = ver
    gl.export_executor.submit(_debounced_export, crawler_key, data, ver)


def _debounced_export(crawler_key, data, submitted_version):
    """큐에서 꺼내졌을 때 최신 버전이 아니면 스킵 (네트워크 지연 시 중간 쓰기 생략)"""
    if _export_version.get(crawler_key, 0) != submitted_version:
        logger.debug(f"[G-SHEETS] {crawler_key} 쓰기 스킵 (v{submitted_version} < v{_export_version[crawler_key]})")
        return
    export_to_gsheet_background(crawler_key, data, overwrite=True)


def export_to_gsheet_background(crawler_key, data_list, overwrite=True, target_date=None):
    from web_lib.bg_tasks import get_crawler  # noqa — 순환 회피
    from crawler import GOOGLE_SHEET_UPLOAD_LIMIT
    if not gl.gsheet_write_enabled:
        return
    logger_instance = get_crawler()
    try:
        # [v6.6.488] crawler_key='COMMUNITY' 일 때만 임시 override 우선. 다른 key (CONTENTS 등) 는 동적값 그대로.
        if crawler_key == 'COMMUNITY' and COMMUNITY_SHEET_URL_OVERRIDE:
            spreadsheet_url = COMMUNITY_SHEET_URL_OVERRIDE
        else:
            spreadsheet_url = get_crawler().settings.get(f'URL_{crawler_key}')
        if not spreadsheet_url:
            raise ValueError(f"'{crawler_key}'에 대한 Google 시트 URL이 설정되지 않음.")

        if crawler_key == "COMMUNITY":
            if not data_list:
                data_list = get_crawler().get_all_latest_articles(limit=GOOGLE_SHEET_UPLOAD_LIMIT, per_game_limit=GOOGLE_SHEET_UPLOAD_LIMIT)
            if not data_list:
                return

            df = prepare_community_upload_df(data_list)
            allowed_dates = {dt_class.now(ZoneInfo("Asia/Seoul")).strftime('%Y-%m-%d')}

            handler = get_handler(log_func=logger_instance._log)
            if not handler.gc:
                raise Exception("Google Sheets 인증 실패.")

            for date_str, group in df.groupby('date_group'):
                if date_str not in allowed_dates:
                    continue
                worksheet_name = f"{date_str}_Community"
                final_df = finalize_community_group_df(group.sort_values(by='dt', ascending=False))
                success, msg = handler.upload_dataframe(spreadsheet_url, worksheet_name, final_df, overwrite=overwrite, hide_sheet=False, apply_style=True)
                if not success:
                    logger_instance._log(f"[{crawler_key}] {worksheet_name} 업로드 실패: {msg}", 'error', 'G-SHEETS')
            return

        date_str = target_date if target_date else get_yesterday_str()
        base_name = SHEET_NAME_MAP.get(crawler_key)
        worksheet_name = f"{date_str}_{base_name}"
        df = pd.DataFrame(data_list)
        for col in df.columns:
            if df[col].apply(lambda x: isinstance(x, list)).any():
                df[col] = df[col].apply(lambda x: ', '.join(map(str, x)) if isinstance(x, list) else x)
            df[col] = df[col].apply(lambda x: "'" + str(x) if isinstance(x, str) and str(x).startswith(('=', '+', '-', '@')) else x)

        handler = get_handler(log_func=logger_instance._log)
        if not handler.gc:
            raise Exception("Google Sheets 인증 실패.")

        success, msg = handler.upload_dataframe(spreadsheet_url, worksheet_name, df, overwrite=overwrite, hide_sheet=True, apply_style=False)

        if success and "데이터가 이미 존재하여" in msg and not overwrite:
            gl.socketio.emit('toast', {'message': "서버에 데이터가 있어 업로드를 건너뛰었습니다."})
        elif success:
            gl.socketio.emit('toast', {'message': f"Google 시트: {msg}"})
        else:
            raise Exception("Google 시트 업로드 실패.")
    except Exception as e:
        logger_instance._log(f"[{crawler_key}] 구글 시트 업로드 중 예외 발생: {e}", 'error', 'G-SHEETS')
        gl.socketio.emit('toast', {'message': f"Google 시트 내보내기 실패: {e}", 'is_error': True})


def upload_config_to_gsheet_background(data_map):
    from web_lib.bg_tasks import get_crawler  # noqa — 순환 회피
    if not gl.gsheet_write_enabled:
        return
    logger_instance = get_crawler()
    try:
        handler = get_handler(log_func=lambda m, l, t: None)
        if handler.gc:
            success, msg = handler.upload_multiple_dataframes(CONFIG_SHEET_URL, data_map)
            if success:
                logger_instance._log(f"설정 저장 완료: {msg}", 'system', 'CONFIG')
            else:
                logger_instance._log(f"설정 저장 실패: {msg}", 'error', 'CONFIG')
    except Exception as e:
        logger_instance._log(f"설정 업로드 오류: {e}", 'error', 'CONFIG')
