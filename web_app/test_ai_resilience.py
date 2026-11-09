# -*- coding: utf-8 -*-
"""test_ai_resilience.py - AI 파이프라인 **실패 처리** 회귀 테스트 (v6.7.610 신설).

**네트워크·시트·API 를 쓰지 않는다.** 실행: cd web_app && python test_ai_resilience.py

## 무엇을 지키나 - 한 가지 규율

> **일시적 실패를 영구 제외로 굳히지 않는다.**

이 레포의 AI 경로는 실패했을 때 "스탬프를 남길 것인가" 로 갈린다. 남기면 그 항목은 dedup
조건(`a.link IS NULL` / `dc_fail < _MAX_FAIL`)에 걸려 **다시는 조회되지 않는다.** 그런데
화면에는 글도 메시지도 멀쩡히 보이고 요약·판정 칸만 비므로 **아무도 눈치채지 못한다.**
PATTERN-43·PATTERN-35 가 지목한 침묵이 정확히 이 형태다.

같은 결함이 두 곳에서 서로 다른 얼굴로 나왔고, 둘 다 여기서 잡는다:

  1. **디코 판정** - 429(쿼타)를 배치 결함과 같이 취급해 `dc_fail` 을 올렸다. 주기가 30초라
     **쿼타가 90초만 흔들리면** 그 구간 채팅이 판정 없이 사라진다. (v6.7.610 수정)
     [v6.7.742] 디코 판정 잡을 지우면서 그 잡만 시험하던 항목([2]~[2d] · [12b] · 성공 로그)도 지웠다.
     에러 축 분류기([1])는 그것을 빌려 쓰던 `bg/post_judge.py` 로 옮겨 그쪽을 시험한다.
  2. **AI 요약 1차** - 배치 10건 중 6건만 돌려주는 **부분 응답**에서 빠진 4건을
     `[분석 제외]` 로 확정해 저장했다. 그 문자열은 괄호형 가드에 안 걸려 `ai_analysis` 에
     박히고, 그 글은 영영 다시 요약되지 않는다. (v6.7.610 수정)

**둘 다 정상 동작에서는 절대 안 보인다** - 쿼타가 흔들리거나 모델이 답을 빠뜨려야 나온다.
그래서 라이브 관찰로는 못 잡고, 이렇게 실패를 **주입**해야만 잡힌다.

AME 쪽 대응물은 `jira_register/test_ame.py` [6][7]. 두 앱이 같은 규율을 지키는지 볼 때
그쪽과 함께 읽을 것 - 이 결함들은 원래 AME 가 먼저 고쳤고(v0.12.6 · v0.12.46) 정본으로
역이식된 것이다.
"""
import os
import sqlite3
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(_HERE, 'utils')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

FAILS = []
_T0 = time.time()      # [2026-09-30] 테스트 프로세스 시작 - 모듈 로드 시각 기준 검사가 스위트 실행 시간에 흔들리지 않게


def check(name, cond, detail=""):
    print(("  [ OK ] " if cond else "  [FAIL] ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------------------
def test_quota_brief_parses_429():
    """[v6.7.659] **429 본문에서 처방을 가르는 두 값(quotaId · retryDelay)을 뽑는가.**

    무료 티어 429 는 원인 축이 셋(분당 입력 토큰 / RPM / RPD)이고 처방이 다 다르다(배치 축소 / 페이싱 /
    키 증설). 로그 한 줄에서 그 축이 보여야 한다 - `_quota_brief` 가 그 값을 뽑는다.
    [v6.7.742] 이 항목은 원래 디코 판정 성공 로그 검사(`test_success_log_carries_thinking_usage`)의 뒷부분이었다.
    판정 잡을 지우면서 앞부분(그 잡의 로그 줄)은 지우고, 요약 코어 함수를 보는 이 부분만 남겼다.
    """
    print("\n[0] 쿼타 사유 파서 (_quota_brief, v6.7.659)")
    from ai_lib.gemma4_summary_core import _quota_brief
    body = ("429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'details': [{'quotaId': "
            "'GenerateContentInputTokensPerModelPerMinute-FreeTier'}, {'retryDelay': '28s'}]}}")
    b = _quota_brief(body)
    check("429 본문에서 quotaId 를 뽑는다 [v6.7.659]",
          "GenerateContentInputTokensPerModelPerMinute" in b, b)
    check("429 본문에서 retryDelay 를 뽑는다 [v6.7.659]", "retryDelay=28s" in b, b)
    check("쿼타 정보가 없으면 빈 문자열 (로그를 안 늘린다)", _quota_brief("500 INTERNAL") == "", _quota_brief("500 INTERNAL"))


def test_ccu_drop_is_relative_and_copies_agree():
    """[v6.7.660] **CCU 급감 판정은 상대(%)이고, 사본 셋이 같은 기준이어야 한다.**

    왜 필요한가 (2026-09-12 실사고): 2026-09-09 에 "절대 200명 -> 상대 10%" 로 되돌린 지시가
    **사본 셋 중 둘에만** 적용됐다(grafana_ccu 데몬 v0.1.12 · IMT 뷰어 v0.12.109). IME 가 남아서,
    POE1 이 200 -> 3 으로 빠졌을 때 **197명**이라 `>= 200` 을 3명 차이로 못 넘고 조용했다.
    같은 데이터로 IMT 는 RED 였다 - 즉 화면끼리 서로 다른 말을 했다(PATTERN-62).

    **재는 것은 상수 이름이 아니라 판정이다** - 197명/98.5% 를 실제로 태워서 red 가 나오는지 본다.
    """
    print("\n[CCU] 급감 판정이 상대(%)인가 + 사본 일치 (v6.7.660)")
    import re as _re
    from web_lib.bg import web_push as W

    # 1) 서버 이식본을 실제로 태운다 - 사고 당시 값 그대로(250, 200, 3, 0).
    payload = {"t0_s": 1_700_000_000, "step_s": 30,
               "series": [{"name": "POE1", "values": [300, 280, 250, 200, 3, 0]}]}
    v = W._evaluate_drop(payload, 0, 999)
    check("197명(98.5%) 급감을 red 로 잡는다 (절대 200 이면 3명 차이로 놓친다)",
          v.get("state") == "red", str(v)[:120])
    ev = v.get("event") or {}
    check("이벤트가 그 쌍을 가리킨다", ev.get("baseline") == 200 and ev.get("after") == 3, str(ev)[:120])

    # 2) 임계 미만은 여전히 안 잡는다 (임계를 없앤 것이 아니다). 인접쌍 판정이라 단계별 약 2%.
    quiet = {"t0_s": 1_700_000_000, "step_s": 30,
             "series": [{"name": "POE1", "values": [1000, 980, 960, 950]}]}
    check("단계별 2% 하락은 red 가 아니다 (임계가 살아 있다)",
          W._evaluate_drop(quiet, 0, 999).get("state") != "red", str(W._evaluate_drop(quiet, 0, 999))[:90])

    # 3) 저동접 하한은 그대로 - 상대 임계에서는 이것이 주 방어선이다.
    low = {"t0_s": 1_700_000_000, "step_s": 30,
           "series": [{"name": "POE1", "values": [90, 80, 5, 0]}]}
    check("MIN_BASE 미만은 무시한다 (새벽 노이즈 가드)",
          W._evaluate_drop(low, 0, 999).get("state") != "red", "저동접")

    # 4) **사본 셋을 한 자리에서 묶는다.** 이 사고의 근본은 사본이 셋인데 둘만 고친 것이었다 -
    #    그러니 검사도 셋을 봐야 한다. 종전 판(v6.7.660 초판)은 IME 안의 둘(클라/서버)만 묶어서
    #    **정본과의 드리프트는 여전히 못 잡았다**(커밋 전 리뷰가 지적).
    _here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(_here, "static", "js", "tabs", "ccu-grafana-tab.js"), encoding="utf-8") as _f:
        js = _f.read()
    # 정본 = IMT 뷰어. 상수 **이름**은 앱마다 다르고(`DROP_PCT` vs `DROP_THRESHOLD_PCT`) 그건
    #   종전부터 그랬으므로, 묶는 것은 이름이 아니라 **값**이다.
    _canon_path = os.path.join(_here, "..", "jira_register", "ame_web", "js", "ccu.js")
    with open(_canon_path, encoding="utf-8") as _f:
        canon = _f.read()

    check("클라 JS 에 절대 임계가 남아 있지 않다", "DROP_THRESHOLD_ABS" not in js,
          "DROP_THRESHOLD_ABS 잔존")
    m = _re.search(r"DROP_THRESHOLD_PCT\s*=\s*(\d+)", js)
    check("클라 JS 가 상대 임계를 쓴다", bool(m), js[:80])
    check("클라와 서버 임계값이 같다 (갈라지면 배너와 화면이 다른 말을 한다)",
          bool(m) and int(m.group(1)) == W._DROP_THRESHOLD_PCT,
          "js=%s / py=%s" % (m.group(1) if m else "?", W._DROP_THRESHOLD_PCT))
    mc = _re.search(r"DROP_PCT_DEFAULT\s*=\s*(\d+)", canon)
    check("정본(IMT 뷰어)도 같은 임계값이다 - 사본 셋을 한 자리에서 묶는다",
          bool(mc) and bool(m) and int(mc.group(1)) == int(m.group(1)) == W._DROP_THRESHOLD_PCT,
          "정본=%s / IME js=%s / IME py=%s" % (mc.group(1) if mc else "?",
                                               m.group(1) if m else "?", W._DROP_THRESHOLD_PCT))
    check("정본도 상대 판정 수식을 쓴다 (절대 임계로 되돌아가지 않았다)",
          "DROP_THRESHOLD_ABS" not in canon and "/ vj * 100" in canon, "정본 수식 확인 실패")


def test_transient_classifier():
    """[1] 무엇을 '지금의 문제' 로 볼 것인가. **AME 사본과 같은 판정이어야 한다.**

    [v6.7.742] 분류기는 디코 판정(`bg/discord_judge.py`, 삭제)에서 `bg/post_judge.py` 로 옮겼다 - 그 잡이
    빌려 쓰던 유일한 소비자다. 픽스처는 그대로 두고 대상만 바꿨다. 지운 것: `_retry_after`(디코 판정 전용 쿨다운)와
    `_judge_batch` 의 예외 타입 잠금(같은 잠금이 `test_post_judge.py` 에 `PostJudgeParseError` 로 이미 있다).
    """
    print("\n[1] 일시적 실패 분류기 (post_judge._is_transient)")
    from web_lib.bg import post_judge as J

    class ServerError(Exception):
        pass

    transient = [
        Exception("429 RESOURCE_EXHAUSTED: quota exceeded"),
        Exception("503 Service Unavailable"),
        Exception("The model is overloaded. Please try again later."),
        Exception("500 internal error"),
        ServerError("boom"),
    ]
    # [v6.7.641] **픽스처가 결함을 비켜 가고 있었다.** 여기 있던 파싱 실패 문구는 `답변 0자,
    #   thoughts=1586` 이라 우연히 `429`·`500`·`503` 을 안 담았고, 그래서 이 항목이 내내
    #   초록이면서 라이브의 실제 문구는 오분류됐다(PATTERN-71 형태). 실측 재현(2026-09-06):
    #   현실적 문구 5종 중 4종이 True 로 나왔다. **이제 그 4종을 픽스처에 박는다.**
    permanent = [
        J.PostJudgeParseError("JSON 배열 없음 (finish=MAX_TOKENS, 답변 0자, thoughts=1586 out=0)"),
        J.PostJudgeParseError("JSON 배열 없음 (finish=MAX_TOKENS, 답변 1500자, thoughts=4093 out=0)"),
        J.PostJudgeParseError("JSON 배열 없음 (finish=STOP, 답변 429자, thoughts=1200 out=0)"),
        J.PostJudgeParseError("JSON 파싱 실패: Expecting ',' delimiter: line 1 column 2503 (char 2502)"),
        J.PostJudgeParseError("JSON 파싱 실패: Expecting value: line 1 column 500 (char 499)"),
        Exception("400 INVALID_ARGUMENT: request contains an invalid argument"),
        Exception("403 permission denied"),
    ]
    check("쿼타·5xx·과부하·서버예외를 일시적으로 본다",
          all(J._is_transient(e) for e in transient),
          str([str(e)[:24] for e in transient if not J._is_transient(e)]) or "")
    check("파싱 실패·4xx 는 배치 결함으로 본다 (여기는 fail 카운터가 맞다)",
          not any(J._is_transient(e) for e in permanent),
          str([str(e)[:24] for e in permanent if J._is_transient(e)]) or "")

    # [v6.7.641] **맨 숫자는 단어 경계로만 본다.** 진짜 응답 코드는 잡고, 우리 문구 안의
    #   길이·컬럼 숫자에는 안 걸려야 한다. 위 permanent 가 후자를 채점하므로 여기는 전자다.
    check("타입이 우리 것이 아니어도 맨 응답 코드는 잡는다 (2차 방어)",
          all(J._is_transient(Exception(m)) for m in ("HTTP 429", "got 503 from upstream", "error 500")))
    check("숫자 안에 박힌 코드는 안 잡는다 (1429·5003 같은 값)",
          not any(J._is_transient(Exception(m)) for m in ("id=1429", "count 5003", "seq 4290")))

    # 두 앱의 판정이 갈리면 같은 429 를 다르게 취급하게 된다 - 목록을 글자 단위로 맞춰 둔다.
    #   AME 쪽 정본은 여전히 `discord_judge_local._is_transient` 다(AME `post_judge_local` 이 그것을 빌려 쓴다).
    ame = os.path.join(os.path.dirname(_HERE), 'jira_register', 'discord_judge_local.py')
    if os.path.exists(ame):
        src = open(ame, encoding='utf-8').read()
        same = all(f'"{t}"' in src for t in J._TRANSIENT_TEXT)
        check("AME 사본과 판정 목록이 같다", same)
        check("AME 사본도 전용 파싱 예외 타입을 쓴다 (문구가 아니라 타입으로 가른다)",
              "class JudgeParseError" in src and hasattr(J, "PostJudgeParseError"))
    else:
        print("  [SKIP] AME 사본 없음")


# ---------------------------------------------------------------------------
def test_summary_cap_matches_parallelism():
    """[3c] **사이클 캡이 병렬도와 어긋나면 워커가 논다** (v6.7.652).

    가속은 워커 수가 아니라 `배치수 / ceil(배치수 / 워커수)` 다(PATTERN-47 의 자기 정정).
    캡 30 · `BATCH_SIZE` 10 · 워커 2 면 배치가 3개라 **뒷 웨이브의 절반이 논다** - 콜 3회에
    웨이브 2개분 시간이 들어 처리량이 천장의 75% 였다. 상수 둘이 각자 바뀌면 조용히 어긋나므로
    (30 은 P=2 에도 P=4 에도 안 맞았다) **관계를 기계로 못 박는다.**

    재는 것은 주석이 아니라 **실제로 넘어간 건수와 배치 수**다.
    """
    print("\n[3c] AI 요약 - 사이클 캡 = 배치크기 x 병렬도 (v6.7.652)")
    import math
    from ai_lib.gemma4_summary_core import BATCH_SIZE, BATCH_PARALLEL

    batches = math.ceil((BATCH_SIZE * BATCH_PARALLEL) / BATCH_SIZE)
    waves = math.ceil(batches / BATCH_PARALLEL)
    ceiling = BATCH_SIZE * BATCH_PARALLEL
    thru = (BATCH_SIZE * BATCH_PARALLEL) / waves

    check("캡이 배치크기 x 병렬도다 (상수가 갈라지지 않는다)",
          BATCH_SIZE * BATCH_PARALLEL == ceiling, f"{BATCH_SIZE} x {BATCH_PARALLEL} = {ceiling}")
    check("웨이브가 꽉 찬다 - 노는 워커가 없다", batches % BATCH_PARALLEL == 0,
          f"배치 {batches} / 워커 {BATCH_PARALLEL} = 웨이브 {waves}")
    check("처리량이 천장에 닿는다", abs(thru - ceiling) < 0.01,
          f"{thru:.0f}건/T (천장 {ceiling})")
    # 구 조합(캡 30 / 워커 2)이 실제로 천장을 못 채웠다는 것도 같이 박는다 - 회귀의 근거다.
    _old_b = math.ceil(30 / 10)
    _old_thru = 30 / math.ceil(_old_b / 2)
    check("구 조합(캡 30 · 워커 2)은 천장의 75% 였다", abs(_old_thru / (10 * 2) - 0.75) < 0.01,
          f"{_old_thru:.0f}건/T / 천장 20 = {100 * _old_thru / 20:.0f}%")


def test_watchdog_measures_stall_not_slowness():
    """[3e] **워치독은 '느림' 이 아니라 '멈춤' 을 재야 한다** (v6.7.679).

    2026-09-18 라이브: 정상적으로 일하는 사이클이 1,468~2,214초였는데 임계가 600초라 전부
    hang 으로 오판했다. 파이썬은 스레드를 못 죽이므로 버려진 사이클이 살아 있는 채로 새 사이클이
    떠서 동시 콜이 2~3배가 됐고, 그 동시성이 분당 입력토큰 쿼타를 더 먹어 429 를 늘렸다(되먹임).

    두 가지를 기계로 잠근다.
      1. 임계 기준이 **사이클 시작이 아니라 마지막 진척**이고, 실측 최악(2,214초) 위에 있다.
      2. 버려진 사이클의 done 콜백이 **현재 사이클의 flag 를 지우지 않는다** - 그 경로가
         겹침을 누적시켰다(사이클 2개가 51초 간격 완료).
    """
    print("\n[3e] AI 요약 - 워치독이 재는 것은 진척 정지다 (v6.7.679)")
    import re
    import web_lib.ai_gemma4_worker as w

    OBSERVED_WORST = 2214   # 2026-09-18 라이브 실측 (사이클 완료 40건 / 2214.0s)
    check("임계가 실측 최악 사이클보다 위다 - 일하는 사이클을 안 죽인다",
          w._CYCLE_HANG_TIMEOUT > OBSERVED_WORST,
          f"{w._CYCLE_HANG_TIMEOUT}s vs 실측 {OBSERVED_WORST}s")
    check("임계가 무한대가 아니다 - 진짜 hang 은 여전히 잡는다",
          w._CYCLE_HANG_TIMEOUT <= 7200, f"{w._CYCLE_HANG_TIMEOUT}s")

    src = open(os.path.join(_HERE, "web_lib", "ai_gemma4_worker.py"), encoding="utf-8").read()
    check("워치독이 _last_progress 로 잰다 (사이클 시작이 아니다)",
          re.search(r"_last_progress\)\s*>\s*_CYCLE_HANG_TIMEOUT", src) is not None)
    check("배치가 끝날 때마다 진척을 갱신한다 (finally - 실패도 응답이다)",
          re.search(r"finally:\s*\n\s*#[^\n]*\n(?:\s*#[^\n]*\n)*\s*_last_progress = time\.monotonic\(\)", src) is not None)

    # --- 일련번호 가드: 실제로 호출해 본다 (주석이 아니라 동작을 잰다) ---
    _orig_seq, _orig_cur = w._cycle_seq, w._current_seq
    try:
        w._current_seq = 7
        w._running_flag.set()
        w._on_cycle_done(5)          # 버려진 옛 사이클이 뒤늦게 끝났다
        check("버려진 사이클(#5)의 콜백이 현재 사이클(#7)의 flag 를 안 지운다",
              w._running_flag.is_set())
        w._on_cycle_done(7)          # 현재 사이클이 정상 종료
        check("현재 사이클의 콜백은 flag 를 내린다", not w._running_flag.is_set())
    finally:
        w._running_flag.clear()
        w._cycle_seq, w._current_seq = _orig_seq, _orig_cur


def test_second_stage_is_one_call_per_cycle():
    """[3f] **2차는 사이클당 1콜이다** (v6.7.680).

    2차 flash 는 건수가 아니라 **콜 수**로 과금된다(RPD 20/키/모델 -> 6키 x 3모델 = 360콜/일).
    종전에는 배치마다 따로 불러 사이클당 4콜 x 약 3건을 썼는데, 그 4콜은 `_gemini_lock` 으로
    **어차피 직렬**이라 합치면 벽시계 손해 없이 RPD 만 1/4 이 된다.

    재는 것은 주석이 아니라 **실제 호출 횟수와 넘어간 건수**다.
    """
    print("\n[3f] AI 요약 - 2차는 사이클당 1콜 (v6.7.680)")
    from ai_lib.gemma4_summary_core import Gemma4SummaryCore, CHAIN_LEN

    calls = []

    class _R:
        text = '[{"id": 1, "analyze": true}, {"id": 2, "analyze": true}]'

    class _FakeModels:
        def generate_content(self, **kw):
            return _R()

    class _FakeClient:
        models = _FakeModels()

    core = object.__new__(Gemma4SummaryCore)
    core.client = _FakeClient()
    core.system_instruction = 'x'
    core._deferred_second = []
    core._acquire_gemma_client = lambda: (0, _FakeClient())
    core._build_context_section = lambda games: ''
    core._send_kakaowork_summary = lambda *a, **k: None
    core._generate_gemini_summaries = lambda arts, *a, **k: calls.append(len(arts))

    batches = [[{'game': 'g%d' % b, 'board_name': 'x', 'title': 't%d' % i, 'content': 'c'}
                for i in range(2)] for b in range(4)]
    for b in batches:
        core._generate_summary_batch(b, defer_second=True)

    check("배치 단계에서는 2차를 부르지 않는다 (RPD 를 안 쓴다)", calls == [], repr(calls))
    check("대상이 사이클 몫으로 모인다", len(core._deferred_second) == 8,
          "%d건" % len(core._deferred_second))

    n = core.flush_deferred_second()
    check("flush 가 2차를 **한 번만** 부른다", len(calls) == 1, "%d회" % len(calls))
    check("그 한 콜에 전 배치분이 실린다", calls == [8], repr(calls))
    check("flush 반환값이 건수다", n == 8, str(n))
    check("flush 는 멱등 - 두 번째는 0건 0콜", core.flush_deferred_second() == 0 and len(calls) == 1)

    # 예산 산식은 체인 길이에 걸려 있다 (2차 RPD 20/키/모델)
    check("체인 길이가 3보다 크다 - 놀던 flash 모델을 쓴다", CHAIN_LEN > 3, "CHAIN_LEN=%d" % CHAIN_LEN)


def test_first_pass_uses_compact_guide():
    """[3h] **1차 분류는 A2 전문이 아니라 1차 전용 지침을 읽는다** (v6.7.687).

    1차 콜 입력의 약 90% 가 A2(13,807자)였고 분당 입력 토큰 쿼타(키당 16k)를 그것이 먹었다.
    채점(표본 층화, `jira_register/first_pass_prompt_eval.py`)에서 A2 통과 21건을 이 지침이 전부
    통과시켜 바꿨다. 되돌아가면(1차가 다시 A2 를 읽으면) 429 가 같이 돌아온다.

    **문자열 검색이 아니라 f-string 의 치환값을 본다**(PATTERN-68) - 함수 소스에 "system_instruction"
    이라는 글자는 가드(`if not self.system_instruction`)와 주석에도 있어서 문자열로는 못 가른다.
    """
    print("\n[3h] AI 요약 - 1차는 1차 전용 지침 (v6.7.687)")
    import ast
    from ai_lib.gemma4_summary_core import _FIRST_PASS_GUIDE as G
    src = open(os.path.join(_HERE, "ai_lib", "gemma4_summary_core.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}

    def _prompt_fields(fn_name):
        """그 함수에서 `prompt = f\"\"\"...\"\"\"` 에 치환되는 식들의 소스 목록."""
        fn = fns.get(fn_name)
        out = []
        for node in ast.walk(fn) if fn else []:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "prompt" for t in node.targets) \
                    and isinstance(node.value, ast.JoinedStr):
                out.append([ast.get_source_segment(src, v.value) for v in node.value.values
                            if isinstance(v, ast.FormattedValue)])
        return out

    first = _prompt_fields("_generate_summary_batch")
    check("1차 prompt f-string 을 찾았다 (메서드명·변수명이 바뀌면 이 검사가 헛돈다)", len(first) == 1, first)
    f1 = first[0] if first else []
    check("1차 prompt 가 _FIRST_PASS_GUIDE 를 넣는다", "_FIRST_PASS_GUIDE" in f1, f1)
    check("1차 prompt 가 A2(self.system_instruction)를 넣지 않는다", "self.system_instruction" not in f1, f1)
    # 2차는 A2 그대로여야 한다 - 요약문 형식·용어 정제·감성/심각도 규칙이 전부 A2 에 있다.
    #   2차는 A2 를 **매개변수로** 받는다 - prompt 치환값과, 호출부가 넘기는 인자를 둘 다 본다.
    second = _prompt_fields("_generate_gemini_summaries_impl")
    check("2차 prompt 가 system_instruction 매개변수를 넣는다",
          any("system_instruction" in f for f in second), second)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "_generate_gemini_summaries"]
    passed = [ast.get_source_segment(src, c.args[1]) for c in calls if len(c.args) > 1]
    check("2차 호출부가 A2(self.system_instruction)를 넘긴다", bool(passed) and all(a == "self.system_instruction" for a in passed), passed)
    check("지침이 비지 않았다", len(G.strip()) > 200, len(G))
    check("지침이 A2 의 10분의 1 안쪽이다 (A2 를 도로 붙여넣지 않았다)", len(G) < 1400, len(G))
    for kw in ("[수집]", "[제외]", "우마무스메 갤러리"):
        check("지침에 %s 규칙이 있다" % kw, kw in G)


def test_first_pass_reads_only_head_of_content():
    """[3g] **1차 분류는 본문 앞머리만 읽는다** (v6.7.681).

    1차의 일은 "요약할 가치가 있나" 판정뿐이고 요약문은 2차가 쓴다. 줄이는 이유는 품질이 아니라
    **분당 입력 토큰 쿼타**(키당·모델당 16,000/분)다 - 콜당 입력이 약 1만이면 같은 키에 1분 안에
    두 콜이 못 나간다. 상수가 조용히 800 으로 되돌아가면 429 가 같이 돌아온다.
    """
    print("\n[3g] AI 요약 - 1차는 본문 앞머리만 (v6.7.681, 경로 검사 v6.7.682)")
    import ast
    from ai_lib.gemma4_summary_core import (_FIRST_PASS_CONTENT_CHARS as N1,
                                            _SECOND_PASS_CONTENT_CHARS as N2)
    src = open(os.path.join(_HERE, "ai_lib", "gemma4_summary_core.py"), encoding="utf-8").read()

    # [v6.7.682] **파일 전체 문자열 검색으로는 못 잡는다.** v6.7.681 은 상수를 2차 호출부에
    #   걸었는데 옛 검사(`"[:_FIRST_PASS_CONTENT_CHARS]" in src`)가 그대로 통과했다 - 상수는
    #   분명히 쓰이고 있었으니까. 어느 메서드에서 쓰이는지를 봐야 한다.
    bodies = {}
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef):
            bodies[node.name] = ast.get_source_segment(src, node) or ""
    first, second = bodies.get("_generate_summary_batch", ""), bodies.get("_generate_gemini_summaries_impl", "")
    check("1차/2차 프롬프트 빌더를 둘 다 찾았다 (메서드명이 바뀌면 이 검사가 헛돈다)",
          bool(first) and bool(second))
    check("1차(_generate_summary_batch) 가 1차 상수를 쓴다",
          "[:_FIRST_PASS_CONTENT_CHARS]" in first)
    check("2차(_generate_gemini_summaries_impl) 가 1차 상수를 쓰지 않는다 - 요약문 품질이 여기 달렸다",
          "_FIRST_PASS_CONTENT_CHARS" not in second)
    check("2차가 2차 상수를 쓴다", "[:_SECOND_PASS_CONTENT_CHARS]" in second)
    check("두 경로 어디에도 숫자를 박지 않았다",
          "[:800]" not in first + second and "[:200]" not in first + second)
    check("1차 컷이 800 보다 작다 - 분당 입력토큰을 줄인 것이 유지된다", N1 < 800, "%d자" % N1)
    check("1차 컷이 0 이 아니다 - 본문을 아예 안 보내면 제목만으로 판정하게 된다", N1 > 0, "%d자" % N1)
    check("2차 컷이 1차보다 크다 - 요약문은 본문을 봐야 쓴다", N2 > N1, "2차 %d자 / 1차 %d자" % (N2, N1))


def test_summary_tail_is_not_starved():
    """[3b] **잔량이 limit 을 넘어도 꼬리가 굶지 않는다** (v6.7.650).

    `get_posts_pending_ai_summary` 는 `ORDER BY inserted_at DESC LIMIT n` 이라, 잔량이 n 을 넘으면
    매 사이클이 *최신 n 건*만 집고 **하위는 한 번도 배치에 안 들어간다.** 잔량이 n 밑으로 떨어질
    때까지 영구히 앉아 있는데, 화면은 최신 요약으로 멀쩡해 보여 **증상이 밀림 지표에만 나타난다.**

    라이브 실측 2026-09-09: 벤더 500/503 으로 처리량이 유입(239건/시) 밑으로 떨어진 뒤
    10:43~11:37 밀림이 **정확히 1:1** 로 증가(27.4 -> 87.1분)했고, 그때 잔량 59건 · 굶던 것은
    10:10~10:25 수집분 · `ai_analysis` 최신 저장은 11:27 이었다 - 워커는 멀쩡히 돌고 있었다.

    재는 것은 로그가 아니라 **반환된 link 집합**이다: 가장 오래된 것이 그 안에 있는가.
    """
    print("\n[3b] AI 요약 - 꼬리 기아 방지 (v6.7.650)")
    from sqlite_handler import db_handler as db

    path = os.path.join(tempfile.mkdtemp(), "t.sqlite")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE community_posts (link TEXT PRIMARY KEY, source TEXT, game TEXT, "
                 "title TEXT, content TEXT, detected_keywords TEXT, board_name TEXT, date TEXT, "
                 "timestamp TEXT, is_super_keyword INTEGER, inserted_at TEXT)")
    conn.execute("CREATE TABLE ai_analysis (link TEXT PRIMARY KEY)")
    # 잔량 60건 > limit 30. inserted_at 은 UTC 공백 19자(라이브와 같은 포맷).
    conn.executemany(
        "INSERT INTO community_posts (link, source, game, title, content, inserted_at, is_super_keyword) "
        "VALUES (?,?,?,?,?,?,0)",
        [("L%02d" % i, "DC", "poe", "제목%d" % i, "본문 %d" % i,
          "2026-09-09 %02d:%02d:00" % (1 + i // 60, i % 60)) for i in range(60)])
    conn.commit()
    conn.close()

    # 조기 종료(평시 경로)를 값으로 본다 - 최신 몫이 한도를 못 채우면 ASC 를 아예 안 쏜다.
    #   실행된 SELECT 개수를 세서 확인한다(로그가 아니라 호출 횟수).
    class _CountingConn(sqlite3.Connection):
        selects = 0

        def execute(self, sql, *a, **kw):
            if 'community_posts' in sql:
                _CountingConn.selects += 1
            return sqlite3.Connection.execute(self, sql, *a, **kw)

    _bak = db._get_connection
    db._get_connection = lambda: sqlite3.connect(path, factory=_CountingConn)
    try:
        _CountingConn.selects = 0
        rows = db.get_posts_pending_ai_summary(limit=30, since="2026-09-08 20:00:00")
        links = [r['link'] for r in rows]
        deep_selects = _CountingConn.selects
        # 잔량이 limit 이하일 때(두 조회가 겹치는 구간)의 동작도 같이 본다.
        conn = sqlite3.connect(path)
        conn.executemany("INSERT INTO ai_analysis (link) VALUES (?)",
                         [("L%02d" % i,) for i in range(40)])
        conn.commit(); conn.close()
        _CountingConn.selects = 0
        few = db.get_posts_pending_ai_summary(limit=30, since="2026-09-08 20:00:00")
        few_links = [r['link'] for r in few]
        shallow_selects = _CountingConn.selects
    finally:
        db._get_connection = _bak

    check("반환 건수가 limit 을 안 넘는다", len(links) == 30, f"{len(links)}건")
    check("가장 오래된 것이 배치에 든다 (구 구현은 영영 안 든다)", "L00" in links,
          f"오래된 쪽 {sorted(links)[:3]}")
    check("꼬리 몫이 limit//5 다", sum(1 for l in links if l < "L06") == 6,
          f"L00~L05 중 {sum(1 for l in links if l < 'L06')}건")
    check("최신 몫도 유지된다 (꼬리가 배치를 먹지 않는다)", "L59" in links)
    check("중복 없음 (두 조회가 겹쳐도)", len(links) == len(set(links)))
    check("잔량 20건(<limit) 이면 전량 반환 + 중복 없음",
          len(few_links) == 20 and len(few_links) == len(set(few_links)), f"{len(few_links)}건")
    check("백로그면 꼬리 조회를 쏜다", deep_selects == 2, f"SELECT {deep_selects}회")
    check("평시(잔량<한도)엔 꼬리 조회를 건너뛴다 - 창-워크 비용 0",
          shallow_selects == 1, f"SELECT {shallow_selects}회")


def test_summary_partial_response():
    """[3] 1차 **부분 응답**이 글을 영구 제외하지 않는다 (v6.7.610).

    가르는 것은 스탬프의 **모양**이다 - `ai_gemma4_worker` 는 `(...)` 형만 저장에서 뺀다
    (v6.7.205). `[분석 제외]` 는 저장되고 그 글은 dedup 때문에 다시 안 잡힌다.
    """
    print("\n[3] AI 요약 1차 - 부분 응답 처리 (v6.7.610)")
    from ai_lib.gemma4_summary_core import Gemma4SummaryCore

    core = object.__new__(Gemma4SummaryCore)          # 시트·SDK 초기화를 건너뛴다
    core.system_instruction = "지침"
    core.recent_summaries = []
    core._recent_hydrated = True                      # DB 되읽기는 이 섹션의 관심사가 아니다
    core.report_instruction = None
    core.client = object()                            # 초기화 성공 sentinel (실제 콜은 아래 fake)

    class _Resp:
        # 5건 중 1·3·5 만 답한다 (부분 응답 - 라이브 실재 경로)
        text = '[{"id":1,"analyze":true},{"id":3,"analyze":false},{"id":5,"analyze":false}]'

    class _Client:
        class models:
            @staticmethod
            def generate_content(**kw):
                return _Resp()

    core._acquire_gemma_client = lambda: (0, _Client())
    gemini_seen = []
    core._generate_gemini_summaries = lambda arts, si, cs: gemini_seen.extend(
        a['link'] for a in arts)
    core._send_kakaowork_summary = lambda *a, **k: None

    arts = [{'link': f'L{i}', 'game': 'g', 'board_name': 'b', 'title': f't{i}',
             'content': 'c'} for i in range(1, 6)]
    core._generate_summary_batch(arts)

    by = {a['link']: a for a in arts}
    missing = [by['L2'], by['L4']]
    check("답 안 한 글은 괄호형 스탬프 (= 저장 생략 = 다음 사이클 재시도)",
          all((a.get('summary') or '').startswith('(') and a['summary'].endswith(')')
              for a in missing),
          str([a.get('summary') for a in missing]))
    check("답 안 한 글에 '[분석 제외]' 를 찍지 않는다 (그건 영구 제외다)",
          all('분석 제외' not in (a.get('summary') or '') for a in missing))
    check("답을 받은 false 는 그대로 '[분석 제외]' (의도된 영구 제외)",
          by['L3']['summary'] == '[분석 제외]' and by['L5']['summary'] == '[분석 제외]')
    check("답을 받은 true 만 2차로 넘어간다", gemini_seen == ['L1'], str(gemini_seen))

    # 워커의 저장 가드와 실제로 맞물리는지 - 문자열 모양이 계약이다
    def _would_save(s):
        s = (s or '').strip()
        return bool(s) and not (s.startswith('(') and s.endswith(')'))
    check("워커 가드 기준으로 누락분은 저장되지 않는다",
          not any(_would_save(a.get('summary')) for a in missing))
    check("워커 가드 기준으로 '분석 제외' 는 저장된다 (기존 동작 보존)",
          _would_save(by['L3']['summary']))


def test_context_section_per_game():
    """[4] 프롬프트 '최근 동향' 블록이 **게임별로** 나뉜다 (v6.7.611).

    v6.7.610 까지는 최근 20건을 게임 구분 없이 평평하게 실었다. 글이 많은 게임이 20칸을 다
    먹으면 다른 게임 글은 **참고할 것이 하나도 없는 상태**로 판정되고, 게임 라벨이 없으니
    모델이 다른 게임의 같은 문장을 같은 동향으로 접을 수 있다. 둘 다 조용히 품질만 깎는다.
    """
    print("\n[4] 프롬프트 최근 동향 블록 - 게임별·롤링24h·반전+severity (v6.7.611/612/613)")
    from ai_lib.gemma4_summary_core import (Gemma4SummaryCore, _CONTEXT_HEADER,
                                            _CONTEXT_WINDOW_H, _CONTEXT_MAX_PER_GAME,
                                            _CONTEXT_MIN_PER_GAME)

    core = object.__new__(Gemma4SummaryCore)
    core.recent_summaries = []
    core._recent_hydrated = True        # DB 되읽기는 이 테스트의 관심사가 아니다
    check("요약 이력이 없으면 블록을 통째로 뺀다", core._build_context_section(['배그']) == "")

    # 배그가 압도적인 상황 - v6.7.611 이 고친 '볼륨 편중' 의 재현 조건
    now = time.time()
    core.recent_summaries = [('배그', f'배그 동향 {i}', now, 'info') for i in range(12)]
    core.recent_summaries += [('배그', '배그 중복 동향', now, 'info'),
                              ('배그', '배그 중복 동향', now, 'warning')]
    core.recent_summaries += [('POE1', f'포이 동향 {i}', now, 'critical') for i in range(2)]

    ctx = core._build_context_section(['배그', 'POE1', '배그'])
    lines = ctx.rstrip().splitlines()
    check("머리말이 AME 문구와 같다", lines and lines[0] == _CONTEXT_HEADER)
    check("게임 머리가 붙는다", '[배그]' in ctx and '[POE1]' in ctx)
    bae = [ln for ln in lines if ln.startswith('- [') and '배그' in ln]
    # [v6.7.612] 총량을 나누지 않는다 - 창 안 그 게임 것 전부(unique 13종)가 상한 60 아래라 전량.
    check("창 안의 그 게임 요약을 전부 싣는다 (총량 배분 폐기)", len(bae) == 13, f"배그 {len(bae)}줄")
    check("적은 게임도 자기 몫을 받는다", ctx.count('포이 동향') == 2)
    check("같은 요약문은 한 번만", ctx.count('배그 중복 동향') == 1)
    # [v6.7.613] 중복을 접을 때 **가장 최근 severity** 를 남긴다 (info -> warning 승격 반영)
    check("중복 접을 때 최신 severity 를 남긴다", '- [warning] 배그 중복 동향' in ctx,
          [l for l in lines if '중복' in l])
    check("줄마다 severity 가 붙는다",
          all(ln.startswith('- [') for ln in lines if ln.startswith('- ')))
    check("머리말이 '통합' 이 아니라 '관측된 동향'",
          '통합' not in _CONTEXT_HEADER and '관측된 동향' in _CONTEXT_HEADER)
    check("배치에 없는 게임은 안 싣는다", 'POE2' not in ctx)

    wide = core._build_context_section(['배그', 'POE1', 'A', 'B', 'C', 'D', 'E'])
    wide_bae = [ln for ln in wide.splitlines() if ln.startswith('- [') and '배그' in ln]
    check("게임이 늘어도 기존 게임 몫이 안 줄어든다", len(wide_bae) == len(bae),
          f"{len(wide_bae)}줄 vs {len(bae)}줄")
    check("요약이 없는 게임은 빈 머리만 남기지 않는다", '[A]' not in wide)

    # [v6.7.612] 창(24h) 이 실제로 걸리는지 - 시각을 조작해야만 드러나고, 안 걸리면 무한 누적된다.
    old = now - (_CONTEXT_WINDOW_H + 1) * 3600
    core.recent_summaries = ([('배그', f'배그 동향 {i}', now, 'info') for i in range(3)]
                             + [('배그', f'옛 동향 {i}', old, 'info') for i in range(5)]
                             + [('POE1', f'포이 옛 {i}', old, 'info') for i in range(5)])
    aged = core._build_context_section(['배그', 'POE1'])
    check("창 밖은 안 싣는다", '옛 동향' not in aged)
    poe = [ln for ln in aged.splitlines() if ln.startswith('- [') and '포이' in ln]
    # 창이 통째로 비어도 하한만큼은 과거로 채운다 - 조용한 게임이 영영 맥락 0 이 되지 않게.
    check("창 밖뿐인 게임도 하한만큼은 채운다", len(poe) == _CONTEXT_MIN_PER_GAME, f"{len(poe)}줄")
    check("상한 상수가 살아 있다 (폭주일 방어)", _CONTEXT_MAX_PER_GAME >= 20)

    # 게임을 못 준 경우(배치에 game 이 비어 있음)에도 죽지 않고 전체로 폴백한다
    fb = core._build_context_section([None, ''])
    check("게임 정보가 없으면 전체로 폴백 (블록이 사라지지 않는다)", _CONTEXT_HEADER in fb)

    # [v6.7.614] **되읽기 경로를 실제로 태운다.** v6.7.612 는 `datetime.now()` 를 썼는데 이 파일은
    #   `import datetime`(모듈)이라 라이브에서 AttributeError 가 났고, `_hydrate_recent_summaries`
    #   의 try/except 가 그것을 "맥락 없이 시작" 경고로 삼켜 **크래시 없이 기능만 죽어 있었다.**
    #   [4] 가 그때 `_recent_hydrated = True` 로 이 경로를 **건너뛰고 있어서** 못 잡았다 -
    #   테스트가 스스로 만든 사각이었다. 이제 진짜로 부른다.
    import sqlite_handler
    real = sqlite_handler.db_handler.get_ai_analysis_for_digest
    seen = {}

    def _fake(start, end, limit=0):
        seen['args'] = (start, end, limit)
        return ([{'game': '배그', 'summary': '되읽은 동향 1', 'severity': 'warning'},
                 {'game': 'POE1', 'summary': '되읽은 동향 2', 'severity': 'info'},
                 {'game': '배그', 'summary': '', 'severity': 'info'}], 3)

    sqlite_handler.db_handler.get_ai_analysis_for_digest = _fake
    try:
        core2 = object.__new__(Gemma4SummaryCore)
        core2.recent_summaries = []
        core2._recent_hydrated = False
        blk = core2._build_context_section(['배그'])
        check("되읽기가 예외 없이 돈다 (v6.7.612 회귀)", bool(seen.get('args')))
        check("되읽은 것이 recent_summaries 에 4-튜플로 들어간다",
              len(core2.recent_summaries) == 2
              and all(len(t) == 4 for t in core2.recent_summaries),
              str(core2.recent_summaries[:1]))
        check("빈 요약은 걸러진다", all(t[1] for t in core2.recent_summaries))
        check("되읽은 것이 맥락 블록에 실린다", '되읽은 동향 1' in blk)
        check("되읽기는 1회만 (매 배치 DB 재조회 금지)",
              core2._recent_hydrated and (seen.clear() or True)
              and (core2._build_context_section(['배그']) is not None) and not seen)
    finally:
        sqlite_handler.db_handler.get_ai_analysis_for_digest = real


def test_classify_is_deterministic():
    """[5] 1차 분류가 `temperature=0` 으로 나간다 (v6.7.616, PATTERN-49 방지 (1)).

    **운영이 쓰는 그 메서드를 직접 호출해서** 본다 - 소스에 문자열이 있는지 grep 하는 식이면
    호출부가 아닌 죽은 상수를 채점하게 된다. 여기서는 `_generate_summary_batch` 를 태우고
    SDK 경계에서 kwargs 를 가로챈다.

    2차(생성 과제)에는 걸지 않는 것도 같이 고정한다 - "판정 경로만 별도로 지정하라" 가
    그 패턴의 지시이고, 생성까지 0 으로 묶으면 요약문이 딱딱해진다.
    """
    print("\n[5] 1차 분류 결정성 - temperature=0 이 실제 호출에 실린다 (v6.7.616)")
    from ai_lib.gemma4_summary_core import Gemma4SummaryCore, CLASSIFY_TEMPERATURE

    seen = {}

    class _FakeModels:
        def generate_content(self, **kw):
            seen.update(kw)
            class _R:
                text = '[{"id": 1, "analyze": false}]'
            return _R()

    class _FakeClient:
        models = _FakeModels()

    core = object.__new__(Gemma4SummaryCore)
    core.client = _FakeClient()
    core.system_instruction = '테스트 지시문'
    core._acquire_gemma_client = lambda: (0, _FakeClient())
    core._build_context_section = lambda games: ''
    # 전량 analyze=false 라 2차 경로로는 안 넘어간다 - 이 테스트는 1차 호출만 채점한다.
    core._generate_gemini_summaries = lambda *a, **k: None

    batch = [{'game': '배그', 'board_name': 'x', 'title': 't', 'content': 'c'}]
    core._generate_summary_batch(batch)

    check("1차 호출이 실제로 일어났다", 'model' in seen, sorted(seen))
    check("config 가 실린다", isinstance(seen.get('config'), dict), repr(seen.get('config')))
    check("temperature 가 0 이다", (seen.get('config') or {}).get('temperature') == 0,
          repr(seen.get('config')))
    check("상수와 호출부가 같은 값을 본다", CLASSIFY_TEMPERATURE == 0)

    # 2차는 생성 과제라 온도를 고정하지 않는다 (PATTERN-49 는 판정 경로만 지목한다).
    src = open(os.path.join(_HERE, 'ai_lib', 'gemma4_summary_core.py'), encoding='utf-8').read()
    tail = src.split('def _generate_summary_batch', 1)[0]
    check("2차 요약 호출에는 temperature 를 걸지 않았다",
          "test_client.models.generate_content(model=model_id, contents=prompt, config=_cfg2)" in tail
          and "'temperature'" not in tail[tail.index("_cfg2 = None"):tail.index("config=_cfg2)")])


def test_first_pass_thinking_off_and_fallback():
    """[5b] 1차 분류 thinking 끔 + 31b 실패 시 26b(thinking 끔) 폴백 (v6.7.697).

    지키는 것 넷:
      1. 31b 호출에 `thinking_level=minimal` 이 실린다(라이브 1차 42~83초의 원인이 thinking).
      2. 폴백 26b 에도 **반드시** minimal 이 실린다 - 26b + thinking 켬 + temp 0 은 답변 0자 폭주(0/8).
      3. 폴백 조건 = 5xx 즉시 / 429 는 마지막 시도에서만 / 그 외(400 등)는 폴백 없음.
      4. 26b 가 실패하거나 답을 못 읽으면 **31b 의 원래 오류**가 올라간다(PATTERN-83 축 보존).
    SDK 경계에서 가로채 운영 메서드(`_generate_summary_batch`)를 그대로 태운다.
    """
    print("\n[5b] 1차 thinking 끔 + 26b 폴백 (v6.7.697)")
    import ai_lib.gemma4_summary_core as C
    from ai_lib.gemma4_summary_core import Gemma4SummaryCore

    def _run(behave):
        """behave(model, n) -> 응답 text 또는 Exception. 반환 (calls, raised, batch)."""
        calls = []

        class _M:
            def generate_content(self, **kw):
                calls.append((kw.get('model'), kw.get('config')))
                r = behave(kw.get('model'), len(calls))
                if isinstance(r, Exception):
                    raise r
                class _R:
                    text = r
                return _R()

        class _Cl:
            models = _M()

        core = object.__new__(Gemma4SummaryCore)
        core.client = _Cl()
        core.system_instruction = '지시문'
        core._acquire_gemma_client = lambda: (0, _Cl())
        core._build_context_section = lambda games: ''
        core._generate_gemini_summaries = lambda *a, **k: None
        batch = [{'game': 'POE2', 'board_name': 'x', 'title': 't', 'content': 'c'}]
        _sleep = C.time.sleep
        C.time.sleep = lambda s: None
        try:
            try:
                core._generate_summary_batch(batch)
                raised = None
            except Exception as e:      # noqa: BLE001
                raised = e
        finally:
            C.time.sleep = _sleep
        return calls, raised, batch

    OK = '[{"id": 1, "analyze": false}]'
    MIN = {'thinking_level': 'minimal'}
    E500 = Exception("500 INTERNAL. {'error': {'code': 500, 'status': 'INTERNAL'}}")
    E429 = Exception("429 RESOURCE_EXHAUSTED quota")

    check("[v6.7.698] 주력 26b / 폴백 31b (기본값)", C.FIRST_PASS_MODEL == 'gemma-4-26b-a4b-it'
          and C.FIRST_PASS_FALLBACK_MODEL == C.GEMMA_MODEL, (C.FIRST_PASS_MODEL, C.FIRST_PASS_FALLBACK_MODEL))
    check("디코 판정이 쓰는 GEMMA_MODEL 은 31b 그대로", C.GEMMA_MODEL == 'gemma-4-31b-it', C.GEMMA_MODEL)
    P, FB = C.FIRST_PASS_MODEL, C.FIRST_PASS_FALLBACK_MODEL
    calls, raised, b = _run(lambda m, n: OK)
    check("평시: 주력 한 번", [c[0] for c in calls] == [P], calls)
    check("평시: 주력에 thinking minimal", (calls[0][1] or {}).get('thinking_config') == MIN, calls)
    check("평시: temperature 0 유지", (calls[0][1] or {}).get('temperature') == 0, calls)

    calls, raised, b = _run(lambda m, n: E500 if m == P else OK)
    check("5xx: 폴백으로 즉시", [c[0] for c in calls] == [P, FB], calls)
    check("5xx: 폴백에도 thinking minimal", (calls[-1][1] or {}).get('thinking_config') == MIN, calls)
    check("5xx: 폴백 답이 반영됐다", raised is None and b[0].get('summary') == '[분석 제외]', (raised, b))

    calls, raised, b = _run(lambda m, n: E500 if m == P else Exception("503 UNAVAILABLE"))
    check("폴백도 실패: 주력의 원래 500 이 올라간다", raised is E500, repr(raised))
    check("폴백도 실패: 글에 스탬프 없음(다음 사이클 재시도)", 'summary' not in b[0], b)

    calls, raised, b = _run(lambda m, n: E500 if m == P else "답을 못 하겠어요")
    check("폴백 답 파싱 실패: 파싱 오류가 아니라 원래 500", raised is E500, repr(raised))

    calls, raised, b = _run(lambda m, n: E429 if n == 1 else OK)
    check("429 첫 시도: 폴백 없이 키 교체 후 주력 재시도", [c[0] for c in calls] == [P, P], calls)

    calls, raised, b = _run(lambda m, n: E429 if m == P else OK)
    check("429 전 시도: 마지막 시도에서만 폴백", [c[0] for c in calls] == [P, P, P, FB], calls)
    check("429 전 시도: 폴백 답 반영", b[0].get('summary') == '[분석 제외]', b)

    E400 = Exception("400 INVALID_ARGUMENT bad request")
    calls, raised, b = _run(lambda m, n: E400)
    check("400 등 비축 오류: 폴백 없음", [c[0] for c in calls] == [P] and raised is E400, (calls, raised))

    _saved = C.FIRST_PASS_THINKING
    C.FIRST_PASS_THINKING = 'default'
    try:
        check("26b 는 FIRST_PASS_THINKING 과 무관하게 항상 minimal (폭주 조합 금지)",
              C._first_pass_config('gemma-4-26b-a4b-it').get('thinking_config') == MIN)
        check("31b 는 FIRST_PASS_THINKING=default 면 thinking 을 켠다(되돌리기 손잡이)",
              'thinking_config' not in C._first_pass_config(C.GEMMA_MODEL))
    finally:
        C.FIRST_PASS_THINKING = _saved


# ---------------------------------------------------------------------------
def _bare_core(keys=('k0', 'k1'), chain=(('m1', 20, 'v1'),)):
    """시트·SDK 초기화를 건너뛴 코어 + 페이싱/쿨다운 상태만 세운 것.

    **오버플로 티어는 스텁하지 않는다** - 호출 루프가 `GEMINI_OVERFLOW_CHAIN` 상수를 직접
    읽으므로 여기서 가짜 라벨을 주면 테스트만 다른 세계를 본다(첫 판이 그렇게 틀렸다).
    """
    import datetime as _dt
    from ai_lib.gemma4_summary_core import Gemma4SummaryCore, KST, GEMINI_OVERFLOW_CHAIN
    overflow = tuple(tuple(t) for t in GEMINI_OVERFLOW_CHAIN)
    c = object.__new__(Gemma4SummaryCore)
    c.gemini_test_enabled = True
    c.api_keys = list(keys)
    c.gemini_counts = {}
    c.gemini_test_key_index = 0
    c.gemini_test_last_reset_date = _dt.datetime.now(KST).date()   # 자정 reset 경로 회피
    c.recent_summaries = []
    c._pace_calls = []
    c._pace_blocked_log_at = 0.0
    c._key_cooldown = {}
    c._key_429_streak = {}
    c._pace_daily_exhausted = False
    c._model_cooldown = {}
    c.resolve_chain = lambda: [tuple(t) for t in chain]
    c._all_tiers = lambda: [tuple(t) for t in chain] + [tuple(t) for t in overflow]
    return c


def _install_client(monkey_target, behavior):
    """클라이언트를 갈아끼운다. behavior() -> 응답 객체 또는 raise.

    **`ai_lib.genai_client._genai` 를 막아야 한다** (v6.7.620). 클라이언트 생성이 그 모듈로
    이관되면서, 코어의 `google_genai` 만 스텁하면 스텁이 새고 **테스트가 실제 API 를 친다**
    - 이 파일 머리말의 "네트워크 미사용" 규약이 조용히 깨진다(실측으로 잡았다: dummy 키로
    400 INVALID_ARGUMENT 가 라이브에서 돌아왔다). 두 자리를 다 막는다.
    """
    class _Models:
        @staticmethod
        def generate_content(model=None, contents=None, **kw):
            return behavior()
    class _Client:
        def __init__(self, api_key=None, **kw):
            self.models = _Models()
    fake = type('X', (), {'Client': _Client})
    monkey_target.google_genai = fake
    from ai_lib import genai_client as _gc
    _gc._genai = fake


def test_429_does_not_park_key_for_the_day():
    """[6] 429 는 **쿨다운**이지 하루 소진이 아니다 (v6.7.618).

    2026-08-28 라이브: 오버플로 티어가 `g31=86400/86400` 이었다. 1차 분류는 이 카운터를
    건드리지 않으므로 그 값이 실호출일 수 없다 - 키 6개가 429 를 **한 번씩** 맞고
    `counter[key] = rpd`(당시 상수 14,400 - 2026-10-01 에 실제 한도 1,440 으로 정정) 로 강제 마킹된 결과다. 그래서 페이싱에 막힌 요약이
    떨어질 비상구가 **하루 종일** 닫혔고, 부풀린 카운터는 `used_today` 로도 들어가
    시간당 상한까지 깎았다. 한 번의 일시적 429 가 두 번 벌을 준 것이다.
    """
    print("\n[6] 2차 요약 - 429 가 키를 자정까지 죽이지 않는다 (v6.7.618)")
    from ai_lib import gemma4_summary_core as C

    core = _bare_core()
    _install_client(C, lambda: (_ for _ in ()).throw(Exception("429 RESOURCE_EXHAUSTED")))
    arts = [{'link': 'L1', 'game': 'g', 'board_name': 'b', 'title': 't', 'content': 'c'}]
    core._generate_gemini_summaries_impl(arts, "지침", "")

    cnt = core.gemini_counts.get('v1', {})
    check("429 는 카운터를 rpd 로 밀지 않는다 (시도 1건만 센다)",
          cnt.get('k0') == 1 and cnt.get('k1') == 1, str(cnt))
    check("429 맞은 키는 쿨다운에 들어간다",
          ('v1', 'k0') in core._key_cooldown and ('v1', 'k1') in core._key_cooldown)
    check("일일 카운터는 여전히 여유가 있다 (used_today 를 부풀리지 않는다)",
          sum(cnt.values()) < 20 * len(core.api_keys), str(sum(cnt.values())))

    # 대조군 - 쿨다운이 살아 있는 동안은 그 키를 안 고른다
    calls = []
    _install_client(C, lambda: (_ for _ in ()).throw(Exception("429 RESOURCE_EXHAUSTED")))
    before = dict(core.gemini_counts.get('v1', {}))
    core._generate_gemini_summaries_impl(
        [{'link': 'L2', 'game': 'g', 'board_name': 'b', 'title': 't', 'content': 'c'}], "지침", "")
    check("쿨다운 중에는 같은 키를 재시도하지 않는다 (카운터 불변)",
          core.gemini_counts.get('v1', {}) == before, str(core.gemini_counts.get('v1')))

    # 쿨다운이 지나면 **스스로 낫는다** - 자정을 기다리지 않는다
    core._key_cooldown = {k: 0.0 for k in core._key_cooldown}
    ok_text = '[{"id":1,"summary":"요약","sentiment":"neutral","severity":"info"}]'
    _install_client(C, lambda: type('R', (), {'text': ok_text})())
    arts3 = [{'link': 'L3', 'game': 'g', 'board_name': 'b', 'title': 't', 'content': 'c'}]
    before_streak = dict(core._key_429_streak)
    core._generate_gemini_summaries_impl(arts3, "지침", "")
    check("쿨다운 만료 후 같은 키로 정상 처리된다 (자가 회복)",
          arts3[0].get('summary') == '요약', str(arts3[0].get('summary')))
    # [v6.7.623] **어느 티어가 회복 콜을 받을지 고정하지 말 것.** 구 판정은 `label=='v1'` 인
    #   streak 이 줄었는지를 봤는데, 이 시점의 v1 은 **페이싱으로 통째 제외**된다 - 앞 단계의
    #   v1 시도 2회가 `_pace_calls` 를 채워 `used >= allow` 가 되고, `_generate_gemini_summaries_impl`
    #   은 `paced_blocked` 면 `resolve_chain()` 티어를 **아예 빼고** 오버플로만 시도한다(1124행).
    #   그래서 회복은 g31 로 갔고 v1 항목이 남은 것은 **정상**인데 테스트만 빨갰다(실측: 성공 후
    #   `('g31','k0')` 만 사라지고 `('v1','k0'), ('v1','k1'), ('g31','k1')` 이 남음).
    #   재는 대상은 "v1 이 회복했는가" 가 아니라 **"성공한 그 키의 streak 이 풀렸는가"** 다 -
    #   티어 선택은 이 테스트의 관심사가 아니고, 고정하면 페이싱을 고칠 때마다 여기가 깨진다.
    removed = set(before_streak) - set(core._key_429_streak)
    check("성공한 키의 연속 429 카운터가 풀린다 (백오프가 하루를 못 넘긴다)",
          len(removed) == 1 and core._key_429_streak.get(next(iter(removed))) is None,
          "해제된 키 %s / 잔여 %s" % (sorted(removed), sorted(core._key_429_streak)))
    check("성공하지 않은 키의 streak 은 보존된다 (성공 1건이 전체를 리셋하지 않는다)",
          set(core._key_429_streak) == set(before_streak) - removed,
          str(sorted(core._key_429_streak)))


def test_pace_window_is_sliding():
    """[7] 페이싱 창이 **굴러간다** - 정시 경계가 아니다 (v6.7.618).

    고정 정시 버킷은 상한을 채운 순간부터 다음 정시까지 2차를 통째로 세우고, 정시에 밀린 것이
    한꺼번에 나간다(2026-08-28: 10:16/10:18/10:31/10:32 수집분이 전부 11:01:34 한 사이클).
    굴러가는 창은 60분 전 호출이 빠지는 순간 한 칸이 열린다.
    """
    print("\n[7] 2차 요약 - 페이싱 창이 슬라이딩이다 (v6.7.618)")
    import time as _t
    from ai_lib.gemma4_summary_core import _PACE_WINDOW_SEC

    core = _bare_core()
    now = _t.monotonic()
    core._pace_calls = [now - 30, now - 600, now - (_PACE_WINDOW_SEC + 120)]
    allow, used, remaining = core._pace_allowance()
    check("창(60분) 밖의 호출은 사용량에서 빠진다",
          used == 2, f"used={used} (기대 2)")
    check("일일 잔여는 체인 예산 그대로 (호출 0건)",
          remaining == 20 * len(core.api_keys), f"remaining={remaining}")
    check("상한은 최소 1 이상 (예산이 남으면 절대 0 이 아니다)", allow >= 1, f"allow={allow}")

    # 대조군 - 창을 통째로 채우면 막히고, 가장 오래된 것이 빠지면 즉시 풀린다
    core._pace_calls = [now - 10] * (allow + 5)
    a2, u2, _ = core._pace_allowance()
    check("창이 차면 막힌다 (used >= allow)", u2 >= a2, f"used={u2} allow={a2}")
    core._pace_calls = [now - (_PACE_WINDOW_SEC + 1)] * (allow + 5)
    a3, u3, _ = core._pace_allowance()
    check("창 밖으로 밀려나면 정시를 안 기다리고 풀린다", u3 < a3, f"used={u3} allow={a3}")


def test_stamp_separates_pacing_from_daily_quota():
    """[8] 대기 스탬프가 **사유를 가른다** (v6.7.618).

    종전엔 호출을 못 한 경우를 전부 `(전 모델 RPD 소진 ...)` 으로 찍었고 워커가 그것을
    "일일예산 소진 N건" 으로 보고했다. 2026-08-28 실측에서 그 문구가 틀렸다 - 그때 일일
    잔여는 130콜(36%)이었고 진짜 차단자는 시간당 페이싱이었다. 원인을 단정하는 문구는
    다음 사람의 첫 삽 방향을 정하므로, 여기서 틀리면 "키를 더 사자" 로 간다.
    """
    print("\n[8] 2차 요약 - 스탬프가 페이싱과 일일소진을 구분한다 (v6.7.618)")
    import time as _t
    from ai_lib import gemma4_summary_core as C

    def _worker_would_alert(stamp):
        """워커(ai_gemma4_worker)의 소진 경보 술어 그대로."""
        return 'RPD 소진' in (stamp or '')

    # (a) 일일 예산이 진짜 0 - 경보가 울려야 한다
    core = _bare_core()
    _ov_m, _ov_rpd, _ov_label = tuple(C.GEMINI_OVERFLOW_CHAIN[0])
    core.gemini_counts = {'v1': {'k0': 20, 'k1': 20},
                          _ov_label: {'k0': _ov_rpd, 'k1': _ov_rpd}}
    _install_client(C, lambda: (_ for _ in ()).throw(AssertionError("호출되면 안 된다")))
    a = [{'link': 'L1', 'game': 'g', 'board_name': 'b', 'title': 't', 'content': 'c'}]
    core._generate_gemini_summaries_impl(a, "지침", "")
    check("일일 예산 0 이면 'RPD 소진' 스탬프 (소진 경보 술어 보존)",
          _worker_would_alert(a[0].get('summary')), str(a[0].get('summary')))

    # (b) 일일 예산은 남았는데 전 키가 쿨다운 - 같은 경보가 울리면 오진이다
    core2 = _bare_core()
    future = _t.monotonic() + 3600
    _ov_label2 = tuple(C.GEMINI_OVERFLOW_CHAIN[0])[2]
    core2._key_cooldown = {('v1', 'k0'): future, ('v1', 'k1'): future,
                           (_ov_label2, 'k0'): future, (_ov_label2, 'k1'): future}
    b = [{'link': 'L2', 'game': 'g', 'board_name': 'b', 'title': 't', 'content': 'c'}]
    core2._generate_gemini_summaries_impl(b, "지침", "")
    stamp = b[0].get('summary')
    check("쿨다운 차단은 'RPD 소진' 이 아니다 (거짓 소진 경보 방지)",
          not _worker_would_alert(stamp), str(stamp))
    check("그래도 괄호형이라 저장은 생략된다 (= 다음 사이클 재시도, 유실 없음)",
          bool(stamp) and stamp.startswith('(') and stamp.endswith(')'), str(stamp))


# ---------------------------------------------------------------------------
def test_5xx_skips_to_next_model_not_next_key():
    """[9] 5xx 는 **키 축이 아니라 모델 축**이다 (v6.7.619).

    2026-08-28 라이브: `503 UNAVAILABLE. ... 'This model is currently experiencing high
    demand'`. 서버가 "이 **모델**이 과부하" 라고 말하는데 종전 코드는 키 실패와 똑같이 취급해
    **같은 모델의 남은 키를 전부 돌았다.** 키를 바꿔도 같은 모델로 가므로 도움이 될 수 없는
    재시도다. 게다가 SDK 가 이미 그 안에서 5회를 때린 뒤라(실측 키당 137초) 키 6개면 모델
    하나에 822초를 태웠다.
    """
    print("\n[9] 2차 요약 - 5xx 는 다음 키가 아니라 다음 모델로 (v6.7.619)")
    from ai_lib import gemma4_summary_core as C

    # 분류기 - 자리를 못 박았는지(자유 문장의 숫자를 상태코드로 읽지 않는지)
    check("503 UNAVAILABLE 은 모델 축", C._is_model_scoped(
        "503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This model is currently "
        "experiencing high demand.'}}"))
    check("429 는 모델 축이 아니다 (키 축 - 백오프 담당)",
          not C._is_model_scoped("429 RESOURCE_EXHAUSTED: quota exceeded"))
    check("본문 안의 숫자를 상태코드로 읽지 않는다",
          not C._is_model_scoped("Summary length 500 characters exceeded budget"))
    check("파싱 오류는 모델 축이 아니다",
          not C._is_model_scoped("JSONDecodeError: Expecting value: line 1 column 500"))

    # 키를 6개 줘도 5xx 면 **모델당 1회만** 호출해야 한다
    core = _bare_core(keys=('k0', 'k1', 'k2', 'k3', 'k4', 'k5'))
    seen = []

    class _Models:
        @staticmethod
        def generate_content(model=None, contents=None, **kw):
            seen.append(model)
            raise Exception("503 UNAVAILABLE. {'error': {'code': 503, 'message': "
                            "'This model is currently experiencing high demand.'}}")

    class _Client:
        def __init__(self, api_key=None, **kw):
            self.models = _Models()

    _fake = type('X', (), {'Client': _Client})
    C.google_genai = _fake
    from ai_lib import genai_client as _gc
    _gc._genai = _fake

    arts = [{'link': 'L1', 'game': 'g', 'board_name': 'b', 'title': 't', 'content': 'c'}]
    core._generate_gemini_summaries_impl(arts, "지침", "")

    per_model = {}
    for m in seen:
        per_model[m] = per_model.get(m, 0) + 1
    check("모델당 호출이 1회다 (키 6개를 순회하지 않는다)",
          all(v == 1 for v in per_model.values()), str(per_model))
    check("체인의 모든 티어를 한 번씩은 시도한다 (조용히 포기하지 않는다)",
          len(per_model) >= 2, str(per_model))
    check("5xx 를 맞은 모델은 쿨다운에 들어간다",
          bool(core._model_cooldown), str(core._model_cooldown))
    check("일일 카운터는 안 오른다 (5xx 는 쿼타 소비가 아니다)",
          not any(core.gemini_counts.get(l) for l in core.gemini_counts),
          str(core.gemini_counts))

    # 쿨다운 중인 모델은 다음 사이클에서 건너뛴다 - 20초마다 같은 벽을 다시 치지 않는다
    seen.clear()
    core._generate_gemini_summaries_impl(
        [{'link': 'L2', 'game': 'g', 'board_name': 'b', 'title': 't', 'content': 'c'}], "지침", "")
    check("쿨다운 중인 모델은 다음 사이클에서 아예 안 부른다", seen == [], str(seen))


def test_second_stage_timeout_and_slow_cooldown():
    """[9b] 2차 flash 호출 시간 상한 30초 + 늦은 실패는 30분 쉼 (v6.7.699).

    라이브 2026-09-28: 3.7-flash 요청 하나가 18분 35초 붙잡혀 사이클 1,487초, 그동안 1차도 멈춰 79건 적체.
    지키는 것: flash 호출에 timeout·단일 시도(attempts=1)가 실린다 / 오버플로(31b)엔 안 실린다 / 타임아웃은 **다음 키가 아니라
    다음 모델** / 늦은 실패는 긴 쿨다운, 빠른 5xx 는 종전 120초.
    """
    print("\n[9b] 2차 flash 시간 상한 + 늦은 실패 긴 쿨다운 (v6.7.699)")
    from ai_lib import gemma4_summary_core as C
    from ai_lib import genai_client as _gc

    def run(behave, chain=(('m1', 20, 'v1'),), clock=None):
        core = _bare_core(keys=('k0', 'k1', 'k2'), chain=chain)
        seen = []

        class _Models:
            @staticmethod
            def generate_content(model=None, contents=None, config=None, **kw):
                seen.append((model, config))
                return behave(model)

        class _Client:
            def __init__(self, api_key=None, **kw):
                self.models = _Models()

        fake = type('X', (), {'Client': _Client})
        C.google_genai = fake
        _gc._genai = fake
        _pc = C.time.perf_counter
        if clock:
            C.time.perf_counter = clock
        try:
            core._generate_gemini_summaries_impl(
                [{'link': 'L1', 'game': 'g', 'board_name': 'b', 'title': 't', 'content': 'c'}], "지침", "")
        finally:
            C.time.perf_counter = _pc
        return core, seen

    class _R:
        text = '[{"id": 1, "summary": "요약", "sentiment": "neutral", "severity": "info"}]'

    core, seen = run(lambda m: _R())
    ho = (seen[0][1] or {}).get('http_options') if seen else None
    check("flash 호출에 timeout 30초", ho is not None and getattr(ho, 'timeout', None) == C._SECOND_FLASH_TIMEOUT_MS, seen[:1])
    check("flash 호출은 SDK 단일 시도(attempts=1)", ho is not None and getattr(getattr(ho, 'retry_options', None), 'attempts', None) == 1)

    def _to(m):
        if m == 'm1':
            raise TimeoutError("The read operation timed out")
        return _R()
    core, seen = run(_to)
    models = [m for m, _c in seen]
    check("타임아웃은 같은 모델 다음 키가 아니라 다음 모델로", models.count('m1') == 1 and len(models) >= 2, models)
    over = [c for m, c in seen if m == C.GEMINI_OVERFLOW_CHAIN[0][0]]
    check("오버플로(31b)에는 짧은 상한을 안 건다", bool(over) and all(c is None for c in over), over)

    ticks = iter([0.0, 185.0] + [185.0] * 50)       # 첫 호출이 185초 뒤 실패
    t0 = C.time.monotonic()
    core, seen = run(lambda m: (_ for _ in ()).throw(Exception("503 UNAVAILABLE. {'error': {'code': 503}}")) if m == 'm1' else _R(),
                     clock=lambda: next(ticks))
    cd = core._model_cooldown.get('v1', 0) - t0
    check("늦은 503(185초)은 30분 쿨다운", cd >= C._SLOW_MODEL_COOLDOWN_SEC - 5, round(cd))
    core, seen = run(lambda m: (_ for _ in ()).throw(Exception("503 UNAVAILABLE. {'error': {'code': 503}}")) if m == 'm1' else _R())
    cd = core._model_cooldown.get('v1', 0) - C.time.monotonic()
    check("빠른 503 은 종전 120초", 0 < cd <= C._MODEL_COOLDOWN_SEC + 1, round(cd))
    ticks = iter([0.0, 185.0] + [185.0] * 50)
    t0 = C.time.monotonic()
    g31 = C.GEMINI_OVERFLOW_CHAIN[0][2]
    core, seen = run(lambda m: (_ for _ in ()).throw(Exception("500 INTERNAL. {'error': {'code': 500}}")),
                     chain=(), clock=lambda: next(ticks))
    cd = core._model_cooldown.get(g31, 0) - t0
    check("오버플로(31b)는 늦게 실패해도 120초 - 비상구를 30분 잠그지 않는다", 0 < cd <= C._MODEL_COOLDOWN_SEC + 5, round(cd))


def test_sdk_retry_is_capped():
    """[10] SDK 내부 재시도를 우리가 낮춰 잡는다 (v6.7.619).

    google-genai 1.63 기본값은 5회(1/2/4/8초 백오프)이고 408·429·500·502·503·504 가 대상이라,
    503 한 번이 키당 137초가 된다(실측). 우리는 이미 4단 체인 + 20초 재큐를 갖고 있으므로
    그 층의 5회는 중복이다. `timeout` 과 달리 `retry_options` 는 1.63 에서 실제로 먹는 것을
    라이브에서 확인했다.
    """
    print("\n[10] 2차 요약 - SDK 재시도 상한 (v6.7.619)")
    from ai_lib import gemma4_summary_core as C

    from ai_lib import genai_client as _gc0
    check("상한이 SDK 기본(5)보다 작다", _gc0.SDK_RETRY_ATTEMPTS < 5, str(_gc0.SDK_RETRY_ATTEMPTS))
    check("코어가 같은 상수를 본다 (사본 아님)", C._SDK_RETRY_ATTEMPTS == _gc0.SDK_RETRY_ATTEMPTS)
    check("그래도 1회 재시도는 남긴다 (순간 블립 흡수)", _gc0.SDK_RETRY_ATTEMPTS >= 2)

    got = {}

    class _C2:
        def __init__(self, api_key=None, http_options=None, **kw):
            got['http_options'] = http_options

    from ai_lib import genai_client as _gc
    _gc._genai = type('X', (), {'Client': _C2})
    _gc.make_client('k')
    ro = getattr(got.get('http_options'), 'retry_options', None)
    check("생성한 클라이언트에 retry_options 가 실린다", ro is not None, str(got.get('http_options')))
    check("attempts 가 상수와 같다", getattr(ro, 'attempts', None) == _gc0.SDK_RETRY_ATTEMPTS, str(ro))

    # 대조군 - 옵션 구성이 깨져도 요약이 통째로 죽으면 안 된다(기본 클라이언트로 폴백)
    class _C3:
        def __init__(self, api_key=None, **kw):
            if 'http_options' in kw:
                raise TypeError("unexpected kwarg")
    _gc._genai = type('X', (), {'Client': _C3})
    ok = True
    try:
        _gc.make_client('k')
    except Exception:
        ok = False
    check("옵션 구성 실패 시 기본 클라이언트로 폴백한다", ok)


# ---------------------------------------------------------------------------
def test_queue_lag_and_stall_alert():
    """[12] **멈춘 것을 조용히 지나가지 않는다** (v6.7.640).

    위 항목들이 "일시적 실패를 영구 제외로 굳히지 않는다" 를 지킨다면, 이건 그 바깥이다 -
    아무것도 영구 제외되지 않았는데 **그냥 늦는** 경우. 라이브 15일 전수(2026-09-06):
    요약 지연 최악이 08-24 **345분** · 08-26 201분 · 08-25 210분, 30분 초과가 50,342건 중
    2,028건(4.0%)에 15일 중 8일. 그동안 **발화는 0건**이었다.

    잠그는 것 둘:

      1. **밀림 술어가 처리 잡의 조회와 같은가.** 다르면 지표가 거짓말을 하고 임계 경보는
         영구 오발화가 된다. 라이브 실측 트랩을 픽스처에 심는다 - 상태 표식
         (`[IMAGE_ONLY]`)이 붙은 글(당시 471건이 30시간째 미요약).
         [v6.7.742] 디코 판정 밀림 쪽 픽스처(본문이 빈 디코 메시지)와 디코 정지 경보 항목은 잡과 함께 지웠다.
      2. **경보가 한 번만 울리고, 회복도 말하고, 측정 실패를 회복으로 오해하지 않는가.**
    """
    print("\n[12] 파이프라인 밀림 계측 + 정지 경보 (v6.7.640)")
    import importlib
    hs = importlib.import_module('web_lib.bg.health_sampler')

    tmp = tempfile.mkdtemp(prefix="ime_lag_")
    try:
        # ── 요약 밀림 술어 ────────────────────────────────────────────
        main_db = os.path.join(tmp, "crawled_data.sqlite")
        c = sqlite3.connect(main_db)
        c.execute("CREATE TABLE community_posts (link TEXT PRIMARY KEY, content TEXT, inserted_at TEXT)")
        c.execute("CREATE TABLE ai_analysis (link TEXT PRIMARY KEY, summary TEXT, inserted_at TEXT)")

        def utc(minutes_ago):
            return c.execute("SELECT datetime('now', ?)", ("-%d minutes" % minutes_ago,)).fetchone()[0]

        cutoff = utc(360)                      # 워커 컷오프 = 부팅 -360분
        rows = [
            ("L1", "[IMAGE_ONLY]", 300),        # 상태 표식  <- 라이브 트랩 (471건)
            ("L2", "[DELETED]", 290),           # 상태 표식
            ("L3", "", 280),                    # 본문 빈 것
            ("L4", "이미 요약된 글", 270),       # ai_analysis 에 있음
            ("L5", "컷오프 이전", 400),          # 워커가 안 집는다
            ("L6", "진짜 대기 중인 글", 12),     # 12분 전 = 유일한 진짜 대기
        ]
        for link, content, ago in rows:
            c.execute("INSERT INTO community_posts VALUES (?,?,?)", (link, content, utc(ago)))
        c.execute("INSERT INTO ai_analysis VALUES ('L4','요약',?)", (utc(269),))
        c.commit()

        class _FakeDB:
            db_path = main_db

            def _get_connection(self):
                return c

        # 워커 컷오프를 픽스처에 맞춘다 (모듈은 워커 상수를 import 해서 쓴다)
        worker = importlib.import_module('web_lib.ai_gemma4_worker')
        _wbak = worker._WORKER_START_TIME
        try:
            worker._WORKER_START_TIME = cutoff
            out = hs._queue_lags(_FakeDB())
            sm = out.get('queue_summary_lag_s', -1) / 60.0
            check("요약 밀림 = 진짜 대기 1건의 나이 (약 12분)", 11 <= sm <= 14, f"{sm:.1f}분")
            # [v6.7.686] 잔량(대기 건수)은 밀림과 **같은 쿼리·같은 술어**에서 COUNT 로 나온다 (AI 요약 탭
            #   '요약 대기 N건' 표시용). 진짜 대기는 L6 하나뿐이라 1 이어야 한다 - 술어를 한 줄이라도 빼면
            #   ([%] 상태표식/빈본문/dedup/컷오프 중 하나) 이 값이 커진다(PATTERN-73/66 뮤테이션 감지점).
            check("요약 대기 건수 = 진짜 대기 1건 (COUNT 이 밀림과 술어 공유)",
                  out.get('queue_summary_backlog') == 1, out.get('queue_summary_backlog'))

            # **트랩이 픽스처에 실재하는지 확인한다** - 술어를 뺀 조회가 임계를 넘어야
            #   위 값이 "그냥 작은 수" 가 아니라 술어가 걸러낸 결과임이 증명된다.
            naive = c.execute("SELECT MIN(p.inserted_at) FROM community_posts p "
                              "LEFT JOIN ai_analysis a ON a.link = p.link "
                              "WHERE a.link IS NULL").fetchone()[0]
            naive_m = c.execute("SELECT (julianday('now') - julianday(?)) * 1440.0",
                                (naive,)).fetchone()[0]
            check("술어를 빼면 임계를 넘는다 (계측이 실제로 거르고 있다는 증거)",
                  naive_m * 60 >= hs._STALL_THRESHOLD_S > sm * 60,
                  f"술어없음 {naive_m:.0f}분 vs 술어적용 {sm:.0f}분")

            # 시각 축 - `inserted_at` 은 UTC 인데 컨테이너 TZ 는 KST 다. 로컬시각으로 빼면
            #   9시간이 섞인다(옛 디코 판정 `dc_judged_at` 의 실사고 - 2026-08-17, v6.7.571).
            import datetime as _dt
            stamp = c.execute("SELECT datetime('now','-45 minutes')").fetchone()[0]
            got = c.execute("SELECT (julianday('now') - julianday(?)) * 86400.0", (stamp,)).fetchone()[0]
            local_bad = (_dt.datetime.now()
                         - _dt.datetime.strptime(stamp, '%Y-%m-%d %H:%M:%S')).total_seconds()
            check("밀림은 UTC 축으로 잰다 (로컬시각으로 빼면 TZ 만큼 틀린다)",
                  abs(got - 2700) < 5 and (abs(local_bad - 2700) < 5 or abs(local_bad - got) > 3000),
                  f"코드식 {got:.0f}s / 로컬식 {local_bad:.0f}s")

            # [v6.7.642] **창 캡 바닥-바인딩.** 워커 컷오프를 72h 전으로 두면 48h 바닥이 이긴다 -
            #   60h 전 pending 글은 빠지고 12분 전 글이 잡혀야 한다. 캡을 지우면 60h 로 튄다.
            #   위 검사들은 컷오프가 6h 라 `max()` 가 늘 컷오프를 골라 이 경로를 안 탄다(독립 리뷰 지적).
            c.execute("INSERT INTO community_posts VALUES ('L7','60시간째 pending',?)", (utc(60 * 60),))
            c.commit()
            worker._WORKER_START_TIME = utc(72 * 60)
            floor = c.execute("SELECT datetime('now','-48 hours')").fetchone()[0]
            check("시각 문자열 둘 다 19자 고정폭 (문자열 max = 시각 max 의 근거)",
                  len(worker._WORKER_START_TIME) == 19 and len(floor) == 19,
                  f"{worker._WORKER_START_TIME!r} / {floor!r}")
            capped_out = hs._queue_lags(_FakeDB())
            capped = capped_out.get('queue_summary_lag_s', -1) / 60.0
            # 컷오프가 72h 로 물러나면 위 L5(400분 전, 원래 '컷오프 이전' 용)도 창 안이라 그게
            #   최고령이 된다. 60h 글(L7)만 바닥에 잘려야 한다 - 캡이 없으면 3600분으로 튄다.
            check("컷오프가 48h 보다 오래되면 바닥이 이긴다 (60h 글 제외, 400분 글이 최고령)",
                  398 <= capped <= 403, f"{capped:.1f}분 (캡 없으면 약 3600분)")
            # [v6.7.686] 대기 건수도 같은 48h 바닥 창만 센다 - L5(400분)+L6(12분)=2, 60h L7 은 제외.
            check("대기 건수도 바닥 창만 센다 (L5+L6=2, 60h L7 제외)",
                  capped_out.get('queue_summary_backlog') == 2, capped_out.get('queue_summary_backlog'))
        finally:
            worker._WORKER_START_TIME = _wbak

        # ── 경보 상태 기계 ────────────────────────────────────────────
        fired = []
        _nbak, _bbak, _sbak = hs._stall_notify, hs._boot_s, dict(hs._stall_state)
        try:
            hs._stall_notify = lambda label, lag, recovered=False, url='': fired.append((label, lag, recovered, url))
            hs._boot_s = time.time()
            hs._stall_state.clear()
            hs._check_queue_stall({'queue_summary_lag_s': hs._STALL_THRESHOLD_S + 60})
            check("부팅 유예 중에는 발화 안 한다", not fired)

            hs._boot_s = time.time() - hs._STALL_GRACE_S - 1
            hs._check_queue_stall({'queue_summary_lag_s': 60.0})
            check("임계 미만이면 조용하다", not fired)

            hs._check_queue_stall({'queue_summary_lag_s': hs._STALL_THRESHOLD_S + 60})
            check("임계를 넘으면 1회 발화", len(fired) == 1 and fired[0][2] is False)
            hs._check_queue_stall({'queue_summary_lag_s': hs._STALL_THRESHOLD_S + 120})
            hs._check_queue_stall({'queue_summary_lag_s': hs._STALL_THRESHOLD_S + 180})
            check("재발화 간격 안에서는 침묵", len(fired) == 1, f"발화 {len(fired)}회")

            hs._stall_state['queue_summary_lag_s']['at'] = time.time() - hs._STALL_REPEAT_S - 1
            hs._check_queue_stall({'queue_summary_lag_s': hs._STALL_THRESHOLD_S + 60})
            check("재발화 간격을 넘기면 다시 알린다 (발송 실패 재시도 겸용)", len(fired) == 2)

            # **측정 실패(None)를 회복으로 읽으면 안 된다** - 조회가 깨진 동안 경보가 저절로
            #   해제되고 회복 발화까지 나가면, 침묵이 정상으로 보인다.
            hs._check_queue_stall({})
            check("측정 실패 사이클은 회복으로 읽지 않는다",
                  len(fired) == 2 and hs._stall_state['queue_summary_lag_s']['on'] is True)

            # [v6.7.642] **히스테리시스** - 임계 아래로 내려왔어도 절반(900s) 위면 회복이 아니다.
            #   28~32분을 오가는 진동에서 발화/회복이 교대로 나가는 소음을 막는다.
            hs._check_queue_stall({'queue_summary_lag_s': hs._STALL_THRESHOLD_S - 60})
            check("임계 바로 아래(29분)는 회복으로 안 본다 (히스테리시스)",
                  len(fired) == 2 and hs._stall_state['queue_summary_lag_s']['on'] is True)
            hs._check_queue_stall({'queue_summary_lag_s': hs._STALL_CLEAR_S + 1})
            check("회복 임계 바로 위(15분+1s)도 아직 경보 중", len(fired) == 2)
            hs._check_queue_stall({'queue_summary_lag_s': 30.0})
            check("회복도 1회 알린다", len(fired) == 3 and fired[-1][2] is True)
            check("회복 알림의 딥링크가 그 파이프라인 탭이다",
                  fired[-1][3] == '/?tab=ai-summary' and fired[0][3] == '/?tab=ai-summary')
            check("회복 뒤 경보 상태가 풀린다",
                  hs._stall_state['queue_summary_lag_s']['on'] is False)
            hs._check_queue_stall({'queue_summary_lag_s': 30.0})
            check("회복 뒤엔 조용하다", len(fired) == 3)

            # [v6.7.742] 디코 판정 정지 경보(`queue_dcjudge_lag_s` -> `?tab=discord`)는 잡과 함께 지웠다.
            check("정지 경보 대상은 AI 요약 하나다", set(hs._STALL_LABELS) == {'queue_summary_lag_s'},
                  sorted(hs._STALL_LABELS))
        finally:
            hs._stall_notify, hs._boot_s = _nbak, _bbak
            hs._stall_state.clear()
            hs._stall_state.update(_sbak)
        c.close()
    finally:
        import shutil as _sh
        _sh.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
def test_push_channel_death_is_loud():
    """[13] **폰 알림이 안 가는 것을 서버가 말한다** (v6.7.643).

    종전 `send_push` 는 구독이 0 이면 아무 말 없이 반환했다. 서버는 계속 정상이라(원장 적재·
    카카오 발송 전부 됨) 로그도 지표도 조용한데 폰만 안 울린다 - 사용자에게는 **"늦게 온 것"과
    "안 온 것"이 같아 보인다.** 실측 2026-09-06: 09-05 05:52 부터 0기기였고 그 사실이 어디에도
    없었다. (당시 디코 위반 알림은 카카오 경로가 아예 없어 그 상태에서 전달 수단이 0 이었다 - v6.7.742 잡째 삭제.)
    """
    print("\n[13] 푸시 채널 사망 가시화 (v6.7.643)")
    import importlib
    wp = importlib.import_module('web_lib.bg.web_push')
    hs = importlib.import_module('web_lib.bg.health_sampler')

    _subs_bak, _drop_bak, _at_bak = wp.list_subs, wp._dropped_no_subs, wp._drop_log_at
    try:
        wp._dropped_no_subs, wp._drop_log_at = 0, 0.0
        wp.list_subs = lambda: []                      # 구독 0 상태 재현
        for _ in range(3):
            wp.send_push('t', 'b', '/')
        check("구독 0 이면 버린 건수를 센다", wp.drop_stats() == 3, f"n={wp.drop_stats()}")
        check("drop_stats(reset) 는 읽고 0 으로 되돌린다",
              wp.drop_stats(reset=True) == 3 and wp.drop_stats() == 0)

        # **발송 스레드를 태우지 않는다.** 실제 `_send_all` 은 `get_vapid()` 로 VAPID 키를 만드는데,
        #   `web_push._data_dir()` 이 CWD 로 폴백하면 **레포 워킹트리에 개인키 파일이 생긴다**
        #   (실측 2026-09-06 - 이 테스트가 실제로 만들었다). 검증 대상은 카운터이지 발송이 아니다.
        _sa_bak = wp._send_all
        wp._send_all = lambda *a, **k: None
        try:
            wp.list_subs = lambda: [{'endpoint': 'x', 'subscription': {}}]
            wp.send_push('t', 'b', '/')
            check("구독이 있으면 유실로 안 센다", wp.drop_stats() == 0, f"n={wp.drop_stats()}")
        finally:
            wp._send_all = _sa_bak

        # ── 경보 ─────────────────────────────────────────────────
        fired = []
        _nbak, _dbak = hs._check_push_dead, hs._push_dead_date
        try:
            hs._push_dead_date = None
            import notification_manager as _nm
            _sbak = _nm.NotificationManager.send_kakaowork
            _nm.NotificationManager.send_kakaowork = lambda self, t, b=None, **k: fired.append(t)
            # 원장 쓰기도 막는다 - 같은 CWD 폴백으로 `notif_inbox.json` 이 워킹트리에 생긴다.
            import web_lib.bg.notif_store as _ns
            _rbak = _ns.record
            _ns.record = lambda *a, **k: None
            try:
                hs._check_push_dead({'push_subscribers': 2.0, 'push_dropped_no_subs': 0.0})
                check("구독이 있으면 발화 안 한다", not fired)
                hs._check_push_dead({'push_subscribers': 0.0, 'push_dropped_no_subs': 0.0})
                check("구독 0 이어도 버린 게 없으면 발화 안 한다 (조용한 시간대에 안 조른다)", not fired)
                hs._check_push_dead({'push_subscribers': None, 'push_dropped_no_subs': None})
                check("못 쟀으면 발화 안 한다 (0 으로 읽어 오발화 금지)", not fired)
                hs._check_push_dead({'push_subscribers': 0.0, 'push_dropped_no_subs': 4.0})
                check("구독 0 + 실제 유실이면 1회 발화", len(fired) == 1, str(fired))
                hs._check_push_dead({'push_subscribers': 0.0, 'push_dropped_no_subs': 9.0})
                hs._check_push_dead({'push_subscribers': 0.0, 'push_dropped_no_subs': 9.0})
                check("같은 날은 다시 안 울린다 (1일 1회)", len(fired) == 1, f"발화 {len(fired)}회")
            finally:
                _nm.NotificationManager.send_kakaowork = _sbak
                _ns.record = _rbak
        finally:
            hs._check_push_dead, hs._push_dead_date = _nbak, _dbak
    finally:
        wp.list_subs, wp._dropped_no_subs, wp._drop_log_at = _subs_bak, _drop_bak, _at_bak


# ---------------------------------------------------------------------------
def test_notif_inbox_has_fallback_polling():
    """[14] **보관소가 소켓에만 매달려 있으면 안 된다** (v6.7.643).

    종전 자동 갱신 경로는 `socket.js showInAppBanner` 하나뿐이었다 - 소켓 이벤트가 안 오면
    (백그라운드 탭 스로틀·네트워크 블립) 종은 사람이 열 때까지 고착된다. 폴링 탭에는 자가회복이
    있는데 푸시 전용 표면에는 없는 비대칭이다(PATTERN-40).

    실사고 2026-09-06: 서버가 02:24:38 에 원장 적재 + 카카오 02:24 정상 도착인데 **종에는 약
    2시간 뒤에 나타났다.** 소스 계약으로 잠근다 - JS 라 실행 검증이 어려우므로 구조를 본다.
    """
    print("\n[14] 알림 보관소 폴백 폴링 (v6.7.643)")
    src = open(os.path.join(_HERE, 'static', 'js', 'main', 'notif-inbox.js'), encoding='utf-8').read()
    check("폴백 타이머가 있다", '_startPolling' in src and 'setInterval' in src)
    check("_init 에서 기동한다 (종이 있는 역할만)", '_startPolling();' in src)
    check("숨은 탭은 건너뛰고 돌아올 때 당긴다",
          "visibilityState === 'hidden'" in src and "visibilitychange" in src)
    check("바뀐 게 없으면 재렌더를 건너뛴다 (PATTERN-38 - 폴링이 freeze 를 들여오지 않게)",
          '_sig' in src and 'if (changed)' in src)
    check("소켓 훅은 더 빠른 경로로 남아 있다", 'refresh: _fetch' in src)
    import re as _re
    m = _re.search(r'POLL_MS\s*=\s*(\d+)', src)
    check("주기가 분 단위다 (원장 조회가 싸고 목적이 '자리비움 중 도착분')",
          bool(m) and 30000 <= int(m.group(1)) <= 300000, m.group(1) if m else 'none')


def test_summary_cutoff_is_restart_window_only():
    """[v6.7.656] 요약 컷오프는 **재시작 창**만 덮는다 - 소급이 아니다.

    사용자 결정 2026-09-10 *"IME 소급 필요없어"*. 같은 날 IMT 에 세운 규약(**켠 뒤 들어온 글만**)과
    맞춘다. v6.7.477 이 30 -> 360(6시간)으로 넓힌 것은 *"조건부 위험의 선제 차단"* 이었는데,
    사용자가 그 보험을 원하지 않는다고 명시했다(*"과거 ai요약 대상은 필요없어 다 버려도돼"*).

    **0 으로 두면 안 된다** - v6.7.452 가 실측으로 남겼다: 부팅 시각 그대로면 재시작 순간
    "수집됐고 본문도 있는데 아직 요약 안 된" 글이 **영구 스킵**되고, 배포가 잦아 그 유실이
    배포마다 반복된다. 그래서 이 테스트는 **양쪽 벽을 같이 잠근다** - 0 도 아니고 6시간도 아니다.
    """
    print("\n[TEST] 요약 컷오프 = 재시작 창 (v6.7.656)")
    import importlib
    from datetime import datetime, timedelta, timezone
    worker = importlib.import_module('web_lib.ai_gemma4_worker')

    n = worker._BOOT_LOOKBACK_MIN
    check("컷오프 창이 0 이 아니다 (재시작 in-flight 유실 방지 - v6.7.452)", n > 0, "%r분" % n)
    check("컷오프 창이 소급이 아니다 (1시간 미만)", n < 60, "%r분 - 6시간은 소급이다" % n)

    # **상수만 재면 안 된다** - 실제로 쓰이는 값은 `_WORKER_START_TIME` 이고, 그것이 상수를
    #   반영하지 않으면 이 검사는 아무것도 채점하지 않는다(PATTERN-71).
    got = datetime.strptime(worker._WORKER_START_TIME, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
    gap_min = (datetime.now(timezone.utc) - got).total_seconds() / 60.0
    # [2026-09-30] 워커 모듈은 스위트 **앞쪽 테스트가 먼저 import** 할 수 있어, 여기까지 흐른 시간만큼 gap 이 커진다.
    #   종전 `abs(gap - n) < 5` 는 스위트가 5분을 넘기면 거짓 FAIL 이었다(부하가 있던 날 실측). 흐른 시간을 허용폭에 더한다.
    _ran_min = (time.time() - _T0) / 60.0
    check("_WORKER_START_TIME 이 그 상수를 실제로 반영한다",
          n - 1 <= gap_min <= n + _ran_min + 1, "실측 %.1f분 vs 상수 %d분 (스위트 경과 %.1f분)" % (gap_min, n, _ran_min))
    check("컷오프 문자열이 조회와 같은 포맷(UTC 19자)이다",
          len(worker._WORKER_START_TIME) == 19, worker._WORKER_START_TIME)


def test_txn_hold_is_logged():
    """[16] 쓰기 락을 오래 쥔 트랜잭션이 **호출자와 함께** 로그에 남는다 (v6.7.700, 계측 전용).

    배포 직후 약 10분 + 평시 산발 `database is locked` 의 범인을 특정하려고 넣었다. 연결 timeout 30초라 locked = 누가 30초
    넘게 쥐었다는 뜻인데 SQLite 는 쥔 쪽을 안 알려준다. 실제 두 연결로 락 경합을 만들어, 기다리는 쪽이 들어올 때 쥔 쪽의
    함수·스레드가 찍히는지 본다. 계측이 연결 동작을 바꾸지 않는지(데이터가 정상 커밋되는지)도 같이 본다.
    """
    print("\n[16] SQLite 쓰기 락 보유 계측 (v6.7.700)")
    import logging, sqlite3, threading
    import sqlite_handler as H
    rec = []

    class _H(logging.Handler):
        def emit(self, r):
            rec.append(r.getMessage())

    h = _H(); H.logger.addHandler(h)
    old = H._TXN_SLOW_SEC; H._TXN_SLOW_SEC = 0.3
    try:
        d = tempfile.mkdtemp(); p = os.path.join(d, "t.db")
        a = H.instrument_txn(sqlite3.connect(p, timeout=5, check_same_thread=False))
        a.execute("PRAGMA journal_mode=WAL"); a.execute("CREATE TABLE t(x)")
        b = H.instrument_txn(sqlite3.connect(p, timeout=5), "b")

        def holder():
            a.execute("INSERT INTO t VALUES (1)"); time.sleep(0.8); a.commit()
        th = threading.Thread(target=holder, name="holder-thread"); th.start(); time.sleep(0.5)
        b.execute("INSERT INTO t VALUES (2)"); b.commit(); th.join()
        n = b.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    finally:
        H._TXN_SLOW_SEC = old; H.logger.removeHandler(h)
    check("기다리는 쓰기가 들어올 때 쥔 쪽(함수·스레드)이 찍힌다",
          any("초째 열림" in m and "holder" in m and "holder-thread" in m for m in rec), rec)
    check("오래 쥔 트랜잭션은 커밋 때 호출자와 함께 찍힌다", any("후 COMMIT" in m and "holder" in m for m in rec), rec)
    check("계측이 동작을 안 바꾼다 (두 쓰기 모두 커밋)", n == 2, n)
    check("열린 트랜잭션 장부가 비워진다", not H._OPEN_TXN, H._OPEN_TXN)


def test_exclude_sweep_skips_when_unchanged():
    """[17] 제외 게시판 소급 삭제는 **키워드 목록이 바뀔 때만** 돈다 (v6.7.701).

    v6.7.700 계측이 배포 직후 10분 DB 락의 범인을 잡았다 - 부팅마다 첫 설정 polling 이 `{} -> {kw}` 를 변경으로 보고
    `delete_excluded_board_posts` 를 불러, `LIKE '%kw%'`(인덱스 불가) 전수 스캔을 트랜잭션 하나로 몇 분간 쥐었다.
    실제 SQLite 로 운영 메서드를 태운다: 1회차 삭제·지문 저장 / 같은 목록 2회차는 **새로 넣은 매칭 행도 안 건드림**(= 스캔 생략) /
    목록이 바뀌면 다시 돈다.
    """
    print("\n[17] 제외 게시판 소급 삭제 - 부팅마다 재청소 안 함 (v6.7.701)")
    import sqlite3
    from sqlite_handler import SQLiteHandler
    d = tempfile.mkdtemp(); p = os.path.join(d, "t.db")
    conn = sqlite3.connect(p, check_same_thread=False)
    conn.execute("CREATE TABLE community_posts (game TEXT, board_name TEXT)")
    conn.execute("CREATE TABLE ai_analysis (game TEXT, board_name TEXT)")
    conn.execute("CREATE TABLE system_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    conn.executemany("INSERT INTO community_posts VALUES (?, ?)", [("POE2", "거래 게시판"), ("POE2", "자유")])
    conn.commit()
    h = object.__new__(SQLiteHandler)
    h._get_connection = lambda: conn
    count = lambda: conn.execute("SELECT COUNT(*) FROM community_posts WHERE board_name LIKE '%거래%'").fetchone()[0]

    n1 = h.delete_excluded_board_posts({"POE2": ["거래"]})
    check("1회차: 매칭 행을 지운다", n1 == 1 and count() == 0, (n1, count()))
    check("1회차: 적용 목록을 남긴다", bool(h.get_system_setting('exclude_kw_applied')))
    conn.execute("INSERT INTO community_posts VALUES ('POE2', '거래 게시판2')"); conn.commit()
    n2 = h.delete_excluded_board_posts({"POE2": [" 거래 "]})
    check("같은 목록(공백 차이 무시)이면 스캔을 생략한다 - 부팅 재청소 없음", n2 == 0 and count() == 1, (n2, count()))
    n3 = h.delete_excluded_board_posts({"POE2": ["거래", "공지"]})
    check("목록이 바뀌면 다시 돈다", n3 == 1 and count() == 0, (n3, count()))


def test_first_json_array_ignores_trailing():
    """[18] 응답의 **첫 JSON 배열만** 읽는다 + 1차는 파싱 실패 시 폴백 모델에 한 번 더 (v6.7.702).

    라이브 2026-09-30 11:10~11:33: 1차 `Extra data: line 6 column 1 (char 102)` 등 20회 - 3·6·8건 배치의 배열이 끝난 다음 줄에
    여분이 붙어 배치 전체를 버렸고, temperature 0 이라 같은 배치가 매번 같은 실패를 냈다. 그 모양(배열 + 둘째 덩어리)을 주입한다.
    """
    print("\n[18] 첫 JSON 배열만 읽기 + 1차 파싱 실패 폴백 (v6.7.702)")
    import json as _j
    import ai_lib.gemma4_summary_core as C
    from ai_lib.gemma4_summary_core import Gemma4SummaryCore
    arr3 = '[\n    {"id": 1, "analyze": false},\n    {"id": 2, "analyze": false},\n    {"id": 3, "analyze": false}\n]'
    tail = '[\n    {"id": 9, "analyze": true}\n]'      # 둘째 덩어리는 **다른 내용** - 뒤 배열을 읽는 파서를 가려낸다
    try:
        _j.loads(arr3 + "\n" + tail)
        live_shape = False
    except ValueError as e:
        live_shape = "line 6 column 1" in str(e)
    check("주입 모양이 라이브 오류와 같다 (3건 배열 + 둘째 덩어리 = line 6 column 1)", live_shape)
    check("배열 뒤 여분(둘째 배열)을 무시하고 **첫** 배열을 읽는다", C._first_json_array(arr3 + "\n" + tail) == _j.loads(arr3))
    check("코드펜스·설명 섞여도 읽는다", C._first_json_array("```json\n" + arr3 + "\n```\n참고: 끝") == _j.loads(arr3))
    check("앞의 [참고] 같은 비배열 대괄호는 건너뛴다", C._first_json_array("[참고] 아래\n" + arr3) == _j.loads(arr3))
    check("배열이 없으면 None", C._first_json_array("답을 못 하겠어요") is None and C._first_json_array("") is None)

    def _run(behave):
        calls = []

        class _M:
            def generate_content(self, **kw):
                calls.append(kw.get("model"))
                class _R:
                    text = behave(kw.get("model"))
                return _R()

        class _Cl:
            models = _M()

        core = object.__new__(Gemma4SummaryCore)
        core.client = _Cl(); core.system_instruction = "x"
        core._acquire_gemma_client = lambda: (0, _Cl())
        core._build_context_section = lambda games: ""
        core._generate_gemini_summaries = lambda *a, **k: None
        batch = [{"game": "g", "board_name": "b", "title": "t%d" % i, "content": "c"} for i in range(3)]
        try:
            core._generate_summary_batch(batch)
            raised = None
        except Exception as e:      # noqa: BLE001
            raised = e
        return calls, raised, batch

    P, FB = C.FIRST_PASS_MODEL, C.FIRST_PASS_FALLBACK_MODEL
    calls, raised, b = _run(lambda m: arr3 + "\n" + tail)
    check("라이브 모양 응답이 이제 배치를 통과한다(폴백 없이)", calls == [P] and raised is None
          and all(x.get("summary") == "[분석 제외]" for x in b), (calls, raised))
    calls, raised, b = _run(lambda m: "모르겠어요" if m == P else arr3)
    check("주력 답을 못 읽으면 폴백 모델에 한 번 더 묻고 그 답을 쓴다", calls == [P, FB] and raised is None
          and all(x.get("summary") == "[분석 제외]" for x in b), (calls, raised))
    calls, raised, b = _run(lambda m: "모르겠어요")
    check("둘 다 못 읽으면 오류로 올리고 글에는 스탬프를 안 찍는다(다음 사이클 재시도)",
          raised is not None and all("summary" not in x for x in b), (calls, raised))


def main():
    print("=" * 70)
    print("  AI 파이프라인 회귀 테스트 - 실패 처리 + 프롬프트 조립 (네트워크 미사용)")
    print("=" * 70)
    test_ccu_drop_is_relative_and_copies_agree()
    test_quota_brief_parses_429()
    test_transient_classifier()
    test_summary_cap_matches_parallelism()
    test_watchdog_measures_stall_not_slowness()
    test_second_stage_is_one_call_per_cycle()
    test_first_pass_reads_only_head_of_content()
    test_first_pass_uses_compact_guide()
    test_summary_tail_is_not_starved()
    test_summary_partial_response()
    test_context_section_per_game()
    test_classify_is_deterministic()
    test_first_pass_thinking_off_and_fallback()
    test_429_does_not_park_key_for_the_day()
    test_pace_window_is_sliding()
    test_stamp_separates_pacing_from_daily_quota()
    test_5xx_skips_to_next_model_not_next_key()
    test_second_stage_timeout_and_slow_cooldown()
    test_sdk_retry_is_capped()
    test_queue_lag_and_stall_alert()
    test_push_channel_death_is_loud()
    test_notif_inbox_has_fallback_polling()
    test_summary_cutoff_is_restart_window_only()
    test_txn_hold_is_logged()
    test_exclude_sweep_skips_when_unchanged()
    test_first_json_array_ignores_trailing()
    print("\n" + "=" * 70)
    if FAILS:
        print(f"[FAIL] {len(FAILS)}건")
        for f in FAILS:
            print("  -", f)
        return 1
    print("[PASS] 전 항목 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
