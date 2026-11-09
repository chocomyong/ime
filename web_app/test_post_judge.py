# -*- coding: utf-8 -*-
"""[v6.7.665] bg/post_judge 의 **LLM 밖 경로**를 태운다 - 후보 조회 SQL · 저장 · 실패 카운트 ·
응답 파싱/정규화.

LLM 콜 자체는 여기서 안 친다(쿼타·비결정성). 대신 **모델 응답을 픽스처로 주입**해
`_judge_batch` 의 파싱·정규화 경로를 태운다 - 버그가 사는 자리가 거기다(v6.7.467 언패킹 사고,
v6.7.641 파싱 예외 오분류 둘 다 이 구간이었다).

프롬프트 품질은 이 파일이 채점하지 않는다. 그건 라이브 실물 채점의 몫이다
(`_workspace/2026-09-15_공식게시판_제재판정.md` 의 홀드아웃 결과).
"""
import os, sys, tempfile, shutil, sqlite3

sys.path.insert(0, os.path.abspath('.'))
sys.path.insert(0, os.path.abspath('utils'))
_tmp = tempfile.mkdtemp(prefix='pjprobe_')
os.environ['IMT_DISABLE_BOOTSTRAP'] = '1'
# DB_DIR 을 import 전에 박는다 - SQLiteHandler 는 싱글턴이라 나중에 경로 인자를 줘도
# 조용히 무시되고 개발 DB 에 쓴다(2026-09-15 에 실제로 겪었다, PATTERN-80 과 같은 모양).
os.environ['DB_DIR'] = _tmp

from utils.sqlite_handler import db_handler
assert os.path.dirname(db_handler.db_path) == _tmp, '격리 실패: %s' % db_handler.db_path
from web_lib.bg import post_judge as pj

fails = []
def ck(name, cond, detail=''):
    print('  [%s] %s %s' % ('PASS' if cond else 'FAIL', name, detail))
    if not cond: fails.append(name)

conn = pj._connect_rw()
def add(link, board, title, days_ago, content='본문'):
    conn.execute(
        "INSERT OR REPLACE INTO community_posts (game, source, title, content, board_name, date, timestamp, link, is_super_keyword, inserted_at)"
        " VALUES ('배틀그라운드','OFFICIAL_WEB',?,?,?,date('now','localtime',?),"
        " datetime('now','localtime',?),?,0,datetime('now'))",
        (title, content, board, '-%d day' % days_ago, '-%d day' % days_ago, link))

add('P1', '배그 플레이어', '최근 글', 0)
add('P2', '배그 플레이어', '이틀 전 글', 2)
add('P3', '배그 플레이어', '컷오프 밖(9일 전)', 9)          # CUTOFF_DAYS=7 -> 제외
add('P4', '배그 공지',     '운영사 공지', 0)                 # 유저 게시판 아님 -> 제외
add('P5', 'POE2 자유',     'POE 글', 1)
add('P6', '배그 플레이어', '이미 판정된 글', 0)
add('P7', '배그 플레이어', '실패 누적 글', 0)
add('P8', '배그 플레이어', '', 0)                            # 제목 빈 글 -> 제외
conn.commit()
conn.execute("INSERT INTO post_verdicts (link,kind,target,content_hash,judged_at,fail) VALUES ('P6','clean','none','h','t',0)")
conn.execute("INSERT INTO post_verdicts (link,kind,target,content_hash,judged_at,fail) VALUES ('P7',NULL,NULL,NULL,'t',?)", (pj.MAX_FAIL,))
conn.commit()

print('=== 후보 조회 ===')
cands = pj._fetch_candidates(conn)
links = sorted(c[0] for c in cands)
print('   ', links)
ck('컷오프 안 유저게시판만 (P1·P2·P5)', links == ['P1', 'P2', 'P5'], '-> %s' % links)
# **상수를 여기 못박는다 - 픽스처 오프셋을 CUTOFF_DAYS 에서 파생시키면 안 된다.** 둘이 같이
# 움직이면 상수를 바꿔도 테스트가 안 빨개져서, 컷오프가 조용히 틀어져도 초록이다(PATTERN-71).
# 창을 바꾸는 것은 의도적 결정이므로 여기와 위 픽스처 오프셋을 같이 고치는 마찰이 있어야 한다.
ck('컷오프 상수가 7일이다 (바꾸려면 위 픽스처 오프셋도 같이)', pj.CUTOFF_DAYS == 7, '-> %d' % pj.CUTOFF_DAYS)
ck('후보 원소는 (link, title, content) 3-튜플', cands and len(cands[0]) == 3)

print()
print('=== 저장 ===')
n = pj._store(conn, [('P1', 'h1', '홍보', 'none'), ('P2', 'h2', '욕설', '게임사')])
ck('2건 저장', n == 2)
row = conn.execute("SELECT kind,target,content_hash FROM post_verdicts WHERE link='P2'").fetchone()
ck('종류·대상·해시가 그대로 들어간다', tuple(row) == ('욕설', '게임사', 'h2'), '-> %s' % (tuple(row),))
ck('judged_at 은 UTC(현지시각 아님)',
   abs(int(conn.execute("SELECT (julianday('now') - julianday(judged_at))*86400 FROM post_verdicts WHERE link='P2'").fetchone()[0])) < 120)
ck('저장된 글은 후보에서 빠진다', sorted(c[0] for c in pj._fetch_candidates(conn)) == ['P5'])

print()
print('=== 실패 카운트 ===')
pj._bump_fail(conn, ['P5'])
f = conn.execute("SELECT fail, kind FROM post_verdicts WHERE link='P5'").fetchone()
ck('fail 1 증가 + kind 는 NULL(제재 조회에 안 걸림)', tuple(f) == (1, None), '-> %s' % (tuple(f),))
ck('fail < MAX_FAIL 이면 후보에 남는다', [c[0] for c in pj._fetch_candidates(conn)] == ['P5'])
for _ in range(pj.MAX_FAIL - 1): pj._bump_fail(conn, ['P5'])
ck('fail >= MAX_FAIL 이면 후보에서 빠진다 (조용한 무한 재시도 방지)',
   pj._fetch_candidates(conn) == [], '-> %s' % pj._fetch_candidates(conn))
ck('실패 글은 제재 목록에 안 걸린다',
   conn.execute("SELECT COUNT(*) FROM post_verdicts WHERE kind IS NOT NULL AND kind<>'clean'").fetchone()[0] == 2)

print()
print('=== 응답 파싱/정규화 (모델 응답 주입) ===')
class _Part:
    def __init__(self, t, th=False): self.text, self.thought = t, th
class _Content:
    def __init__(self, parts): self.parts = parts
class _Cand:
    def __init__(self, parts): self.content, self.finish_reason = _Content(parts), 'STOP'
class _Resp:
    def __init__(self, parts): self.candidates, self.text = [_Cand(parts)], None
class _Models:
    def __init__(self, payload): self._p = payload
    def generate_content(self, **kw):
        return _Resp([_Part('생각 중...', th=True), _Part(self._p)])
class _Client:
    def __init__(self, payload): self.models = _Models(payload)
class _Core:
    def __init__(self, payload): self._p = payload
    def _acquire_gemma_client(self): return 0, _Client(self._p)

BATCH = [('L1', '클랜 모집', '오세요'), ('L2', 'ㅅㅂ 패치', '운영진아'),
         ('L3', '질문', '어떻게 하나요'), ('L4', '저격', 'xxx 새끼')]

out = pj._judge_batch(_Core(
    '설명을 좀 붙이고 [' +
    '{"id":1,"kind":"홍보","target":"게임사"},'        # 홍보인데 target 이 옴 -> none 으로
    '{"id":2,"kind":"욕설","target":"게임사"},'
    '{"id":3,"kind":"헛소리","target":"none"},'         # 미지의 종류 -> clean
    '{"id":4,"kind":"비방","target":"이상한값"}'        # 미지의 대상 -> 불특정
    '] 끝'), BATCH)
got = {o[0]: (o[2], o[3]) for o in out}
print('   ', got)
ck('JSON 앞뒤 잡텍스트를 견딘다', len(out) == 4)
ck('홍보의 target 은 none 으로 정규화', got.get('L1') == ('홍보', 'none'))
ck('욕설의 target 은 보존', got.get('L2') == ('욕설', '게임사'))
ck('미지의 종류는 clean 으로 강등', got.get('L3') == ('clean', 'none'))
ck('미지의 대상은 불특정으로', got.get('L4') == ('비방', '불특정'))
ck('반환 원소는 (link, hash, kind, target)', out and len(out[0]) == 4 and out[0][0] in got)
ck('해시가 채워진다', all(len(o[1]) == 40 for o in out))

for bad, label in [('JSON 아님', 'JSON 배열 없음'), ('[{"id":1,', '깨진 JSON')]:
    try:
        pj._judge_batch(_Core(bad), BATCH); ck('%s -> 예외' % label, False)
    except pj.PostJudgeParseError:
        ck('%s -> PostJudgeParseError' % label, True)
    except Exception as e:
        ck('%s -> PostJudgeParseError' % label, False, '-> %s' % type(e).__name__)

ck('파싱 실패는 시점 문제가 아니다 (분할 재시도로 가야 한다)',
   pj._is_transient(pj.PostJudgeParseError('x')) is False)
ck('429 는 시점 문제다 (분할 금지, 다음 사이클)',
   pj._is_transient(Exception('429 RESOURCE_EXHAUSTED')) is True)
ck('500 도 시점 문제다', pj._is_transient(Exception('500 INTERNAL')) is True)

print()
print('=== 범위 계약 ===')
ck('act 레인이 4종', set(pj.ACT_KINDS) == {'개인정보', '도배', '불법거래', '홍보'})
ck('욕설·비방은 act 가 아니다 (사람이 맥락 판단)',
   '욕설' not in pj.ACT_KINDS and '비방' not in pj.ACT_KINDS)
ck('공지·패치노트 게시판은 대상 아님',
   not any(b in pj.USER_BOARDS for b in ('배그 공지', 'POE1 공지', 'POE2 패치노트')))
ck('프롬프트가 디코 판정 것과 다르다', '멤버모집' not in pj._PROMPT and '유저 게시판' in pj._PROMPT)

conn.close()
shutil.rmtree(_tmp, ignore_errors=True)
print()
print('RESULT: ' + ('ALL PASS' if not fails else 'FAIL -> ' + ', '.join(fails)))
sys.exit(1 if fails else 0)
