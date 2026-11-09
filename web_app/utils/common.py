import os
import time
import logging
import functools
from datetime import datetime as dt_class, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import pandas as pd


# [v6.6.281] 네트워크 호출용 재시도 데코레이터 (B5).
# tenacity 외부 의존성 없이 최소 구현. exponential backoff + 로깅.
#
# 사용 예:
#   from utils.common import with_retry
#   @with_retry(max_attempts=3, backoff_base=2.0)
#   def fetch_api(url):
#       return requests.get(url, timeout=10)
def with_retry(max_attempts=3, backoff_base=2.0, backoff_cap=30.0, retry_on=(Exception,), logger=None):
    """네트워크/외부 API 호출에 지수 백오프 재시도 적용.
    Args:
        max_attempts: 최대 시도 횟수 (기본 3).
        backoff_base: 지수 베이스 (2.0 = 1s, 2s, 4s, 8s ...).
        backoff_cap: 최대 대기 초 (기본 30).
        retry_on: 재시도 대상 예외 튜플.
        logger: 로깅용. None 이면 모듈 logger 사용.
    """
    _log = logger or logging.getLogger(__name__)
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except retry_on as e:
                    last_exc = e
                    if attempt >= max_attempts:
                        _log.warning(f"[retry] {fn.__name__} 최종 실패 ({attempt}/{max_attempts}): {e}")
                        raise
                    wait = min(backoff_base ** (attempt - 1), backoff_cap)
                    _log.info(f"[retry] {fn.__name__} 실패 ({attempt}/{max_attempts}), {wait:.1f}s 후 재시도: {e}")
                    time.sleep(wait)
            # 이론상 도달 불가 (위의 raise 에서 return 됨)
            raise last_exc  # pragma: no cover
        return wrapper
    return decorator

# [v6.6.484] kill_processes_by_cmdline() 제거 — EDR/Falcon 이 PowerShell
# Get-CimInstance Win32_Process + Stop-Process 패턴과 wmic process kill /
# taskkill /F /PID 패턴을 격리시키는 사고로 인해 외부 프로세스 종료 헬퍼를
# 통째로 제거. 좀비 chrome.exe / chromedriver.exe 정리는 OS / 운영자 수동 처리에 위임.


# .env 파일 로드 (프로젝트 루트 기준)
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_project_root, '.env'))

def get_kakao_credentials(account='default'):
    """환경변수에서 Kakao 자격증명을 로드합니다.
    account='default': KAKAO_ID/PW (개인 계정)
    account='monitor': KAKAO_MONITOR_ID/PW (모니터링 계정)
    """
    if account == 'monitor':
        return os.environ.get('KAKAO_MONITOR_ID', ''), os.environ.get('KAKAO_MONITOR_PW', '')
    return os.environ.get('KAKAO_ID', ''), os.environ.get('KAKAO_PW', '')

# 공통 구글 시트 URL 상수 모음
# [v6.7.745] 시트 ID 3종은 utils/private_config.py 한 자리 - 여기서는 기존 이름을 그대로 다시 내보낸다
from private_config import AI_ANALYSIS_SHEET_URL, CONFIG_SHEET_URL, CONTENTS_SHEET_URL  # noqa: F401

# COMMUNITY_SHEET_URL_OVERRIDE: truthy 일 때만 동적값(crawler.settings['URL_COMMUNITY']) 무시하고 강제 사용.
# None 이면 기존 동적 흐름 (web_lib/bg/scheduler.py + web_lib/bg/export.py 영향).
COMMUNITY_SHEET_URL_OVERRIDE = None

# 시트 이름 맵핑 딕셔너리
SHEET_NAME_MAP = {
    "ODIN_GLOBAL": "Odin_global",
    "ACCOUNT_TRADE": "Account_trade",
    "JASA_GAMES": "Jasa",
    "TASA_GAMES": "Tasa",
    "COMMUNITY": "Community",
    "COMMENTS": "Comments"
}

def get_yesterday_str(fmt="%Y-%m-%d"):
    """어제 날짜를 지정된 포맷의 문자열로 반환합니다. (KST 기준)"""
    kst = ZoneInfo("Asia/Seoul")
    return (dt_class.now(kst) - timedelta(days=1)).strftime(fmt)

def format_iso_to_24h(iso_string):
    """ISO 포맷 문자열을 읽기 쉬운 24시간제 문자열로 포맷팅합니다."""
    if not iso_string: 
        return None
    try:
        return dt_class.fromisoformat(iso_string).strftime('%Y. %m. %d. %H:%M:%S')
    except (ValueError, TypeError):
        return iso_string

def normalize_timestamp(ts):
    """타임스탬프 문자열의 시:분:초를 zero-fill 정규화합니다.
    예: '2026-03-22 9:53:37' -> '2026-03-22 09:53:37'"""
    if not ts:
        return "0000-00-00 00:00:00"
    ts_str = str(ts)
    if ts_str in ("N/A", "N\\A"):
        return "0000-00-00 00:00:00"
    parts = ts_str.split(' ')
    if len(parts) != 2:
        return ts_str
    d, t = parts
    t_parts = t.split(':')
    if len(t_parts) == 3:
        return f"{d} {t_parts[0].zfill(2)}:{t_parts[1].zfill(2)}:{t_parts[2].zfill(2)}"
    return ts_str

def parse_hhmm_to_dt(d_str):
    """HH:MM 형식 또는 일반 날짜 문자열을 KST datetime으로 파싱합니다.
    HH:MM 형식의 경우 오늘 날짜를 기준으로 변환하며, 미래 시간이면 어제로 보정합니다."""
    try:
        d_str = str(d_str).strip()
        if ':' in d_str and len(d_str) <= 5:
            now = dt_class.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)
            parsed = dt_class.strptime(d_str, '%H:%M').replace(year=now.year, month=now.month, day=now.day)
            if parsed > now:
                parsed -= timedelta(days=1)
            return parsed
        return pd.to_datetime(d_str)
    except Exception:
        return pd.NaT


def prepare_community_upload_df(data_list):
    """커뮤니티 게시글 데이터를 Google Sheets 업로드용 DataFrame으로 변환합니다.
    필수 컬럼 보장, 수식 인젝션 방지, 날짜 파싱, 정렬, date_group 컬럼 추가를 수행합니다."""
    df = pd.DataFrame(data_list)
    for col in ['game', 'source', 'timestamp', 'date', 'title', 'link', 'board_name', 'content']:
        if col not in df.columns:
            df[col] = ""
    for col in df.columns:
        df[col] = df[col].apply(
            lambda x: "'" + str(x) if isinstance(x, str) and str(x).startswith(('=', '+', '-', '@')) else x
        )

    if 'timestamp' in df.columns:
        df['dt'] = pd.to_datetime(df['timestamp'], errors='coerce')
    elif 'date' in df.columns:
        df['dt'] = pd.to_datetime(df['date'], errors='coerce')
    else:
        df['dt'] = pd.NaT

    mask_nat = df['dt'].isna()
    if 'date' in df.columns and mask_nat.any():
        df.loc[mask_nat, 'dt'] = df.loc[mask_nat, 'date'].apply(parse_hhmm_to_dt)

    now_kst_naive = dt_class.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)
    df['dt'] = df['dt'].fillna(now_kst_naive)
    df['dt'] = pd.to_datetime(df['dt'], errors='coerce')
    df.loc[df['dt'].dt.year < 2020, 'dt'] = pd.NaT
    df['dt'] = df['dt'].fillna(now_kst_naive)
    df = df.sort_values(by='dt', ascending=False)
    df['date'] = df['dt'].dt.strftime('%H:%M')
    df['date_group'] = df['dt'].dt.strftime('%Y-%m-%d')
    return df


def finalize_community_group_df(group):
    """업로드 직전 커뮤니티 그룹 DataFrame을 최종 가공합니다.
    컬럼명 정규화, 순서 재정렬, 타임스탬프 포맷팅, 리스트→문자열 변환을 수행합니다."""
    group = group.rename(columns={'cafe': 'game', 'type': 'source'})
    desired_order = ['game', 'source', 'detected_keywords', 'title', 'content', 'board_name', 'date', 'timestamp', 'link', 'is_super_keyword']
    for col in desired_order:
        if col not in group.columns:
            group[col] = None
    final_df = group[desired_order].copy()
    if 'timestamp' in final_df.columns:
        final_df['timestamp'] = final_df['timestamp'].astype(str).str.replace('T', ' ')
    for col in final_df.columns:
        if final_df[col].apply(lambda x: isinstance(x, list)).any():
            final_df[col] = final_df[col].apply(lambda x: ', '.join(map(str, x)) if isinstance(x, list) else x)
    return final_df


def install_crash_hook(app_name, crash_dump_path=None):
    """앱 크래시 시 카카오워크로 알림 전송 + 이전 크래시 덤프 감지.
    1) sys.excepthook 등록 — Python 예외 발생 시 알림
    2) 이전 crash_dump.txt가 있으면 내용 전송 후 초기화
    """
    import sys
    import traceback

    if crash_dump_path is None:
        crash_dump_path = os.path.join(_project_root, 'crash_dump.txt')

    def _send(error_text):
        try:
            from notification_manager import NotificationManager
            nm = NotificationManager()
            nm.send_crash_report(app_name, error_text)
        except Exception:
            pass  # 알림 전송 실패해도 앱 종료를 막지 않음

    # 1) Python excepthook — 미처리 예외 잡기
    _original_hook = sys.excepthook
    def _crash_hook(exc_type, exc_value, exc_tb):
        error_text = ''.join(traceback.format_exception(exc_type, exc_value, exc_tb))
        _send(error_text)
        _original_hook(exc_type, exc_value, exc_tb)
    sys.excepthook = _crash_hook

    # 2) 이전 크래시 덤프 감지 — 앱 시작 시 확인
    try:
        if os.path.exists(crash_dump_path):
            with open(crash_dump_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read().strip()
            if content:
                _send(f"[이전 크래시 감지]\n{content}")
                with open(crash_dump_path, 'w', encoding='utf-8') as f:
                    f.write('')  # 전송 후 초기화
    except Exception:
        pass


def setup_logging(log_name: str = None):
    """웹, 터미널, GUI 공통의 기본 로깅 설정.

    log_name 이 주어지면 `web_app/logs/{log_name}.log` 에 파일로도 기록하며,
    자정마다 회전되어 이전 날짜는 `{YYYY-MM-DD}_{log_name}.log` 형태로 보관됩니다.
    보관 기간은 무제한(backupCount=0)이며, 자동 삭제 없음.
    """
    handlers = [logging.StreamHandler()]

    if log_name:
        try:
            import os
            import sys
            from pathlib import Path
            from logging.handlers import TimedRotatingFileHandler

            # PyInstaller onefile 대응: frozen 상태에서는 .exe 가 있는 폴더 기준
            if getattr(sys, 'frozen', False):
                base_dir = Path(sys.executable).resolve().parent
            else:
                # 개발 실행: utils/common.py -> web_app/ 기준
                base_dir = Path(__file__).resolve().parent.parent
            log_dir = base_dir / 'logs'
            log_dir.mkdir(exist_ok=True)

            file_handler = TimedRotatingFileHandler(
                filename=str(log_dir / f'{log_name}.log'),
                when='midnight',
                backupCount=0,  # 자동 삭제 없음
                encoding='utf-8',
                delay=True,
            )

            # 회전 시 기본 파일명 "terminal.log.2026-04-12" 를
            # "2026-04-12_terminal.log" 로 재명명
            def _date_prefix_namer(default_name):
                dirname, basename = os.path.split(default_name)
                if '.' in basename:
                    idx = basename.rfind('.')
                    stem = basename[:idx]
                    suffix = basename[idx + 1:]
                    # 날짜 포맷(YYYY-MM-DD)인 경우에만 변환
                    if len(suffix) == 10 and suffix[4] == '-' and suffix[7] == '-':
                        return os.path.join(dirname, f"{suffix}_{stem}")
                return default_name

            file_handler.namer = _date_prefix_namer
            handlers.append(file_handler)
        except Exception as e:
            # 파일 핸들러 실패해도 콘솔 로깅은 유지
            print(f">>> [setup_logging] 파일 핸들러 초기화 실패: {e}")

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s [%(levelname)s] [%(name)s] %(message)s',
                        handlers=handlers,
                        force=True)
    logging.getLogger('apscheduler').setLevel(logging.WARNING)
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
