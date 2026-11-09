# -*- coding: utf-8 -*-
"""[v6.7.738] 피드 동률 정렬 + 검색 중 헤더 건수를 **실제 브라우저에서** 잰다 (승인 개선 #10).

둘 다 feed-tab.js 안의 browser-global 로직이라 단위 테스트로는 못 태운다 - 실제 페이지에
feed-tab.js 를 로드하고 loadFeedPane() 로 데이터를 흘려 DOM 결과를 읽는다(test_feed_toggle_ui.py 선례).

  ① 같은 '분' 출처 정렬: 공식 3 > 카페(다음·네이버) 2 > DC 1.
     - 크롤러가 초를 출처별로 다르게 쓴다(DC·다음 :5x, 네이버·배그공식 :00, POE공식 실제 초).
       full timestamp DESC 로 먼저 가르면 DC/다음(:5x)이 공식·네이버(:00) 위로 떠 _srcPrio 에
       닿지 못한다 - 이것이 라이브에서 #10 이 안 먹던 실제 증상이다(PATTERN-57).
     - 그래서 '첫 카드=공식, 끝 카드=DC' 는 뮤턴트(분 비교를 빼고 초부터 가르는 옛 비교자)에서
       반드시 깨진다.
  ② 검색 중 건수: 검색어가 있으면 헤더가 '보이는 개수', 비우면 렌더 전체 건수로 복원.

실행: python3 test_feed_sort_search_ui.py   (playwright + chromium 필요)
종료코드: 0 = ALL PASS / 1 = FAIL / 3 = 환경 없음(PASS 아님 - 조용히 통과시키지 않는다)
"""
import functools
import http.server
import os
import socket
import sys
import threading

_STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')

# 프로브 페이지는 static/ 에 파일로 두지 않는다 - 테스트 픽스처가 배포물에 섞이지 않게 메모리에서 낸다.
PROBE_HTML = """<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="/css/style-feed.css"><style>body{margin:0;background:#0f1117}</style></head><body>
<div class="pane-window" style="width:480px">
  <div class="feed-src-bar"><div class="feed-srcpills">
    <button type="button" class="feed-srcpill active" data-source="">전체</button>
  </div></div>
  <input id="feed-search-input-header" type="text" oninput="filterFeedHeader()">
  <span id="feed-count-header"></span>
  <div id="feed-tab" class="tab-content" style="height:360px;padding:0">
    <div id="feed-body" class="feed-body"></div>
    <div id="feed-empty-state" class="empty-state" style="display:none"></div>
  </div>
</div>
<script>
  const API_BASE = '/api';
  let _localSettingsData = { games: ['G'], visibility: { G: true }, limits: { G: 0 } };
  window.__rows = [];
  // fetch 를 완전히 대역 - 테스트가 채운 window.__rows 를 그대로 돌려준다(타이밍 무관).
  window.fetch = function () {
    return Promise.resolve({ ok: true, json: async () => window.__rows });
  };
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

    fails = []

    def ck(name, cond, detail=''):
        print('  [%s] %s %s' % ('PASS' if cond else 'FAIL', name, detail))
        if not cond:
            fails.append(name)

    # 같은 '분' 안의 출처 4종 - 초(second)는 크롤러가 실제로 쓰는 값으로 준다(라이브 재현).
    #   DC·다음 = `second = 59 - crawl_idx`(:5x, crawler.py), 네이버·배그공식 = :00
    #   (naver_cafe.py / official_pubg.py), POE공식만 실제 초. 종전 픽스처는 넷 다 똑같은 :00 을 줘
    #   라이브에 없는 '초까지 동률' 을 합성했다 - 그래서 full timestamp DESC 로 가르는 옛 비교자도
    #   통과해버렸다(PATTERN-71: 채점 입력을 손으로 채우면 검사가 영원히 통과).
    #   핵심: 공식·네이버(:00)가 DC/다음(:5x)보다 '초 문자열' 은 작지만 같은 분이므로 출처 우선순위가
    #   이겨야 한다. 초로 먼저 가르는 회귀(옛 비교자)면 DC/다음이 위로 떠 반드시 FAIL 한다.
    _MIN = '2026-09-16T01:00'
    SORT_ROWS = [
        {'link': 'z_dc', 'title': 'DC글', 'game': 'G', 'source': 'DC', 'timestamp': _MIN + ':58', 'content': '', 'detected_keywords': []},   # 59 - crawl_idx(1)
        {'link': 'm_daum', 'title': '다음글', 'game': 'G', 'source': 'DAUM', 'timestamp': _MIN + ':59', 'content': '', 'detected_keywords': []},  # 59 - crawl_idx(0)
        {'link': 'n_naver', 'title': '네이버글', 'game': 'G', 'source': 'NAVER', 'timestamp': _MIN + ':00', 'content': '', 'detected_keywords': []},
        {'link': 'a_off', 'title': '공식글', 'game': 'G', 'source': 'OFFICIAL_WEB', 'timestamp': _MIN + ':00', 'content': '', 'detected_keywords': []},
    ]
    # 검색 건수용 - 3건은 'apple', 2건은 'banana'. 시각은 유니크(순서 무관).
    SEARCH_ROWS = []
    for i in range(5):
        kw = 'apple' if i < 3 else 'banana'
        SEARCH_ROWS.append({'link': 's%d' % i, 'title': '%s row %d' % (kw, i), 'game': 'G', 'source': 'DC',
                            'timestamp': '2026-09-16T0%d:00:00' % i, 'content': '', 'detected_keywords': []})

    import json as _json
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            try:
                pg = b.new_page()
                errs = []
                pg.on('pageerror', lambda e: errs.append(str(e)))
                pg.goto('http://127.0.0.1:%d/__probe' % port)
                pg.wait_for_function("typeof window.loadFeedPane === 'function'")
                ck('스크립트 로드 오류 없음', not errs, '-> %s' % (errs[:1] or ''))

                print('  --- ① 같은 분, 초는 출처별 실제값: 공식 > 카페 > DC ---')
                pg.evaluate("window.__rows = %s; window._feedBootHydrated = false;" % _json.dumps(SORT_ROWS))
                pg.evaluate("window.loadFeedPane()")
                pg.wait_for_selector("#feed-body .feed-card[data-href]")
                pg.wait_for_timeout(120)
                order = pg.evaluate(
                    "[...document.querySelectorAll('#feed-body .feed-card[data-href]')].map(c => c.dataset.source)")
                ck('같은 분 4건이 모두 렌더된다', len(order) == 4, '-> %s' % order)
                ck('첫 카드 = 공식(OFFICIAL_WEB)', order and order[0] == 'OFFICIAL_WEB', '-> %s' % order)
                ck('끝 카드 = DC', order and order[-1] == 'DC', '-> %s' % order)
                ck('가운데 두 장 = 카페(DAUM·NAVER)', set(order[1:3]) == {'DAUM', 'NAVER'}, '-> %s' % order)

                print('  --- ② 검색 중 헤더 건수 = 보이는 개수, 비우면 전체 복원 ---')
                pg.evaluate("window.__rows = %s; window._feedBootHydrated = false;" % _json.dumps(SEARCH_ROWS))
                pg.evaluate("window.loadFeedPane()")
                pg.wait_for_selector("#feed-body .feed-card[data-href]")
                pg.wait_for_timeout(120)
                cnt0 = pg.eval_on_selector('#feed-count-header', 'e => e.textContent')
                ck('검색 전 헤더 건수 = 전체(5)', cnt0 == '5', '-> %r' % cnt0)

                pg.eval_on_selector('#feed-search-input-header', "e => { e.value = 'apple'; }")
                pg.evaluate("window.filterFeedHeader()")
                pg.wait_for_timeout(300)   # 디바운스 150ms
                cnt1 = pg.eval_on_selector('#feed-count-header', 'e => e.textContent')
                shown = pg.evaluate(
                    "[...document.querySelectorAll('#feed-body .feed-card[data-href]')].filter(c => c.style.display !== 'none').length")
                ck("검색('apple') 중 헤더 건수 = 보이는 개수(3)", cnt1 == '3', '-> %r (실제 표시 %s)' % (cnt1, shown))
                ck('헤더 건수가 실제 보이는 카드 수와 일치', str(shown) == cnt1, '-> 표시=%s 헤더=%r' % (shown, cnt1))

                pg.eval_on_selector('#feed-search-input-header', "e => { e.value = ''; }")
                pg.evaluate("window.filterFeedHeader()")
                pg.wait_for_timeout(300)
                cnt2 = pg.eval_on_selector('#feed-count-header', 'e => e.textContent')
                ck('검색을 비우면 전체 건수(5)로 복원', cnt2 == '5', '-> %r' % cnt2)

                print('  --- ③ 분이 다르면 분이 이긴다 (출처 우선순위는 같은 분 안에서만) ---')
                # [v6.7.738] 주 정렬 키를 '분' 으로 바꾼 쪽의 위험 - 서로 다른 분의 순서가 출처 때문에
                #   뒤집히면 안 된다. ①은 같은 분만 봐서 이걸 채점하지 못했다(2라운드 재현 검증 지적).
                #   뮤턴트: 출처를 분보다 먼저 비교 / 분 대신 시(slice 13)로 자름 -> 공식이 맨 위로 떠 FAIL.
                MIX_ROWS = [
                    {'link': 'x_dc59', 'title': 'DC 1분 59', 'game': 'G', 'source': 'DC', 'timestamp': '2026-09-16T01:01:59', 'content': '', 'detected_keywords': []},
                    {'link': 'x_dc58', 'title': 'DC 1분 58', 'game': 'G', 'source': 'DC', 'timestamp': '2026-09-16T01:01:58', 'content': '', 'detected_keywords': []},
                    {'link': 'x_off', 'title': '공식 0분', 'game': 'G', 'source': 'OFFICIAL_WEB', 'timestamp': '2026-09-16T01:00:00', 'content': '', 'detected_keywords': []},
                    {'link': 'x_daum', 'title': '다음 59분', 'game': 'G', 'source': 'DAUM', 'timestamp': '2026-09-16T00:59:59', 'content': '', 'detected_keywords': []},
                ]
                pg.evaluate("window.__rows = %s; window._feedBootHydrated = false;" % _json.dumps(MIX_ROWS))
                pg.evaluate("window.loadFeedPane()")
                pg.wait_for_selector("#feed-body .feed-card[data-href]")
                pg.wait_for_timeout(120)
                mix = pg.evaluate(
                    "[...document.querySelectorAll('#feed-body .feed-card[data-href]')].map(c => c.textContent)")
                titles = [next((r['title'] for r in MIX_ROWS if r['title'] in t), '?') for t in mix]
                ck('분 내림차순이 출처보다 먼저 (DC 1분 > 공식 0분 > 다음 59분)',
                   titles == ['DC 1분 59', 'DC 1분 58', '공식 0분', '다음 59분'], '-> %s' % titles)

                print('  --- ④ 판정 대기 점은 서버 judge_pending 으로만 ---')
                # [v6.7.738] 종전 클라는 "공식이고 verdict_kind 가 null" 이면 점을 찍어 공지·7일 지난 글에도
                #   상시 점이 떴다. 이제 서버가 판정 잡의 후보 술어로 정한 judge_pending 만 본다.
                #   뮤턴트: 옛 _feedJudgePending -> notice 카드에 점이 생겨 FAIL.
                J_ROWS = [
                    {'link': 'j_wait', 'title': '대기 글', 'game': 'G', 'source': 'OFFICIAL_WEB', 'timestamp': '2026-09-16T05:00:00', 'content': '',
                     'detected_keywords': [], 'verdict_kind': None, 'verdict_target': None, 'verdict_at': None, 'judge_pending': True},
                    {'link': 'j_notice', 'title': '공지 글', 'game': 'G', 'source': 'OFFICIAL_WEB', 'timestamp': '2026-09-16T04:00:00', 'content': '',
                     'detected_keywords': [], 'verdict_kind': None, 'verdict_target': None, 'verdict_at': None, 'judge_pending': False},
                    {'link': 'j_clean', 'title': '클린 글', 'game': 'G', 'source': 'OFFICIAL_WEB', 'timestamp': '2026-09-16T03:00:00', 'content': '',
                     'detected_keywords': [], 'verdict_kind': 'clean', 'verdict_target': 'none', 'verdict_at': 'x', 'judge_pending': False},
                    {'link': 'j_ad', 'title': '홍보 글', 'game': 'G', 'source': 'OFFICIAL_WEB', 'timestamp': '2026-09-16T02:00:00', 'content': '',
                     'detected_keywords': [], 'verdict_kind': '홍보', 'verdict_target': 'none', 'verdict_at': 'x', 'judge_pending': False},
                ]
                pg.evaluate("window.__rows = %s; window._feedBootHydrated = false;" % _json.dumps(J_ROWS))
                pg.evaluate("window.loadFeedPane()")
                pg.wait_for_selector("#feed-body .feed-card[data-href]")
                pg.wait_for_timeout(120)
                dots = pg.evaluate(
                    "Object.fromEntries([...document.querySelectorAll('#feed-body .feed-card[data-href]')]"
                    ".map(c => [c.textContent.includes('대기 글') ? 'wait' : c.textContent.includes('공지 글') ? 'notice'"
                    " : c.textContent.includes('클린 글') ? 'clean' : 'ad', [!!c.querySelector('.feed-judge-pending'), !!c.querySelector('.feed-flag-chip')]]))")
                ck('judge_pending=true 카드에만 대기 점', dots.get('wait') == [True, False], '-> %s' % dots)
                ck('공지(verdict 없음, judge_pending=false)에는 점 없음', dots.get('notice') == [False, False], '-> %s' % dots)
                ck('clean 에는 점·칩 없음', dots.get('clean') == [False, False], '-> %s' % dots)
                ck('홍보 판정은 칩(점 아님)', dots.get('ad') == [False, True], '-> %s' % dots)

                ck('런타임 오류 없음', not errs, '-> %s' % (errs[:1] or ''))
            finally:
                # 반드시 닫는다 - 남긴 브라우저가 호스트 load 를 올려 배포 게이트를 막는다.
                b.close()
    finally:
        srv.shutdown()

    print()
    print('RESULT: ' + ('ALL PASS' if not fails else 'FAIL -> ' + ', '.join(fails)))
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
