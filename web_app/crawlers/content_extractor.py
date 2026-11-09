# -*- coding: utf-8 -*-
"""
내용 크롤러 (content_extractor.py)
- 메인 크롤러가 수집한 글 중 content IS NULL인 행의 본문을 추출
- 별도 백그라운드 스레드로 비동기 실행
- DC: requests (로그인 불필요)
- 카페: Selenium 헤드리스 (JS 렌더링 필요)
"""
import re
import time
import random
import logging
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

# [v6.7.206] 본문 추출 일과성 실패 재시도 한도 — 한 번의 transient 실패로 영구 [FAILED] 박제 방지.
#   실패마다 '[FAILED:N]' 로 N 을 올리고, attempt+1 이 이 값에 도달하면 종료 마커 '[FAILED]'(재시도 끝).
_CONTENT_MAX_RETRIES = 3
# [v6.7.206] 카페 드라이버 연속 실패 임계 — 좀비 드라이버가 [FAILED] 만 양산하는 것을 self-heal(폐기→재생성).
_CAFE_FAIL_RESET_THRESHOLD = 5
# [v6.7.206] 마지막 본문 추출 성공 시각(time.monotonic) — contents.check_content_extractor_health 의 stall 가시화용.
_LAST_PROGRESS_TS = 0.0


def _extract_dc_content(link):
    """DC 갤러리 글 본문 추출 (로그인 불필요, requests 사용)"""
    try:
        headers = {'User-Agent': random.choice(USER_AGENTS)}
        resp = requests.get(link, headers=headers, timeout=10, allow_redirects=True)

        # 삭제된 글 감지: 404 또는 JS redirect(/derror/deleted/)
        if resp.status_code == 404 or "derror/deleted" in resp.text:
            return "[DELETED_POST]"

        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')

        if soup.select_one("div.error_notice") or "삭제된 게시물" in resp.text:
            return "[DELETED_POST]"

        content_div = soup.select_one(".write_div")
        if content_div:
            text = content_div.get_text("\n", strip=True)
            return text if text else "[IMAGE_ONLY]"
        return ""
    except Exception as e:
        logger.debug(f"[내용크롤러] DC 본문 추출 실패 ({link}): {e}")
        return ""


# ── 네이버 카페 본문 추출 (비회원 cafe-articleapi/v3, requests) ──────────────
# [v6.7.296] NAVER 소스는 기존에 아래 else(다음 카페 Selenium) 로 떨어져 iframe #down / #user_contents
#   구조 불일치로 전건 실패 → community_posts.content = '[FAILED:N]'. 알림 탭 네이버 공지 툴
#   (naver_cafe_watcher.fetch_notice_article) 이 검증한 비회원 API 방식(로그인/Selenium 불필요)을 계승해 해소.
_NAVER_CAFE_LINK_RE = re.compile(r"/cafes/(\d+)/articles/(\d+)")
_NAVER_ARTICLE_API = "https://apis.naver.com/cafe-web/cafe-articleapi/{ver}/cafes/{cid}/articles/{aid}"
# [v6.7.683] 엔드포인트 **버전은 우리가 고르는 값이 아니라 네이버가 끄는 값**이다 - 목록으로 두고 넘어간다.
#   2026-09-22 11:40 KST 경 `v3` 가 전건 500(`{"errorCode":"9999"}`)으로 돌아섰다. 이 파일이 v3 하나만
#   알고 있어서 그날 수집된 네이버 글이 **전건 `[FAILED]`(종결 마커)로 박제**됐다 - 본문이 없으면
#   `_PENDING_AI_WHERE` 가 걸러 AI 요약에서도 영영 빠진다. 라이브 실측(컨테이너에서, 같은 시각):
#     v3 · v2 · 파라미터 변형 6종 -> 전부 500 9999 / **v2.1 -> 200 + contentHtml 정상**
#     v2.1 의 404 = errorCode 4003(삭제) · 401 = 0004(회원공개) 로 **아래 분기 규약과 동일**
#     기존 본문 보유 글 5건 재추출 -> 4건 바이트 동일(남은 1건은 작성자가 글을 고친 것)
#   **마지막으로 성공한 버전에 고정한다** - 매 글마다 앞 버전부터 다시 타면 장애 내내 요청이 2배가
#   되고 그 낭비가 곧 차단 위험이다. 형제 도구 `naver_cafe_watcher` · `notice_alert/source_naver` 도
#   같은 날 같은 증상으로 멎었고, `jira_register/crawl_naver_cafe` 는 처음부터 v2.1 이라 무사했다.
_NAVER_API_VERSIONS = ("v2.1", "v3")
# [v6.7.684] **상류 전면 장애는 그 글의 실패가 아니다** - 재시도 한도를 태우면 안 된다.
#   `[FAILED:N]` 3회 -> `[FAILED]` 는 "일과성 실패" 를 전제한 기계인데, 2026-09-22 처럼 API 가
#   몇 시간 통째로 죽으면 그 전제가 깨진다 - 수집되는 족족 3회를 소모하고 **종결 마커로 굳어**
#   API 가 돌아와도 자동 복구되지 않는다(그날 385건이 그렇게 영구 누락됐다). 전 버전 5xx 일
#   때는 DB 를 건드리지 않고 그 글을 다음 사이클로 넘긴다. 표시값이 아니라 흐름 제어용이라
#   저장되지 않는다.
_UPSTREAM_DOWN = "\x00upstream-down"
_naver_api_ver_idx = 0
_naver_api_all_down = False     # 전 버전 5xx 상태 - 전이할 때만 로그(글마다 반복 금지)
# 네이버 에디터가 빈 단락에 삽입하는 제로폭 공백류 — strip 이 못 잘라 빈 줄로 남는 것 선제거.
_NAVER_ZW_TRANS = dict.fromkeys(map(ord, "​‌‍﻿⁠"), None)

# [v6.7.590] 회원 공개 글 대응 — 네이버 회원 쿠키(NID_AUT/NID_SES) 주입.
#   배그 공식카페 '🚨┃제보 게시판'(menuId 107)은 글이 전부 `openArticle=false`(= 카페 회원에게만
#   공개)라 비회원 API 가 **401 errorCode 0004** 를 준다. 게시판 자체는 `restrictMenu=false` 라
#   목록·제목이 비회원에게도 보여서, **제목만 쌓이고 본문은 전건 실패**하는 모양이 됐다 —
#   최초 수집 2026-05-22 이래 1,909건 중 본문 성공 6건(그 6건은 작성자가 전체공개로 올린 글).
#   같은 카페 자유/건의/질문 게시판은 전부 openArticle=true 라 비회원으로 200 이다.
#   실측·판별 근거 전문: `_workspace/2026-08-19_pubg_jebo_board_member_only.md`.
#
#   **자동 로그인은 하지 않는다.** 서버에서 `nid.naver.com/nidlogin.login` 을 Selenium 으로 시도하면
#   rcaptcha(영수증 이미지 문제)로 튕긴다 — 2026-08-19 실측: `form.action=nidlogin.rcaptcha`,
#   captcha 요소 visible, 행동지문 `#bvsd` 빈 값. 댓글 크롤러의 카카오(다음) 로그인은 이 관문이
#   없어서 성립하는 것이고 네이버는 다르다. 그래서 **이미 성립한 세션을 빌려 쓴다** — 사용자가
#   브라우저에서 로그인한 뒤 쿠키 2개만 admin 화면에 붙여넣고, 코드는 그것을 헤더로 싣기만 한다
#   (AME `crawl_naver_cafe.set_cookie` 와 같은 방식. 비밀번호는 받지도 저장하지도 않는다).
_NAVER_COOKIE_KEY = 'naver_cookie'
_NAVER_COOKIE_TTL = 60.0        # 매 글마다 DB 를 묻지 않는다. 갱신 반영은 최대 60초 지연.
_naver_cookie_cache = {'value': '', 'ts': -1e9}


def _get_naver_cookie():
    """system_settings 의 네이버 회원 쿠키 (`NID_AUT=..; NID_SES=..`). 없으면 빈 문자열."""
    now = time.monotonic()
    if now - _naver_cookie_cache['ts'] < _NAVER_COOKIE_TTL:
        return _naver_cookie_cache['value']
    value = _naver_cookie_cache['value']
    try:
        from sqlite_handler import db_handler as _db
        value = (_db.get_system_setting(_NAVER_COOKIE_KEY, '') or '').strip()
    except Exception as e:
        # 조회 실패 시 직전 값 유지 — 일과성 DB 오류로 쿠키가 사라진 것처럼 굴면 안 된다.
        logger.debug(f"[내용크롤러] 네이버 쿠키 조회 실패(직전 값 유지): {e}")
    _naver_cookie_cache['value'] = value
    _naver_cookie_cache['ts'] = now
    return value


_naver_auth_state = None        # **DB 에 실제로 들어간** 마지막 값 — 매 글마다 DB 를 쓰지 않기 위한 것
_naver_auth_logged = None       # 로그를 한 번만 내기 위한 별도 축 (아래 주석 참조)


def _set_naver_auth_state(state):
    """네이버 세션 상태를 system_settings 에 남긴다 — admin 화면이 이걸 읽어 표시한다.

    'ok'(회원 본문 열림) / 'expired'(쿠키는 있는데 401 = 세션 만료) / 'absent'(쿠키 없음).
    **조용한 만료가 최악**이므로(PATTERN-59) 상태가 바뀔 때 경고 로그도 함께 남긴다.
    전이 때만 쓰기 — 정상 사이클에서 글마다 DB 를 건드리지 않는다.

    [v6.7.592] **쓰기가 성공했을 때만 메모리를 전진**시킨다. 앞선 판은 메모리를 먼저 바꾸고
      DB 쓰기가 실패해도 그대로 뒀는데, 그러면 `database is locked` 한 번에 상태가 **영구
      미기록**으로 굳는다(전이가 끝났다고 보고 다시 안 쓴다) — 배포 직후 실측으로 드러났다.
      로그 축(`_naver_auth_logged`)을 분리한 이유는, 쓰기 재시도가 도는 동안 같은 경고를
      글마다 반복하지 않기 위해서다. 상태 자체는 다음 사이클에 다시 시도된다.
    """
    global _naver_auth_state, _naver_auth_logged
    if state == _naver_auth_state:
        return
    if state != _naver_auth_logged:
        _naver_auth_logged = state
        if state == 'expired':
            logger.warning("[내용크롤러] 네이버 쿠키가 만료됐다(401) — admin 화면에서 재발급 필요")
    try:
        from sqlite_handler import db_handler as _db
        if _db.set_system_setting('naver_cookie_status', state):
            _naver_auth_state = state
    except Exception as e:
        logger.debug(f"[내용크롤러] 네이버 인증 상태 기록 실패(다음 사이클 재시도): {e}")


def _clear_naver_cookie_cache():
    """admin 이 쿠키를 새로 저장했을 때 60초 TTL 을 기다리지 않게 하는 즉시 반영 훅."""
    _naver_cookie_cache['ts'] = -1e9


def _extract_naver_body_text(content_html):
    """네이버 카페 contentHtml → 본문 텍스트.
    naver_cafe_watcher._extract_body_text 계승: script/style 제거 + inline 요소 unwrap(한 문장이
    <a>/<span> 경계로 여러 줄 쪼개지는 것 방지) + 표는 행 단위(' | ') + 제로폭 공백 제거."""
    if not content_html:
        return ""
    try:
        soup = BeautifulSoup(content_html, "html.parser")
    except Exception:
        return ""
    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    for tag in soup.find_all(["a", "span", "b", "i", "em", "strong", "u", "font", "mark",
                              "small", "sub", "sup", "abbr", "label", "time", "ins", "del"]):
        tag.unwrap()
    soup.smooth()
    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            cells = [" ".join(c.get_text(" ", strip=True).split())
                     for c in tr.find_all(["td", "th"])]
            cells = [c for c in cells if c]
            if cells:
                rows.append(" | ".join(cells))
        table.replace_with("\n" + "\n".join(rows) + "\n" if rows else "\n")
    text = soup.get_text(separator="\n").translate(_NAVER_ZW_TRANS)
    lines = [" ".join(ln.split()) for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _naver_api_get(cafe_id, article_id, headers):
    """기사 API GET. **5xx 면 다음 엔드포인트 버전으로 넘어가고, 성공한 버전에 고정한다.**

    5xx 만 전환 사유로 본다 - 404/401 은 그 글에 대한 확정 답이라 버전을 바꿀 이유가 없고,
    timeout/연결오류는 버전 문제가 아니라 그대로 올려 호출자의 일과성 실패 경로에 태운다.
    전 버전이 5xx 면 마지막 응답을 그대로 돌려준다(호출자의 `raise_for_status` 가 처리).

    **전환·전면장애를 WARNING 으로 남기는 것이 이 함수의 절반**이다. 종전 실패 경로는
    `logger.debug` 하나뿐이라 2026-09-22 의 5시간이 로그에 한 줄도 안 남았다 - 추출기는
    다른 소스(DC)로 진행도를 갱신하고 있어서 stall 경고도 안 울렸다(소스별로는 못 본다).
    """
    global _naver_api_ver_idx, _naver_api_all_down
    total = len(_NAVER_API_VERSIONS)
    resp = None
    first_bad = None
    for offset in range(total):
        idx = (_naver_api_ver_idx + offset) % total
        ver = _NAVER_API_VERSIONS[idx]
        url = (_NAVER_ARTICLE_API.format(ver=ver, cid=cafe_id, aid=article_id)
               + "?query=&useCafeId=true&requestFrom=A")
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code >= 500:
            if first_bad is None:
                first_bad = (ver, resp.status_code)
            continue
        if idx != _naver_api_ver_idx:
            logger.warning(f"[내용크롤러] 네이버 기사 API 버전 전환 "
                           f"{_NAVER_API_VERSIONS[_naver_api_ver_idx]}({first_bad[1]}) -> {ver} "
                           f"- 이후 {ver} 로 고정")
            _naver_api_ver_idx = idx
        if _naver_api_all_down:
            _naver_api_all_down = False
            logger.warning(f"[내용크롤러] 네이버 기사 API 복구 ({ver})")
        return resp
    if not _naver_api_all_down:
        _naver_api_all_down = True
        logger.error(f"[내용크롤러] 네이버 기사 API 전 버전 5xx "
                     f"({'/'.join(_NAVER_API_VERSIONS)}) - 본문 추출 전면 정지. "
                     f"마지막 응답: {resp.status_code if resp is not None else '?'} "
                     f"{(resp.text[:120] if resp is not None else '')}")
    return resp


def _extract_naver_content(link):
    """네이버 카페 글 본문 추출 — 비회원 cafe-articleapi/v3 (Selenium/로그인 불필요).
    link (https://cafe.naver.com/f-e/cafes/{cafe_id}/articles/{article_id}) 에서 cafe_id/article_id 파싱.
    삭제/존재하지 않는 글 = 404(errorCode 4003) → '[DELETED_POST]' (실측 2026-07-05). DC 추출기와 반환 규약 동일."""
    m = _NAVER_CAFE_LINK_RE.search(link or "")
    if not m:
        logger.debug(f"[내용크롤러] NAVER link 형식 인식 실패: {link}")
        return ""
    cafe_id, article_id = m.group(1), m.group(2)
    try:
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            # br 제외 — 슬림 컨테이너 brotli 미설치 시 br 응답 디코드 실패 회피 (poe_watcher v1.0.35 교훈).
            "Accept-Encoding": "gzip, deflate",
            "Referer": f"https://cafe.naver.com/f-e/cafes/{cafe_id}",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        }
        # [v6.7.590] 회원 공개 글은 이 쿠키가 있어야 열린다. 없으면 기존 비회원 동작 그대로.
        cookie = _get_naver_cookie()
        had_cookie = bool(cookie)
        if cookie:
            headers["Cookie"] = cookie
        resp = _naver_api_get(cafe_id, article_id, headers)
        if resp is not None and resp.status_code >= 500:
            # [v6.7.684] 전 버전 5xx = 네이버 쪽 전면 장애. 이 글의 문제가 아니므로 재시도
            #   한도를 태우지 않는다(위 _UPSTREAM_DOWN 주석). 로그는 _naver_api_get 이 이미 냈다.
            return _UPSTREAM_DOWN
        if resp.status_code == 401 and had_cookie:
            # [v6.7.594] **쿠키를 실었는데 401 이면 원인이 둘 겹쳐 있다** — (a) 쿠키가 만료됐거나
            #   (b) 글이 회원 공개인데 우리 계정이 못 읽거나. 그대로 종결하면 (a) 일 때 **원래 잘
            #   읽히던 공개 글까지 재시도 없이 `[MEMBER_ONLY]` 로 박제**된다(종결 마커라 자동 복구 없음).
            #   쿠키를 빼고 한 번 더 물어 둘을 가른다: 200 이면 공개 글이었고 범인은 쿠키다 —
            #   본문은 살리고 상태만 'expired' 로 남긴다. 또 401 이면 진짜 회원 공개 글이다.
            #
            #   [v6.7.685] **그 판별을 코드가 안 하고 있었다** - 재시도 결과를 보기도 전에
            #   'expired' 를 먼저 썼다. 그래서 **쿠키가 멀쩡해도 회원공개 글을 만날 때마다**
            #   상태가 'expired' 로 뒤집히고 "재발급 필요" 경고가 나갔다(2026-09-22 실측: 하루 19건,
            #   같은 날 대조군으로 쿠키 유효 확인 - 공개 글은 쿠키를 실어도 200, 회원공개 글은
            #   쿠키 유무와 무관하게 401). 상태가 글마다 expired<->ok 로 진동하니 admin 화면의
            #   표시는 **마지막에 처리된 글이 무엇이었나**를 보여줄 뿐이었고, 경고는 늑대소년이 됐다.
            #   **비회원 재시도가 200 일 때만** 'expired' 로 쓴다 - 그것이 "쿠키가 범인" 의 유일한
            #   증거다. 또 401 이면 쿠키에 대해 아무것도 알 수 없으므로 **상태를 건드리지 않는다**
            #   (모르는 것을 단정하지 않는다 - PATTERN-81).
            headers.pop("Cookie", None)
            cookie = ''   # 재시도는 비회원 요청 — 성공해도 상태를 'ok' 로 되돌리지 않는다
            resp = _naver_api_get(cafe_id, article_id, headers)
            if resp is not None and resp.status_code >= 500:
                return _UPSTREAM_DOWN
            if resp is not None and resp.status_code < 400:
                _set_naver_auth_state('expired')
        if resp.status_code == 404:   # 삭제/존재하지 않는 글
            return "[DELETED_POST]"
        if resp.status_code == 401:
            # [v6.7.590] 회원 공개 글(openArticle=false). 재시도로 뚫리는 종류가 아니므로 **종결
            #   마커**로 박제한다 — `[FAILED:N]` 3회 재시도에 태우면 하루 ~75회를 확정 실패에 쓴다.
            #   쿠키를 새로 넣으면 admin 경로가 이 마커를 되돌려 재수집한다(requeue_member_only_posts).
            if not had_cookie:
                _set_naver_auth_state('absent')
            # [v6.7.685] 쿠키가 있었는데 여기까지 왔다면 **비회원 재시도도 401** 이었다는 뜻이다
            #   = 진짜 회원공개 글. 쿠키의 생사에 대해서는 아무것도 알 수 없으므로 상태를 안 쓴다.
            return "[MEMBER_ONLY]"
        resp.raise_for_status()
        if cookie:
            _set_naver_auth_state('ok')   # 회원 세션으로 200 을 받았다 = 살아 있다
        art = resp.json().get("result", {}).get("article", {})
        if not isinstance(art, dict) or not art:
            # 200 이지만 article 부재 = 비공개/회원제 등 — 확정 불가 → 재시도 경로(빈 문자열)
            return ""
        text = _extract_naver_body_text(art.get("contentHtml") or "")
        return text if text else "[IMAGE_ONLY]"
    except Exception as e:
        logger.debug(f"[내용크롤러] NAVER 본문 추출 실패 ({link}): {e}")
        return ""


def _extract_cafe_content(driver, link):
    """다음 카페 글 본문 추출 (Selenium, JS 렌더링 필요)"""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    try:
        driver.get(link)

        # iframe 전환
        try:
            WebDriverWait(driver, 5).until(
                EC.frame_to_be_available_and_switch_to_it((By.ID, "down"))
            )
        except Exception:
            pass

        # 삭제된 글 감지
        page_source = driver.page_source
        if "삭제된 게시물입니다" in page_source or "해당 글은 삭제되었습니다" in page_source:
            driver.switch_to.default_content()
            return "[DELETED_POST]"

        # 본문 추출
        content_el = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.ID, "user_contents"))
        )
        content_text = content_el.text.strip()
        driver.switch_to.default_content()

        if content_text:
            return content_text
        return ""
    except Exception as e:
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
        logger.debug(f"[내용크롤러] 카페 본문 추출 실패 ({link}): {e}")
        return ""


def _create_headless_driver():
    """내용 크롤러 전용 헤드리스 드라이버 생성"""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    options = Options()
    options.page_load_strategy = 'eager'
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-setuid-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-zygote")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36")
    options.add_argument("--lang=ko_KR")
    options.add_argument("--disable-extensions")
    options.add_argument("--mute-audio")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    options.add_experimental_option('useAutomationExtension', False)
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-features=VizDisplayCompositor")
    options.add_argument("--password-store=basic")
    options.add_experimental_option("prefs", {
        "profile.default_content_setting_values.notifications": 2,
    })

    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(15)
    return driver


def run_content_extractor(stop_event=None, log_func=None, interval=30, batch_size=20, since=None):
    """
    백그라운드 스레드로 실행. content IS NULL인 글의 본문을 추출하여 SQLite에 저장.

    :param stop_event: threading.Event -- 중단 신호
    :param log_func: 로그 출력 함수 (msg, level, tag)
    :param interval: 배치 처리 후 대기 시간 (초)
    :param batch_size: 한 번에 처리할 글 수
    :param since: ISO 문자열 -- 이 시각 이후 수집된 글만 처리 (None이면 전체)
    """
    global _LAST_PROGRESS_TS

    def _log(msg, level='info'):
        if log_func:
            log_func(msg, level, 'CONTENT')
        else:
            print(f"[CONTENT] {msg}")

    # SQLite 핸들러 로드
    try:
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'utils'))
        from sqlite_handler import db_handler as _db
    except Exception as e:
        _log(f"SQLite 핸들러 로드 실패: {e}", 'error')
        return

    # 카페용 Selenium 드라이버 (필요 시 생성)
    cafe_driver = None

    def _get_cafe_driver():
        nonlocal cafe_driver
        if cafe_driver is None:
            try:
                cafe_driver = _create_headless_driver()
                _log("카페 본문 추출용 헤드리스 드라이버 생성 완료.")
            except Exception as e:
                _log(f"헤드리스 드라이버 생성 실패: {e}", 'error')
        return cafe_driver

    _log(f"내용 크롤러 시작 (배치: {batch_size}건, 대기: {interval}초, 기준시각: {since or '전체'})")

    consecutive_cafe_failures = 0  # [v6.7.206] 카페 드라이버 연속 실패 카운터 (self-heal 용)
    try:
        while True:
            if stop_event and stop_event.is_set():
                _log("내용 크롤러 중단 신호 수신.")
                break

            try:
                # 1. 미수집 글 조회 (since 이후 삽입된 글만)
                pending = _db.get_posts_without_content(limit=batch_size, since=since)
                if not pending:
                    if stop_event:
                        stop_event.wait(interval)
                    else:
                        time.sleep(interval)
                    continue

                extracted_count = 0
                failed_count = 0
                # [v6.7.684] 이 배치에서 네이버 상류가 죽은 것을 확인했는가.
                #   확인했으면 같은 배치의 나머지 네이버 글은 **요청조차 하지 않는다**
                #   (같은 답이 확정이고, 죽은 엔드포인트를 두드리는 것은 차단 위험만 키운다).
                #   DC·다음·공식은 그대로 진행한다 - 한 소스의 장애로 전체를 세우지 않는다.
                naver_upstream_down = False

                for post in pending:
                    if stop_event and stop_event.is_set():
                        break

                    link = post['link']
                    source = (post.get('source') or '').upper()

                    # 2. 소스별 본문 추출
                    if 'DC' in source:
                        content = _extract_dc_content(link)
                    elif source == 'NAVER' and naver_upstream_down:
                        continue      # [v6.7.684] 상류 장애 확정 - DB 미변경으로 다음 사이클에 다시 잡힌다
                    elif source == 'NAVER':
                        # [v6.7.296] 네이버 카페 — 비회원 API(requests). 기존엔 else(다음 카페 Selenium)로
                        #   떨어져 iframe 구조 불일치 전건 [FAILED]. 알림 탭 네이버 공지 툴 방식 계승.
                        content = _extract_naver_content(link)
                    elif source == 'OFFICIAL_WEB':
                        # [v6.7.90 → v6.7.136] 공식 홈페이지 본문 추출 — 도메인별 fetcher 분기.
                        #   PUBG (bbs-pubg.kakaogames.com / bbs.pubg.game.daum.net): div.view_cont > div.content
                        #   POE  (poe.kakaogames.com / poe.game.daum.net): table.forumPostListTable > tr:first > div.content
                        #   Selenium 불필요 — HTTP+BS4. Gemma4 AI 요약까지 자연 흐름.
                        try:
                            from urllib.parse import urlparse as _urlparse
                            host = (_urlparse(link).hostname or '').lower()
                            # [v6.7.279] POE KR 도메인 이전 - poe.kakaogames.com 추가(구 poe.game.daum.net 병행).
                            if 'poe.kakaogames.com' in host or 'poe.game.daum.net' in host:
                                from crawlers.official_poe import fetch_poe_post_content
                                content = fetch_poe_post_content(link)
                            else:
                                # PUBG (bbs-pubg.kakaogames.com / bbs.pubg.game.daum.net) + 미지 도메인 fallback
                                from crawlers.official_pubg import fetch_pubg_post_content
                                content = fetch_pubg_post_content(link)
                        except Exception as e:
                            _log(f"OFFICIAL_WEB 본문 추출 실패: {e}", 'warning')
                            content = ""
                    else:
                        drv = _get_cafe_driver()
                        if drv:
                            content = _extract_cafe_content(drv, link)
                            # [v6.7.482] 추출 직후 페이지 비우기 — 상주 드라이버의 renderer 메모리 반납.
                            #   cafe_driver 는 while 루프 *밖* lazy 생성이라 스레드 수명 내내 산다(실측 19h36m).
                            #   그동안 마지막으로 연 카페 페이지가 renderer 에 그대로 남아, %CPU 0.0 인 유휴
                            #   상태로 711MB 를 물고 있었다(renderer 3개 = 393MB, 나머지 318MB 는 프로세스 골격).
                            #   quit() 대신 about:blank 인 이유: 카페 글 유입 간격이 중앙값 7.7분/평균 13.9분
                            #   (48h 205건 실측)이라 유휴 타임아웃은 하루 11~26회 재기동을 부르고, 그 기동이
                            #   PATTERN-31(Selenium 기동 → CPU 포화)의 트리거다. 페이지만 비우면 재기동 0회로
                            #   renderer 쪽을 상시 반납한다. 실패는 삼킨다 — 드라이버 이상은 아래 self-heal 담당.
                            try:
                                drv.get("about:blank")
                            except Exception:
                                pass
                        else:
                            content = ""
                        # [v6.7.206] 카페 드라이버 self-heal — 연속 실패 임계 도달 시 드라이버 폐기→재생성.
                        if content:
                            consecutive_cafe_failures = 0
                        else:
                            consecutive_cafe_failures += 1
                            if consecutive_cafe_failures >= _CAFE_FAIL_RESET_THRESHOLD:
                                _log(f"카페 드라이버 연속 {consecutive_cafe_failures}회 실패 — 드라이버 재생성", 'warning')
                                if cafe_driver:
                                    try:
                                        cafe_driver.quit()
                                    except Exception:
                                        pass
                                cafe_driver = None
                                consecutive_cafe_failures = 0

                    # 3. 결과 저장
                    #   [v6.7.590] 종결 마커 표 — 재시도로 뚫리지 않는 결과는 여기서 끝낸다.
                    #   추출기 반환값 → DB 저장값. `[MEMBER_ONLY]` 는 쿠키를 새로 넣으면
                    #   admin 경로가 되돌려 재수집한다(그 전까지 재시도는 낭비).
                    terminal = {"[DELETED_POST]": "[DELETED]", "[MEMBER_ONLY]": "[MEMBER_ONLY]"}
                    if content == _UPSTREAM_DOWN:
                        # [v6.7.684] 상류 전면 장애 - **아무것도 쓰지 않는다**. content 가 그대로
                        #   NULL/[FAILED:N] 로 남아 다음 사이클에 다시 잡히고, API 가 돌아오면
                        #   사람 손 없이 복구된다. 실패 카운트에도 넣지 않는다(이 글의 실패가 아니다).
                        naver_upstream_down = True
                        continue
                    if content in terminal:
                        _db.update_post_content(link, terminal[content])
                        failed_count += 1
                    elif content:
                        _db.update_post_content(link, content)
                        extracted_count += 1
                        _LAST_PROGRESS_TS = time.monotonic()  # [v6.7.206] 진행도 갱신(stall 가시화)
                    else:
                        # [v6.7.206] 일과성 실패 N회 한정 재시도 — '[FAILED:N]' 카운트업 후 한도 도달 시 종료 '[FAILED]'.
                        #   한 번의 transient 실패로 본문이 영구 [FAILED] 박제되어 AI 요약에서 누락되는 것을 방지.
                        prev = post.get('content') or ''
                        attempt = 0
                        if prev.startswith('[FAILED:') and prev.endswith(']'):
                            try:
                                attempt = int(prev[len('[FAILED:'):-1])
                            except ValueError:
                                attempt = 0
                        if attempt + 1 < _CONTENT_MAX_RETRIES:
                            _db.update_post_content(link, f"[FAILED:{attempt + 1}]")
                        else:
                            _db.update_post_content(link, "[FAILED]")
                        failed_count += 1

                    # 요청 간 딜레이 (봇 탐지 방지)
                    time.sleep(random.uniform(1.0, 2.5))

                if extracted_count > 0 or failed_count > 0:
                    _log(f"배치 완료: {extracted_count}건 추출, {failed_count}건 실패/삭제 (미수집 {len(pending)}건 중)")

            except Exception as e:
                error_msg = str(e)
                _log(f"내용 크롤러 오류: {error_msg}", 'error')

                # 드라이버 비정상 종료 시 재생성
                critical_errors = ["invalid session", "no such window", "chrome not reachable", "disconnected", "Connection refused"]
                if any(err in error_msg for err in critical_errors):
                    _log("드라이버 연결 끊김 감지. 다음 배치에서 재생성합니다.", 'warning')
                    if cafe_driver:
                        try:
                            cafe_driver.quit()
                        except Exception:
                            pass
                    cafe_driver = None

            # 배치 간 대기
            if stop_event:
                stop_event.wait(interval)
            else:
                time.sleep(interval)
    finally:
        # 종료 시 드라이버 정리
        if cafe_driver:
            try:
                cafe_driver.quit()
                _log("카페 드라이버 종료.")
            except Exception:
                pass

    _log("내용 크롤러 종료.")
