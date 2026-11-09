# -*- coding: utf-8 -*-
"""[v6.7.731] AI 요약 탭 결함 3종을 실제 브라우저(ai-summary-tab.js 원본 로드)에서 잰다.

단위로는 못 잡는다 - 전부 렌더/정규식/IntersectionObserver/필터가 실제 DOM 위에서 얽혀야 재현된다.
  (2) **하이라이트 엔티티 보존** - 이스케이프된 HTML 위에서 검색어를 찾으면 'amp'(=&amp; 조각)가
      엔티티 한가운데를 <mark> 로 갈라 "R&D" 가 "R&amp;D" 로 깨져 보였다. 원문 기준 분할 후 조각별
      이스케이프하면 안 깨진다. 렌더된 셀의 textContent 를 재야만 "안 깨졌다" 를 안다.
  (3) **IO 폴백 중복 삽입** - IntersectionObserver 생성이 실패하면(구형/차단) 폴백이 rendered 를 0
      으로 리셋해 첫 청크(50행)를 두 번 그렸다. 리셋 제거 후엔 행 수 == 데이터 수여야 한다.
      IO 를 강제로 던지게 하고 DOM 행 수/고유 key 를 세야만 "중복 안 났다" 를 안다.
  (R) **_rerenderAiSummary** - 설정에서 게임을 끄고 저장하면 그 게임 행이 즉시 사라져야 한다
      (구: 다음 신규 요약까지 남음). 가시성 끈 뒤 재렌더 전/후 행 수를 재야만 증명된다.

실행: `python3 test_ai_summary_defects.py` (playwright + chromium 필요)
종료코드: 0 = ALL PASS / 1 = FAIL / 3 = 환경 없음(**PASS 아님**)
"""
import functools
import http.server
import json
import os
import socket
import sys
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_STATIC = os.path.join(_HERE, 'static')

PROBE_HTML = """<!doctype html><html><head><meta charset="utf-8">
<style>
  html,body{margin:0;background:#0b0b0f;color:#eee}
  #ai-summary-tab{height:600px;overflow-y:auto;padding:12px 14px 20px}
</style></head><body class="ai-summary-mode-table">
<span id="ai-summary-count-header"></span>
<input id="ai-summary-search-input-header" type="text">
<span id="ai-summary-backlog"></span>
<div id="ai-summary-tab" class="tab-content">
  <button id="ai-summary-newpill" class="ai-sum-newpill" type="button" style="display:none;"><span id="ai-summary-newpill-count">0</span></button>
  <div id="ai-summary-empty-state" class="empty-state" style="display:none;"><p>표시할 AI 요약이 없습니다</p></div>
  <div id="ai-summary-body"><div class="loading">로드 중…</div></div>
</div>
<script>
  var activeApps = ['ai-summary'];
  var _localSettingsData = { visibility: {}, limits: {} };
  window.fetch = function (url) {
    if (String(url).indexOf('/backlog') >= 0)
      return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve({}); } });
    return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(window.__TESTROWS || []); } });
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


def _row(i, game, title, summary, source='DC'):
    iso = '2026-09-26T%02d:00:00' % (23 - (i % 24))
    return {'link': 'L%d' % i, 'title': title, 'summary': summary, 'game': game,
            'source': source, 'board_name': '자유', 'severity': 'info',
            'sentiment': 'neutral', 'content': '본문 %d' % i, 'timestamp': iso, 'date': iso}


def _open(ctx, url, rows, io_throw=False):
    pg = ctx.new_page()
    if io_throw:
        pg.add_init_script("window.IntersectionObserver = function(){ throw new Error('io-off'); };")
    pg.add_init_script("window.__TESTROWS = %s;" % json.dumps(rows))
    pg.goto(url)
    pg.wait_for_function("typeof window.loadAiSummaryTab === 'function'")
    pg.evaluate("window.aiSummaryTab_register && window.aiSummaryTab_register()")
    pg.evaluate("window.loadAiSummaryTab()")
    return pg


def run(pw, fails, url):
    def ck(name, cond, detail=''):
        print('  [%s] %s %s' % ('PASS' if cond else 'FAIL', name, detail))
        if not cond:
            fails.append(name)

    b = pw.chromium.launch()
    try:
        ctx = b.new_context(viewport={'width': 760, 'height': 820})

        # ── (2) 하이라이트 엔티티 보존 ──
        # title 에 엔티티(R&D), summary 에 검색어 'amp' 를 넣어 "검색은 summary 로 매칭되지만
        # title 에는 'amp' 가 원문에 없어 하이라이트되면 안 된다" 를 만든다.
        pg = _open(ctx, url, [_row(0, 'G0', 'R&D 로드맵 공지', 'ramp up 관련 요약입니다')])
        pg.wait_for_function("document.querySelectorAll('#ai-summary-body tr.ai-sum-row').length === 1", timeout=5000)
        pg.eval_on_selector('#ai-summary-search-input-header', "e => e.value = 'amp'")
        pg.evaluate("window.filterAiSummary && window.filterAiSummary()")
        pg.wait_for_timeout(80)
        n = pg.eval_on_selector_all('#ai-summary-body tr.ai-sum-row', 'els => els.length')
        ck("(2) summary 가 'amp' 매칭이라 행이 남는다", n == 1, '-> 행=%d' % n)
        title_txt = pg.eval_on_selector('#ai-summary-body .col-title-text', 'e => e.textContent')
        title_marks = pg.eval_on_selector_all('#ai-summary-body .col-title-text mark', 'els => els.length')
        ck("(2) 제목 엔티티 보존(R&D, 'amp;' 조각 없음)",
           ('R&D' in title_txt) and ('amp;' not in title_txt),
           '-> textContent=%r' % title_txt)
        ck("(2) 제목엔 하이라이트 안 붙음(원문에 'amp' 없음)", title_marks == 0, '-> 제목 mark=%d' % title_marks)
        sum_marks = pg.eval_on_selector_all('#ai-summary-body .col-content-text mark', 'els => els.length')
        ck("(2) 요약엔 하이라이트가 붙는다(ramp 의 amp)", sum_marks >= 1, '-> 요약 mark=%d' % sum_marks)
        pg.close()

        # ── (3) IO 폴백 중복 삽입 없음 ──
        rows120 = [_row(i, 'G%d' % (i % 3), '제목 %d' % i, '요약 %d' % i) for i in range(120)]
        pg = _open(ctx, url, rows120, io_throw=True)
        pg.wait_for_function("document.querySelectorAll('#ai-summary-body tr.ai-sum-row').length > 0", timeout=5000)
        pg.wait_for_timeout(120)
        total = pg.eval_on_selector_all('#ai-summary-body tr.ai-sum-row', 'els => els.length')
        distinct = pg.eval_on_selector_all('#ai-summary-body tr.ai-sum-row',
                                           "els => new Set(els.map(e => e.getAttribute('data-key'))).size")
        ck("(3) IO 실패 폴백이 전행을 중복 없이 1회만 그린다(120==120)",
           total == 120 and distinct == 120, '-> 행=%d 고유key=%d (데이터 120)' % (total, distinct))
        pg.close()

        # ── (R) _rerenderAiSummary: 가시성 끄면 즉시 사라진다 ──
        rowsAB = [_row(i, 'G0', 'A 제목 %d' % i, 'A 요약 %d' % i) for i in range(5)] + \
                 [_row(100 + i, 'G1', 'B 제목 %d' % i, 'B 요약 %d' % i) for i in range(5)]
        pg = _open(ctx, url, rowsAB, io_throw=True)
        pg.wait_for_function("document.querySelectorAll('#ai-summary-body tr.ai-sum-row').length === 10", timeout=5000)

        def _game_counts():
            return pg.evaluate("""() => {
                const out = {};
                document.querySelectorAll('#ai-summary-body .col-game-name').forEach(e => {
                    const g = e.textContent.trim(); out[g] = (out[g]||0)+1;
                });
                return out;
            }""")

        before = _game_counts()
        ck("(R) 재렌더 전 G1 행 5개(대조군)", before.get('G1') == 5, '-> %s' % before)
        pg.evaluate("window.exists = (typeof window._rerenderAiSummary === 'function')")
        exists = pg.evaluate("window.exists")
        ck("(R) window._rerenderAiSummary 가 노출돼 있다", exists is True, '-> %s' % exists)
        pg.evaluate("_localSettingsData.visibility = { 'G1': false }; window._rerenderAiSummary();")
        pg.wait_for_timeout(100)
        after = _game_counts()
        ck("(R) 가시성 끈 뒤 G1 행 즉시 사라짐(G0 는 유지)",
           (after.get('G1', 0) == 0) and (after.get('G0') == 5), '-> %s' % after)
        # 비활성이면 no-op (throw 없이 목록 불변)
        pg.evaluate("activeApps = []; _localSettingsData.visibility = {}; window._rerenderAiSummary();")
        pg.wait_for_timeout(60)
        noop = _game_counts()
        ck("(R) 비활성 탭에서는 no-op(목록 불변)",
           (noop.get('G1', 0) == 0) and (noop.get('G0') == 5), '-> %s' % noop)
        pg.close()

        ctx.close()
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
