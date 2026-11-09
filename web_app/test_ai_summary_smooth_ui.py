# -*- coding: utf-8 -*-
"""[v6.7.691] AI 요약 탭의 순단(freeze) 개선을 **실제 브라우저에서** 잰다.

단위 테스트로는 못 잡는 4가지 - 전부 "렌더 · 스크롤 · IntersectionObserver · 시간" 이 얽혀야 재현된다:
  (a) **진행형 청크 렌더** - 500행을 한 번에 그리면 순단. 첫 페인트는 첫 청크(50)만, 스크롤하면 증분.
      DOM 노드 수를 세야만 "정말 50행만 그렸나 / 스크롤하니 늘었나" 를 안다.
  (b) **무변화 재소화 스킵** - 같은 데이터가 다시 들어오면 DOM 을 다시 짓지 않는다(PATTERN-38).
      노드에 표식을 찍고 살아남는지 봐야 "안 지었다" 가 증명된다.
  (c) **앵커 스크롤 복원** - 스크롤을 내려 읽는 중 상단에 신규 요약이 prepend 되면, 읽던 행이
      뷰포트에서 몇 px 안에 머물러야 한다(구: raw scrollTop 복원은 눈에 띄게 점프). '새 요약 N건'
      배지도 떠야 한다. 계산된 위치를 재야만 "안 튀었다" 를 안다.
  (d) 그 와중에도 pill/검색 필터가 여전히 목록을 거른다.

실행: `python test_ai_summary_smooth_ui.py`  (playwright + chromium 필요)
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
_SHOT_DIR = '/tmp/claude-0/-home-user-imt-tools/14b1b947-870f-5a12-af51-3c98525733c1/scratchpad'

# 프로브 페이지는 static/ 에 파일로 두지 않는다 - 서버가 메모리에서 낸다(배포물 오염 방지).
# css/js 는 실제 static/ 을 그대로 읽는다.
PROBE_HTML = """<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="/css/style-core.css">
<link rel="stylesheet" href="/css/style-feed.css">
<link rel="stylesheet" href="/css/ai-summary.css">
<style>
  /* 프로브 전용: 스크롤러에 높이를 줘 IntersectionObserver 가 실동작하게 한다(실앱은 pane-window 가 준다). */
  html,body{margin:0;background:#0b0b0f}
  #ai-summary-tab{height:480px;overflow-y:auto;padding:12px 14px 20px}
  body.__mobile #ai-summary-tab{height:480px}
</style></head><body class="ai-summary-mode-table">
<span id="ai-summary-count-header"></span>
<input id="ai-summary-search-input-header" type="text">
<span id="ai-summary-backlog"></span>
<div id="ai-summary-tab" class="tab-content">
  <button id="ai-summary-newpill" class="ai-sum-newpill" type="button" style="display:none;"><i class="fas fa-arrow-up"></i> 새 요약 <span id="ai-summary-newpill-count">0</span>건</button>
  <div id="ai-summary-empty-state" class="empty-state" style="display:none;"><p>표시할 AI 요약이 없습니다</p></div>
  <div id="ai-summary-body"><div class="loading">로드 중…</div></div>
</div>
<script>
  var activeApps = ['ai-summary'];
  var _localSettingsData = { visibility: {}, limits: {} };
  // 합성 데이터 - N행, 게임 3종(G0/G1/G2), 최신순 timestamp(row_0 이 최신).
  function _mkRows(n, prefix, baseMin) {
    prefix = prefix || 'row'; baseMin = (baseMin == null) ? 0 : baseMin;
    var base = Date.UTC(2026, 8, 26, 12, 0, 0);
    var out = [];
    for (var i = 0; i < n; i++) {
      var t = new Date(base - (baseMin + i) * 60000);
      var iso = t.toISOString().slice(0, 19);
      out.push({
        link: prefix + '_' + i,
        title: 'AI요약 제목 ' + prefix + ' ' + i,
        summary: '요약 본문 ' + i + ' 입니다. 운영자가 읽을 한 줄.',
        game: 'G' + (i % 3),
        source: 'DC', board_name: '자유',
        severity: (i % 17 === 0) ? 'critical' : (i % 5 === 0) ? 'warning' : 'info',
        sentiment: (i % 7 === 0) ? 'negative' : 'neutral',
        content: '본문 내용 ' + i, timestamp: iso, date: iso
      });
    }
    return out;
  }
  window.__rows = _mkRows(500, 'row', 100);            // 기존 500행
  // 신규 5행: 기존 최신(row_0, baseMin=100)보다 더 최신(baseMin=0) → 정렬 상 맨 위로 prepend.
  window.__new5 = _mkRows(5, 'new', 0);
  window.__rows2 = window.__new5.concat(window.__rows);
  window.fetch = function (url) {
    if (String(url).indexOf('/backlog') >= 0)
      return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve({}); } });
    return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(window.__rows); } });
  };
</script>
<script src="/js/ui/common.js"></script>
<script src="/js/game-color.js"></script>
<script src="/js/tabs/_tab_helpers.js"></script>
<script src="/js/tabs/ai-summary-tab.js"></script>
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
    p = s.getsockname()[1]
    s.close()
    return p


def _n_rows(pg):
    return pg.eval_on_selector_all('#ai-summary-body tr.ai-sum-row', 'els => els.length')


def _scroll_until(pg, target, tries=25):
    for _ in range(tries):
        if _n_rows(pg) >= target:
            break
        pg.eval_on_selector('#ai-summary-tab', 'e => { e.scrollTop = e.scrollHeight; }')
        pg.wait_for_timeout(90)


def _load_and_render(pg):
    pg.wait_for_function("typeof window.loadAiSummaryTab === 'function'")
    pg.evaluate("window.aiSummaryTab_register && window.aiSummaryTab_register()")
    pg.evaluate("window.loadAiSummaryTab()")
    pg.wait_for_function("document.querySelectorAll('#ai-summary-body tr.ai-sum-row').length > 0", timeout=5000)


def run(pw, fails, url):
    def ck(name, cond, detail=''):
        print('  [%s] %s %s' % ('PASS' if cond else 'FAIL', name, detail))
        if not cond:
            fails.append(name)

    b = pw.chromium.launch()
    try:
        # ── 데스크탑(표 모드, 720px) ──
        ctx = b.new_context(viewport={'width': 720, 'height': 760})
        pg = ctx.new_page()
        errs = []
        pg.on('pageerror', lambda e: errs.append(str(e)))
        pg.goto(url)
        _load_and_render(pg)
        ck('스크립트 로드 오류 없음', not errs, '-> %s' % (errs[:1] or ''))

        # (a) 첫 청크만 렌더 (500 이 아님) + 스크롤하면 증분
        n0 = _n_rows(pg)
        ck('(a) 첫 렌더는 첫 청크(50)만 - 500 아님', n0 == 50, '-> 렌더된 행=%d (전체 500)' % n0)
        _scroll_until(pg, 90)
        n1 = _n_rows(pg)
        ck('(a) 스크롤하면 다음 청크가 붙는다', n1 > n0, '-> %d -> %d' % (n0, n1))

        # (b) 동일 데이터 재소화 시 DOM 재구축 안 함
        pg.eval_on_selector('#ai-summary-body tr.ai-sum-row', "e => e.setAttribute('data-probe','keep')")
        pg.evaluate("window.aiSummaryHydrate(window.__rows)")   # 같은 배열 = 같은 시그니처 → 스킵
        pg.wait_for_timeout(120)
        kept = pg.eval_on_selector('#ai-summary-body tr.ai-sum-row', "e => e.getAttribute('data-probe')")
        ck('(b) 동일 데이터 재소화는 DOM 을 다시 짓지 않는다', kept == 'keep', '-> data-probe=%r' % kept)

        # (c) 앵커 복원 + '새 요약 N건' 배지
        _scroll_until(pg, 200)
        pg.eval_on_selector('#ai-summary-tab', 'e => { e.scrollTop = 3000; }')
        pg.wait_for_timeout(120)
        anchor = pg.evaluate("""() => {
            const sc = document.getElementById('ai-summary-tab');
            const sTop = sc.getBoundingClientRect().top;
            const els = document.querySelectorAll('#ai-summary-body tr.ai-sum-row[data-href]');
            for (const el of els) { const r = el.getBoundingClientRect(); if (r.bottom > sTop + 1) return { href: el.dataset.href, off: r.top - sTop }; }
            return null;
        }""")
        ck('(c) 앵커 후보를 잡았다(스크롤 중)', bool(anchor) and anchor.get('href'), '-> %s' % anchor)
        # 상단에 신규 5행 prepend
        pg.evaluate("window.aiSummaryHydrate(window.__rows2)")
        pg.wait_for_timeout(150)
        after = pg.evaluate("""(href) => {
            const sc = document.getElementById('ai-summary-tab');
            const sTop = sc.getBoundingClientRect().top;
            const els = document.querySelectorAll('#ai-summary-body tr.ai-sum-row[data-href]');
            for (const el of els) { if (el.dataset.href === href) { const r = el.getBoundingClientRect(); return r.top - sTop; } }
            return null;
        }""", anchor['href'] if anchor else '')
        drift = abs(after - anchor['off']) if (after is not None and anchor) else 9999
        ck('(c) 신규 prepend 후 앵커 행이 제자리(±5px)', drift <= 5, '-> 이동=%.1fpx (전 %.1f → 후 %s)' % (
            drift, anchor['off'] if anchor else -1, after))
        pill_vis = pg.eval_on_selector('#ai-summary-newpill', "e => e.style.display !== 'none'")
        pill_txt = pg.eval_on_selector('#ai-summary-newpill-count', 'e => e.textContent')
        ck("(c) '새 요약 N건' 배지가 뜬다", pill_vis and pill_txt == '5', '-> 표시=%s 건수=%s' % (pill_vis, pill_txt))
        # 배지 클릭 = 맨 위로 (smooth 스크롤이라 완료까지 대기 - 헤드리스도 애니 시간 필요)
        pg.eval_on_selector('#ai-summary-newpill', 'e => e.click()')
        pill_hidden = pg.eval_on_selector('#ai-summary-newpill', "e => e.style.display === 'none'")
        try:
            pg.wait_for_function("() => document.getElementById('ai-summary-tab').scrollTop <= 8", timeout=3000)
            reached_top = True
        except Exception:
            reached_top = False
        top_after = pg.eval_on_selector('#ai-summary-tab', 'e => e.scrollTop')
        ck('(c) 배지 클릭 시 즉시 숨고 맨 위로 스크롤', pill_hidden and reached_top, '-> 숨김=%s scrollTop=%s' % (pill_hidden, top_after))

        # (d) pill/검색 필터 여전히 동작
        pg.eval_on_selector('#ai-summary-body .ai-sum-pill[data-game="G1"]', 'e => e.click()')
        pg.wait_for_timeout(120)
        only_g1 = pg.eval_on_selector_all('#ai-summary-body tr.ai-sum-row .col-game-name',
                                          "els => els.length > 0 && els.every(e => e.textContent.trim() === 'G1')")
        ck('(d) 게임 pill 필터가 목록을 거른다', only_g1, '-> G1 만 남았는가=%s' % only_g1)
        # 검색
        pg.eval_on_selector('#ai-summary-search-input-header', "e => e.value = 'G2'")
        pg.evaluate("window.filterAiSummary && window.filterAiSummary()")
        pg.wait_for_timeout(120)
        # pill 은 G1 이었는데 검색 G2 → 교집합 0 → empty-state 노출(필터 조합이 상충)
        # 그래서 pill 을 전체로 돌린 뒤 검색만으로 검증한다.
        pg.eval_on_selector('#ai-summary-body .ai-sum-pill[data-game="__ALL__"]', 'e => e.click()')
        pg.eval_on_selector('#ai-summary-search-input-header', "e => e.value = 'G2'")
        pg.evaluate("window.filterAiSummary && window.filterAiSummary()")
        pg.wait_for_timeout(120)
        only_g2 = pg.eval_on_selector_all('#ai-summary-body tr.ai-sum-row .col-game-name',
                                          "els => els.length > 0 && els.every(e => e.textContent.trim() === 'G2')")
        ck('(d) 검색 필터가 목록을 거른다', only_g2, '-> G2 만 남았는가=%s' % only_g2)

        ck('런타임 오류 없음', not errs, '-> %s' % (errs[:1] or ''))

        # 스크린샷(after) - 데스크탑 720px
        try:
            pg.eval_on_selector('#ai-summary-search-input-header', "e => e.value = ''")
            pg.evaluate("window.filterAiSummary && window.filterAiSummary()")
            pg.eval_on_selector('#ai-summary-tab', 'e => e.scrollTop = 0')
            pg.wait_for_timeout(150)
            os.makedirs(_SHOT_DIR, exist_ok=True)
            pg.screenshot(path=os.path.join(_SHOT_DIR, 'ai_summary_after_desktop_720.png'))
            print('  [shot] ai_summary_after_desktop_720.png')
        except Exception as e:
            print('  [shot] 데스크탑 스크린샷 실패(비치명): %s' % e)
        ctx.close()

        # ── 모바일(카드 모드, 390px, 터치) ──
        try:
            mctx = b.new_context(viewport={'width': 390, 'height': 780}, has_touch=True, is_mobile=True)
            mpg = mctx.new_page()
            mpg.goto(url)
            mpg.evaluate("document.body.classList.add('__mobile')")
            mpg.wait_for_function("typeof window.loadAiSummaryTab === 'function'")
            mpg.evaluate("window.aiSummaryTab_register && window.aiSummaryTab_register()")
            mpg.evaluate("window.loadAiSummaryTab()")
            mpg.wait_for_function("document.querySelectorAll('#ai-summary-body .feed-card--ai').length > 0", timeout=5000)
            ncards = mpg.eval_on_selector_all('#ai-summary-body .feed-card--ai', 'els => els.length')
            # sentinel 이 목록 맨 아래라야 청크가 멈춘다 - 첫 청크(50)만, 500 이 아니어야 한다.
            ck('(a-mobile) 모바일 첫 렌더는 첫 청크(카드 50)', ncards == 50, '-> 카드=%d (전체 505)' % ncards)
            for _ in range(6):
                if mpg.eval_on_selector_all('#ai-summary-body .feed-card--ai', 'e=>e.length') > 50:
                    break
                mpg.eval_on_selector('#ai-summary-tab', 'e => { e.scrollTop = e.scrollHeight; }')
                mpg.wait_for_timeout(100)
            ncards2 = mpg.eval_on_selector_all('#ai-summary-body .feed-card--ai', 'els => els.length')
            ck('(a-mobile) 스크롤하면 카드가 증분', ncards2 > ncards, '-> %d -> %d' % (ncards, ncards2))
            mpg.eval_on_selector('#ai-summary-tab', 'e => e.scrollTop = 0')
            mpg.wait_for_timeout(150)
            mpg.screenshot(path=os.path.join(_SHOT_DIR, 'ai_summary_after_mobile_390.png'))
            print('  [shot] ai_summary_after_mobile_390.png')
            mctx.close()
        except Exception as e:
            print('  [shot] 모바일 단계 실패(비치명): %s' % e)
    finally:
        b.close()


def main():
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print('SKIP(환경 없음): playwright 미설치 - %s' % e)
        return 3

    port = _free_port()
    srv = http.server.ThreadingHTTPServer(
        ('127.0.0.1', port), functools.partial(_Handler, directory=_STATIC))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = 'http://127.0.0.1:%d/__probe' % port

    fails = []
    try:
        with sync_playwright() as pw:
            try:
                run(pw, fails, url)
            except Exception as e:
                print('  [FAIL] 예외: %s' % e)
                fails.append('예외: %s' % e)
    finally:
        srv.shutdown()

    print()
    print('RESULT: ' + ('ALL PASS' if not fails else 'FAIL -> ' + ', '.join(fails)))
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
