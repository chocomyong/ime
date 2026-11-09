# -*- coding: utf-8 -*-
"""
Gemma4 + Gemini 동향 요약 코어 (Flask 웹앱 내부 전용)

[설계 의도]
- `ai_processor_gemma4.py`의 핵심 함수를 Flask 전용 경량 사본으로 재패키징한다.
- 독립 데몬(`ai_processor_gemma4.py`)은 별도 빌드로 계속 운영되며, 본 모듈은 Flask 프로세스 안에서만 사용된다.
- Selenium 드라이버, 카카오워크 알림, 일일 시트 순회 루프는 제외한다. (본문은 content_extractor가 이미 SQLite에 채운다)

[파이프라인 (v6.7.63~)]
1차 분류 - Gemma4: 글마다 "Gemini 가 요약할 가치 있는가" yes/no 만 판단.
                v6.7.698~ 주력 `gemma-4-26b-a4b-it`(thinking 끔), 26b 가 5xx/429 면 `gemma-4-31b-it` 폴백
                (되돌리기 `IMT_FIRST_PASS_PRIMARY=31b`, 아래 v6.7.697·698 주석).
                응답 포맷 `{"id": N, "analyze": true|false}` (legacy "분석 제외" 문자열도 false 로 호환).
2차 요약 - **모델 체인** (v6.7.600~ 동적 확정, `resolve_chain()`): Gemma4 가 analyze=true 분류한
                글만 호출하여 `summary` + `sentiment` + `severity` 정식 생성. SQLite `ai_analysis`
                저장은 Gemini 결과. 체인은 매일 1회 `models.list` 로 **살아있는 정식 flash 최신
                `CHAIN_LEN` 개**를 확정한다(구 하드코딩 `GEMINI_CHAIN` 대체, v6.7.680 에 3 -> 5).
                앞 모델의 **전 키를 소진해야** 다음 모델로 내려간다(동일 키 풀에서 모델만 교체).
                RPD 카운터는 체인 라벨별로 분리 (`gemini_counts[label][api_key]`, KST 자정 reset).
                이력: v6.7.173(2026-05-30) 3.5 를 solare 전용으로 양보하며 3-flash-preview 로 강등 →
                v6.7.446(2026-07-28) **유보 해제**(solare 가 API 미사용) + preview 대신 정식 3.6 을 head 로
                → v6.7.600(2026-08-22) 하드코딩이 늙는 문제로 동적 발견 전환.

[사용처]
- `web_lib/ai_gemma4_worker.py`의 APScheduler 잡이 이 클래스의 인스턴스를 싱글턴으로 유지하면서 배치 요약을 수행한다.
"""
import os
import sys
import re
import time
import json
import hashlib
import logging
import datetime
import threading
from zoneinfo import ZoneInfo  # [Gemini 3.1 test] KST 자정 reset 용

logger = logging.getLogger(__name__)

if getattr(sys, 'frozen', False):
    _BASE_DIR = sys._MEIPASS
else:
    _BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.append(os.path.join(_BASE_DIR, 'utils'))

try:
    from google import genai as google_genai
    _HAS_GENAI = True
except ImportError:
    _HAS_GENAI = False
    logger.warning("[Gemma4Core] google-genai SDK 미설치 — 요약 기능 비활성")

# [v6.7.620] SDK 재시도 상한은 `ai_lib/genai_client.py` 가 정본이다 - 웹앱 안에만 클라이언트
#   생성부가 여럿이라(요약 코어 · account_trade) 여기에 두면 복제가 늘고
#   각자 다른 속도로 늙는다. 근거·실측·재확인 명령은 그 모듈 docstring.
from ai_lib.genai_client import make_client as _make_client, SDK_RETRY_ATTEMPTS as _SDK_RETRY_ATTEMPTS

from google_sheets_handler import get_handler


GEMMA_MODEL = 'gemma-4-31b-it'
BATCH_SIZE = 10

# [v6.7.616] **1차 분류는 결정적으로.** PATTERN-49 방지 (1) 을 IME 에 적용한다 - 그 패턴이
#   "인접 미측정" 으로 지목했다가 2026-08-12 에 실재를 확인하고도 `라이브 회귀 위험 때문에
#   별건` 으로 남겨 둔 자리가 정확히 아래 1차 호출이다. AME 는 같은 모델에 이미 걸었다
#   (`jira_register/feed_ai.py` `CLASSIFY_TEMPERATURE`, `[AME-C v0.1.15]`).
#   근거(AME 실측): 같은 10건을 3회 판정했더니 요약 대상이 **1건 -> 2건 -> 3건**(뒤집힘 2/10).
#   temperature 0 에서 뒤집힘 1/10 로 준다. **0 이 되지는 않는다** - gemma 의 thinking 이 남은
#   비결정성으로 보였다. [v6.7.697 정정] `thinking_budget=0` 은 400 이지만 `thinking_level="minimal"`
#   로는 꺼진다 - 아래 `FIRST_PASS_THINKING`.
#   그래도 확률적으로 틀리는 것보다 결정적으로 틀리는 편이 낫다 - 재현되니 고칠 수 있다.
#   **2차 요약(`_call_gemini_chain`)에는 걸지 않는다** - 판정이 아니라 생성 과제이고,
#   "판정 경로만 별도로 지정하라" 가 그 패턴의 지시다.
#   회귀 위험(모델이 이 파라미터를 거부하면 1차가 통째로 죽는다)은 **적용 전에 라이브에서
#   해소했다** - 2026-08-27 `imt-container` 에서 이 SDK 경로로 `config` 없음/`temperature=0`
#   양쪽을 실제 호출해 둘 다 200 을 확인했다.
CLASSIFY_TEMPERATURE = 0

# [v6.7.697] **1차 분류는 thinking 을 끈다 + 31b 가 실패하면 26b(thinking 끔)로 한 번 더 묻는다.**
#   근거 = `_workspace/2026-09-27_gemma4_26b_재시험.md`.
#   - 라이브 2026-09-28 08:19~08:35: 1차 콜이 1~4건에 **42~83초**이고 사이클 시간이 곧 1차 시간이었다
#     (2차는 사이클당 1콜). 같은 창에 `500 INTERNAL` 로 배치 통째 실패가 13사이클 중 3회, 429 는 0회.
#   - 1차 10건 채점(커뮤니티 글, 09-28): thinking 을 꺼도 **라벨이 한 칸도 안 바뀌었다**(31b 켬=끔,
#     26b 켬=끔). 시간만 31b 78->28초 · 26b 43->4초.
#   - 폴백 26b 는 **반드시 thinking 끔**이다. 26b + thinking 켬 + temperature 0 은 thinking 이
#     출력 상한을 다 태우고 답변 0자로 끝난다(두 앱 0/8, 09-27 재현 348초). 끄면 정상 종료.
#   - 폴백 조건: **5xx(모델 축)는 즉시**, 429(키 축)는 기존 키 교체를 다 쓴 마지막 시도에서만.
#     26b 까지 실패하거나 26b 답을 못 읽으면 **31b 의 원래 오류를 올린다**(PATTERN-83 - 폴백이
#     1차 실패의 축을 지우면 안 된다). 답 못 받은 글은 v6.7.610 대로 미판정 = 다음 사이클 재시도.
#   되돌리기 = `IMT_FIRST_PASS_THINKING=default` + **컨테이너 recreate**(모듈 로드 시 1회 평가).
#   이 값은 31b 에만 듣는다 - 26b 는 위 사유로 항상 끈다.
#
# [v6.7.698] **주력과 폴백을 뒤집는다 - 주력 26b(thinking 끔), 26b 가 5xx/429 면 31b(thinking 끔).**
#   - 1차 재현율 20건(09-28, 커뮤니티 글 2회차): 26b 7/10 = 31b 7/10, 오통과 각 1/10 - 26b 가 못하다는 근거 없음.
#   - 속도: 26b 10건 4초 vs 31b 41초. v6.7.697 라이브에서 31b 는 thinking 을 꺼도 20~70초였고(엔드포인트 지연),
#     500 을 받은 26b 폴백은 2~4초였다 - 남은 1차 병목이 31b 자체였다.
#   되돌리기 = `IMT_FIRST_PASS_PRIMARY=31b` + 컨테이너 recreate(v6.7.697 배치로 돌아간다).
#   **`GEMMA_MODEL`(31b)은 그대로다** - 게시판 판정(`bg/post_judge`)이 그 상수를 쓴다(디코 판정은 v6.7.742 삭제). 1차만 아래 둘을 본다.
FIRST_PASS_THINKING = (os.environ.get('IMT_FIRST_PASS_THINKING') or 'minimal').strip().lower()
_FP_26B = 'gemma-4-26b-a4b-it'
if (os.environ.get('IMT_FIRST_PASS_PRIMARY') or '26b').strip().lower() == '31b':
    FIRST_PASS_MODEL, FIRST_PASS_FALLBACK_MODEL = GEMMA_MODEL, _FP_26B
else:
    FIRST_PASS_MODEL, FIRST_PASS_FALLBACK_MODEL = _FP_26B, GEMMA_MODEL


_TRAIL_LOGGED = set()


def _first_json_array(text, where=""):
    """[v6.7.702] 응답에서 **첫 JSON 배열만** 읽는다. 없거나 못 읽으면 None. 배열 뒤 여분은 무시한다(자리별 1회 로그).

    종전 파서는 모두 "첫 `[` 부터 **마지막** `]` 까지" 를 통째로 `json.loads` 했다. 라이브 2026-09-30 11:10~11:33 에 1차가
    `Extra data: line 6 column 1 (char 102)` 등으로 20회 실패 - 오류 위치를 역산하면 3·6·8건 배치의 배열이 정확히 끝난 다음 줄에
    **뭔가가 더 붙어** 있었다(26b thinking 끔). 배열 자체는 온전한데 뒤꼬리 때문에 배치 전체를 버렸고, temperature 0 이라 같은 배치는
    매번 같은 응답 = 같은 실패였다. `raw_decode` 로 첫 배열이 끝나는 자리에서 멈춘다. 앞쪽 `[` 가 배열이 아니면(예: `[참고]`) 다음 `[` 로.
    """
    if not text:
        return None
    t = text.replace('-Infinity', 'null').replace('Infinity', 'null').replace('NaN', 'null')
    dec = json.JSONDecoder()
    i, tries = t.find('['), 0
    while i != -1 and tries < 8:
        try:
            v, end = dec.raw_decode(t, i)
        except ValueError:
            v = None
        if isinstance(v, list):
            rest = t[end:].strip().strip('`').strip()
            if rest and where not in _TRAIL_LOGGED:
                _TRAIL_LOGGED.add(where)
                logger.info(f"[Gemma4Core] {where} 응답 뒤 여분을 무시하고 첫 배열만 읽음: {rest[:160]!r}")
            return v
        i, tries = t.find('[', i + 1), tries + 1
    return None


def _first_pass_config(model):
    """1차 분류 호출 config. 26b 는 항상 thinking 끔, 31b 는 `FIRST_PASS_THINKING` 을 따른다."""
    cfg = {'temperature': CLASSIFY_TEMPERATURE}
    if model == _FP_26B or FIRST_PASS_THINKING == 'minimal':
        cfg['thinking_config'] = {'thinking_level': 'minimal'}
    return cfg

# [v6.7.612] 프롬프트에 실을 '최근 동향' 창. 근거는 `_build_context_section` docstring.
#   **AME `feed_ai.py` 와 같은 값·같은 문자열을 유지할 것** - 두 앱을 나란히 비교 중이다.
#
# **자정 리셋이 아니라 롤링 24시간이다** (사용자 요청 "계속 컨텍스트가 쌓이는 개념으로").
#   '금일' 로 자르면 00:05 의 맥락은 5분치 = 사실상 0 이고, 그건 v6.7.611 이 고치려던 상태와
#   같은 형태다(맥락 없는 판정). 롤링이면 절벽이 없고 양이 하루 내내 고르다.
_CONTEXT_WINDOW_H = 24
# 게임당 상한. AME 요약 시트 8일 실측(2026-08-18~25) 게임별 일 최대 = 배그 86 / POE1 44 /
#   POE2 12. 60 이면 평상시에는 걸리지 않고 **폭주일에만** 잘라 프롬프트 폭주를 막는다
#   (PATTERN-43 의 방향: 유입이 늘수록 비용이 비례해 커지는 구조를 만들지 않는다).
_CONTEXT_MAX_PER_GAME = 60
# 창 안이 이만큼도 안 되면 **창 밖 과거로 채운다**. 조용한 게임과 기동 직후가 그 경우이고,
#   거기서 블록이 비면 정확히 v6.7.611 이전 상태로 돌아간다.
_CONTEXT_MIN_PER_GAME = 3
# [v6.7.613] **머리말을 반전했다.** 구 문구는 `(같은 게임 안에서 중복/유사 내용 통합 시 참고)`
#   였는데 '통합' 은 억제 방향이고, 운영에서는 **같은 사안의 반복 제보가 곧 사안 확대 신호**다
#   (사용자 2026-08-26). **문구를 덧붙이는 방식은 효과 0 으로 측정돼 제거했다** - 아래 주석.
_CONTEXT_HEADER = "[참고: 최근 24시간 이 게임에서 관측된 동향]"
# [v6.7.615] **1차에 붙였던 "유사해도 true" 지시는 제거했다 - 효과가 정확히 0 이었다.**
#   재측정(2026-08-26, 누출 차단, temperature 0, 노이즈 바닥 0):
#       맥락 없음 14/28(50.0%) / 머리말·표기만 22/28 / 거기에 지시 추가 22/28
#   지시 유무로 뒤집힌 글이 **0건**이다. 매 1차 콜마다 토큰만 쓰는 문구였다.
#   [v6.7.616 추기] 이 측정은 `temperature 0` 에서 했는데 **당시 운영은 기본값이었다** - 즉
#   운영이 들어가지 않는 레짐에서 잰 것이었다(PATTERN-49 의 "채점 조건이 운영 조건과 다르면
#   결함이 채점에서 안 보인다"). v6.7.616 이 1차를 `temperature 0` 으로 옮겨 **그 간극이 닫혔고**,
#   위 숫자는 이제 운영과 같은 조건의 값이다. 결론(지시 제거)은 바뀌지 않는다.
#   **방향은 문구로 부탁하는 것이 아니라 머리말과 데이터(severity 표기)로 준다** - PATTERN-50.
_SEV_KEEP_RANK = {"critical": 0, "warning": 1}
# `recent_summaries` 보관 상한. 24시간 최대 실측(라이브 전체 718건/일, `bg/trend_digest.py`
#   MAX_ROWS 주석)의 4배. 창 필터가 실질 상한이고 이 값은 메모리 폭주 방어일 뿐이다.
_RECENT_CAP = 3000

# ── [v6.7.454] 1차 분류 배치 병렬도 ──────────────────────────────────────────────
#   왜 병렬화인가 (측정 근거: _workspace/2026-07-28_ai_summary_capacity_model.md):
#   1차 콜 소요는 T(n) = 10.7s + 3.58s x n (실측 28표본, R^2 0.867). 배치 10 기준 **고정비는 23%**
#   뿐이고 77%가 건당 한계비용이라, 배치를 무한대로 키워도 순차 상한은 60/3.58 = **16.8건/분**이다.
#   그런데 피크(19시 KST) 실효 유입이 12.6건/분이고 현행 순차 처리량이 12.9건/분 - **가동률 0.98**.
#   대기행렬은 rho 가 1에 가까우면 지연이 폭증하므로(p50 1.4분인데 p90 4.4분), BATCH_SIZE/사이클 캡/
#   주기 조정은 **어느 것도 rho 를 0.7 아래로 못 내린다**. 기울기를 건드리는 유일한 수단이 병렬화다.
#   2 병렬 = 25.8건/분 → rho 0.49 (수집량이 늘어도 여유가 남는다).
#
#   왜 CPU 를 안 잡아먹나: 이 콜은 원격 API 대기(I/O)라 스레드가 GIL 을 놓고 잠든다. Selenium/Chrome
#   다중 기동으로 2코어 NAS 를 포화시켰던 PATTERN-31 과는 부하 성격이 다르다.
#
#   [v6.7.652] **2 -> 4.** v6.7.454 가 "보수적 시작 ... 필요하면 env 로 올린다" 로 남긴 자리를
#   실측으로 연다. 근거 셋:
#     (a) **직렬 구간이 9% 뿐이다.** 2차 요약은 `_gemini_lock` 으로 직렬화돼 있어 그게 천장이면
#         병렬도를 올려도 안 듣는데, 라이브 실측 2026-09-09 15:14~15:32(5 사이클)에서
#         `사이클 완료` 대비 2차 구간이 **평균 9%** 였다. 즉 91% 가 병렬 가능 구간이다.
#         암달로 P2 대비 P4 는 (0.09+0.91/2)/(0.09+0.91/4) = 약 1.7배. 웨이브 충전까지
#         합친 **실효 2.32배**(2차가 건수에 비례해 같이 느는 것까지 반영 - CHANGELOG v6.7.652).
#         **되돌리기는 env + 컨테이너 recreate** 다 - 이 상수는 모듈 로드 시 1회 평가된다.
#     (b) **키가 병목이 아니다.** 키 6개 라운드로빈이라 P4 여도 키당 동시성은 1 미만이고,
#         `_GEMMA_KEY_MIN_INTERVAL`(5초) 은 콜이 46초라 애초에 발화하지 않는다(아래 주석).
#     (c) **CPU 도 아니다.** 원격 API 대기라 GIL 을 놓고 잔다(위 문단).
#   재측정: `imt-diag logs webapp` 의 `사이클 완료 N건 / 배치 M개 / 병렬 P / X.Xs`.
#   되돌리려면 env `IMT_GEMMA_BATCH_PARALLEL=2` - 코드 배포 없이 된다.
BATCH_PARALLEL = max(1, int(os.environ.get('IMT_GEMMA_BATCH_PARALLEL', '4')))

# 같은 키를 연속으로 두드리지 않기 위한 최소 간격(초). [v6.7.454] 전역 1개 → **키별**로 분리.
#   구 구현은 `last_gemma_call_time` 전역 하나라, 병렬 배치 2개가 동시에 출발하면 뒤엣놈이 5초를
#   자게 되어 **병렬화를 스스로 되돌린다**. 키별로 재면 원 의도(한 키 연타 방지)는 그대로 유지되고
#   서로 다른 키를 쓰는 병렬 배치는 대기 없이 나간다.
#   참고: 현행 부하에서 이 스로틀은 실제로 발화하지 않는다 - 콜 자체가 13~53초라 같은 키가 다시
#   불리기까지 최소 (키수 x 콜소요) 가 지나기 때문. 저부하 구간용 가드로만 남는다.
_GEMMA_KEY_MIN_INTERVAL = 5.0

# ── 프롬프트에 싣는 본문 길이 - **1차와 2차는 다른 상수다** ───────────────────────────
# [v6.7.681] 1차 분류가 읽는 본문 길이 (구 800). 1차의 일은 "요약할 가치가 있나" 판정뿐이고
#   요약문을 쓰는 것은 2차다 - 그 판정 신호는 제목과 본문 앞머리에 거의 다 있다(사용자 결정
#   2026-09-19).
#
# [v6.7.682] **v6.7.681 은 이 상수를 2차 호출부(`_generate_gemini_summaries_impl`)에 걸었다.**
#   겨냥한 곳은 1차(`_generate_summary_batch`)였는데 그 자리는 `[:800]` 그대로였다. 그래서
#   ① 1차 입력은 1토큰도 안 줄었고 ② **2차 요약이 읽는 본문만 800 -> 200 으로 조용히 깎였다**
#   (요약문을 쓰는 쪽이라 품질에 바로 닿는다). 커밋 본문의 "2차 요약 경로 무변경" 은 반대였다.
#   테스트 [3g] 가 통과한 이유: `"[:_FIRST_PASS_CONTENT_CHARS]" in src` 로 **파일 전체를
#   문자열 검색**해 어느 메서드에 걸렸는지 구별하지 못했다. 지금은 메서드 본문을 뜯어 본다.
#
# **그리고 줄여도 얼마 안 줄어든다 - 콜당 입력의 주범은 본문이 아니다.**
#   라이브 실측 2026-09-20 (`community_posts`, 09-19 유입 3,000건 표본):
#     본문 길이 분포 = 1~50자 75.2% / 51~200자 20.2% / 201~800자 4.0% / 800자 초과 0.6%
#     평균 = 원본 59.4자 · 800컷 52.3자 · 200컷 42.7자  (**200자 넘는 글이 4.6% 뿐**)
#   배치 10건이면 기사 몫이 약 523자 -> 427자, 즉 **콜당 100자쯤** 줄어든다. v6.7.681 이 적은
#   "8,000자 -> 2,000자" 는 모든 글이 800자를 꽉 채운다는 전제였고 실제 분포가 아니다.
#   재측정:
#     scripts/imt-diag-py.sh webapp - <<'PY'
#     import sqlite3
#     c = sqlite3.connect("file:/app/data/crawled_data.sqlite?mode=ro", uri=True, timeout=20)
#     n=a=b=0
#     for (s,) in c.execute("SELECT content FROM community_posts ORDER BY inserted_at DESC LIMIT 3000"):
#         s = s or ''; n+=1; a+=min(len(s),800); b+=min(len(s),200)
#     print(n, a/n, b/n)
#     PY
#
#   콜당 입력 약 1만 토큰의 출처는 **A2 공통 지침(약 13,807자)** 이다(`_build_context_section`
#   docstring 의 비용 실측과 같은 숫자). 429(`키당·모델당 16,000 토큰/분`, 2026-09-18 라이브:
#   key#0~#5 전부 `retryDelay=25~51s`)를 본문 컷으로 푸는 것은 3% 를 깎는 일이다 - **줄이려면
#   A2 를 줄이거나 1차를 프롬프트 없는 모델로 옮겨야 한다**(후자 검토: `_workspace/2026-09-20_jev_1차분류_검토.md`).
#   **모델 용량 문제는 아니다** - 입력 한계는 262,144 토큰이라 전후 모두 2~4% 구간이다.
_FIRST_PASS_CONTENT_CHARS = 200

# ── [v6.7.714] 1차 로컬 모드 - imt-fp(로컬 인코더) 점수로 1차를 대신한다 ────────────────────────
#   근거·결정: `_workspace/2026-09-29_판정탭_그림자운영_제안.md` §15-1(가린 판정자 재현율 = gemma 1차) · §17(결정) ·
#   §16-1(목적 = 데이터 - 무료 키 사용분은 구글 학습 대상, 1차를 로컬로 두면 구글로 나가는 글 100% -> 12~16%).
#   모드: 'gemma' = 종전(전 글 gemma 1차) / 'local' = 점수 >= HI 는 바로 2차, 나머지 제외 /
#         'local_cascade' = 그 사이(LO <= p < HI)만 gemma 1차에 묻는다(§14 판정 PASS 일 때 켠다).
#   **imt-fp 가 실패·지연·이상 응답이면 그 배치는 gemma 1차로 간다** - 요약이 멈추지 않게(§17-2 설계 조건).
#   로컬이 제외한 글은 `LOCAL_EXCLUDED_SENTINEL` 로 남긴다 - `[분석 제외] <꼬리>` 형은 판정 탭(fp_scorer.gemma_class)과
#   재학습 추출(first_pass_clf prepare)이 **gemma 판정이 아님**으로 거른다 = 모델 자기 판정이 다음 학습 라벨로 되먹지 않는다.
#   되돌리기 = `IMT_FIRST_PASS_MODE=gemma` + 컨테이너 recreate(모듈 로드 시 1회 평가).
FIRST_PASS_MODE = (os.environ.get('IMT_FIRST_PASS_MODE') or 'gemma').strip().lower()
if FIRST_PASS_MODE not in ('gemma', 'local', 'local_cascade'):
    FIRST_PASS_MODE = 'gemma'
FP_SCORE_URL = (os.environ.get('FP_SCORE_URL') or 'http://fp-scorer:8765').rstrip('/') + '/score'
LOCAL_FIRST_HI = float(os.environ.get('IMT_FP_HI') or 0.5)      # 1배 문턱(§15 - 통과율이 gemma 1차와 같은 자리)
LOCAL_FIRST_LO = float(os.environ.get('IMT_FP_LO') or 0.1)      # 구간 위임 하한(§14 사전등록값)
LOCAL_FIRST_TIMEOUT_S = float(os.environ.get('IMT_FP_TIMEOUT_S') or 120)   # imt-fp 직렬 대기 90초 + 채점 - 넘으면 gemma 로
LOCAL_EXCLUDED_SENTINEL = '[분석 제외] 로컬 1차'

# [v6.7.687] **1차는 A2 전문 대신 1차 전용 지침을 읽는다.** 1차가 답하는 것은 글마다 예/아니오
#   하나("요약할 가치가 있나")인데 입력의 약 90% 가 A2(13,807자) 였고, 그 대부분(페르소나 · 용어 정제 ·
#   요약문 형식 · 감성/심각도 · 예시)은 2차가 쓰는 규칙이다. A2 의 classification_guidelines ·
#   community_context_rules · exclusion_criteria 만 남겨 607자로 줄였다. **2차는 A2 그대로다.**
#   채점(2026-09-24, IMT 전용 키 · gemma-4-31b · temperature 0 · 배치 10 · 본문 200컷 · 맥락 블록 제외):
#     표본1 최근 2일 층화 100(IME 통과 30/탈락 70): 공통 90건 A2 vs 이 지침 일치 88/90,
#       **A2 통과 21건 전부 이 지침도 통과**, 이 지침만 더 통과한 2건은 IME 운영 판정도 통과.
#       노이즈 바닥(A2 재실행) 80/80. 콜당 프롬프트 15,234자 -> 2,035자.
#     표본2 배그 제외 7일 층화 60(우마 36·POE2 16·POE1 5·오딘 3): 공통 50건 일치 47/50. A2 만 통과한 2건은
#       IME 운영 판정도 탈락(POE 빌드·탐험 잡담), 이 지침만 통과한 1건은 질문 글(IME 탈락).
#     **두 표본 140건에서 IME 통과글을 A2 는 잡고 이 지침만 놓친 경우 0건.**
#     한계: 채점은 맥락 블록 없이 했다(운영은 붙는다) - 배포 후 통과율 그래프로 전후를 본다.
#   재측정: `jira_register/first_pass_prompt_eval.py <sample.json> <out.json> --repeat-a2`
#   (이 상수를 AST 로 읽는다 - 사본을 두지 않는다). 사용자 기준: gemma 보다 **더 잡는 쪽**이 낫다.
#   **A2 를 고칠 때 이 지침도 같이 볼 것** - A2 의 수집/제외 기준이 바뀌면 1차가 옛 기준으로 거른다.
_FIRST_PASS_GUIDE = """게임 운영 모니터링 팀이 요약해서 볼 가치가 있는 글인가?
[수집] 1) 버그·오류·서버 중단·무한 로딩·접속 종료·결제 오류 등 현상 제보 2) 시스템·콘텐츠·보상·BM·패치·대회·운영 대응의 불합리함을 구체적 근거와 함께 지적하는 불만 3) 구체적 개선안이 있는 건의 4) 점검·업데이트 등 공식 공지 5) 구체적 근거가 서술된 긍정 평가 6) 핵·부정행위·집단행동·법적 대응(공정위·게임위) 예고 7) 우마무스메 원본마(실제 말)의 사망·부상 소식.
[제외] 질문·인사·잡담·유저 간 문답 / 미출시 콘텐츠 추측·기대·떡밥 / 뽑기·강화·매칭 등 운에 따른 결과 보고나 불운 호소 / 아이템·계정 거래, 파티·길드 모집 / 이미지·링크만 있어 텍스트로 파악 불가한 글 / 고유 명사 없이 욕설·비속어·밈·어그로로만 된 감정 배설 / 'ㅋㅋ'·이모지·초성뿐인 글 / '갓겜' 류 단순 칭찬 / 개인 소망 / 특정 유저·스트리머 저격이나 인플루언서 잡담 / 타 게임 비교·분탕 / 커뮤니티(디시·카페) 자체 사건이나 사이트 서버 오류.
디시인사이드는 근거가 있으면 수집, 배설이면 제외한다. 단 우마무스메 갤러리는 서버 마비·결제 오류·원본마 소식만 수집하고 나머지는 전부 제외한다."""

# 2차 요약이 읽는 본문 길이. 여기서 요약문이 나오므로 **1차와 같이 깎지 않는다**(v6.7.682 로
#   v6.7.681 이 잘못 깎은 200 에서 원래 값으로 복원). 이 값을 줄이면 429 는 그대로인 채
#   요약 품질만 떨어진다 - 2차는 사이클당 1콜이라 분당 입력 토큰에 거의 기여하지 않는다.
_SECOND_PASS_CONTENT_CHARS = 800

# [이력] v6.7.65 → v6.7.125 → v6.7.173 은 (primary, fallback) **2모델 하드코딩** 시대의 구조였다.
#   v6.7.446 에서 아래 체인 리스트로 대체됐고 그 시절 상수(GEMINI_31_MODEL 등)는 전부 제거됐다.
#   Config_Settings.GEMINI_31_TEST = 'KILL' 이면 체인 전체 OFF (시트 Key 이름만 옛 '31' 잔재 - 시트 계약).
# ── [v6.7.446] 2차 요약 모델 체인 — 앞에서부터 시도, **그 모델의 전 키를 소진해야** 다음 모델로 ──
#   구조 변경: 옛 코드는 (primary, fallback) 2모델을 8곳에 하드코딩해 모델 하나 추가에 전 파일 수정이
#   필요했다. 체인을 리스트 하나로 모아 **여기만 고치면 전 경로(카운터·예산·자정리셋·스탬프)가 따라온다.**
#
#   [유지 규칙] head 는 **항상 최신 Flash 모델**로 둔다(사용자 지침 2026-07-28). 새 Flash 가 나오면
#   맨 앞에 추가하고 필요 시 꼬리를 잘라낸다. 그 외 코드는 손댈 필요 없다.
#
#   per_key_rpd = **20/key 세 모델 모두 동일** (3.6/3.5/2.5 — 사용자 확인 2026-07-28. 2.5·3-preview 는
#   v6.7.39 실측으로 이미 20이었고 3.6/3.5 도 같은 값임이 확인됐다). Google 이 무료 티어 per-model RPD 를
#   공식 문서에서 내리고 AI Studio 대시보드로 옮겨(2026-07) 문서로는 확인이 안 되므로, 값이 바뀌면
#   **대시보드 확인이 근거**다.
#   덧붙여 이 숫자는 **자체 페이싱 힌트**이지 API 가 강제하는 상한이 아니다. 실제 상한은 429 로 통보되고
#   그때 `counter[key] = rpd` 로 그 키를 소진 마킹하므로(아래 호출 루프), 값이 어긋나도 자기교정된다.
PER_MODEL_RPD = 20

# ── [v6.7.600] 체인을 **동적으로 확정**한다 - 하드코딩은 그대로 늙는다 ────────────────
#   왜 바꾸나: 위 v6.7.446 의 "새 Flash 가 나오면 맨 앞에 추가한다" 는 사람이 하는 절차라
#   지켜지지 않았다. `gemini-3.7-flash` 는 2026-08-14 에 나왔는데 IME 만 못 쓰고 있었다 -
#   같은 레포의 다른 사본 둘은 이미 처리됐다(POE `crawlers/poe_ai_review.py` 가 v6.7.518 에
#   수동 등재하며 *"최신 모델은 자동으로 안 잡힌다"* 를 주석에 박아 뒀고, AME
#   `jira_register/feed_ai.py` 는 v0.1.28 에 동적 발견으로 전환했다). **사본 3곳 중 IME 만 남았다.**
#
#   그리고 하드코딩은 **은퇴에도 취약하다**: Gemini 는 preview 부터 순차 셧다운되고
#   `gemini-2.5-flash` 도 2026-10-16 셧다운 예정이라, 손으로 박은 꼬리는 언젠가 404 가 된다.
#
#   이식 원본 = AME `feed_ai.py:discover_chain()`. **무인 운영 전제**(아무도 안 고쳐주는 환경에서
#   도는 것이 목표)라 "실패 시 무엇이 남는가" 가 설계의 중심이라는 점까지 그대로 가져온다.
CHAIN_LEN = 5             # 체인 길이 - 예산 산식(키수 x PER_MODEL_RPD x 길이)이 이 값에 걸려 있다
#   [v6.7.680] 3 -> 5. 2차 flash 는 **RPD 20/키/모델** 이라 길이가 곧 일일 천장이다
#   (6키 x 20 x 3 = 360콜/일 -> x5 = 600). 발견은 `models.list` 동적이라 3.5·2.5 가 자동으로
#   꼬리에 붙고, 셧다운되면 알아서 빠진다. 3.5 유보는 v6.7.446 에 이미 해제됐다(solare 미사용).
#   앞 모델의 전 키를 소진해야 내려가므로 평시 품질은 그대로고 **꼬리에만 용량이 붙는다.**
_CHAIN_RETRY_S = 1800     # 발견 실패 후 재시도 간격(30분). '날짜를 찍어 하루를 막는 것'과 다르다
_CHAIN_DB_KEY = 'gemini_chain_last'   # system_settings 키 (기존 k/v 테이블 재사용 - 신규 테이블 없음)

# **정식 flash 만 고른다.** 끝을 `-flash` 로 못박아 lite·image·tts·native-audio·live·preview 와
#   `-latest` 별칭을 전부 뺀다. 별칭을 넣으면 실명과 **같은 RPD 버킷을 둘로 세어** 예산이 부풀려진다
#   (AME v0.1.28 이 같은 이유로 `gemini-flash-latest` 를 체인에서 뺐다).
_FLASH_RE = re.compile(r'^gemini-(\d+)(?:\.(\d+))?-flash$')

# ③ 최후 폴백 - 발견도 저장본도 없을 때만 쓴다. **이 값도 늙는다**(그게 이 커밋의 이유다).
#   3.7 은 POE 크롤러가 v6.7.518(2026-08-14)에 **같은 키 풀로 HTTP 200 실호출 확인**한 모델이다
#   (이름만 보고 넣으면 매 사이클 404 를 한 번씩 태운다 - 그쪽 주석의 경고).
DEFAULT_GEMINI_CHAIN = ['gemini-3.7-flash', 'gemini-3.6-flash', 'gemini-3.5-flash']


def _chain_ver(model_id):
    """`gemini-3.7-flash` → `(3, 7)`. 정식 flash 가 아니면 None (= 체인 후보에서 탈락)."""
    m = _FLASH_RE.match(model_id or '')
    return (int(m.group(1)), int(m.group(2) or 0)) if m else None


def _chain_label(model_id):
    """`gemini-3.7-flash` → `v37`. 기존 라벨 규칙(v36/v35/v25)과 **같은 형태를 유지**한다 -
    이 문자열이 `gemini_counts` 의 키이자 로그·소진 경보에 그대로 나간다.

    **라벨은 모델별로 유일해야 한다.** RPD 버킷이 모델별이므로, 위치 기반(t0/t1/t2)으로 매기면
    체인이 갈리는 날 새 모델이 옛 모델의 소진 카운터를 물려받아 **쓰지도 않은 쿼타가 소진 상태로**
    시작한다.
    """
    m = _FLASH_RE.match(model_id or '')
    return f"v{m.group(1)}{m.group(2) or '0'}" if m else str(model_id)

# ── [v6.7.447] 오버플로 티어 — Gemini 가 **전부 막혔을 때만** 도는 마지막 안전망 ──
#   왜 필요한가: 위 체인은 키당 RPD 20 이라 총 360콜/일(6키)인데, 실측 요약 발생이 평시 137건/일 ·
#   폭주일 359건/일(2026-07-25 POE1 리그)이라 **여유가 하루치도 안 된다**. 소진되면 자정까지 요약이
#   통째로 멈추고(PATTERN-43 의 17시간 침묵), 침묵은 성공 시에만 울리는 알림 구조상 **정상 상태와
#   구분되지 않아** 며칠 뒤에나 발견된다. 이 티어는 그 실패 모드를 **침묵 → 품질 저하**로 내린다.
#
#   왜 gemma-4 인가: RPD **1,440/키/모델**(사용자 확인 2026-10-01 - 07-28 의 14,400 은 틀린 값이었다)
#   = 6키 8,640콜/일. 필요량(평시 요약 137건/일 · 폭주일 359건)보다 여전히 크다.
#   단 **같은 31b 버킷을 1차 분류 폴백·디코 판정 잡도 쓴다**(1차 주력은 26b - 버킷이 따로다).
#   부족해지면 `gemma-4-26b` 등을 이 리스트에 추가하면 된다.
#
#   **페이싱 예산에서 제외한다**(`_pace_allowance` 는 확정 체인만 합산). 여기를 예산에 넣으면
#   시간당 상한이 140콜/시로 뛰어 **희소한 Gemini 360콜을 1~2시간에 태우고** 내려앉는다 - v6.7.436 이
#   막으려던 바로 그 경로다. 오버플로는 '예산이 막힌 뒤' 도는 것이지 예산을 늘리는 것이 아니다.
#
#   품질 주의: 이 모델은 1차에서 분류(analyze true/false)만 하던 모델이라 요약 품질은 Gemini 보다
#   낮다. 파싱 실패 시엔 기존과 동일하게 대기 스탬프로 떨어지므로 **회귀 위험은 0**이다.
GEMINI_OVERFLOW_CHAIN = [
    ('gemma-4-31b-it', 1440, 'g31'),   # [2026-10-01] 14400 -> 1440 (사용자 확인 - 위 주석)
]
# [v6.7.600] `_ALL_MODEL_TIERS` 상수는 제거됐다 - 체인이 동적이라 모듈 로드 시점에 확정할 수 없다.
#   호출처는 인스턴스 메서드 `self._all_tiers()` 를 쓴다(확정 체인 + 오버플로).
KST = ZoneInfo("Asia/Seoul")

# [v6.7.436] 일일 호출 예산 시간당 페이싱 - 2026-07-25 실사고(AI 요약 17시간 정지) 재발 방지.
#   사고 경위: 03~05시 크롤 백로그 폭주(1,400건/시)로 Gemini 2차 요약이 2시간 만에 306건 발화 →
#   일일 예산(6키 × 20 × 2모델 = 240콜, 실측 콜당 1~2건)을 통째 소진 → 06시부터 자정까지 analyze=true
#   글이 전부 '(전 모델 RPD 소진)' 스탬프 → v6.7.205 정책상 저장 생략 → 요약 0건 + 무한 재시도 →
#   23:39 컨테이너 재시작(카운터 리셋)까지 17시간 침묵. 그동안 유실된 것은 POE1 패치노트/점검 공지,
#   배그 건의게시판 조치요청 등 정확히 요약 대상이던 글들(실측: 미저장 pending 잔존분).
#   처방: 남은 예산을 남은 시간에 균등 배분해 "폭주가 하루치를 몇 시간에 태우는" 경로를 구조적으로 차단.
#   부수효과(의도): 상한에 걸린 글은 저장 없이 pending 유지 → 다음 시간대에 함께 배치되어 콜당 건수가
#   올라간다(콜 수 기준 RPD 이므로 처리량이 오히려 증가).
_GEMINI_PACE_ENABLED = True

# [v6.7.618] 페이싱 창 길이(초). **정시 경계가 아니라 굴러가는 창**이다 - 근거는 `_pace_allowance`.
_PACE_WINDOW_SEC = 3600

# [v6.7.618] 429 백오프. 종전의 "자정까지 소진 마킹" 을 대체한다.
#   왜 지수인가: 429 는 **분당 한도(RPM)** 일 수도 **일일 한도(RPD)** 일 수도 있고 응답만으로는
#   확실히 못 가른다. 고정 쿨다운은 어느 한쪽에서 반드시 틀린다 - 짧으면 RPD 소진 키를 하루 종일
#   두드리고, 길면 RPM 블립 하나에 키를 몇 시간 버린다. 지수는 **관측으로 가른다**: RPM 블립은
#   첫 재시도에서 풀려 60초로 끝나고, 진짜 RPD 소진은 연속 실패로 상한까지 올라가 하루 헛호출이
#   키당 10여 회로 묶인다. 어느 쪽이든 **스스로 낫는다** - 그게 자정 마킹과의 결정적 차이다.
_KEY_COOLDOWN_BASE_SEC = 60
_KEY_COOLDOWN_MAX_SEC = 7200

# [v6.7.619] **키 축과 모델 축을 가른다.** 429 는 키가 문제라 다음 키로 가는 것이 맞지만,
#   5xx 는 서버가 "이 모델이 과부하" 라고 말하는 것이라 키를 바꿔도 같은 모델로 간다.
#   2026-08-28 실측 메시지:
#     503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This model is currently
#     experiencing high demand. Spikes in demand are usually temporary...'}}
#   숫자만 substring 으로 찾으면 본문의 '500' 같은 값에 오탐하므로 **코드가 놓이는 두 자리**
#   (선두 `503 ` / `'code': 503`)와 gRPC 상태명만 본다.
#   (첫 판은 `[\s(]50[0-9][\s.,:]` 였는데 *"Summary length 500 characters exceeded"* 를
#    503 계열로 오판했다 - 자유 문장 안의 숫자를 상태코드로 읽는다. 자리를 못 박아 고쳤다.)
_MODEL_SCOPED_RE = re.compile(
    r"(?:^|:\s)(50[0-9])[\s.:]|'code':\s*(50[0-9])|\bUNAVAILABLE\b|\bINTERNAL\b")
_MODEL_COOLDOWN_SEC = 120
# [v6.7.699] **2차(flash) 호출 시간 상한 + 늦은 실패는 오래 쉰다.**
#   라이브 2026-09-28 18:15~18:40: 3.7-flash 요청 **하나가 18분 35초** 응답 없이 붙잡혀(그 뒤 503) 사이클 하나가
#   1,487초였고, 사이클은 직렬이라 그동안 1차도 멈춰 79건이 쌓였다(30분 밀림 경보). 이어진 사이클도 3.7 이
#   매번 약 3분 뒤 503 이라 6~7분씩 걸렸다. 1차(26b)는 내내 2~4초로 정상이었다.
#   - 상한: flash(페이싱 티어)만 **30초**. 정상 2차는 5건 9초 · 7건 약 15초(09-28 실측). 서버 최소 deadline 은 10초
#     (`Manually set deadline ... Minimum allowed deadline is 10s` - SDK 1.63 이 timeout 을 **서버까지 실어 보낸다**,
#     `genai_client.py` 의 "timeout 미적용" 서술은 이 실측으로 정정). SDK `attempts=1`(= 단일 시도, 재시도 없음) - 늦은 실패를 두 번 기다리지 않는다.
#   - 오버플로 31b(thinking 켬)에는 **짧은 상한을 걸지 않는다** - 10건 요약에 100초가 넘는 모델이라 비상구가 막힌다.
#   - 실패까지 `_SLOW_FAIL_SEC` 이상 걸린 **flash**(타임아웃 포함)는 `_SLOW_MODEL_COOLDOWN_SEC` 쉰다 - 120초면
#     6분 사이클마다 같은 벽을 또 3분씩 친다.
_SECOND_FLASH_TIMEOUT_MS = 30000
_SLOW_FAIL_SEC = 20
_SLOW_MODEL_COOLDOWN_SEC = 1800


def _is_timeout(ex, msg):
    """클라이언트/서버 시간 초과인가. 서버 deadline 초과는 504 로 와 `_is_model_scoped` 가 잡지만,
    httpx 읽기 타임아웃은 상태코드가 없어 문구·타입으로 가른다(모델 축으로 취급 - 키를 바꿔도 같은 모델이다)."""
    name = type(ex).__name__.lower()
    return 'timeout' in name or 'timed out' in (msg or '').lower() or 'DEADLINE_EXCEEDED' in (msg or '')


def _status_of(msg):
    m = _MODEL_SCOPED_RE.search(msg or '')
    if not m:
        return '?'
    return m.group(1) or m.group(2) or (m.group(0) or '').strip()


def _is_model_scoped(msg):
    """이 실패가 **모델 축**인가(= 키를 바꿔도 소용없나).

    429/RESOURCE_EXHAUSTED 는 키 축이므로 여기서 제외한다 - 그쪽은 키별 백오프가 담당하고,
    둘을 섞으면 쿼타 소진 키 하나 때문에 멀쩡한 모델을 통째로 쿨다운시킨다.
    """
    msg = msg or ''
    if '429' in msg or 'RESOURCE_EXHAUSTED' in msg or 'quota' in msg.lower():
        return False
    return bool(_MODEL_SCOPED_RE.search(msg))


# [v6.7.659] **429 가 어느 쿼타인지 로그에 남긴다.** 2026-09-11 23:17~23:38 라이브: 21분에
#   429 가 8회, **키 6개 전부**에 걸렸는데 우리 줄은 `429 쿼타 key#N - 키 교체 후 재시도` 뿐이라
#   RPM 인지 RPD 인지 **분당 입력 토큰**인지 구분할 수 없었다. 처방이 전부 다른데 로그가 셋을
#   같은 모양으로 보여준다 - 키 증설(RPD) · 페이싱(RPM) · 배치 축소(입력 토큰)는 서로 다른 일이다.
#   같은 날 IMT 실측이 그 답을 한 번 보여줬다(`generate_content_free_tier_input_token_count`,
#   16,000/분) - **같은 것인지 여기서는 아직 모르고, 이 줄이 다음 429 에서 답한다.**
#   키 풀도 다르다(IME=K1~K6 / IMT=공용 4개)라 그대로 옮겨 읽으면 안 된다.
#
#   `_RETRY_DELAY_RE` 는 **여기가 정본이다** - 판정 쪽(`discord_judge._retry_after`)이 사본을
#   갖고 있었는데 이번에 걷어내고 여기서 import 하게 했다. 이 파일이 그쪽에 `_is_model_scoped`·
#   `GEMMA_MODEL` 을 주는 것과 같은 방향이다(core -> judge 단방향, 순환 아님).
#   **사본을 다시 만들지 말 것** - 두 벌이 되면 다른 속도로 늙는다(PATTERN-62).
#   [v6.7.742] 그 소비자(디코 판정 잡)는 삭제됐다 - 규약(core 가 정본, 판정 쪽은 import)은 그대로 둔다.
_QUOTA_ID_RE = re.compile(r"quotaId['\"]?\s*[:=]\s*['\"]([^'\"]+)")
_RETRY_DELAY_RE = re.compile(r"retrydelay['\"]?\s*[:=]\s*['\"]?(\d+)\s*s", re.I)


def _quota_brief(msg):
    """429 본문에서 **처방을 가르는 두 값**만 뽑는다 - 없으면 빈 문자열(로그가 안 길어진다)."""
    out = ''
    try:
        q = _QUOTA_ID_RE.search(msg or '')
        if q:
            out += ' quota=%s' % q.group(1)
        d = _RETRY_DELAY_RE.search(msg or '')
        if d:
            out += ' retryDelay=%ss' % d.group(1)
    except Exception:
        pass
    return out

from private_config import CONFIG_SHEET_URL  # noqa: E402  [v6.7.745] 시트 ID 는 utils/private_config.py 한 자리
CONFIG_RELOAD_INTERVAL_SEC = 300


def _pick_context(recent, game, cutoff):
    """[v6.7.613] 그 게임의 맥락 줄 `[(요약문, severity)]` (오래된 것 -> 최신 순).

    **AME `feed_ai._pick_context` 와 같은 규칙이어야 한다** - 두 앱을 나란히 켜고 요약 건수를
    비교하는 중이라 여기가 갈리면 그 차이를 해석할 수 없다. 세 가지를 한다:

    1. **창(24h) 안**의 그 게임 요약을 오래된 것부터. 중복 문장은 접되 **가장 최근의 severity 를
       남긴다** - 같은 동향이 info -> warning 으로 올라갔다면 지금 등급이 알고 싶은 값이다.
    2. **상한 초과 시 덜 심각한 것부터 버린다.** 종전에는 `[-cap:]` 로 오래된 것부터 버렸는데,
       잘려 나가는 것이 '덜 중요한 것' 이 아니라 '먼저 온 것' 이었다. 남길 것을
       (심각도, 최신순)으로 고르고 **출력은 다시 시간순**으로 되돌린다.
    3. 창 안이 하한 미만이면 **창 밖 과거로 채운다** (조용한 게임 · 기동 직후).
    """
    def collapse(rows):
        # 뒤에서부터 훑어 **최근 것을 남기고** 중복을 접는다. 그 뒤 시간순으로 되돌린다.
        seen, out = set(), []
        for s, sv in reversed(rows):
            if s in seen:
                continue
            seen.add(s)
            out.append((s, sv))
        out.reverse()
        return out

    fresh = collapse([(s, sv) for gg, s, t, sv in recent if gg == game and t >= cutoff])
    if len(fresh) < _CONTEXT_MIN_PER_GAME:
        allg = collapse([(s, sv) for gg, s, _t, sv in recent if gg == game])
        return allg[-_CONTEXT_MIN_PER_GAME:]
    if len(fresh) > _CONTEXT_MAX_PER_GAME:
        idx = sorted(range(len(fresh)),
                     key=lambda i: (_SEV_KEEP_RANK.get(fresh[i][1], 2), -i))[:_CONTEXT_MAX_PER_GAME]
        fresh = [fresh[i] for i in sorted(idx)]
    return fresh



class Gemma4SummaryCore:
    """Gemma4 1차 분류 + Gemini 2차 요약 실행 코어. Flask 프로세스 내 싱글턴으로 운용.

    [역할 분리 — v6.7.63~]
    - Gemma4 (v6.7.698~ 주력 26b, 폴백 31b): "Gemini 가 요약할 가치 있는가" yes/no 분류만 (`{"id": N, "analyze": bool}`).
    - 확정 체인 (`resolve_chain()`, v6.7.600~): analyze=true 글만 정식 summary/sentiment/severity 생성.
      앞 모델의 전 키 소진 시에만 다음 모델로 폴백. 모델 추가/교체는 그 상수 한 곳만 고치면 된다.

    [상태]
    - `gemini_counts`: `{체인 라벨: {api_key: 오늘 호출 수}}` RPD 카운터 (KST 자정 reset).
    - `gemini_test_enabled`: Config_Settings.GEMINI_31_TEST=KILL 시 체인 전체 OFF
      (시트 Key 이름은 옛 '31' 잔재 - 시트 계약이라 유지).
    """

    def __init__(self):
        self.handler = get_handler()
        self.client = None
        self.api_keys = []
        self.current_api_key_index = 0
        self.system_instruction = None
        # [v6.7.472] Config_LLM B2 = 종합 동향 리포트 프롬프트 (bg/trend_digest.py 소비).
        #   A2:B2 를 이미 읽고 있었는데 row[0](A2)만 쓰고 **B2 를 버리고 있었다** - 은퇴한
        #   AI 프로세서가 쓰던 셀이라 소비처가 사라져 있었다(v6.7.470 까지).
        self.report_instruction = None
        # [v6.7.611/612/613] `[(게임, 요약문, 저장시각epoch, severity)]` - 구 구조는 요약문
        #   문자열만이었다(게임별 맥락·24시간 창·심각도 표기가 전부 불가). 소비처는
        #   `_build_context_section` -> `_pick_context` 하나뿐이라 원소 확장이 안전하다.
        self.recent_summaries = []
        # [v6.7.680] 사이클 단위 2차 대상 모음 (배치 스레드가 extend - GIL 하에서 원자적)
        self._deferred_second = []
        self._recent_hydrated = False       # 첫 사용 시 DB 에서 24시간치 되읽기 (재시작 공백 방지)
        # [v6.7.454] 병렬 배치 대비 - 구 `last_gemma_call_time`(전역 1개) 을 키별 dict 로 교체.
        #   {key_index: 다음 콜이 시작 가능한 시각(monotonic 아님, time.time 기준)}
        self._last_call_by_key = {}
        # [v6.7.454] 병렬 배치의 공유 상태 보호.
        #   _gemma_lock  : 1차 키 라운드로빈 + 키별 스로틀 (짧게 잡고 sleep 은 락 밖에서)
        #   _gemini_lock : 2차 전체를 직렬화. RPD 카운터/페이싱/키 인덱스가 **예산 가드 그 자체**라
        #     레이스가 나면 상한이 새거나 증가분이 유실된다(PATTERN-43 이 막으려던 바로 그 실패).
        #     2차는 analyze=true 인 3%만 타고 사이클당 0~1콜이라, 직렬화해도 처리량 손실이 사실상 0.
        self._gemma_lock = threading.Lock()
        self._gemini_lock = threading.Lock()
        self._last_config_reload = 0.0
        self._kill_flag = False
        # [v6.7.65~] Gemini 2차 요약 상태 — 모델별 RPD 카운터 분리 (v3 / v25 폴백).
        self.gemini_test_enabled = True            # 기본 ON, Config_Settings.GEMINI_31_TEST=KILL 시 OFF
        # [v6.7.446] {label: {api_key: 오늘 호출 수}} — 체인 모델별 x 키별 RPD 카운터 (KST 자정 reset).
        #   구 gemini_v3_per_key_count / gemini_v25_per_key_count 2개 속성을 대체(참조는 이 파일 안뿐이었다).
        # [v6.7.600] 빈 dict 로 시작한다 - 체인이 동적이라 라벨을 미리 알 수 없다. 호출 직전
        #   `setdefault(label, {})` 가 채우므로 사전 생성이 필요 없다(오버플로 라벨도 같은 경로).
        self.gemini_counts = {}
        # [v6.7.600] 동적 체인 확정 상태.
        #   _chain_cache_key = (KST 날짜, 키 지문) - **날짜만으로는 부족하다**(PATTERN-55).
        #     발견 결과는 '어떤 키로 조회했는가'에 종속되므로 키가 교체되면 캐시를 버려야 한다.
        #   _chain_fail_at   = 마지막 발견 실패 시각. 재시도 간격을 벌리는 데만 쓴다.
        #     **실패는 날짜를 찍지 않는다** - 찍으면 부팅 순간 네트워크가 한 번 흔들릴 때
        #     하루 내내 옛 체인으로 돈다(AME v0.1.28 이 고친 결함).
        #   _chain_pin       = Config_Settings.GEMINI_CHAIN_PIN (배포 없이 고정하는 비상구).
        self._chain_cache = []
        self._chain_cache_key = None
        self._chain_fail_at = 0.0
        self._chain_source = 'default'
        self._chain_pin = []
        self.gemini_test_key_index = 0             # round-robin 시작 인덱스 (양 모델 공유)
        self.gemini_test_last_reset_date = None    # KST date — 자정 자동 reset (양 모델 동시)
        # [v6.7.436] 시간당 페이싱 상태. 프로세스 로컬(재시작 시 리셋).
        # [v6.7.618] **고정 정시 버킷 -> 슬라이딩 60분 창.** 정시 버킷은 상한을 채운 순간부터
        #   다음 정시까지 2차를 통째로 세우고, 정시에 밀린 것이 한꺼번에 나간다 - 요약도 알림도
        #   뭉쳐서 오고 오래된 건은 그만큼 늦다. 2026-08-28 실측: 10:16 수집분이 11:01:34 에
        #   저장됐고(44분) 같은 사이클에 4건이 함께 나갔다. 창을 굴리면 상한은 같은데 방출이
        #   고르게 퍼진다(대기 상한 = 창 길이가 아니라 '한 콜이 빌 때까지').
        self._pace_calls = []                      # 최근 호출 시도 시각(단조시계) - 60분 창
        self._pace_blocked_log_at = 0.0            # '상한 도달' 로그 스로틀(단조시계)
        # [v6.7.618] 429 를 맞은 (label, key) 의 재시도 금지 시각 + 연속 실패 횟수.
        #   종전에는 429 한 번에 `counter[key] = rpd` 로 **그 키를 자정까지 소진 처리**했다.
        #   RPD 가 큰 티어에서 이것이 치명적이다 - 오버플로(`g31`)는 키 6개가 429 를 한 번씩만
        #   맞아도 카운터가 만석이 되어 **하루 종일 비상구가 닫힌다.** 2026-08-28 실측이 정확히 그
        #   상태였다(당시 상수 14,400 으로 86,400/86,400 - 1차 분류는 이 카운터를 안 쓰므로 실호출일
        #   수 없다. 상수는 2026-10-01 에 실제 한도 1,440 으로 정정). 게다가 부풀린 카운터는 `_pace_allowance` 의 `used_today` 로도
        #   들어가 **시간당 허용량까지 같이 깎는다** - 한 번의 429 가 두 번 벌을 준다.
        self._key_cooldown = {}                    # {(label, key): 재시도 가능 시각(단조시계)}
        self._key_429_streak = {}                  # {(label, key): 연속 429 횟수} - 백오프 지수
        self._pace_daily_exhausted = False         # 이번 호출의 차단 사유(일일 0 인가) - 스탬프가 읽는다
        # [v6.7.619] 모델 축 쿨다운 {label: 재시도 가능 시각}. 5xx 를 맞은 모델을 20초마다
        #   다시 두드리지 않게 한다(키 쿨다운과 축이 다르므로 별도 dict 다).
        self._model_cooldown = {}

        if not _HAS_GENAI:
            logger.error("[Gemma4Core] google-genai SDK가 없어 초기화 중단")
            return

        try:
            self._load_system_instruction()
            self._load_notification_config()
            self._init_gemma4()
            self._last_config_reload = time.time()
            logger.info("[Gemma4Core] 초기화 완료 (model=%s, keys=%d)", GEMMA_MODEL, len(self.api_keys))
        except Exception as e:
            logger.error(f"[Gemma4Core] 초기화 중 예외: {e}")

    def _init_gemma4(self):
        """google.genai 클라이언트 초기화."""
        if not self.api_keys:
            self.client = None
            return
        try:
            current_key = self.api_keys[self.current_api_key_index % len(self.api_keys)]
            self.client = _make_client(current_key)
        except Exception as e:
            logger.error(f"[Gemma4Core] 클라이언트 초기화 실패: {e}")
            self.client = None

    def _switch_api_key(self):
        """Round Robin으로 다음 API 키로 교체."""
        if not self.api_keys:
            return
        self.current_api_key_index = (self.current_api_key_index + 1) % len(self.api_keys)
        self._init_gemma4()

    def _acquire_gemma_client(self):
        """[v6.7.454] 1차 분류용 **배치 전용** 클라이언트 확보 (key_index, client) 반환.

        병렬 배치가 `self.client` 를 서로 갈아끼우면 (a) 어느 키로 나갔는지 로그가 거짓이 되고
        (b) 한 배치의 429 키 교체가 **다른 배치의 진행 중인 호출까지** 흔든다. 그래서 1차도 2차와
        같은 방식(호출마다 로컬 클라이언트 - `_generate_gemini_summaries` 의 `test_client` 선례)으로
        바꾼다. `self.client` 는 초기화 성공 여부 sentinel 로만 남는다.

        키 라운드로빈과 키별 스로틀 계산은 락 안에서, **sleep 은 락 밖에서** 한다
        (락을 쥔 채 자면 병렬 배치가 그 시간만큼 직렬화된다).
        """
        if not self.api_keys:
            return None, None
        with self._gemma_lock:
            idx = self.current_api_key_index % len(self.api_keys)
            self.current_api_key_index = (idx + 1) % len(self.api_keys)
            key = self.api_keys[idx]
            now = time.time()
            ready_at = self._last_call_by_key.get(idx, 0.0)
            wait = max(0.0, ready_at + _GEMMA_KEY_MIN_INTERVAL - now)
            # 예약 - 다른 스레드가 같은 키를 집어도 이 시각 뒤로 밀리게 한다.
            self._last_call_by_key[idx] = now + wait
        if wait > 0:
            time.sleep(wait)
        try:
            return idx, _make_client(key)
        except Exception as e:
            logger.error(f"[Gemma4Core] 1차 클라이언트 생성 실패 key#{idx}: {e}")
            return idx, None

    def _hydrate_recent_summaries(self):
        """[v6.7.612] 부팅 후 첫 사용 시 **DB 에서 최근 24시간 요약을 되읽는다.**

        `recent_summaries` 는 프로세스 메모리라 **재시작하면 비고**, 그러면 맥락 블록이 다시
        찰 때까지(배포 직후 수십 분) 판정이 v6.7.611 이전 상태로 돈다 - 배포가 잦은 앱에서
        이건 드문 사고가 아니라 상시 조건이다. 게다가 AME 수집기는 DB 기반이라 재시작해도
        맥락이 유지되므로, 이걸 안 하면 **재시작 직후마다 두 앱의 입력이 갈린다**(지금 요약
        건수를 나란히 비교하는 중이라 그 차이가 그대로 노이즈가 된다).

        **부팅 시가 아니라 첫 호출 때** 한다 - `__init__` 은 시트 인증 경로라 여기서 DB 를
        건드리면 초기화 실패 원인이 하나 늘고, 요약 워커가 돌기 전에는 필요도 없다.
        실패해도 조용히 넘어간다(맥락이 없을 뿐 요약은 나간다).

        조회는 `get_ai_analysis_for_digest` 재사용 - `bg/trend_digest.py` 가 쓰는 그 함수이고
        `idx_ai_inserted_notable` 부분 인덱스가 이미 깔려 있다(없으면 수십만 행 전수 스캔).
        `AI_NOTABLE_FILTER` 가 '분석 제외'·빈 요약을 걸러 주므로 여기서 다시 거를 것이 없다.
        """
        if self._recent_hydrated:
            return
        self._recent_hydrated = True          # 실패해도 재시도하지 않는다 (매 배치 DB 재조회 방지)
        try:
            from sqlite_handler import db_handler
            # **`import datetime`(모듈) 이다** - 이 파일의 다른 자리도 전부 `datetime.datetime`.
            #   v6.7.612 가 `datetime.now()` 로 써서 라이브에서 AttributeError 가 났고, 아래
            #   try/except 가 그것을 "맥락 없이 시작" 경고로 삼켜 **크래시 없이 기능만 죽어**
            #   있었다(2026-08-26 배포 후 실측으로 발견). 가드가 일시적 실패를 덮으라고 있는
            #   것인데 코드 결함까지 덮은 자리다.
            now = datetime.datetime.now(datetime.timezone.utc)
            rows, _total = db_handler.get_ai_analysis_for_digest(
                (now - datetime.timedelta(hours=_CONTEXT_WINDOW_H)).strftime('%Y-%m-%d %H:%M:%S'),
                now.strftime('%Y-%m-%d %H:%M:%S'), limit=_RECENT_CAP)
        except Exception as e:
            logger.warning(f"[Gemma4Core] 최근 요약 되읽기 실패 (맥락 없이 시작): {e}")
            return
        if not rows:
            return
        # 되읽은 것에는 정확한 저장시각이 없다(조회가 안 준다). **창 안이라는 사실만 알면 되므로**
        #   전부 '창 시작' 시각으로 찍는다 - 이후 append 되는 실시간 분이 자연히 뒤에 온다.
        stamp = time.time() - _CONTEXT_WINDOW_H * 3600 + 1
        seeded = [(r.get('game') or '공통', (r.get('summary') or '').strip(), stamp,
                   r.get('severity') or 'info')
                  for r in rows if (r.get('summary') or '').strip()]
        self.recent_summaries = seeded + self.recent_summaries
        logger.info(f"[Gemma4Core] 최근 {_CONTEXT_WINDOW_H}시간 요약 {len(seeded)}건 되읽음 "
                    f"(재시작 직후 맥락 공백 방지)")

    def _build_context_section(self, batch_games):
        """[v6.7.611] 프롬프트에 실을 '최근 동향' 맥락 블록 - **게임별로 나눠서** 준다.

        ## 왜 게임별인가 (사용자 요청 2026-08-25)

        v6.7.610 까지는 `recent_summaries` 가 **요약문 문자열만** 담은 평평한 리스트였고, 최근
        20건을 게임 구분 없이 통째로 실었다. 두 가지가 조용히 망가진다:

        1. **볼륨 편중** - 글이 많은 게임이 20칸을 다 먹는다. 배그가 압도적인 시간대에는 POE 글을
           판정할 때 맥락이 전부 배그 것이라 **참고할 것이 하나도 없는 상태**로 돈다.
        2. **게임 간 오통합** - `"서버 점검 지연에 대한 불만 동향"` 은 세 게임에 다 있을 수 있는데,
           모델은 게임 구분 없이 *"중복/유사 내용 통합 시 참고"* 라는 지시를 받는다. 다른 게임의
           같은 문장을 같은 동향으로 접을 수 있다.

        ## 어떻게

        - **이번 배치에 등장하는 게임만** 싣는다. 안 나온 게임의 동향은 이 판정에 쓸모가 없고
          입력 토큰만 먹는다.
        - 게임 이름을 머리로 붙여 모델이 어느 게임 동향인지 알게 한다.

        ## 창 (v6.7.612 - 사용자 요청 "계속 컨텍스트가 쌓이는 개념으로")

        v6.7.611 은 **총 20줄을 게임 수로 나눠** 배분했다. 게임이 늘면 게임당 몫이 얇아지는
        구조라, "그 게임에서 오늘 무슨 동향이 있었나" 를 담기엔 부족했다. 이제 **게임마다
        독립적으로 최근 `_CONTEXT_WINDOW_H`(24)시간 전부**를 싣는다 - 총량 예산을 나누지 않는다.

        **자정 리셋이 아니라 롤링이다.** '금일' 로 자르면 00:05 의 맥락은 5분치 = 사실상 0 이고
        그건 이 기능이 없던 상태와 같다. 롤링이면 절벽이 없다.

        상·하한 둘로 감싼다: 게임당 최대 `_CONTEXT_MAX_PER_GAME`(60, 폭주일 방어 - 유입이
        늘수록 프롬프트가 비례해 커지는 구조를 만들지 않는다) / 최소 `_CONTEXT_MIN_PER_GAME`(3,
        창 안이 비면 창 밖 과거로 채운다 - 조용한 게임과 기동 직후가 그 경우다).

        **비용 실측** (AME 요약 시트 8일, 2026-08-18~25): 게임별 일 최대 배그 86 / POE1 44 /
        POE2 12 건, 요약문 평균 35자. 최악(배그 60줄 상한)이 약 2,300자로 A2 공통 지침
        13,807자의 17% 다. 3게임이 한 배치에 다 나와도 A2 의 40% 안쪽.

        **AME(`jira_register/feed_ai.py:_context_section`)와 출력 형식·머리말이 같아야 한다** -
        두 앱을 나란히 켜고 요약 건수를 비교하는 중이라(2026-08-25 검증), 한쪽만 바꾸면 그
        차이가 파이프라인 차이인지 프롬프트 차이인지 구별되지 않는다. 머리말 문자열 동일성은
        `jira_register/test_ame.py` 가 두 파일을 읽어 검사한다.
        """
        self._hydrate_recent_summaries()
        if not self.recent_summaries:
            return ""
        cutoff = time.time() - _CONTEXT_WINDOW_H * 3600
        want = list(dict.fromkeys(g for g in batch_games if g))
        if not want:
            want = list(dict.fromkeys(r[0] for r in self.recent_summaries))
        blocks = []
        for g in want:
            # 게임별로 오래된 것부터. **중복 제거는 게임 안에서** 한다 - 같은 문장이라도 게임이
            #   다르면 남긴다(서로 다른 게임의 동시 발생은 그 자체가 신호다).
            lines = _pick_context(self.recent_summaries, g, cutoff)
            if not lines:
                continue
            blocks.append(f"[{g}]\n" + "\n".join(f"- [{sv}] {s}" for s, sv in lines))
        if not blocks:
            return ""
        # 구 구현의 선두 `\n` 은 뺐다 - 템플릿이 `{context_section}` 앞뒤로 이미 빈 줄을 두고
        #   있어 빈 줄이 하나 더 생기던 것이고, **AME 와 블록 문자열이 글자 단위로 같아야**
        #   두 앱의 프롬프트 차이를 "이 블록 때문" 으로 오해하지 않는다.
        return f"{_CONTEXT_HEADER}\n" + "\n".join(blocks) + "\n"

    def _load_system_instruction(self):
        """Config_LLM 탭에서 프롬프트 2종 로드 - **A2 = 분류/요약 공용, B2 = 종합 동향 리포트**.

        [v6.7.472] 호출은 원래부터 `A2:B2` 범위였는데 `row[0]`(A2)만 쓰고 B2 를 버렸다. 셀을 읽는
        시트 API 호출은 그대로 두고 저장만 추가한다(쿼터 증가 0). B2 소비처는 `bg/trend_digest.py`.
        갱신은 `maybe_reload_config` 300초 주기라 **시트만 고치면 배포 없이 반영**된다.
        """
        try:
            sh = self.handler.gc.open_by_url(CONFIG_SHEET_URL)
            ws = sh.worksheet("Config_LLM")
            cell_values = ws.get('A2:B2')
            new_instruction = None
            new_report_instruction = None
            if cell_values and len(cell_values) > 0:
                row = cell_values[0]
                if row and len(row) > 0 and row[0]:
                    new_instruction = row[0].strip()
                if row and len(row) > 1 and row[1]:
                    new_report_instruction = row[1].strip()
            if new_instruction and new_instruction != self.system_instruction:
                self.system_instruction = new_instruction
                logger.info("[Gemma4Core] 시스템 프롬프트 갱신")
            if new_report_instruction and new_report_instruction != self.report_instruction:
                self.report_instruction = new_report_instruction
                logger.info("[Gemma4Core] 동향 리포트 프롬프트(B2) 갱신")
        except Exception as e:
            logger.warning(f"[Gemma4Core] 프롬프트 로드 실패 (기본값 유지): {e}")

    def _load_notification_config(self):
        """Config_Settings에서 GEMINI_API_KEYS와 REMOTE_KILL 플래그 파싱."""
        try:
            df = self.handler.read_dataframe(CONFIG_SHEET_URL, "Config_Settings")
            if df is None or df.empty:
                return
            records = df.to_dict(orient='records')
            new_api_keys = []
            kill_signals = {}
            new_gemini_test_enabled = True  # [Gemini 3.1 test] 키 없으면 ON (default)
            new_chain_pin = []              # [v6.7.600] 키 없으면 자동 확정 (default)
            for row in records:
                key = row.get('Key', '')
                if key == 'GEMINI_API_KEYS':
                    value = str(row.get('Value', '')).strip()
                    if value:
                        new_api_keys = [k.strip() for k in value.split(',') if k.strip()]
                elif key and key.startswith('REMOTE_KILL'):
                    kill_signals[key] = str(row.get('Value', '')).strip().upper()
                elif key == 'GEMINI_31_TEST':
                    # [Gemini 3.1 test] KILL 만 OFF, 그 외(빈/RUN/true) ON. REMOTE_KILL 컨벤션 일관.
                    val = str(row.get('Value', '')).strip().upper()
                    new_gemini_test_enabled = (val != 'KILL')
                elif key == 'GEMINI_CHAIN_PIN':
                    # [v6.7.600] 체인 수동 고정 - 동적 발견이 이상한 모델을 물었을 때 **배포 없이**
                    #   막는 비상구다(300초 뒤 반영). 콤마 구분 model id, 비우면 자동 복귀.
                    #   정식 flash 형태가 아닌 값은 버린다 - 오타로 체인 전체를 404 로 만들지 않는다.
                    raw_pin = str(row.get('Value', '')).strip()
                    new_chain_pin = [m for m in (x.strip() for x in raw_pin.split(','))
                                     if _chain_ver(m)]

            if new_api_keys and new_api_keys != self.api_keys:
                self.api_keys = new_api_keys
                self.current_api_key_index = 0
                logger.info(f"[Gemma4Core] API 키 {len(self.api_keys)}개 갱신")
                self._init_gemma4()

            self._kill_flag = (
                kill_signals.get('REMOTE_KILL') == 'KILL'
                or kill_signals.get('REMOTE_KILL_AI') == 'KILL'
            )

            # [Gemini 3.1 test] 토글 변경 로그
            if new_gemini_test_enabled != self.gemini_test_enabled:
                self.gemini_test_enabled = new_gemini_test_enabled
                state = 'ON' if new_gemini_test_enabled else 'OFF (KILL)'
                logger.info(f"[Gemma4Core] Gemini 3.1 test 상태 변경: {state}")

            # [v6.7.600] 체인 고정 변경 로그 - 해제(빈 값)도 로그로 남긴다.
            #   PATTERN-59 와 같은 이유다: 고정된 상태와 자동 확정 상태가 구분돼야 한다.
            if new_chain_pin != self._chain_pin:
                self._chain_pin = new_chain_pin
                self._chain_source = ''   # 다음 resolve_chain 에서 확정 로그를 다시 찍게 한다
                logger.info("[Gemma4Core] 체인 고정 %s",
                            ' -> '.join(new_chain_pin) if new_chain_pin else '해제 (자동 확정)')
        except Exception as e:
            logger.warning(f"[Gemma4Core] 설정 로드 실패: {e}")

    def maybe_reload_config(self):
        """5분 경과 시 프롬프트/API 키 재로드."""
        now = time.time()
        if now - self._last_config_reload < CONFIG_RELOAD_INTERVAL_SEC:
            return
        self._load_system_instruction()
        self._load_notification_config()
        self._last_config_reload = now

    def should_kill(self) -> bool:
        return self._kill_flag

    # ── [v6.7.600] 2차 요약 체인 확정 ──────────────────────────────────────────────
    def _chain_fingerprint(self):
        """캐시 키에 넣을 API 키 집합 지문. **키 값 자체는 어디에도 안 나간다**(해시 앞 8자)."""
        return hashlib.sha1(','.join(self.api_keys).encode('utf-8')).hexdigest()[:8]

    def _save_chain(self, chain):
        """마지막 성공 발견을 남긴다 - **조회가 실패했을 때 유일한 최신 근거다.**

        기존 `system_settings` k/v 테이블을 재사용한다(신규 테이블 없음 → 스키마 게이트 영향 0).
        """
        try:
            from sqlite_handler import db_handler
            db_handler.set_system_setting(_CHAIN_DB_KEY, ','.join(chain))
        except Exception as e:
            logger.debug(f"[Gemma4Core] 체인 저장 실패: {type(e).__name__}: {e}")

    def _load_chain(self):
        """저장된 마지막 성공 체인. **정식 flash 형태가 아닌 값은 버린다** - 손으로 잘못 넣거나
        옛 포맷이 남아 있어도 그것 때문에 404 를 태우지 않는다."""
        try:
            from sqlite_handler import db_handler
            raw = db_handler.get_system_setting(_CHAIN_DB_KEY, '') or ''
        except Exception as e:
            logger.debug(f"[Gemma4Core] 체인 로드 실패: {type(e).__name__}: {e}")
            return []
        return [m for m in (x.strip() for x in str(raw).split(',')) if _chain_ver(m)]

    def _discover_models(self):
        """`models.list` 에서 살아있는 정식 flash 최신 `CHAIN_LEN` 개. 실패하면 빈 리스트."""
        found = []
        for k in (self.api_keys or []):
            try:
                # `vertexai=False` - 환경변수로 Vertex 모드에 빠지면 api_key 를 무시하고 ADC 를
                #   찾다가 죽는다(AME v0.12.41 이 같은 SDK 에서 실측한 함정).
                cl = _make_client(k, vertexai=False)
                pairs = []
                for m in cl.models.list():
                    name = str(getattr(m, 'name', '') or '').replace('models/', '')
                    if _chain_ver(name):
                        pairs.append((_chain_ver(name), name))
                pairs.sort(reverse=True)
                found = [n for _v, n in pairs[:CHAIN_LEN]]
                if found:
                    break
            except Exception as e:
                # **키를 순서대로 시도한다** - 첫 키가 죽거나 권한이 빠져도 나머지로 발견이 된다.
                logger.debug(f"[Gemma4Core] 모델 목록 조회 실패(키 교체): {type(e).__name__}")
        return found

    def _pick_chain(self):
        """(체인 model_id 리스트, 확정 경로) 를 돌려준다. 아래 `resolve_chain` 이 유일한 호출자."""
        if self._chain_pin:
            return list(self._chain_pin), 'pinned'
        today = datetime.datetime.now(KST).strftime('%Y-%m-%d')
        cache_key = (today, self._chain_fingerprint())
        if self._chain_cache and self._chain_cache_key == cache_key:
            # 캐시 히트도 **경로는 'discover'** 다 - 캐시 키에 오늘 날짜가 들어 있으므로 이 값은
            #   '오늘 발견에 성공한 결과' 그 자체다. 별도 라벨로 두면 하루에 두 번(discover →
            #   cache) 확정 로그가 찍혀 "무엇이 바뀌었다" 는 신호가 노이즈에 묻힌다.
            return list(self._chain_cache), 'discover'
        # 실패 직후 매 사이클 두드리지 않는다(30분). **날짜는 안 찍으므로** 재시도가 계속 열려 있다.
        if not (self._chain_fail_at and (time.time() - self._chain_fail_at) < _CHAIN_RETRY_S):
            found = self._discover_models()
            if found:
                self._chain_cache, self._chain_cache_key, self._chain_fail_at = found, cache_key, 0.0
                self._save_chain(found)
                return list(found), 'discover'
            self._chain_fail_at = time.time()
        cached = self._load_chain()
        if cached:
            return cached[:CHAIN_LEN], 'db'
        return list(DEFAULT_GEMINI_CHAIN), 'default'

    def resolve_chain(self):
        """2차 요약 체인을 확정해 `[(model_id, rpd, label), ...]` 로 돌려준다.

        폴백 4단 - **뒤로 갈수록 오래된 근거**다:
          ⓪ `Config_Settings.GEMINI_CHAIN_PIN` : 특정 모델이 말썽일 때 **배포 없이** 고정하는 비상구
          ① `models.list` 발견 (하루 1회)      : 살아있는 정식 flash 최신 3개
          ② `system_settings` 저장본           : 마지막으로 성공한 발견
          ③ `DEFAULT_GEMINI_CHAIN`             : 최후

        ②가 없으면 네트워크가 한 번 흔들릴 때마다 ③(손으로 박은 값)으로 추락한다.

        **[PATTERN-59]** 자가치유가 검출기를 약화시키지 않도록 확정 경로를 `_chain_source` 에
        남기고 바뀔 때 로그를 찍는다. 이게 없으면 *"발견이 계속 실패해 ③ 으로만 도는 상태"* 와
        *"정상 발견"* 이 로그에서 완전히 같아 보인다 - **조용히 늙는 것이 이 커밋이 고치는 결함
        그 자체**라, 새 메커니즘이 같은 방식으로 조용해지면 안 된다.
        """
        chain, source = self._pick_chain()
        if source != self._chain_source:
            logger.info("[Gemma4Core] 체인 확정(%s): %s", source, ' -> '.join(chain))
            self._chain_source = source
        return [(m, PER_MODEL_RPD, _chain_label(m)) for m in chain]

    def _all_tiers(self):
        """확정 체인 + 오버플로 티어. 구 `_ALL_MODEL_TIERS` 상수의 대체."""
        return self.resolve_chain() + list(GEMINI_OVERFLOW_CHAIN)

    def _pace_allowance(self):
        """[v6.7.436] 최근 60분의 Gemini 호출 상한과 사용량을 계산한다.

        상한 = 남은 일일 예산 / 남은 시간 (최소 1). 앞 시간대가 덜 쓰면 뒤 시간대 상한이 자동으로
        올라가는 자기평형 배분이라 별도 이월 로직이 필요 없다. 예산이 0 이면 상한도 0.
        반환: (allow, used, remaining) - allow<=used 이면 지금은 호출 금지.

        [v6.7.618] **창이 굴러간다 - 정시 버킷이 아니다.**

        종전엔 버킷이 `(KST 날짜, 시)` 라 상한을 채운 순간부터 **다음 정시까지** 2차가 통째로
        섰다. 밀린 것은 정시에 한꺼번에 나가므로 (a) 요약이 뭉쳐서 뜨고 (b) 알림도 뭉쳐서 오고
        (c) 가장 오래된 건은 최대 창 하나만큼 늦는다. 2026-08-28 실측 - 10:16/10:18/10:31/10:32
        수집분 4건이 전부 `11:01:34` 한 사이클에 저장됐다(최대 44분).

        굴러가는 창은 **같은 시간당 상한을 유지하면서** 방출을 고르게 편다: 60분 전 호출이
        빠져나가는 순간 한 칸이 열리므로, 대기는 '다음 정시까지' 가 아니라 '한 칸이 빌 때까지' 다.
        총 호출량은 안 변한다 - 바뀌는 것은 도착 분포뿐이다(백테스트: `test_ai_resilience.py`).
        """
        now_kst = datetime.datetime.now(KST)
        # 단조시계로 창을 민다. 벽시계를 쓰면 NTP 보정 한 번에 창이 통째로 비거나 얼어붙는다.
        _mono = time.monotonic()
        cutoff = _mono - _PACE_WINDOW_SEC
        if self._pace_calls:
            self._pace_calls = [t for t in self._pace_calls if t >= cutoff]

        n = max(1, len(self.api_keys))
        # [v6.7.446] 체인 전 모델 합산 - 모델을 늘리면 예산도 자동으로 늘어난다(키수 x 체인 RPD 합).
        # [v6.7.600] 체인이 동적이라 여기서 확정본을 받는다. **길이가 CHAIN_LEN 로 고정**이라
        #   총예산(키수 x 20 x 3)은 종전과 같다 - 이 변경이 페이싱 강도를 바꾸지 않는다.
        chain = self.resolve_chain()
        total = n * sum(rpd for _m, rpd, _l in chain)
        # [v6.7.447] 오버플로 티어는 예산 밖 - 체인 라벨만 합산(넣으면 gemma4 호출이 Gemini 잔여를 깎는다)
        used_today = sum(sum(self.gemini_counts.get(l, {}).values()) for _m, _r, l in chain)
        remaining = max(0, total - used_today)
        # [v6.7.618] 남은 시간을 **분 단위까지** 본다. 종전 `24 - hour` 는 한 시간 내내 같은 값이라
        #   10:00 과 10:59 의 상한이 같았다 - 창이 굴러가면 그 계단이 그대로 톱니로 남는다.
        elapsed_h = now_kst.hour + now_kst.minute / 60.0 + now_kst.second / 3600.0
        remaining_hours = max(1.0, 24.0 - elapsed_h)
        allow = 0 if remaining <= 0 else max(1, int(remaining / remaining_hours))
        return allow, len(self._pace_calls), remaining

    def generate_trend_report(self, data_section):
        """[v6.7.472] Config_LLM **B2** 프롬프트로 '종합 동향 리포트' 1건 생성. `(text, label)` 반환.

        실패 시 `(None, 사유)` - 호출부(`bg/trend_digest.py`)가 그 사유를 그대로 카카오로 알린다.
        조용히 None 을 삼키면 리포트가 안 오는 상태와 정상이 구분되지 않는다(PATTERN-43).

        [설계]
        - **모델은 기존 체인 재사용** (`resolve_chain()` → `GEMINI_OVERFLOW_CHAIN`). 키 라운드로빈과
          RPD 카운터(`gemini_counts`)도 2차 요약과 **같은 것**을 쓴다 - 따로 세면 상한이 샌다.
        - **시간당 페이싱 게이트는 우회하되 카운터는 증가**시킨다. 이 리포트는 하루 **2콜**로
          예산(6키 x 60 = 360콜/일)의 0.6% 라, 페이싱에 막혀 리포트가 통째로 안 나가는 쪽이
          훨씬 나쁘다. 카운터를 안 올리면 그건 그대로 429 를 자초한다.
        - **`temperature=0`** (PATTERN-49). 같은 구간 데이터에 매번 다른 병합·정렬이 나오면
          프롬프트 개정의 효과를 측정할 수 없다. dict config 형식은 v6.7.470 라이브 검증분.
        - 락은 2차 요약과 공유한다(`_gemini_lock`) - 보호 대상이 카운터/키 인덱스로 동일하다.
          하루 2회 1콜이라 2차 요약이 밀려도 그 틱만 스킵된다(max_instances=1, 유실 없음).
        """
        if not _HAS_GENAI:
            return None, 'google-genai SDK 미설치'
        if not self.report_instruction:
            return None, 'Config_LLM B2(리포트 프롬프트)가 비어 있음'
        if not self.api_keys:
            return None, 'api_keys 없음 (Config_Settings.GEMINI_API_KEYS)'
        if not self.gemini_test_enabled:
            return None, 'Config_Settings.GEMINI_31_TEST=KILL'

        prompt = f"{self.report_instruction}\n\n[수집된 데이터]\n{data_section}\n"
        last_err = None

        with self._gemini_lock:
            # KST 자정 reset — 2차 요약 경로와 같은 카운터를 공유하므로 여기서도 확인한다.
            today = datetime.datetime.now(KST).date()
            if self.gemini_test_last_reset_date != today:
                self.gemini_counts = {}
                self.gemini_test_last_reset_date = today

            n = len(self.api_keys)
            for model_id, rpd, label in self._all_tiers():
                counter = self.gemini_counts.setdefault(label, {})
                tried_keys = set()
                while True:
                    picked_key = None
                    picked_idx = None
                    for offset in range(n):
                        idx = (self.gemini_test_key_index + offset) % n
                        k = self.api_keys[idx]
                        if k in tried_keys or counter.get(k, 0) >= rpd:
                            continue
                        picked_key, picked_idx = k, idx
                        break
                    if picked_key is None:
                        break   # 이 모델 전 키 소진/시도 완료 → 다음 티어
                    self.gemini_test_key_index = (picked_idx + 1) % n
                    tried_keys.add(picked_key)
                    try:
                        client = _make_client(picked_key)
                        _t0 = time.perf_counter()
                        resp = client.models.generate_content(
                            model=model_id, contents=prompt, config={'temperature': 0})
                        counter[picked_key] = counter.get(picked_key, 0) + 1
                        text = (resp.text or '').strip() if resp is not None else ''
                        if text:
                            logger.info(f"[Gemma4Core] 동향 리포트 생성 {label} "
                                        f"{len(text)}자 / {time.perf_counter() - _t0:.1f}s")
                            return text, label
                        last_err = f'{label} 빈 응답'
                    except Exception as ex:
                        last_err = ex
                        if '429' in str(ex) or 'quota' in str(ex).lower():
                            counter[picked_key] = rpd   # 이 (모델,키) 소진 마킹 → 다음 키/티어
                        logger.warning(f"[Gemma4Core] 동향 리포트 {label} key#{picked_idx} 실패: {ex}")
        return None, f'전 모델 실패 ({last_err})'

    def _send_kakaowork_webhook(self, text, blocks=None):
        """카카오워크 웹훅 전송 (NotificationManager 경유, 기존 ai_processor 설정 공유).
        [v6.6.343] `ai_summary` 카테고리 가드 적용 — 관리자 탭 토글로 OFF 시 스킵.
        """
        try:
            from notification_manager import NotificationManager
            NotificationManager().send_ai_summary(text, blocks)
        except Exception as e:
            logger.warning(f"[Gemma4Core] 카카오워크 전송 실패: {e}")

    def _send_kakaowork_summary(self, game, title, summary, link, content="", date=None):
        """Gemma4 1차 분류 + Gemini 2차 정식 요약 블록 메시지 전송. [v6.7.66~]

        본문 `summary` 는 Gemini 2차 요약 결과 (v6.7.63 파이프라인 분리 이후).
        과거 `gemini_summary` kwarg (Gemini 3.1 A/B test 비교용 'test:' 라인) 은 v6.7.66 에서
        정식 운영 메시지화하면서 제거. 메인 `summary` 자체가 곧 Gemini 결과이므로 별도 라벨 불필요.

        카카오워크 토스트/푸시 알림은 `text` 폴백과 blocks의 첫 요소(header)를 참조하므로,
        양쪽 모두에 요약 본문이 노출되도록 헤더와 description 순서를 요약 우선으로 구성한다.

        [v6.6.487] sentinel 가드 — '[NOT_NOTICE]' / '[FAILED]' / '[DELETED]' 등 sentinel 값은
        호출자 가드를 우회한 경우에도 송신 차단 (이중 안전망).
        """
        if isinstance(summary, str) and summary.startswith('[') and summary.endswith(']'):
            return

        preview_content = ""
        if content:
            clean = str(content).replace('\n', ' ').strip()
            limit = 70
            preview_content = f"{clean[:limit]}..." if len(clean) > limit else clean

        display_time = ""
        if date:
            try:
                clean_date = str(date).replace('T', ' ')
                if len(clean_date) >= 16:
                    display_time = clean_date[:16]
            except (ValueError, IndexError, TypeError):
                pass
        if not display_time:
            display_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')

        # 카카오워크 블록 헤더 text는 20자 제한 — 초과 시 API가 400 bad_request로 거부함
        # 게임 태그가 이미 길면 snippet을 버리고 태그만 싣는다.
        HEADER_MAX = 20
        prefix = f"[{game}] "
        remain = HEADER_MAX - len(prefix)
        if remain >= 4:
            snippet_src = summary.replace('\n', ' ').strip()
            snippet = snippet_src if len(snippet_src) <= remain else f"{snippet_src[:remain-1]}…"
            header_text = f"{prefix}{snippet}"
        else:
            header_text = prefix.strip()[:HEADER_MAX]

        blocks = [
            {"type": "header", "text": header_text, "style": "yellow"},
            {"type": "description", "term": "요약",
             "content": {"type": "text", "text": f"**{summary}**", "markdown": True}, "accent": True},
            {"type": "description", "term": "게임",
             "content": {"type": "text", "text": f"**{game}**", "markdown": True}, "accent": True},
            {"type": "description", "term": "일시",
             "content": {"type": "text", "text": display_time, "markdown": True}, "accent": False},
            {"type": "description", "term": "제목",
             "content": {"type": "text", "text": title, "markdown": True}, "accent": False},
        ]
        if preview_content:
            blocks.append({"type": "description", "term": "내용",
                           "content": {"type": "text", "text": preview_content, "markdown": True},
                           "accent": False})
        blocks.append({"type": "description", "term": "링크",
                       "content": {"type": "text", "text": link, "markdown": True}, "accent": False})
        blocks.append({"type": "divider"})
        blocks.append({"type": "button", "text": "게시글 확인", "style": "default",
                       "action_type": "open_system_browser", "value": link})

        # 푸시 알림 fallback — 요약 본문을 최상위 text로 전달
        text = f"[{game}] {summary}"
        self._send_kakaowork_webhook(text, blocks)

    def _generate_gemini_summaries(self, non_excluded_articles, system_instruction, context_section,
                                   bypass_pace=False, force_summary=False):
        """[v6.7.454] 2차 요약 **직렬화 래퍼**. 실제 로직은 `_generate_gemini_summaries_impl`.

        왜 락을 메서드 안에 두는가: 호출처가 둘이다 - 워커의 배치 경로(v6.7.454 부터 **병렬**)와
        admin 재분석(`web_lib/routes/analysis.py:327`, 요청 스레드). 호출처마다 락을 걸면 하나를
        빠뜨렸을 때 조용히 깨진다(그 결함이 곧 RPD 초과 = PATTERN-43 재발).

        보호 대상은 전부 **예산 가드 그 자체**다: `gemini_counts`(모델별 x 키별 RPD),
        `gemini_test_key_index`(라운드로빈), `_pace_calls`(60분 창 페이싱), `_key_cooldown`/
        `_key_429_streak`(429 백오프), 자정 reset,
        `recent_summaries` 갱신. 레이스가 나면 두 스레드가 같은 키를 골라 상한을 넘기거나
        증가분이 유실된다.

        비용은 사실상 0 - 2차는 analyze=true 인 약 3%만 타고 사이클당 0~1콜이다
        (실측: 사이클 총 소요와 1차 콜 합의 차이가 2초).

        [v6.7.459] `bypass_pace` — 사람이 명시적으로 누른 수동 요약 요청 전용.
        **시간당 페이싱 게이트만** 면제하고 일일 RPD 카운터(`gemini_counts`)는 그대로 증가시킨다.
        즉 총 예산은 자동 파이프라인과 공유하되 배분 순서만 사람에게 양보하는 것이다 - 게다가
        `_pace_allowance` 의 `used_today` 가 이 카운터에서 계산되므로, 수동 호출을 쓴 만큼
        **다음 시간대 자동 상한이 자연히 줄어든다**(별도 정산 로직 불요). 폭주 방어는 여기가 아니라
        호출부(`routes/analysis.py` 의 수동 전용 시간/일일 상한)가 맡는다 - 이 인자는 게이트를
        여는 스위치일 뿐이라 라우트 가드 없이 켜면 PATTERN-43 재발 경로가 된다.
        """
        with self._gemini_lock:
            return self._generate_gemini_summaries_impl(
                non_excluded_articles, system_instruction, context_section,
                bypass_pace=bypass_pace, force_summary=force_summary)

    def _generate_gemini_summaries_impl(self, non_excluded_articles, system_instruction, context_section,
                                        bypass_pace=False, force_summary=False):
        """[Gemini 2차 요약 — v6.7.63~, v6.7.65 폴백 활성] Gemma4 가 analyze=true 분류한 글만 호출.

        응답 포맷: `{"id": N, "summary": "...", "sentiment": "...", "severity": "..."}`.
        각 article 의 `summary` / `sentiment` / `severity` 를 in-place 로 채운다.
        SQLite `ai_analysis` 저장은 이 결과 기반 (Gemma4 결과 X).

        [v6.7.65 폴백 활성]
        모델 우선순위: gemini-3-flash-preview (RPD 20/key) → gemini-2.5-flash (RPD 20/key).
        - 전 키 v3 RPD 여유 있음 → v3 호출.
        - 전 키 v3 모두 소진 → v25 시도 (전 키 v25 RPD 여유 검사).
        - 양 모델 모두 소진 → '(전 모델 RPD 소진)' 메시지.
        runtime 429/quota 예외 시 해당 (모델, 키)에 **지수 백오프 쿨다운**을 걸고 같은 모델의 다음
        키로 폴백한다 (v6.7.618. 종전의 '카운터를 RPD 로 강제 마킹' 은 자정까지 안 풀려 오버플로
        비상구를 하루 종일 닫았다 - 그 경위는 429 처리부 주석).
        sentiment/severity 는 enum 검증 (`_SENTIMENT_ENUM` / `_SEVERITY_ENUM`) 후 기본값 (neutral/info) 으로 fallback.
        """
        _SENTIMENT_ENUM = {'positive', 'negative', 'neutral', 'mixed'}
        _SEVERITY_ENUM = {'critical', 'warning', 'info'}

        if not non_excluded_articles:
            return

        def _stamp_failure(msg):
            for a in non_excluded_articles:
                a['summary'] = msg
                a.setdefault('sentiment', 'neutral')
                a.setdefault('severity', 'info')

        if not self.gemini_test_enabled:
            _stamp_failure('(Gemini 비활성 — Config_Settings.GEMINI_31_TEST=KILL)')
            return

        if not self.api_keys:
            _stamp_failure('(api_keys 없음)')
            return

        # KST 자정 reset — 체인 전 모델 카운터 동시 초기화 [v6.7.446]
        today = datetime.datetime.now(KST).date()
        if self.gemini_test_last_reset_date != today:
            if self.gemini_test_last_reset_date is not None:
                # [v6.7.600] 실제로 쓴 라벨을 그대로 찍는다 - 체인이 동적이라 '오늘 무엇을 썼나'는
                #   카운터 자신이 갖고 있고, 어제 체인이 오늘과 다를 수 있다.
                logger.info("[Gemma4Core] Gemini 자정 reset (" + ', '.join(
                    f"{label} 합계: {sum(counts.values())}"
                    for label, counts in sorted(self.gemini_counts.items())) + ")")
            self.gemini_counts = {}   # [v6.7.447] 오버플로 포함 (호출 직전 setdefault 가 채운다)
            # [v6.7.618] 429 백오프도 같이 푼다. 자정에 RPD 가 리셋되므로 어제 소진으로 벌어둔
            #   쿨다운을 그대로 들고 가면 하루의 첫 시간을 헛되이 버린다.
            self._key_cooldown = {}
            self._key_429_streak = {}
            self.gemini_test_last_reset_date = today

        n = len(self.api_keys)

        # [v6.7.436] 시간당 페이싱 게이트 - 상한 도달 시 호출 없이 대기 스탬프.
        #   스탬프가 '(...)' 형이라 워커(v6.7.205)가 저장을 생략 → 글은 pending 으로 남아 다음 시간대에
        #   자연 재시도된다(유실 없음). 폭주 시 하루치 예산을 몇 시간에 태우던 경로 차단.
        #   [v6.7.447] 게이트에 걸려도 **return 하지 않고 오버플로 티어로 흘린다**. 예산은 희소한
        #   Gemini 를 아끼는 장치지 요약을 멈추는 장치가 아니다 - 멈춤(침묵)은 정상 상태와 구분되지
        #   않아 발견이 늦다(PATTERN-43). 오버플로가 실패하면 그때 기존과 동일한 대기 스탬프가 찍힌다.
        #   [v6.7.459] bypass_pace=True(수동 요청) 면 게이트 자체를 건너뛴다 - 사람이 누른 1건이
        #   시간당 상한에 막혀 오버플로 티어로 떨어지면, 정작 품질이 필요한 경로가 저품질 모델을
        #   타게 된다(수동 요청은 '이 글은 요약할 가치가 있다' 는 명시 판단이므로 체인 상위가 맞다).
        paced_blocked = False
        # [v6.7.618] 스로틀은 '버킷당 1회' 에서 '10분당 1회' 로. 창이 굴러가면 버킷 경계가 없다.
        _LOG_EVERY = 600
        self._pace_daily_exhausted = False   # 아래 스탬프가 사유를 가르는 데 쓴다
        if _GEMINI_PACE_ENABLED and not bypass_pace:
            allow, used, remaining = self._pace_allowance()
            if remaining <= 0:
                # 일일 예산 자체가 0 - 아래 시간당 대기와 문구를 구분해야 워커의 소진 경보('RPD 소진'
                # 술어, _notify_quota_exhausted)가 발화한다. 자정(KST) 카운터 리셋이 유일한 회복 경로.
                self._pace_daily_exhausted = True
                if time.monotonic() - self._pace_blocked_log_at >= _LOG_EVERY:
                    self._pace_blocked_log_at = time.monotonic()
                    logger.warning(f"[Gemma4Core] 일일 Gemini 예산 소진 - {len(non_excluded_articles)}건 "
                                   f"오버플로({GEMINI_OVERFLOW_CHAIN[0][0] if GEMINI_OVERFLOW_CHAIN else '없음'})로 처리 시도")
                paced_blocked = True
            elif used >= allow:
                if time.monotonic() - self._pace_blocked_log_at >= _LOG_EVERY:
                    self._pace_blocked_log_at = time.monotonic()
                    logger.info(f"[Gemma4Core] Gemini 최근 60분 상한 도달 ({used}/{allow}, 일일 잔여 {remaining}) "
                                f" - {len(non_excluded_articles)}건 오버플로로 처리 시도")
                paced_blocked = True

        # non_excluded 만으로 prompt 재구성
        articles_text = ""
        for idx, article in enumerate(non_excluded_articles):
            content_preview = str(article.get('content') or '')[:_SECOND_PASS_CONTENT_CHARS].replace('\n', ' ')
            articles_text += (
                f"[{idx+1}] 게임: {article.get('game', '')} "
                f"| 게시판: {article.get('board_name', '')} "
                f"| 제목: {article.get('title', '')} "
                f"| 내용: {content_preview}\n"
            )

        # [v6.7.460] force_summary — 사람이 명시적으로 요청한 글은 2차의 **거절 경로도 닫는다**.
        #   v6.7.459 는 1차(Gemma4)만 우회했는데, 2차 프롬프트에 "가치 없으면 분석 제외를 넣어라" 가
        #   그대로 남아 있어 라이브 첫 시험에서 '분석 제외(사유: ...)' 가 돌아왔다. 요청 버튼이 겨냥하는
        #   글의 87%가 이미 1차에서 제외 판정된 글이라, 2차까지 같은 판단을 하면 버튼이 무용지물이 된다
        #   (누를 때마다 같은 거절 + 쿼타만 소비). 사람의 클릭 = "이 글은 요약 가치가 있다" 는 명시 판단이므로
        #   그 판단을 모델 판단보다 위에 둔다. 자동 파이프라인은 이 인자를 안 쓰므로 노이즈 필터가 그대로 유지된다.
        _verdict_line = (
            '순서를 반드시 지켜라. **이 글은 운영자가 명시적으로 요약을 요청한 글이다** — '
            '"분석 제외" 를 출력하지 말고, 내용이 사소해 보여도 반드시 실제 내용 요약을 작성하라.'
            if force_summary else
            '순서를 반드시 지키고, 정말 가치가 없는 글은 summary 에 "분석 제외" 를 넣어라.'
        )
        prompt = f"""
        {system_instruction}

        {context_section}

        아래 {len(non_excluded_articles)}개의 글을 분석하여 JSON 형식으로 출력하라.
        각 글에 대해 한 줄 요약 + 감정(sentiment) + 심각도(severity) 를 채워라.
        sentiment 는 positive / negative / neutral / mixed 중 하나, severity 는 critical / warning / info 중 하나.
        {_verdict_line}

        [
            {{"id": 1, "summary": "요약문 또는 분석 제외", "sentiment": "neutral", "severity": "info"}},
            ...
        ]

        [글 목록]
        {articles_text}
        """

        # 모델 우선순위 — (model_id, counter_dict, rpd_limit, label, paced) [v6.7.446/447]
        #   paced=True 인 티어만 시간당 페이싱 카운트를 소비한다. 페이싱에 막힌 사이클에서는
        #   Gemini 티어를 통째로 건너뛰고 오버플로만 시도한다.
        _tiers = ([] if paced_blocked else [(m, r, l, True) for m, r, l in self.resolve_chain()]) \
            + [(m, r, l, False) for m, r, l in GEMINI_OVERFLOW_CHAIN]
        # [v6.7.619] 모델 쿨다운 중인 티어는 건너뛴다. 이게 없으면 위에서 건 쿨다운이 아무것도
        #   안 한다 - 20초 뒤 다음 사이클이 같은 과부하 모델을 다시 치고 또 137초를 태운다.
        _mono_now = time.monotonic()
        _cooled = [l for m, r, l, p in _tiers if self._model_cooldown.get(l, 0.0) > _mono_now]
        if _cooled:
            _tiers = [t for t in _tiers if t[2] not in _cooled]
            logger.info(f"[Gemma4Core] 모델 쿨다운으로 건너뜀: {', '.join(_cooled)}")
        model_plans = [
            (model_id, self.gemini_counts.setdefault(label, {}), rpd, label, paced)
            for model_id, rpd, label, paced in _tiers
        ]

        last_err = None
        for model_id, counter, rpd, label, paced in model_plans:
            # [v6.7.72] 이 모델의 모든 키를 소진할 때까지 retry — 단일 키 429 시 바로 다음 모델로
            # 점프하지 않고 같은 모델의 남은 키 전체 시도. 모든 키 소진되면 그제야 다음 모델 폴백.
            # tried_keys = 이번 batch 한정 — non-quota 에러 시 counter 영구 손상 없이 다른 키 재시도.
            tried_keys = set()
            while True:
                # 사용 가능한 키 round-robin 탐색 (이 모델 한정, 이번 batch 미시도 키 우선)
                picked_key = None
                picked_idx = None
                _now_mono = time.monotonic()
                for offset in range(n):
                    idx = (self.gemini_test_key_index + offset) % n
                    k = self.api_keys[idx]
                    if k in tried_keys:
                        continue
                    if counter.get(k, 0) >= rpd:
                        continue
                    # [v6.7.618] 429 쿨다운 중인 키는 건너뛴다. 종전에는 이 자리를 `counter` 를
                    #   rpd 로 부풀려 표현했는데, 그러면 (a) 자정까지 안 풀리고 (b) 그 부풀린 값이
                    #   `_pace_allowance` 의 `used_today` 로 들어가 시간당 상한까지 깎았다.
                    if self._key_cooldown.get((label, k), 0.0) > _now_mono:
                        continue
                    picked_key = k
                    picked_idx = idx
                    break
                if picked_key is None:
                    # 이 모델 전 키 소진 또는 모두 시도 완료 → 다음 모델로 폴백
                    break
                self.gemini_test_key_index = (picked_idx + 1) % n
                tried_keys.add(picked_key)

                try:
                    test_client = _make_client(picked_key)
                except Exception as ex:
                    last_err = ex
                    logger.warning(f"[Gemma4Core] Gemini {label} client 초기화 실패 key#{picked_idx}: {ex}")
                    continue

                try:
                    # [v6.7.73] F-7 record_ai_latency hook — Gemini 2차 요약 call latency 측정
                    _t0 = time.perf_counter()
                    # [v6.7.436] 페이싱 카운트는 '시도' 기준 - 429 도 쿼타를 소비하므로 성공분만 세면 상한이 샌다.
                    # [v6.7.447] 오버플로 티어(paced=False)는 예산 밖이라 세지 않는다 - 세면 gemma4 호출이
                    #   희소한 Gemini 의 시간당 상한을 갉아먹어 티어 분리의 의미가 사라진다.
                    # [v6.7.459] 수동 요청은 시간 버킷을 **소비하지도 않는다**. 소비시키면 사람이 한 번
                    #   누를 때마다 그 시간대 자동 요약분이 그만큼 줄어 "수동으로 1건 봤더니 자동이
                    #   멈췄다" 가 된다. 총량 공유는 아래 `counter[picked_key] += 1`(일일 RPD)이 이미
                    #   담당하고, 그 값이 다음 시간대 상한 계산에 반영되므로 정산은 자동으로 맞는다.
                    if paced and not bypass_pace:
                        # [v6.7.618] 시각을 쌓는다 - 60분 창이 스스로 밀어낸다(카운터 리셋 불요).
                        self._pace_calls.append(time.monotonic())
                    # [v6.7.699] flash 만 시간 상한 30초 + 단일 시도(`attempts=1`). 오버플로(31b)는 종전대로 무설정.
                    #   **temperature 는 여전히 안 건다**(생성 과제 - PATTERN-49 는 판정 경로만 지목).
                    _cfg2 = None
                    if paced:
                        try:
                            from google.genai import types as _gt
                            _cfg2 = {'http_options': _gt.HttpOptions(
                                timeout=_SECOND_FLASH_TIMEOUT_MS,
                                retry_options=_gt.HttpRetryOptions(attempts=1))}
                        except Exception:
                            _cfg2 = None
                    response = test_client.models.generate_content(model=model_id, contents=prompt, config=_cfg2)
                    try:
                        from web_lib.bg.health_sampler import record_ai_latency as _rec
                        _rec((time.perf_counter() - _t0) * 1000.0)
                    except Exception:
                        pass
                    counter[picked_key] = counter.get(picked_key, 0) + 1
                    # [v6.7.618] 성공 = 이 키가 살아났다. 연속 429 카운터를 풀지 않으면 백오프가
                    #   하루 내내 커진 채로 남아, 낮에 한 번 흔들린 키가 밤까지 2시간 간격이 된다.
                    self._key_429_streak.pop((label, picked_key), None)
                    self._key_cooldown.pop((label, picked_key), None)
                    text = response.text.strip() if response and response.text else ''
                    results = _first_json_array(text, "2차")      # [v6.7.702] 첫 배열만 - 뒤꼬리로 배치를 버리지 않는다
                    if not isinstance(results, list):
                        raise ValueError("응답에서 JSON 배열을 못 읽음")
                    id_to_result = {}
                    for r in results:
                        rid = r.get('id')
                        if not rid:
                            continue
                        raw_sentiment = str(r.get('sentiment') or '').strip().lower()
                        raw_severity = str(r.get('severity') or '').strip().lower()
                        id_to_result[rid] = {
                            'summary': (r.get('summary') or '').strip(),
                            'sentiment': raw_sentiment if raw_sentiment in _SENTIMENT_ENUM else 'neutral',
                            'severity': raw_severity if raw_severity in _SEVERITY_ENUM else 'info',
                        }
                    for i, a in enumerate(non_excluded_articles, 1):
                        rec = id_to_result.get(i)
                        if rec is None:
                            a['summary'] = '(id 매핑 실패)'
                            a.setdefault('sentiment', 'neutral')
                            a.setdefault('severity', 'info')
                            continue
                        a['summary'] = rec['summary']
                        a['sentiment'] = rec['sentiment']
                        a['severity'] = rec['severity']
                        if (rec['summary']
                                and not (rec['summary'].startswith('[') and rec['summary'].endswith(']'))
                                and '분석 제외' not in rec['summary']
                                and '분석제외' not in rec['summary']):
                            # [v6.7.611] **(게임, 요약) 튜플로 쌓는다.** 종전에는 문자열만 쌓아
                            #   꺼낼 때 어느 게임 것인지 알 방법이 없었다(그래서 맥락 블록이
                            #   게임을 섞었다). 소비처는 `_build_context_section` 하나뿐이라
                            #   원소 타입을 바꿔도 파급이 없다(2026-08-25 전수 확인).
                            self.recent_summaries.append(
                                (a.get('game') or '공통', rec['summary'], time.time(),
                                 rec.get('severity') or 'info'))
                            if len(self.recent_summaries) > _RECENT_CAP:
                                self.recent_summaries = self.recent_summaries[-_RECENT_CAP:]
                    used = counter[picked_key]
                    total = sum(counter.values())
                    logger.info(f"[Gemma4Core] Gemini {label} OK key#{picked_idx} ({used}/{rpd}, 전체 {total}/{n * rpd}, {len(non_excluded_articles)} 건)")
                    return
                except Exception as ex:
                    last_err = ex
                    msg = str(ex)
                    # [v6.7.618] 429/quota → **지수 백오프 쿨다운**. 같은 모델 다음 키로 retry.
                    #   종전에는 `counter[picked_key] = rpd` 로 그 키를 자정까지 소진 처리했다.
                    #   RPD 가 큰 티어에서 이것이 무너진다 - 오버플로 `g31` 은 (당시 상수 14,400 - 2026-10-01
                    #   에 실제 한도 1,440 으로 정정) 키 6개가 429 를 한 번씩만 맞아도 86,400/86,400 이 되어 **하루 종일** 비상구가
                    #   닫혔다(2026-08-28 라이브 실측. 1차 분류는 이 카운터를 안 쓰므로 그 값이
                    #   실호출일 수 없다). 게다가 부풀린 카운터가 `used_today` 로 들어가 시간당
                    #   상한까지 깎았다. 지금은 **시도분 1건만 세고**(429 도 쿼타를 먹으니 정직하다)
                    #   재시도를 시간으로 미룬다 - RPM 블립이면 60초에 풀리고 진짜 RPD 소진이면
                    #   연속 실패로 상한까지 올라간다. 어느 쪽이든 자정을 안 기다린다.
                    # 비-quota 에러 → counter 유지, tried_keys 만으로 이번 batch 중복 방지. 다음 batch 때 재시도 가능.
                    if '429' in msg or 'quota' in msg.lower() or 'RESOURCE_EXHAUSTED' in msg:
                        counter[picked_key] = counter.get(picked_key, 0) + 1
                        _ck = (label, picked_key)
                        _streak = self._key_429_streak.get(_ck, 0) + 1
                        self._key_429_streak[_ck] = _streak
                        _wait = min(_KEY_COOLDOWN_MAX_SEC,
                                    _KEY_COOLDOWN_BASE_SEC * (4 ** (_streak - 1)))
                        self._key_cooldown[_ck] = time.monotonic() + _wait
                        logger.warning(f"[Gemma4Core] Gemini {label} key#{picked_idx} 429 - "
                                       f"{_wait:.0f}초 쿨다운(연속 {_streak}회), 같은 모델 다음 키 시도")
                    elif _is_model_scoped(msg) or _is_timeout(ex, msg):
                        # [v6.7.619] **5xx 는 키 문제가 아니라 모델 문제다.** 서버가 그렇게 말한다:
                        #   "This model is currently experiencing high demand"(2026-08-28 실측).
                        #   그런데 종전 코드는 이것을 키 실패와 똑같이 취급해 **같은 모델의 남은
                        #   키를 전부 돌았다** - 도움이 될 수 없는 재시도다(과부하인 것은 모델이고
                        #   키를 바꿔도 같은 모델로 간다). 게다가 SDK 가 이미 그 안에서 5회를
                        #   때린 뒤라(키당 137초 실측) 키 6개 = 모델 하나에 822초를 태웠다.
                        #   지금은 **즉시 다음 모델로 내려간다** - 그쪽은 실제로 다른 백엔드다.
                        #   모델에 짧은 쿨다운을 걸어 20초마다 같은 벽을 다시 치지 않게 한다.
                        # [v6.7.699] 늦게 실패한 **flash** 는(타임아웃 포함) 오래 쉰다 - 다음 사이클이 같은 벽을 또 치지 않게.
                        #   오버플로 31b 는 성공도 100초+ 라 늦음이 신호가 아니고, 30분 잠그면 flash 가 다 잠긴 때 2차가 통째로 선다(리뷰 지적).
                        _took = time.perf_counter() - _t0
                        _cd = _SLOW_MODEL_COOLDOWN_SEC if (paced and _took >= _SLOW_FAIL_SEC) else _MODEL_COOLDOWN_SEC
                        self._model_cooldown[label] = time.monotonic() + _cd
                        logger.warning(f"[Gemma4Core] Gemini {label} 모델측 실패("
                                       f"{'타임아웃' if _is_timeout(ex, msg) else _status_of(msg)}, {_took:.0f}초) - "
                                       f"키 순회 생략하고 다음 모델로, {_cd}초 모델 쿨다운")
                        break
                    else:
                        logger.warning(f"[Gemma4Core] Gemini {label} key#{picked_idx} 실패: {ex} - 같은 모델 다음 키 시도")
                    continue

        # 체인 전 모델 소진 또는 호출 실패 [v6.7.446]
        #   **'RPD 소진' 문자열은 유지할 것** - 워커(ai_gemma4_worker.py:208)가 이 술어로 소진 경보를
        #   발화한다(PATTERN-43 의 '침묵 가시화'). 문구를 바꾸면 경보가 조용히 죽는다.
        usage = ', '.join(f"{label}={sum(self.gemini_counts.get(label, {}).values())}/{n * rpd}"
                          for _m, rpd, label in self._all_tiers())
        # [v6.7.618] **사유를 가른다.** 종전엔 호출을 한 번도 못 한 경우를 전부 'RPD 소진' 으로
        #   찍었고, 워커가 그것을 "일일예산 소진 N건" 으로 보고했다. 2026-08-28 실측에서 그 문구가
        #   틀렸다 - 그때 일일 잔여는 130콜(36%)이었고 진짜 차단자는 **시간당 페이싱 + 오버플로
        #   쿨다운**이었다. 원인을 단정하는 문구는 다음 사람의 첫 삽 방향을 정하므로, 여기서 틀리면
        #   "키를 더 사자" 로 간다(오늘 아침 `ScrapeDown` 이 401 을 단정한 것과 같은 종류).
        #   **'RPD 소진' 술어는 일일 예산이 실제로 0 일 때만** 남긴다 - 그래야 워커의 소진 경보
        #   (`_notify_quota_exhausted`)가 살 데서만 울린다.
        if last_err is None and getattr(self, '_pace_daily_exhausted', False):
            _stamp_failure(f'(전 모델 RPD 소진 {usage})')
        elif last_err is None:
            _stamp_failure(f'(2차 대기: 페이싱/쿨다운 {usage})')
        else:
            _stamp_failure(f'(Gemini 전 모델 호출 실패: {type(last_err).__name__})')
        logger.warning(f"[Gemma4Core] Gemini 전 모델 실패 ({usage}): {last_err}")

    def _run_second_stage(self, non_excluded, context_section):
        """[v6.7.680] 2차 요약 + 알림. 배치 경로와 사이클 flush 경로가 **같은 코드**를 쓴다."""
        if non_excluded:
            try:
                self._generate_gemini_summaries(non_excluded, self.system_instruction, context_section)
            except Exception as gem_err:
                logger.warning(f"[Gemma4Core] Gemini 2차 요약 wrapper 실패: {gem_err}")
                for a in non_excluded:
                    a.setdefault('summary', f'(Gemini wrapper 실패: {type(gem_err).__name__})')
                    a.setdefault('sentiment', 'neutral')
                    a.setdefault('severity', 'info')
        # 알림은 2차 결과가 붙은 뒤에. 분석 제외 sentinel([..])·실패 placeholder((..))는 차단.
        for article in (non_excluded or []):
            summary = article.get('summary', '')
            if (summary
                    and not (summary.startswith('[') and summary.endswith(']'))
                    and not (summary.startswith('(') and summary.endswith(')'))
                    and '분석 제외' not in summary
                    and '분석제외' not in summary):
                try:
                    self._send_kakaowork_summary(
                        article.get('game', ''),
                        article.get('title', ''),
                        summary,
                        article.get('link', ''),
                        article.get('content', ''),
                        article.get('timestamp', ''),
                    )
                except Exception as notify_err:
                    logger.warning(f"[Gemma4Core] 알림 전송 실패: {notify_err}")

    def _local_scores(self, batch_articles):
        """[v6.7.714] imt-fp POST /score -> 점수 목록. **실패·지연·모양 이상이면 None**(호출부가 gemma 1차로 간다).

        None 과 [] 를 섞지 않는다 - 빈 배치는 호출부가 먼저 거른다. 점수는 0~1 유한수여야 하고 개수가 글 수와 같아야 한다.
        """
        import urllib.request
        t0 = time.time()
        try:
            # 본문 생성도 try 안 - 직렬화 실패(이상 문자 등)도 같은 처방(gemma 1차)이다(커밋 전 리뷰 W2).
            #   본문은 1차가 읽는 앞부분만 보낸다 - imt-fp build_text 가 어차피 200자로 자른다.
            #   [v6.7.715] link 도 보낸다 - imt-fp 가 "이 글의 1차는 로컬이 정했다" 를 남겨 판정 탭이 gemma 비교에서 뺀다(제안서 §14-1).
            body = json.dumps({"items": [{"link": a.get('link', ''), "game": a.get('game', ''), "board": a.get('board_name', ''),
                                          "title": a.get('title', ''), "content": str(a.get('content') or '')[:_FIRST_PASS_CONTENT_CHARS]}
                                         for a in batch_articles]}, ensure_ascii=False).encode('utf-8')
            req = urllib.request.Request(FP_SCORE_URL, data=body, headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=LOCAL_FIRST_TIMEOUT_S) as r:
                ps = json.loads(r.read().decode('utf-8')).get('p')
        except Exception as e:   # 네트워크·503(바쁨)·500·타임아웃 - 전부 같은 처방(gemma 1차)이라 한 곳에서 받는다
            logger.warning(f"[Gemma4Core] 로컬 1차 실패({type(e).__name__}: {str(e)[:120]}) - gemma 1차로 폴백 "
                           f"({len(batch_articles)}건, {time.time() - t0:.1f}s)")
            return None
        if (not isinstance(ps, list) or len(ps) != len(batch_articles)
                or not all(isinstance(p, (int, float)) and not isinstance(p, bool) and 0.0 <= p <= 1.0 for p in ps)):
            logger.warning(f"[Gemma4Core] 로컬 1차 응답 이상(개수 {len(ps) if isinstance(ps, list) else type(ps).__name__} / "
                           f"{len(batch_articles)}) - gemma 1차로 폴백")
            return None
        return ps

    def _local_first_pass(self, batch_articles, defer_second):
        """[v6.7.714] 로컬 점수로 1차를 가른다. 처리했으면 True, imt-fp 실패면 False(호출부가 gemma 1차).

        - p >= HI          : 1차 통과 -> 2차 대상(`_gemma_analyze=True` - 2차·알림 경로가 이 플래그를 읽는다)
        - LO <= p < HI     : local_cascade 일 때만 gemma 1차에 묻는다(그 배치 몫만 `_skip_local=True` 로 재진입)
        - 그 밖            : `LOCAL_EXCLUDED_SENTINEL` (gemma '[분석 제외]' 와 같은 neutral/info 기본값)
        """
        t0 = time.time()
        ps = self._local_scores(batch_articles)
        if ps is None:
            return False
        direct, band, dropped = [], [], 0
        for a, p in zip(batch_articles, ps):
            a['_fp_score'] = round(p, 6)
            if p >= LOCAL_FIRST_HI:
                a['_gemma_analyze'] = True
                direct.append(a)
            elif FIRST_PASS_MODE == 'local_cascade' and p >= LOCAL_FIRST_LO:
                band.append(a)
            else:
                a['_gemma_analyze'] = False
                a['summary'] = LOCAL_EXCLUDED_SENTINEL
                a['sentiment'] = 'neutral'
                a['severity'] = 'info'
                dropped += 1
        logger.info(f"[Gemma4Core] 로컬 1차 {len(batch_articles)}건 {time.time() - t0:.1f}s - 통과 {len(direct)} · "
                    f"gemma 구간 {len(band)} · 제외 {dropped} ({FIRST_PASS_MODE})")
        # 직행 몫을 **먼저** 넘긴다 - 구간 몫의 gemma 가 예외를 던져도(구글 장애) 로컬이 이미 통과시킨 글은 막히지 않게(커밋 전 리뷰 W1).
        if direct:
            if defer_second:
                self._deferred_second.extend(direct)
            else:
                self._run_second_stage(direct, self._build_context_section([a.get('game') for a in direct]))
        if band:
            try:
                self._generate_summary_batch(band, defer_second=defer_second, _skip_local=True)
            except Exception as e:   # 구간 글은 summary 가 비어 저장되지 않고 다음 사이클에 다시 잡힌다(유실 아님)
                logger.warning(f"[Gemma4Core] 구간 위임 gemma 1차 실패({type(e).__name__}) - 구간 {len(band)}건은 다음 사이클 재시도")
        return True

    def flush_deferred_second(self):
        """[v6.7.680] 사이클 끝에서 **1회** - 모인 2차 대상을 한 콜로 처리하고 건수를 돌려준다.

        워커가 저장·알림을 하기 **전에** 불러야 한다 - 여기서 summary 가 붙는다.
        맥락 블록은 모인 글 전체의 게임으로 다시 만든다(배치별 맥락을 합치면 중복된다).
        """
        pend, self._deferred_second = self._deferred_second, []
        if not pend:
            return 0
        ctx = self._build_context_section([a.get('game') for a in pend])
        logger.info(f"[Gemma4Core] 2차 통합 호출 {len(pend)}건 (배치별 분할 호출 대체)")
        self._run_second_stage(pend, ctx)
        return len(pend)

    def _generate_summary_batch(self, batch_articles, defer_second=False, _skip_local=False):
        """
        Gemma4(1차 분류) + Gemini(2차 요약) 통합 배치 처리. [v6.7.63~]

        1) Gemma4 호출 — 글마다 `{"id": N, "analyze": true|false}` 분류만 받는다.
           legacy `{"id": N, "summary": "분석 제외"}` 응답은 analyze=false 로 매핑하여 호환.
        2) analyze=true 인 글만 모아 `_generate_gemini_summaries` 호출 — 정식 summary/sentiment/severity 생성.
        3) analyze=false 인 글은 `[분석 제외]` sentinel 로 마킹 (SQLite 재시도 방지용).
        4) 알림 발송 — 2차 결과의 summary 가 곧 메인 본문.

        입력 `batch_articles` 의 각 dict 는 `summary` / `sentiment` / `severity` 가 in-place 로 채워진다.
        """
        if not self.client:
            logger.warning("[Gemma4Core] client=None — 배치 스킵 (API 키/모델 로드 실패 의심)")
            return
        if not batch_articles:
            return

        if not self.system_instruction:
            logger.warning("[Gemma4Core] 시스템 프롬프트 없음 — 배치 스킵 (prompt 로드 실패 의심)")
            return

        # [v6.7.714] 1차 로컬 모드 - 처리했으면 여기서 끝. 실패면(None) 아래 gemma 1차로 그대로 간다.
        #   구간 위임의 gemma 몫은 이 함수를 `_skip_local=True` 로 다시 불러 아래 gemma 경로를 그대로 탄다.
        if FIRST_PASS_MODE != 'gemma' and not _skip_local:
            if self._local_first_pass(batch_articles, defer_second):
                return

        # [v6.7.454] 배치 전용 키/클라이언트 확보 (구 `_switch_api_key()` + 전역 5초 스로틀 대체).
        #   병렬 배치가 서로의 `self.client` 를 갈아끼우던 경로를 제거한다.
        key_idx, client = self._acquire_gemma_client()
        if client is None:
            logger.warning("[Gemma4Core] 1차 클라이언트 확보 실패 — 배치 스킵")
            return

        articles_text = ""
        for idx, article in enumerate(batch_articles):
            content_preview = str(article.get('content') or '')[:_FIRST_PASS_CONTENT_CHARS].replace('\n', ' ')
            articles_text += (
                f"[{idx+1}] 게임: {article.get('game', '')} "
                f"| 게시판: {article.get('board_name', '')} "
                f"| 제목: {article.get('title', '')} "
                f"| 내용: {content_preview}\n"
            )

        context_section = self._build_context_section(
            [a.get('game') for a in batch_articles])

        # [v6.7.63] Gemma4 = 1차 분류기. summary 생성 책임 X, "analyze" yes/no 만 판단.
        # legacy "분석 제외" 문자열 응답도 analyze=false 로 호환.
        prompt = f"""
        {_FIRST_PASS_GUIDE}

        {context_section}

        아래 {len(batch_articles)}개의 글에 대해, 후속 LLM(Gemini) 이 정식 요약/분류할 가치가 있는지만 판단하라.
        가치 있음 = true, 광고/도배/스팸/잡담/단순 인사 등 분석 가치 없음 = false.
        반드시 입력 순서를 지켜 JSON 배열로 출력한다.

        [
            {{"id": 1, "analyze": true}},
            {{"id": 2, "analyze": false}},
            ...
        ]

        [글 목록]
        {articles_text}
        """

        max_retries = 3
        for attempt in range(max_retries):
            try:
                # [v6.7.73] F-7 record_ai_latency hook — Gemma4 1차 분류 call latency 측정
                _t0 = time.perf_counter()
                _fb_origin = None       # [v6.7.697] 폴백으로 받은 답이면 31b 의 원래 오류(축 보존용)
                try:
                    response = client.models.generate_content(
                        model=FIRST_PASS_MODEL,               # [v6.7.698] 기본 26b
                        contents=prompt,
                        config=_first_pass_config(FIRST_PASS_MODEL),   # [v6.7.616] temp 0 + [v6.7.697] thinking
                    )
                except Exception as _e1:
                    _m1 = str(_e1)
                    _quota = '429' in _m1 or 'quota' in _m1.lower() or 'RESOURCE_EXHAUSTED' in _m1
                    if not (_is_model_scoped(_m1) or (_quota and attempt == max_retries - 1)):
                        raise
                    logger.warning(f"[Gemma4Core] 1차 {FIRST_PASS_MODEL} 실패"
                                   f"({'429' if _quota else _status_of(_m1)}) - "
                                   f"{FIRST_PASS_FALLBACK_MODEL} 폴백")
                    try:
                        response = client.models.generate_content(
                            model=FIRST_PASS_FALLBACK_MODEL,
                            contents=prompt,
                            config=_first_pass_config(FIRST_PASS_FALLBACK_MODEL),
                        )
                    except Exception as _e2:
                        logger.warning(f"[Gemma4Core] 1차 폴백도 실패: {str(_e2)[:120]} - 원래 오류로 처리")
                        raise _e1
                    _fb_origin = _e1
                _el = time.perf_counter() - _t0
                try:
                    from web_lib.bg.health_sampler import record_ai_latency as _rec
                    _rec(_el * 1000.0)
                except Exception:
                    pass
                # [v6.7.453] 계측 계기 - 1차 콜 소요를 **로그에** 남긴다. record_ai_latency 는 health
                #   샘플로만 들어가 사후 조회가 안 되고, 지금까지 "배치 N건에 몇 초 걸리나" 를 로그
                #   타임스탬프 차이로 손으로 세고 있었다(정확도·표본 모두 부족).
                #   이 값이 있어야 처방(배치 병렬화 / BATCH_SIZE 조정 / 사이클 캡 상향)의 순이득을
                #   계산할 수 있다 - 배치를 키웠을 때 콜 시간이 선형으로 늘면 순이득이 0이다.
                logger.info(f"[Gemma4Core] 1차 분류 {len(batch_articles)}건 {_el:.1f}s (key#{key_idx})"
                            + (f" [{FIRST_PASS_FALLBACK_MODEL} 폴백]" if _fb_origin is not None
                               else f" [{FIRST_PASS_MODEL}]"))
                text = response.text.strip() if response and response.text else ""
                results = _first_json_array(text, "1차")
                if results is None and _fb_origin is None:
                    # [v6.7.702] **주력 답을 못 읽으면 폴백 모델에 한 번 더 묻는다.** temperature 0 이라 같은 모델에 다시 물으면
                    #   같은 답이 온다 - 종전엔 그 배치가 다음 사이클에도 똑같이 실패했다(글이 계속 미판정으로 남는다).
                    logger.warning(f"[Gemma4Core] 1차 {FIRST_PASS_MODEL} 응답 파싱 실패({len(text)}자) - "
                                   f"{FIRST_PASS_FALLBACK_MODEL} 폴백")
                    try:
                        _r2 = client.models.generate_content(
                            model=FIRST_PASS_FALLBACK_MODEL, contents=prompt,
                            config=_first_pass_config(FIRST_PASS_FALLBACK_MODEL))
                        results = _first_json_array(_r2.text.strip() if _r2 and _r2.text else "", "1차")
                    except Exception as _e3:
                        logger.warning(f"[Gemma4Core] 1차 파싱 폴백도 실패: {str(_e3)[:120]}")
                if not isinstance(results, list):
                    # [v6.7.697] 폴백 답을 못 읽었으면 파싱 실패가 아니라 첫 모델의 원래 실패로 올린다.
                    if _fb_origin is not None:
                        raise _fb_origin
                    raise ValueError(f"1차 응답에서 JSON 배열을 못 읽음 ({len(text)}자)")

                # 1차 순회 — Gemma4 응답으로 _gemma_analyze flag 만 설정. summary/sentiment/severity 는 Gemini 가 채움.
                # [v6.7.610] **모델이 답한 id 만 확정한다.** 아래 누락 처리의 근거가 여기 모인다.
                answered = set()
                for res in results:
                    idx = (res.get('id') or 0) - 1
                    if not (0 <= idx < len(batch_articles)):
                        continue
                    answered.add(idx)
                    article = batch_articles[idx]
                    raw_analyze = res.get('analyze')
                    if isinstance(raw_analyze, bool):
                        analyze = raw_analyze
                    elif isinstance(raw_analyze, str):
                        analyze = raw_analyze.strip().lower() in ('true', '1', 'yes', 'y')
                    else:
                        # legacy 호환: summary 필드에 "분석 제외" 가 있으면 false, 그 외 (요약문 본문 등) 는 true
                        legacy_summary = str(res.get('summary', '') or '').strip()
                        if legacy_summary and ('분석 제외' in legacy_summary or '분석제외' in legacy_summary):
                            analyze = False
                        elif legacy_summary:
                            analyze = True
                        else:
                            analyze = False
                    article['_gemma_analyze'] = analyze

                # [v6.7.610] **답 안 한 id 를 '분석 제외' 로 확정하지 않는다.**
                #   구 구현은 `setdefault(..., False)` 로 누락분을 analyze=False 로 내리고, 그
                #   아래에서 `'[분석 제외]'` 를 찍었다. 그 문자열은 `(...)` 형이 아니라
                #   **저장된다**(`ai_gemma4_worker` 의 v6.7.205 가드는 괄호형만 거른다) →
                #   `ai_analysis` 에 link 가 박히고 `a.link IS NULL` dedup 때문에 **그 글은
                #   영영 다시 요약되지 않는다.** 주석은 "재시도 방지" 라고 적었지만 실제로 막은
                #   것은 재시도가 아니라 **요약 자체**였다.
                #   배치 10건 중 6건만 돌려주는 부분 응답은 실재하고(AME 수집기 v0.12.6 이 같은
                #   결함을 먼저 잡았다), 그때 빠진 4건은 **판정이 아니라 미판정**이다. 괄호형
                #   스탬프를 찍어 저장을 건너뛰면 `a.link IS NULL` 이 유지돼 다음 사이클이 다시
                #   집는다 - 이미 있는 '일과성 실패' 경로를 그대로 쓴다(새 상태를 만들지 않는다).
                missing = [i for i in range(len(batch_articles)) if i not in answered]
                if missing:
                    logger.warning(
                        f"[Gemma4Core] 1차 응답 누락 {len(missing)}/{len(batch_articles)}건 - "
                        f"미판정으로 남긴다 (저장 생략, 다음 사이클 재시도)")
                    for i in missing:
                        batch_articles[i]['summary'] = '(1차 응답 누락 - 다음 사이클 재시도)'
                        batch_articles[i]['sentiment'] = 'neutral'
                        batch_articles[i]['severity'] = 'info'

                # analyze=False 글 - '[분석 제외]' sentinel + neutral/info 기본값으로 마킹.
                #   **답을 받은 것만 대상**이다(위 `answered`) - 여기가 영구 제외를 만드는 자리다.
                for i in answered:
                    article = batch_articles[i]
                    if not article.get('_gemma_analyze'):
                        article['summary'] = '[분석 제외]'
                        article['sentiment'] = 'neutral'
                        article['severity'] = 'info'

                # 2차 — Gemini 정식 요약 (analyze=true 만)
                non_excluded = [a for a in batch_articles if a.get('_gemma_analyze')]
                if defer_second:
                    # [v6.7.680] **사이클 단위로 모아 한 콜로 보낸다.** 2차 flash 는 건수가 아니라
                    #   **콜 수(RPD 20/키/모델)** 로 과금되는데, 종전에는 배치마다 따로 불러
                    #   사이클당 4콜 x 3건 을 썼다. 그 4콜은 `_gemini_lock` 으로 **어차피 직렬**이라
                    #   합쳐도 벽시계 손해가 없고 RPD 만 1/4 이 된다(같은 모델·같은 프롬프트라
                    #   품질 변화 0). 모으는 쪽은 워커가 `flush_deferred_second()` 로 닫는다.
                    self._deferred_second.extend(non_excluded)
                    return
                self._run_second_stage(non_excluded, context_section)
                return

            except Exception as e:
                msg = str(e)
                if "429" in msg or "quota" in msg.lower():
                    # noqa: em dash intentional - 이 문구는 v6.7.659 리뷰가 **byte-identical**
                    #   을 확인한 기존 라이브 로그다. ' - ' 로 바꾸면 문구가 달라진다.
                    logger.warning(f"[Gemma4Core] 429 쿼타 key#{key_idx} — 키 교체 후 재시도 "
                                   f"({attempt+1}/{max_retries}){_quota_brief(msg)}")
                    time.sleep(10)
                    # [v6.7.454] 구 `self._switch_api_key()` 는 **공유** client 를 갈아끼워, 병렬 배치
                    #   상황에서 남의 진행 중인 호출까지 키를 바꿔버렸다. 이 배치의 로컬 클라이언트만 교체.
                    key_idx, client = self._acquire_gemma_client()
                    if client is None:
                        logger.warning("[Gemma4Core] 429 재시도용 클라이언트 확보 실패 — 배치 중단")
                        return
                    continue
                logger.error(f"[Gemma4Core] 배치 처리 오류: {e}")
                raise

