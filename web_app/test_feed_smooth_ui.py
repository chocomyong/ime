# -*- coding: utf-8 -*-
"""[v6.7.688] 피드 탭 순단 제거를 **실제 브라우저에서** 잰다 (PATTERN-38).

세 가지를 잰다 - 셋 다 "시간·스크롤·DOM 동일성" 이 있어야 재현되므로 단위 테스트로는 못 잡는다:
  (a) **동일 데이터 refetch 는 DOM 을 다시 안 짓는다** - 소켓 raw_articles_update(800ms 디바운스)가
      크롤 사이클마다 loadFeedPane 을 태우는데, 데이터가 그대로면 재렌더가 곧 순단이다.
      기존 카드에 표식을 박고 refetch 후 그 표식이 살아있는지로 "안 지었음" 을 증명한다.
  (b) **스크롤 내려둔 채 새 글이 위에 붙어도 보던 카드가 제자리에 있고, "새 글 N건" pill 이 뜬다** -
      앵커 카드의 뷰포트 위치가 수 px 안에서 유지되는지 잰다.
  (c) **필터/토글은 그대로 다시 그린다** - 스킵이 토글 렌더까지 삼키면 안 된다.

실행: `python test_feed_smooth_ui.py`  (playwright + chromium 필요)
종료코드: 0 = ALL PASS / 1 = FAIL / 3 = 환경 없음(**PASS 아님** - 조용히 통과시키지 않는다)
"""
import functools
import http.server
import os
import socket
import sys
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_STATIC = os.path.join(_HERE, 'static')
_SHOT_DIR = ('/tmp/claude-0/-home-user-imt-tools/'
             '14b1b947-870f-5a12-af51-3c98525733c1/scratchpad')

# 프로브 페이지는 static/ 에 파일로 두지 않는다 - 픽스처가 배포물에 섞이지 않게 서버가 메모리에서
# 낸다. 나머지 경로(css/js)는 실제 static/ 을 그대로 읽는다. #feed-tab 에 고정 높이를 줘서
# #feed-body(flex:1, overflow-y:auto)가 실제로 스크롤되게 한다(앵커 보존 측정에 필수).
PROBE_HTML = """<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="/css/style-feed.css">
<style>body{margin:0;background:#0f1117}</style></head><body>
<div class="pane-window" style="width:480px">
  <span id="feed-count-header"></span>
  <div id="feed-tab" class="tab-content" style="height:420px;padding:0">
    <div id="feed-body" class="feed-body"></div>
    <div id="feed-empty-state" class="empty-state" style="display:none"></div>
  </div>
</div>
<script>
  const API_BASE = '/api';
  // limits {G:0} = 무제한 - 기본 게임당 10 캡을 끄지 않으면 _applyFeedFilters 가 10건으로 잘라
  //   스크롤/앵커 테스트가 성립하지 않는다.
  let _localSettingsData = { games: ['G'], visibility: { G: true }, limits: { G: 0 } };
  // 응답 데이터는 테스트가 window.__data 로 손에 쥔다. 매 fetch 가 현재 __data 를 반환한다.
  window.fetch = function () {
    return Promise.resolve({ ok: true, json: async () => (window.__data || []) });
  };
  function _rows(tag, n, tsBase) {
    // timestamp 는 **고정폭 정렬 가능 문자열** - 피드 정렬이 localeCompare(DESC) 라 큰 값이 최신.
    //   base 가 크면(NEW=900 > A=800) 그 그룹 전체가 확실히 위로 간다(시:분 wrap 함정 회피).
    const o = []; const base = tsBase == null ? 800 : tsBase;
    for (let i = 0; i < n; i++) {
      const m = base - i;                       // 큰 값일수록 최신
      const ts = 'T' + String(500000 + m);      // 6자리 고정폭 → 사전순 = 수치순
      o.push({ link: tag + '_' + i, title: tag + ' 글 ' + i, game: 'G', source: 'DC',
        board_name: 'b', date: ts, timestamp: ts, content: '본문 ' + i,
        detected_keywords: [] });
    }
    return o;
  }
  window._rows = _rows;
</script>
<script src="/js/ui/common.js"></script>
<script src="/js/tabs/_tab_helpers.js"></script>
<script src="/js/core/highlight.js"></script>
<script src="/js/tabs/feed-tab.js"></script>
</body></html>"""


class _Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/__probe'):
            body = PROBE_HTML.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return super().do_GET()

    def log_message(self, *a):
        pass


def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _load(pg, port):
    pg.goto('http://127.0.0.1:%d/__probe' % port)
    pg.wait_for_function("typeof window.loadFeedPane === 'function'")


# 첫 렌더: __data 채우고 loadFeedPane → 카드가 뜰 때까지 대기.
_FIRST = """async () => {
  window.__data = window._rows('A', 120, 800);
  window.loadFeedPane();
}"""


def main():
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print('SKIP(환경 없음): playwright 미설치 - %s' % e)
        return 3

    os.makedirs(_SHOT_DIR, exist_ok=True)
    port = _free_port()
    srv = http.server.ThreadingHTTPServer(
        ('127.0.0.1', port), functools.partial(_Handler, directory=_STATIC))
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    fails = []

    def ck(name, cond, detail=''):
        print('  [%s] %s %s' % ('PASS' if cond else 'FAIL', name, detail))
        if not cond:
            fails.append(name)

    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            try:
                pg = b.new_page(viewport={'width': 560, 'height': 700})
                errs = []
                pg.on('pageerror', lambda e: errs.append(str(e)))
                _load(pg, port)
                ck('스크립트 로드 오류 없음', not errs, '-> %s' % (errs[:1] or ''))

                # ── (a) 동일 데이터 refetch 는 DOM 을 다시 안 짓는다 ──
                print('  --- (a) 동일 데이터 refetch = 재렌더 스킵 ---')
                pg.evaluate(_FIRST)
                pg.wait_for_selector('#feed-body .feed-card[data-href]')
                # 첫 카드에 표식 박기 - 재렌더되면 새 노드로 바뀌어 표식이 사라진다.
                pg.eval_on_selector('#feed-body .feed-card[data-href]',
                                    "e => e.setAttribute('data-probe', 'keep')")
                cnt1 = pg.eval_on_selector('#feed-count-header', 'e => e.textContent')
                pg.evaluate("window.loadFeedPane()")   # 동일 __data 로 소켓 refetch 흉내
                pg.wait_for_timeout(200)
                survived = pg.query_selector('#feed-body [data-probe="keep"]') is not None
                ck('동일 데이터 refetch 가 카드 DOM 을 다시 짓지 않는다 (표식 생존)', survived)

                # ── (c) 필터/토글은 그대로 다시 그린다 ──
                print('  --- (c) 토글은 스킵하지 않고 다시 그린다 ---')
                pg.evaluate("window.setFeedLayoutMode('table')")
                pg.wait_for_timeout(150)
                has_table = pg.query_selector('#feed-body .feed-data-table') is not None
                probe_gone = pg.query_selector('#feed-body [data-probe="keep"]') is None
                ck('레이아웃 토글이 표로 다시 렌더된다', has_table)
                ck('토글은 이전 카드 DOM 을 버린다 (스킵 아님)', probe_gone)
                pg.evaluate("window.setFeedLayoutMode('cards')")
                pg.wait_for_timeout(150)

                # ── (b) 스크롤 내린 채 새 글 prepend → 앵커 유지 + pill ──
                print('  --- (b) 스크롤 보존 + "새 글 N건" pill ---')
                pg.wait_for_selector('#feed-body .feed-card[data-href]')
                pg.eval_on_selector('#feed-body', 'e => { e.scrollTop = 320; }')
                pg.wait_for_timeout(60)
                before = pg.evaluate("""() => {
                  const body = document.getElementById('feed-body');
                  const bt = body.getBoundingClientRect().top;
                  for (const el of body.querySelectorAll('.feed-card[data-href]')) {
                    const r = el.getBoundingClientRect();
                    if (r.bottom > bt + 1) return { link: el.dataset.href, top: r.top - bt };
                  }
                  return null;
                }""")
                ck('스크롤 후 앵커 카드를 찾았다', bool(before), '-> %s' % before)
                # 새 글 5건을 위에 prepend (더 최신 timestamp) 후 refetch.
                pg.evaluate("""() => {
                  const nw = window._rows('NEW', 5, 900);   // base 900 > 800 = 더 최신
                  window.__data = nw.concat(window._rows('A', 120, 800));
                  window.loadFeedPane();
                }""")
                pg.wait_for_timeout(250)
                after_top = pg.evaluate("""(link) => {
                  const body = document.getElementById('feed-body');
                  const bt = body.getBoundingClientRect().top;
                  const el = body.querySelector('[data-href="' + CSS.escape(link) + '"]:not(.feed-detail-row)');
                  return el ? (el.getBoundingClientRect().top - bt) : null;
                }""", (before or {}).get('link'))
                if before and after_top is not None:
                    drift = abs(after_top - before['top'])
                    ck('앵커 카드가 뷰포트 위치를 유지한다 (drift < 8px)', drift < 8,
                       '-> before=%.1f after=%.1f drift=%.1f' % (before['top'], after_top, drift))
                else:
                    ck('앵커 카드가 뷰포트 위치를 유지한다 (drift < 8px)', False,
                       '-> 앵커 카드 소실 (after=%s)' % after_top)
                pill_txt = pg.eval_on_selector('#feed-newpill', 'e => e.style.display !== "none" ? e.textContent : ""') \
                    if pg.query_selector('#feed-newpill') else ''
                ck('"새 글 N건" pill 이 뜬다', '새 글' in pill_txt, '-> %r' % pill_txt)
                ck('pill 이 새 글 건수(5)를 말한다', '5' in pill_txt, '-> %r' % pill_txt)

                # pill 클릭 → 맨 위로 + pill 숨김
                if pg.query_selector('#feed-newpill'):
                    pg.eval_on_selector('#feed-newpill', 'e => e.click()')
                    # smooth 스크롤 완료를 기다린다(고정 400ms 는 애니메이션 중간을 재서 간헐 FAIL 했다 - v6.7.692).
                    #   2초 안에 맨 위에 못 가면 그대로 FAIL 이다(판정 완화 아님).
                    try:
                        pg.wait_for_function("document.getElementById('feed-body').scrollTop <= 2", timeout=2000)
                    except Exception:
                        pass
                    st = pg.eval_on_selector('#feed-body', 'e => e.scrollTop')
                    ck('pill 클릭이 맨 위로 스크롤한다', st <= 6, '-> scrollTop=%s' % st)

                # ── (e) [v6.7.694] 못 본 새 글은 다음 refetch 에도 남는다 ──
                #   라이브 보고: 새 글 도착 직후 본문 수집 등으로 한 번 더 렌더가 돌면(새 글 0) pill 이 사라졌다.
                print('  --- (e) pill 유지 · 누적 · 스크롤로 소진 ---')
                PILL = "(() => { const e = document.getElementById('feed-newpill'); return e && e.style.display !== 'none' ? e.textContent.trim() : null })()"
                pg.evaluate(_FIRST)
                pg.wait_for_timeout(300)
                pg.evaluate("document.getElementById('feed-body').scrollTop = 1200")
                pg.wait_for_timeout(150)
                pg.evaluate("""() => { window.__data = window._rows('P', 3, 900).map(a => Object.assign(a, { content: '' })).concat(window.__data); window.loadFeedPane(); }""")
                pg.wait_for_timeout(300)
                p1 = pg.evaluate(PILL)
                pg.evaluate("""() => { window.__data = window.__data.map(a => a.link.startsWith('P_') ? Object.assign({}, a, { content: '본문 수집됨' }) : a); window.loadFeedPane(); }""")
                pg.wait_for_timeout(300)
                p2 = pg.evaluate(PILL)
                ck('새 글 0건인 후속 refetch(본문 수집)에도 pill 유지', p1 == '새 글 3건' and p2 == '새 글 3건', '-> %r -> %r' % (p1, p2))
                pg.evaluate("""() => { window.__data = window._rows('Q', 2, 950).concat(window.__data); window.loadFeedPane(); }""")
                pg.wait_for_timeout(300)
                p3 = pg.evaluate(PILL)
                ck('다음 사이클 새 글이 누적된다 (3+2)', p3 == '새 글 5건', '-> %r' % p3)
                # 맨 위 쪽 새 글 몇 개만 화면에 들어오게 올린다 -> 본 만큼 줄어든다(0 은 아님)
                pg.evaluate("""() => { const b = document.getElementById('feed-body');
                  const el = b.querySelector('[data-href="P_1"]'); b.scrollTop += el.getBoundingClientRect().top - b.getBoundingClientRect().top - 2; }""")
                pg.wait_for_timeout(250)
                p4 = pg.evaluate(PILL)
                n4 = int(''.join(ch for ch in (p4 or '0') if ch.isdigit()) or 0)
                ck('스크롤로 일부를 보면 건수가 줄어든다', 0 < n4 < 5, '-> %r' % p4)
                pg.evaluate("document.getElementById('feed-body').scrollTop = 0")
                pg.wait_for_timeout(250)
                ck('맨 위로 가면 pill 이 걷힌다', pg.evaluate(PILL) is None)

                # ── (d) [v6.7.692] 분류(게임별) 모드 - 섹션마다 자체 스크롤 ──
                #   v6.7.688 은 바깥 #feed-body 만 보존해서 데이터가 바뀌면 모든 섹션이 맨 위로 튕기고
                #   pill 도 안 떴다(라이브 보고). 섹션 안에서 앵커 유지 + 헤더 칩 + 전역 pill 을 잰다.
                print('  --- (d) 게임별 모드 섹션 스크롤 보존 ---')
                pg.evaluate("""() => {
                  _localSettingsData = { games: ['G1','G2'], visibility: {}, limits: { G1: 0, G2: 0 } };
                  window.__mk = (g, tag, n, base) => { const o = []; for (let i = 0; i < n; i++) {
                    const ts = 'T' + String(500000 + base - i);
                    o.push({ link: tag + g + '_' + i, title: g + ' ' + tag + i, game: g, source: 'DC', board_name: 'b',
                      date: ts, timestamp: ts, content: '본문', detected_keywords: [] }); } return o; };
                  window.setFeedViewMode('by-game');
                  window.__data = window.__mk('G1', 'A', 40, 800).concat(window.__mk('G2', 'A', 40, 700));
                  window.loadFeedPane();
                }""")
                pg.wait_for_timeout(400)
                sec_js = """(k) => document.querySelector('#feed-body [data-sec="' + k + '"] .feed-game-section-body')"""
                scrollable = pg.evaluate("(k) => { const sb = (" + sec_js + ")(k); return sb ? sb.scrollHeight > sb.clientHeight + 20 : false; }", 'g:G1')
                ck('게임별 섹션이 자체 스크롤한다(측정 전제)', scrollable)
                snap = """(k) => { const sb = (%s)(k); const t = sb.getBoundingClientRect().top;
                  const el = [...sb.querySelectorAll('[data-href]')].find(e => e.getBoundingClientRect().bottom > t + 1);
                  return { link: el.dataset.href, top: el.getBoundingClientRect().top - t, st: sb.scrollTop }; }""" % sec_js
                pg.evaluate("(k) => { (" + sec_js + ")(k).scrollTop = 500; }", 'g:G1')
                pg.wait_for_timeout(100)
                b4 = pg.evaluate(snap, 'g:G1')
                pg.evaluate("""() => { window.__data = window.__mk('G1', 'N', 3, 900).concat(window.__mk('G2', 'N', 2, 900)).concat(window.__data);
                  window.loadFeedPane(); }""")
                pg.wait_for_timeout(400)
                af = pg.evaluate("""(a) => { const sb = (%s)('g:G1'); const el = sb.querySelector('[data-href="' + a + '"]');
                  return el ? el.getBoundingClientRect().top - sb.getBoundingClientRect().top : null; }""" % sec_js, b4['link'])
                drift = abs(af - b4['top']) if af is not None else 9999
                ck('게임별 - 스크롤한 섹션의 앵커가 제자리 (drift < 8px)', drift < 8,
                   '-> st=%s drift=%.1f' % (b4['st'], drift))
                chips = pg.evaluate("""() => [...document.querySelectorAll('#feed-body .feed-sec-newchip')].map(c =>
                  [c.closest('[data-sec]').dataset.sec, c.textContent])""")
                ck('게임별 - 스크롤 중이던 G1 섹션에만 "새 글 3" 칩', chips == [['g:G1', '새 글 3']], '-> %s' % chips)
                pill = pg.evaluate("(() => { const e = document.getElementById('feed-newpill'); return e && e.style.display !== 'none' ? e.textContent : null })()")
                ck('게임별 - 전역 pill 이 뜬다', bool(pill) and '3' in pill, '-> %r' % pill)
                # 칩이 떠 있는 채로 한 사이클 더 - 건수가 누적된다
                pg.evaluate("""() => { window.__data = window.__mk('G1', 'M', 2, 950).concat(window.__data); window.loadFeedPane(); }""")
                pg.wait_for_timeout(400)
                chips2 = pg.evaluate("() => [...document.querySelectorAll('#feed-body .feed-sec-newchip')].map(c => c.textContent)")
                ck('게임별 - 다음 사이클에 칩 건수가 누적된다(3+2)', chips2 == ['새 글 5'], '-> %s' % chips2)
                pg.eval_on_selector('#feed-body .feed-sec-newchip', 'e => e.click()')
                try:
                    pg.wait_for_function("(" + sec_js + ")('g:G1').scrollTop <= 2", timeout=2000)
                except Exception:
                    pass
                st_after = pg.evaluate("(k) => (" + sec_js + ")(k).scrollTop", 'g:G1')
                left = pg.evaluate("() => document.querySelectorAll('#feed-body .feed-sec-newchip').length")
                ck('게임별 - 칩 클릭이 그 섹션을 맨 위로 + 칩 제거', st_after <= 6 and left == 0,
                   '-> scrollTop=%s chips=%s' % (st_after, left))
                pg.evaluate("window.setFeedViewMode('timeline')")
                pg.wait_for_timeout(200)

                # 스크린샷 - 데스크톱 pane(480) / 모바일(390)
                pg.evaluate("window.__data = window._rows('A', 30, 800); window.loadFeedPane()")
                pg.wait_for_timeout(200)
                pg.eval_on_selector('.pane-window', "e => e.style.width='480px'")
                pg.wait_for_timeout(60)
                pg.locator('#feed-tab').screenshot(path=os.path.join(_SHOT_DIR, 'feed_desktop_480.png'))
                pg.eval_on_selector('.pane-window', "e => e.style.width='390px'")
                pg.set_viewport_size({'width': 420, 'height': 700})
                pg.wait_for_timeout(60)
                pg.locator('#feed-tab').screenshot(path=os.path.join(_SHOT_DIR, 'feed_mobile_390.png'))

                ck('런타임 오류 없음', not errs, '-> %s' % (errs[:1] or ''))
            finally:
                # **반드시 닫는다** - 남긴 브라우저가 호스트 load 를 올려 imt-deploy 게이트를 막는다.
                b.close()
    finally:
        srv.shutdown()

    print()
    print('RESULT: ' + ('ALL PASS' if not fails else 'FAIL -> ' + ', '.join(fails)))
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
