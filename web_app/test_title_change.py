# -*- coding: utf-8 -*-
"""[2026-09-16] 원본에서 **제목이 바뀐 사실**을 잃지 않는다.

## 실사고

POE1 도움말 `view-thread/4005967` 의 제목이 `산성의 중재자` -> `..` 로 바뀌었는데 IME 화면에는
옛 제목이 남아, 게시판에서 본 글을 IME 에서 찾을 수 없었다(사용자 *"왜 갑자기 사라졌지"*).
글은 삭제된 적이 없다 - 지금도 게시판에 있다.

감시 도구에서 **의미 있는 제목을 `..` 같은 무의미 문자열로 바꾸는 것은 글을 묻으려는 행동**
이라 그 자체가 신호다. 디스코드 쪽은 이미 `edited` 를 추적하는데 커뮤니티 글만 빠져 있었다.
그래서 덮어쓰되 **최초 제목을 남긴다.**

## 잠그는 것

1. 제목이 바뀌면 현재 제목은 **최신으로 따라간다**(화면이 원본과 다른 말을 하면 안 된다).
2. **최초 제목**이 보존된다 - 두 번 바뀌어도 첫 원본이 유지된다.
3. 안 바뀌면 `title_changed_at` 을 건드리지 않는다(매 크롤마다 갱신되면 신호가 아니라 잡음이다).
4. 본문·수집시각 등 기존 보존 규약이 안 깨진다.

실행: python test_title_change.py     (0 = PASS / 1 = FAIL)
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
    tmp = tempfile.mkdtemp(prefix='imt_t_')
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
    conn.executescript("""CREATE TABLE IF NOT EXISTS community_posts(
        game TEXT, source TEXT, detected_keywords TEXT, title TEXT, content TEXT,
        board_name TEXT, date TEXT, timestamp TEXT, link TEXT PRIMARY KEY,
        is_super_keyword INTEGER, inserted_at TEXT, title_prev TEXT, title_changed_at TEXT);""")

    L = 'https://poe.kakaogames.com/forum/view-thread/4005967'

    def save(title):
        h.save_community_posts([{'game': 'POE1', 'source': 'OFFICIAL_WEB', 'detected_keywords': [],
                                 'title': title, 'board_name': 'POE1 도움말', 'date': '09-16',
                                 'timestamp': '2026-09-16T19:43:02', 'link': L,
                                 'is_super_keyword': False}])

    def row():
        return conn.execute("SELECT title, title_prev, title_changed_at, inserted_at "
                            "FROM community_posts WHERE link=?", (L,)).fetchone()

    save('산성의 중재자')
    t0 = row()
    ck('첫 저장에는 변경 표식이 없다', t0[1] is None and t0[2] is None,
       'prev=%r at=%r' % (t0[1], t0[2]))

    # 같은 제목으로 다시 크롤 - 매 사이클 일어나는 일이다
    save('산성의 중재자')
    t1 = row()
    ck('제목이 그대로면 아무것도 안 찍는다 (잡음 방지)', t1[1] is None and t1[2] is None,
       'prev=%r at=%r' % (t1[1], t1[2]))

    conn.execute("UPDATE community_posts SET content='본문' WHERE link=?", (L,))
    conn.commit()

    save('..')                                   # 작성자가 제목을 바꿨다
    t2 = row()
    ck('현재 제목은 원본을 따라간다', t2[0] == '..', repr(t2[0]))
    ck('최초 제목이 보존된다', t2[1] == '산성의 중재자', repr(t2[1]))
    ck('변경 시각이 찍힌다', bool(t2[2]), repr(t2[2]))
    ck('본문은 안 지워진다 (기존 보존 규약)',
       conn.execute("SELECT content FROM community_posts WHERE link=?", (L,)).fetchone()[0] == '본문')
    ck('최초 수집시각은 유지된다', t2[3] == t0[3], '%r -> %r' % (t0[3], t2[3]))

    save('...')                                  # 또 바꿨다
    t3 = row()
    ck('두 번째 변경이 **첫 원본을 덮지 않는다**', t3[1] == '산성의 중재자',
       'prev=%r (원본이 덮이면 "원래 뭐였나" 를 잃는다)' % t3[1])
    ck('현재 제목은 계속 따라간다', t3[0] == '...', repr(t3[0]))

    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'utils', 'sqlite_handler.py'), encoding='utf-8').read()
    ck('변경을 로그로도 말한다 (저장부 도달 여부를 라이브에서 확인할 수 있어야 한다)',
       '제목 변경 감지' in src)
    ck('최초 제목 보존 조건이 살아 있다 (title_prev IS NULL)',
       'community_posts.title_prev IS NULL' in src)

    # 화면까지 닿는가 - **클래스만 붙이고 CSS 가 없으면 조용한 no-op** 이다(v6.7.126 함정).
    _here = os.path.dirname(os.path.abspath(__file__))
    js = open(os.path.join(_here, 'static', 'js', 'tabs', 'feed-tab.js'), encoding='utf-8').read()
    css = open(os.path.join(_here, 'static', 'css', 'style-feed.css'), encoding='utf-8').read()
    ck('상세에 제목 변경 행을 넣는다', 'titleChangedRow' in js and 'title_prev' in js)
    ck('그 클래스에 CSS 규칙이 실재한다 (조용한 no-op 금지)',
       '.feed-detail-titlechg' in css and '.feed-titlechg-arrow' in css)
    ck('옛 제목과 현재 제목이 같으면 행을 안 만든다 (잡음 방지)',
       "_prevT !== String(a.title || '').trim()" in js)
    ck('제목은 escape 해서 넣는다 (원본에서 온 값이다)',
       'escapeHtml(_prevT)' in js)

    # **되돌리는 writer** - 이 결함의 본체. 메모리 스냅샷이 옛 제목을 들고 있으면 사이클 끝
    #   콜백(`bg/export.main_crawler_export_callback` -> `save_community_posts`)이 DB 를 되덮는다.
    cr = open(os.path.join(_here, 'crawlers', 'crawler.py'), encoding='utf-8').read()
    ck('메모리 스냅샷의 제목을 새로 읽은 값으로 맞춘다',
       "_old['title'] = _a.get('title')" in cr,
       'append-only 면 첫 관측 제목이 남아 사이클 끝에 DB 를 되덮는다')
    ck('제목만 맞춘다 (timestamp 는 v6.6.102 가 일부러 보존한다)',
       "_old['timestamp']" not in cr and "_old['content']" not in cr)

    # 되돌림을 시뮬레이션한다 - 메모리 스냅샷을 그대로 저장하면 무슨 일이 나는가
    save('..')
    conn.execute("UPDATE community_posts SET title='..' WHERE link=?", (L,))
    conn.commit()
    save('산성의 중재자')                     # 옛 제목을 든 스냅샷이 되덮는 상황
    ck('되덮여도 최초 제목은 첫 값을 유지한다',
       row()[1] == '산성의 중재자', repr(row()[1]))

    print()
    print('RESULT: ' + ('ALL PASS' if not FAILS else 'FAIL -> ' + ', '.join(FAILS)))
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
