# crawlers package initialization

class DriverSessionDead(RuntimeError):
    """Selenium 드라이버 세션 자체가 죽었다 - **URL 단위 실패가 아니다.**

    [2026-09-16 실사고] chromedriver 가 죽은 뒤 9시간 동안 NAVER·DAUM 수집이 0건이었다.
    회복 기계(`_crawl_naver_with_driver` / `_crawl_daum_with_driver` 의 quit -> None ->
    `_get_driver()` 재생성)는 **이미 있었고 정상이었다.** 안 돈 이유는 그 위 세 층이
    전부 드라이버 사망을 **정상 반환으로 번역**했기 때문이다:

      1. `naver_cafe.crawl_naver_cafe` - URL 단위 except 가 삼키고 빈 list 반환
      2. `crawler._crawl_naver` - 그 위에서 또 except -> continue
         (`_crawl_daum` 은 `is_connection_error` 로 **감지까지 하고 `return`** 했다)
      3. hang 판정 - "예외 없이 반환 = hang 아님" 이라 lane/task 시계가 계속 갱신

    그래서 스레드 alive · 레인 alive · 태스크 alive · 수집 0건이 9시간 공존했다.
    이 예외는 그 번역을 끊는 자리다 - **던져야 위의 재생성이 돈다.**
    """
