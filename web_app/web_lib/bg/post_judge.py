# -*- coding: utf-8 -*-
"""bg.post_judge - 공식 홈페이지(배그·POE) 유저 게시판 게시물의 삭제 대상 판정.

배그팀 요구(2026-09-15 회의 "삭제해야 될 커뮤니티 글이 방치되고 있다")를 받는 잡이다.
판정 결과는 `post_verdicts` 에 쌓이고 피드탭 제재 토글이 그것을 조회한다.

**축이 7종이고 레인이 둘이다** (사용자 실무 기준 2026-09-15):

    act(조치)  개인정보 · 불법거래 · 홍보 · 도배   -> 예외 없는 삭제 대상
    rev(검토)  욕설 · 비방                      -> 사람이 맥락을 본다
    clean

사용자 원문: *"비방이나 욕설은 게임사의 잘못 등으로 삭제가 안 되는 경우도 많음, 하지만
개인정보나 홍보(자유게시판이라 클랜 홍보는 전부 삭제 대상) 게시물은 전부 삭제 대상"*.
그래서 **이 잡은 탐지만 한다** - 욕설/비방에 "지워라/두어라" 를 시키지 않는다. 그 판단에
필요한 맥락(게임사 귀책 여부)이 게시물 본문 밖에 있기 때문이다. 대신 `target` 으로
지목 대상을 내서 사람이 그 예외를 3초에 거르게 한다.

**디스코드 판정(bg/discord_judge.py)과 프롬프트를 공유하지 않았다.** 그쪽은 "배틀그라운드
멤버모집 채널" 전용이고 3단계가 채널 은어 화이트리스트였다 - 홈페이지 게시물에 쓰면 판정 근거
자체가 무관하다. 인프라(클라이언트·키 로테이션)는 요약 코어(`ai_gemma4_worker._get_core`)를 빌려 쓴다.
[v6.7.742] 디코 판정 잡은 삭제됐고, 그쪽에서 빌려 쓰던 에러 축 분류(`_is_transient`)는 이 파일로 옮겼다.
"""
import hashlib
import logging
import re
import sqlite3
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# 판정 대상 게시판 - **유저가 쓰는 곳만.** 공지·소식·패치노트·이벤트는 운영사 글이라
#   삭제 대상 판정이 무의미하고 쿼타만 태운다(30일 실측 1,310건 중 143건이 그쪽).
#   'gm' 은 사용자 지시로 포함(2026-09-15: "배그 GM 게시판은 gm이 쓰는 곳이긴한데 그냥 대상으로 올려").
USER_BOARDS = ('배그 플레이어', '배그 gm',
               'POE1 자유', 'POE1 도움말', 'POE1 문의',
               'POE2 자유', 'POE2 도움말', 'POE2 문의')

KINDS = ('개인정보', '도배', '불법거래', '홍보', '욕설', '비방', 'clean')
TARGETS = ('게임사', '특정인', '불특정', 'none')
ACT_KINDS = ('개인정보', '도배', '불법거래', '홍보')

# **컷오프는 '최근 N일' 이지 '부팅 시각' 이 아니다.** 디코 판정은 컷오프가 프로세스 기동
#   시각이라 재시작 순간 미판정 백로그가 영구 유실된다(v6.7.488, 실측 3,500건). 여기는
#   게시물이라 하루 유입이 수십 건뿐이고 배포가 잦으므로 그 규약을 쓰면 안 된다.
#   요약 워커의 '부팅-6h' 와 같은 정신이되 창을 더 넓게 잡았다(사용자 지시 2026-09-15).
# [v6.7.668] 3 -> 7. 사용자 지시(2026-09-15 "7일치도 마저 판정해줘") - 리더 보고용 표본을
#   넓힌다. 별도 백필 스크립트로 안 하는 이유: `imt-diag exec` 로 띄우는 일회성 프로세스는
#   웹앱 모듈을 통째로 다시 로드해 무겁고, 실제로 **exit 137(강제 종료)로 죽었다**(2026-09-15).
#   이미 떠 있는 프로세스 안에서 도는 잡이 그 위험이 없다. 판정 끝난 글은 후보에서 빠지므로
#   창을 넓혀도 정상 구간의 비용은 0 이다(조회 한 번이 늘 뿐).
CUTOFF_DAYS = 7

BATCH_CAP = 10          # 10건 x 1200자 = 약 7,300 입력토큰. 키당 분당 16,000 안.
FETCH_CAP = 60          # 사이클당 후보 조회 상한
CONTENT_CAP = 1200      # 제목+본문 p99 = 3,762자. 1200 이면 약 93%가 온전히 들어간다.
CYCLE_SEC = 120
TEMPERATURE = 0         # 판정은 분류 과제다. 기본 샘플링이면 같은 문장이 회차마다 뒤집힌다(v6.7.470 실측).
MAX_OUTPUT_TOKENS = 16384
MAX_SPLIT_DEPTH = 2
DAILY_CALL_CAP = 400    # 하루 유입이 약 64건이라 배치 10 기준 7콜이면 충분하다. 폭주 상한.
MAX_FAIL = 3            # 같은 글에서 이만큼 실패하면 영구 제외 (조용한 무한 재시도 방지)

_calls_today = 0
_calls_day = None

# [v6.7.666] 프롬프트 개정 1차 - 라이브 검수에서 나온 오류 3종을 막는다(경위는 CHANGELOG).
_PROMPT = """너는 게임 공식 홈페이지 유저 게시판(배틀그라운드 · POE) 의 게시물 관제자다.
아래 글들을 읽고 각각이 운영정책상 삭제 검토 대상인지 분류한다.

이 게시판은 30일 1,167건 기준으로 홍보가 약 6%, 욕설·비방이 약 5%, 도배가 약 1.5% 이고
나머지 대다수는 게임에 대한 불만·질문·잡담이다. 기본값은 clean 이다.
아래 단계에 명시적으로 걸리지 않으면 clean 으로 둔다.

0단계 - 개인정보 (예외 없음)
  타인의 신원·연락 정보가 본문에 드러나면 "개인정보".
  해당: 실명, 전화번호, 주소, 이메일, 계좌번호, 주민번호, 재직처·학교 특정,
        타인의 카카오톡 ID·디스코드 태그를 당사자 동의 없이 공개
  해당 아님: 게임 내 닉네임·전적 링크. 게임 커뮤니티의 공개 식별자이므로
             그 자체로는 개인정보가 아니다. (닉네임 지목 공격은 4단계에서 본다)
  본인의 연락처를 스스로 올린 것은 개인정보가 아니라 3단계(홍보)로 본다.
  참고: 이 게시판에서 실제 개인정보 노출은 30일에 1건 정도로 매우 드물다.

1단계 - 도배
  내용이 없는 글. 아래 중 하나면 "도배".
   - 의미 없는 문자·기호·이모지의 나열(예: 막대 그래프 모양 아스키, 자음만 늘어놓은 난타)
   - 같은 글자·문장부호의 반복(예: 물음표나 마침표만 수십 개)
   - 같은 문장을 세 번 이상 반복해 길이만 늘린 글
   - 한두 글자만 있어 뜻이 통하지 않는 글
  해당 아님: 짧아도 뜻이 통하는 글(예: "랜쿼드", "ㅇㅇ 맞음"). 제목에 기호가 섞여 있어도
             본문이 실제 내용이면 도배가 아니다.

2단계 - 불법거래
  핵·치트·매크로의 판매·배포·사용법 안내, 계정/클랜의 거래·양도, 대리(랭크 대리 등),
  현금 거래 유도. 걸리면 "불법거래".
  **주의**: 이 게시판에 실제 판매글은 30일에 0건이다. "핵" 이라는 단어는 대부분
  핵 사용자를 욕하거나 제보하는 글에 나온다. 파는 행위·사는 행위가 글에 있어야 한다.
  단순히 핵을 언급·비난·제보하는 글은 불법거래가 아니다(5단계 참조).

3단계 - 홍보 (예외 없음)
  클랜·길드·스쿼드 모집, 디스코드 서버 유치, 개인 방송·유튜브·카페·외부 커뮤니티
  유치, 상업 광고. 걸리면 "홍보".
  이 게시판은 자유게시판이고 모집 전용 채널이 아니다. 친목 목적이어도 홍보다.
  제목이 모집과 무관해 보여도 본문이 모집이면 홍보다.
  해당 아님: 클랜 기능에 대한 문의·버그 신고, 운영사가 공지한 대회·이벤트 안내.
             **게임에 접속하라는 권유는 홍보가 아니다** - "다들 들어오세요", "인증하면 됩니다"
             처럼 접속 문제 해결을 알리는 글은 clean 이다(클랜·외부 커뮤니티 유치가 아니다).

4단계 - 욕설 / 비방
  명시적 욕설어가 있으면 "욕설". 욕설어 없이 대상을 깎아내리면 "비방".
  그리고 지목 대상을 함께 낸다:
    "게임사"  - 게임사·운영진·개발사·특정 업데이트를 향한 것
    "특정인"  - 특정 유저나 특정 클랜을 향한 것(게임 내 닉네임 지목 포함)
    "불특정"  - 대상이 특정되지 않은 것(핵쟁이 일반, 특정 구간 유저 일반 등)
  **사람 이름이나 게임 닉네임이 지목돼 있으면 "특정인" 이다** - 글이 게임 얘기 중에 나왔다는
  이유로 "게임사" 로 내지 말 것. 대상은 글이 누구를 향하는지로 정한다.
  강조 부사로만 쓰인 비속어(존나·개·미친 등)만 있고 공격 대상이 없으면 clean 이다.

5단계 - 게임 비판과 신고는 clean
  게임·업데이트·확률·서버 상태에 대한 강한 불만, 항의, 조롱 섞인 표현은
  욕설어가 없는 한 clean 이다. 환불 요구·이탈 선언도 clean 이다.
  핵 의심 제보, 피해 공유, 버그 신고, 해결법 공유도 clean 이다.
  단 특정 닉네임을 지목하며 인신공격이나 욕설이 섞이면 4단계로 본다.

우선순위: 0 > 1 > 2 > 3 > 4. 여러 개에 걸리면 앞선 단계로 낸다.

출력은 JSON 배열만. 설명 금지.
[{"id":번호,"kind":"개인정보|도배|불법거래|홍보|욕설|비방|clean","target":"게임사|특정인|불특정|none"}]
target 은 kind 가 욕설·비방일 때만 채우고 나머지는 "none".

"""


class PostJudgeParseError(ValueError):
    """응답이 JSON 배열이 아니다.

    **일반 ValueError 로 두면 안 된다.** 이 문구에는 숫자가 들어가고(`답변 1500자`,
    `column 2503`), 에러를 문자열로 분류하는 축이 그 숫자를 HTTP 코드로 읽는다.
    디코 판정이 v6.7.641 에서 정확히 그렇게 데였다(`503` 오인 -> 무한 재전송).
    """


def _db_path():
    from sqlite_handler import db_handler
    return db_handler.db_path


def _connect_rw():
    conn = sqlite3.connect(_db_path(), timeout=30)
    try:                                            # [v6.7.700] 메인 DB 쓰기 락 보유 계측(sqlite_handler)
        from sqlite_handler import instrument_txn
        instrument_txn(conn, "post_judge")
    except Exception:
        pass
    conn.row_factory = sqlite3.Row
    return conn


def _hash(title, content):
    return hashlib.sha1(((title or '') + (content or '')).encode('utf-8', 'ignore')).hexdigest()


def _budget_ok():
    """일일 콜 상한. **실패 콜도 센다** - 실패가 무료가 아니기 때문이다."""
    global _calls_today, _calls_day
    today = datetime.now().strftime('%Y-%m-%d')
    if _calls_day != today:
        _calls_day, _calls_today = today, 0
    return _calls_today < DAILY_CALL_CAP


def _answer_text(resp):
    """답변 파트만 뽑는다 (thinking 파트 제외).

    `gemma-4-31b-it` 는 응답을 `Part(thought=True)` + `Part(답변)` 으로 준다.
    `resp.text` 는 SDK 버전·파트 구성에 따라 None 이 되거나 thinking 을 섞는다.
    """
    try:
        parts = (resp.candidates or [])[0].content.parts or []
        joined = "".join((p.text or "") for p in parts if not getattr(p, "thought", False)).strip()
        if joined:
            return joined
    except Exception:
        pass
    try:
        return (resp.text or "").strip()
    except Exception:
        return ""


def _finish_reason(resp):
    try:
        return getattr((resp.candidates or [])[0], "finish_reason", None)
    except Exception:
        return None


# [v6.7.742] 아래 분류기 본문은 디코 판정(`bg/discord_judge.py`, 이 버전에서 삭제)의 `_is_transient` 에서 **그대로**
#   옮겨 왔다. 종전엔 그 함수를 함수 안에서 import 해 빌려 썼는데, 모듈만 지우면 import 가 실패해 종전 `except`
#   폴백(맨 숫자 '502'·'504' 까지 시점 문제로 봄)으로 **조용히 판정이 바뀐다**(PATTERN-63 - import 는 통과하고
#   판정 잡이 돌 때만 갈린다). 그래서 위임 경로의 동작을 이 파일로 옮기고, 한 번도 돌지 않던 폴백은 지웠다.
#   동작 동일은 `test_removed_discord_judge_v742.py` [5] 가 실호출 표로 잰다.
#   **목록·정규식의 근거**(v6.7.610 / v6.7.641, 경위는 CHANGELOG):
#   - 타입 이름 = SDK·requests 가 던지는 일시적 예외들. 우리 파싱 실패는 타입(`PostJudgeParseError`)으로 먼저 거른다.
#   - 맨 숫자 코드는 **단어 경계로** 본다 - `답변 1500자` · `column 2503` 같은 우리 문구의 숫자를 쿼타·서버
#     코드로 읽으면 배치 결함이 '시점 문제' 가 되어 같은 배치가 쿨다운마다 재전송된다(디코 판정이 v6.7.641 에서 데였다).
#   - 502·504 는 여기서 시점 문제가 **아니다**(옮기기 전과 같다). 넓히려면 그 자체로 동작 변경이니 따로 결정할 것.
_TRANSIENT_NAMES = ("ServerError", "ConnectionError", "ConnectTimeout", "ReadTimeout",
                    "Timeout", "DeadlineExceeded", "ServiceUnavailable")
_TRANSIENT_CODE_RE = re.compile(r"(?<!\d)(429|500|503)(?!\d)")
_TRANSIENT_TEXT = ("resource_exhausted", "quota", "rate limit", "rate_limit",
                   "unavailable", "internal error", "deadline", "overloaded")


def _is_transient(e):
    """시점 문제(쿼타·서버)인가 - 그렇다면 분할하지 않고 다음 사이클에 맡긴다.

    파싱 실패(`PostJudgeParseError`)는 문구에 숫자가 있어도 배치 결함이다(분할 재시도로 간다).
    """
    if isinstance(e, PostJudgeParseError):
        return False
    if any(n in type(e).__name__ for n in _TRANSIENT_NAMES):
        return True
    s = str(e).lower()
    return bool(_TRANSIENT_CODE_RE.search(s)) or any(t in s for t in _TRANSIENT_TEXT)


def _candidate_where():
    """후보 술어(`p` = community_posts, `v` = post_verdicts LEFT JOIN). 바인드 값은 USER_BOARDS.

    `timestamp` 는 KST naive ISO 이고 컨테이너 TZ 가 Asia/Seoul 이라 `localtime` 이 맞다.
    (`inserted_at` 은 UTC 다 - 섞으면 9시간 어긋난다.)

    [v6.7.738] 판정 잡의 후보 조회와 화면의 '판정 대기' 표식(pending_links)이 **이 문자열 하나**를
    쓴다. 화면이 따로 술어를 가졌을 때(공식이고 판정 행이 없으면 대기) 잡이 영영 안 보는 공지·소식·
    패치노트·이벤트 게시판과 7일 지난 글에 점이 상시 찍혔다(PATTERN-73 - 표시 술어가 처리 잡과 어긋남).
    """
    ph = ','.join('?' * len(USER_BOARDS))
    return ("p.source='OFFICIAL_WEB' AND p.board_name IN (%s) "
            "  AND p.timestamp >= date('now','localtime','-%d day') "
            "  AND (v.link IS NULL OR (v.kind IS NULL AND COALESCE(v.fail,0) < %d)) "
            "  AND p.title IS NOT NULL AND TRIM(p.title) <> '' " % (ph, CUTOFF_DAYS, MAX_FAIL))


def _fetch_candidates(conn):
    """최근 CUTOFF_DAYS 일 유저 게시판 중 아직 판정 안 된 글."""
    sql = (
        "SELECT p.link, p.title, p.content FROM community_posts p "
        "LEFT JOIN post_verdicts v ON v.link = p.link "
        "WHERE " + _candidate_where() +
        "ORDER BY p.timestamp DESC LIMIT %d" % FETCH_CAP)
    return [(r['link'], r['title'], r['content']) for r in conn.execute(sql, USER_BOARDS)]


def pending_links(conn, links):
    """주어진 link 중 판정 잡이 아직 판정할 글(= 후보)의 집합 - 화면 '판정 대기' 표식의 정본.

    후보 술어는 `_fetch_candidates` 와 같은 `_candidate_where()` 다. 플래너는 link PK(`IN`) 또는
    `idx_posts_source_ts`(공식 x 최근 7일) 중 하나로 SEARCH 한다 - 둘 다 범위가 작다(PATTERN-70,
    임시 DB 실측은 후자). 메모리에만 있고 아직 DB 에 안 들어간 글은 잡도 못 보므로 대기가 아니다.
    """
    links = [ln for ln in (links or []) if ln]
    out = set()
    for i in range(0, len(links), 900):   # SQLITE_MAX_VARIABLE_NUMBER 아래로
        chunk = links[i:i + 900]
        sql = ("SELECT p.link FROM community_posts p "
               "LEFT JOIN post_verdicts v ON v.link = p.link "
               "WHERE p.link IN (%s) AND " % ','.join('?' * len(chunk)) + _candidate_where())
        out.update(r[0] for r in conn.execute(sql, tuple(chunk) + USER_BOARDS))
    return out


def _store(conn, rows):
    if not rows:
        return 0
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
    conn.executemany(
        "INSERT OR REPLACE INTO post_verdicts (link, kind, target, content_hash, judged_at, fail)"
        " VALUES (?,?,?,?,?,0)",
        [(link, kind, target, h, now) for (link, h, kind, target) in rows])
    conn.commit()
    return len(rows)


def _bump_fail(conn, links):
    """판정 자체가 결함인 글의 실패 횟수를 올린다.

    **시점 문제(429·5xx)에는 부르지 않는다** - 벤더가 나쁜 동안 멀쩡한 글이 영구 제외된다
    (PATTERN-67: 차단기가 묶음 단위라 원인 1건에 묶음 전체가 영구 제외).
    `kind` 를 NULL 로 두므로 제재 조회에는 안 걸리고 후보 조회에서만 빠진다.
    """
    if not links:
        return
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
    for ln in links:
        conn.execute(
            "INSERT INTO post_verdicts (link, kind, target, content_hash, judged_at, fail) "
            "VALUES (?, NULL, NULL, NULL, ?, 1) "
            "ON CONFLICT(link) DO UPDATE SET fail = COALESCE(fail,0) + 1, judged_at = ?",
            (ln, now, now))
    conn.commit()


def _judge_batch(core, batch):
    """gemma-4-31b 1콜. 반환 [(link, hash, kind, target)] 또는 None(키 없음).

    batch 원소는 `(link, title, content)` 3-튜플이고 **인덱스로 꺼낸다 - 언패킹 금지.**
    디코 판정이 후보 튜플에 필드를 하나 더하면서 언패킹 한 줄만 안 고쳐, LLM 콜을 지불한
    **직후** `too many values to unpack` 으로 터진 적이 있다(v6.7.467).
    """
    global _calls_today
    idx, client = core._acquire_gemma_client()
    if client is None:
        return None
    from ai_lib.gemma4_summary_core import GEMMA_MODEL
    body = "\n".join(
        "%d. 제목: %s\n   내용: %s" % (
            i + 1,
            (c[1] or '').replace("\n", " "),
            re.sub(r'\s+', ' ', c[2] or '')[:CONTENT_CAP])
        for i, c in enumerate(batch))
    t0 = time.time()
    resp = client.models.generate_content(
        model=GEMMA_MODEL, contents=_PROMPT + body,
        config={'temperature': TEMPERATURE, 'max_output_tokens': MAX_OUTPUT_TOKENS})
    _calls_today += 1
    text = _answer_text(resp)
    # [v6.7.702] 첫 JSON 배열만 읽는다(뒤꼬리 무시) - `gemma4_summary_core._first_json_array` docstring.
    from ai_lib.gemma4_summary_core import _first_json_array
    parsed = _first_json_array(text, "게시판 판정")
    if parsed is None:
        raise PostJudgeParseError("JSON 배열 없음 (finish=%s, 답변 %d자)"
                                  % (_finish_reason(resp), len(text)))

    out = []
    for o in parsed:
        try:
            i = int(o.get("id", 0)) - 1
        except (TypeError, ValueError):
            continue
        if not (0 <= i < len(batch)):
            continue
        kind = o.get("kind") or "clean"
        target = o.get("target") or "none"
        if kind not in KINDS:
            kind, target = "clean", "none"
        if kind not in ("욕설", "비방"):
            target = "none"
        elif target not in TARGETS:
            target = "불특정"
        row = batch[i]
        out.append((row[0], _hash(row[1], row[2]), kind, target))
    logger.info("[POSTJUDGE] 배치 %d건 / %.1fs / key#%s / 판정 %d건",
                len(batch), time.time() - t0, idx, len(out))
    return out


def _judge_with_split(core, conn, batch, depth=0):
    """실패하면 이등분해 재시도. 반환 [(link, hash, kind, target)].

    **분할은 배치 결함(파싱 실패)에만 듣는다.** 쿼타·서버 축(429/5xx)은 쪼개도 같은 벽에
    부딪히므로 그대로 올려 다음 사이클에 맡긴다 - 디코 판정이 5xx 를 "시점 문제" 로 읽어
    분할이 **라이브에서 한 번도 안 돈** 적이 있다(PATTERN-77, v6.7.649 에서 수정).
    """
    try:
        r = _judge_batch(core, batch)
        return r if r is not None else []
    except Exception as e:
        if _is_transient(e):
            raise                       # 시점 문제 - 다음 사이클
        if depth >= MAX_SPLIT_DEPTH or len(batch) <= 1:
            logger.warning("[POSTJUDGE] 배치 %d건 판정 포기(깊이 %d): %s", len(batch), depth, e)
            _bump_fail(conn, [c[0] for c in batch])
            return []
        mid = len(batch) // 2
        logger.info("[POSTJUDGE] 판정 실패(%d건) - %d/%d 로 쪼개 재시도(깊이 %d): %s",
                    len(batch), mid, len(batch) - mid, depth + 1, str(e)[:80])
        out = []
        for half in (batch[:mid], batch[mid:]):
            if half:
                out += _judge_with_split(core, conn, half, depth + 1)
        return out


def _cycle_impl():
    from web_lib.ai_gemma4_worker import _get_core
    core = _get_core()
    if core is None:
        return
    conn = _connect_rw()
    try:
        cands = _fetch_candidates(conn)
        if not cands:
            return
        stored = 0
        for i in range(0, len(cands), BATCH_CAP):
            if not _budget_ok():
                logger.warning("[POSTJUDGE] 일일 콜 상한(%d) 도달 - 이번 사이클 중단", DAILY_CALL_CAP)
                break
            try:
                stored += _store(conn, _judge_with_split(core, conn, cands[i:i + BATCH_CAP]))
            except Exception as e:
                # 시점 문제 - 남은 배치도 같은 벽에 부딪힌다. 사이클을 접고 다음에 다시.
                logger.info("[POSTJUDGE] 사이클 중단(시점 문제): %s", str(e)[:120])
                break
        if stored:
            flagged = conn.execute(
                "SELECT COUNT(*) FROM post_verdicts WHERE kind IS NOT NULL AND kind <> 'clean'"
            ).fetchone()[0]
            logger.info("[POSTJUDGE] 저장 %d건 / 대기 %d건 / 누적 제재 %d건",
                        stored, max(0, len(cands) - stored), flagged)
    finally:
        conn.close()


def run_post_judge_cycle():
    try:
        _cycle_impl()
    except Exception as e:
        logger.error("[POSTJUDGE] 사이클 예외: %s", e, exc_info=True)


def stats():
    """진단용. 화면·로그가 '돌고 있다' 를 주장할 때 근거로 쓴다."""
    conn = _connect_rw()
    try:
        pending = len(_fetch_candidates(conn))
        by = {r[0] or '(실패)': r[1] for r in conn.execute(
            "SELECT kind, COUNT(*) FROM post_verdicts GROUP BY kind")}
        return {'calls_today': _calls_today, 'cap': DAILY_CALL_CAP,
                'pending': pending, 'by_kind': by, 'cutoff_days': CUTOFF_DAYS}
    finally:
        conn.close()
