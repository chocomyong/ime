# -*- coding: utf-8 -*-
"""crawlers/official_poe.py — POE 한국 공식 홈페이지 (poe.game.daum.net) HTTP 크롤러.

[활성 / 폐기 금지 — 검수 2026-05-31]
  본 모듈은 웹앱 통합 모니터링 피드의 "공식 홈페이지 게시판" 메인 수집기다
  (배그 `official_pubg.py` 의 POE 형제). 발화 경로:
    - crawler.py:296   주기 크롤 태스크 dict 등록 (_crawl_official_with_driver)
    - crawler.py:451~499  2분 폴링이 Config_Games 의 Official_URL → self.official_urls 갱신
    - crawler.py:1682  dispatcher {'POE1': crawl_poe_official} 로 호출 ([v6.7.279] 'POE2'→'POE1')
  게이트: self.official_urls.get('POE1') 존재(=시트 POE1 행 셀에 URL 있음) 시에만 발화. 셀 비면 휴면.
  [v6.7.279] 게임 태그 분리: 시트는 POE1 행 단일이나 'news2'/'patch-notes2' 등 '2' suffix 보드는 POE2 로 태깅.
  poe_watcher(pathofexile.com 글로벌 티저/공지 → 카카오워크 알림)와 역할이 다름 — 중복 아님, 보완 관계.
  시트 게이트 의존이라 "안 쓰는 레거시 아닌가?" 로 오인하기 쉬우나 조건부 활성일 뿐이다.
  오인 폐기 시 웹앱 피드에서 POE 한국 공식 게시판 글이 영구 소실된다. 폐기 금지.

`official_pubg.py` 동등 패턴 — 정적 HTML 서버사이드 렌더링, requests + BeautifulSoup.
Selenium 불필요 → 메모리/CPU 부담 거의 없음.

크롤 대상 (시트 SSoT — `Config_Games` POE1 행의 `Official_URL` 셀이 단일 출처):
- 예: https://poe.kakaogames.com/forum/view-forum/news (POE1) , .../news2 (POE2 분리)
- (사용자가 시트 셀에 콤마/줄바꿈 구분으로 추가하면 즉시 활성. 코드 변경 0.)
- [v6.7.279] KR POE 도 도메인 이전 - poe.game.daum.net → poe.kakaogames.com (301 redirect, HTML 구조 동일).

PUBG 와의 핵심 차이 (selector / 시간 형식):
- PUBG: `<a class="link_subject">` / `<td class="cell_date">` / "HH:MM" 또는 "YY.MM.DD"
- POE : `td.thread > div.thread_title > div.title > a` / `div.postBy > span.post_date` /
        "YYYY. M. D. 오전/오후 H:MM:SS" 절대 시간 (한국어 12시간제)

오늘 글 식별:
- PUBG : `HH:MM` 형식만 오늘 글 (`.` 포함 = 다른 날 skip)
- POE  : 절대 시간 파싱 후 `dt.date() == now.date()` 비교 (날짜 정보 항상 포함)

반환 article dict 스키마 (community_posts 와 정합, PUBG 동일):
- link (절대 URL, 게시글 직링크 = `/forum/view-thread/{threadId}`)
- title
- game (board 별 POE1/POE2 = board_meta['game'], 시트 Config_Games 의 GameName 과 일치 필수)
- source = 'OFFICIAL_WEB' (frontend feed-tab.js:281 가 '공식' 라벨로 렌더)
- timestamp (ISO 8601 KST naive)
- date ('HH:MM' 표시용 — frontend common.js:formatTime 이 ISO timestamp 우선 사용,
  date 는 fallback. PUBG 와 동등 박제)
- board_name ('POE1 자유' / 'POE2 소식' 등 — 게임 prefix + _BOARD_NAME_MAP 매핑)
- detected_keywords (caller 가 _detect_keywords 실행)
- is_super_keyword (caller 가 설정)
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


# [v6.7.279] KR POE(poe.kakaogames.com) 는 한 사이트에 POE1/POE2 보드 공존 — 'news2'/'patch-notes2'
#   처럼 '2' suffix 보드 = POE2 전용. 게임 태그는 board segment 로 분리(시트 Config_Games 행은 POE1 단일).
GAME_NAME_DEFAULT = "POE1"   # '2' suffix(POE2 전용) 외 기본 게임. 시트 Config_Games 의 GameName 과 일치 필수.
SOURCE = "OFFICIAL_WEB"

_THREAD_ID_RE = re.compile(r"/view-thread/(\d+)")

# URL path segment → board_name 라벨 매핑 (게임 prefix 는 _infer_game_and_base 가 별도 부착).
# /forum/view-forum/news → 'POE1 소식' , /forum/view-forum/news2 → 'POE2 소식'
_BOARD_NAME_MAP = {
    'free': '자유',
    'news': '소식',
    'notice': '공지',
    'announcements': '공지',
    'patch-notes': '패치노트',
    'event': '이벤트',
    'support': '문의',
    'help': '도움말',
    'bug-reports': '버그',
    'feedback': '피드백',
}


def _infer_game_and_base(segment_key: str, default_game: str) -> Tuple[str, str]:
    """board segment → (game, base_segment). '2' suffix(news2 / patch-notes2 …) = POE2 전용 보드.
    그 외는 default_game. KR POE(poe.kakaogames.com) 가 POE1/POE2 보드 공존(실측 2026-06-29)."""
    if len(segment_key) > 1 and segment_key.endswith('2'):
        return 'POE2', segment_key[:-1]
    return default_game, segment_key


# [v6.7.282] 제목 기반 POE2 마커 — event/news/notice 처럼 '2' suffix 가 아니라 board 휴리스틱상 POE1 로 떨어지는
#   공용 게시판에 올라온 POE2 콘텐츠를 글 단위로 POE2 로 보정. board-segment 만으론 분리 불가(예: event 보드의
#   '패스 오브 엑자일 2 - 0.5.0 고대의 귀환 시즌 이벤트 대축제 당첨자 발표' → board=POE1 오태깅).
#   보수적 마커만(확정적 POE2 신호) — POE1 버전은 3.x 라 '0.5.0'/'2'/'고대의 귀환(Ancients)' 은 POE2 고유.
#   ('선조의 귀환(Ancestors)' = POE1 3.29 와 혼동 방지 위해 '귀환' 단독 아닌 '고대의 귀환' 전체 매칭.)
_POE2_TITLE_MARKERS = ("패스 오브 엑자일 2", "poe2", "poe 2", "0.5.0", "고대의 귀환")


def _title_is_poe2(title: str) -> bool:
    t = (title or "").lower()
    return any(m in t for m in _POE2_TITLE_MARKERS)


def _extract_thread_id(url: str) -> str:
    """URL 에서 threadId 추출. dedup 보조 키. 추출 실패 시 url 그대로 반환."""
    m = _THREAD_ID_RE.search(url or "")
    return m.group(1) if m else (url or "")


def _extract_board_meta(url: str, default_game: str = GAME_NAME_DEFAULT) -> Dict[str, str]:
    """URL → {url, base, board_name, game} 자동 추출. 게임은 board segment 로 추론(POE1/POE2 분리).

    예: https://poe.kakaogames.com/forum/view-forum/news  → game 'POE1', board_name 'POE1 소식'
        https://poe.kakaogames.com/forum/view-forum/news2 → game 'POE2', board_name 'POE2 소식'

    글 상세 페이지 URL (`/forum/view-thread/N`) 이 절대 path 라 base 는 scheme+host 만으로 충분.
    """
    try:
        p = urlparse(url)
    except Exception:
        return {'url': url, 'base': url, 'board_name': f'{default_game} unknown', 'game': default_game}
    base = f"{p.scheme}://{p.netloc}"
    # path = '/forum/view-forum/news2' → segments [..., 'news2'] → last = 'news2'
    segments = [s for s in (p.path or '').strip('/').split('/') if s]
    segment_key = segments[-1] if segments else 'unknown'
    game, base_key = _infer_game_and_base(segment_key, default_game)
    board_label = _BOARD_NAME_MAP.get(base_key, base_key)
    board_name = f"{game} {board_label}"
    return {'url': url, 'base': base, 'board_name': board_name, 'game': game}


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}


_DATETIME_RE = re.compile(
    r"(\d{4})\.\s*(\d+)\.\s*(\d+)\.\s*(오전|오후)\s*(\d+):(\d+):(\d+)"
)


def _parse_korean_datetime(text: str, now: datetime) -> Optional[datetime]:
    """`<span class="post_date">` 텍스트가 "YYYY. M. D. 오전/오후 H:MM:SS" 형식이면 datetime 반환.

    예: ", 2026. 5. 24. 오후 3:07:58" → datetime(2026, 5, 24, 15, 7, 58)
    선두의 ", " (`<span class="post_date">` 텍스트가 ", 2026..." 으로 시작) 자동 strip.

    오늘 글 식별 책임은 caller — 본 함수는 단순 파서. caller 가 `dt.date() == now.date()` 비교.
    None 반환 = 파싱 실패 (caller 가 skip).
    """
    if not text:
        return None
    text = text.strip().lstrip(',').strip()
    m = _DATETIME_RE.search(text)
    if not m:
        return None
    try:
        yyyy = int(m.group(1))
        mm = int(m.group(2))
        dd = int(m.group(3))
        ampm = m.group(4)
        hh = int(m.group(5))
        mi = int(m.group(6))
        ss = int(m.group(7))
        # 한국어 12시간제 → 24시간 변환
        # 오전 12:XX = 0:XX (자정), 오후 12:XX = 12:XX (정오)
        if ampm == '오전' and hh == 12:
            hh = 0
        elif ampm == '오후' and hh < 12:
            hh += 12
        return datetime(yyyy, mm, dd, hh, mi, ss)
    except (ValueError, AttributeError):
        return None


def _parse_board(html: str, board: Dict[str, str], now: datetime) -> List[Dict]:
    """단일 게시판 HTML 파싱 → article dict 리스트 (오늘 글만).

    selector 명세 (사용자 제공 raw HTML 기반, 2026-05-25):
    - 게시글 row: `<tr>` (헤더 row 는 thread title 없어 자동 skip)
    - 제목 + 글 URL: `td.thread > div.thread_title > div.title > a`
    - 작성 시각:   `td.thread > div.postBy > span.post_date` (선두 ", " strip 후 파싱)
    - threadId:    href `/forum/view-thread/(\\d+)` 정규식

    주의: `td.last_post > div.post_date > a` 는 마지막 댓글 시각 — 작성일 아님. 사용 금지.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as e:
        logger.warning(f"[OFFICIAL] {board['board_name']} BeautifulSoup 파싱 실패: {e}")
        return []

    articles: List[Dict] = []
    seen_ids = set()
    for tr in soup.find_all("tr"):
        # td.thread 가 있어야 게시글 row (헤더 / 페이지네이션 등 자동 skip)
        td_thread = tr.find("td", class_="thread")
        if not td_thread:
            continue
        title_div = td_thread.find("div", class_="title")
        if not title_div:
            continue
        link_el = title_div.find("a")
        if not link_el:
            continue
        title = link_el.get_text(strip=True)
        href = (link_el.get("href") or "").strip()
        if not title or not href:
            continue

        # 절대 URL 변환 — POE href 는 `/forum/view-thread/N` 절대 path 라 base = scheme+host
        if href.startswith("http://") or href.startswith("https://"):
            full_url = href
        else:
            full_url = board["base"] + (href if href.startswith("/") else "/" + href)

        # 작성 시각 — `div.postBy > span.post_date`
        post_by = td_thread.find("div", class_="postBy")
        if not post_by:
            continue
        post_date_span = post_by.find("span", class_="post_date")
        if not post_date_span:
            continue
        date_text = post_date_span.get_text(strip=True)
        dt = _parse_korean_datetime(date_text, now)
        if dt is None:
            continue  # 파싱 실패 → skip
        # 오늘 글만 — PUBG 와 동일 정책
        if dt.date() != now.date():
            continue

        # threadId 기준 dedup
        thread_id = _extract_thread_id(full_url)
        if thread_id in seen_ids:
            continue
        seen_ids.add(thread_id)

        # [v6.7.282] 제목 기반 POE2 보정 — 공용 보드(event/news 등, board=POE1)의 POE2 글을 글 단위로 승격.
        art_game = board.get('game', GAME_NAME_DEFAULT)
        art_board_name = board.get('board_name', '')
        if art_game != 'POE2' and _title_is_poe2(title):
            art_game = 'POE2'
            art_board_name = re.sub(r'^POE1\b', 'POE2', art_board_name)  # 'POE1 이벤트' → 'POE2 이벤트'

        articles.append({
            "link": full_url,
            "title": title,
            "game": art_game,
            "source": SOURCE,
            "timestamp": dt.isoformat(),
            "date": dt.strftime("%H:%M"),
            "board_name": art_board_name,
            "detected_keywords": [],
            "is_super_keyword": False,
        })
    return articles


def crawl_poe_official(urls: List[str], game_name: str = GAME_NAME_DEFAULT, timeout: int = 15) -> List[Tuple[Dict[str, str], List[Dict]]]:
    """POE 한국 공식 홈페이지(poe.kakaogames.com) multi-URL 크롤링 — 시트 SSoT.

    인자 `urls` — `Config_Games` POE1 행의 `Official_URL` 셀에서 split 된 URL 리스트.
    인자 `game_name` — 기본 게임(POE1). board segment 가 '2' suffix 면 `_extract_board_meta` 가 POE2 로 분리 태깅.
    각 URL → `_extract_board_meta()` 로 board_name/base/game 자동 추출 → `_parse_board()` 호출.

    반환: [(board_meta, articles), ...] — URL 별 결과 튜플(board_meta['game'] = POE1|POE2). 빈 리스트 = 전부 실패.
    """
    now = datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)
    results: List[Tuple[Dict[str, str], List[Dict]]] = []
    if not urls:
        return results
    for url in urls:
        board = _extract_board_meta(url, default_game=game_name)
        articles: List[Dict] = []
        try:
            resp = requests.get(board["url"], headers=_HEADERS, timeout=timeout)
            resp.raise_for_status()
            articles = _parse_board(resp.text, board, now)
            logger.info(f"[OFFICIAL] {board['board_name']} 수집: {len(articles)}건 (오늘 글만)")
        except requests.exceptions.RequestException as e:
            logger.warning(f"[OFFICIAL] {board['board_name']} HTTP 요청 실패: {e}")
            board["error"] = str(e)[:200]   # [v6.7.739] 디스패처 연속 실패 감지용(official_pubg 와 같은 계약)
        except Exception as e:
            logger.error(f"[OFFICIAL] {board['board_name']} 처리 오류: {e}")
            board["error"] = str(e)[:200]
        results.append((board, articles))
    return results


# ─────────────────────────────────────────────────────────────────────────────
# 본문 추출 — content_extractor.py 가 OFFICIAL_WEB source + poe.game.daum.net 도메인
# 만나면 본 함수 호출. crawler.py:_crawl_official_with_driver dispatcher 와 정합.
# ─────────────────────────────────────────────────────────────────────────────
# HTML 구조 (실측 2026-05-25 https://poe.game.daum.net/forum/view-thread/3932937):
#   <h1 class="topBar last layoutBoxTitle">제목</h1>
#   <table class="forumTable forumPostListTable">
#       <tr> <td> ... <div class="content">본문</div> ... </td> </tr>  ← OP (첫 row)
#       <tr> ... </tr>  ← 댓글
#   </table>
# = `table.forumPostListTable` 안 첫 `<tr>` 의 `div.content` = OP 본문.


def fetch_poe_post_content(link: str, timeout: int = 15) -> str:
    """단일 POE 글 본문 추출.

    반환:
    - 본문 텍스트 (str) — 성공
    - "[DELETED_POST]" — HTTP 404 / 본문 컨테이너 없음 (글 삭제 / 구조 변경)
    - "" (빈 문자열) — 기타 실패 (caller 가 [FAILED] 마킹)
    """
    try:
        resp = requests.get(link, headers=_HEADERS, timeout=timeout)
    except requests.exceptions.RequestException as e:
        logger.warning(f"[OFFICIAL-CONTENT] POE HTTP 요청 실패: {link[:80]} — {e}")
        return ""

    if resp.status_code == 404:
        return "[DELETED_POST]"
    if resp.status_code != 200:
        logger.warning(f"[OFFICIAL-CONTENT] POE HTTP {resp.status_code}: {link[:80]}")
        return ""

    try:
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        logger.warning(f"[OFFICIAL-CONTENT] POE BS4 파싱 실패: {e}")
        return ""

    # 본문 — table.forumPostListTable 안 첫 <tr> 의 div.content
    table = soup.find("table", class_="forumPostListTable")
    if not table:
        return "[DELETED_POST]"
    first_tr = table.find("tr")
    if not first_tr:
        return "[DELETED_POST]"
    content_div = first_tr.find("div", class_="content")
    if not content_div:
        return "[DELETED_POST]"

    # <br> → \n, 인용 등 nested element 자연 처리
    try:
        text = content_div.get_text(separator="\n", strip=True)
    except Exception as e:
        logger.warning(f"[OFFICIAL-CONTENT] POE 텍스트 추출 실패: {e}")
        return ""

    # 정규화 — 연속 빈줄 1개로, ZWSP / BOM 제거
    text = (text or "").replace("﻿", "").replace("​", "").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)

    if not text:
        # 본문 영역은 있으나 텍스트 0 — 이미지/임베드만 있는 글 가능성. 빈 본문도 valid.
        return ""

    return text
