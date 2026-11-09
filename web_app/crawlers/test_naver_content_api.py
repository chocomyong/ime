# -*- coding: utf-8 -*-
"""네이버 기사 API 엔드포인트 버전 폴백 하네스 - `content_extractor._naver_api_get`.

**왜 이 파일이 있나**: 2026-09-22 11:40 KST 경 `cafe-articleapi/v3` 가 전건 500
(`{"errorCode":"9999"}`)으로 돌아섰고, 당시 코드는 v3 하나만 알아서 그날 수집된 네이버 글이
전건 `[FAILED]` 로 박제됐다(본문이 없으면 AI 요약에서도 빠진다). 같은 사고가 다시 왔을 때
**폴백이 실제로 동작하는가**를 네트워크 없이도 판정할 수 있어야 한다.

실행:
  python3 crawlers/test_naver_content_api.py          # 오프라인 (네트워크 불요, 항상 실행 가능)
  python3 crawlers/test_naver_content_api.py --live    # + 라이브 (실제 네이버 API 를 친다)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crawlers import content_extractor as ce


class _FakeResp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _install(route):
    """requests.get 을 route(ver) -> _FakeResp 로 치환. 원복 함수를 돌려준다."""
    orig = ce.requests.get
    calls = []

    def fake_get(url, **kw):
        ver = url.split("/cafe-articleapi/", 1)[1].split("/", 1)[0]
        calls.append(ver)
        return route(ver)

    ce.requests.get = fake_get
    return calls, (lambda: setattr(ce.requests, "get", orig))


def _reset(versions=("v2.1", "v3")):
    ce._NAVER_API_VERSIONS = versions
    ce._naver_api_ver_idx = 0
    ce._naver_api_all_down = False


_FAIL = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        _FAIL.append(name)


def t_first_version_wins():
    """1순위가 살아 있으면 2순위를 치지 않는다 (요청 2배 방지)."""
    _reset()
    calls, undo = _install(lambda ver: _FakeResp(200, {"ok": ver}))
    try:
        r = ce._naver_api_get("1", "2", {})
        check("1순위 성공 시 1회만 호출", calls == ["v2.1"], f"calls={calls}")
        check("1순위 응답 반환", r.status_code == 200 and r.json() == {"ok": "v2.1"})
        check("포인터 고정 유지", ce._naver_api_ver_idx == 0)
    finally:
        undo()


def t_rotates_on_5xx_and_pins():
    """5xx 면 다음 버전으로 넘어가고, **그 다음 글부터는 새 버전만** 친다."""
    _reset(("v3", "v2.1"))            # 사고 당시 순서 재현: v3 가 1순위
    route = lambda ver: _FakeResp(500, text='{"errorCode":"9999"}') if ver == "v3" else _FakeResp(200, {"ok": ver})
    calls, undo = _install(route)
    try:
        r = ce._naver_api_get("1", "2", {})
        check("5xx -> 다음 버전으로 전환", r.status_code == 200 and r.json() == {"ok": "v2.1"}, f"calls={calls}")
        check("전환 후 포인터 이동", ce._naver_api_ver_idx == 1)
        calls.clear()
        ce._naver_api_get("1", "3", {})
        check("두 번째 글은 죽은 버전을 다시 안 친다", calls == ["v2.1"], f"calls={calls}")
    finally:
        undo()


def t_no_rotate_on_4xx():
    """404/401 은 그 글에 대한 확정 답 - 버전을 바꿀 이유가 없다.
    (여기서 회전하면 삭제글 1건마다 전 버전을 훑어 요청이 배로 뛴다)"""
    for status in (404, 401):
        _reset()
        calls, undo = _install(lambda ver: _FakeResp(status))
        try:
            r = ce._naver_api_get("1", "2", {})
            check(f"{status} 는 회전 없이 즉시 반환", calls == ["v2.1"] and r.status_code == status, f"calls={calls}")
        finally:
            undo()


def t_all_down():
    """전 버전 5xx = 마지막 응답 반환(호출자의 raise_for_status 가 일과성 실패로 처리)."""
    _reset()
    calls, undo = _install(lambda ver: _FakeResp(503))
    try:
        r = ce._naver_api_get("1", "2", {})
        check("전 버전 시도", sorted(calls) == ["v2.1", "v3"], f"calls={calls}")
        check("마지막 응답 반환", r is not None and r.status_code == 503)
        check("전면장애 플래그 셋", ce._naver_api_all_down is True)
        calls.clear()
        ce._naver_api_get("1", "3", {})
        check("전면장애 중에도 매번 전 버전 재시도(복구 감지 경로)", sorted(calls) == ["v2.1", "v3"], f"calls={calls}")
    finally:
        undo()


def t_exception_propagates():
    """timeout/연결오류는 버전 문제가 아니다 - 삼키지 말고 올려 일과성 실패 경로에 태운다."""
    _reset()

    def boom(url, **kw):
        raise ce.requests.ConnectionError("boom")

    orig = ce.requests.get
    ce.requests.get = boom
    try:
        raised = False
        try:
            ce._naver_api_get("1", "2", {})
        except ce.requests.ConnectionError:
            raised = True
        check("연결오류는 그대로 전파", raised)
    finally:
        ce.requests.get = orig


def t_upstream_down_sentinel():
    """전 버전 5xx 면 `_extract_naver_content` 는 빈 문자열이 아니라 **센티널**을 돌려준다.

    이 구분이 없으면 상류 전면 장애가 `[FAILED:N]` 3회를 태우고 종결 마커 `[FAILED]` 로 굳어
    API 가 돌아와도 자동 복구되지 않는다 - 2026-09-22 에 385건이 그렇게 영구 누락됐다.
    """
    link = "https://cafe.naver.com/f-e/cafes/28866679/articles/1"
    _reset()
    calls, undo = _install(lambda ver: _FakeResp(500, text='{"errorCode":"9999"}'))
    try:
        got = ce._extract_naver_content(link)
        check("전 버전 5xx -> 센티널(빈 문자열 아님)", got == ce._UPSTREAM_DOWN, f"got={got!r}")
        check("빈 문자열과 구분된다", got != "")
    finally:
        undo()


def t_decisive_status_still_terminal():
    """404/401 은 그대로 종결 마커 - 상류 장애 분기가 기존 규약을 먹지 않는다(대조군)."""
    link = "https://cafe.naver.com/f-e/cafes/28866679/articles/1"
    for status, expect in ((404, "[DELETED_POST]"), (401, "[MEMBER_ONLY]")):
        _reset()
        calls, undo = _install(lambda ver: _FakeResp(status, {}))
        try:
            got = ce._extract_naver_content(link)
            check(f"{status} -> {expect}", got == expect, f"got={got!r}")
        finally:
            undo()


def t_200_still_extracts():
    """정상 200 은 기존대로 본문 텍스트 (대조군)."""
    link = "https://cafe.naver.com/f-e/cafes/28866679/articles/1"
    _reset()
    payload = {"result": {"article": {"contentHtml": "<p>\ud55c \uc904</p><p>\ub450 \uc904</p>"}}}
    calls, undo = _install(lambda ver: _FakeResp(200, payload))
    try:
        got = ce._extract_naver_content(link)
        check("200 -> 본문 텍스트", got == "\ud55c \uc904\n\ub450 \uc904", f"got={got!r}")
    finally:
        undo()


def t_cookie_expired_only_on_proof():
    """**쿠키 만료 판정은 증거가 있을 때만.**

    401(쿠키 있음) 뒤 비회원 재시도가 200 이어야 "범인은 쿠키" 가 증명된다. 또 401 이면
    그냥 회원공개 글이고 쿠키에 대해서는 아무것도 알 수 없다. v6.7.685 이전에는 재시도
    결과를 보기도 전에 'expired' 를 써서, 쿠키가 멀쩡해도 회원공개 글마다 상태가 뒤집히고
    "재발급 필요" 경고가 나갔다(2026-09-22 라이브 19건).
    """
    link = "https://cafe.naver.com/f-e/cafes/28866679/articles/1"
    orig_state, orig_cookie = ce._set_naver_auth_state, ce._get_naver_cookie
    seen = []
    ce._set_naver_auth_state = lambda st: seen.append(st)
    ce._get_naver_cookie = lambda: "NID_AUT=x; NID_SES=y"
    try:
        # (a) 진짜 회원공개 글: 쿠키 유무와 무관하게 401 -> 상태를 건드리지 않는다
        _reset()
        seen.clear()
        calls, undo = _install(lambda ver: _FakeResp(401, {}))
        try:
            got = ce._extract_naver_content(link)
        finally:
            undo()
        check("회원공개 글 -> [MEMBER_ONLY]", got == "[MEMBER_ONLY]", f"got={got!r}")
        check("회원공개 글에 'expired' 를 쓰지 않는다", "expired" not in seen, f"seen={seen}")

        # (b) 진짜 만료 쿠키: 쿠키 실으면 401, 빼면 200 -> 그때만 'expired'
        _reset()
        seen.clear()
        state = {"n": 0}

        def route(ver):
            state["n"] += 1
            if state["n"] == 1:
                return _FakeResp(401, {})          # 쿠키를 실은 첫 요청
            return _FakeResp(200, {"result": {"article": {"contentHtml": "<p>x</p>"}}})

        calls, undo = _install(route)
        try:
            got = ce._extract_naver_content(link)
        finally:
            undo()
        check("만료 쿠키 -> 본문은 살린다", got == "x", f"got={got!r}")
        check("만료가 증명될 때만 'expired'", seen == ["expired"], f"seen={seen}")
    finally:
        ce._set_naver_auth_state, ce._get_naver_cookie = orig_state, orig_cookie


def t_live():
    """라이브: 실제 글 본문이 나오는가 + **폴백을 떼면 다시 실패하는가**(반증)."""
    link = os.environ.get("NAVER_TEST_LINK",
                          "https://cafe.naver.com/f-e/cafes/28866679/articles/6282831")
    _reset()
    got = ce._extract_naver_content(link)
    check("[live] 기본 설정으로 본문 추출", bool(got) and not got.startswith("["),
          f"len={len(got)} head={got[:40]!r}")

    _reset(("v3",))               # 사고 당시 코드로 되돌린다
    broken = ce._extract_naver_content(link)
    check("[live] v3 단독으로 되돌리면 실패가 재현된다(반증)", broken == "",
          f"got={broken[:40]!r}")

    _reset()
    dead = ce._extract_naver_content("https://cafe.naver.com/f-e/cafes/28866679/articles/99999999")
    check("[live] 없는 글 -> [DELETED_POST]", dead == "[DELETED_POST]", f"got={dead!r}")


if __name__ == "__main__":
    print("== 오프라인 ==")
    t_first_version_wins()
    t_rotates_on_5xx_and_pins()
    t_no_rotate_on_4xx()
    t_all_down()
    t_exception_propagates()
    t_upstream_down_sentinel()
    t_decisive_status_still_terminal()
    t_200_still_extracts()
    t_cookie_expired_only_on_proof()
    if "--live" in sys.argv:
        print("== 라이브 ==")
        t_live()
    print(f"\n실패 {len(_FAIL)}건" + (": " + ", ".join(_FAIL) if _FAIL else ""))
    sys.exit(1 if _FAIL else 0)
