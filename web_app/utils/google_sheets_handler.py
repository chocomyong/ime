"""
[컨텍스트 엔지니어링: 데이터 흐름 요약]
1. 역할: 구글 시트 API 중앙 인터페이스. 인증, 할당량 관리(계정 전환), 배치 I/O를 처리합니다.
2. 입력 데이터 (Inbound):
   - main_gui.py / gui_workers.py: 업로드할 DataFrame 또는 읽어올 워크시트 이름.
   - 인증 정보 (JSON): 구글 클라우드 프로젝트의 서비스 계정 키 (service_accounts.py 경유).
3. 출력 데이터 (Outbound):
   - Google Sheets API: REST 요청 (배치 업데이트, 읽기, 스타일 포맷팅, 워크시트 생성).
   - main_gui.py / gui_workers.py: 가공된 DataFrame, 레코드 리스트 또는 성공/오류 상태 메시지.
4. 핵심 데이터 구조 (Schemas):
   - 시트 데이터 (커뮤니티 게시글): [game, source, detected_keywords, title, content, board_name, date, timestamp, link, is_super_keyword]
   - 시트 데이터 (설정): [Key, Value] (Config_Settings 탭 기준)
   - 시트 데이터 (CCU): [Server_Name, Before_CCU, Current_CCU, Change_Prev, CCU_5min_Ago, Change_5m]
"""

import gspread
from gspread_dataframe import set_with_dataframe, get_as_dataframe
import pandas as pd
import os
import sys
from gspread.utils import rowcol_to_a1, a1_range_to_grid_range
import time
import threading
import random
import json
import ssl
import requests
import datetime # 날짜 기록용
from service_accounts import SERVICE_ACCOUNTS # 공통 서비스 계정 모듈 임포트


# 데이터 허브 시트 URL (설정 시트와 동일)
from private_config import CONFIG_SHEET_URL, SHARE_WITH_EMAIL  # [v6.7.745] 비공개 값 한 자리
HUB_SHEET_URL = CONFIG_SHEET_URL

# 설정 시트 URL (Config_Selectors 로드용)
# (CONFIG_SHEET_URL - 위 import)

# 새로 생성된 시트를 공유할 사용자 이메일 주소
# (SHARE_WITH_EMAIL - 위 import)

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive.file'
]

# ====================================================================
# 전역 상수
# ====================================================================
SPREADSHEET_CACHE_TTL = 120       # [v6.6.223] 10분 → 2분 단축 (Keep-Alive SSL 소켓 장수명으로 인한 TLS 재협상 중 AV 회피)
SELECTOR_CACHE_TTL = 300          # 선택자 캐시 유효 시간 (초)
MAX_API_RETRIES = 10              # API 재시도 최대 횟수
MAX_RETRY_WAIT = 30               # 재시도 최대 대기 시간 (초)

# ====================================================================
# 전역 스레드 동기화 및 상태 관리
# ====================================================================
_gc_lock = threading.RLock()                    # 인증/계정 전환 보호
# [v6.5.61] RLock 전환 — 재진입 안전 (메서드 간 내부 호출 시 데드락 방지)
_network_lock = threading.RLock()               # SSL 동시 연결 제한

# Spreadsheet 개체 캐싱
_spreadsheet_cache = {}                         # {url: (spreadsheet_obj, timestamp)}
_spreadsheet_cache_lock = threading.Lock()

# [v6.5.146] 워크시트 이름 목록 캐싱 (worksheets() API 호출 절감)
_worksheet_names_cache = {}  # {url: (set_of_names, timestamp)}
_worksheet_names_cache_lock = threading.Lock()
_WORKSHEET_NAMES_CACHE_TTL = 300  # 5분

# 라운드-로빈 계정 관리
_request_counter = 0
_current_account_index = random.randint(0, max(0, len(SERVICE_ACCOUNTS) - 1)) if SERVICE_ACCOUNTS else 0


class QuotaTracker:
    """API 쿼터 사용량 추적 (스레드 안전)"""
    def __init__(self):
        self._lock = threading.Lock()
        self.total_calls = 0
        self.module_calls = {}      # {tag: count}
        self.error_429_count = 0
        self.start_time = time.time()

    def record_call(self, tag='General'):
        with self._lock:
            self.total_calls += 1
            self.module_calls[tag] = self.module_calls.get(tag, 0) + 1

    def record_429(self):
        with self._lock:
            self.error_429_count += 1

    def get_summary(self):
        with self._lock:
            uptime_m = (time.time() - self.start_time) / 60
            avg_rate = self.total_calls / uptime_m if uptime_m > 0 else 0
            return {
                'total': self.total_calls,
                'avg_rate': avg_rate,
                'error_429': self.error_429_count,
                'top_modules': sorted(self.module_calls.items(), key=lambda x: x[1], reverse=True)[:10]
            }

    def get_display_info(self):
        with self._lock:
            uptime_m = (time.time() - self.start_time) / 60
            avg_rate = self.total_calls / uptime_m if uptime_m > 0 else 0
            return f"{self.total_calls} ({avg_rate:.1f}/m, 429: {self.error_429_count})"

_quota = QuotaTracker()

class GoogleSheetsHandler:
    """
    [클래스 역할]
    Google Sheets API와의 모든 통신을 전담하는 중앙 래퍼(Wrapper) 클래스임.
    
    [핵심 최적화 및 방어 기법]
    - 프로액티브 로드 밸런싱: 싱글톤 락(`_gc_lock`)을 활용하여 요청 시마다 여러 서비스 계정을 순환(Round-Robin)하여, 특정 계정에 API 할당량(Quota) 초과가 발생하는 것을 원천 차단함.
    - 지수 백오프(Exponential Backoff): 429(Too Many Requests) 및 5xx 에러 발생 시 대기 시간을 2배씩 늘려가며 재시도하여 엔터프라이즈급 네트워크 장애 허용성(Fault Tolerance)을 보장함.
    - 배치 업데이트(Batch Processing): 다중 시트 읽기/쓰기 및 서식(Style) 일괄 적용을 단일 API 호출로 묶어 네트워크 오버헤드를 극적으로 최소화함.
    - 스레드 안전성: 멀티스레딩 환경(Flask, PySide6)에서 계정 전환 시 충돌을 막기 위해 `threading.RLock()`을 적용함.
    """
    def __init__(self, log_func=None):
        self.gc = None
        self.log_func = log_func
        with _gc_lock:
            self._authenticate()
        
        # [v6.4.31] 선택자 설정 캐시
        self._selectors_cache = {} 
        self._selectors_cache_lock = threading.Lock()

    def _log(self, message, level='info', tag='G-SHEETS'):
        if self.log_func:
            self.log_func(message, level, 'DATABASE')
        else:
            print(f"[{level.upper()}] [{tag}] {message}")

    @staticmethod
    def _harden_session(gc):
        # [v6.6.337] requests.Session.trust_env = False — get_netrc_auth() 경로 차단.
        # share_desktop faulthandler 덤프에서 `requests/utils.py:223 get_netrc_auth` →
        # `<frozen genericpath>:19 exists` 가 매 HTTP 요청마다 호출되며 Access Violation 의
        # 유력 지점으로 관측됨. netrc/HTTP_PROXY 환경변수 의존 기능은 이 앱에서 쓰지 않음.
        if gc is None:
            return
        try:
            http_client = getattr(gc, 'http_client', None)
            if http_client is not None:
                sess = getattr(http_client, 'session', None)
                if sess is not None:
                    sess.trust_env = False
            sess2 = getattr(gc, 'session', None)
            if sess2 is not None:
                sess2.trust_env = False
        except Exception:
            pass

    def _authenticate(self):
        global _current_account_index, _request_counter
        with _gc_lock:
            _request_counter += 1
            if len(SERVICE_ACCOUNTS) > 1:
                _current_account_index = (_request_counter) % len(SERVICE_ACCOUNTS)
            try:
                creds = SERVICE_ACCOUNTS[_current_account_index]
                self.gc = gspread.service_account_from_dict(creds)
                self._harden_session(self.gc)
            except Exception as e:
                self._log(f"데이터베이스 연결 실패: {e}", 'error')
                self.gc = None

    def switch_account(self):
        self._log(f"API 쿼타 오류 감지. 다음 계정으로 연결 전환 시도...", 'warning')
        global _request_counter, _current_account_index
        with _gc_lock:
            _request_counter += 1
            if len(SERVICE_ACCOUNTS) > 1:
                _current_account_index = (_request_counter) % len(SERVICE_ACCOUNTS)
            new_creds = SERVICE_ACCOUNTS[_current_account_index]
            new_client = gspread.service_account_from_dict(new_creds)
            self._harden_session(new_client)
            if self.gc:
                for k in ['auth', 'session', 'http_client']:
                    if hasattr(new_client, k): setattr(self.gc, k, getattr(new_client, k))
                self._harden_session(self.gc)
            else:
                self.gc = new_client
            with _spreadsheet_cache_lock:
                _spreadsheet_cache.clear()

    def get_spreadsheet(self, url):
        with _spreadsheet_cache_lock:
            if url in _spreadsheet_cache:
                sheet, ts = _spreadsheet_cache[url]
                if time.time() - ts < SPREADSHEET_CACHE_TTL:
                    return sheet
        sheet = self._retry_on_quota_error(self.gc.open_by_url, url)
        with _spreadsheet_cache_lock:
            _spreadsheet_cache[url] = (sheet, time.time())
        return sheet

    def _get_cached_worksheet_names(self, spreadsheet, url):
        """[v6.5.146] 워크시트 이름 목록을 5분 캐싱하여 매 폴링마다 worksheets() API 호출 절감."""
        with _worksheet_names_cache_lock:
            if url in _worksheet_names_cache:
                names, ts = _worksheet_names_cache[url]
                if time.time() - ts < _WORKSHEET_NAMES_CACHE_TTL:
                    return names
        names = {ws.title for ws in self._retry_on_quota_error(spreadsheet.worksheets, tag='BATCH_READ_META')}
        with _worksheet_names_cache_lock:
            _worksheet_names_cache[url] = (names, time.time())
        return names

    def _invalidate_worksheet_names_cache(self, url):
        """시트 생성/삭제 후 워크시트 이름 캐시 무효화."""
        with _worksheet_names_cache_lock:
            _worksheet_names_cache.pop(url, None)

    def _retry_on_quota_error(self, func, *args, **kwargs):
        tag = kwargs.pop('tag', 'General')
        _quota.record_call(tag)

        max_retries = MAX_API_RETRIES
        status_code = -1
        for attempt in range(max_retries):
            try:
                return func(*args, **kwargs)
            except gspread.exceptions.APIError as e:
                status_code = getattr(e, 'code', None)
                if status_code is None and hasattr(e, 'response'):
                    status_code = getattr(e.response, 'status_code', None)
                
                if status_code == 429 or (status_code and 500 <= status_code < 600):
                    if status_code == 429:
                        _quota.record_429()
                        error_type = "API 할당량 초과"
                        self.switch_account()

                        # [추가] 429 에러 발생 시 현재까지의 통계를 요약 출력
                        self._log_quota_summary()
                    else:
                        error_type = "구글 서버 일시 장애"

                    wait_time = min(MAX_RETRY_WAIT, (3 * (2 ** attempt)) + random.uniform(1, 3))
                    self._log(f"[{error_type}({status_code})] 구글 측 응답 지연. {wait_time:.1f}초 후 재시도 ({attempt+1}/{max_retries})...", 'warning')
                    time.sleep(wait_time)
                elif status_code in (401, 403):
                    # [v6.7.76] 인증 토큰 만료 / 권한 거부 — 장기 가동 시 silent fail 의 root cause.
                    # 기존엔 raise 후 caller swallow 로 gc 살아있지만 모든 API 가 silent fail.
                    # 재인증(self.gc 재생성) + spreadsheet 캐시 무효화 + 재시도.
                    self._log(f"[인증 오류({status_code})] 토큰 만료/세션 깨짐 감지 — 재인증 시도 ({attempt+1}/{max_retries})...", 'warning')
                    try:
                        self._authenticate()
                        with _spreadsheet_cache_lock:
                            _spreadsheet_cache.clear()
                    except Exception as reauth_e:
                        self._log(f"재인증 실패: {reauth_e}", 'error')
                    wait_time = min(MAX_RETRY_WAIT, (2 * (2 ** attempt)) + random.uniform(0.5, 1.5))
                    time.sleep(wait_time)
                else:
                    raise e
            except requests.exceptions.RequestException as e:
                wait_time = min(30, (3 * (2 ** attempt)) + random.uniform(1, 3))
                self._log(f"네트워크 연결 오류({e}). {wait_time:.1f}초 후 재시도 ({attempt+1}/{max_retries})...", 'warning')
                time.sleep(wait_time)
            except (ssl.SSLError, ConnectionError, OSError) as e:
                # [v6.6.223] SSL/소켓 레벨 예외 포착 — 이전에는 raw로 올라와 Access Violation과 구별 불가
                # 캐시된 Spreadsheet의 Keep-Alive 소켓이 끊겼을 때 재시도로 새 연결 유도
                with _spreadsheet_cache_lock:
                    _spreadsheet_cache.clear()
                wait_time = min(MAX_RETRY_WAIT, (3 * (2 ** attempt)) + random.uniform(1, 3))
                self._log(f"SSL/소켓 오류({type(e).__name__}: {e}). 캐시 무효화 후 {wait_time:.1f}초 뒤 재시도 ({attempt+1}/{max_retries})...", 'warning')
                time.sleep(wait_time)
        raise Exception(f"API 재시도 횟수 초과 (마지막 상태 코드: {status_code})")

    def _log_quota_summary(self):
        """현재까지의 Quota 사용 통계를 로그로 출력합니다."""
        summary = _quota.get_summary()
        self._log(f"--- Google Sheets Quota 통계 ({_quota.get_display_info()}) ---", 'system')
        for mod, count in summary['top_modules']:
            pct = count / summary['total'] * 100 if summary['total'] > 0 else 0
            self._log(f"  - [{mod}]: {count}회 ({pct:.1f}%)", 'system')
        self._log("------------------------------------------", 'system')

    def get_quota_display_info(self):
        """UI에 표시할 요약 정보를 반환합니다."""
        return _quota.get_display_info()

    def apply_conditional_formatting(self, spreadsheet_url, worksheet_name):
        if not self.gc: return False
        # [v6.6.189] PATTERN-12: _network_lock 추가 — SSL Access Violation 방지
        with _network_lock:
            try:
                spreadsheet = self._retry_on_quota_error(self.gc.open_by_url, spreadsheet_url, tag='STYLE')
                worksheet = self._retry_on_quota_error(spreadsheet.worksheet, worksheet_name, tag='STYLE')
                self._log(f"'{worksheet_name}' 시트에 조건부 서식 및 기본 스타일 적용 시작...", 'info')
                self._apply_basic_style(worksheet)
                self._apply_conditional_formatting(worksheet)
                return True
            except Exception as e:
                self._log(f"조건부 서식 적용 실패: {e}", 'error')
                return False

    def upload_dataframe(self, spreadsheet_url, worksheet_name, df, overwrite=True, hide_sheet=True, apply_style=False):
        if not self.gc: return False, "인증 실패"
        if df.empty: return True, "저장할 데이터 없음 (건너뜀)"
        with _network_lock:
          try:
            spreadsheet = self.get_spreadsheet(spreadsheet_url)
            worksheet = None
            final_worksheet_name = worksheet_name
            created_new = False
            # [v6.4.33] 최적화: 시트 목록을 한 번에 가져와서 메모리에서 검색 (API 호출 급감)
            all_worksheets = self._retry_on_quota_error(spreadsheet.worksheets, tag='UPLOAD_META')
            all_titles = [w.title for w in all_worksheets]
            title_to_ws = {w.title: w for w in all_worksheets}

            if not overwrite:
                counter = 1
                while True:
                    current_name = worksheet_name if counter == 1 else f"{worksheet_name}{counter}"
                    if current_name in all_titles:
                        ws = title_to_ws[current_name]
                        # 시트가 존재하면 내용이 있는지 확인 (성능 위해 A1 값만 1회 확인)
                        if self._retry_on_quota_error(lambda: ws.acell('A1').value, tag='UPLOAD_CHECK'):
                            counter += 1
                            continue
                        else:
                            worksheet = ws
                            final_worksheet_name = current_name
                            break
                    else:
                        # 시트가 없으면 새로 생성
                        worksheet = self._retry_on_quota_error(spreadsheet.add_worksheet, title=current_name, rows=1, cols=1, tag='UPLOAD_INIT')
                        final_worksheet_name = current_name
                        created_new = True
                        break
            else:
                if worksheet_name in all_titles:
                    worksheet = title_to_ws[worksheet_name]
                    if not ("Community" in worksheet_name or "Comments" in worksheet_name):
                        self._retry_on_quota_error(spreadsheet.del_worksheet, worksheet, tag='UPLOAD_INIT')
                        worksheet = self._retry_on_quota_error(spreadsheet.add_worksheet, title=worksheet_name, rows=1, cols=1, tag='UPLOAD_INIT')
                        created_new = True
                else:
                    worksheet = self._retry_on_quota_error(spreadsheet.add_worksheet, title=worksheet_name, rows=1, cols=1, tag='UPLOAD_INIT')
                    created_new = True
            
            self._retry_on_quota_error(set_with_dataframe, worksheet, df, include_index=False, include_column_header=True, resize=True, tag='UPLOAD_DATA')
            if created_new:
                self._invalidate_worksheet_names_cache(spreadsheet_url)
            # [v6.5.149] 조건부 서식은 새 시트 생성 시 한 번만 적용 (중복 누적 방지)
            # open-ended GridRange(endRowIndex 없음)로 적용된 규칙은 행 증가와 무관하게 영구 적용됨
            if apply_style and created_new:
                self._apply_basic_style(worksheet, df)
                self._apply_conditional_formatting(worksheet)
            if hide_sheet:
                self._retry_on_quota_error(worksheet.hide, tag='UPLOAD_HIDE')
            return True, f"저장 완료 ({final_worksheet_name})"
          except Exception as e:
            self._log(f"데이터 저장 중 오류 발생: {e}", 'error')
            return False, f"저장 오류: {e}"

    def upload_multiple_dataframes(self, spreadsheet_url, data_map, overwrite=True):
        if not self.gc: return False, "인증 실패"
        with _network_lock:
          try:
            spreadsheet = self._retry_on_quota_error(self.gc.open_by_url, spreadsheet_url, tag='BATCH_UPLOAD')
            value_ranges, requests, processed_sheets = [], [], []
            for sheet_name, df in data_map.items():
                try: ws = self._retry_on_quota_error(spreadsheet.worksheet, sheet_name, tag='BATCH_UPLOAD')
                except gspread.exceptions.WorksheetNotFound: ws = self._retry_on_quota_error(spreadsheet.add_worksheet, title=sheet_name, rows=100, cols=10, tag='BATCH_UPLOAD')
                df_clean = df.fillna("")
                values = [df_clean.columns.tolist()] + df_clean.values.tolist()
                value_ranges.append({'range': f"'{sheet_name}'!A1", 'values': values})
                sheet_id = ws.id
                needed_rows, needed_cols = max(len(df) + 20, 100), len(df.columns)
                requests.append({"updateSheetProperties": {"properties": {"sheetId": sheet_id, "gridProperties": {"rowCount": needed_rows, "columnCount": needed_cols}}, "fields": "gridProperties(rowCount,columnCount)"}})
                requests.append({"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": 1, "endIndex": needed_rows}, "properties": {"pixelSize": 25}, "fields": "pixelSize"}})
                requests.append({"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1}, "cell": {"userEnteredFormat": {"backgroundColor": {"red": 0.2, "green": 0.2, "blue": 0.2}, "horizontalAlignment": "CENTER", "textFormat": {"foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}, "bold": True}}}, "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)"}})
                processed_sheets.append(sheet_name)
            if value_ranges: self._retry_on_quota_error(spreadsheet.values_batch_update, {'valueInputOption': 'USER_ENTERED', 'data': value_ranges}, tag='BATCH_UPLOAD')
            if requests: self._retry_on_quota_error(spreadsheet.batch_update, {'requests': requests}, tag='BATCH_UPLOAD')
            return True, f"일괄 저장 완료 ({len(processed_sheets)}개 테이블)"
          except Exception as e:
            self._log(f"일괄 저장 중 오류: {e}", 'error')
            return False, str(e)

    def _apply_basic_style(self, worksheet, df=None):
        try:
            requests, sheet_id = [], worksheet.id
            row_count = len(df) + 1 if df is not None else 1000
            col_count = len(df.columns) if df is not None else 8
            requests.append({"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1}, "cell": {"userEnteredFormat": {"backgroundColor": {"red": 0.2, "green": 0.2, "blue": 0.2}, "horizontalAlignment": "CENTER", "textFormat": {"foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}, "bold": True}}}, "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)"}})
            requests.append({"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 3}, "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER", "textFormat": {"bold": True}}}, "fields": "userEnteredFormat(horizontalAlignment,textFormat.bold)"}})
            requests.append({"updateSheetProperties": {"properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}}, "fields": "gridProperties.frozenRowCount"}})
            requests.append({"setBasicFilter": {"filter": {"range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": row_count, "startColumnIndex": 0, "endColumnIndex": col_count}}}})
            col_widths = [(0, 120), (1, 60), (2, 110), (3, 300), (4, 100), (5, 120), (6, 50), (7, 140), (8, 100)]
            for col_idx, width in col_widths:
                requests.append({"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": col_idx, "endIndex": col_idx + 1}, "properties": {"pixelSize": width}, "fields": "pixelSize"}})
            requests.append({"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 1, "startColumnIndex": 6, "endColumnIndex": 7}, "cell": {"userEnteredFormat": {"numberFormat": {"type": "TIME", "pattern": "hh:mm"}}}, "fields": "userEnteredFormat.numberFormat"}})
            requests.append({"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 1, "startColumnIndex": 7, "endColumnIndex": 8}, "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE_TIME", "pattern": "yyyy-mm-dd hh:mm:ss"}}}, "fields": "userEnteredFormat.numberFormat"}})
            requests.append({"addBanding": {"bandedRange": {"range": {"sheetId": sheet_id, "startRowIndex": 1}, "rowProperties": {"firstBandColor": {"red": 1.0, "green": 1.0, "blue": 1.0}, "secondBandColor": {"red": 0.96, "green": 0.96, "blue": 0.96}}}}})
            if requests:
                try: self._retry_on_quota_error(worksheet.spreadsheet.batch_update, {"requests": requests}, tag='STYLE')
                except Exception as e:
                    if "banding" in str(e).lower() or "overlaps" in str(e).lower():
                        requests.pop(); self._retry_on_quota_error(worksheet.spreadsheet.batch_update, {"requests": requests}, tag='STYLE')
                    else: raise e
        except Exception as e: self._log(f"스타일 적용 중 오류 (무시됨): {e}", 'warning')

    def _apply_conditional_formatting(self, worksheet):
        """[v6.5.149] 새 시트 생성 시 한 번만 호출. open-ended GridRange로 적용되어 행 증가와 무관하게 영구 적용."""
        try:
            sid = worksheet.id

            # [v6.6.144] 조건부 서식 중복 방지: 이미 규칙이 존재하면 스킵
            try:
                url = f"https://sheets.googleapis.com/v4/spreadsheets/{worksheet.spreadsheet.id}"
                params = {'fields': 'sheets(properties(sheetId),conditionalFormats)'}
                if hasattr(self.gc, 'request'):
                    res = self.gc.request('get', url, params=params)
                else:
                    res = self.gc.session.request('get', url, params=params)
                data = res.json()
                target = next((s for s in data.get('sheets', []) if s['properties']['sheetId'] == sid), None)
                if target and target.get('conditionalFormats'):
                    self._log(f"조건부 서식이 이미 {len(target['conditionalFormats'])}개 존재 — 중복 적용 스킵", 'system')
                    return
            except Exception as e:
                self._log(f"조건부 서식 중복 체크 중 오류 (무시하고 계속): {e}", 'warning')

            def col_range(start_row, col):
                """start_row행부터 col열(1-based) 전체를 커버하는 open-ended GridRange"""
                return {"sheetId": sid, "startRowIndex": start_row - 1,
                        "startColumnIndex": col - 1, "endColumnIndex": col}

            rules = [
                {'ranges': [col_range(2, 2)], 'booleanRule': {'condition': {'type': 'TEXT_EQ', 'values': [{'userEnteredValue': 'DAUM'}]}, 'format': {'backgroundColor': {'red': 0.968, 'green': 0.902, 'blue': 0.0}}}},
                {'ranges': [col_range(2, 2)], 'booleanRule': {'condition': {'type': 'TEXT_EQ', 'values': [{'userEnteredValue': 'DC'}]}, 'format': {'backgroundColor': {'red': 0.357, 'green': 0.545, 'blue': 0.847}}}},
                {'ranges': [col_range(2, 3)], 'booleanRule': {'condition': {'type': 'CUSTOM_FORMULA', 'values': [{'userEnteredValue': '=$I2=TRUE'}]}, 'format': {'backgroundColor': {'red': 0.992, 'green': 0.847, 'blue': 0.208}, 'textFormat': {'bold': True}}}},
                {'ranges': [col_range(2, 3)], 'booleanRule': {'condition': {'type': 'CUSTOM_FORMULA', 'values': [{'userEnteredValue': '=AND($I2=FALSE, $C2<>"")'}]}, 'format': {'backgroundColor': {'red': 1.0, 'green': 0.541, 'blue': 0.502}, 'textFormat': {'bold': True}}}}
            ]
            game_colors = {'오딘': {'red': 0.85, 'green': 0.92, 'blue': 1.0}, '아키에이지워': {'red': 0.85, 'green': 1.0, 'blue': 0.85}, '아레스': {'red': 1.0, 'green': 0.85, 'blue': 0.85}, '가디스오더': {'red': 1.0, 'green': 0.95, 'blue': 0.8}, '가디언테일즈': {'red': 0.92, 'green': 0.85, 'blue': 1.0}, '우마무스메': {'red': 1.0, 'green': 0.85, 'blue': 0.95}, '프렌즈타운': {'red': 0.85, 'green': 1.0, 'blue': 1.0}}
            for game, color in game_colors.items(): rules.append({'ranges': [col_range(2, 1)], 'booleanRule': {'condition': {'type': 'TEXT_CONTAINS', 'values': [{'userEnteredValue': game}]}, 'format': {'backgroundColor': color}}})
            requests = [{"addConditionalFormatRule": {"rule": rule, "index": 0}} for rule in rules]
            if requests: self._retry_on_quota_error(worksheet.spreadsheet.batch_update, {'requests': requests}, tag='STYLE')
        except Exception as e: self._log(f"조건부 서식 적용 중 오류: {e}", 'warning')

    def read_dataframe(self, spreadsheet_url, worksheet_name):
        if not self.gc: return None
        # [v6.7.73] F-7 record_sheets_latency hook — 시트 read 응답 시간 측정
        import time as _time
        _t0 = _time.perf_counter()
        with _network_lock:
          try:
            spreadsheet = self.get_spreadsheet(spreadsheet_url)
            try: worksheet = self._retry_on_quota_error(spreadsheet.worksheet, worksheet_name, tag='READ_GET_WS')
            except gspread.exceptions.WorksheetNotFound: return None
            df = self._retry_on_quota_error(get_as_dataframe, worksheet, evaluate_formulas=False, dtype=str, tag='READ_DATA')
            df = df.dropna(how='all').dropna(axis=1, how='all')
            return df
          except Exception as e: self._log(f"데이터 읽기 실패: {e}", 'error'); return None
          finally:
            try:
                from web_lib.bg.health_sampler import record_sheets_latency as _rec
                _rec((_time.perf_counter() - _t0) * 1000.0)
            except Exception:
                pass

    # [v6.7.744] read_values()(탭 raw 값 읽기, v6.7.353~) 삭제 - 유일한 호출처가 지운 bg/alert_overlay_sync.py 였다
    #   (PATTERN-48 - 지운 기능만 쓰던 헬퍼를 남기지 않는다). 되살리려면 git show cb0080cd:web_app/utils/google_sheets_handler.py.

    def read_multiple_worksheets(self, spreadsheet_url, worksheet_names, max_rows=None):
        """[v6.4.71] 다중 워크시트 읽기

        max_rows: 읽을 최대 행 수. None이면 전체(A:Z). 지정 시 A1:Z{max_rows+1} 범위만 읽어
                  대용량 시트 폴링 지연을 방지한다 (헤더 1행 + 데이터 max_rows행).

        [v6.5.146] worksheets() 호출을 5분 캐싱하여 매 폴링마다 API 1회 절감.
        """
        if not self.gc: return {}
        # [v6.7.73] F-7 record_sheets_latency hook — 시트 batch read 응답 시간 측정
        import time as _time
        _t0 = _time.perf_counter()
        with _network_lock:
            try:
                spreadsheet = self.get_spreadsheet(spreadsheet_url)

                # 워크시트 목록을 캐싱하여 반복 API 호출 절감 (5분 TTL)
                existing_sheets = self._get_cached_worksheet_names(spreadsheet, spreadsheet_url)
                valid_names = [name for name in worksheet_names if name in existing_sheets]

                if not valid_names:
                    return {name: pd.DataFrame() for name in worksheet_names}

                result = {name: pd.DataFrame() for name in worksheet_names}
                if max_rows:
                    ranges = [f"'{name}'!A1:Z{max_rows + 1}" for name in valid_names]
                else:
                    ranges = [f"'{name}'!A:Z" for name in valid_names]

                response = self._retry_on_quota_error(spreadsheet.values_batch_get, ranges, tag='BATCH_READ')
                value_ranges = response.get('valueRanges', [])

                for i, val_range in enumerate(value_ranges):
                    name = valid_names[i]
                    values = val_range.get('values', [])
                    if values:
                        try:
                            df = pd.DataFrame(values[1:], columns=values[0])
                            result[name] = df.dropna(how='all').dropna(axis=1, how='all')
                        except Exception:
                            result[name] = pd.DataFrame(values)
                return result
            except Exception as e:
                self._log(f"다중 읽기 실패: {e}", 'error')
                return {name: pd.DataFrame() for name in worksheet_names}
            finally:
                try:
                    from web_lib.bg.health_sampler import record_sheets_latency as _rec
                    _rec((_time.perf_counter() - _t0) * 1000.0)
                except Exception:
                    pass

    def create_spreadsheet(self, title):
        if not self.gc: return None
        with _network_lock:
          try:
            spreadsheet = self.gc.create(title)
            if SHARE_WITH_EMAIL: spreadsheet.share(SHARE_WITH_EMAIL, perm_type='user', role='writer', notify=False)
            return spreadsheet
          except Exception as e: self._log(f"생성 실패: {e}", 'error'); return None

    def get_selectors(self, site_filter=None):
        if not self.gc: return {}, {}
        with self._selectors_cache_lock:
            if site_filter in self._selectors_cache:
                sel, attr, ts = self._selectors_cache[site_filter]
                if time.time() - ts < SELECTOR_CACHE_TTL: return sel, attr
        with _network_lock:
          try:
            spreadsheet = self.get_spreadsheet(CONFIG_SHEET_URL)
            worksheet = self._retry_on_quota_error(spreadsheet.worksheet, "Config_Selectors", tag='CONFIG_READ')
            records = self._retry_on_quota_error(worksheet.get_all_records, tag='CONFIG_READ')
            selectors, attributes = {}, {}
            for row in records:
                site, target, selector, regex = row.get('Site', ''), row.get('Target', ''), row.get('Selector', ''), row.get('Regex', '')
                if site_filter and site != site_filter: continue
                if not target or not selector: continue
                if target not in selectors: selectors[target] = []
                if selector not in selectors[target]: selectors[target].append(selector)
                if regex: attributes[target] = regex
            with self._selectors_cache_lock: self._selectors_cache[site_filter] = (selectors, attributes, time.time())
            return selectors, attributes
          except Exception as e: self._log(f"선택자 로드 실패: {e}", 'error'); return {}, {}

    def update_selector(self, site_name, target, new_selector):
        if not self.gc: return False
        with _network_lock:
          try:
            spreadsheet = self._retry_on_quota_error(self.gc.open_by_url, CONFIG_SHEET_URL, tag='CONFIG_UPDATE')
            worksheet = self._retry_on_quota_error(spreadsheet.worksheet, "Config_Selectors", tag='CONFIG_UPDATE')
            all_values = self._retry_on_quota_error(worksheet.get_all_values, tag='CONFIG_UPDATE')
            if not all_values: return False
            headers = all_values[0]
            try: idx_site, idx_target = headers.index('Site'), headers.index('Target')
            except ValueError: return False
            row_idx = -1
            for i, row in enumerate(all_values[1:]):
                if len(row) > max(idx_site, idx_target) and row[idx_target] == target and row[idx_site] == site_name:
                    row_idx = i + 2; break
            if row_idx != -1:
                now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                updates = [{'range': rowcol_to_a1(row_idx, 3), 'values': [[new_selector]]}, {'range': rowcol_to_a1(row_idx, 6), 'values': [[now]]}]
                self._retry_on_quota_error(worksheet.batch_update, updates, tag='CONFIG_UPDATE')
                return True
            return False
          except Exception as e: self._log(f"업데이트 실패: {e}", 'error'); return False


# [추가] 전역 공유 핸들러 (Singleton Pattern)
_shared_handler = None
_handler_lock = threading.RLock()

def get_handler(log_func=None):
    global _shared_handler
    with _handler_lock:
        if _shared_handler is None: _shared_handler = GoogleSheetsHandler(log_func=log_func)
        elif log_func: _shared_handler.log_func = log_func
        return _shared_handler
