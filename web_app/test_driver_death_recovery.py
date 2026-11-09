# -*- coding: utf-8 -*-
"""[2026-09-16] 드라이버 세션 사망이 **상위로 전파돼 재생성을 유도하는가.**

실사고: chromedriver 가 죽은 뒤 9시간 동안 NAVER·DAUM 수집이 0건이었다. 회복 기계
(`_crawl_*_with_driver` 의 quit -> None -> `_get_driver()`)는 이미 있었고 정상이었는데,
그 위 세 층이 드라이버 사망을 **정상 반환으로 번역**해서 한 번도 안 돌았다.

이 하네스가 채점하는 것은 "에러가 안 난다" 가 아니라 **던지는가/안 던지는가의 구분**이다:
  - 드라이버가 죽었다 -> 던진다 (URL 을 넘기면 안 된다)
  - URL 하나가 실패했다 -> 안 던진다 (다음 URL 로 간다 - 기존 내성 유지)
둘을 못 가르면 고친 것이 아니다. PATTERN-68 을 피해 소스 문자열이 아니라 **실행(naver_cafe)과
AST(crawler.py)** 로 잰다 - 주석에 걸리는 검사를 만들지 않는다.
"""
import ast
import os
import sys

sys.path.insert(0, os.path.abspath('.'))
sys.path.insert(0, os.path.abspath('crawlers'))
sys.path.insert(0, os.path.abspath('utils'))

from selenium.common.exceptions import NoSuchElementException

from crawlers import DriverSessionDead
from crawlers.naver_cafe import crawl_naver_cafe

fails = []


def ck(name, cond, detail=''):
    print('  [%s] %s %s' % ('PASS' if cond else 'FAIL', name, detail))
    if not cond:
        fails.append(name)


# ── 라이브에서 실제로 올라온 예외 모양 (2026-09-16 로그 인용).
#    selenium 예외가 아니라 urllib3 의 것이 그대로 올라온다 - 타입으로 가르면 놓친다.
_LIVE_ERR = ("HTTPConnectionPool(host='localhost', port=46676): Max retries exceeded with url: "
             "/session/cc6fc5ba0cda7552ade947a587a92f3b/url (Caused by NewConnectionError("
             "\"HTTPConnection(host='localhost', port=46676): Failed to establish a new connection: "
             "[Errno 111] Connection refused\"))")

_ROW_HTML = """
<table><tr>
  <td><a class="board_name" href="/f-e/cafes/1/menus/2">자유게시판</a></td>
  <td><div class="board-list"><div class="inner_list">
      <a class="article" href="https://cafe.naver.com/f-e/cafes/1/articles/999?referrerAllArticles=true">살아있는 글</a>
  </div></div></td>
  <td class="td_normal type_date">%s</td>
</tr></table>"""


class FakeDriver:
    """살아있는/죽은 드라이버를 같은 인터페이스로 흉내낸다.

    `alive=False` 면 **모든 명령**이 라이브와 같은 예외를 던진다 - `title` 프로브까지 포함해서.
    프로브만 살려두면 검사가 통과해도 라이브에서는 안 통한다(그게 이 사고의 모양이다)."""

    def __init__(self, alive=True, fail_urls=(), wait_fails=False):
        self.alive = alive
        self.fail_urls = set(fail_urls)
        self.wait_fails = wait_fails
        self.visited = []
        self.title_probes = 0

    def _boom(self):
        raise Exception(_LIVE_ERR)

    @property
    def title(self):
        self.title_probes += 1
        if not self.alive:
            self._boom()
        return '네이버 카페'

    def get(self, url):
        self.visited.append(url)
        if not self.alive or url in self.fail_urls:
            self._boom()

    def find_element(self, *a, **kw):
        if not self.alive:
            self._boom()
        if self.wait_fails:
            raise NoSuchElementException('렌더링 미완료')
        return object()

    @property
    def page_source(self):
        if not self.alive:
            self._boom()
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo('Asia/Seoul'))
        return _ROW_HTML % now.strftime('%H:%M')


print('=== 1) naver_cafe - 실행으로 잰다 ===')

# (A) 드라이버 사망 -> 던진다
d = FakeDriver(alive=False)
try:
    crawl_naver_cafe(d, ['https://cafe.naver.com/f-e/cafes/1/menus/0'], wait_timeout=0)
    ck('드라이버 사망이면 DriverSessionDead 를 던진다', False, '-> 던지지 않고 반환했다')
except DriverSessionDead as e:
    ck('드라이버 사망이면 DriverSessionDead 를 던진다', True, '-> %s' % str(e)[:48])
except Exception as e:
    ck('드라이버 사망이면 DriverSessionDead 를 던진다', False, '-> 다른 예외: %r' % e)
ck('사망 판정은 프로브를 실제로 태운다', d.title_probes >= 1, '-> title 호출 %d회' % d.title_probes)

# (B) 살아있는데 URL 하나만 실패 -> 안 던진다 + 다음 URL 로 간다 (기존 내성 유지)
U_BAD, U_OK = 'https://cafe.naver.com/bad', 'https://cafe.naver.com/ok'
d = FakeDriver(alive=True, fail_urls=[U_BAD])
try:
    res = crawl_naver_cafe(d, [U_BAD, U_OK], wait_timeout=0)
    ck('URL 단위 실패는 던지지 않는다', True)
    ck('실패한 URL 은 빈 결과', res[0][1] == [], '-> %s' % res[0][1])
    ck('다음 URL 은 계속 수집한다', len(res) == 2 and len(res[1][1]) == 1,
       '-> %s' % [len(r[1]) for r in res])
except Exception as e:
    ck('URL 단위 실패는 던지지 않는다', False, '-> %r' % e)
    ck('실패한 URL 은 빈 결과', False)
    ck('다음 URL 은 계속 수집한다', False)

# (C) 렌더링 대기 실패 + 드라이버 생존 -> 안 던진다 (비회원 카페/SPA 미완료는 정상 경로)
d = FakeDriver(alive=True, wait_fails=True)
try:
    res = crawl_naver_cafe(d, ['https://cafe.naver.com/f-e/cafes/1/menus/0'], wait_timeout=0)
    ck('렌더링 대기 실패는 살아있으면 안 던진다', True, '-> %s' % [len(r[1]) for r in res])
except Exception as e:
    ck('렌더링 대기 실패는 살아있으면 안 던진다', False, '-> %r' % e)

# (D) 렌더링 대기 경로에서 죽어도 잡힌다 (한쪽만 고치면 다른 쪽이 조용한 출구가 된다)
class DiesInWait(FakeDriver):
    def find_element(self, *a, **kw):
        self.alive = False      # 대기 도중 chromedriver 사망
        self._boom()

d = DiesInWait(alive=True)
try:
    crawl_naver_cafe(d, ['https://cafe.naver.com/f-e/cafes/1/menus/0'], wait_timeout=0)
    ck('렌더링 대기 중 사망도 전파한다', False, '-> 던지지 않았다')
except DriverSessionDead:
    ck('렌더링 대기 중 사망도 전파한다', True)
except Exception as e:
    ck('렌더링 대기 중 사망도 전파한다', False, '-> 다른 예외: %r' % e)


print()
print('=== 2) crawler.py - AST 로 잰다 (소스 문자열·주석에 안 걸리게) ===')

_tree = ast.parse(open(os.path.join('crawlers', 'crawler.py'), encoding='utf-8').read())
_fns = {n.name: n for n in ast.walk(_tree) if isinstance(n, ast.FunctionDef)}


def _dead_exits(fn):
    """이 함수 안에서 '드라이버 사망' 으로 빠져나가는 자리들.
    반환: [(자리를 담고 있는 statement list, 그 statement)] - 앞 문장을 볼 수 있게 블록째 준다."""
    out = []
    for node in ast.walk(fn):
        for field in ('body', 'orelse', 'finalbody'):
            block = getattr(node, field, None)
            if not isinstance(block, list):
                continue
            for st in block:
                if not isinstance(st, ast.Raise):
                    continue
                exc = st.exc
                # `raise DriverSessionDead(...)` 또는 except DriverSessionDead 안의 bare `raise`
                named = (isinstance(exc, ast.Call) and getattr(exc.func, 'id', '') == 'DriverSessionDead')
                bare = exc is None and isinstance(node, ast.ExceptHandler) and \
                    getattr(node.type, 'id', '') == 'DriverSessionDead'
                if named or bare:
                    out.append((block, st))
    return out


def _saves(fn):
    return [n for n in ast.walk(fn)
            if isinstance(n, ast.Call) and getattr(n.func, 'attr', '') == '_save_partial_before_raise']


for fname in ('_crawl_naver', '_crawl_daum'):
    fn = _fns.get(fname)
    exits = _dead_exits(fn) if fn else []
    ck('%s 가 드라이버 사망을 던진다' % fname, bool(exits), '-> 전파 지점 %d곳' % len(exits))
    # **유실 가드**: `_update_articles` 는 메모리 dedup 만 갱신하고 SQLite 저장은 꼬리에서 한다.
    #   꼬리에 못 가고 던지면 그 글들은 다음 사이클에 '이미 본 글' 로 걸려 영구 유실된다.
    ok = bool(exits)
    for block, st in exits:
        prior = block[:block.index(st)]
        ok = ok and any(isinstance(c, ast.Call) and getattr(c.func, 'attr', '') == '_save_partial_before_raise'
                        for p in prior for c in ast.walk(p))
    ck('%s 는 던지기 전에 모은 글을 저장한다' % fname, ok,
       '-> 선저장 호출 %d개' % len(_saves(fn)) if fn else '')

# 구 결함의 정확한 모양: 연결 오류를 감지해 놓고 `return` 으로 나가던 것.
_daum = _fns.get('_crawl_daum')
_conn_returns = []
for node in ast.walk(_daum) if _daum else []:
    if not isinstance(node, ast.If):
        continue
    if getattr(node.test, 'id', '') != 'is_connection_error':
        continue
    _conn_returns += [n for n in ast.walk(node) if isinstance(n, ast.Return)]
ck('연결 오류 분기가 조용히 return 하지 않는다', not _conn_returns,
   '-> return %d개' % len(_conn_returns))

print()
print('RESULT: ' + ('ALL PASS' if not fails else 'FAIL -> ' + ', '.join(fails)))
sys.exit(1 if fails else 0)
