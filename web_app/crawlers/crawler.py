# -*- coding: utf-8 -*-
"""
[INTERFACE] 이 파일의 상세 통신 명세 및 데이터 스키마는 프로젝트 루트의 
[INTERFACE_REGISTRY.md](../INTERFACE_REGISTRY.md)를 반드시 참고하십시오.

[역할]
- 커뮤니티 스크래핑 엔진 (다음 카페, 디시인사이드). 모든 수집 로직의 컨트롤 타워입니다.
- 수집된 데이터를 Article 스키마에 맞춰 정규화합니다.
"""

import re
import sys
import subprocess
import time
import os
import random
import socket
import threading
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timedelta
from collections import defaultdict
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional, Tuple
import logging
import csv
from io import StringIO
import copy
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException, TimeoutException

from bs4 import BeautifulSoup
import gspread
import pandas as pd
from notification_manager import NotificationManager
from service_accounts import SERVICE_ACCOUNTS # 공통 서비스 계정 모듈 임포트

# 차단 우회를 위한 cloudscraper 임포트 시도
try:
    import cloudscraper
except ImportError:
    cloudscraper = None

# IP 차단 감지용 예외 클래스
class IPBlockedError(Exception):
    pass

# 로깅
logger = logging.getLogger(__name__) # app.py에서 설정한 로거를 사용

# ====================================================================
# 설정
# ====================================================================
 
# [v6.7.111] GAME_COMMUNITIES / CAFE_LIST / DC_GALLERIES 모듈 상수 **폐기**.
#   시트 (`Config_Games`) 가 단일 출처 (SSoT). 시트 read 실패 시 빈 dict 로 시작 → 빈 cycle (안전한 no-op).
#   `__init__` 의 `self.cafe_list = {}` / `self.dc_galleries = {}` 초기화 → polling 이 시트 read 성공 시 즉시 채움.
#   시트 초기 setup 도구 (`utils/setup_config_sheet.py`) 는 자체 dict 보유 — 의존성 0.
#   복구 필요 시 git history (v6.7.110 이전) 에서 원본 dict 참조 가능.

# [v6.7.110] Config_Games 의 URL 컬럼별 host 화이트리스트 — 시트 입력 안전망.
#   사용자가 잘못된 URL (예: NaverCafe_URL 셀에 cafe.daum.net URL) 입력 시
#   해당 1개만 skip + warning log. 다른 URL/컬럼 영향 0.
#   매칭 정책: host 정확 일치 OR endswith('.' + ok_host) — 서브도메인 허용.
HOST_WHITELIST = {
    'Cafe_URL': ('cafe.daum.net',),
    'DC_URL': ('gall.dcinside.com',),
    'NaverCafe_URL': ('cafe.naver.com',),
    # 'Official_URL' 은 게임별 화이트리스트 — 아래 OFFICIAL_HOST_WHITELIST 참조.
}
# [v6.7.110] Official_URL — 게임마다 허용 도메인이 달라 별도 dict.
#   향후 게임 추가 시 entry 1줄. 매칭 정책은 HOST_WHITELIST 와 동일 (서브도메인 허용).
# [v6.7.136] POE2 추가 — poe.game.daum.net 한국 공식 자유게시판.
OFFICIAL_HOST_WHITELIST = {
    '배틀그라운드': ('bbs.pubg.game.daum.net', 'bbs-pubg.kakaogames.com'),  # [v6.7.251] 공식 게시판 도메인 이전(다음 병행 유지)
    # [v6.7.279] POE KR 도 도메인 이전(poe.game.daum.net → poe.kakaogames.com, 301). 시트는 POE1 행 단일
    #   (POE2 전용 보드 news2/patch-notes2 는 official_poe.py 가 board segment 로 분리 태깅). 두 host 모두 허용.
    'POE1': ('poe.kakaogames.com', 'poe.game.daum.net'),
    'POE2': ('poe.kakaogames.com', 'poe.game.daum.net'),
}

SUPER_KEYWORDS = [
    "공지", "업데이트", "점검", "패치", "필독", "임시"
]
HIGHLIGHT_KEYWORDS = [
    "오류", "현상", "확인",
    "부고","해킹","정지", "팅", "버그", "서버", "접속","장애","터짐", 
    "다운","팅","원본마","먹통","이벤트", 
    "프레임","추모","터짐","매칭","끼임",
    "죽음","간헐적","IOS","Android","인자작","과금",
    "결제","환불","먹통","렉","강제","종료",
    "튕김","지연","지연현상","렉걸림","버벅","끊김",
    "다운로드","업로드","불통","불안정","불편","장애",
    "점검중","점검예정","상태","다운",
    "불가","불편함","문제","안켜","안됨","ㅆㅂ",
    "씨","ㅅㅂ","ㅂㅅ","병신","ㅉㅉ","ㅗ",
    "사망","공유","검토","신고","졷",
    "핵","계정","차단","복구",
    "복원","탈퇴","요청","왜","아이폰",
    "안드로이드","업뎃","부모","자녀","제발",
    "자식","환장","열받","짜증","화남","화가",
    "도움","문의","지원","불만","개선","건의",
    "개편","불법","사기","피해","당함","당한",
    "요구","법적","조치","고소","고발",
    "공정위","콘텐츠","제재","처벌","처리","이럼",
    "추가","삭제","필요","원함","원해",
    "해줘","부탁","운영","시발",
    "좆","꺼져","ㅄ","미친","ㅇㅇㅇ2","밸런스",
    "억울","개같","좃","ㅈ같",
    "진행","IOS"
]

# [원복] 키워드 목록을 원래대로 분리하고 정리합니다.
# 키워드는 모두 소문자로 변환하여 처리 효율을 높입니다.
SUPER_KEYWORDS = list(dict.fromkeys([kw.strip().lower() for kw in SUPER_KEYWORDS if kw and kw.strip()]))
HIGHLIGHT_KEYWORDS = list(dict.fromkeys([kw.strip().lower() for kw in HIGHLIGHT_KEYWORDS if kw and kw.strip()]))
# 슈퍼 키워드와 중복되는 일반 키워드는 일반 키워드 목록에서 제거하여 우선순위를 보장합니다.
HIGHLIGHT_KEYWORDS = [k for k in HIGHLIGHT_KEYWORDS if k not in SUPER_KEYWORDS]

# [v6.7.739] 공식 게시판 연속 실패 경보 임계. OFFICIAL 태스크 주기(interval_min 180 ~ interval_max 300초)
#   기준 6회 = 약 18~30분 - 점검 같은 짧은 끊김엔 안 울리고, 멈추면 반 시간 안에 드러난다.
#   배그 공식은 2026-10-09 부터 403 이었는데 경고 로그 한 줄뿐이라 2주를 몰랐다(PATTERN-88).
OFFICIAL_FAIL_ALERT_STREAK = 6

# [설정] 기본값 정의 (구글 시트 로드 실패 시 사용되는 Fallback 값)
# 주의: 구글 시트(Config_Settings)에 값이 있다면 그 값이 우선 적용됩니다.
MAX_CAFE_ARTICLES = 15 # 기본 수집 개수 하향 (200 -> 15)
MAX_DC_ARTICLES = 15   # 기본 수집 개수 하향 (200 -> 15)
PER_GAME_LIMIT = 10 # [v6.7.133] 50 → 10 (사용자 정책: 어차피 많은 글 불러오지 않음 + 모달 default 일관성). 사용자가 시트 `Config_Settings.PER_GAME_LIMIT` 또는 UI 모달에서 게임별 박제 시 우선 적용.
MAX_MEMORY_ARTICLES = 10000 # 메모리에 저장할 전체 최신글 최대 개수
GOOGLE_SHEET_UPLOAD_LIMIT = 5000 # 3000 -> 5000 상향 (데이터셋 확보용)
UI_DISPLAY_LIMIT = 1000 # 웹/앱 화면에 표시할 최대 글 개수
# [v6.6.332] 게임별 특수 상한 제거 — 사용자 필터 UI 에서 limit=0(무제한) 설정 시에도
#   우마무스메 100 / 가디언테일즈 100 특수 상한이 서버단에 남아 있어 "전체 탭에 106건 고정"
#   증상 발생. 모든 게임을 PER_GAME_LIMIT 단일 기준으로 통일해 UI limit 이 투명하게 작동하도록.
#   (필요 시 향후 향후 UI 에서 서버 PER_GAME_LIMIT 자체를 관리자 전용으로 조정 가능하게 확장).
GAME_SPECIFIC_LIMITS = {}
HEADLESS = True
WEBDRIVER_WAIT_TIME = 45
MONITORING_INTERVAL_CAFE_MIN = 70
MONITORING_INTERVAL_CAFE_MAX = 120
MONITORING_INTERVAL_DC_MIN = 70
MONITORING_INTERVAL_DC_MAX = 120
# [v6.7.288] _run_task_in_executor 의 future.result() 무한대기 방지 상한.
# 재시도 3회 x WEBDRIVER_WAIT_TIME(45s) 여유를 감안해도 정상 사이클은 이 안에 끝나야 함 —
# 초과 시 태스크 내부 hang 으로 간주하고 해당 사이클만 실패 처리, 다음 사이클로 진행.
TASK_EXECUTOR_TIMEOUT = 240
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:109.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15"
]
ARTICLE_ROW_SELECTOR = "tbody tr"
IFRAME_ID = "down"

# 설정 시트 URL 및 서비스 계정 정보
from private_config import CONFIG_SHEET_URL  # [v6.7.745] 시트 ID 는 utils/private_config.py 한 자리

# 서비스 계정 풀 (utils/service_accounts.py에서 로드)
# SERVICE_ACCOUNTS = [...] # 기존 하드코딩 제거

# [v6.6.464] CONFIG 로드 retry 정책 — 점진적 backoff + 글로벌 lock + 429 우선 처리.
#   기존 "while True + 5초 sleep + 다중 스레드 동시 진입" 으로 quota 자가 회복 불가능 디도스 패턴 발생 → 해결.
#   sleep 시퀀스: 5초 → 30초 → 2분 → 10분 → 30분 → 1시간(cap) + jitter ±15%.
#   429/quota 감지 시 즉시 2차(2분) 단계부터 시작해서 분당 카운터 회복 시간 확보.
_config_load_lock = threading.Lock()
_RETRY_BACKOFF_SEQ = [5, 30, 120, 600, 1800]   # 시도 1~5 차 sleep (초)
_RETRY_MAX_SLEEP = 3600                         # 6 차+ cap (1시간)

class CrawlerManager:
    """
    [클래스 역할]
    메인 커뮤니티(다음 카페, 디시인사이드)의 실시간 모니터링을 총괄하는 중앙 제어(Orchestrator) 클래스임.
    
    [핵심 아키텍처 및 방어 기법]
    - 동적 태스크 스케줄링: `crawler_tasks` 딕셔너리에 정의된 함수들을 지정된 주기(interval)마다 실행함.
      [v6.7.291] 레인 병렬 — 태스크를 드라이버(세션) 경계로 묶은 레인(CAFE/DC/HTTP) 단위로 병렬 실행,
      레인 안은 직렬. 이전에는 전 태스크 완전 직렬이라 한 태스크 장기화가 전체 주기를 허구화했음.
    - 드라이버 생명주기 제어: 무한 구동 시 발생하는 봇 차단(Ban)을 방지하기 위해 일정 횟수 사용 후 드라이버를 자동 파기하고 새로 생성함(Session Refresh).
    - 메모리 캐싱 및 중복 필터링: `seen_links`를 통해 이미 수집된 게시글을 식별하고, 불필요한 AI 분석이나 API 호출을 원천 차단함.
    """
    def __init__(self, socketio=None, status_emitter=None, executor=None, data_export_callback=None, app_name="W", load_config=True, mode_name="Unknown"):
        self.app_name = app_name # 앱 구분 이름 (WEB/DESKTOP)
        self.socketio = socketio
        self.status_emitter = status_emitter
        self.executor = executor # [v6.7.291] 하위호환 fallback 큐 — 메인 태스크는 레인 전용 executor(_get_lane_executor) 사용으로 전환, lane 미지정 호출만 이 큐 경유
        self.data_export_callback = data_export_callback # 데이터 내보내기 콜백
        self.is_running = False
        # [v6.7.128] silent stop 자동복구 분기용 — True 이면 사용자가 명시적 /api/crawler/stop
        # 호출한 것, False 이면 SIGTERM/내부 cleanup 등으로 인한 비자발 정지.
        # `check_main_crawler_health` 가 이 플래그로 자동 재시작 여부 판단.
        self.user_initiated_stop = False
        # [v6.7.288] 루프 heartbeat — _monitoring_loop 가 살아있어도 hang 되면 thread.is_alive()
        # 만으론 못 잡음(2026-07-01 11:50~ 사고: future.result() 무한대기로 10시간+ 정지,
        # is_running/is_alive 둘 다 True 라 check_main_crawler_health 가 "정상"으로 오판).
        # check_main_crawler_health 가 이 시각 staleness 로 hang 을 별도 감지.
        # [v6.7.289] last_loop_heartbeat 은 '루프 반복 여부'만 나타내는 진단용 breadcrumb 로
        # 강등 — 240s 타임아웃 덕에 hang 중에도 루프가 계속 돌아(웹앱 max_workers=1 + 4태스크
        # → 1반복 ~960s) 이 값이 오르내리기 때문. hang 판정 자체는 아래 last_successful_crawl_ts.
        self.last_loop_heartbeat = time.time()
        # [v6.7.289] hang 판정의 정본 신호 — "타임아웃 없이 태스크가 반환한 마지막 시각".
        # 태스크가 timeout(hang) 되면 갱신 안 됨 → hang 동안 단조 증가하는 staleness →
        # 감지가 태스크 수×타임아웃 산술에 안 흔들리고 dedup 도 견고(heartbeat 방식의 약점 해소).
        # [v6.7.291] 레인 병렬화 이후엔 "임의 레인 성공" 신호로 의미 완화 — 한 레인만 hang 하면
        # 다른 레인 성공이 계속 갱신해 이 전역 신호로는 못 잡는다. hang 판정의 정본은 아래
        # lane_last_success (per-lane staleness, check_main_crawler_health 가 사용).
        self.last_successful_crawl_ts = time.time()
        # [v6.7.291] 레인 병렬화 상태 — 태스크를 브라우저/세션 리소스 단위 '레인'으로 묶어
        # 레인끼리 병렬, 레인 안은 직렬. CAFE(DAUM+NAVER: self.driver 공유) / DC(dc_driver) /
        # HTTP(OFFICIAL: requests). 신규 Chrome 0개 — 기존 드라이버 경계를 그대로 병렬 경계로 사용.
        self.lane_last_success = {}          # {lane: ts} — 레인별 hang 판정 신호 (start() 에서 시드)
        # [v6.7.551] 태스크별 신호 - **레인 단위 판정의 사각지대를 닫는다.** 한 레인에 주기가
        # 크게 다른 태스크가 같이 있으면(CAFE = NAVER 70~120초 + DAUM 900~1200초) 빈번한 쪽이
        # lane_last_success 를 계속 갱신해 **느린 쪽만 죽어도 레인은 정상으로 보인다**.
        # 2026-08-15 모니터링 검토 실측: DAUM 이 죽어도 NAVER 가 CAFE 시계를 리셋하므로 감지 0.
        # 게다가 수집 로그는 `new_count>0 or keyword_count>0` 일 때만 찍혀(_process_and_save)
        # **조용한 레인과 죽은 레인이 로그상 구별되지 않는다** - 이 신호가 유일한 구분 수단이다.
        self.task_last_success = {}          # {task key: ts} — 태스크별 hang 판정 신호 (start() 에서 시드)
        self._lane_executors = {}            # {lane: ThreadPoolExecutor(1)} — _get_lane_executor lazy 생성
        self._lane_executors_lock = threading.Lock()
        self._lane_threads = {}              # {lane: Thread} — _monitoring_loop 이 기동/join
        self._active_task_labels = set()     # 실행 중 태스크 라벨 집합 (병렬 status 표기 합성용)
        self._active_labels_lock = threading.Lock()
        self.driver = None
        self.dc_driver = None # DC 전용 지속성 드라이버 (세션 유지용)
        self.dc_driver_usage_count = 0 # 드라이버 사용 횟수
        self.dc_driver_max_usage = random.randint(30, 60) # 드라이버 교체 임계값 (초기 랜덤)
        # [v6.7.486] DC fetch 모드 (시트 DC_FETCH_MODE: selenium | http). 아래 3종은 자동 폴백 상태.
        self.dc_http_block_count = 0    # 403/429 연속 횟수 (성공 시 리셋)
        self.dc_http_forced_off = False # 차단 2회 → 시트가 http 여도 selenium 으로 강제 복귀
        self._dc_last_sheet_mode = None # 시트 값이 바뀌면 forced_off 해제 (사람이 다시 켜는 경로)
        self.articles_by_game = {}
        self.seen_links = {}
        self.new_alerts = {}
        self.keyword_alerts = {}
        self.all_latest_articles = []  # 모든 최신글 저장
        self._comments_cache = []       # [신규] 댓글 데이터 인메모리 캐시
        self.ack_all_alerts_history = []  # 확인된 전체 알림 기록
        self.ack_history = []
        self.logs = []
        self.last_update = datetime.now()
        # 서버 시작 시간을 KST Naive 기준으로 설정 (글 작성 시간과 비교용)
        self.server_start_time = datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)
        
        # [삭제] 하이라이트 임계값 관리 변수 (로직을 앱으로 이관)
        # self.current_max_ts, self.highlight_threshold_ts 제거됨
        
        # [신규] 이번 주기 수집 시작 시각 (데이터 스탬핑용)
        self.current_cycle_start_time = self.server_start_time
        self.status = "대기 중"
        self.crawler_thread = None
        self.lock_thread = None # 락 관리 스레드 핸들
        self.lock = threading.RLock() # 재진입 가능한 락으로 변경 (데드락 방지)
        self.config_lock = threading.RLock() # 설정 데이터 전용 락 (설정 창 충돌 방지)
        self.processed_alert_links = set() # 알림 중복 방지용 링크 집합
        self.processed_official_alert_links = set() # 공지 알림 중복 방지
        self.night_trends = {}
        
        # 설정 및 게임 목록 인스턴스 변수 초기화 (기본값)
        self.settings = {}
        # [v6.7.111] 시트 SSoT — fallback 모듈 상수 폐기. polling 이 시트 read 성공 시 즉시 채움.
        self.cafe_list = {}        # {GameName: List[str]} — Config_Games:Cafe_URL
        self.dc_galleries = {}     # {GameName: List[str]} — Config_Games:DC_URL
        self.super_keywords = SUPER_KEYWORDS.copy()
        self.highlight_keywords = HIGHLIGHT_KEYWORDS.copy()
        self.official_keywords = []  # OFFICIAL 타입 키워드 (다음 카페 공지 전용)
        self.night_report_games = []
        self.ccu_games = []
        self.last_config_check_time = 0
        self.dc_cooldown_until = 0 # DC 차단 시 대기 시간 (Timestamp)
        self.machine_id = socket.gethostname() # 컴퓨터 이름만 사용
        self.mode_name = mode_name # 실행 모드 이름 (Viewer Mode, Desktop Mode 등)
        
        # 설정 원본 데이터 저장소 (설정 창 표시용, Active=FALSE 포함)
        self.raw_config_games = []
        self.raw_config_keywords = []
        self.raw_config_sub_crawlers = []
        self.raw_config_tabs = [] # 탭 설정 원본
        self.tab_config = {}      # 탭 가시성 설정 {TabName: bool}
        self.comment_targets = [] # [신규] 댓글 크롤링 대상 목록 [{'game': '오딘', 'url': '...'}]
        # [v6.7.86 → v6.7.110] Config_Games 의 Official_URL 컬럼 — 게임별 공식 홈페이지 URL 리스트.
        # v6.7.110 부터 String 단일 → List[str] 로 변경 (시트 셀 콤마 구분 multi-URL 지원).
        # Active=TRUE 행만 등록 (cafe_list / dc_galleries 와 동일 정책).
        self.official_urls = {}   # {GameName: List[str]}
        self._official_fail_streak = {}   # [v6.7.739] {board_name: 연속 실패 횟수} - _note_official_board_result
        # [v6.7.110 read 인프라 → v6.7.113 본체 크롤러 활성] NaverCafe_URL 컬럼 — 네이버 카페 크롤 대상 URL 리스트.
        #   v6.7.113 부터 '_crawl_naver_with_driver' (활성 task, line ~307 enabled=True) → _crawl_naver(self.driver)
        #   → crawlers/naver_cafe.py crawl_naver_cafe 가 실제 크롤링. self.driver(DAUM 공유 chrome)로 신 UI
        #   SPA 렌더 후 parse. DAUM/DC 와 동일 활성 상태. **로그인 세션은 쓰지 않는다** (2026-08-20
        #   claim audit 정정 — 종전 "회원제 카페는 네이버 로그인 세션 의존" 은 참인 적이 없다.
        #   `_get_driver()` 에 user-data-dir 이 없어 쿠키가 0이다. 상세는 naver_cafe.py 헤더).
        self.naver_cafe_list = {}   # {GameName: List[str]} — Config_Games:NaverCafe_URL (활성)
        # [v6.7.131] Config_Games.ExcludeBoards 키워드 — {GameName: [kw1, kw2, ...]}
        # board_name 이 키워드 substring 포함하면 article 폐기 (메모리/SQLite/시트 진입 전 차단).
        # 4 메인 소스 (Daum/NaverCafe/공식/DC) 단일 진입점 `_update_articles` 에서 일괄 필터링.
        # DC 는 board_name 이 `{game} 갤러리` 고정 라벨이라 사실상 영향 0 (의도된 미적용).
        self.board_exclude_kw = {}  # {GameName: List[str]}

        # [v6.7.159] 부팅 race 가드 — Config_Games 1회 polling 완료 전엔 _update_articles 가
        # NAVER/DAUM article 진입 차단 (ExcludeBoards dict 가 비어있는 상태에서 leak 방지).
        # 첫 polling 에서 games_records 처리 완료 시 True 마킹 (`_apply_configuration` 끝).
        self._first_config_poll_done = False

        # 서브 크롤러 설정 저장소
        self.sub_crawlers = {
            'JASA': {},
            'TASA': {},
            'ACCOUNT': [],
            'ODIN_GLOBAL': {}
        }
        
        self.current_account_index = 0 # 현재 사용 중인 계정 인덱스

        # [리팩토링] 크롤러 작업 레지스트리 (확장성 확보)
        # 새로운 사이트(예: 네이버) 추가 시 여기에 등록하면 메인 루프 수정 없이 자동 실행됨
        # [v6.7.291] `lane` 키 — 레인 병렬화의 병렬 경계. Selenium WebDriver 는 thread-safe 하지
        #   않으므로 "드라이버(세션) 경계 = 병렬 경계" 원칙으로 묶는다: 같은 lane = 같은 드라이버
        #   공유 = 레인 내 직렬 (driver race 원천 차단), 다른 lane 끼리는 병렬. 신규 태스크 등록 시
        #   기존 드라이버를 쓰면 그 드라이버의 lane 에, 독립 세션이면 새 lane 문자열로 등록.
        self.crawler_tasks = {
            'DAUM': {
                'func': self._crawl_daum_with_driver,
                'interval_min': MONITORING_INTERVAL_CAFE_MIN,
                'interval_max': MONITORING_INTERVAL_CAFE_MAX,
                'next_run': None,
                'status_label': 'Daum 카페',
                'enabled': True,
                'lane': 'CAFE'  # self.driver 사용 (NAVER 와 공유)
            },
            'DC': {
                'func': self._crawl_dc_with_driver, # Selenium 드라이버 사용 함수로 변경
                'interval_min': MONITORING_INTERVAL_DC_MIN,
                'interval_max': MONITORING_INTERVAL_DC_MAX,
                'next_run': None,
                'status_label': 'DC 갤러리',
                'enabled': True,
                'lane': 'DC'  # dc_driver 전용 (지속성 세션)
            },
            # [v6.7.87] 공식 홈페이지 크롤러 — bbs.pubg.game.daum.net (PC002 + PN001).
            #   HTTP fetch + BeautifulSoup (Selenium 불필요) → 메모리/CPU 부담 거의 없음.
            #   오늘 글 (HH:MM 형식) 만 수집. YY.MM.DD 은 skip — 메인 크롤러 (DAUM/DC) 와 동일 정책.
            #   3~5분 주기 — 공식 게시판은 글 주기가 길어 빈도 낮춤.
            'OFFICIAL': {
                'func': self._crawl_official_with_driver,
                'interval_min': 180,
                'interval_max': 300,
                'next_run': None,
                'status_label': '공식 홈페이지',
                'enabled': True,
                'lane': 'HTTP'  # requests + BS4 — 드라이버 미사용
            },
            # [v6.7.113] 네이버 카페 크롤러 — cafe.naver.com/f-e/ 신 UI SPA.
            #   self.driver 공유 (DAUM 과 동일 chrome instance) + SPA 렌더링 대기 후 page_source parse.
            #   **주기 주의 [v6.7.487 정정]**: 초기값은 DAUM 과 같은 상수(MONITORING_INTERVAL_CAFE_*)를
            #   쓰지만, **시트 Hot Reload 대상이 아니다** — 아래 설정 반영 블록이 `DAUM`·`DC` 만 갱신하므로
            #   `Config_Settings.MONITORING_INTERVAL_CAFE_*` 를 바꿔도 NAVER 는 **모듈 기본값(70~120초)
            #   그대로 남는다.** 구 주석은 "카페 계열이므로 DAUM 과 동일" 이라 적어 두 값이 함께 움직이는
            #   것처럼 읽혔고, 실제로 2026-08-05 에 DAUM 을 5~15초 → 900~1200초로 바꿨을 때 NAVER 는
            #   안 바뀌었다(그래서 DAUM 이 레인을 독점하던 동안 밀려 있던 NAVER 가 오히려 정상화됐다).
            #   의도가 "함께 움직이는 것" 이면 아래 블록에 NAVER 분기를 추가해야 한다 — 미결.
            #   `Config_Games.NaverCafe_URL` 컬럼 비어있으면 자동 skip (`_crawl_naver_with_driver` gate).
            'NAVER': {
                'func': self._crawl_naver_with_driver,
                'interval_min': MONITORING_INTERVAL_CAFE_MIN,
                'interval_max': MONITORING_INTERVAL_CAFE_MAX,
                'next_run': None,
                'status_label': '네이버 카페',
                'enabled': True,
                'lane': 'CAFE'  # self.driver 사용 (DAUM 과 공유 — 같은 레인이라 직렬 보장)
            }
        }
        
        self.initial_crawl_done = False # 초기 크롤링 완료 여부 플래그
        self.last_articles_update_ts = 0.0 # [최적화] 데이터 변경 시간
        self.last_all_articles_update_ts = 0.0 # [최적화] 전체글 변경 시간

        # 로그 전송 최적화 (Throttling)
        self.log_lock = threading.Lock()
        self.log_version = 0 # 로그 변경 감지용 버전 카운터
        self._log_thread_active = False
        if self.socketio: # 웹앱 환경에서만 로그 병합 전송 루프 실행
            self._log_thread_active = True
            threading.Thread(target=self._log_emission_loop, daemon=True).start()

        # 초기 설정 로드
        if load_config:
            self.load_config_from_sheet()
        
        logger.info("CrawlerManager 초기화됨")

    def _is_true(self, value):
        """설정값의 True/False 여부를 유연하게 판단합니다."""
        return str(value).strip().upper() in ['TRUE', 'T', '1', 'YES', 'Y', 'ON']

    def _validate_url_host(self, column: str, url: str, game_name: str) -> bool:
        """[v6.7.110] URL host 화이트리스트 검사 — 시트 입력 안전망.
        위반 시 False + warning log (해당 URL skip). 빈 URL 은 True (skip 무관).
        Official_URL 은 게임별 dict (`OFFICIAL_HOST_WHITELIST`) 참조, 그 외는 `HOST_WHITELIST` 참조.
        매칭 정책: host 정확 일치 OR endswith('.' + ok_host) — 서브도메인 허용 (예: bbs.pubg.game.daum.net).
        whitelist 없는 컬럼은 통과 (안전망 미적용)."""
        if not url or not str(url).strip():
            return True
        url = str(url).strip()
        try:
            from urllib.parse import urlparse
            host = (urlparse(url).hostname or '').lower()
        except Exception:
            self._log(f"[{column}] {game_name} URL 파싱 실패 → skip: {url[:80]}", 'warning', '설정')
            return False
        if column == 'Official_URL':
            allowed = OFFICIAL_HOST_WHITELIST.get(game_name, ())
        else:
            allowed = HOST_WHITELIST.get(column, ())
        if not allowed:
            return True
        for ok_host in allowed:
            if host == ok_host or host.endswith('.' + ok_host):
                return True
        self._log(
            f"⚠ {game_name} {column} 호스트 화이트리스트 위반 → skip: {host} (허용: {', '.join(allowed)})",
            'warning', '설정'
        )
        return False

    def _parse_multi_urls(self, raw, column: str, game_name: str):
        """[v6.7.110] 시트 셀 raw 값 → 콤마 split → host validation 통과한 URL 만 list 로 반환.
        빈 / 단일 URL 도 동일 흐름 (backward compat). 콤마 양쪽 공백 / 셀 내 줄바꿈 자동 정규화.
        반환 List[str] (validation 모두 실패 시 빈 list)."""
        if raw is None:
            return []
        s = str(raw).strip()
        if not s:
            return []
        # 콤마 + 줄바꿈 둘 다 구분자로 (사용자가 시트에서 Alt+Enter 로 입력하는 경우 흡수)
        import re as _re
        urls = [u.strip() for u in _re.split(r'[,\n]', s) if u.strip()]
        return [u for u in urls if self._validate_url_host(column, u, game_name)]

    def get_config_data(self, key):
        """설정 데이터를 스레드 안전하게 복사하여 반환합니다."""
        with self.config_lock:
            if hasattr(self, key):
                data = getattr(self, key)
                if data:
                    return copy.deepcopy(data)
            return []

    def _apply_configuration(self, settings_records=None, games_records=None, kw_records=None, night_records=None, ccu_records=None, sub_records=None, tabs_records=None, contents_records=None):
        """설정 데이터를 받아 메모리에 적용하고 변경 사항을 반환합니다."""
        try:
            changes = []
            # 설정 적용 중 데이터 접근 차단 (설정 창 팅김 방지)
            with self.config_lock:

                # 1. Config_Settings
                if settings_records is not None:
                    self.raw_config_settings = settings_records # 원본 저장
                    new_settings = {}
                    for row in settings_records:
                        key = row.get('Key')
                        value = row.get('Value')
                        if key and isinstance(key, str): key = key.strip()
                        if value is not None and isinstance(value, str): value = value.strip()
                        
                        if key and value is not None:
                            if isinstance(value, (int, float)):
                                new_settings[key] = value
                            elif str(value).replace('.','',1).isdigit():
                                try:
                                    new_settings[key] = float(value) if '.' in str(value) else int(value)
                                except Exception:
                                    new_settings[key] = value
                            else:
                                new_settings[key] = value
                    
                    if self.settings != new_settings:
                        self.settings = new_settings
                        changes.append("일반 설정")
                        
                        # 주요 스레드 설정값 로깅 (적용 확인용)
                        th_odin = self.settings.get('THREAD_ODIN_GLOBAL', 'N/A')
                        th_account = self.settings.get('THREAD_ACCOUNT_TRADE', 'N/A')
                        th_jasa = self.settings.get('THREAD_JASA_GAMES', 'N/A')
                        th_tasa = self.settings.get('THREAD_TASA_GAMES', 'N/A')
                        th_server = self.settings.get('MAX_SERVER_WORKERS', 'N/A')
                        self._log(f"스레드 설정: 오딘({th_odin}), 계정({th_account}), 자사({th_jasa}), 타사({th_tasa}), 서버({th_server})", 'info', 'CONFIG')
                        
                        # 크롤링 주기 설정 실시간 반영 (Hot Reload)
                        # 키 존재 여부 확인하여 KeyError 방지
                        if 'MONITORING_INTERVAL_CAFE_MIN' in self.settings and 'DAUM' in self.crawler_tasks:
                            self.crawler_tasks['DAUM']['interval_min'] = int(self.settings['MONITORING_INTERVAL_CAFE_MIN'])
                        if 'MONITORING_INTERVAL_CAFE_MAX' in self.settings and 'DAUM' in self.crawler_tasks:
                            self.crawler_tasks['DAUM']['interval_max'] = int(self.settings['MONITORING_INTERVAL_CAFE_MAX'])
                        
                        if 'MONITORING_INTERVAL_DC_MIN' in self.settings and 'DC' in self.crawler_tasks:
                            self.crawler_tasks['DC']['interval_min'] = int(self.settings['MONITORING_INTERVAL_DC_MIN'])
                        if 'MONITORING_INTERVAL_DC_MAX' in self.settings and 'DC' in self.crawler_tasks:
                            self.crawler_tasks['DC']['interval_max'] = int(self.settings['MONITORING_INTERVAL_DC_MAX'])


                # 2. Config_Games
                if games_records is not None:
                    self.raw_config_games = games_records
                    new_cafe_list = {}
                    new_dc_galleries = {}
                    new_naver_cafe_list = {}  # [v6.7.110 인프라 → v6.7.113 본체 활성] NaverCafe_URL 컬럼
                    new_official_urls = {}    # [v6.7.86 → v6.7.110] String → List[str]
                    new_board_exclude_kw = {}  # [v6.7.131] ExcludeBoards 콤마 구분 키워드
                    for row in games_records:
                        if self._is_true(row.get('Active')):
                            game_name = row.get('GameName')
                            if not game_name:
                                continue
                            # [v6.7.110] 4 컬럼 multi-URL parsing + host validation 통합.
                            #   _parse_multi_urls 가 콤마/줄바꿈 split + 컬럼별 화이트리스트 검사 + 위반 URL skip.
                            cafe_urls = self._parse_multi_urls(row.get('Cafe_URL'), 'Cafe_URL', game_name)
                            dc_urls = self._parse_multi_urls(row.get('DC_URL'), 'DC_URL', game_name)
                            naver_urls = self._parse_multi_urls(row.get('NaverCafe_URL'), 'NaverCafe_URL', game_name)
                            official_url_list = self._parse_multi_urls(row.get('Official_URL'), 'Official_URL', game_name)

                            if cafe_urls: new_cafe_list[game_name] = cafe_urls
                            if dc_urls: new_dc_galleries[game_name] = dc_urls
                            if naver_urls: new_naver_cafe_list[game_name] = naver_urls
                            if official_url_list: new_official_urls[game_name] = official_url_list

                            # [v6.7.131] ExcludeBoards 키워드 파싱 — 콤마 구분, 각 element strip,
                            # 빈 셀이면 등록 안 함 (기존 동작 유지 — 회귀 0).
                            exclude_str = str(row.get('ExcludeBoards', '') or '').strip()
                            if exclude_str:
                                kws = [kw.strip() for kw in exclude_str.split(',') if kw.strip()]
                                if kws:
                                    new_board_exclude_kw[game_name] = kws

                    if self.cafe_list != new_cafe_list:
                        self.cafe_list = new_cafe_list
                        changes.append("카페 목록")
                    elif list(self.cafe_list.keys()) != list(new_cafe_list.keys()):
                        self.cafe_list = new_cafe_list
                        changes.append("게임 순서")

                    if self.dc_galleries != new_dc_galleries:
                        self.dc_galleries = new_dc_galleries
                        changes.append("DC 목록")
                    elif list(self.dc_galleries.keys()) != list(new_dc_galleries.keys()):
                        self.dc_galleries = new_dc_galleries
                        changes.append("게임 순서")

                    # [v6.7.110 인프라 → v6.7.113 본체 활성] NaverCafe_URL — '_crawl_naver_with_driver' 가 이 list 를 크롤.
                    if self.naver_cafe_list != new_naver_cafe_list:
                        self.naver_cafe_list = new_naver_cafe_list
                        changes.append("네이버 카페 목록")

                    # [v6.7.86 → v6.7.110] Official URL — String → List[str] 변경.
                    if self.official_urls != new_official_urls:
                        self.official_urls = new_official_urls
                        changes.append("공식 홈페이지")

                    # [v6.7.131] ExcludeBoards 키워드 — diff swap (cafe_list 와 동일 패턴).
                    if self.board_exclude_kw != new_board_exclude_kw:
                        self.board_exclude_kw = new_board_exclude_kw
                        changes.append("게시판 제외 키워드")
                        # [v6.7.174] 키워드 변경 시 이미 저장된 매칭 행 1회 retroactive 삭제 (SQLite).
                        # push-gate 는 신규만 차단 → 키워드 추가 전 저장분은 잔존 → v6.7.172 단일게임
                        # 필터 피드(SQLite 직독)에 노출. 첫 polling 시 {} → {kw} 도 diff 라 부팅 1회 자동 청소.
                        try:
                            from sqlite_handler import db_handler as _db
                            _swept = _db.delete_excluded_board_posts(new_board_exclude_kw)
                            if _swept:
                                self._log(f"ExcludeBoards 키워드 변경 — 기존 {_swept}행 retroactive 삭제", 'info', '설정')
                        except Exception as _e:
                            self._log(f"ExcludeBoards retroactive 삭제 실패: {_e}", 'warning', '설정')

                    # [v6.7.159] 첫 Config_Games polling 완료 마킹 — `_update_articles` 의 NAVER/DAUM race 가드 해제.
                    # ExcludeBoards 가 비어있어도 polling 자체가 1회 끝나면 마킹 (race 차단이 목적이지 dict 비어있음 자체는 정상).
                    if not self._first_config_poll_done:
                        self._first_config_poll_done = True
                        self._log("Config_Games 첫 polling 완료 — NAVER/DAUM article 진입 가드 해제", 'info', 'CONFIG')

                # 3. Config_Keywords
                if kw_records is not None:
                    if not isinstance(kw_records, list):
                        kw_records = []
                    self.raw_config_keywords = kw_records
                    new_super = []
                    new_highlight = []
                    new_official = []  # OFFICIAL 타입 (다음 카페 공지 전용)
                    for row in kw_records:
                        if not isinstance(row, dict): continue
                        if self._is_true(row.get('Active')):
                            try:
                                kw_raw = str(row.get('Keyword', '')).strip().lower()
                                if not kw_raw: continue
                                # [안전장치] 콤마로 구분된 대량의 키워드 파싱
                                keywords = [k.strip() for k in kw_raw.split(',') if k.strip()]
                                kw_type = str(row.get('Type', 'NORMAL')).strip().upper()
                                for kw in keywords:
                                    if kw_type == 'SUPER': new_super.append(kw)
                                    elif kw_type == 'OFFICIAL': new_official.append(kw)
                                    else: new_highlight.append(kw)
                            except Exception: continue # 파싱 오류 시 해당 행 건너뜀
                    new_super_keywords = list(set(new_super))
                    new_highlight_keywords = list(set(new_highlight))
                    new_official_keywords = list(set(new_official))

                    if set(self.super_keywords) != set(new_super_keywords):
                        self.super_keywords = new_super_keywords
                        changes.append("슈퍼 키워드")
                    if set(self.highlight_keywords) != set(new_highlight_keywords):
                        self.highlight_keywords = new_highlight_keywords
                        changes.append("일반 키워드")
                    if set(self.official_keywords) != set(new_official_keywords):
                        self.official_keywords = new_official_keywords
                        changes.append("공지 키워드")

                # 4. Config_NightReport
                if night_records is not None:
                    self.raw_config_night_report = night_records # 원본 저장
                    new_night_report_games = [r.get('GameName') for r in night_records if self._is_true(r.get('Active')) and r.get('GameName')]
                    if set(self.night_report_games) != set(new_night_report_games):
                        self.night_report_games = new_night_report_games
                        changes.append("야간 동향 대상")

                # 5. Config_CCU
                if ccu_records is not None:
                    self.raw_config_ccu = ccu_records # 원본 저장
                    new_ccu_games = [r.get('GameName') for r in ccu_records if self._is_true(r.get('Active')) and r.get('GameName')]
                    if set(self.ccu_games) != set(new_ccu_games):
                        self.ccu_games = new_ccu_games
                        changes.append("CCU 대상")

                # 6. Config_SubCrawler
                if sub_records is not None:
                    self.raw_config_sub_crawlers = sub_records
                    new_sub_crawlers = {'JASA': {}, 'TASA': {}, 'ACCOUNT': [], 'ODIN_GLOBAL': {}}
                    for row in sub_records:
                        if self._is_true(row.get('Active')):
                            cat = row.get('Category')
                            name = row.get('GameName')
                            url = row.get('URL')
                            try: unit = int(str(row.get('Unit')).replace(',', ''))
                            except Exception: unit = 1000
                            
                            if cat in ['JASA', 'TASA', 'ODIN_GLOBAL']:
                                try: price_min = int(str(row.get('PriceMin', '')).replace(',', '').strip()) if str(row.get('PriceMin', '')).replace(',', '').strip().isdigit() else None
                                except Exception: price_min = None
                                try: price_max = int(str(row.get('PriceMax', '')).replace(',', '').strip()) if str(row.get('PriceMax', '')).replace(',', '').strip().isdigit() else None
                                except Exception: price_max = None
                                new_sub_crawlers[cat][name] = {'url': url, 'unit': unit, 'price_min': price_min, 'price_max': price_max}
                            elif cat == 'ACCOUNT':
                                if not any(item['name'] == name for item in new_sub_crawlers['ACCOUNT']):
                                    new_sub_crawlers['ACCOUNT'].append({'name': name, 'url': url})
                    
                    if self.sub_crawlers != new_sub_crawlers:
                        self.sub_crawlers = new_sub_crawlers
                        changes.append("서브 크롤러 설정")
                
                # 7. Config_Tabs (신규)
                if tabs_records is not None:
                    self.raw_config_tabs = tabs_records
                    new_tab_config = {}
                    for row in tabs_records:
                        tab_name = str(row.get('TabName', '')).strip()
                        visible = self._is_true(row.get('Visible', 'TRUE'))
                        if tab_name:
                            new_tab_config[tab_name] = visible
                    
                    if self.tab_config != new_tab_config:
                        self.tab_config = new_tab_config
                        changes.append("탭 가시성 설정")
                        
                # 8. Config_Contents (신규)
                if contents_records is not None:
                    new_comment_targets = []
                    for row in contents_records:
                        if self._is_true(row.get('Active')):
                            game = row.get('GameName')
                            url = row.get('Comment_URL')
                            if game and url:
                                new_comment_targets.append({'game': game, 'url': url})
                    
                    self.comment_targets = new_comment_targets
                    # 댓글 설정 변경은 로그만 남기고 changes에는 추가하지 않음 (UI 갱신 불필요)

                if changes:
                    change_msg = ", ".join(changes)
                    # 사용자 요청으로 상세 변경 내역 로그 제거 (UI에서 완료 메시지만 표시)
                    # self._log(f"설정 변경 감지: {change_msg}", 'system', 'CONFIG')
                    return f"설정 갱신됨: {change_msg}"
                
                return None
        except Exception as e:
            self._log(f"설정 적용 중 예외 발생: {e}", 'error', 'CONFIG')
            return f"설정 적용 실패: {e}"

    def update_config_from_dataframe(self, data_map):
        """DataFrame 딕셔너리를 받아 설정을 갱신합니다."""
        try:
            settings_records = data_map['Config_Settings'].to_dict('records') if 'Config_Settings' in data_map else None
            games_records = data_map['Config_Games'].to_dict('records') if 'Config_Games' in data_map else None
            kw_records = data_map['Config_Keywords'].to_dict('records') if 'Config_Keywords' in data_map else None
            night_records = data_map['Config_NightReport'].to_dict('records') if 'Config_NightReport' in data_map else None
            ccu_records = data_map['Config_CCU'].to_dict('records') if 'Config_CCU' in data_map else None
            sub_records = data_map['Config_SubCrawler'].to_dict('records') if 'Config_SubCrawler' in data_map else None
            tabs_records = data_map['Config_Tabs'].to_dict('records') if 'Config_Tabs' in data_map else None
            contents_records = data_map['Config_Contents'].to_dict('records') if 'Config_Contents' in data_map else None
            
            return self._apply_configuration(settings_records, games_records, kw_records, night_records, ccu_records, sub_records, tabs_records, contents_records)
        except Exception as e:
            self._log(f"설정 데이터 변환 중 예외 발생: {e}", 'error', 'CONFIG')
            return f"설정 데이터 처리 실패: {e}"

    def update_config_from_batch_data(self, batch_data):
        """values_batch_get 응답 데이터를 받아 설정을 갱신합니다."""
        try:
            # batch_data는 8개 탭의 raw 데이터 리스트임 (각각 'values' 키를 가진 dict)
            def parse_records(value_range_dict):
                values = value_range_dict.get('values', [])
                if not values: return []
                headers = values[0]
                records = []
                for row in values[1:]:
                    # 헤더 길이만큼 row 데이터 확보 (부족하면 빈 문자열, 넘치면 자름)
                    record = {headers[i]: (row[i] if i < len(row) else "") for i in range(len(headers))}
                    records.append(record)
                return records

            settings_records = parse_records(batch_data[0]) if len(batch_data) > 0 else None
            games_records = parse_records(batch_data[1]) if len(batch_data) > 1 else None
            kw_records = parse_records(batch_data[2]) if len(batch_data) > 2 else None
            night_records = parse_records(batch_data[3]) if len(batch_data) > 3 else None
            ccu_records = parse_records(batch_data[4]) if len(batch_data) > 4 else None
            sub_records = parse_records(batch_data[5]) if len(batch_data) > 5 else None
            tabs_records = parse_records(batch_data[6]) if len(batch_data) > 6 else None
            contents_records = parse_records(batch_data[7]) if len(batch_data) > 7 else None
            
            return self._apply_configuration(settings_records, games_records, kw_records, night_records, ccu_records, sub_records, tabs_records, contents_records)
        except Exception as e:
            self._log(f"배치 설정 데이터 처리 중 예외 발생: {e}", 'error', 'CONFIG')
            return f"배치 설정 처리 실패: {e}"

    def _update_client_status(self):
        """현재 앱의 상태(Machine_ID, Status)를 Config_State 시트에 기록합니다."""
        try:
            creds = SERVICE_ACCOUNTS[self.current_account_index]
            gc = gspread.service_account_from_dict(creds)
            sh = gc.open_by_url(CONFIG_SHEET_URL)
            ws = sh.worksheet("Config_Clients")
            
            now_str = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S")
            
            # 전체 데이터 가져오기 (헤더 포함)
            all_values = ws.get_all_values()
            
            found_row_idx = -1
            
            # 2행부터 검색 (1행은 헤더)
            # A: Machine_ID, B: Status, C: Last_Updated
            for i in range(1, len(all_values)):
                row = all_values[i]
                if len(row) >= 2:
                    m_id = row[0]
                    status = row[1]
                    # Machine_ID와 Status가 모두 일치하는 행 찾기
                    if m_id == self.machine_id and status == self.mode_name:
                        found_row_idx = i + 1 # 1-based row index
                        break
            
            if found_row_idx != -1:
                # 기존 행 업데이트 (C: Last_Updated) — [v6.7.389] Quota Management 원칙: update_cell 금지 → 명시 range batch 형.
                #   함정: gspread 6.2.1 signature 는 update(values, range_name=...) (values 우선). CLAUDE.md 원칙 예시의
                #   update(range_name, data) 는 gspread<6 옛 순서라 그대로 쓰면 깨짐 → keyword 인자로 순서 무관 고정.
                #   raw=False 로 USER_ENTERED 유지(update_cell 기본값과 동일 — 값 타입 보존).
                ws.update(values=[[now_str]], range_name=f'C{found_row_idx}', raw=False)
            else:
                # 새 행 추가 (Machine_ID, Status, Last_Updated)
                ws.append_row([self.machine_id, self.mode_name, now_str])
                
        except Exception as e:
            self._log(f"클라이언트 상태 기록 실패: {e}", 'warning', 'STATUS')

    def _status_maintenance_loop(self):
        """주기적으로 클라이언트 상태(실행 중)를 갱신하는 루프"""
        while self.is_running:
            try:
                self._update_client_status()
            except Exception as e:
                self._log(f"상태 갱신 루프 오류: {e}", 'error', 'STATUS')
            
            # 2분(120초) 대기 (1초씩 체크하여 빠른 종료 지원)
            for _ in range(120):
                if not self.is_running: break
                time.sleep(1)

    def load_config_from_sheet(self):
        """구글 시트에서 설정을 로드 + 인스턴스 변수 적용.
        [v6.6.464] wrapper — 글로벌 lock 으로 다중 스레드 직렬화 + 점진적 backoff.
        실제 로직은 _load_config_from_sheet_inner 에 위임."""
        # [v6.6.464] 다중 스레드 직렬화 — 다른 스레드가 이미 retry 중이면 이번엔 skip.
        #   "1초마다 새 retry 메시지" 디도스 패턴(quota 자가 회복 불가) 차단.
        if not _config_load_lock.acquire(blocking=False):
            self._log("CONFIG 로드 다른 스레드 진행 중 — 이번 cycle skip", 'info', 'CONFIG')
            return None
        try:
            return self._load_config_from_sheet_inner()
        finally:
            _config_load_lock.release()

    def _load_config_from_sheet_inner(self):
        """[v6.6.464] 기존 load_config_from_sheet 본체 — 들여쓰기 그대로 보존.
        wrapper 가 lock 관리, 본체는 batch read + 점진적 backoff retry 만 담당."""
        # [v6.6.153] PATTERN-12: 다중 스레드 SSL Access Violation 방지
        from utils.google_sheets_handler import _network_lock

        # [v6.7.747] 서비스 계정이 0개면(키 파일 없음 - utils/service_accounts.py 가 부팅 때 [FAIL] 을 남긴다) 재시도해도
        #   소용없다. 전에는 아래 except 의 `% len(SERVICE_ACCOUNTS)` 가 ZeroDivisionError 로 터져 원인을 가렸고, 나눗셈만
        #   막으면 '성공할 때까지 무한 재시도' 라 영원히 멈춘다 - 그래서 루프 전에 돌아온다(변경 없음 = 빈 목록).
        if not SERVICE_ACCOUNTS:
            self._log("설정 로드 건너뜀 - 서비스 계정 0개(키 파일 없음, 부팅 로그의 [FAIL] 참조)", 'error', 'CONFIG')
            return []

        # [v6.6.464] 점진적 backoff 시도 카운터
        attempt = 0

        # 성공할 때까지 무한 재시도 (필수 설정 로드 보장)
        while True:
            try:
                # 배치 처리를 위해 한 번에 요청할 범위 목록 정의 (1회 호출로 모든 설정 로드)
                # 각 탭의 데이터가 있는 범위를 넉넉하게 지정 (A:Z)
                ranges = [
                    "Config_Settings!A:Z",
                    "Config_Games!A:Z",
                    "Config_Keywords!A:Z",
                    "Config_NightReport!A:Z",
                    "Config_CCU!A:Z",
                    "Config_SubCrawler!A:Z", # 서브 크롤러 설정
                    "Config_Tabs!A:Z",       # 탭 설정
                    "Config_Contents!A:Z"    # [신규] 댓글 관리 설정
                ]

                with _network_lock:
                    creds = SERVICE_ACCOUNTS[self.current_account_index]
                    gc = gspread.service_account_from_dict(creds)
                    sh = gc.open_by_url(CONFIG_SHEET_URL)
                    # API 호출 (1 quota)
                    response = sh.values_batch_get(ranges)

                value_ranges = response.get('valueRanges', [])

                # 데이터 파싱 헬퍼 함수 (get_all_records와 동일한 형태인 리스트[딕셔너리]로 변환)
                def parse_records(value_range):
                    values = value_range.get('values', [])
                    if not values: return []
                    headers = values[0]
                    records = []
                    for row in values[1:]:
                        # 헤더 길이만큼 row 데이터 확보 (부족하면 빈 문자열, 넘치면 자름)
                        record = {headers[i]: (row[i] if i < len(row) else "") for i in range(len(headers))}
                        records.append(record)
                    return records

                # 1. Config_Settings 처리
                settings_records = parse_records(value_ranges[0])
                # 2. Config_Games 처리
                games_records = parse_records(value_ranges[1])
                # 3. Config_Keywords 처리
                kw_records = parse_records(value_ranges[2])
                # 4, 5. NightReport, CCU
                night_records = parse_records(value_ranges[3])
                ccu_records = parse_records(value_ranges[4])
                # 6. Config_SubCrawler 처리
                sub_records = parse_records(value_ranges[5])
                # 7. Config_Tabs 처리
                tabs_records = parse_records(value_ranges[6]) if len(value_ranges) > 6 else []
                # 8. Config_Contents 처리
                contents_records = parse_records(value_ranges[7]) if len(value_ranges) > 7 else []

                changes = self._apply_configuration(settings_records, games_records, kw_records, night_records, ccu_records, sub_records, tabs_records, contents_records)
                
                # [신규] SQLite에 설정 캐시 저장 (Viewer 모드 지원용)
                try:
                    from utils.sqlite_handler import db_handler
                    db_handler.update_system_config('settings', self.settings)
                    db_handler.update_system_config('sub_crawlers', self.sub_crawlers)
                    db_handler.update_system_config('game_list', list(self.cafe_list.keys()))
                    db_handler.update_system_config('night_report_games', self.night_report_games)
                    db_handler.update_system_config('ccu_games', self.ccu_games)
                    db_handler.update_system_config('tab_config', self.tab_config)
                    # logger.info("[SQLITE] 시트 설정을 로컬 DB에 동기화 완료")
                except Exception as e:
                    logger.warning(f"[SQLITE] 설정 동기화 실패: {e}")

                # 변경 사항이 있으면 시그널 발송 (GUI 갱신용)
                if changes and hasattr(self, 'signals') and hasattr(self.signals, 'config_updated'):
                    self.signals.config_updated.emit(changes)
                
                return changes

            except Exception as e:
                # [v6.6.464] 점진적 backoff + 429 우선 처리.
                err_str = str(e)
                is_quota = ('429' in err_str) or ('quota' in err_str.lower()) or ('exhausted' in err_str.lower())

                self._log(f"설정 로드 실패 (계정 {self.current_account_index}, 시도 {attempt+1}): {e}", 'warning', 'CONFIG')
                self.current_account_index = (self.current_account_index + 1) % len(SERVICE_ACCOUNTS)

                # 429 / quota 감지 시 즉시 2차(2분) 단계부터 시작 — 분당 카운터 회복 시간 확보
                if is_quota and attempt < 2:
                    attempt = 2

                # 점진적 backoff: 5 → 30 → 120 → 600 → 1800 → 3600 (cap, 1시간)
                if attempt < len(_RETRY_BACKOFF_SEQ):
                    base_sleep = _RETRY_BACKOFF_SEQ[attempt]
                else:
                    base_sleep = _RETRY_MAX_SLEEP

                # jitter ±15% — 다중 프로세스(로컬 + 운영 컨테이너) 동시 깨어남 방지
                sleep_sec = base_sleep * random.uniform(0.85, 1.15)

                tag = ' [429-quota]' if is_quota else ''
                self._log(
                    f"설정 로드 재시도 대기 ({sleep_sec:.0f}초{tag}, 시도 {attempt+1}→{attempt+2})",
                    'info', 'CONFIG'
                )
                time.sleep(sleep_sec)
                attempt += 1

    def _send_kakaowork_alert(self, articles: List[Dict]):
        """카카오워크로 알림 메시지를 전송합니다."""
        for article in articles:
            is_super = article.get('is_super_keyword')
            game_name = article.get('game', 'Unknown')
            title = article.get('title', '')
            keywords = ", ".join(article.get('detected_keywords', []))
            
            # 일시 포맷 변경 (YYYY-MM-DD HH:MM)
            display_date = article.get('date', '')
            ts_str = str(article.get('timestamp', '')).strip()
            if ts_str:
                try:
                    dt = pd.to_datetime(ts_str).to_pydatetime()
                    display_date = dt.strftime('%Y-%m-%d %H:%M')
                except Exception:
                    pass
            
            # 카카오워크 블록형 메시지 구성
            if is_super:
                header_text = f"[키워드] {keywords}"
                style = "red"
            else:
                header_text = f"[{game_name}] 알림"
                style = "blue"
            
            # 카카오워크 헤더 20자 제한 대응
            if len(header_text) > 20:
                header_text = header_text[:19] + "…"
            
            blocks = [
                {"type": "header", "text": header_text, "style": style},
                {
                    "type": "description",
                    "term": "게임",
                    "content": {"type": "text", "text": f"**{game_name}**", "markdown": True},
                    "accent": True
                },
                {
                    "type": "description",
                    "term": "일시",
                    "content": {"type": "text", "text": display_date, "markdown": True},
                    "accent": False
                },
                {
                    "type": "description",
                    "term": "키워드",
                    "content": {"type": "text", "text": f"**{keywords}**", "markdown": True},
                    "accent": True
                },
                {
                    "type": "description",
                    "term": "글 제목",
                    "content": {"type": "text", "text": f"**{title}**", "markdown": True},
                    "accent": False
                },
                {
                    "type": "description",
                    "term": "링크",
                    "content": {"type": "text", "text": article.get('link', ''), "markdown": True},
                    "accent": False
                },
                {"type": "divider"},
                {
                    "type": "button",
                    "text": "게시글 확인",
                    "style": "primary",
                    "action_type": "open_system_browser",
                    "value": article.get('link', '')
                }
            ]

            text = f"[{game_name}] {title}"
            # [v6.6.527] trace_meta — 응답코드/실패 사유까지 ring buffer 에 기록
            _trace_meta = {
                'kind': 'SUPER', 'game': game_name, 'title': title,
                'link': article.get('link', ''),
            }
            NotificationManager().send_keyword_alert(text, blocks, trace_meta=_trace_meta)

    def _send_kakaowork_official_alert(self, articles: List[Dict]):
        """다음 카페 공지 알림 (OFFICIAL 키워드 매칭) 카카오워크 전송."""
        for article in articles:
            game_name = article.get('game', 'Unknown')
            title = article.get('title', '')
            keywords = ", ".join(article.get('detected_official_keywords', []))

            # 일시 포맷 (YYYY-MM-DD HH:MM)
            display_date = article.get('date', '')
            ts_str = str(article.get('timestamp', '')).strip()
            if ts_str:
                try:
                    dt = pd.to_datetime(ts_str).to_pydatetime()
                    display_date = dt.strftime('%Y-%m-%d %H:%M')
                except Exception:
                    pass

            header_text = f"[공지] {game_name}"
            # 카카오워크 헤더 20자 제한 대응
            if len(header_text) > 20:
                header_text = header_text[:19] + "…"

            blocks = [
                {"type": "header", "text": header_text, "style": "yellow"},
                {
                    "type": "description",
                    "term": "게임",
                    "content": {"type": "text", "text": f"**{game_name}**", "markdown": True},
                    "accent": True
                },
                {
                    "type": "description",
                    "term": "일시",
                    "content": {"type": "text", "text": display_date, "markdown": True},
                    "accent": False
                },
                {
                    "type": "description",
                    "term": "키워드",
                    "content": {"type": "text", "text": f"**{keywords}**", "markdown": True},
                    "accent": True
                },
                {
                    "type": "description",
                    "term": "글 제목",
                    "content": {"type": "text", "text": f"**{title}**", "markdown": True},
                    "accent": False
                },
                {
                    "type": "description",
                    "term": "링크",
                    "content": {"type": "text", "text": article.get('link', ''), "markdown": True},
                    "accent": False
                },
                {"type": "divider"},
                {
                    "type": "button",
                    "text": "공지 확인",
                    "style": "primary",
                    "action_type": "open_system_browser",
                    "value": article.get('link', '')
                }
            ]

            text = f"[공지][{game_name}] {title}"
            # [v6.6.527] OFFICIAL 도 trace_meta 동봉 — 응답코드까지 추적
            _trace_meta = {
                'kind': 'OFFICIAL', 'game': game_name, 'title': title,
                'link': article.get('link', ''),
            }
            NotificationManager().send_official_alert(text, blocks, trace_meta=_trace_meta)

    def _emit_status(self):
        """Triggers the emission of the global status via the callback."""
        if self.status_emitter:
            self.status_emitter()

    def _enrich_articles_for_web(self, articles, update_state=False):
        """
        웹/데스크톱 전송용 데이터 가공 — `source_community` 파생이 유일한 책임.

        [v6.7.455] is_new 주입 제거 (컬럼 은퇴). 이 함수는 원래 is_new 주입이 주 목적이었고
        v6.7.59 가 그걸 위해 메모리 핫셋(80k row scan 제거)까지 붙였는데, 신규 표시가
        도착 잔광(클라이언트 diff)으로 일원화되면서 소비처가 사라졌다. 핫셋·cold fallback·
        PERF 계측이 함께 제거됐고, 남은 것은 아래 link/source 기반 커뮤니티 판별뿐이다.

        [v6.7.456] 입력에 실려온 `is_new` 를 여기서 떨군다. v455 가 주입 줄만 지웠는데,
        `articles.py` 의 `?games=` 경로는 `get_latest_posts()` 즉 **`SELECT *`** 결과를 넘기므로
        DB 컬럼값이 `a.copy()` 를 그대로 통과해 API 응답에 실렸다(구 코드는 같은 자리에서
        덮어쓰기라 안 보이던 것). 값은 마킹 중단 + 부팅 리셋 제거로 **영구 동결**이라 무의미하다.
        웹 레이어를 지나는 dict 에서 이 키가 사라지는 지점을 여기 한 곳으로 고정한다.
        """
        if not articles:
            return []

        enriched = []
        for a in articles:
            new_a = a.copy()
            new_a.pop('is_new', None)
            source_raw = str(a.get('source', '')).upper()
            link = str(a.get('link', '') or a.get('comment_link', ''))

            # 커뮤니티 그룹 판별
            # [v6.6.490] OFFICIAL_WEB 분기 안전망 — official_watcher 회수 후에도 SQLite 잔존 데이터가
            # UI 에 표시될 때 link URL 기반 fallback 으로 'DC' 덮어쓰기 되지 않도록 보존.
            # [v6.7.114] NAVER 분기 추가 — source='NAVER' 또는 cafe.naver.com link.
            #   v6.7.113 commit 시 JS ternary 14곳은 박았으나 본 backend enrich 의 NAVER 분기 누락 →
            #   NAVER article 이 else fallback 'DC' 로 변환되어 frontend 가 DC 푸른 배지로 표시되던 결함 fix.
            if source_raw == 'OFFICIAL_WEB':
                comm = 'OFFICIAL_WEB'
            elif source_raw == 'NAVER' or 'cafe.naver.com' in link:
                comm = 'NAVER'
            elif 'dcinside.com' in link:
                comm = 'DC'
            elif 'cafe.daum.net' in link or 'DAUM' in source_raw or '카페' in source_raw:
                comm = 'DAUM'
            else:
                comm = 'DC'

            new_a['source_community'] = comm
            enriched.append(new_a)

        return enriched



    def _enrich_game_articles_for_web(self, articles_by_game):
        """웹 전송용 게임별 데이터 가공"""
        enriched = {}
        for game, sources in articles_by_game.items():
            enriched[game] = {}
            for source, articles in sources.items():
                if isinstance(articles, list):
                    enriched[game][source] = self._enrich_articles_for_web(articles)
                else:
                    enriched[game][source] = articles
            if 'total' in sources:
                enriched[game]['total'] = sources['total']
        return enriched

    def _datetime_from_string(self, date_str: str) -> datetime:
        """다양한 날짜 문자열을 datetime 객체로 변환"""
        # KST 기준으로 현재 시간 설정 (서버 시간대와 무관하게 정확한 날짜 계산)
        # 기준 시간: 이 함수가 호출되는 시점 (크롤링 수행 시점)
        today = datetime.now(ZoneInfo("Asia/Seoul"))
        date_str = str(date_str).strip()
        
        # 'HH:MM' 또는 'YY.MM.DD HH:MM' 형식
        if ':' in date_str: 
            # 공백이 없으면 시간만 있는 것으로 간주 (예: "9:15" 또는 "09:15")
            if ' ' not in date_str:
                try:
                    h_str, m_str = date_str.split(':')[:2]
                    dt = today.replace(hour=int(h_str), minute=int(m_str), second=0, microsecond=0)

                    # [v6.6.102] 자정 경계 보정 (엄격 모드)
                    # 카페가 미래 시각의 글을 보여줄 리 없으므로, 현재 시각보다 미래로 파싱된 경우는
                    # 반드시 '어제'의 시간으로 간주한다. 단, 크롤러-카페 간 시계 오차를 고려해
                    # 2분 버퍼를 두어 "방금 올라온 오늘 글"이 어제로 오판되는 것을 방지한다.
                    # (기존 12시간 룰은 자정 직후 어제 오전 글이 오늘로 찍히는 버그 원인이었음)
                    if dt > today + timedelta(minutes=2):
                        dt -= timedelta(days=1)

                    return dt.replace(tzinfo=None) # 다른 로직과의 호환성을 위해 naive datetime 반환
                except ValueError:
                    self._log(f"시간 파싱 오류 (HH:MM): {date_str}", 'error', 'CRAWLER')
                    return today # 안전한 대체값
            else: # YY.MM.DD HH:MM 또는 MM.DD HH:MM
                try:
                    date_part, time_part = date_str.split(' ', 1)
                    h_str, m_str = time_part.split(':')[:2]
                    
                    if date_part.count('.') == 2: # YY.MM.DD
                        parts = date_part.split('.')
                        year = int(f"20{parts[0]}") if len(parts[0]) == 2 else int(parts[0])
                        month = int(parts[1])
                        day = int(parts[2])
                    elif date_part.count('.') == 1: # MM.DD
                        parts = date_part.split('.')
                        year = today.year
                        month = int(parts[0])
                        day = int(parts[1])
                    else:
                        raise ValueError("Unknown date format")
                    
                    return datetime(year, month, day, int(h_str), int(m_str))
                except Exception:
                    self._log(f"날짜-시간 파싱 오류 (YY.MM.DD HH:MM / MM.DD HH:MM): {date_str}", 'error', 'CRAWLER')
                    return today # 안전한 대체값
        # 'YY.MM.DD' (DC 갤러리, 시간 정보 없음)
        else: 
            try:
                if date_str.count('.') >= 2: # YY.MM.DD
                    parts = date_str.split('.')
                    year_str = parts[0]
                    year = int(f"20{year_str}") if len(year_str) == 2 else int(year_str)
                    month = int(parts[1])
                    day = int(parts[2])
                    return datetime(year, month, day, 0, 0)
                else: # MM.DD (현재 연도 가정)
                    parts = date_str.split('.')
                    if len(parts) == 2:
                        return datetime(today.year, int(parts[0]), int(parts[1]), 0, 0)
                    return today
            except Exception:
                self._log(f"날짜 파싱 오류 (YY.MM.DD / MM.DD): {date_str}", 'error', 'CRAWLER')
                return today # 안전한 대체값

    def _log(self, message: str, level: str = 'info', tag: str = ''):
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        log_entry = {'time': timestamp, 'level': level, 'tag': tag, 'message': message}
        
        # 로그 리스트 접근 시 락 사용 (스레드 안전성 확보)
        with self.log_lock:
            self.logs.append(log_entry)
            self.log_version += 1 # 로그 변경 시 버전 증가
            if len(self.logs) > 10000:
                self.logs.pop(0)
        
        # 콘솔 출력 포맷을 사용자 요청에 맞게 변경
        console_timestamp_part = datetime.now().strftime('%H:%M:%S') # User wanted time at the end
        console_message = f"{f'[{tag}] ' if tag else ''}{message} {console_timestamp_part}"
        logger.log(getattr(logging, level.upper(), logging.INFO), console_message)

        # 여기서 직접 emit하지 않고 _log_emission_loop에서 주기적으로 전송함
        # (빈번한 전송으로 인한 클라이언트 렌더링 깜빡임 방지)

    def _next_log_payload(self, last_sent_version):
        """[v6.7.731] 마지막 전송 버전 이후의 로그 증분 payload 를 계산(결정적 테스트 가능 단위로 분리).

        증분 기준을 `len(self.logs)`(상한 10000 에서 pop(0) 시 길이가 고정) 에서 단조 증가
        `self.log_version`(append 마다 +1, pop 과 무관) 으로 바꾼 것이 핵심. 버퍼가 가득 차 pop(0)
        이 시작되면 길이가 10000 에 고정돼 `current==last` 로 영구 멈추고 `logs[last:]` 도 빈 배열이
        되어 로그 탭이 멎던 결함을 고친다. 신규분이 버퍼에 남은 양을 넘으면(clear 포함) 전체 재전송.
        반환: (payload 또는 None, 새 last_sent_version). `log_lock` 안에서 호출할 것.
        """
        current_version = self.log_version
        if current_version == last_sent_version:
            return None, last_sent_version
        n_new = current_version - last_sent_version
        if n_new < 0 or n_new >= len(self.logs):
            snapshot = list(self.logs[-10000:])
            return {'logs': snapshot, 'count': len(snapshot), 'reset': True}, current_version
        # 버퍼 꼬리에서 신규분만 (pop 으로 앞이 밀려도 신규는 항상 꼬리에 있다)
        delta = list(self.logs[-n_new:])
        return {'delta': delta, 'count': current_version}, current_version

    def _log_emission_loop(self):
        """로그를 주기적으로 모아서 전송하는 백그라운드 루프 (delta 전송)."""
        last_sent_version = 0
        while getattr(self, '_log_thread_active', False):
            time.sleep(1.0)
            if self.socketio:
                with self.log_lock:
                    payload, last_sent_version = self._next_log_payload(last_sent_version)
                if payload:
                    # [v6.7.731] 운영 로그는 admin room 으로만 전송 - 익명 포함 전체 broadcast 금지(/api/logs admin 가드와 정합).
                    self.socketio.emit('logs_update', payload, room='admin')

    def _create_local_driver(self):
        """DC 크롤링 전용 독립 드라이버 생성 (메인 드라이버와 충돌 방지)"""
        try:
            options = Options()
            options.page_load_strategy = 'eager'
            options.add_argument(f"user-agent={random.choice(USER_AGENTS)}")
            
            options.add_argument("--window-size=1920,1080")
            options.add_argument("--disable-gpu")
            options.add_argument("--lang=ko_KR")
            options.add_experimental_option("prefs", {"profile.managed_default_content_settings.images": 2})
            options.add_experimental_option('excludeSwitches', ['enable-logging'])

            options.add_argument("--disable-extensions")
            options.add_argument("--mute-audio")
            options.add_argument("--disable-background-networking")
            options.add_argument("--disable-default-apps")
            options.add_argument("--disable-sync")
            options.add_argument("--disable-translate")
            options.add_argument("--disable-infobars")
            options.add_argument("--dns-prefetch-disable")
            options.add_argument("--no-first-run")
            
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option('useAutomationExtension', False)

            if HEADLESS:
                options.add_argument("--headless=new")
                options.add_argument("--disable-gpu")

            # [서버 안정성 강화] 헤드리스 모드에서는 OS 무관하게 필수 (Windows headless도 동일)
            if HEADLESS or sys.platform != 'win32':
                options.add_argument("--no-sandbox")
                options.add_argument("--disable-dev-shm-usage")

            options.add_argument("--disable-software-rasterizer")
            
            # 드라이버 생성 재시도 로직 추가 (일시적 크래시 방지)
            # [v6.6.415] webdriver_manager 제거 → Selenium 4.6+ 내장 Selenium Manager 위임 (EDR 일관성 / dropper 패턴 회피)
            creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            for attempt in range(3):
                try:
                    driver = webdriver.Chrome(service=Service(log_output=os.devnull, service_args=['--log-level=OFF'], creationflags=creation_flags), options=options)
                    
                    # 페이지 로드 타임아웃 설정 (무한 대기 방지)
                    wait_time = self.settings.get('WEBDRIVER_WAIT_TIME', WEBDRIVER_WAIT_TIME)
                    driver.set_page_load_timeout(wait_time)
                    driver.set_script_timeout(wait_time)
                    
                    return driver
                except Exception as e:
                    if attempt < 2:
                        time.sleep(2)
                        continue
                    self._log(f"DC 전용 드라이버 생성 실패: {e}", 'error', 'SYSTEM')
                    return None
        except Exception as e:
            self._log(f"드라이버 옵션 설정 중 오류: {e}", 'error', 'SYSTEM')
            return None

    def _get_driver(self):
        if self.driver:
            return self.driver
        
        self.status = "웹 드라이버 초기화 중..."
        self._emit_status()
        
        self._log("웹 드라이버 초기화 중...", 'info', 'SYSTEM')
        options = Options()
        options.page_load_strategy = 'eager' # [최적화] 리소스 로딩 대기 최소화 (DOM 로드 후 즉시 실행)

        # [안정성 테스트] 실행 환경의 보안 소프트웨어와 충돌하지 않게 옵션을 최소화합니다.
        options.add_argument(f"user-agent={random.choice(USER_AGENTS)}")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--disable-gpu")
        options.add_argument("--lang=ko_KR")
        options.add_experimental_option("prefs", {"profile.managed_default_content_settings.images": 2})
        options.add_experimental_option('excludeSwitches', ['enable-logging']) # DevTools 로그 숨김

        # [안정성 강화] 리소스 사용량을 줄이기 위한 추가 옵션
        options.add_argument("--disable-extensions")
        options.add_argument("--mute-audio")
        options.add_argument("--disable-background-networking")
        options.add_argument("--disable-default-apps")
        options.add_argument("--disable-sync")
        options.add_argument("--disable-translate")
        # [최적화] 추가 성능 향상 옵션
        options.add_argument("--disable-infobars")
        options.add_argument("--dns-prefetch-disable")
        options.add_argument("--no-first-run")
        
        # 봇 탐지 회피 옵션 (AutomationControlled 비활성화)
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)

        if HEADLESS:
            options.add_argument("--headless=new")
            options.add_argument("--disable-gpu") # 헤드리스 시 명시

        # [서버 안정성 강화] 헤드리스 모드에서는 OS 무관하게 필수 (Windows headless도 동일)
        if HEADLESS or sys.platform != 'win32':
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            
        options.add_argument("--disable-software-rasterizer")

        creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        # [v6.6.415] webdriver_manager 제거 → Selenium Manager 위임
        driver = webdriver.Chrome(service=Service(log_output=os.devnull, service_args=['--log-level=OFF'], creationflags=creation_flags), options=options)
        
        # 페이지 로드 타임아웃 설정 (무한 대기 방지)
        # 설정된 시간(45초) 내에 로드되지 않으면 TimeoutException 발생 -> 재시도 로직 작동
        wait_time = self.settings.get('WEBDRIVER_WAIT_TIME', WEBDRIVER_WAIT_TIME)
        driver.set_page_load_timeout(wait_time)
        driver.set_script_timeout(wait_time)
        
        self._log("웹 드라이버 초기화 완료", 'info', 'SYSTEM')
        return driver

    def start(self):
        if self.is_running:
            self._log("크롤러가 이미 실행 중입니다", 'warning', 'SYSTEM')
            return
        self.is_running = True
        # [v6.7.128] 시작 시 user_initiated_stop 플래그 reset — 이전 정지가 사용자 의도였더라도
        # 다시 시작되면 다음 healthcheck 사이클은 정상 자동복구 보호 범위.
        self.user_initiated_stop = False
        # [v6.7.289] 재시작 시 hang 타임스탬프 리셋 — __init__(부팅) 시각이 남아 재시작 직후
        # 첫 루프 반복/첫 성공 크롤 전에 헬스체크가 돌면 false hang 판정하는 창(window) 차단.
        _now_ts = time.time()
        self.last_loop_heartbeat = _now_ts
        self.last_successful_crawl_ts = _now_ts
        # [v6.7.291] 레인별 hang 신호도 활성 레인 전체를 now 로 시드 — 부팅/재시작 직후
        # 첫 성공 크롤 전에 헬스체크가 돌아도 false hang 판정 안 나게 (v6.7.289 와 동일 원리).
        self.lane_last_success = {
            task.get('lane', 'DEFAULT'): _now_ts
            for task in self.crawler_tasks.values() if task['enabled']
        }
        # [v6.7.551] 태스크 신호도 같은 원리로 시드 - 재시작 직후 첫 성공 전에 헬스체크가
        # 돌아도 false hang 이 안 나게. (시드 안 하고 '없으면 미검사' 로 두면 재시작 전의
        # 낡은 값이 남아 첫 사이클 전에 오탐한다.)
        self.task_last_success = {
            key: _now_ts
            for key, task in self.crawler_tasks.items() if task['enabled']
        }
        self.status = "실행 중 (시작됨)" # 상태 메시지 명확화
        self._log("메인 크롤러가 시작되었습니다", 'info', 'SYSTEM')

        self._emit_status()

        # 락 관리 스레드 시작
        if not self.lock_thread or not self.lock_thread.is_alive():
            self.lock_thread = threading.Thread(target=self._status_maintenance_loop, daemon=True)
            self.lock_thread.start()

        if not self.crawler_thread or not self.crawler_thread.is_alive():
            # [v6.6.341] _supervised_monitoring_loop 로 교체 — _monitoring_loop 외곽에서
            # 예외 발생 시 카카오워크 알림 + 10초 후 자동 재진입. 스레드 사망 방지.
            self.crawler_thread = threading.Thread(target=self._supervised_monitoring_loop, daemon=True)
            self.crawler_thread.start()

    def stop(self, user_initiated: bool = False):
        """[v6.7.128] user_initiated 파라미터 추가.

        - True  : `/api/crawler/stop` 라우트 (사용자 컨트롤바 클릭). healthcheck 가 자동복구 안 함.
        - False : shutdown handler (SIGTERM) / 내부 cleanup / 부수효과. healthcheck 가 1분 주기로 자동 재시작.

        caller traceback 을 INFO 로 박제 — 다음 silent stop 발생 시 호출 경로 즉시 식별.
        """
        import traceback as _tb
        # 최근 5 frame 만 (gunicorn/flask 내부 노이즈 컷)
        caller_chain = ''.join(_tb.format_stack(limit=8)[:-1]).strip()
        self._log(
            f"[STOP] user_initiated={user_initiated} caller=\n{caller_chain}",
            'info', 'SYSTEM'
        )

        self.is_running = False
        self.user_initiated_stop = bool(user_initiated)
        self.status = "중지됨"
        self._log("메인 크롤러 중지 요청됨.", 'info', 'SYSTEM')

        # 드라이버 직접 종료 로직 제거.
        # _monitoring_loop 스레드가 스스로 정리하도록 유도합니다.
        # 데드락 우려는 driver.set_page_load_timeout() 설정으로 완화되었습니다.
        # 스레드가 종료되면서 루프 끝에 있는 정리 로직이 호출됩니다.
        self._emit_status()

    def get_lane_hang_thresholds(self, base_sec):
        """[v6.7.291] 레인별 hang 판정 임계(초) — check_main_crawler_health(scheduler.py)가 호출.

        레인마다 태스크 수·주기가 달라 단일 임계는 저빈도 레인(HTTP: 180~300s 주기)에서 오탐.
        임계 = max(base, 레인 내 최대 interval_max x 2 + TASK_EXECUTOR_TIMEOUT x 2) —
        일시 예외/timeout 2연속까지는 흡수하고 그 이상 정체만 hang 으로 판정.

        **임계값을 여기 숫자로 적지 않는다** - `interval_max` 가 구글시트 hot reload 대상이라
        런타임에 움직인다. 구 주석의 "CAFE/DC: 720s" 는 2026-08-05 에 DAUM 주기가 5~15초 →
        900~1200초로 바뀐 뒤에도 그대로 남아 **실제(CAFE 2,880초)의 4분의 1** 을 가리키고
        있었다(2026-08-15 실측). 현재값이 필요하면 이 함수를 호출해서 볼 것.
        """
        thresholds = {}
        for task in self.crawler_tasks.values():
            if not task['enabled']:
                continue
            lane = task.get('lane', 'DEFAULT')
            cand = task['interval_max'] * 2 + TASK_EXECUTOR_TIMEOUT * 2
            thresholds[lane] = max(thresholds.get(lane, 0), base_sec, cand)
        return thresholds

    def get_task_hang_thresholds(self, base_sec):
        """[v6.7.551] 태스크별 hang 판정 임계(초) - check_main_crawler_health 가 호출.

        레인 임계와 **같은 공식**이되 max 를 레인이 아니라 태스크로 잡는다. 레인 임계는 그 레인의
        가장 느린 태스크에 맞춰지므로(CAFE = DAUM 기준), 같은 레인의 빠른 태스크(NAVER)가 죽어도
        느린 쪽 임계 안에 숨는다. 반대로 느린 태스크(DAUM)는 빠른 태스크가 레인 시계를 계속
        갱신해 아예 판정 대상이 되지 못한다 - **양방향 사각지대라 태스크 축이 따로 필요하다.**

        2026-08-15 실측 기준 산출값: DAUM 2,880초 / NAVER 720초 / DC 600초 / OFFICIAL 1,080초
        (DAUM 만 시트 hot reload 대상이라 이 값도 시트를 따라 움직인다 - 위 함수 주석 참조).
        """
        return {
            key: max(base_sec, task['interval_max'] * 2 + TASK_EXECUTOR_TIMEOUT * 2)
            for key, task in self.crawler_tasks.items() if task['enabled']
        }

    def _get_lane_executor(self, lane):
        """[v6.7.291] 레인 전용 executor lazy 생성 — max_workers=1 로 레인 내 직렬 보장.

        레인 내 태스크가 hang 하면 그 레인 worker 만 점유되어 해당 레인만 마비되고
        다른 레인은 계속 돈다 (v6.7.288 잔여 한계의 blast radius 를 전체 → 레인으로 축소).
        """
        with self._lane_executors_lock:
            ex = self._lane_executors.get(lane)
            if ex is None:
                ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"lane-{lane}")
                self._lane_executors[lane] = ex
            return ex

    def _run_task_in_executor(self, task_func, *args, lane=None):
        """작업을 큐(executor)에 넣고 완료될 때까지 대기합니다.

        [v6.7.288] future.result() 가 무기한 대기였던 게 사고 원인(2026-07-01 11:50~,
        메인 크롤러 10시간+ 무응답 — task_func 내부 hang 시 이 한 줄에서 _monitoring_loop
        전체가 영구 정지, thread.is_alive()/is_running 은 계속 True 라 헬스체크도 못 잡음).
        TASK_EXECUTOR_TIMEOUT 상한을 두어 초과 시 TimeoutError 를 그대로 올린다 —
        호출부(_lane_loop)의 태스크별 try/except 가 이번 사이클만 실패 처리하고 다음
        사이클로 진행(태스크 내부 스레드 자체는 여전히 hang 상태로 executor worker 를
        점유할 수 있으나, 최소 루프 자체는 죽지 않음).

        [v6.7.291] lane 지정 시 해당 레인 전용 executor(max_workers=1) 사용 — 레인끼리
        병렬, 레인 안은 직렬. 미지정 시 기존 주입 executor 경유 (하위호환 fallback).
        """
        executor = self._get_lane_executor(lane) if lane else self.executor
        if executor:
            # 실행 시작 시 '대기 중'으로 상태가 변경되어 혼란을 주는 코드 제거
            # 호출 전 설정된 '실행 중 (OOO)' 상태가 유지되도록 함
            try:
                # 큐에 작업을 제출하고, 내 차례가 되어 작업이 끝날 때까지 블로킹(대기)
                future = executor.submit(task_func, *args)
                return future.result(timeout=TASK_EXECUTOR_TIMEOUT)
            except FuturesTimeoutError:
                raise FuturesTimeoutError(
                    f"작업 타임아웃 ({TASK_EXECUTOR_TIMEOUT}s 초과) — 내부 hang 의심"
                )
            except Exception as e:
                self._log(f"작업 큐 실행 중 오류: {e}", 'error', 'SYSTEM')
                return None
        else:
            # executor가 없으면 그냥 실행 (예외 처리)
            return task_func(*args)

    def _supervised_monitoring_loop(self):
        """[v6.6.341] `_monitoring_loop` self-healing 래퍼.

        메인 크롤러 스레드가 내부 예외로 죽지 않도록 외곽을 try/except 로 감싸고
        예외 발생 시 카카오워크 알림 + 10초 대기 후 재진입. `is_running` 이 False 면
        정상 종료 (사용자 Stop 요청).
        """
        cooldown = 10
        while self.is_running:
            try:
                self._monitoring_loop()
                # 정상 종료 (is_running=False 로 인한 while 탈출)
                return
            except Exception as e:
                self._log(f"[SUPERVISE] monitoring_loop 예외 → {cooldown}s 후 재진입: {e}", 'error', 'SYSTEM')
                try:
                    from notification_manager import NotificationManager
                    from zoneinfo import ZoneInfo
                    ts = datetime.now(ZoneInfo('Asia/Seoul')).strftime('%Y-%m-%d %H:%M:%S')
                    blocks = [
                        {"type": "header", "text": "크롤러 재시작 — 메인 크롤러", "style": "yellow"},
                        {"type": "text", "text": f"*예외:* `{str(e)[:300]}`\n*시각:* {ts}", "markdown": True},
                    ]
                    NotificationManager().send_kakaowork("크롤러 재시작 — 메인", blocks)
                except Exception as _alert_err:
                    self._log(f"[SUPERVISE] 알림 전송 실패: {_alert_err}", 'warning', 'SYSTEM')
                time.sleep(cooldown)
                # 재진입 — is_running 유지되면 while 다시 돎

    def _monitoring_loop(self):
        """[v6.7.291] 레인 병렬 구조 — 이 스레드는 감독(supervisor) 역할만 수행.

        기존(v6.7.289 이하): 이 스레드가 4태스크를 순차 for 로 직접 돌려 완전 직렬 —
        한 태스크의 장기화/hang 이 나머지 태스크 전부를 지연시켜(최악 4x240s=960s/라운드)
        명목 주기(70~120s)가 허구화됐다. 병렬화하되 Selenium WebDriver 는 thread-safe 하지
        않으므로 "드라이버(세션) 경계 = 병렬 경계" 원칙으로 레인을 나눈다:
          CAFE (DAUM+NAVER: self.driver 공유 — 레인 내 직렬로 driver race 원천 차단)
          DC   (dc_driver 전용 지속성 세션)
          HTTP (OFFICIAL: requests+BS4, 드라이버 미사용)
        신규 Chrome 인스턴스 0개 — 기존 2 드라이버 + requests 경계를 그대로 병렬화.
        naver_cafe_watcher(imt-ncw)의 "네트워크 대기만 병렬, 공유 상태 변경은 각 스레드
        직렬" 원칙 채용 — 수집 결과 쓰기는 seen_links(self.lock)/SQLite(thread-local+WAL)/
        시트(_network_lock) 가 각각 기존 lock 으로 방어되어 있음.
        """
        # 레인별 태스크 그룹핑 (enabled 만) — dict 순서 보존이라 레인 내 순서 = 등록 순서
        lanes = {}
        for key, task in self.crawler_tasks.items():
            if task['enabled']:
                lanes.setdefault(task.get('lane', 'DEFAULT'), []).append((key, task))

        # 레인 스레드 기동
        self._lane_threads = {}
        for lane, tasks in lanes.items():
            t = threading.Thread(
                target=self._lane_loop, args=(lane, tasks),
                name=f"crawler-lane-{lane}", daemon=True
            )
            self._lane_threads[lane] = t
            t.start()
        self._log(f"레인 병렬 기동 — {', '.join(f'{ln}({len(ts)})' for ln, ts in lanes.items())}", 'info', 'SYSTEM')

        self._emit_status()

        # 감독 루프 — config 자동 갱신 + heartbeat 만. 태스크 실행은 레인 스레드가 담당.
        while self.is_running:
            # [v6.7.288] 루프 반복마다 heartbeat 갱신 (진단용 breadcrumb — 판정은 lane_last_success)
            self.last_loop_heartbeat = time.time()

            # [v6.6.231] 날짜 변경 시 purge_old_articles 호출 블록 제거
            # (타임스탬프 파싱 실패로 특정 커뮤니티 글만 선택적으로 삭제되던 버그 원인)

            # 2분마다 설정(Config) 자동 갱신 (실행 중일 때도 반영)
            if time.time() - self.last_config_check_time > 120:
                self.last_config_check_time = time.time()
                threading.Thread(target=self.load_config_from_sheet, daemon=True).start()

            time.sleep(1)

        # 종료 — 레인 스레드들이 is_running=False 를 보고 내려올 때까지 대기 후 드라이버 정리.
        # (드라이버는 레인 태스크가 사용 중일 수 있어, join 전에 quit 하면 use-after-free 성 오류)
        for lane, t in self._lane_threads.items():
            t.join(timeout=15)
            if t.is_alive():
                self._log(f"[{lane}] 레인 스레드 종료 대기 초과(15s) — 드라이버 정리 강행", 'warning', 'SYSTEM')

        # 스레드가 종료되기 직전에 자신이 생성한 드라이버 리소스를 정리합니다.
        # 이 로직이 없으면 앱 종료 시 충돌이 발생할 수 있습니다.
        if self.driver:
            try:
                self.driver.quit()
                self._log("메인 크롤러(Daum) 웹 드라이버가 정상적으로 종료되었습니다.", 'info', 'SYSTEM')
            except Exception as e:
                self._log(f"메인 크롤러(Daum) 웹 드라이버 종료 중 오류 발생: {e}", 'error', 'SYSTEM')
            finally:
                self.driver = None

        # DC 드라이버도 함께 정리
        if self.dc_driver:
            try:
                self.dc_driver.quit()
                self._log("메인 크롤러(DC) 웹 드라이버가 정상적으로 종료되었습니다.", 'info', 'SYSTEM')
            except Exception as e:
                self._log(f"메인 크롤러(DC) 웹 드라이버 종료 중 오류 발생: {e}", 'error', 'SYSTEM')
            finally:
                self.dc_driver = None

    def _lane_loop(self, lane, tasks):
        """[v6.7.291] 한 레인의 태스크들을 자기 페이스로 도는 루프 (레인당 스레드 1개).

        _supervised_monitoring_loop 와 동일한 self-heal 정신 — 레인 스레드 예외는 감독
        스레드의 try/except 가 못 잡으므로 자체 재진입(10s 쿨다운)을 내장한다.
        """
        while self.is_running:
            try:
                self._lane_loop_inner(lane, tasks)
                return  # is_running=False 로 인한 정상 종료
            except Exception as e:
                self._log(f"[{lane}] 레인 루프 예외 → 10s 후 재진입: {e}", 'error', 'SYSTEM')
                time.sleep(10)

    def _lane_loop_inner(self, lane, tasks):
        """레인 본체 — due 태스크를 레인 전용 executor(직렬)로 실행. tasks = [(key, task), ...]"""
        # 다음 실행 시간 초기화 — 첫 실행도 interval 랜덤 지연 (기존 동작 유지 + 레인 간 자연 스태거)
        now = time.time()
        for key, task in tasks:
            task['next_run'] = now + random.uniform(task['interval_min'], task['interval_max'])

        while self.is_running:
            now = time.time()
            for key, task in tasks:
                if not self.is_running:
                    return
                if now < task['next_run']:
                    continue

                # [v6.7.73] F-7 record_crawler_cycle hook — DAUM/DC 메인 cycle 성공/실패 추적
                _cycle_success = False
                try:
                    self._set_task_active(task['status_label'], True)
                    # [신규] 이번 주기(실행 직전) 시간을 기록하여 배경색 표시 기준으로 사용
                    self.current_cycle_start_time = datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)

                    self._run_task_in_executor(task['func'], lane=lane)
                    _cycle_success = True
                    # [v6.7.289] 태스크가 timeout 없이 반환 = 크롤 머신 응답 정상(내부에서
                    # 예외를 삼켜 수집 0건이어도 '반환'했으면 hang 아님). timeout(hang) 시엔
                    # _run_task_in_executor 가 raise 라 미도달.
                    # [v6.7.291] 레인별 신호(lane_last_success)가 hang 판정 정본, 전역
                    # last_successful_crawl_ts 는 하위호환용 "임의 레인 성공" 신호로 유지.
                    _ts = time.time()
                    self.lane_last_success[lane] = _ts
                    self.task_last_success[key] = _ts   # [v6.7.551] 태스크 단위 신호
                    self.last_successful_crawl_ts = _ts

                    # [트리거] 메인 크롤링(카페/디시) 완료 후 댓글 크롤러 즉시 실행 신호 발송
                    if key in ('DAUM', 'DC'):
                        try:
                            from web_lib.globals import comment_trigger_event
                            if comment_trigger_event:
                                comment_trigger_event.set()
                        except Exception:
                            pass
                except Exception as e:
                    self._log(f"{task['status_label']} 크롤링 중 오류 발생: {e}", 'error', key)
                finally:
                    self._set_task_active(task['status_label'], False)
                    # [v6.7.73] DAUM/DC 메인 크롤러만 cycle 카운트 (다른 task 는 success_rate 노이즈)
                    if key in ('DAUM', 'DC'):
                        try:
                            from web_lib.bg.health_sampler import record_crawler_cycle as _rec
                            _rec(_cycle_success)
                        except Exception:
                            pass
                    # 오류 발생 여부와 관계없이 다음 크롤링 시간을 항상 설정
                    if self.is_running:
                        task['next_run'] = time.time() + random.uniform(task['interval_min'], task['interval_max'])
                        self._emit_status()

            time.sleep(1) # 1초마다 다음 작업 확인

    def _set_task_active(self, label, active):
        """[v6.7.291] 병렬 레인들의 status 문자열 합성 — 실행 중 태스크 라벨 집합 기반.

        기존 단일 루프의 '실행 중 (X)' ↔ '실행 대기 중' 토글을 레인별로 하면 서로 덮어쓰므로,
        집합에 넣고 빼면서 매번 전체를 재계산한다. (태스크 함수 내부의 self.status 직접 대입은
        잔존 — 다음 _set_task_active/_emit_status 에서 재계산되므로 일시 표기 흔들림만 허용)
        """
        with self._active_labels_lock:
            if active:
                self._active_task_labels.add(label)
            else:
                self._active_task_labels.discard(label)
            labels = sorted(self._active_task_labels)
        self.status = f"실행 중 ({', '.join(labels)})" if labels else "실행 대기 중"
        self._emit_status()

    def _crawl_all(self, initial: bool = False):
        if initial:
            self._log("메인 크롤러 초기 실행 시작", 'info', 'CRAWLER')
            self.status = "실행 중: 초기 데이터 수집 중..." # 초기 실행 상태 표시
            self._emit_status()
        
        # 등록된 모든 크롤러 순차 실행
        for key, task in self.crawler_tasks.items():
            if task['enabled']:
                try:
                    task['func']()
                except Exception as e:
                    self._log(f"초기 실행 중 {task['status_label']} 오류: {e}", 'error', key)

        if initial:
            self._log("메인 크롤러(카페/디시) 초기 실행 완료", 'info', 'CRAWLER')
            self.initial_crawl_done = True # 초기 크롤링 완료 표시

    def _crawl_daum_with_driver(self):
        """Daum 카페 크롤링을 위해 드라이버를 생성하고, 작업 후 종료합니다."""
        # 크롤링할 카페가 없으면 즉시 종료 (로그 및 드라이버 실행 방지)
        if not self.cafe_list:
            return

        # 3회 재시도 정책 적용 (실패 시 드라이버 종료 후 재시작)
        max_retries = 3
        for attempt in range(max_retries):
            try:
                if not self.is_running:
                    return

                # 1. 드라이버가 없거나 유효하지 않으면 생성
                if self.driver is None:
                    self.driver = self._get_driver()
                
                if not self.driver:
                    raise Exception("드라이버 생성 실패")

                # 2. 크롤링 수행
                self.status = "실행 중: Daum 카페 크롤링 진행 중"
                self._emit_status()
                
                self._crawl_daum(self.driver)
                break # 성공 시 루프 탈출

            except Exception as e:
                if not self.is_running:
                    return

                self._log(f"Daum 카페 크롤링 오류 ({attempt + 1}/{max_retries}): {e}", 'warning', '카페')
                
                # 실패 시 드라이버 확실히 종료
                if self.driver:
                    try: self.driver.quit()
                    except Exception: pass
                    self.driver = None
                
                if attempt < max_retries - 1:
                    self._log(f"5초 후 {attempt + 1}차 재시도합니다. (브라우저 재시작)", 'warning', '카페')
                    time.sleep(5)
                else:
                    self._log("Daum 카페 크롤링 최종 실패.", 'error', '카페')

    def _crawl_naver_with_driver(self):
        """[v6.7.113] 네이버 카페 크롤링 — DAUM 과 동일 self.driver 공유 + 3회 재시도.

        [활성화 gate] `Config_Games.NaverCafe_URL` 컬럼 비어있으면 자동 skip.
        [로그인 없음 — 2026-08-20 claim audit 정정] 종전 이 자리에 "[네이버 로그인 의존] 운영 환경의
        chrome profile 에 네이버 쿠키가 박혀 있어야 한다" 고 적혀 있었으나 **그런 프로필은 존재한
        적이 없다** — `_get_driver()` 에 `user-data-dir` 지정이 없어 매 기동 임시 프로필(쿠키 0)이다.
        지금 대상(배그 공식카페)은 목록이 비회원 공개라 그대로 동작한다. 회원제 카페를 넣으면 빈
        list → 빈 cycle 인데, 그건 "만료" 가 아니라 **처음부터 세션이 없는 것**이다.
        (본문 추출은 별개 경로 — `content_extractor` 가 회원 쿠키를 싣는다, v6.7.590~)
        """
        if not self.naver_cafe_list:
            return
        max_retries = 3
        for attempt in range(max_retries):
            try:
                if not self.is_running:
                    return
                if self.driver is None:
                    self.driver = self._get_driver()
                if not self.driver:
                    raise Exception("드라이버 생성 실패")
                self.status = "실행 중: 네이버 카페 크롤링 진행 중"
                self._emit_status()
                self._crawl_naver(self.driver)
                break
            except Exception as e:
                if not self.is_running:
                    return
                self._log(f"네이버 카페 크롤링 오류 ({attempt + 1}/{max_retries}): {e}", 'warning', '네이버')
                if self.driver:
                    try: self.driver.quit()
                    except Exception: pass
                    self.driver = None
                if attempt < max_retries - 1:
                    self._log(f"5초 후 {attempt + 1}차 재시도합니다. (브라우저 재시작)", 'warning', '네이버')
                    time.sleep(5)
                else:
                    self._log("네이버 카페 크롤링 최종 실패.", 'error', '네이버')

    def _crawl_official_with_driver(self):
        """[v6.7.87 → v6.7.136] 공식 홈페이지 크롤러 — 게임별 dict dispatcher.
        Selenium 불필요 (HTTP + BS4). crawler_tasks dict 의 `func` 인터페이스 유지를 위해
        `_with_driver` 네이밍 사용. 실제로는 driver 미사용.

        [활성화 gate] 사용자가 Config_Games 의 Official_URL 컬럼에 해당 게임 URL 을 채워야만
        발동. 빈 list 면 dispatcher 가 자동 skip → 사용자 의도 = 비활성으로 해석.

        [v6.7.136] 게임별 dict dispatcher 도입 — '배틀그라운드' 하드코딩 → OFFICIAL_CRAWLERS
        dict 로 확장. 미래 게임 추가 시 dict entry 1줄 + 신규 모듈 1개.
        """
        if not self.is_running:
            return
        # Gate — 사용자가 시트에 URL 안 적었으면 자동 발동 안 함.
        if not getattr(self, 'official_urls', None):
            return

        # [v6.7.136] 게임별 dispatcher — (게임명, 크롤 함수). 시트 GameName 과 정확 일치 필수.
        try:
            from crawlers.official_pubg import crawl_pubg_official
            from crawlers.official_poe import crawl_poe_official
        except Exception as e:
            self._log(f"공식 홈페이지 모듈 import 실패: {e}", 'error', '공식')
            return

        official_crawlers = {
            '배틀그라운드': crawl_pubg_official,
            # [v6.7.279] 'POE2'→'POE1' — KR POE 시트 행 = POE1 단일(poe.kakaogames.com). POE2 전용 보드
            #   (news2/patch-notes2)는 crawl_poe_official 가 board segment 로 POE2 분리 태깅(board_meta['game']).
            'POE1': crawl_poe_official,
        }

        # 박제할 게임이 1개라도 있는지 사전 확인
        active_games = [g for g in official_crawlers if self.official_urls.get(g)]
        if not active_games:
            return

        self.status = "실행 중: 공식 홈페이지 크롤링 진행 중"
        self._emit_status()

        for game_name, crawl_fn in official_crawlers.items():
            urls = self.official_urls.get(game_name) or []
            if not urls:
                continue

            try:
                board_results = crawl_fn(urls)
            except Exception as e:
                self._log(f"{game_name} 공식 홈페이지 크롤링 호출 실패: {e}", 'error', '공식')
                continue

            all_collected = []
            for board_meta, articles in board_results:
                try:   # [v6.7.739] 연속 실패 감지 - 감지가 터져도 아래 저장(앞 게시판 글 포함)을 막지 않게
                    self._note_official_board_result(game_name, board_meta)
                except Exception as e:
                    self._log(f"공식 게시판 실패 감지 오류: {e}", 'warning', '공식')
                if not articles:
                    continue
                # 키워드 감지 — DAUM/DC 와 동일 정책. Config_Keywords 의 슈퍼/일반 키워드 적용.
                for a in articles:
                    try:
                        kws, is_super = self._detect_keywords(a.get('title', ''), a.get('board_name', ''))
                        a['detected_keywords'] = kws
                        a['is_super_keyword'] = is_super
                    except Exception:
                        pass
                try:
                    # source 는 article dict 의 'source' 필드 사용 (모듈마다 'OFFICIAL_WEB' 통일).
                    article_source = articles[0].get('source', 'OFFICIAL_WEB')
                    # [v6.7.279] 게임 태그는 board_meta['game'] 우선 — POE 는 보드별 POE1/POE2 분리(news2 등).
                    #   PUBG 등 board_meta 에 'game' 없는 모듈은 dispatch game_name 으로 폴백(기존 동작 보존).
                    board_game = board_meta.get('game') or game_name
                    # [v6.7.456] 반환 신규 건수는 소비처가 없어 버린다 - v455 가 유일 소비처였던
                    #   `mark_new_posts` 를 지우면서 `new_count_by_game` 이 write-only 로 남았다.
                    #   DAUM/NAVER/DC 3경로와 달리 공식웹은 알림 퍼지 가드가 없어 여기만 고아가 됐다.
                    self._update_articles(article_source, board_game, articles)
                except Exception as e:
                    self._log(f"{game_name} _update_articles 실패 ({board_meta.get('board_name')}): {e}", 'error', '공식')
                all_collected.extend(articles)

            # SQLite 저장 - DAUM/DC 와 동일 흐름. (신규 글 마킹은 v6.7.455 컬럼 은퇴로 소멸)
            if all_collected:
                try:
                    from sqlite_handler import db_handler as _db
                    _db.save_community_posts(all_collected)
                    # [v6.7.455] is_new 마킹 제거 (컬럼 은퇴)
                except Exception as e:
                    self._log(f"{game_name} 공식 SQLite 저장 실패: {e}", 'error', '공식')

    def _note_official_board_result(self, game_name, board_meta):
        """[v6.7.739] 공식 게시판 연속 실패 감지. 크롤 모듈은 실패해도 빈 목록을 돌려줘 '오늘 글 0건' 과
        구분이 안 되므로, 모듈이 실패 시 board_meta['error'] 를 남기고 여기서 게시판별로 센다.
        OFFICIAL_FAIL_ALERT_STREAK 회째에 **한 번만** 오류 로그 + 앱 안 알림 보관소(admin)에 적재하고,
        성공하면 0 으로 되돌린다(다음 연속 실패는 다시 한 번 알린다). 앱 밖 발송은 v6.7.729 사용자 결정으로
        꺼져 있어 보관소가 보이는 자리다."""
        streaks = self._official_fail_streak
        name = board_meta.get('board_name') or board_meta.get('url') or game_name
        err = board_meta.get('error')
        if not err:
            prev = streaks.pop(name, 0)
            if prev >= OFFICIAL_FAIL_ALERT_STREAK:
                self._log(f"{name} 수집 복구 ({prev}회 연속 실패 뒤 성공)", 'info', '공식')
            return
        n = streaks.get(name, 0) + 1
        streaks[name] = n
        if n != OFFICIAL_FAIL_ALERT_STREAK:
            return
        self._log(f"{name} 수집 {n}회 연속 실패 - 새 글이 들어오지 않는다. 마지막 오류: {err}", 'error', '공식')
        try:
            from web_lib.bg import notif_store
            notif_store.record('crawl', f"{name} 수집이 멈췄습니다", f"{n}회 연속 실패 · {err}",
                               src='OFFICIAL_WEB', game=board_meta.get('game') or game_name, min_role='admin')
        except Exception as e:
            self._log(f"{name} 연속 실패 알림 적재 실패: {e}", 'warning', '공식')

    def _save_partial_before_raise(self, articles, label):
        """[2026-09-16] 드라이버 사망으로 사이클을 중단할 때 **이미 모은 글을 먼저 저장한다.**

        `_update_articles` 는 메모리 dedup(`seen_links`)만 갱신하고 SQLite 저장은 사이클 꼬리에서
        한 번에 한다. 꼬리에 못 가고 던지면 그 글들은 다음 사이클에 **'이미 본 글'로 걸러져 영구
        유실**된다 - 회복을 고치다가 유실을 새로 만들지 않기 위한 자리다."""
        if not articles:
            return
        try:
            from sqlite_handler import db_handler as _db
            _db.save_community_posts(articles)
            self._log(f"드라이버 사망 직전 {len(articles)}건 선저장 완료", 'info', label)
        except Exception as e:
            self._log(f"드라이버 사망 직전 선저장 실패: {e}", 'error', label)

    def _crawl_daum(self, driver):
        if not driver:
            return []

        all_collected_articles = []
        total_new_count = 0 # 전체 새 글 개수 추적
        new_count_by_game = {}  # [v6.7.455] 값은 미사용 - '이번 사이클에 신규가 있었나' 가드로만 남음
        # [v6.7.110] cafe_list value 는 List[str] (multi-URL). game 단위 outer + URL 단위 inner 순회.
        cafe_list_items = list(self.cafe_list.items())  # value 는 List[str]

        # 게임 접속 순서 섞기 (패턴 감지 회피)
        random.shuffle(cafe_list_items)

        wait_time = self.settings.get('WEBDRIVER_WAIT_TIME', WEBDRIVER_WAIT_TIME)

        # [v6.7.110] multi-URL 환경 — total_targets = 모든 게임의 URL 총합. status msg 진척 표시용.
        total_targets = sum(len(urls) for _, urls in cafe_list_items)
        target_idx = 0

        for game_name, cafe_urls in cafe_list_items:
            if not self.is_running: # 각 게임 크롤링 시작 전에 중지 신호를 확인합니다.
                self._log("크롤러 중지 신호 감지. Daum 카페 크롤링을 중단합니다.", 'info', '카페')
                break

            # [v6.7.110] 같은 게임의 N URL 순회. seen_links 는 game 단위 통합 dedup.
            for url_in_game_idx, cafe_url in enumerate(cafe_urls):
                if not self.is_running:
                    break

                # [안전장치] 카페 간 이동 시 사람처럼 보이게 랜덤 지연 추가 (첫 URL 제외).
                if target_idx > 0:
                    time.sleep(random.uniform(2.0, 4.0))
                target_idx += 1

                today_articles_for_game = []  # URL 단위 reset (동일 게임 N URL = 게시판 별 분리)

                # [최적화] 이미 수집한 링크 목록 가져오기 (중복 AI 검사 방지). game 단위 통합.
                key = f"DAUM_{game_name}"
                with self.lock:
                    known_links = self.seen_links.get(key, set()).copy()

                # 카페 접속 재시도 로직 추가 (최대 3회)
                for attempt in range(3):
                    try:
                        if not self.is_running:
                            return all_collected_articles

                        self.status = f"실행 중: Daum 카페 크롤링 중 ({target_idx}/{total_targets}): {game_name}"
                        self._emit_status()
                        driver.get(cafe_url)
                        # [최적화] CSS 애니메이션 강제 제거
                        driver.execute_script("var style = document.createElement('style'); style.innerHTML = '* { transition: none !important; animation: none !important; }'; document.head.appendChild(style);")

                        WebDriverWait(driver, wait_time).until(EC.frame_to_be_available_and_switch_to_it((By.ID, IFRAME_ID)))
                        # iframe 내부에도 CSS 애니메이션 강제 제거 적용
                        driver.execute_script("var style = document.createElement('style'); style.innerHTML = '* { transition: none !important; animation: none !important; }'; document.head.appendChild(style);")

                        rows = driver.find_elements(By.CSS_SELECTOR, ARTICLE_ROW_SELECTOR)

                        for crawl_idx, row in enumerate(rows):
                            try:
                                title_elem = row.find_element(By.CSS_SELECTOR, "a[href*='recent_bbs_read']")
                                date_elem = row.find_element(By.CSS_SELECTOR, "td:nth-child(4)")
                                date = date_elem.text.strip()

                                if ':' not in date:
                                    continue

                                # [최적화] 뷰어 앱의 문자열 대소 비교 누락 방지를 위해 9:15 -> 09:15 강제 통일
                                parsed_dt = self._datetime_from_string(date)
                                # [FIX] 크롤링 순서를 초(second)에 인코딩하여 같은 분 내 글 순서 보존
                                parsed_dt = parsed_dt.replace(second=max(59 - crawl_idx, 0))
                                if ' ' not in date:
                                    date = parsed_dt.strftime('%H:%M')

                                title = title_elem.text.strip()
                                if title and title_elem.get_attribute('href'):
                                    link = title_elem.get_attribute('href')
                                    link = self._normalize_daum_link(link) # 링크 정규화 적용 (설정 기반)

                                    # [최적화] 이미 수집한 글이면 AI 분석 및 추가 처리 건너뜀
                                    if link in known_links:
                                        continue

                                    detected_kws, is_super = self._detect_keywords(title, "")
                                    official_kws = self._detect_official_keywords(title)
                                    today_articles_for_game.append({
                                        'title': title,
                                        'link': link,
                                        'board_name': row.find_element(By.CSS_SELECTOR, "td:nth-child(1)").text.strip(),
                                        'date': date, 'source': 'DAUM', 'game': game_name,
                                        'detected_keywords': detected_kws,
                                        'is_super_keyword': is_super,
                                        'detected_official_keywords': official_kws,
                                        'is_official_keyword': bool(official_kws),
                                        'content': '', # 초기값
                                        'timestamp': parsed_dt.isoformat()
                                    })
                            except NoSuchElementException:
                                # 링크가 없는 경우 (규제된 글, 광고 등) 확인 및 로깅
                                try:
                                    if "관리자에 의해 규제된 글입니다" in row.text:
                                        pass#출력하지 않아도됨.
                                        # self._log(f"다음 카페 '{game_name}': 규제된 글 발견 (건너뜀)", 'debug', '카페')

                                except Exception: pass
                                continue
                            except Exception as e:
                                self._log(f"다음 카페 '{game_name}' 게시물 파싱 중 오류: {e}", 'debug', '카페')
                                continue

                        driver.switch_to.default_content()

                        # 날짜 문자열(HH:MM) 대신 타임스탬프 기준으로 정렬 (자정 경계 문제 해결)
                        sorted_articles = sorted(today_articles_for_game, key=lambda x: x['timestamp'], reverse=True)
                        max_articles = self.settings.get('MAX_CAFE_ARTICLES', MAX_CAFE_ARTICLES)
                        final_articles_for_game = sorted_articles[:max_articles]

                        new_cnt = self._update_articles('DAUM', game_name, final_articles_for_game)
                        total_new_count += new_cnt
                        if new_cnt > 0:
                            new_count_by_game[game_name] = new_count_by_game.get(game_name, 0) + new_cnt

                        all_collected_articles.extend(final_articles_for_game)
                        break # 성공 시 루프 탈출

                    except Exception as e:
                        # 앱 종료 시(is_running=False) 또는 드라이버 통신 오류 시 즉시 탈출 (불필요한 재시도 방지)
                        is_connection_error = "NewConnectionError" in str(e) or "MaxRetryError" in str(e) or "invalid session" in str(e) or "WinError 10061" in str(e)
                        if not self.is_running:
                            return all_collected_articles
                        if is_connection_error:
                            # [2026-09-16] **감지는 원래 있었다. 없던 것은 처방이다**(PATTERN-61).
                            #   구 코드는 여기서 조용히 `return` 해서 `_crawl_daum_with_driver` 가
                            #   "정상 종료" 로 읽고 드라이버를 재생성하지 않았다 - 죽은 세션 그대로
                            #   다음 사이클로 갔다. 던져야 그 위의 quit -> None -> 재생성이 돈다.
                            from crawlers import DriverSessionDead
                            self._log("다음 카페 - WebDriver 세션 사망. 드라이버 재생성을 위해 전파합니다.",
                                      'error', '카페')
                            self._save_partial_before_raise(all_collected_articles, '카페')
                            raise DriverSessionDead(f"DAUM 크롤 중 드라이버 세션 사망: {e}") from e

                        if attempt < 2:
                            self._log(f"다음 카페 '{game_name}' 접속/로딩 실패 (시도 {attempt+1}/3): {e}", 'warning', '카페')
                            driver.switch_to.default_content() # 프레임 복귀
                            time.sleep(2)
                            continue
                        else:
                            self._log(f"다음 카페 '{game_name}' 크롤링 최종 실패: {e}", 'error', '카페')
                            driver.switch_to.default_content()

                            # 드라이버 생존 여부 확인 (죽었으면 즉시 루프 탈출하여 재시작 유도)
                            try:
                                _ = driver.window_handles
                            except Exception as _probe_e:
                                # [2026-09-16] 여기도 같은 자리였다 - "재시작 유도" 라고 적어놓고
                                #   `return` 해서 아무도 재시작하지 않았다. 던지는 것이 곧 유도다.
                                from crawlers import DriverSessionDead
                                self._log("WebDriver 비정상 종료 감지. 드라이버 재생성을 위해 전파합니다.", 'error', '카페')
                                self._save_partial_before_raise(all_collected_articles, '카페')
                                raise DriverSessionDead(f"DAUM 크롤 중 드라이버 세션 사망: {_probe_e}") from _probe_e

        # 모든 크롤링이 끝난 후 빈 페이지로 이동하여 대기 시간 동안의 리소스 점유율 최소화
        try:
            driver.get("about:blank")
        except Exception:
            pass

        if total_new_count == 0:
            pass  # [로그 다이어트] 최신 글 없음은 정상 상태

        # SQLite 우선 저장 (하류 소비처가 행을 찾을 수 있도록)
        if all_collected_articles:
            try:
                from sqlite_handler import db_handler as _db
                _db.save_community_posts(all_collected_articles)
            except Exception:
                pass

        # [v6.7.455] is_new 마킹 제거 (컬럼 은퇴). `new_count_by_game` 은 **알림 정리 가드**로만 남는다
        #   — "이번 사이클에 신규가 있었나" 신호. 값(개수) 자체는 이제 아무도 안 읽는다.
        if new_count_by_game:
            # [FIX] 처리 완료된 DAUM 신규 알림 정리 (누적 방지)
            for link in list(self.new_alerts):
                if str(self.new_alerts[link].get('source', '')).upper() == 'DAUM':
                    del self.new_alerts[link]

        if self.socketio:
            # [v6.7.433] 경량 신호 — 옛 본문(무제한 ~1.22MB) broadcast 제거. 크롤 사이클마다 전 클라가
            #   10000개를 메인스레드 파싱하던 정상상태 순단(PATTERN-38)의 진원. 클라는 핑 받고 활성 뷰만 자체 refetch.
            self.socketio.emit('raw_articles_update', {})

        return all_collected_articles

    def _crawl_naver(self, driver):
        """[v6.7.113] 네이버 카페 본체 — multi-URL nested loop (game outer + URL inner).

        DAUM 패턴 따라:
        - seen_links key = f"NAVER_{game_name}" game 단위 통합 dedup
        - 키워드 감지 (`_detect_keywords` + `_detect_official_keywords`) — DAUM 동일
        - timestamp DESC 정렬 + MAX_CAFE_ARTICLES 자르기
        - _update_articles('NAVER', game_name, articles) + SQLite save
        - 신규 알림 정리 (source='NAVER')
        - socketio emit raw_articles_update
        """
        if not driver:
            return []

        try:
            from crawlers import DriverSessionDead
            from crawlers.naver_cafe import crawl_naver_cafe, SOURCE as NAVER_SOURCE
        except Exception as e:
            self._log(f"네이버 카페 모듈 import 실패: {e}", 'error', '네이버')
            return []

        all_collected_articles = []
        total_new_count = 0
        new_count_by_game = {}

        naver_list_items = list(self.naver_cafe_list.items())   # value 는 List[str]
        random.shuffle(naver_list_items)

        total_targets = sum(len(urls) for _, urls in naver_list_items)
        target_idx = 0

        for game_name, naver_urls in naver_list_items:
            if not self.is_running:
                self._log("크롤러 중지 신호 감지. 네이버 카페 크롤링을 중단합니다.", 'info', '네이버')
                break

            for url_in_game_idx, naver_url in enumerate(naver_urls):
                if not self.is_running:
                    break

                if target_idx > 0:
                    time.sleep(random.uniform(2.0, 4.0))
                target_idx += 1

                self.status = f"실행 중: 네이버 카페 크롤링 중 ({target_idx}/{total_targets}): {game_name}"
                self._emit_status()

                # seen_links — game 단위 통합 dedup (DAUM/DC 패턴 일관)
                key = f"NAVER_{game_name}"
                with self.lock:
                    known_links = self.seen_links.get(key, set()).copy()

                try:
                    board_results = crawl_naver_cafe(driver, [naver_url])
                except DriverSessionDead:
                    # [2026-09-16] 드라이버 사망은 URL 실패가 아니다 - 여기서 continue 하면
                    #   `_crawl_naver_with_driver` 의 재생성이 영영 안 돈다(실사고 9시간).
                    self._log("네이버 카페 - WebDriver 세션 사망. 드라이버 재생성을 위해 전파합니다.",
                              'error', '네이버')
                    self._save_partial_before_raise(all_collected_articles, '네이버')
                    raise
                except Exception as e:
                    self._log(f"네이버 카페 '{game_name}' URL{url_in_game_idx+1} 처리 실패: {e}", 'warning', '네이버')
                    continue

                today_articles_for_game = []
                for _meta, articles in board_results:
                    for a in articles:
                        if a.get('link') in known_links:
                            continue
                        a['game'] = game_name
                        a['content'] = ''
                        try:
                            kws, is_super = self._detect_keywords(a.get('title', ''), a.get('board_name', ''))
                            a['detected_keywords'] = kws
                            a['is_super_keyword'] = is_super
                        except Exception:
                            a['detected_keywords'] = []
                            a['is_super_keyword'] = False
                        try:
                            official_kws = self._detect_official_keywords(a.get('title', ''))
                            a['detected_official_keywords'] = official_kws
                            a['is_official_keyword'] = bool(official_kws)
                        except Exception:
                            a['detected_official_keywords'] = []
                            a['is_official_keyword'] = False
                        today_articles_for_game.append(a)

                if today_articles_for_game:
                    sorted_articles = sorted(today_articles_for_game, key=lambda x: x['timestamp'], reverse=True)
                    max_articles = self.settings.get('MAX_CAFE_ARTICLES', MAX_CAFE_ARTICLES)
                    final_articles_for_game = sorted_articles[:max_articles]
                    try:
                        new_cnt = self._update_articles(NAVER_SOURCE, game_name, final_articles_for_game)
                        total_new_count += new_cnt
                        if new_cnt > 0:
                            new_count_by_game[game_name] = new_count_by_game.get(game_name, 0) + new_cnt
                        all_collected_articles.extend(final_articles_for_game)
                    except Exception as e:
                        self._log(f"네이버 _update_articles 실패 ({game_name}): {e}", 'error', '네이버')

        # SQLite 저장 + 신규 마킹 — DAUM/DC 와 동일 흐름.
        if all_collected_articles:
            try:
                from sqlite_handler import db_handler as _db
                _db.save_community_posts(all_collected_articles)
                # [v6.7.455] is_new 마킹 제거 (컬럼 은퇴)
            except Exception as e:
                self._log(f"네이버 SQLite 저장 실패: {e}", 'error', '네이버')

        # 신규 알림 정리 — source='NAVER' 누적 방지
        if new_count_by_game:
            for link in list(self.new_alerts):
                if str(self.new_alerts[link].get('source', '')).upper() == NAVER_SOURCE:
                    del self.new_alerts[link]

        if self.socketio:
            # [v6.7.433] 경량 신호 (PATTERN-38) — 본문 broadcast 제거, 클라 자체 refetch.
            self.socketio.emit('raw_articles_update', {})

        return all_collected_articles

    def _dc_fetch_mode(self):
        """[v6.7.486] DC 목록 fetch 모드 — 'selenium'(기본) | 'http'.

        시트 `Config_Settings.DC_FETCH_MODE` 를 매 사이클 읽는다(`self.settings` 는 폴링이
        갱신하므로 **배포 없이 즉시 되돌릴 수 있는 것**이 이 설계의 목적이다 — 롤백이
        재배포라면 그건 롤백이 아니다. 재배포 자체가 PATTERN-52 위험을 동반한다).

        차단 자동 복귀: 403/429 가 2회 누적되면 시트가 http 여도 selenium 으로 강제 전환한다.
        사람이 시트 값을 바꾸면(예: http → selenium → http) 그 강제 상태가 풀린다.
        """
        raw = str(self.settings.get('DC_FETCH_MODE', 'selenium') or 'selenium').strip().lower()
        mode = 'http' if raw == 'http' else 'selenium'

        if self._dc_last_sheet_mode is not None and raw != self._dc_last_sheet_mode:
            # 사람이 시트를 건드렸다 = 강제 폴백 해제 (재시도 의사 표시로 본다)
            if self.dc_http_forced_off:
                self._log("DC fetch 모드 시트 변경 감지 - 차단 강제 폴백 해제", 'info', 'DC')
            self.dc_http_forced_off = False
            self.dc_http_block_count = 0
        self._dc_last_sheet_mode = raw

        if mode == 'http' and self.dc_http_forced_off:
            return 'selenium'
        return mode

    def _fetch_dc_page(self, driver, url, game_name):
        """[v6.7.486] DC 목록 페이지 1장의 HTML 을 가져온다 (selenium | http 공통 진입점).

        **파싱은 호출부가 한다** — 두 모드가 같은 파싱 코드를 타므로 파싱 정확도 차이가
        구조적으로 발생할 수 없다(전환 리스크를 fetch 하나로 가둔 것이 이 분리의 이유).

        Returns:
            HTML 문자열 / None(소프트 실패 — 호출부가 consecutive_failures 처리)
        Raises:
            IPBlockedError — 403/429 (호출부가 쿨다운 + selenium 복귀 처리)
        """
        if self._dc_fetch_mode() == 'http':
            from crawlers import dc_http
            html, status = dc_http.fetch_list_html(url)
            if status in (403, 429):
                raise IPBlockedError(f"HTTP {status}")
            if html is None:
                self._log(f"DC '{game_name}' HTTP 응답 이상 (status={status})", 'warning', 'DC')
                return None
            # [v6.7.486] selenium 의 WebDriverWait(목록 선택자 등장) 과 **같은 판정**을 HTTP 에도 건다.
            #   dcinside 는 없는 갤러리·소프트 차단에도 **200 을 준다**(실측) — status 만 믿으면
            #   '200 인데 목록이 빈' 페이지가 실패로 안 세져 3연속 실패 쿨다운이 무력해진다.
            if ('ub-content' not in html) and ('gall_list' not in html):
                self._log(f"DC '{game_name}' HTTP 200 이나 목록 마커 없음 (빈 페이지 또는 차단 가능성)", 'warning', 'DC')
                return None
            self.dc_http_block_count = 0  # 성공 = '연속' 차단 카운트 리셋
            return html

        # selenium 경로 (기본) — 옛 동작 그대로
        if driver is None:
            # 사이클 **도중** 시트가 http → selenium 으로 바뀐 경우(진입 시 드라이버를 이미 quit 했다).
            # 다음 사이클이 정상적으로 드라이버를 만드므로 이번 것만 건너뛴다.
            self._log(f"DC '{game_name}' 드라이버 없음(사이클 중 모드 변경) - 이번 사이클 건너뜀", 'warning', 'DC')
            return None
        driver.get(url)
        try:
            # 대기 시간 10초 -> 30초로 상향 (네트워크 지연 및 렌더링 대기)
            WebDriverWait(driver, 30).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "tbody tr.ub-content, table.gall_list tbody tr"))
            )
        except TimeoutException:
            self._log(f"DC '{game_name}' 로딩 시간 초과 (빈 페이지 또는 차단 가능성)", 'warning', 'DC')
            return None
        return driver.page_source

    def _crawl_dc_with_driver(self):
        """
        [메서드 역할]
        DC 갤러리 크롤링을 위한 독립적인 셀레니움(Selenium) 환경을 구성하고 실행함.
        **[v6.7.486] `DC_FETCH_MODE=http` 면 드라이버를 만들지 않고, 떠 있으면 quit 한다** —
        유휴 드라이버는 메모리를 돌려주지 않으므로(PATTERN-31 `about:blank` 실험) 회수 수단은
        프로세스 종료뿐이다. 이 quit 을 빠뜨리면 "HTTP 로 바꿨는데 934MB 가 그대로" 가 된다.

        [안티 봇 우회 (Anti-Ban) 전략]
        - 랜덤 교체 주기: `dc_driver_usage_count`가 임계값(30~60회)에 도달하면 드라이버를 강제 종료하고 새 신분(User-Agent, Session)으로 위장하여 403 차단을 회피함.
        - 지수 백오프: 서버 오류 발생 시 즉시 재시도하지 않고 대기 후 재시도(최대 3회)하여 서버 부하 및 영구 차단을 방어함.
        """
        """DC 갤러리 크롤링 (Selenium 사용 - 차단 우회)"""
        # 크롤링할 갤러리가 없으면 즉시 종료 (로그 및 드라이버 실행 방지)
        if not self.dc_galleries:
            return

        # [v6.7.486] HTTP 모드 — 드라이버를 만들지 않고, 떠 있던 것은 종료해 메모리를 회수한다.
        #   quit 이 유일한 회수 수단이다(PATTERN-31: about:blank 로는 RSS 가 안 돌아온다).
        if self._dc_fetch_mode() == 'http':
            if self.dc_driver:
                self._log("DC HTTP 모드 전환 - 전용 드라이버 종료 (메모리 회수)", 'info', 'DC')
                try:
                    self.dc_driver.quit()
                except Exception: pass
                self.dc_driver = None
                self.dc_driver_usage_count = 0
            self.status = "실행 중: DC 갤러리 크롤링 진행 중 (HTTP)"
            self._emit_status()
            try:
                self._crawl_dc_pages(None)
            except Exception as e:
                self._log(f"DC 갤러리 크롤링 오류(HTTP): {e}", 'error', 'DC')
            return

        # 드라이버 수명 관리 (랜덤 주기 교체)
        if self.dc_driver and self.dc_driver_usage_count >= self.dc_driver_max_usage:
            self._log(f"DC 드라이버 교체 ({self.dc_driver_usage_count}회 사용)", 'info', 'DC')
            try:
                self.dc_driver.quit()
            except Exception: pass
            self.dc_driver = None
            self.dc_driver_usage_count = 0
            self.dc_driver_max_usage = random.randint(30, 60) # 다음 교체 주기 랜덤 설정 (30~60회)

        # 3회 재시도 정책
        max_retries = 3
        for attempt in range(max_retries):
            try:
                if not self.is_running: return

                # 드라이버가 없거나 죽었을 때만 새로 생성 (세션 유지)
                if self.dc_driver is None:
                    self._log(f"DC 전용 드라이버 초기화 중...", 'info', 'DC')
                    self.dc_driver = self._create_local_driver()
                    self.dc_driver_usage_count = 0 # 생성 시 카운트 초기화
                
                if not self.dc_driver:
                    raise Exception("DC 드라이버 생성 실패")

                self.status = "실행 중: DC 갤러리 크롤링 진행 중"
                self._emit_status()
                
                # 생성된/유지된 드라이버 사용
                self._crawl_dc_pages(self.dc_driver)
                
                # 사용 횟수 증가
                self.dc_driver_usage_count += 1
                break # 성공 시 루프 탈출

            except Exception as e:
                if not self.is_running: return
                self._log(f"DC 갤러리 크롤링 오류 ({attempt + 1}/{max_retries}): {e}", 'warning', 'DC')
                
                # 오류 발생 시 드라이버 재시작 (세션 초기화 감수)
                if self.dc_driver:
                    try: self.dc_driver.quit()
                    except Exception: pass
                    self.dc_driver = None
                    self.dc_driver_usage_count = 0 # 리셋
                
                if attempt < max_retries - 1:
                    time.sleep(5)
                else:
                    self._log("DC 갤러리 크롤링 최종 실패.", 'error', 'DC')

    def _crawl_dc_pages(self, driver):
        """DC 갤러리 순회 및 파싱 (v6.7.486: selenium/http 공통 — fetch 만 `_fetch_dc_page` 가 분기).

        `driver` 는 HTTP 모드에서 None 이다. 파싱·dedup·저장 경로는 두 모드가 완전히 동일하다.
        """
        all_collected_articles = []
        total_new_count = 0
        new_count_by_game = {}  # [v6.7.455] 값은 미사용 - '이번 사이클에 신규가 있었나' 가드로만 남음
        consecutive_failures = 0 # 연속 실패 카운트
        
        # DC 차단 쿨타임 체크
        if time.time() < self.dc_cooldown_until:
            remaining = int(self.dc_cooldown_until - time.time())
            self._log(f"DC 차단 보호 모드 작동 중... ({remaining}초 남음)", 'warning', 'DC')
            return []

        # [v6.7.110] dc_galleries value 는 List[str] (multi-URL). game 단위 outer + URL 단위 inner 순회.
        dc_galleries_items = list(self.dc_galleries.items())  # value 는 List[str]
        random.shuffle(dc_galleries_items)

        # [v6.7.110] multi-URL 환경 — total_targets / target_idx 진척 표시.
        total_targets = sum(len(urls) for _, urls in dc_galleries_items)
        target_idx = 0

        for game_name, dc_urls in dc_galleries_items:
            if not self.is_running: break

            # [v6.7.110] 같은 게임의 N URL 순회. seen_links 는 game 단위 통합 dedup.
            for url_in_game_idx, url in enumerate(dc_urls):
                if not self.is_running: break

                if target_idx > 0: time.sleep(random.uniform(2.0, 4.0))
                target_idx += 1

                try:
                    self.status = f"실행 중: DC 갤러리 크롤링 중 ({target_idx}/{total_targets}): {game_name}"
                    self._emit_status()

                    # [v6.7.486] fetch 계층 분리 — selenium(driver.get + wait) / http(requests) 공통 진입점.
                    page_html = self._fetch_dc_page(driver, url, game_name)
                    if page_html is None:
                        consecutive_failures += 1 # 실패 시 카운트 증가

                        # 연속 3회 실패 시 쿨타임 적용 후 중단
                        if consecutive_failures >= 3:
                            self._log("DC 갤러리 연속 접속 실패(3회). IP 차단으로 간주하고 10분간 수집을 중단합니다.", 'error', 'DC')
                            self.dc_cooldown_until = time.time() + 600 # 10분 대기
                            return all_collected_articles
                        continue
                    consecutive_failures = 0 # 성공 시 카운트 초기화

                    # HTML 파싱 (BeautifulSoup 사용) — selenium/http 가 같은 코드를 탄다
                    soup = BeautifulSoup(page_html, 'html.parser')

                    # 선택자 유연화 (tbody 명시 제거)
                    rows = soup.select("tr.ub-content")
                    if not rows:
                        rows = soup.select("table.gall_list tbody tr")

                    if not rows:
                        self._log(f"DC '{game_name}' 게시글 목록을 찾을 수 없습니다.", 'warning', 'DC')
                    else:
                        # [디버그] 행 개수 확인
                        # self._log(f"DC '{game_name}' 행 {len(rows)}개 발견. 파싱 시작...", 'debug', 'DC')
                        pass

                    articles = []
                    collected_count = 0

                    key = f"DC_{game_name}"
                    with self.lock:
                        known_links = self.seen_links.get(key, set()).copy()

                    max_dc = self.settings.get('MAX_DC_ARTICLES', MAX_DC_ARTICLES)
                    for crawl_idx, tr in enumerate(rows):
                        if collected_count >= max_dc: break
                        try:
                            a_tag = tr.select_one("td.gall_tit a") or tr.select_one(".ub-word > a") or tr.select_one(".tit > a")
                            date_tag = tr.select_one("td.gall_date") or tr.select_one(".gall_date") or tr.select_one(".date")

                            if not a_tag or not date_tag:
                                continue

                            title = a_tag.get_text(strip=True)
                            href = a_tag.get("href", "")
                            post_time = date_tag.get_text(strip=True)

                            # [디버그] 날짜 필터링 로그 (필요시 주석 해제)
                            if ":" not in post_time:
                                # self._log(f"날짜 필터링됨: {post_time} ({title})", 'debug', 'DC')
                                continue

                            # [최적화] 뷰어 앱의 문자열 대소 비교 누락 방지를 위해 9:15 -> 09:15 강제 통일
                            parsed_dt = self._datetime_from_string(post_time)
                            # [FIX] 크롤링 순서를 초(second)에 인코딩하여 같은 분 내 글 순서 보존
                            parsed_dt = parsed_dt.replace(second=max(59 - crawl_idx, 0))
                            if ' ' not in post_time:
                                post_time = parsed_dt.strftime('%H:%M')

                            if not href or href.startswith("javascript:"):
                                continue

                            link = href
                            if href.startswith("/"):
                                link = "https://gall.dcinside.com" + href
                            elif not href.startswith("http"):
                                link = url.rstrip('/') + href

                            if link in known_links:
                                # self._log(f"이미 수집된 링크: {title}", 'debug', 'DC')
                                continue

                            detected_kws, is_super = self._detect_keywords(title, "")
                            articles.append({
                                'title': title,
                                'link': link,
                                'board_name': f'{game_name} 갤러리',
                                'date': post_time,
                                'source': 'DC',
                                'game': game_name,
                                'detected_keywords': detected_kws,
                                'is_super_keyword': is_super,
                                'content': '',
                                'timestamp': parsed_dt.isoformat()
                            })
                            collected_count += 1
                        except Exception as e:
                            pass  # [로그 다이어트] 파싱 에러는 per-article이라 생략
                            continue

                    if articles:
                        new_cnt = self._update_articles('DC', game_name, articles)
                        total_new_count += new_cnt
                        if new_cnt > 0:
                            new_count_by_game[game_name] = new_count_by_game.get(game_name, 0) + new_cnt
                        all_collected_articles.extend(articles)
                        # self._log(f"DC 갤러리 '{game_name}' 파싱 완료: {len(articles)}개 글 확인", 'info', 'DC')

                except IPBlockedError as e:
                    # [v6.7.486] HTTP 모드에서만 발생 (403/429). 차단은 되돌리기 어려운 손해라
                    #   즉시 10분 대기 + 세션 폐기, 연속 2회면 Selenium 으로 자동 복귀한다.
                    self.dc_http_block_count += 1
                    self.dc_cooldown_until = time.time() + 600
                    try:
                        from crawlers import dc_http
                        dc_http.reset_session()
                    except Exception:
                        pass
                    if self.dc_http_block_count >= 2:
                        self.dc_http_forced_off = True
                        self._log(f"DC HTTP 차단 감지 2회({e}) - Selenium 으로 자동 복귀 (시트 DC_FETCH_MODE 재설정 시 해제)", 'error', 'DC')
                    else:
                        self._log(f"DC HTTP 차단 감지({e}) - 10분 대기 (연속 2회 시 Selenium 자동 복귀)", 'warning', 'DC')
                    return all_collected_articles
                except Exception as e:
                    self._log(f"DC '{game_name}' 크롤링 중 오류: {e}", 'error', 'DC')

        if total_new_count == 0:
            pass  # [로그 다이어트] 최신 글 없음은 정상 상태

        # SQLite 우선 저장 (하류 소비처가 행을 찾을 수 있도록)
        if all_collected_articles:
            try:
                from sqlite_handler import db_handler as _db
                _db.save_community_posts(all_collected_articles)
            except Exception:
                pass

        # [v6.7.455] is_new 마킹 제거 (컬럼 은퇴). 아래 가드는 알림 정리용으로만 남는다.
        if new_count_by_game:
            # [FIX] 처리 완료된 DC 신규 알림 정리 (누적 방지)
            for link in list(self.new_alerts):
                if str(self.new_alerts[link].get('source', '')).upper() == 'DC':
                    del self.new_alerts[link]

        if self.socketio:
            # [v6.7.433] 경량 신호 — 옛 본문(무제한 ~1.22MB) broadcast 제거. 크롤 사이클마다 전 클라가
            #   10000개를 메인스레드 파싱하던 정상상태 순단(PATTERN-38)의 진원. 클라는 핑 받고 활성 뷰만 자체 refetch.
            self.socketio.emit('raw_articles_update', {})

        if self.data_export_callback:
            # self._log("DC 크롤링 완료, 데이터 내보내기 콜백 호출", 'info', 'DC') # [로그 다이어트]
            latest_data = self.get_all_latest_articles(limit=GOOGLE_SHEET_UPLOAD_LIMIT, per_game_limit=GOOGLE_SHEET_UPLOAD_LIMIT)
            self.data_export_callback("MAIN_CRAWLER", latest_data)

        return all_collected_articles

    # [v6.7.111] _crawl_dc + _crawl_gallery_requests dead method (호출처 0) — 삭제.
    #   crawler_tasks DC 핸들러는 _crawl_dc_with_driver → _crawl_dc_pages 활성 경로만.
    #   옛 requests 기반 DC 크롤링 ~240 LoC — Selenium 차단 우회 도입 후 폐기. git history 보존.


    def _is_board_excluded(self, game_name: str, board_name: str) -> bool:
        """[v6.7.131] Config_Games.ExcludeBoards substring 매칭으로 게시판 제외 여부 판정.

        True 반환 시 `_update_articles` 진입부에서 해당 article 폐기 (메모리/SQLite/시트 진입 전).
        빈 키워드 dict / 빈 board_name 은 False 반환 — 기존 동작 유지 (회귀 0).

        매칭 정책: substring (풀네임 X) + 공백 strip + 대소문자 보존.
        한국어 키워드 위주 (클랜/모집/분양 등) 사용 가정 — 정규화 불필요.
        """
        if not board_name:
            return False
        kw_list = self.board_exclude_kw.get(game_name, [])
        if not kw_list:
            return False
        for kw in kw_list:
            if kw in board_name:
                return True
        return False

    def _detect_keywords(self, title: str, board_name: str) -> Tuple[List[str], bool]:
        # 게시판 이름은 제외하고 글 제목에서만 키워드를 검사하도록 변경합니다.
        text = (title or "").lower()
        
        # 1. 슈퍼 키워드를 먼저 확인합니다.
        detected_super = [kw for kw in self.super_keywords if kw in text] # 인스턴스 변수 사용
            
        # 2. 일반 키워드를 확인합니다.
        detected_highlight = [kw for kw in self.highlight_keywords if kw in text] # 인스턴스 변수 사용

        # [개선] 슈퍼 키워드가 감지되면, 해당 슈퍼 키워드에 포함되는 일반 키워드는 결과에서 제외합니다.
        if detected_super:
            # 가장 긴 슈퍼 키워드를 기준으로 삼아, 그 안에 포함된 일반 키워드는 무시
            longest_super = max(detected_super, key=len)
            detected_highlight = [kw for kw in detected_highlight if kw not in longest_super]

        # 키워드 병합 및 정렬
        content_keywords = detected_super + detected_highlight
        content_keywords.sort(key=lambda k: text.find(k) if k in text else 999) # 제목 내 등장 위치순 정렬
        content_keywords = list(dict.fromkeys(content_keywords)) # 중복 제거
        content_keywords = content_keywords[:2] # 최대 2개 제한

        all_keywords = content_keywords

        # 슈퍼 키워드가 하나라도 감지되면 중요 글로 판단
        is_super = bool(detected_super)

        return all_keywords, is_super

    def _detect_official_keywords(self, title: str) -> List[str]:
        """OFFICIAL 타입 키워드 감지 (다음 카페 공지 전용). SUPER 흐름과 독립."""
        text = (title or "").lower()
        return [kw for kw in self.official_keywords if kw in text]

    def _update_articles(self, source: str, game_name: str, articles: List[Dict]) -> int:
        # [v6.7.159] 부팅 race 가드 — Config_Games 1회 polling 완료 전엔 NAVER/DAUM article 진입 차단.
        # ExcludeBoards dict 가 채워지기 전에 article 이 메모리/SQLite/시트로 진입하면 이후 polling 으로
        # dict 가 채워져도 sweep 되지 않고 영구 노출됨 (피드탭/SQLite/뷰어 일관 leak).
        # DC 는 board_name 고정 라벨이라 ExcludeBoards 영향 0 → skip 안 함.
        # OFFICIAL/PUBG 는 게시판 단일이라 race 위험 낮음 → 일단 가드 미적용 (필요 시 추가).
        # NAVER + DAUM 만 1 사이클 대기 (보통 60~120초). 부팅 후 첫 polling 완료 시 자동 해제.
        if source in ('NAVER', 'DAUM') and not self._first_config_poll_done:
            self._log(
                f"{source} {game_name} {len(articles)}건 — Config_Games 첫 polling 완료 전이라 skip (ExcludeBoards race 방지)",
                'warning', source
            )
            return 0

        # [v6.7.131] ExcludeBoards 키워드 필터링 — 4 소스 (Daum/Naver/PUBG/DC) 일괄 적용.
        # `_update_articles` 가 메모리/SQLite/시트 push 진입의 단일 지점이라
        # 여기서 1회 거르면 모든 하류 저장 차단됨.
        # board_name 이 키워드 substring 포함하면 폐기. 키워드 없으면 기존 동작 그대로.
        if articles and self.board_exclude_kw.get(game_name):
            original_cnt = len(articles)
            kept = [
                a for a in articles
                if not self._is_board_excluded(game_name, a.get('board_name', ''))
            ]
            excluded_cnt = original_cnt - len(kept)
            if excluded_cnt > 0:
                # [v6.7.174] in-place 슬라이스 대입 — 재바인딩(articles=[...])이면 호출자(_crawl_*)의
                # 리스트 ref 는 미필터 그대로라, all_collected.extend(articles) → save_community_posts 로
                # SQLite 에 클랜 등 제외 게시판이 그대로 저장됨. v6.7.172(피드 SQLite 직독) 이후 이 미필터
                # SQLite 행이 단일게임 필터 피드에 노출됨. 슬라이스 대입은 호출자 ref 까지 필터 반영.
                articles[:] = kept
                self._log(
                    f"{game_name} 게시판 필터로 {excluded_cnt}개 제외 "
                    f"(키워드: {', '.join(self.board_exclude_kw[game_name])})",
                    'info', source
                )

        with self.lock:
            alerts_to_emit = [] # 이번에 전송할 슈퍼 키워드 알림 목록
            official_alerts_to_emit = [] # 공지 알림 목록 (DAUM 전용)
            new_count = 0
            keyword_count = 0

            if game_name not in self.articles_by_game: self.articles_by_game[game_name] = {}
            key = f"{source}_{game_name}"
            current_links = {a['link'] for a in articles}
            
            # 전체 목록 중복 방지를 위한 현재 링크 집합 생성
            existing_links_in_all = {a['link'] for a in self.all_latest_articles}
            # [2026-09-16] **이미 있는 글의 제목을 새로 읽은 값으로 맞춘다.** 아래 두 append 는
            #   `link not in existing_links_in_all` 이라 **첫 관측 객체를 영원히 들고 간다.**
            #   그 스냅샷이 사이클 끝에 `get_all_latest_articles()` -> `data_export_callback`
            #   -> `save_community_posts()` 로 **DB 를 되덮는다**(`bg/export.py`). 그래서
            #   원본에서 제목이 바뀌면 이런 일이 벌어졌다(라이브 실측 2026-09-16):
            #       21:10:32  크롤 저장  '산성의 중재자' -> '..'      (DB 가 옳게 바뀜)
            #       21:11:41  사이클 끝  '..' -> '산성의 중재자'      (메모리가 되덮음)
            #   즉 **제목 변경이 1분마다 원복**돼 화면에는 영원히 옛 제목만 보였다. 부팅 시
            #   SQLite 선로드로 옛 제목이 메모리에 들어오므로 재시작해도 안 풀린다.
            #   **제목만 맞춘다** - `timestamp` 는 v6.6.102 가 일부러 보존하는 값이고, 본문·AI
            #   태그는 아래 `merged_map` 이 이미 보존 규약을 갖고 있다. 여기서 넓히지 않는다.
            _latest_by_link = {a['link']: a for a in self.all_latest_articles}
            for _a in articles:
                _old = _latest_by_link.get(_a.get('link'))
                if _old is not None and _old.get('title') != _a.get('title'):
                    _old['title'] = _a.get('title')
            max_memory = self.settings.get('MAX_MEMORY_ARTICLES', MAX_MEMORY_ARTICLES)

            if key not in self.seen_links: # First time processing for this game/source
                # Treat all current articles as new for initial population
                for article in articles: # [최적화] reversed 제거 (append+sort 방식 사용)
                    self.new_alerts[article['link']] = article
                    if article['detected_keywords']:
                        self.keyword_alerts[article['link']] = article
                        keyword_count += 1
                        # 초기 데이터도 중복 방지 목록에는 등록 (알림은 안 보냄)
                        if article.get('is_super_keyword'):
                            self.processed_alert_links.add(article['link'])
                    else:
                        new_count += 1

                    # 공지 알림 중복 방지 등록 (초기 데이터는 알림 미발송)
                    if article.get('is_official_keyword') and source == 'DAUM':
                        self.processed_official_alert_links.add(article['link'])

                    # 전체 최신글 목록에 추가 (링크 중복 검사 강화)
                    # [v6.7.158] sort 전 pop() 제거 — 인자 없는 pop() 은 마지막 원소(=방금 append한 새 글)를 즉시 제거하여 피드탭 신규글 누락. 상한 적용은 함수 말미 sort 직후로 일원화.
                    if article['link'] not in existing_links_in_all:
                        self.all_latest_articles.append(article)
                        existing_links_in_all.add(article['link']) # 루프 내 중복 방지 업데이트
            else: # Subsequent crawls
                new_links = current_links - self.seen_links[key]
                # [v6.6.527] 알림 트레이스 — 매칭 결정 시점에 ring buffer 기록.
                from utils import notification_trace as _ntrace
                for article in articles: # [최적화] reversed 제거
                    if article['link'] in new_links:
                        # 새 알림과 키워드 알림에 추가
                        self.new_alerts[article['link']] = article
                        if article['detected_keywords']:
                            self.keyword_alerts[article['link']] = article
                            keyword_count += 1

                            # 알림 수집 (슈퍼 키워드만 알림 전송하도록 복구)
                            if article.get('is_super_keyword'):
                                _is_dup = article['link'] in self.processed_alert_links
                                _ntrace.record(
                                    'match', kind='SUPER', game=game_name, source=source,
                                    title=article.get('title', ''), link=article.get('link', ''),
                                    keywords=list(article.get('detected_keywords') or []),
                                    result='dedupe' if _is_dup else 'queued',
                                )
                                if not _is_dup:
                                    alerts_to_emit.append(article)
                                    self.processed_alert_links.add(article['link'])

                        else:
                            new_count += 1

                        # 공지 알림 수집 (DAUM 전용, SUPER 흐름과 독립)
                        if article.get('is_official_keyword') and source == 'DAUM':
                            _is_dup_off = article['link'] in self.processed_official_alert_links
                            _ntrace.record(
                                'match', kind='OFFICIAL', game=game_name, source=source,
                                title=article.get('title', ''), link=article.get('link', ''),
                                keywords=list(article.get('detected_official_keywords') or []),
                                result='dedupe' if _is_dup_off else 'queued',
                            )
                            if not _is_dup_off:
                                official_alerts_to_emit.append(article)
                                self.processed_official_alert_links.add(article['link'])

                        # 전체 최신글 목록에 추가 (링크 중복 검사 강화)
                        # [v6.7.158] sort 전 pop() 제거 — 위 첫-사이클 분기와 동일 결함. 상한 적용은 함수 말미 sort 직후로 일원화.
                        if article['link'] not in existing_links_in_all:
                            self.all_latest_articles.append(article)
                            existing_links_in_all.add(article['link'])

            # 기존 데이터에 새 데이터를 병합하여 누적 (최대 PER_GAME_LIMIT개 유지)
            if game_name not in self.articles_by_game: self.articles_by_game[game_name] = {}
            if source not in self.articles_by_game[game_name]: self.articles_by_game[game_name][source] = []
            
            # 기존 데이터와 새 데이터 병합 (링크 기준 중복 제거)
            merged_map = {a['link']: a for a in self.articles_by_game[game_name][source]}
            for a in articles:
                # 기존 데이터가 있다면 중요 정보(content, AI 태그 등)를 보존 (덮어쓰기 방지)
                if a['link'] in merged_map:
                    existing = merged_map[a['link']]

                    # 1. 본문(content) 보존
                    if existing.get('content'):
                        a['content'] = existing['content']

                    # 2. AI 중요도(is_super_keyword) 보존
                    if existing.get('is_super_keyword') is True:
                        a['is_super_keyword'] = True

                    # 3. AI 픽 태그 보존
                    existing_kws = existing.get('detected_keywords', [])
                    new_kws = a.get('detected_keywords', [])
                    if "AI 픽" in existing_kws and "AI 픽" not in new_kws:
                        new_kws.append("AI 픽")
                        a['detected_keywords'] = new_kws

                    # 4. [v6.6.102] timestamp 보존
                    # 카페가 HH:MM만 보여주는 경우 크롤 시점에 따라 날짜가 흔들릴 수 있으므로,
                    # 최초 수집 시점에 찍힌 timestamp를 신뢰하고 재크롤링 시 덮어쓰지 않는다.
                    # (앱 재시작/seen_links 리셋/per_game_limit 밀림 등으로 재수집되더라도 원본 유지)
                    if existing.get('timestamp'):
                        a['timestamp'] = existing['timestamp']

                merged_map[a['link']] = a
            
            # 최신순 정렬 및 개수 제한
            merged_list = sorted(merged_map.values(), key=lambda x: x.get('timestamp', ''), reverse=True)
            per_game_limit = self.settings.get('PER_GAME_LIMIT', PER_GAME_LIMIT)
            self.articles_by_game[game_name][source] = merged_list[:per_game_limit]
            
            # seen_links를 현재 유지 중인 게시글 목록의 링크로 동기화 (덮어쓰기 방지)
            # 이렇게 하면 오래된 글은 자연스럽게 빠지고, 현재 유효한 글들은 유지됨
            self.seen_links[key] = {a['link'] for a in self.articles_by_game[game_name][source]}
            
            # [필수] 전체 최신글 목록 정렬 (append로 추가했으므로 정렬 필수)
            self.all_latest_articles.sort(key=lambda x: x.get('timestamp', ''), reverse=True)

            # [v6.7.158] 메모리 상한 적용 — 정렬 후 슬라이싱 (이전엔 append 직후 pop() 인자 누락으로 신규글 즉시 제거 → 피드탭 누락)
            if len(self.all_latest_articles) > max_memory:
                self.all_latest_articles = self.all_latest_articles[:max_memory]

            self.last_update = datetime.now()
            # [최적화] 데이터 변경 시점 기록
            self.last_articles_update_ts = time.time()
            self.last_all_articles_update_ts = time.time()
            self.status = "실행 중"
            
            if new_count > 0 or keyword_count > 0:
                # [v6.7.292] NAVER 도입 이전의 이진 분기 유물 정정 — NAVER/OFFICIAL 수집이
                # 'DC' 로 오태깅되어 레인별 로그 판독(v6.7.291 per-lane hang 진단)을 오염시켰음.
                log_tag = '카페' if source == 'DAUM' else source
                self._log(f"[{game_name}] {source} 수집: 새 글 {new_count}개, 키워드 {keyword_count}개", 'info', log_tag)

            # [Sticky Highlighting] 개별 게임 수집 시에는 임계값을 전진시키지 않습니다. (주기 완료 시점에 일괄 처리)
            # self._enrich_articles_for_web(articles, update_state=True)
            
            # [최적화] 실시간성을 위해 제목 등 기본 데이터는 업데이트하되, 표시 상태는 전진시키지 않고 현재 상태 유지
            if self.socketio and self.initial_crawl_done:
                # 개별 업데이트는 이제 최소화하거나 필요 시에만 수행
                pass 

            # 3. 슈퍼 키워드 알림 전송 (초기 크롤링 완료 후 발생한 건만)
            if self.initial_crawl_done and alerts_to_emit:
                # [통합] 알림 대상 필터링 (앱 실행 이후 작성된 글 + 슈퍼 키워드)
                real_alerts = []
                for article in alerts_to_emit:
                    # 슈퍼 키워드 확인
                    if not article.get('is_super_keyword'):
                        continue

                    # 작성 시간 확인 (앱 실행 이후인지)
                    try:
                        ts_str = str(article.get('timestamp', '')).strip()
                        if not ts_str: continue
                        article_dt = pd.to_datetime(ts_str).to_pydatetime()
                        if article_dt.tzinfo: article_dt = article_dt.replace(tzinfo=None)
                        
                        # 앱 실행 시점보다 늦게 작성된 글만 알림 대상
                        if article_dt > self.server_start_time:
                            real_alerts.append(article)
                    except Exception: continue

                if real_alerts:
                    # 1. 웹소켓 전송 (웹 앱, 확장프로그램)
                    if self.socketio:
                        self.socketio.emit('super_keyword_alert', real_alerts)

                    # 2. 카카오워크 전송 (공통)
                    self._send_kakaowork_alert(real_alerts)

            # 4. 공지 알림 전송 (DAUM 전용, 초기 크롤링 완료 후 + 앱 실행 이후 작성된 글만)
            if self.initial_crawl_done and official_alerts_to_emit:
                real_official = []
                for article in official_alerts_to_emit:
                    if not article.get('is_official_keyword'):
                        continue
                    try:
                        ts_str = str(article.get('timestamp', '')).strip()
                        if not ts_str: continue
                        article_dt = pd.to_datetime(ts_str).to_pydatetime()
                        if article_dt.tzinfo: article_dt = article_dt.replace(tzinfo=None)
                        if article_dt > self.server_start_time:
                            real_official.append(article)
                    except Exception: continue

                if real_official:
                    self._send_kakaowork_official_alert(real_official)

            # 첫 크롤링이 완료되었으므로 알림 활성화 플래그를 켭니다.
            if not self.initial_crawl_done:
                self.initial_crawl_done = True

            return new_count + keyword_count # 전체 추가된 글 개수 반환


    def extract_game_from_comment_link(self, comment_link: str):
        """
        [v6.6.238] 댓글 링크(raw _c21_/bbs_read?grpid=...)의 grpid 로 게임 식별.
        DAUM 카페 전용 (댓글 탭은 현재 카페 댓글만 수집).
        raw_config_games 의 Cafe_URL 에 grpid 부분문자열 매칭 → GameName 반환.
        매칭 실패 시 None.
        """
        if not comment_link or 'cafe.daum.net' not in comment_link:
            return None
        if not hasattr(self, 'raw_config_games') or not self.raw_config_games:
            return None
        try:
            m = re.search(r'grpid=([^&#]+)', comment_link)
            if not m:
                return None
            grpid = m.group(1)
            for g in self.raw_config_games:
                cafe_url = g.get('Cafe_URL') or ''
                if grpid and grpid in cafe_url:
                    return g.get('GameName')
        except Exception:
            return None
        return None

    def _normalize_daum_link(self, url: str) -> str:
        """
        다음 카페 링크를 표준 형식으로 변환합니다.
        slug 소스: contents_crawler.CAFE_ID_MAP (댓글 크롤러와 단일 소스 공유)
        입력: https://cafe.daum.net/_c21_/bbs_read?grpid=1YvZ5&fldid=DEH8&datanum=37066...
        출력: https://cafe.daum.net/odin/DEH8/37066
        """
        if not url or "cafe.daum.net" not in url:
            return url

        try:
            try:
                from .contents_crawler import CAFE_ID_MAP
            except ImportError:
                from contents_crawler import CAFE_ID_MAP
            parsed = urlparse(url)
            query = parse_qs(parsed.query)

            grpid = query.get('grpid', [''])[0]
            fldid = query.get('fldid', [''])[0]
            datanum = query.get('datanum', [''])[0]

            cafe_id = CAFE_ID_MAP.get(grpid)
            if cafe_id and fldid and datanum:
                return f"https://cafe.daum.net/{cafe_id}/{fldid}/{datanum}"
            return url
        except Exception:
            return url

    def generate_night_trend_from_input(self, sheet_link: str, trend_data: str) -> str:
        """사용자 입력으로 동향 보고서 생성 (엑셀 복사 포맷 지원)"""
        today_str = datetime.now().strftime('%Y-%m-%d')
        yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y. %m. %d")
        try:
            data_io = StringIO(trend_data.strip())
            reader = csv.reader(data_io, delimiter='\t', quotechar='"')
            parsed_records = [row for row in reader]
            
            # main_gui.py와 동일한 파싱 로직 적용 (줄바꿈 분리 대응)
            is_newline_separated = False
            if parsed_records:
                single_col_rows = sum(1 for row in parsed_records if len(row) <= 1)
                if len(parsed_records) > 1 and (single_col_rows / len(parsed_records) > 0.8):
                    is_newline_separated = True
            
            final_records = []
            if is_newline_separated:
                self._log("줄바꿈으로 분리된 데이터 감지. 재파싱을 시도합니다.", "warning", "TREND")
                lines = [line.strip() for line in trend_data.split('\n') if line.strip()]
                i = 0
                while i < len(lines):
                    if i + 2 >= len(lines): break
                    category = lines[i]
                    title = lines[i+1]
                    count = lines[i+2]
                    urls = []
                    i += 3
                    while i < len(lines) and (lines[i].startswith('http') or lines[i].startswith('www')):
                        urls.append(lines[i])
                        i += 1
                    final_records.append([category, title, count, '\n'.join(urls)])
            else:
                final_records = parsed_records
                
        except Exception as e:
            self._log(f"동향 데이터 CSV 파싱 오류: {e}", "error", "TREND")
            return f"데이터 파싱 오류: {e}"

        sections = defaultdict(list)
        for parts in final_records:
            if not parts: continue
            
            category = parts[0] if len(parts) > 0 else "기타"
            title = parts[1] if len(parts) > 1 else ""
            count = parts[2] if len(parts) > 2 else ""
            urls = parts[3] if len(parts) > 3 else ""
            
            sections[category].append({'title': title, 'count': count, 'urls': urls})

        report_lines = [f"금일 야간 동향 보고드립니다. {yesterday_str}", "", "[동향 취합 히스토리]", sheet_link,    ""]
        for cat, items in sections.items():
            # 카테고리 헤더 추가
            report_lines.append(f"\n[ {cat} ]\n")
            
            for it in items:
                title, cnt, urls = it['title'], it['count'], it['urls']
                count_display = ""
                if str(cnt).strip():
                    clean_cnt = str(cnt).strip().replace(" ", "") # 공백 제거
                    if clean_cnt.endswith('+'):
                        numeric_part = clean_cnt[:-1]
                        count_display = f" [{numeric_part}건 이상]"
                    else:
                        count_display = f" [{clean_cnt}건]"
                
                report_lines.append(f"{title}{count_display}")
                if urls:
                    # 다중 링크 처리: 줄바꿈으로 분리 후 각 줄의 앞뒤 공백 및 따옴표 제거
                    url_list = [u.strip().strip('"').strip("'") for u in urls.split('\n') if u.strip()]
                    formatted_urls = "\n".join(url_list)
                    report_lines.append(formatted_urls)
                    
                report_lines.append("")

        report_text = "\n".join(report_lines).strip()
        
        with self.lock:
            self.night_trends[today_str] = {'timestamp': datetime.now().isoformat(), 'report': report_text}
        
        self._log(f"사용자 입력으로 동향 생성: {today_str}", 'info', 'TREND')
        
        return report_text

    def get_status(self) -> str: return self.status
    def get_last_update(self) -> str: return self.server_start_time.strftime('%Y-%m-%d %H:%M:%S')
    def get_articles_by_game(self, limit: int = None) -> Dict: 
        with self.lock:
            result = {}

            for gn, src in self.articles_by_game.items():
                daum_list = src.get('DAUM', [])
                dc_list = src.get('DC', [])
                
                # 게임별 탭에서도 기본적으로 PER_GAME_LIMIT (100) 개수만큼 제한합니다.
                final_limit = limit if limit is not None else self.settings.get('PER_GAME_LIMIT', PER_GAME_LIMIT)
                daum_list = daum_list[:final_limit]
                dc_list = dc_list[:final_limit]
                
                result[gn] = {'daum': daum_list, 'dc': dc_list, 'total': len(daum_list) + len(dc_list)}
            return result

    def get_articles_by_community(self, limit: int = None, per_game_limit: int = None) -> Dict:
        """커뮤니티별(DAUM/DC)로 모든 게임의 게시글을 통합하여 반환.

        [v6.7.169] per_game_limit override 추가 — admin 커뮤니티 탭 디버깅용 전량 노출.
        값이 명시되면(>0) 게임별 상한을 그 값으로 대체 (Config PER_GAME_LIMIT / GAME_SPECIFIC_LIMITS 무시).
        None 이면 기존 동작 (Config 기반 per-game 상한) 유지 — 회귀 안전.
        """
        with self.lock:
            daum_articles = []
            dc_articles = []

            default_pgl = self.settings.get('PER_GAME_LIMIT', PER_GAME_LIMIT)
            for gn, sources in self.articles_by_game.items():
                # 각 게임별로 최대 N개까지만 가져와서 통합 리스트에 추가
                # 이렇게 하면 특정 게임(우마무스메)이 전체 리스트를 도배하는 것을 방지함
                if per_game_limit is not None and per_game_limit > 0:
                    game_limit = per_game_limit
                else:
                    game_limit = GAME_SPECIFIC_LIMITS.get(gn, default_pgl)
                g_daum = sources.get('DAUM', [])[:game_limit]
                g_dc = sources.get('DC', [])[:game_limit]
                
                daum_articles.extend(g_daum)
                dc_articles.extend(g_dc)
            
            daum_articles.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
            dc_articles.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
            
            if limit:
                daum_articles = daum_articles[:limit]
                dc_articles = dc_articles[:limit]
                
            return {'daum': daum_articles, 'dc': dc_articles}
    
    # [최적화] 변경 감지용 타임스탬프 반환 메서드
    def get_last_articles_update_ts(self) -> float: return self.last_articles_update_ts
    def get_last_all_articles_update_ts(self) -> float: return self.last_all_articles_update_ts

    def get_keyword_alerts(self) -> List[Dict]:
        with self.lock: return sorted(list(self.keyword_alerts.values()), key=lambda x: x.get('timestamp', ''), reverse=True)

    def get_all_latest_articles(self, limit: int = None, per_game_limit: int = None) -> List[Dict]:
        """모든 최신글 목록 반환.

        [v6.6.335] **서버 조회 시 Config_Settings 시트 값 무시** — 사용자 정책:
        "웹앱 표시 상한은 클라이언트 필터링 UI 에서만 적용한다".
        - `limit`, `per_game_limit` 모두 None 이면 **무제한** (메모리에 있는 전체 반환).
        - 값이 명시적으로 전달되면(예: 엑셀/Google Sheets 업로드용 GOOGLE_SHEET_UPLOAD_LIMIT)
          그 값은 그대로 존중.
        - 메모리 저장 측(`articles_by_game[game][source] = merged_list[:PER_GAME_LIMIT]`) 은
          여전히 Config_Settings 적용 — 메모리 보호용 독립 상한.
        """
        with self.lock:
            unlimited_per_game = (per_game_limit is None or per_game_limit <= 0)
            unlimited_total = (limit is None or limit <= 0)

            filtered_articles = []
            game_counts = defaultdict(int)

            for article in self.all_latest_articles:
                game = article.get('game', 'Unknown')

                if not unlimited_per_game:
                    if game_counts[game] >= per_game_limit:
                        continue
                    game_counts[game] += 1

                filtered_articles.append(article)

                if not unlimited_total and len(filtered_articles) >= limit:
                    break

            return filtered_articles

    def get_comments(self, limit=1000):
        """캐시된 댓글 데이터를 반환합니다. 캐시가 비어있으면 백그라운드 로드를 트리거합니다."""
        if not self._comments_cache:
            # 캐시가 비어있으면 백그라운드 스레드로 로드 (HTTP 요청 블로킹 방지)
            import threading
            t = threading.Thread(target=self.refresh_comments_cache, daemon=True)
            t.start()
            return []  # 아직 로드 안 됐으면 빈 배열 반환 (프론트에서 재시도)
        return self._comments_cache[:limit]

    def refresh_comments_cache(self):
        """[백엔드 캐시 갱신] Google Sheets에서 댓글 데이터를 새로 읽어 메모리 캐시를 갱신합니다."""
        try:
            self._log("댓글 캐시 갱신 시작...", 'info', 'COMMENTS')
            import sys, os
            sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'utils'))
            from google_sheets_handler import get_handler
            from contents_crawler import load_initial_data

            handler = get_handler(log_func=self._log)
            if not handler.gc:
                self._log("댓글 캐시 갱신 실패: 시트 인증 불가", 'warning', 'COMMENTS')
                return

            df = load_initial_data(handler)
            if df.empty:
                self._log("댓글 캐시 갱신: 데이터 없음", 'warning', 'COMMENTS')
                self._comments_cache = []
                return

            df = df.fillna("")
            records = df.to_dict('records')
            records.sort(key=lambda x: str(x.get('timestamp') or x.get('time') or x.get('crawl_date') or ""), reverse=True)
            self._comments_cache = records
            # [Sticky Highlighting] 댓글 데이터 로드 성공 시점에만 명시적으로 강조 임계값을 전진시킵니다.
            self._enrich_articles_for_web(records, update_state=True)
            self._log(f"댓글 캐시 갱신 완료: {len(records)}건", 'info', 'COMMENTS')
        except Exception as e:
            self._log(f"댓글 캐시 갱신 중 오류: {e}", 'error', 'COMMENTS')


    def restore_all_acked_articles(self):
        """확인된 모든 글을 복구합니다."""
        with self.lock:
            if not self.ack_all_alerts_history:
                return 0
            
            count = len(self.ack_all_alerts_history)
            # 복구 시 ack_time, ack_type 필드 제거
            restored_articles = []
            for acked_article in self.ack_all_alerts_history:
                original_article = {k: v for k, v in acked_article.items() if k not in ['ack_time', 'ack_type']}
                restored_articles.append(original_article)

            self.all_latest_articles.extend(restored_articles)
            self.ack_all_alerts_history.clear()
            
            # 시간순으로 다시 정렬
            self.all_latest_articles.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
            
            self._log(f"{count}개의 확인된 글을 복구했습니다.", 'info', 'RESTORE')
            return count

    def get_ack_history(self) -> List[Dict]:
        with self.lock: return list(self.ack_history)
    def clear_ack_history(self):
        with self.lock: self.ack_history.clear(); self._log('확인 기록 초기화', 'info', 'ACK_ALL')
    def export_ack_history_csv(self) -> str:
        with self.lock:
            sio = StringIO()
            writer = csv.DictWriter(sio, fieldnames=['ack_time', 'ack_type', 'source', 'game', 'title', 'link', 'detected_keywords'])
            writer.writeheader()
            for item in self.ack_history:
                writer.writerow({
                    'ack_time': item.get('ack_time', ''), 'ack_type': item.get('ack_type', ''),
                    'source': item.get('source', ''), 'game': item.get('game', ''),
                    'title': item.get('title', '').replace('\n', ' '),'link': item.get('link', ''),
                    'detected_keywords': ','.join(item.get('detected_keywords', []))})
            return sio.getvalue()
    def get_games(self) -> List[str]:
        # 카페 또는 DC 중 하나라도 설정된 모든 게임 목록 반환 (합집합)
        active_games = set(self.cafe_list.keys()) | set(self.dc_galleries.keys())

        # [fix] Cafe_URL/DC_URL 없이 Active=TRUE 만 설정된 게임도 포함.
        # 뷰어 모드는 URL 없이 GameName+Active 만으로 Config_Games를 운용하므로
        # URL 기반 active_games 만으론 get_games()가 [] 를 반환해 게임 섹션 전체가 사라지는 버그 방지.
        if hasattr(self, 'raw_config_games'):
            for row in self.raw_config_games:
                if self._is_true(row.get('Active')):
                    gn = row.get('GameName')
                    if gn:
                        active_games.add(gn)

        # 설정 파일(raw_config_games)의 순서를 우선적으로 따름
        if hasattr(self, 'raw_config_games') and self.raw_config_games:
            ordered_games = []
            seen = set()
            for row in self.raw_config_games:
                game_name = row.get('GameName')
                if game_name in active_games and game_name not in seen:
                    ordered_games.append(game_name)
                    seen.add(game_name)
            
            # 설정 파일에는 없지만 활성화된 게임이 있다면 뒤에 추가 (예외 처리)
            for game in sorted(list(active_games)):
                if game not in seen:
                    ordered_games.append(game)
            return ordered_games
            
        return sorted(list(active_games))
    def get_galleries(self) -> List[str]: return list(self.dc_galleries.keys()) # 인스턴스 변수 사용
    def get_night_report_games(self) -> List[str]: return self.night_report_games
    def get_ccu_games(self) -> List[str]: return self.ccu_games
    def get_logs(self, limit: int = 100, level: Optional[str] = None) -> List[Dict]:
        with self.log_lock: # 로그 전용 락 사용
            logs = self.logs[-limit:]
            return [l for l in logs if not level or l['level'].upper() == level.upper()]
    def clear_logs(self):
        with self.log_lock: # 로그 전용 락 사용
            self.logs.clear()
        self._log('로그 초기화', 'info', 'LOG')
    def get_night_trends(self, date: Optional[str] = None) -> Dict: 
        return self.night_trends.get(date, {}) if date else dict(list(self.night_trends.items())[-7:])
    def get_next_crawl_times(self):
        with self.lock:
            # 동적 작업 목록에서 시간 추출 (기존 앱 호환성 유지)
            return {
                'cafe': self.crawler_tasks['DAUM']['next_run'],
                'dc': self.crawler_tasks['DC']['next_run']
            }

    def get_community_urls(self):
        """커뮤니티 URL 목록 반환.

        [v6.7.110] cafe_list / dc_galleries value 가 List[str] 로 변경되었으나,
        frontend / gui_lib 의 기존 호출처는 단일 string 가정 → backward compat 으로 첫 URL 만 노출.
        multi-URL 전체 노출이 필요하면 별도 endpoint 또는 향후 확장.
        """
        return {
            'cafe': {g: (urls[0] if urls else '') for g, urls in self.cafe_list.items()},
            'dc':   {g: (urls[0] if urls else '') for g, urls in self.dc_galleries.items()},
        }

    def load_existing_data(self, data_list: List[Dict]) -> int:
        """기존 데이터(구글 시트 등)를 로드하여 메모리 상태를 복원합니다."""
        added_count = 0
        with self.lock:
            count = 0
            # 기존 링크를 조회하기 위한 맵 생성 (업데이트를 위해)
            existing_article_map = {a['link']: a for a in self.all_latest_articles if a.get('link')}
            
            # 게임별 목록의 객체들도 링크로 조회할 수 있게 매핑 (객체 분리 시 동기화를 위해)
            game_article_map = {}
            for game in self.articles_by_game:
                for source in self.articles_by_game[game]:
                    for a in self.articles_by_game[game][source]:
                        if a.get('link'):
                            game_article_map[a['link']] = a

            # [신규] 날짜 필터링 기준 설정 (오늘/어제 데이터만 로드)
            now = datetime.now(ZoneInfo("Asia/Seoul"))
            today_str = now.strftime('%Y-%m-%d')
            yesterday_str = (now - timedelta(days=1)).strftime('%Y-%m-%d')
            allowed_dates = {today_str, yesterday_str}

            import pandas as pd
            added_count = 0
            skipped_count = 0
            error_count = 0

            for item in data_list:
                # 필수 필드 확인
                if not item.get('link') or not item.get('game') or not item.get('source'):
                    error_count += 1
                    continue
                
                # [신규] 오래된 데이터 사전 필터링 (무한 삭제 루프 방지)
                try:
                    ts_str = str(item.get('timestamp', '')).strip()
                    if not ts_str:
                        ts_str = str(item.get('date', '')).strip()
                        if not ts_str:
                            error_count += 1
                            continue
                            
                    # Pandas를 사용하여 유연하게 파싱
                    article_dt = pd.to_datetime(ts_str).to_pydatetime()
                    if article_dt.tzinfo:
                        article_dt = article_dt.replace(tzinfo=None)
                        
                    # 날짜가 오늘/어제가 아니면 로드하지 않음 (단, 시트에서 직접 가져온 경우엔 좀 더 유연하게 허용)
                    # if article_dt.strftime('%Y-%m-%d') not in allowed_dates:
                    #     continue
                        
                    # 파싱에 성공했으면 안전한 ISO 8601 포맷으로 덮어씀
                    item['timestamp'] = article_dt.isoformat()
                except Exception:
                    error_count += 1
                    continue # 날짜 파싱 실패 시 건너뜀

                # 호환성 패치: 구버전 앱이 올린 Raw Link 정규화
                if 'cafe.daum.net' in item['link']:
                    item['link'] = self._normalize_daum_link(item['link'])

                link = item['link']
                
                # 이미 존재하는 링크라면 데이터 병합(업데이트) 시도
                if link in existing_article_map:
                    skipped_count += 1
                    existing_article = existing_article_map[link]
                    
                    # 1. Content 업데이트 (메모리에 없는데 시트에는 있는 경우 - AI 프로세서 작업 결과)
                    if not existing_article.get('content') and item.get('content'):
                        existing_article['content'] = item['content']
                        
                    # 2. AI 태그/중요도 업데이트
                    if not existing_article.get('is_super_keyword') and item.get('is_super_keyword') is True:
                        existing_article['is_super_keyword'] = True
                    
                    # 3. 키워드 업데이트 (AI 픽 추가 등)
                    existing_kws = existing_article.get('detected_keywords', [])
                    new_kws = item.get('detected_keywords', [])
                    
                    # new_kws가 리스트나 문자열이 아닌 경우(예: NaN, None) 빈 리스트로 초기화
                    if not isinstance(new_kws, (list, str)):
                        new_kws = []

                    if isinstance(new_kws, str): # 문자열로 왔을 경우 리스트 변환
                         new_kws = [k.strip() for k in new_kws.split(',') if k.strip()]
                    
                    if "AI 픽" in new_kws and "AI 픽" not in existing_kws:
                        existing_article['detected_keywords'].append("AI 픽")

                    # 게임별 목록(articles_by_game)에 있는 객체도 동기화
                    # (크롤링 과정에서 전체 목록과 게임별 목록의 객체가 분리되었을 경우를 대비)
                    if link in game_article_map:
                        game_article = game_article_map[link]
                        if game_article is not existing_article:
                            if not game_article.get('content') and item.get('content'):
                                game_article['content'] = item['content']
                            
                            if not game_article.get('is_super_keyword') and item.get('is_super_keyword') is True:
                                game_article['is_super_keyword'] = True
                                
                            g_kws = game_article.get('detected_keywords', [])
                            if "AI 픽" in new_kws and "AI 픽" not in g_kws:
                                g_kws.append("AI 픽")

                    # 이미 존재하므로 리스트에 추가하지 않고 다음으로 넘어감
                    continue
                
                # 새로운 링크라면 추가 진행
                added_count += 1
                existing_article_map[link] = item # 맵에도 추가하여 중복 방지

                # 키워드 재감지 (저장된 데이터의 키워드가 잘못되었을 경우를 대비해 현재 로직으로 갱신)
                # title이 없더라도 빈 문자열로 감지하여 기존 키워드를 초기화함
                title = item.get('title', '')
                
                # 원본 데이터의 키워드 파싱 (모델 미로드 환경 대비)
                original_kw_val = item.get('detected_keywords')
                original_kws = []
                if isinstance(original_kw_val, str):
                    original_kws = [k.strip() for k in original_kw_val.split(',') if k.strip()]
                elif isinstance(original_kw_val, list):
                    original_kws = original_kw_val

                kws, is_super = self._detect_keywords(title, item.get('board_name', ''))
                
                # 모델 유무와 상관없이, 기존 데이터에 'AI 픽' 태그가 있었다면 유지 (데이터 일관성)
                # 재로드 시마다 AI 예측값이 달라지거나(모델 변경 등), 모델 로드 실패 시 태그가 사라지는 것 방지
                had_ai_tag = any(tag in original_kws for tag in ["AI 픽", "AI감지"])
                if had_ai_tag:
                    if "AI 픽" not in kws:
                        kws.append("AI 픽")
                    is_super = True # AI 픽은 중요 글로 취급 (알림 대상)

                item['detected_keywords'] = kws
                item['is_super_keyword'] = is_super

                # 데이터 타입 복원 (문자열 -> 리스트/불리언)
                kw = item.get('detected_keywords')
                if isinstance(kw, str):
                    item['detected_keywords'] = [k.strip() for k in kw.split(',') if k.strip()]
                elif kw is None:
                    item['detected_keywords'] = []
                elif not isinstance(kw, list): # 리스트가 아닌 다른 타입(숫자 등)일 경우 안전하게 초기화
                    item['detected_keywords'] = []

                if 'is_super_keyword' in item and isinstance(item['is_super_keyword'], str):
                    item['is_super_keyword'] = item['is_super_keyword'].lower() == 'true'

                # date 필드가 없으면 timestamp 값을 기반으로 복구 (화면 표시용)
                if not item.get('date'):
                    ts = item.get('timestamp', '')
                    try:
                        item['date'] = pd.to_datetime(ts).strftime('%H:%M')
                    except Exception:
                        item['date'] = ''

                # 1899-12-30 날짜 제거 (구글 시트 시간 포맷 이슈 대응)
                if item.get('date'):
                    item['date'] = str(item['date']).replace('1899-12-30', '').strip()

                # 1. 전체 목록에 추가
                self.all_latest_articles.append(item)
                
                # 2. 게임별/소스별 분류 및 중복 방지 목록(seen_links) 복원
                game = item['game']
                source = item['source']
                
                if game not in self.articles_by_game: self.articles_by_game[game] = {}
                if source not in self.articles_by_game[game]: self.articles_by_game[game][source] = []
                
                self.articles_by_game[game][source].append(item)

            # seen_links 복원 (중복 수집 방지 핵심 로직)
            new_seen_links = defaultdict(set)
            for article in self.all_latest_articles:
                key = f"{article.get('source')}_{article.get('game')}"
                if key and article.get('link'):
                    new_seen_links[key].add(article.get('link'))
            self.seen_links = new_seen_links

            # 데이터 로드 후 최신순 정렬 (필수)
            self.all_latest_articles.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
            
            # 메모리 관리: 최대 개수 유지
            max_memory = self.settings.get('MAX_MEMORY_ARTICLES', MAX_MEMORY_ARTICLES)
            if len(self.all_latest_articles) > max_memory:
                self.all_latest_articles = self.all_latest_articles[:max_memory]
            
            # 게임별 목록도 정렬
            for game in self.articles_by_game:
                for source in self.articles_by_game[game]:
                    self.articles_by_game[game][source].sort(key=lambda x: x.get('timestamp', ''), reverse=True)

            # [신규] 초기 로드 시점의 가장 최신 시각을 current_max_ts로 설정하여 
            # 처음 불러오는 데이터들은 강조되지 않도록 함 (데스크탑 앱 로직 참고)
            for article in self.all_latest_articles:
                source_raw = str(article.get('source', '')).upper()
                link = str(article.get('link', ''))
                
                key = ''
                if 'comment_link' not in article and 'comment' not in article:
                    if 'DAUM' in source_raw or '카페' in source_raw: key = 'DAUM'
                    else: key = 'DC'
                else:
                    if 'dcinside.com' in link: key = 'DC_COMMENT'
                    else: key = 'DAUM_COMMENT'

                if not key: continue

                try:
                    ts_str = str(article.get('timestamp', '')).strip()
                    if ts_str:
                        import pandas as pd
                        dt = pd.to_datetime(ts_str).to_pydatetime()
                        if dt.tzinfo: dt = dt.replace(tzinfo=None)
                        if dt > self.current_max_ts[key]:
                            self.current_max_ts[key] = dt
                            self.highlight_threshold_ts[key] = dt # threshold도 똑같이 설정해서 신규 탐지 방지
                except Exception: pass

            # 기존 데이터를 로드했으므로 초기화 완료로 간주하고 알림 기능을 활성화합니다.
            self.initial_crawl_done = True

            return added_count