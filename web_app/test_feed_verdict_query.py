"""[v6.7.664] get_latest_posts(source=, flagged=) 새 경로를 실제로 태운다.

PATTERN-66: "에러가 없다" 는 증거가 아니다 - 새 경로에 데이터를 넣고 출력을 인용한다.
PATTERN-71: 픽스처가 판정 입력을 손으로 채우지 않는다 - 조회는 운영과 같은 함수를 부른다.
"""
import os, sys, tempfile, shutil

sys.path.insert(0, os.path.abspath('.'))
sys.path.insert(0, os.path.abspath('utils'))

_tmp = tempfile.mkdtemp(prefix='imtprobe_')
os.environ['IMT_DISABLE_BOOTSTRAP'] = '1'
# **DB_DIR 을 import 전에 박는다.** SQLiteHandler 는 싱글턴이고 모듈 import 시점에 이미
# 인스턴스가 만들어지므로, 나중에 `SQLiteHandler(db_name=...)` 를 불러도 `_initialized` 가
# True 라 __init__ 이 조기 반환하고 **경로 인자가 조용히 무시된다**. 실제로 이 하네스의
# 첫 판이 그 경로로 개발 DB(web_app/crawled_data.sqlite)에 픽스처를 썼다(2026-09-15).
# 테스트가 진짜 상태를 건드리는 것은 PATTERN-80 과 같은 모양이라 env 로 못 박는다.
os.environ['DB_DIR'] = _tmp

from utils.sqlite_handler import SQLiteHandler

h = SQLiteHandler()
assert os.path.dirname(h.db_path) == _tmp, \
    '격리 실패 - 개발/라이브 DB 를 가리키고 있다: %s' % h.db_path

conn = h._get_connection()

# 라이브와 같은 모양의 픽스처 - 소스 4종 + 판정 5종(+clean).
POSTS = [
    # (game, source, title, timestamp, link, board_name)
    ('배틀그라운드', 'OFFICIAL_WEB', '클랜원 구합니다 디코 오세요', '2026-09-15T12:00:00', 'L1', '배그 플레이어'),
    ('배틀그라운드', 'OFFICIAL_WEB', '핵 판매합니다 카톡 ㄱㄱ',     '2026-09-15T11:00:00', 'L2', '배그 플레이어'),
    ('배틀그라운드', 'OFFICIAL_WEB', '010-1234-5678 로 연락주세요', '2026-09-15T10:00:00', 'L3', '배그 플레이어'),
    ('배틀그라운드', 'OFFICIAL_WEB', '운영진 일 좀 제대로 해라 시발', '2026-09-15T09:00:00', 'L4', '배그 플레이어'),
    ('배틀그라운드', 'OFFICIAL_WEB', 'XXX 저놈 핵쟁이 쓰레기',      '2026-09-15T08:00:00', 'L5', '배그 플레이어'),
    ('배틀그라운드', 'OFFICIAL_WEB', '오늘 업데이트 뭐 바뀜?',       '2026-09-15T07:00:00', 'L6', '배그 플레이어'),
    ('POE1',        'OFFICIAL_WEB', '9월 16일 무점검 패치 안내',    '2026-09-15T06:00:00', 'L7', 'POE1 공지'),
    ('배틀그라운드', 'DC',           '디시 글',                    '2026-09-15T13:00:00', 'L8', '배그 갤'),
    ('배틀그라운드', 'NAVER',        '네이버 글',                  '2026-09-15T13:30:00', 'L9', '카페'),
    ('배틀그라운드', 'DAUM',         '다음 글',                    '2026-09-15T13:40:00', 'L10', '카페'),
]
for g, s, t, ts, ln, bd in POSTS:
    conn.execute(
        "INSERT OR REPLACE INTO community_posts (game, source, title, content, board_name, date, timestamp, link, is_super_keyword, inserted_at)"
        " VALUES (?,?,?,?,?,?,?,?,0,?)", (g, s, t, t, bd, ts[:10], ts, ln, ts))

VERDICTS = [
    ('L1', '홍보', 'none'),
    ('L2', '불법거래', 'none'),
    ('L3', '개인정보', 'none'),
    ('L4', '욕설', '게임사'),
    ('L5', '비방', '특정인'),
    ('L6', 'clean', 'none'),
    # L7~L10 은 판정 없음(NULL) - 판정 대상이 아니거나 아직 안 돈 글
]
for ln, k, tg in VERDICTS:
    conn.execute("INSERT OR REPLACE INTO post_verdicts (link, kind, target, content_hash, judged_at, fail)"
                 " VALUES (?,?,?,?,?,0)", (ln, k, tg, 'h_' + ln, '2026-09-15T05:00:00'))
conn.commit()

def run(label, **kw):
    rows = h.get_latest_posts(limit=100, **kw)
    got = [(r['link'], r.get('verdict_kind'), r.get('verdict_target')) for r in rows]
    print('  %-34s n=%2d  %s' % (label, len(rows), got))
    return rows

print('=== 새 경로 실행 결과 ===')
r_all      = run('전체 (파라미터 없음)')
r_official = run("source='OFFICIAL_WEB'", source='OFFICIAL_WEB')
r_dc       = run("source='DC'", source='DC')
r_flag     = run('flagged=True', flagged=True)
r_of_flag  = run("source='OFFICIAL_WEB' + flagged", source='OFFICIAL_WEB', flagged=True)
r_game     = run("games=['POE1'] (기존 경로 회귀)", games=['POE1'])

print()
print('=== 판정(assert) ===')
fails = []
def ck(name, cond, detail=''):
    print('  [%s] %s %s' % ('PASS' if cond else 'FAIL', name, detail))
    if not cond: fails.append(name)

ck('전체는 10건', len(r_all) == 10, '-> %d' % len(r_all))
ck('공식만 7건', len(r_official) == 7 and all(x['source'] == 'OFFICIAL_WEB' for x in r_official))
ck('DC 1건', len(r_dc) == 1 and r_dc[0]['link'] == 'L8')
ck('flagged 는 clean/NULL 을 뺀 5건', sorted(x['link'] for x in r_flag) == ['L1', 'L2', 'L3', 'L4', 'L5'],
   '-> %s' % sorted(x['link'] for x in r_flag))
ck('clean(L6) 은 flagged 에서 제외', all(x['link'] != 'L6' for x in r_flag))
ck('판정 없음(L7~L10) 도 flagged 에서 제외', all(x['link'] not in ('L7','L8','L9','L10') for x in r_flag))
ck('2축 교차 = 공식 ∩ flagged', sorted(x['link'] for x in r_of_flag) == ['L1','L2','L3','L4','L5'])
ck('판정 컬럼이 실린다', r_of_flag and r_of_flag[0].get('verdict_kind') is not None)
ck('target 이 실린다 (L4=게임사)', any(x['link']=='L4' and x.get('verdict_target')=='게임사' for x in r_flag))
ck('기존 games 경로 회귀 없음', len(r_game) == 1 and r_game[0]['link'] == 'L7')
ck('판정 없는 글도 LEFT JOIN 으로 살아남는다', any(x['link']=='L9' and x.get('verdict_kind') is None for x in r_all))
ck('정렬은 timestamp DESC', [x['link'] for x in r_all][:3] == ['L10','L9','L8'],
   '-> %s' % [x['link'] for x in r_all][:3])

# [PATTERN-44] 부분 인덱스의 WHERE 와 쿼리의 WHERE 가 **글자 단위로 같은지** 채점한다.
#   왜 필요한가: `NULL <> 'clean'` 은 이미 NULL(제외)이라 `kind IS NOT NULL` 절은 **결과에는
#   불필요**하고 부분 인덱스와 글자를 맞추는 데만 필요하다. 누가 "간소화" 로 그 절을 빼면
#   위 assert 는 전부 초록인 채 인덱스만 조용히 죽는다.
#
#   **EXPLAIN QUERY PLAN 으로 재지 않는다.** 처음엔 그렇게 짰는데 플래너가 테이블 통계에 따라
#   경로를 바꿔서, 10행짜리 테스트 DB 에서는 정상 코드도 뮤턴트도 **똑같이 인덱스를 안 탄다**.
#   둘을 못 가르는 검사는 없느니만 못하다. 그래서 실행 계획이 아니라 **소스와 DDL 의 결합**을
#   본다 - 이 검사가 잡으려는 것이 정확히 "한쪽만 고쳤다" 이므로 이 쪽이 재는 자리도 맞다.
import inspect, re as _re
_ddl = conn.execute(
    "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_post_verdicts_flagged'"
).fetchone()
_norm = lambda s: _re.sub(r'\s+', ' ', s or '').replace('v.', '').strip().lower()
_idx_where = _norm(_ddl[0].split(' WHERE ', 1)[1]) if (_ddl and ' WHERE ' in _ddl[0]) else ''
_src = _norm(inspect.getsource(type(h).get_latest_posts))
ck('부분 인덱스 WHERE 가 쿼리 WHERE 와 글자 단위로 같다',
   bool(_idx_where) and _idx_where in _src,
   '-> 인덱스측 "%s"' % _idx_where)

# [v6.7.672] **제재 조회가 작은 쪽(post_verdicts)에서 출발하는가.**
#   라이브 2026-09-16: 제재 단독(출처=전체)이 16.8초(콜드)/0.617초(웜)였다. 원인은 플래너가
#   `ORDER BY p.timestamp DESC LIMIT` 을 공짜로 만들려고 수십만 행짜리 community_posts 에서
#   출발하는데, 제재 대상이 200건뿐이라 **LIMIT 이 영영 안 차서 끝까지 훑는 것**이었다.
#
#   **소스 문자열로 검사하지 않는다** - 첫 판이 `inspect.getsource` 에 'CROSS JOIN' 이 있는지
#   봤는데, CROSS JOIN 을 지운 뮤턴트가 **그대로 통과했다**: 그 선택을 설명하는 주석에 같은
#   글자가 있었기 때문이다(PATTERN-68 의 정확한 모양). 대신 **실제로 실행된 SQL 을 가로채**
#   그것을 EXPLAIN 한다. `CROSS JOIN` 의 조인 순서는 SQLite 가 통계와 무관하게 지키므로
#   10행짜리 테스트 DB 에서도 정상 코드와 뮤턴트가 갈린다(위 부분 인덱스 검사와 달리).
_traced = []
conn.set_trace_callback(lambda st: _traced.append(st))
h.get_latest_posts(limit=100, flagged=True)
conn.set_trace_callback(None)
_sel = [t for t in _traced if t.lstrip().upper().startswith('SELECT')]
_plan = conn.execute("EXPLAIN QUERY PLAN " + _sel[-1]).fetchall() if _sel else []
ck('제재 조회는 큰 테이블을 훑지 않는다 (작은 쪽에서 출발)',
   bool(_plan) and _plan[0][3].split()[1] == 'v',
   '-> %s' % (_plan[0][3] if _plan else '실행된 SELECT 를 못 잡았다'))

# [v6.7.670] **판정 잡의 분류표와 화면의 레인 표가 드리프트하지 않는가.**
#   실사고 2026-09-15: `post_judge.KINDS` 에 '도배' 가 있었는데 `feed-tab.js` 의
#   FEED_VERDICT_LANE 에만 빠져 있었다. `_feedVerdictKind` 는 미지 라벨을 **조용히 버리는**
#   설계라(모르는 값을 강조로 승격하지 않는다) 에러가 한 줄도 안 났고, 서버 제재 필터는
#   도배를 목록에 넣으므로 **표식 없는 카드가 제재 목록에 섞여 나왔다**. 라이브에서 act
#   79건 중 64건(81%)이 그 상태였다. 한쪽만 늘리면 다음 라벨도 같은 방식으로 사라진다.
_js = open(os.path.join('static', 'js', 'tabs', 'feed-tab.js'), encoding='utf-8').read()
_lane_block = _js.split('FEED_VERDICT_LANE = {', 1)[1].split('}', 1)[0]
_lane_keys = set(_re.findall(r"'([^']+)'\s*:\s*'(?:act|rev)'", _lane_block))

import importlib
_pj = importlib.import_module('web_lib.bg.post_judge')
_judge_kinds = {k for k in _pj.KINDS if k != 'clean'}

ck('레인 표가 판정 잡의 모든 분류를 덮는다',
   _judge_kinds <= _lane_keys,
   '-> 화면에서 조용히 사라지는 분류: %s' % (sorted(_judge_kinds - _lane_keys) or '없음'))
ck('레인 표에 판정 잡에 없는 분류가 없다',
   _lane_keys <= _judge_kinds,
   '-> 판정 잡이 내지 않는 분류: %s' % (sorted(_lane_keys - _judge_kinds) or '없음'))
# act/rev 배정도 판정 잡이 정본이다 - 색이 뒤바뀌면 "지워라" 와 "검토하라" 가 뒤집힌다.
_lane_map = dict(_re.findall(r"'([^']+)'\s*:\s*'(act|rev)'", _lane_block))
_mismatch = sorted(k for k in _judge_kinds & _lane_keys
                   if _lane_map.get(k) != ('act' if k in _pj.ACT_KINDS else 'rev'))
ck('act/rev 배정이 판정 잡과 일치한다', not _mismatch, '-> 어긋난 분류: %s' % (_mismatch or '없음'))

shutil.rmtree(_tmp, ignore_errors=True)
print()
print('RESULT: ' + ('ALL PASS' if not fails else 'FAIL -> ' + ', '.join(fails)))
sys.exit(1 if fails else 0)
