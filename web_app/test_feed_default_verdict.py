# -*- coding: utf-8 -*-
"""[v6.7.738] 피드 제재 판정과 '판정 대기' 를 /api/articles/all 두 경로에서 태운다.

승인 개선 #1: 종전엔 game/source/flagged 축이 하나라도 켜져야 SQLite 직결 경로로 가서 판정
(post_verdicts) 컬럼이 붙었고, 기본 보기(메모리 캐시 경로)에는 안 붙어 제재 칩·대기 표식이
안 보였다(v6.7.664 비대칭). 이제 기본 보기 응답에도 SQLite 경로와 같은 필드·같은 의미로 붙인다.

'판정 대기'(judge_pending)는 서버가 판정 잡의 후보 술어(post_judge._candidate_where)로 정한다.
종전 클라 판정("공식이고 판정 없음")은 잡이 영영 안 보는 공지 게시판·7일 지난 글·실패 한도를
넘은 글에도 점을 찍었다 - 아래 L6·L7·L8 이 그 자리다(PATTERN-73).

- PATTERN-66: '에러 없음' 은 증거가 아니다 - 실제 라우트를 test_client 로 태우고 응답 필드를 인용한다.
- PATTERN-71: 판정 입력을 손으로 응답에 꽂지 않는다 - 조회는 운영과 같은 함수.
- PATTERN-70: IN 조회가 PK 자동 인덱스를 타는지 EXPLAIN 으로 확인.
- 픽스처 시각은 **지금 기준 상대값**이다 - 고정 날짜는 7일 창 밖으로 늙어 대기 검사가 조용히 뒤집힌다.
- 라이브 DB import 금지: 임시 파일 DB 로 격리(PATTERN-80 / live-container-no-import-app).

실행: IMT_DISABLE_BOOTSTRAP=1 python3 test_feed_default_verdict.py   (0=ALL PASS / 1=FAIL)
"""
import os
import sys
import shutil
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath('.'))
sys.path.insert(0, os.path.abspath('utils'))

_tmp = tempfile.mkdtemp(prefix='imtfeeddefv_')
os.environ['IMT_DISABLE_BOOTSTRAP'] = '1'   # ASSERT-01 - 크롤러/스케줄러 실기동 차단
# DB_DIR 을 import 전에 박는다 - SQLiteHandler 는 싱글턴이고 모듈 import 시점에 인스턴스가
#   만들어지므로, 나중에 경로 인자를 줘도 조용히 무시된다(test_feed_verdict_query.py 와 동일 사유).
os.environ['DB_DIR'] = _tmp

import app as appmod          # noqa: E402  (blueprint 등록 + sqlite_handler.db_handler 생성)
import sqlite_handler as _rsh  # noqa: E402  (라우트가 쓰는 바로 그 싱글턴)
from web_lib.bg import post_judge  # noqa: E402

_db = _rsh.db_handler
assert os.path.dirname(_db.db_path) == _tmp, '격리 실패 - 개발/라이브 DB 를 가리킨다: %s' % _db.db_path


def _ago(hours):
    return (datetime.now() - timedelta(hours=hours)).strftime('%Y-%m-%dT%H:%M:%S')


UB = post_judge.USER_BOARDS[0]   # 유저 게시판(판정 대상)
assert '공지' not in ''.join(post_judge.USER_BOARDS), '픽스처 전제 - 공지 게시판은 판정 대상이 아니다'

# (game, source, title, timestamp, link, board_name) 와 기대 judge_pending
POSTS = [
    ('배틀그라운드', 'OFFICIAL_WEB', '핵 판매합니다 카톡 ㄱㄱ', _ago(1),       'L1', UB),           # 홍보(act)
    ('배틀그라운드', 'OFFICIAL_WEB', '운영진 일 똑바로 해라',   _ago(2),       'L2', UB),           # 욕설(rev)
    ('배틀그라운드', 'OFFICIAL_WEB', '오늘 업데이트 뭐 바뀜?',  _ago(3),       'L3', UB),           # 판정 없음 -> 대기
    ('배틀그라운드', 'OFFICIAL_WEB', '패치 노트 정리본',        _ago(4),       'L4', UB),           # clean
    ('배틀그라운드', 'DC',           '디시 글 하나',            _ago(0.5),     'L5', '배그 갤'),    # 비공식
    ('배틀그라운드', 'OFFICIAL_WEB', '정기 점검 안내',          _ago(5),       'L6', '배그 공지'),  # 운영사 게시판 - 잡이 안 본다
    ('배틀그라운드', 'OFFICIAL_WEB', '열흘 전 질문',            _ago(24 * 10), 'L7', UB),           # 7일 창 밖
    ('배틀그라운드', 'OFFICIAL_WEB', '판정이 계속 깨지는 글',   _ago(6),       'L8', UB),           # 실패 한도 도달
    ('배틀그라운드', 'OFFICIAL_WEB', '한 번 실패한 글',         _ago(7),       'L9', UB),           # 실패 1회 - 아직 후보
]
EXPECT_PENDING = {'L1': False, 'L2': False, 'L3': True, 'L4': False, 'L5': False,
                  'L6': False, 'L7': False, 'L8': False, 'L9': True}
conn = _db._get_connection()
for g, s, t, ts, ln, bd in POSTS:
    conn.execute(
        "INSERT OR REPLACE INTO community_posts (game, source, title, content, board_name, date, timestamp, link, is_super_keyword, inserted_at)"
        " VALUES (?,?,?,?,?,?,?,?,0,?)", (g, s, t, t, bd, ts[:10], ts, ln, ts))
VERDICTS = [('L1', '홍보', 'none', 0), ('L2', '욕설', '게임사', 0), ('L4', 'clean', 'none', 0),
            ('L8', None, None, post_judge.MAX_FAIL), ('L9', None, None, 1)]
for ln, k, tg, fail in VERDICTS:
    conn.execute("INSERT OR REPLACE INTO post_verdicts (link, kind, target, content_hash, judged_at, fail)"
                 " VALUES (?,?,?,?,?,?)", (ln, k, tg, 'h_' + ln, _ago(1), fail))
conn.commit()

fails = []


def ck(name, cond, detail=''):
    print('  [%s] %s %s' % ('PASS' if cond else 'FAIL', name, detail))
    if not cond:
        fails.append(name)


print('=== A. get_verdicts_for_links (link 집합 한정 조회) ===')
vm = _db.get_verdicts_for_links(['L1', 'L2', 'L3', 'L4', 'L5', 'L_MISSING'])
print('  vm =', {k: v['verdict_kind'] for k, v in vm.items()})
ck('판정 있는 글만 맵에 담긴다 (L1,L2,L4)', set(vm.keys()) == {'L1', 'L2', 'L4'}, '-> %s' % sorted(vm.keys()))
ck('필드명이 verdict_kind/target/at 다',
   bool(vm.get('L1')) and set(vm['L1'].keys()) == {'verdict_kind', 'verdict_target', 'verdict_at'},
   '-> %s' % sorted(vm.get('L1', {}).keys()))
ck('kind/target 값이 실린다 (L2=욕설/게임사)',
   vm.get('L2', {}).get('verdict_kind') == '욕설' and vm.get('L2', {}).get('verdict_target') == '게임사')
ck('빈 입력은 {} (조회 생략)', _db.get_verdicts_for_links([]) == {} and _db.get_verdicts_for_links(None) == {})

print('=== B. IN 조회가 PK 자동 인덱스를 탄다 (SCAN 아님, PATTERN-70) ===')
plan = conn.execute(
    "EXPLAIN QUERY PLAN SELECT link, kind, target, judged_at FROM post_verdicts WHERE link IN (?,?,?)",
    ('L1', 'L2', 'L3')).fetchall()
detail = ' | '.join(r[3] for r in plan)
ck('판정 IN 조회가 SEARCH + USING INDEX (풀스캔 아님)',
   ('SEARCH' in detail) and ('USING INDEX' in detail), '-> %s' % detail)

print('=== D. 판정 대기 = 판정 잡의 후보 (같은 술어, 실행으로 대조) ===')
all_links = [p[4] for p in POSTS]
pend = post_judge.pending_links(conn, all_links)
_rconn = post_judge._connect_rw()
try:
    cands = {c[0] for c in post_judge._fetch_candidates(_rconn)}
finally:
    _rconn.close()
print('  pending_links =', sorted(pend), '/ 잡 후보 =', sorted(cands))
ck('대기 집합이 잡이 실제로 고르는 후보와 같다', pend == cands, '-> 차이 %s' % sorted(pend ^ cands))
ck('대기 집합이 기대와 같다 (L3·L9 만)', pend == {k for k, v in EXPECT_PENDING.items() if v}, '-> %s' % sorted(pend))
ck('빈 입력은 빈 집합', post_judge.pending_links(conn, []) == set() and post_judge.pending_links(conn, None) == set())
ph = ','.join('?' * 3)
plan = conn.execute(
    "EXPLAIN QUERY PLAN SELECT p.link FROM community_posts p LEFT JOIN post_verdicts v ON v.link = p.link "
    "WHERE p.link IN (%s) AND " % ph + post_judge._candidate_where(),
    ('L1', 'L2', 'L3') + post_judge.USER_BOARDS).fetchall()
detail = ' | '.join(r[3] for r in plan)
ck('대기 조회가 community_posts 를 인덱스로 SEARCH (풀스캔 아님 - PK 든 source_ts 든)',
   ('SEARCH p USING INDEX' in detail) and ('SCAN p' not in detail), '-> %s' % detail)

print('=== C. /api/articles/all 기본 보기 라우트(메모리 캐시 경로) ===')
import web_lib.routes.articles as articles_mod  # noqa: E402


class _FakeCrawler:
    """메모리 소스(크롤러)를 대역으로. 판정 조인과 대기 표식(새 경로)만 검사 대상이라 메모리 레코드는 고정.
    _enrich_articles_for_web 는 실물과 동형으로 사본을 만들고 source_community 를 파생한다."""

    def get_all_latest_articles(self):
        return [dict(link=ln, source=s, title=t, game=g, timestamp=ts, board_name=bd, content=t)
                for g, s, t, ts, ln, bd in POSTS]

    def _enrich_articles_for_web(self, articles):
        out = []
        for a in articles:
            na = dict(a)
            na['source_community'] = 'OFFICIAL_WEB' if str(a.get('source', '')).upper() == 'OFFICIAL_WEB' else 'DC'
            out.append(na)
        return out


articles_mod.get_crawler = lambda: _FakeCrawler()
articles_mod.invalidate_article_caches()   # 혹시 모를 이전 캐시 리셋

client = appmod.app.test_client()
resp = client.get('/api/articles/all')
ck('기본 보기 200', resp.status_code == 200, '-> %s' % resp.status_code)
rows = resp.get_json() or []
by = {r.get('link'): r for r in rows}
print('  응답 =', [(r.get('link'), r.get('verdict_kind'), r.get('judge_pending')) for r in rows])

# 같은 필드·같은 의미(= SQLite LEFT JOIN): 모든 글에 키가 있고, 판정 없으면 None.
ck('모든 글에 verdict_kind 키가 있다 (LEFT JOIN 동형 - 같은 필드)', all('verdict_kind' in r for r in rows))
ck('모든 글에 verdict_target/at 키도 있다',
   all(('verdict_target' in r and 'verdict_at' in r) for r in rows))
ck('공식+판정 글에 kind 가 실린다 (L1=홍보)', by.get('L1', {}).get('verdict_kind') == '홍보')
ck('공식+판정 글에 target 이 실린다 (L2=게임사)', by.get('L2', {}).get('verdict_target') == '게임사')
ck('clean(L4) 은 kind=clean 그대로', by.get('L4', {}).get('verdict_kind') == 'clean')
ck('비공식(L5) 은 verdict_kind=None (판정 대상 아님)', by.get('L5', {}).get('verdict_kind') is None)
got = {ln: by.get(ln, {}).get('judge_pending') for ln in EXPECT_PENDING}
ck('기본 보기 judge_pending 이 글마다 기대와 같다 (공지 L6·7일 밖 L7·실패 한도 L8 은 대기 아님)',
   got == EXPECT_PENDING, '-> %s' % {k: v for k, v in got.items() if v != EXPECT_PENDING[k]})

print('=== E. /api/articles/all?source=OFFICIAL_WEB (SQLite 직결 경로) ===')
resp = client.get('/api/articles/all?source=OFFICIAL_WEB')
ck('SQLite 경로 200', resp.status_code == 200, '-> %s' % resp.status_code)
srows = resp.get_json() or []
sby = {r.get('link'): r for r in srows}
exp_off = {k: v for k, v in EXPECT_PENDING.items() if k != 'L5'}
sgot = {ln: sby.get(ln, {}).get('judge_pending') for ln in exp_off}
print('  응답 =', [(r.get('link'), r.get('verdict_kind'), r.get('judge_pending')) for r in srows])
ck('SQLite 경로 judge_pending 도 기대와 같다 (같은 술어 - 경로 간 비대칭 없음)',
   sgot == exp_off, '-> %s' % {k: v for k, v in sgot.items() if v != exp_off[k]})

shutil.rmtree(_tmp, ignore_errors=True)
print()
print('RESULT: ' + ('ALL PASS' if not fails else 'FAIL -> ' + ', '.join(fails)))
sys.exit(1 if fails else 0)
