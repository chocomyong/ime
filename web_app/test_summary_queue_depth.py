# -*- coding: utf-8 -*-
"""[2026-09-16] 요약 대기열 - **세는 쪽과 집는 쪽이 같은 집합을 봐야 한다.**

## 왜 있나

워커 로그는 `요약 대기 {len(pending)}건` 을 찍는데 그 값은 조회 상한
(`BATCH_SIZE * BATCH_PARALLEL` = 40)에 **잘린 값**이다. 그래서 백로그가 40이든 95든
로그에는 똑같이 `40건` 으로 보이고 **잔량이 커지는 것을 로그로 못 본다.**

라이브 실측 2026-09-16 18:00~18:30: 로그는 `요약 대기 40건` 으로 30분간 평평했는데
실제 잔량은 **95건** · 밀림 **74.5분** 이었다. 그 구간의 요약 지연(수집 후 28~60분)을
로그만으로는 설명할 수 없었다 - 사용자가 *"17시44분 글이 왜 이제와서 요약되지"* 로
먼저 알아챘고, 그 답은 DB 를 직접 세고 나서야 나왔다.

## 이 검사가 잠그는 것

1. 두 함수가 **글자 단위로 같은 술어**를 쓴다(`_PENDING_AI_WHERE` 1벌).
   다르면 "잔량 N" 이 실제 배치 대상과 다른 집합을 세고, 그 숫자로 내린 판단이 전부 어긋난다.
2. 잔량이 상한을 넘을 때 **세는 쪽은 안 잘린다**(이 결함의 본체).
3. 제외 규칙(본문 없음 · `[...]` 센티넬 · 이미 분석됨)이 양쪽에서 같게 걸린다.
4. `since` 창이 양쪽에 같게 걸린다.

실행: python test_summary_queue_depth.py    (0 = PASS / 1 = FAIL)
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'utils'))

FAILS = []


def ck(name, cond, detail=''):
    print('  [%s] %s%s' % ('PASS' if cond else 'FAIL', name, ('  ' + detail) if detail else ''))
    if not cond:
        FAILS.append(name)


def main():
    import sqlite_handler as S

    tmp = tempfile.mkdtemp(prefix='imt_q_')
    os.environ['DB_DIR'] = tmp
    # **싱글턴을 피해 임시 DB 로 격리한다.** `SQLiteHandler` 는 `__new__` 가 인스턴스를
    #   하나로 묶고 `__init__` 이 `_initialized` 로 재초기화를 막는다 - 그냥 생성하면
    #   **이미 떠 있는 운영 경로 인스턴스가 그대로 돌아온다**(첫 판이 그래서 로컬 dev DB 에
    #   썼고, 두 번째 실행부터 앞선 실행의 행이 남아 결과가 흔들렸다).
    h = S.SQLiteHandler()
    h.db_path = os.path.join(tmp, 'test.sqlite')
    try:
        del h._thread_local.conn
    except Exception:
        pass
    conn = h._get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS community_posts (
            link TEXT PRIMARY KEY, source TEXT, game TEXT, title TEXT, content TEXT,
            detected_keywords TEXT, board_name TEXT, date TEXT, timestamp TEXT,
            is_super_keyword INTEGER, inserted_at TEXT);
        CREATE TABLE IF NOT EXISTS ai_analysis (link TEXT PRIMARY KEY, summary TEXT);
    """)

    def put(link, content, inserted, analyzed=False):
        conn.execute("INSERT OR REPLACE INTO community_posts"
                     "(link,source,game,title,content,detected_keywords,board_name,date,"
                     " timestamp,is_super_keyword,inserted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (link, 'DC', 'G', 't', content, '', 'b', '01-01', '2026-09-16T01:00:00',
                      0, inserted))
        if analyzed:
            conn.execute("INSERT OR REPLACE INTO ai_analysis(link,summary) VALUES(?,?)", (link, 'x'))

    SINCE = '2026-09-16 00:00:00'
    N = 95                                  # 라이브에서 관측된 잔량
    for i in range(N):
        put('L%03d' % i, '본문', '2026-09-16 0%d:%02d:00' % (1 + i // 60, i % 60))
    # 대상이 아닌 것들 - 양쪽에서 똑같이 빠져야 한다
    put('X_nocontent', '', '2026-09-16 02:00:00')
    put('X_blank', '   ', '2026-09-16 02:00:00')
    put('X_sentinel', '[분석 제외]', '2026-09-16 02:00:00')
    put('X_done', '본문', '2026-09-16 02:00:00', analyzed=True)
    put('X_old', '본문', '2026-09-15 23:00:00')      # since 창 밖
    conn.commit()

    LIMIT = 40                              # BATCH_SIZE(10) * BATCH_PARALLEL(4)
    pending = h.get_posts_pending_ai_summary(limit=LIMIT, since=SINCE)
    depth = h.count_posts_pending_ai_summary(since=SINCE)

    ck('집는 쪽은 조회 상한에 걸린다 (이게 로그를 속이던 값)',
       len(pending) == LIMIT, '%d건' % len(pending))
    ck('세는 쪽은 안 잘린다 - 실제 잔량을 준다',
       depth == N, '센 값 %d / 실제 %d' % (depth, N))
    ck('그래서 로그가 둘을 구분해 말할 수 있다',
       depth > len(pending), '%d > %d' % (depth, len(pending)))

    # 제외 규칙이 양쪽에서 같게 걸리는가 - 세는 쪽만 느슨하면 "잔량" 이 부풀어 오진을 만든다
    links = {r['link'] for r in pending}
    ck('본문 없음·센티넬·이미 분석됨·창 밖은 집는 쪽에 안 들어온다',
       not (links & {'X_nocontent', 'X_blank', 'X_sentinel', 'X_done', 'X_old'}),
       '섞인 것: %s' % sorted(links & {'X_nocontent', 'X_blank', 'X_sentinel', 'X_done', 'X_old'}))
    ck('세는 쪽도 같은 것을 뺀다 (술어 1벌)', depth == N, '%d' % depth)

    # 잔량이 상한 밑이면 두 값이 같아야 한다 - 그때는 워커가 세는 조회를 아예 안 낸다
    conn.execute("DELETE FROM community_posts WHERE link LIKE 'L0%' AND link > 'L004'")
    conn.commit()
    p2 = h.get_posts_pending_ai_summary(limit=LIMIT, since=SINCE)
    d2 = h.count_posts_pending_ai_summary(since=SINCE)
    ck('평시(잔량 < 상한)에는 두 값이 같다', len(p2) == d2 and d2 < LIMIT,
       'pending %d / count %d' % (len(p2), d2))

    # 술어가 **한 벌**인지 - 사본이 생기면 언젠가 한쪽만 고쳐진다
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'utils', 'sqlite_handler.py'), encoding='utf-8').read()
    ck('대기열 술어 정의가 한 곳뿐이다',
       src.count('_PENDING_AI_WHERE = ') == 1 and src.count('{_PENDING_AI_WHERE}') == 2,
       '정의 %d · 사용 %d' % (src.count('_PENDING_AI_WHERE = '), src.count('{_PENDING_AI_WHERE}')))

    w = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'web_lib', 'ai_gemma4_worker.py'), encoding='utf-8').read()
    ck('워커가 상한에 걸렸을 때만 센다 (평시 비용 0)',
       'if len(pending) >= _lim else len(pending)' in w,
       '무조건 세면 평시에도 매 사이클 COUNT 가 나간다')

    print()
    print('RESULT: ' + ('ALL PASS' if not FAILS else 'FAIL -> ' + ', '.join(FAILS)))
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
