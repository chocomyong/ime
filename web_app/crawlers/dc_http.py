"""[v6.7.486] DC 갤러리 목록 HTTP fetch — Selenium 대체 경로 (fetch 계층만).

**왜 있나 (실측 근거, 2026-08-05)**
`dc_driver`(전용 Chrome)가 갤러리 목록 6장을 가져오는 데 **CPU 0.50 코어 + 메모리 934MB(PSS)**
를 쓴다 — 120초·100초 창 2회(0.504 / 0.501) + 병렬 세션의 92초 창 1회(0.564) = n=3 수렴.
2코어 호스트의 25%p 다. 목록 페이지는 **서버 렌더링 HTML** 이라 브라우저가 필요 없고,
`requests` + 기존 셀렉터로 같은 데이터가 그대로 나온다(컨테이너 안 실측 12/12 200,
갤러리당 48~50건 추출, CPU 240ms/요청 = 6갤러리/45초 기준 **0.032 코어**).
→ 절감 약 0.47 코어(호스트 23.5%p) + 934MB.

**역사 — 왜 Selenium 이었나 (2026-08-05 운영자 확인으로 정정됨)**
이 레포는 2026-01-05 ~ 02-14 **약 6주간 DC 를 requests 로 크롤**했다(당시 7갤러리 /
52~64초 = 약 10,430 요청/일 — 현행 Selenium 11,520 요청/일의 **90%**). `836dc87f` 가
Selenium 으로 옮기며 403/429 감지를 함께 넣어 "차단당해서 옮긴 것" 처럼 보이지만,
**실제 차단은 IME 목록 크롤이 아니라 당시 함께 돌던 AI 프로세서(2026-07-05 은퇴)가
앱 시작 시 미수집 본문을 일괄 크롤하다 발생한 것**이다(운영자 실측). 즉 목록 크롤에
대한 차단 근거는 없고, 6주치 무사고 운영 기록이 그 반대편에 있다.

**파싱은 여기 없다.** `crawler.py` 의 `_crawl_dc_pages` 가 selenium/http 양쪽 HTML 을
**같은 코드로** 파싱한다 — fetch 계층만 갈아끼우는 구조라 파싱 정확도 리스크가 구조적으로 0 이다.

**cloudscraper 는 쓰지 않는다.** 옛 구현은 우선 사용했으나 평 `requests` 로 12/12 200 이
확인됐고, 차단 시 처방은 우회가 아니라 **Selenium 자동 복귀**(crawler.py)다.
"""

import logging
import threading

import requests

logger = logging.getLogger(__name__)

# 세션 고정 UA. crawler.py 의 UA 풀(로테이션)을 쓰지 않는 이유 — HTTP 는 브라우저가 아니라
# TLS/헤더 지문이 UA 와 무관하게 고정된다. 고정 IP 에서 UA 만 순환하는 것은 정상 사용자
# 패턴이 아니며(오늘 §11-2 기록), naver_cafe_watcher 도 세션당 UA 고정 방식이다.
# 값은 컨테이너 실제 Chrome(136)에 맞춘다 — 선언 UA 와 실제 엔진을 어긋나게 두지 않는다.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36")

_BASE_HEADERS = {
    'User-Agent': _UA,
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7',
    'Accept-Encoding': 'gzip, deflate',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
}

_session = None
_session_lock = threading.Lock()


def _get_session():
    """세션 재사용 (keep-alive). DC 레인은 단일 스레드지만 reset 과의 경합만 락으로 막는다."""
    global _session
    with _session_lock:
        if _session is None:
            s = requests.Session()
            s.headers.update(_BASE_HEADERS)
            # netrc stat 회피 (PATTERN-24 계열 — 이 앱은 netrc/proxy 환경변수를 쓰지 않는다)
            s.trust_env = False
            _session = s
        return _session


def reset_session():
    """세션 폐기 — 차단 감지 후 새 연결로 재시작할 때 호출."""
    global _session
    with _session_lock:
        if _session is not None:
            try:
                _session.close()
            except Exception:
                pass
        _session = None


def fetch_list_html(url: str, timeout: int = 20):
    """DC 갤러리 목록 페이지 1장을 가져온다.

    Returns:
        (html, status_code) — 200 이 아니면 html 은 None.
    Raises:
        requests.RequestException — 네트워크 오류/타임아웃 (호출부가 재시도·실패 카운트 처리).

    차단(403/429) 판정은 **호출부(crawler.py)** 가 status_code 로 한다 — 이 모듈은
    crawler.py 를 import 하지 않아야 하므로(순환 방지, PATTERN-05) 예외를 정의하지 않는다.
    """
    sess = _get_session()
    # Referer 를 자기 자신으로 — 옛 구현(836dc87f^)이 쓰던 값. 목록 → 목록 이동으로 보인다.
    resp = sess.get(url, headers={'Referer': url}, timeout=timeout)
    if resp.status_code != 200:
        return None, resp.status_code
    return resp.text, 200
