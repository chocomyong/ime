# -*- coding: utf-8 -*-
"""google-genai 클라이언트 생성 - **재시도 상한을 건 한 자리** (v6.7.620).

**왜 따로 두나**: SDK 기본 재시도가 우리 폴백과 곱해져 시간을 태운다. 그 상한은 클라이언트를
만드는 모든 자리에 걸려야 하는데, 웹앱만 해도 생성부가 여럿이라 각자 복제하면 서로 다른 속도로
늙는다. 여기 하나만 두고 전부 이걸 부른다.

**근거 - 실측 2026-08-28 (google-genai 1.63.0, 라이브 컨테이너에서 직접 확인)**:

    _RETRY_ATTEMPTS = 5   # 초기 호출 포함
    백오프 1 / 2 / 4 / 8초 (지수 + 지터)
    대상 408 · 429 · 500 · 502 · 503 · 504

즉 503 한 번에 SDK 가 5회를 때린다 - **키당 137초**(11:42:38 -> 11:44:55 실측). 이 앱들은 이미
자체 폴백(모델 체인 · 키 라운드로빈 · 재큐)을 갖고 있으므로 그 층의 5회는 **중복 증폭**이다.
`attempts=2`(1회 재시도)면 순간 커넥션 블립은 여전히 흡수하면서 상위 폴백이 제때 돈다.

**`timeout` 과 달리 `retry_options` 는 1.63 에서 실제로 먹는다** - 라이브에서 확인했다
(`Client(...)._api_client._http_options.retry_options` 에 그대로 실리고 `retry_args()` 가
tenacity 설정으로 변환한다). `timeout` 은 #911/#681 로 미적용이라 여기 걸지 않는다.
**[2026-09-28 정정] `timeout` 은 1.63 에서도 서버까지 실린다** - 10초 미만을 주면 서버가 `Minimum allowed deadline is 10s`
로 거절한다(실측). 여기(전역)에는 여전히 안 건다 - 모델마다 적정값이 달라(31b 요약은 100초+) 호출부가 건다
(`gemma4_summary_core._SECOND_FLASH_TIMEOUT_MS`).

재확인:
    from google.genai import types as t
    import google.genai._api_client as a
    print(a._RETRY_ATTEMPTS, a._RETRY_INITIAL_DELAY, a._RETRY_MAX_DELAY)
"""
from google import genai as _genai

SDK_RETRY_ATTEMPTS = 2
SDK_RETRY_MAX_DELAY = 4.0


def make_client(api_key, **kw):
    """재시도 상한을 건 genai 클라이언트.

    옵션 구성이 실패하면 **기본 클라이언트로 폴백**한다 - SDK 판이 바뀌어 필드명이 달라져도
    AI 경로가 통째로 죽지 않게. (조용한 폴백이지만 여기서 죽는 쪽이 훨씬 나쁘다.)
    """
    try:
        from google.genai import types as _t
        return _genai.Client(
            api_key=api_key,
            http_options=_t.HttpOptions(retry_options=_t.HttpRetryOptions(
                attempts=SDK_RETRY_ATTEMPTS, max_delay=SDK_RETRY_MAX_DELAY)),
            **kw)
    except Exception:
        return _genai.Client(api_key=api_key, **kw)
