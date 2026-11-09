# -*- coding: utf-8 -*-
"""crawlers/naver_cafe.py — 네이버 카페 신 UI (/f-e/) 크롤러.

[v6.7.113] 신 UI SPA — Selenium 으로 페이지 로딩 후 page_source → BeautifulSoup parse.
초기 HTML 은 빈 shell 이라 JS 렌더링 완료 (a.article 박힐 때까지) 대기 후 dom 캡처.
**로그인 세션을 쓰지 않는다** — 2026-08-20 claim audit 정정. 종전 이 줄은 "로그인 세션 필수
(회원제 카페 다수)" 였는데 **한 번도 참인 적이 없었다**: caller(`crawler.py`)의 공유 driver 는
`_get_driver()` 에 `user-data-dir` 지정이 없어 **매 기동 임시 프로필 = 쿠키 0** 이다. 현재 크롤
대상(배그 공식카페)은 목록·제목이 비회원에게 열려 있어(`restrictMenu=false`) 로그인 없이
3개월+ 정상 수집해 왔다. 회원제 카페를 새로 넣으면 빈 목록이 오는데, 그건 "세션 만료"가 아니라
**처음부터 세션이 없는 것**이다 — 이 구분을 못 하면 있지도 않은 로그인을 고치려 든다(실제로
이 문장이 `jira_register` 의 설계 근거로 인용됐다). **본문은 별개다**: `content_extractor` 가
회원 쿠키(`system_settings.naver_cookie`)를 실어 회원 공개 글을 읽는다(v6.7.590~).

크롤 대상 (시트 SSoT — `Config_Games.NaverCafe_URL` 셀, 콤마 구분 multi-URL):
- 전체글보기: https://cafe.naver.com/f-e/cafes/{cafe_id}/menus/0
- 특정 게시판: https://cafe.naver.com/f-e/cafes/{cafe_id}/menus/{menu_id}

각 <tr> 행 구조 (사용자 박제 2026-05-22):
- td 1: <a class="board_name" href="/f-e/cafes/{cafe_id}/menus/{menu_id}">{board_name}</a>
- td 2: <div class="board-list"><div class="inner_list">
          <a class="article" href="https://cafe.naver.com/f-e/cafes/{cafe_id}/articles/{article_id}?referrerAllArticles=true">
            <span class="head">[prefix]</span>{title}
          </a>
- td 3: <div class="ArticleBoardWriterInfo">...nickname...</div>  (수집 안 함)
- td 4: <td class="td_normal type_date">{HH:MM 또는 YY.MM.DD}</td>
- td 5: <td class="td_normal type_readCount">{count}</td>  (수집 안 함)

정책 (DAUM/DC/OFFICIAL 와 동일):
- HH:MM (오늘 글) 만 수집. YY.MM.DD 는 skip.
- 사용자 명시 수집 필드: 게시판 이름 / 글 제목 / 작성일.

반환 article dict 스키마 (community_posts 와 정합):
- link (절대 URL, query param 정규화 후 — referrerAllArticles 제거)
- title
- board_name (예: "🏰┃클랜/방송/디스코드" / "✒️┃자유게시판")
- date ('HH:MM' 표시용)
- source = 'NAVER'
- timestamp (ISO 8601 KST naive)
- (caller 가 game / detected_keywords / is_super_keyword / content 박음)
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from crawlers import DriverSessionDead
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

logger = logging.getLogger(__name__)

SOURCE = 'NAVER'

_ARTICLE_ID_RE = re.compile(r"/articles/(\d+)")


def _driver_alive(driver) -> bool:
    """드라이버가 아직 명령을 받는가. **예외 타입으로 가르지 않고 드라이버에게 직접 묻는다.**

    타입 판별을 안 쓰는 이유: chromedriver 프로세스가 죽으면 selenium 예외가 아니라 urllib3 의
    `MaxRetryError` 가 그대로 올라온다(라이브 로그 2026-09-16 실측 -
    `HTTPConnectionPool(host='localhost', port=46676) ... Connection refused`). 타입·문자열로
    가르면 새 모양이 나올 때마다 조용히 빠져나가고, 그 조용함이 곧 9시간 무수집이었다.
    명령 하나(`driver.title`)를 실제로 태워서 판정하면 모양이 바뀌어도 판정이 안 흔들린다.
    죽은 세션에 대한 이 호출은 즉시 실패한다(라이브 실측 - 연결 거부 3회 재시도가 1ms 내)."""
    try:
        _ = driver.title
        return True
    except Exception:
        return False


def _normalize_link(url: str) -> str:
    """네이버 카페 article URL 정규화 — query param (referrerAllArticles 등) 제거.
    dedup key 안정화 + 사용자 표시 URL 깔끔."""
    if not url:
        return url
    if '?' in url:
        return url.split('?', 1)[0]
    return url


def _extract_article_id(url: str) -> Optional[str]:
    """articleId 추출 — dedup key 보조."""
    m = _ARTICLE_ID_RE.search(url or '')
    return m.group(1) if m else None


def _parse_today_time(text: str, now: datetime) -> Optional[datetime]:
    """`<td class="type_date">` HH:MM → 오늘 날짜+시각 datetime.
    YY.MM.DD (다른 날) 또는 잘못된 형식 → None (caller skip).
    DAUM/DC/OFFICIAL 와 동일 정책."""
    text = (text or '').strip()
    if not text:
        return None
    if '.' in text:    # YY.MM.DD
        return None
    if ':' not in text:
        return None
    try:
        hh_str, mm_str = text.split(':', 1)
        hh, mm = int(hh_str), int(mm_str)
        if not (0 <= hh < 24 and 0 <= mm < 60):
            return None
        return datetime(now.year, now.month, now.day, hh, mm, 0)
    except (ValueError, AttributeError):
        return None


def _parse_row(tr, now: datetime) -> Optional[Dict]:
    """단일 <tr> → article dict 또는 None (skip).

    skip 조건:
    - a.article 없음 (헤더/광고/regulated 행)
    - title 빈 문자열
    - date HH:MM 형식 아님 (어제 이전 글 / 알 수 없는 형식)
    """
    article_a = tr.find('a', class_='article')
    if not article_a:
        return None
    href = (article_a.get('href') or '').strip()
    if not href:
        return None

    title = article_a.get_text(strip=True)
    if not title:
        return None

    # board_name — a.board_name. 전체글보기에서는 항상 있어야 정상.
    board_a = tr.find('a', class_='board_name')
    board_name = board_a.get_text(strip=True) if board_a else ''

    # date — td.type_date
    date_td = tr.find('td', class_='type_date')
    date_text = date_td.get_text(strip=True) if date_td else ''
    parsed_dt = _parse_today_time(date_text, now)
    if parsed_dt is None:
        return None

    link = _normalize_link(href)
    return {
        'link': link,
        'title': title,
        'board_name': board_name,
        'date': date_text,
        'source': SOURCE,
        'timestamp': parsed_dt.isoformat(),
    }


def parse_naver_cafe_html(html: str, now: Optional[datetime] = None) -> List[Dict]:
    """page_source → article dict list.

    오늘 글 (HH:MM) 만 수집. articleId 기준 dedup (sticky / 일반 리스트 중복 노출 케어).
    """
    if now is None:
        now = datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)
    try:
        soup = BeautifulSoup(html, 'html.parser')
    except Exception as e:
        logger.warning(f"[NAVER] BeautifulSoup 파싱 실패: {e}")
        return []

    articles: List[Dict] = []
    seen_ids = set()
    for tr in soup.find_all('tr'):
        try:
            article = _parse_row(tr, now)
            if not article:
                continue
            aid = _extract_article_id(article['link'])
            if aid:
                if aid in seen_ids:
                    continue
                seen_ids.add(aid)
            articles.append(article)
        except Exception as e:
            logger.debug(f"[NAVER] row 파싱 오류: {e}")
            continue
    return articles


def crawl_naver_cafe(driver, urls: List[str], wait_timeout: int = 15) -> List[Tuple[Dict[str, str], List[Dict]]]:
    """[v6.7.113] 네이버 카페 multi-URL 크롤링 — driver 공유.

    인자:
    - driver: Selenium driver (caller 가 로그인 세션 박은 상태로 전달)
    - urls: Config_Games.NaverCafe_URL 셀에서 split 된 URL list
    - wait_timeout: SPA 렌더링 대기 (a.article / a.board_name 박힐 때까지)

    반환: [(url_meta, articles), ...] — URL 별 결과. 빈 list = driver 없음 또는 urls 비어있음.
    """
    now = datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)
    results: List[Tuple[Dict[str, str], List[Dict]]] = []
    if not driver or not urls:
        return results
    for url in urls:
        url_meta = {'url': url}
        articles: List[Dict] = []
        try:
            driver.get(url)
            # SPA 렌더링 대기 — article 또는 board_name 박힐 때까지.
            # 로그인 만료 / 비회원 카페 진입 시 a.article 안 박혀 timeout (caller 에 빈 list 반환).
            try:
                WebDriverWait(driver, wait_timeout).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "a.article, a.board_name"))
                )
            except Exception as e:
                logger.warning(
                    f"[NAVER] {url[:60]} 렌더링 대기 실패 (SPA 미완료 또는 로그인 만료): {e}"
                )
                # 렌더링 대기 실패도 드라이버 사망으로 올 수 있다 - 같은 판정을 여기에도 건다
                #   (한쪽만 고치면 다른 쪽이 그대로 조용한 출구가 된다 - PATTERN-72).
                if not _driver_alive(driver):
                    logger.error("[NAVER] WebDriver 세션 사망 감지(렌더링 대기) - 상위로 전파한다")
                    raise DriverSessionDead(f"NAVER 렌더링 대기 중 드라이버 세션 사망: {e}") from e
                results.append((url_meta, []))
                continue
            html = driver.page_source
            articles = parse_naver_cafe_html(html, now)
            logger.info(f"[NAVER] {url[:60]} 수집: {len(articles)}건 (오늘 글만)")
        except DriverSessionDead:
            raise            # 렌더링 대기 분기에서 이미 판정됐다 - 다시 감싸지 않는다
        except Exception as e:
            logger.error(f"[NAVER] {url[:60]} 처리 오류: {e}")
            # [2026-09-16] **여기서 삼키면 상위의 드라이버 재생성이 영영 안 돈다.**
            #   URL 하나가 실패한 것과 드라이버가 죽은 것은 처방이 정반대다(전자는 다음 URL 로,
            #   후자는 브라우저 재시작). 구 코드는 둘을 같은 except 로 받아 **후자도 '다음 URL'**
            #   로 흘려보냈고, 그 결과 죽은 세션(cc6fc5ba…)에 9시간 동안 같은 요청을 쐈다.
            if not _driver_alive(driver):
                logger.error("[NAVER] WebDriver 세션 사망 감지 - 상위로 전파해 재생성을 유도한다")
                raise DriverSessionDead(f"NAVER 크롤 중 드라이버 세션 사망: {e}") from e
        results.append((url_meta, articles))
    return results
