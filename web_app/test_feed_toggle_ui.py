# -*- coding: utf-8 -*-
"""[v6.7.672] 피드 탭 축 토글을 **실제 브라우저에서** 잰다.

이 두 가지는 단위 테스트로는 못 잡는다 - 둘 다 "시간" 이 있어야 재현된다:
  ① **지난 축의 응답이 화면을 덮는가** - fetch 두 개가 겹치고 늦은 쪽이 나중에 도착해야 난다.
     구 코드는 `await` **뒤에** `_feedQueryKey()` 를 다시 불러 캐시에 찍어서, 옛 축의 응답에
     새 축의 키가 찍혔다. 그러면 `_feedCacheIsStaleForFilters()` 가 "맞는 캐시" 라고 답해
     refetch 도 안 걸린다 - 화면이 **조용히 틀린다**(PATTERN-55 가 games 에서 막은 것과 같은
     결함이 시간 축으로 다시 열린 것).
  ② **토글 중 화면이 무슨 말을 하는가** - v6.7.669 가 "옛 목록을 둔다" 로 바꾸면서 진행 신호가
     하나도 없게 됐다. 계산된 opacity 까지 봐야 CSS 가 진짜 먹었는지 안다.

실행: `python test_feed_toggle_ui.py`  (playwright + chromium 필요)
종료코드: 0 = ALL PASS / 1 = FAIL / 3 = 환경 없음(**PASS 아님** - 조용히 통과시키지 않는다)
"""
import functools
import http.server
import os
import socket
import sys
import threading

_STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')

# 프로브 페이지는 **static/ 에 파일로 두지 않는다** - 테스트 픽스처가 배포물에 섞이지 않게
# 서버가 메모리에서 낸다. 나머지 경로(css/js)는 실제 static/ 을 그대로 읽는다.
PROBE_HTML = """<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="/css/style-feed.css"><style>body{margin:0;background:#0f1117}</style></head><body>
<!-- [v6.7.731] 판정 탭 툴바 - 피드와 **같은** .feed-srcpills/.feed-srcpill/.feed-flagpill 클래스를
     공유한다(verdict.html). 피드 소스바보다 DOM 앞에 둬서 _setFeedBusy 가 '첫 .feed-src-bar' 를
     잡던 구결함이 재현되는 자리에 놓는다. 판정 pill 은 data-vd-* 를 쓰고 data-source 가 없다. -->
<div class="feed-src-bar vd-toolbar" id="verdict-toolbar"><div class="feed-srcpills">
  <button type="button" class="feed-srcpill active" data-vd-view="all">전체</button>
  <button type="button" class="feed-srcpill" data-vd-view="pass">모델 통과</button>
  <button type="button" class="feed-flagpill vd-flag" data-vd-mismatch>불일치만</button>
</div></div>
<div class="pane-window" style="width:480px">
  <div class="feed-src-bar"><div class="feed-srcpills">
    <button type="button" class="feed-srcpill active" data-source="">전체</button>
    <button type="button" class="feed-srcpill" data-source="OFFICIAL_WEB">공식</button>
    <button type="button" class="feed-srcpill" data-source="DC">DC</button>
    <button type="button" class="feed-flagpill" data-flagtoggle="1">제재</button>
  </div></div>
  <span id="feed-count-header"></span>
  <div id="feed-tab" class="tab-content" style="height:360px;padding:0">
    <div id="feed-body" class="feed-body"></div>
    <div id="feed-empty-state" class="empty-state" style="display:none"></div>
  </div>
</div>
<script>
  const API_BASE = '/api';
  let _localSettingsData = { games: [], visibility: {}, limits: {} };
  window.__gate = {};
  function _rows(tag, n) { const o = []; for (let i = 0; i < n; i++) o.push({
      link: tag + '_' + i, title: tag + ' 글 ' + i, game: 'G', source: 'DC', board_name: 'b',
      date: '01:00', timestamp: '2026-09-16T01:00:00', content: '', detected_keywords: [] }); return o; }
  // 응답 시점을 테스트가 손으로 쥔다 - sleep 으로 타이밍을 맞추면 느린 기계에서 흔들린다.
  window.fetch = function (url) {
    const tag = (url.match(/source=([A-Z_]+)/) || [, 'ALL'])[1] + (/flagged=1/.test(url) ? '+FLAG' : '');
    return new Promise(res => { window.__gate[tag] = () => res({ ok: true, json: async () => _rows(tag, window.__n == null ? 3 : window.__n) }); });
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

                print('  --- 느린 토글: 화면이 무슨 말을 하는가 ---')
                pg.evaluate("window.setFeedSource('OFFICIAL_WEB')")
                pg.wait_for_timeout(250)
                ck('토글 직후 진행 표시가 붙는다',
                   pg.eval_on_selector('.pane-window .feed-src-bar', 'e => e.classList.contains("is-busy")'))
                ck('옛 목록을 비우지 않고 흐리게 둔다',
                   pg.eval_on_selector('#feed-body', 'e => e.classList.contains("feed-body--swapping")'))
                # dim 은 지연 0.18s + 전이 0.18s 라 **고정 대기로 재면 흔들린다**(첫 판이 300ms 로
                # 재다가 간헐 FAIL). 값이 내려앉을 때까지 기다리되 타임아웃을 두어, 클래스만 붙고
                # CSS 가 안 먹은 경우는 여전히 FAIL 로 잡는다.
                try:
                    pg.wait_for_function(
                        "() => parseFloat(getComputedStyle(document.getElementById('feed-body')).opacity) < 0.9",
                        timeout=2000)
                    _dim = True
                except Exception:
                    _dim = False
                op = pg.eval_on_selector('#feed-body', 'e => getComputedStyle(e).opacity')
                ck('흐리기가 CSS 까지 실제로 먹었다', _dim, '-> opacity=%s' % op)
                ck('pill 은 기다리지 않고 즉시 활성',
                   pg.eval_on_selector('.feed-srcpill[data-source="OFFICIAL_WEB"]',
                                       'e => e.classList.contains("active")'))
                pg.evaluate("window.__gate['OFFICIAL_WEB']()")
                pg.wait_for_timeout(200)
                ck('응답이 오면 진행 표시가 걷힌다',
                   not pg.eval_on_selector('.pane-window .feed-src-bar', 'e => e.classList.contains("is-busy")'))
                ck('새 축의 목록이 렌더된다',
                   'OFFICIAL_WEB 글' in pg.eval_on_selector('#feed-body', 'e => e.textContent'))

                print('  --- 레이스: 늦은 응답이 화면을 덮는가 ---')
                pg.evaluate("window.setFeedSource('DC')")       # 요청 A (느림)
                pg.wait_for_timeout(50)
                pg.evaluate("window.setFeedFlagged(true)")      # 요청 B (축 변경)
                pg.wait_for_timeout(50)
                pg.evaluate("window.__gate['DC+FLAG']()")       # B 먼저 도착
                pg.wait_for_timeout(150)
                pg.evaluate("window.__gate['DC']()")            # A 가 뒤늦게 도착
                pg.wait_for_timeout(200)
                txt = pg.eval_on_selector('#feed-body', 'e => e.textContent')
                ck('지난 축의 응답이 화면을 덮지 않는다',
                   ('DC+FLAG 글' in txt) and ('DC 글' not in txt),
                   '-> 렌더된 것: %s' % ('DC+FLAG' if 'DC+FLAG 글' in txt else ('DC' if 'DC 글' in txt else '없음')))
                ck('캐시 키가 현재 축과 일치한다',
                   pg.evaluate("window._feedCacheIsStaleForFilters()") is False)
                ck('레이스 후 진행 표시가 남지 않는다',
                   not pg.eval_on_selector('.pane-window .feed-src-bar', 'e => e.classList.contains("is-busy")'))
                print('  --- 서버 축 판별기 + 0건 문구 (v6.7.673) ---')
                # 이 축은 **클라에 되거르는 경로가 없다** - 그래서 0건일 때 "수집이 없다" 가
                #   아니라 "이 필터 때문" 이라고 말해야 하고, boot 하이드레이션도 이 판별기를
                #   봐야 한다(그쪽은 필터 없이 받아오므로).
                pg.evaluate("window.setFeedSource(''); window.setFeedFlagged(false)")
                pg.wait_for_timeout(50)
                try:
                    pg.evaluate("window.__gate[''] && window.__gate['']()")
                except Exception:
                    pass
                pg.evaluate("window.__gate['ALL'] && window.__gate['ALL']()")
                pg.wait_for_timeout(150)
                ck('축이 모두 꺼져 있으면 판별기가 false',
                   pg.evaluate("window._feedServerAxisOn()") is False)

                pg.evaluate("window.__n = 0")            # 이 조합은 서버가 0건을 준다
                pg.evaluate("window.setFeedSource('DC')")
                pg.wait_for_timeout(50)
                ck('축이 켜지면 판별기가 true',
                   pg.evaluate("window._feedServerAxisOn()") is True)
                pg.evaluate("window.__gate['DC']()")
                pg.wait_for_timeout(250)
                _et = pg.eval_on_selector('#feed-empty-state', 'e => e.textContent')
                ck('0건 문구가 켜진 필터를 그대로 말한다',
                   ('출처=DC' in _et) and ('상단' in _et), '-> %r' % _et[:70])
                ck('"크롤러가 새 글을 수집하면" 이라고 거짓말하지 않는다',
                   '크롤러가 새 글을 수집하면' not in _et, '-> %r' % _et[:70])
                pg.evaluate("window.__n = null")

                print('  --- 판정 탭 pill 과의 선택자 충돌 (v6.7.731 defect 1·2) ---')
                # 판정 탭도 .feed-srcpills/.feed-srcpill/.feed-flagpill 를 공유한다. 피드의 위임·동기화·
                #   진행바가 판정 pill 로 새면 안 된다. (verdict.html 은 다른 에이전트 소유 - 피드 쪽만 고쳤다.)
                pg.evaluate("window.setFeedSource('OFFICIAL_WEB')")
                pg.wait_for_timeout(30)
                pg.evaluate("window.__gate['OFFICIAL_WEB'] && window.__gate['OFFICIAL_WEB']()")
                pg.wait_for_timeout(120)
                _src0 = pg.evaluate("localStorage.getItem('imt_feed_source')")
                # ① 판정 '모델 통과' pill 클릭 - 구결함: 이 document capture 위임이 먼저 받아 setFeedSource('') 로 풀었다.
                pg.eval_on_selector("#verdict-toolbar .feed-srcpill[data-vd-view='pass']", "e => e.click()")
                pg.wait_for_timeout(120)
                ck('판정 pill 클릭이 피드 출처(LS)를 안 바꾼다',
                   pg.evaluate("localStorage.getItem('imt_feed_source')") == 'OFFICIAL_WEB',
                   '-> %r -> %r' % (_src0, pg.evaluate("localStorage.getItem('imt_feed_source')")))
                # ② 피드 출처를 '' 로 되돌린다 - 구결함: _syncFeedSourceBarUI 가 data-source 없는 판정
                #    pill 을 ('' === '') 로 전부 active 로 칠했다. 판정 pill 의 active 는 정적 'all' 1개뿐이어야.
                pg.evaluate("window.setFeedSource('')")
                pg.wait_for_timeout(30)
                try: pg.evaluate("window.__gate[''] && window.__gate['']()")
                except Exception: pass
                pg.evaluate("window.__gate['ALL'] && window.__gate['ALL']()")
                pg.wait_for_timeout(120)
                _vd_active = pg.evaluate("[...document.querySelectorAll('#verdict-toolbar .feed-srcpill, #verdict-toolbar .feed-flagpill')].filter(b=>b.classList.contains('active')).length")
                ck('_syncFeedSourceBarUI 가 판정 pill 을 안 칠한다 (정적 all 1개만 active)',
                   _vd_active == 1, '-> active 판정 pill 수=%s' % _vd_active)
                # ③ 느린 토글 중 진행바가 판정 툴바(DOM 상 첫 .feed-src-bar)엔 안 붙는다(구 _setFeedBusy 결함).
                pg.evaluate("window.setFeedSource('DC')")
                pg.wait_for_timeout(120)
                ck('진행바가 판정 툴바엔 안 붙는다',
                   not pg.eval_on_selector('#verdict-toolbar', 'e => e.classList.contains("is-busy")'))
                ck('진행바는 피드 소스바에 붙는다',
                   pg.eval_on_selector('.pane-window .feed-src-bar', 'e => e.classList.contains("is-busy")'))
                pg.evaluate("window.__gate['DC'] && window.__gate['DC']()")
                pg.wait_for_timeout(120)

                print('  --- 스크롤 내린 채 축 토글 = 새 목록이므로 맨 위로 (v6.7.731 defect 3) ---')
                # 질의가 바뀐 응답은 '같은 목록의 갱신' 이 아니다. 앵커 복원으로 옛 스크롤을 붙들거나
                #   윗글을 '새 글' 로 적립하면 안 되고, 새로 그려 맨 위로 보내야 한다.
                pg.evaluate("_localSettingsData = { games: ['G'], visibility: { G: true }, limits: { G: 0 } }")
                pg.evaluate("window.__n = 60")
                pg.evaluate("window.setFeedSource('DC')")
                pg.wait_for_timeout(30)
                pg.evaluate("window.__gate['DC'] && window.__gate['DC']()")
                pg.wait_for_selector("#feed-body .feed-card[data-href]")
                pg.eval_on_selector('#feed-body', 'e => { e.scrollTop = 240; }')
                pg.wait_for_timeout(80)
                _st_scrolled = pg.eval_on_selector('#feed-body', 'e => e.scrollTop')
                ck('토글 전 스크롤이 내려가 있다(측정 전제)', _st_scrolled > 40, '-> scrollTop=%s' % _st_scrolled)
                pg.evaluate("window.setFeedSource('OFFICIAL_WEB')")       # 다른 축 = 새 목록
                pg.wait_for_timeout(30)
                pg.evaluate("window.__gate['OFFICIAL_WEB'] && window.__gate['OFFICIAL_WEB']()")
                pg.wait_for_timeout(250)
                _txt2 = pg.eval_on_selector('#feed-body', 'e => e.textContent')
                ck('축 토글 후 새 목록이 렌더된다', ('OFFICIAL_WEB 글' in _txt2) and ('DC 글' not in _txt2),
                   '-> %s' % ('OFFICIAL_WEB' if 'OFFICIAL_WEB 글' in _txt2 else ('DC' if 'DC 글' in _txt2 else '없음')))
                _st_after = pg.eval_on_selector('#feed-body', 'e => e.scrollTop')
                ck('축이 바뀐 목록은 맨 위로 간다 (옛 스크롤 복원 안 함)', _st_after <= 4, '-> scrollTop=%s' % _st_after)
                _pill_after = pg.eval_on_selector('#feed-newpill', 'e => e.style.display !== "none" ? e.textContent : ""') \
                    if pg.query_selector('#feed-newpill') else ''
                ck('축 토글이 가짜 "새 글 N건" pill 을 띄우지 않는다', '새 글' not in _pill_after, '-> %r' % _pill_after)
                pg.evaluate("window.__n = null")

                ck('런타임 오류 없음', not errs, '-> %s' % (errs[:1] or ''))
            finally:
                # **반드시 닫는다** - 남긴 브라우저가 호스트 load 를 올려 imt-deploy 게이트를 막는다.
                b.close()
    finally:
        srv.shutdown()

    # [v6.7.731] **boot 이 그 판별을 거치는가** - 브라우저 프로브는 boot.js 를 안 태운다(앱 전체 init).
    #   그래서 이 계약만 소스로 잠근다: boot 은 글로벌 프리페치를 **직접** 하이드레이트하지 않고,
    #   games 축까지 보고 캐시키를 찍는 판단을 feed-tab.js 의 _feedAdoptBootPrefetch 에 위임해야 한다.
    #   boot 에 `_feedTotalCache = articles` 직접 대입이 다시 생기면 그 위임을 우회한 것이다
    #   (게임 숨긴 사용자가 글로벌 스트림을 첫 화면으로 보고, 캐시키 null 이라 스스로 못 고친다).
    _boot = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'static', 'js', 'main', 'boot.js'), encoding='utf-8').read()
    ck('boot 하이드레이션이 피드 판별기(_feedAdoptBootPrefetch)를 거친다',
       'window._feedAdoptBootPrefetch(' in _boot and '_feedTotalCache = articles' not in _boot,
       '-> boot 가 판별을 우회하면 게임 숨긴 사용자가 글로벌 스트림을 첫 화면으로 본다')
    _ft = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'js', 'tabs', 'feed-tab.js'), encoding='utf-8').read()
    ck('판별 함수는 feed-tab.js 에만 (사본이 생기면 축이 늘 때 갈라진다)',
       _ft.count('function _feedServerAxisOn(') == 1 and _boot.count('function _feedServerAxisOn(') == 0
       and _ft.count('function _feedAdoptBootPrefetch(') == 1
       and _boot.count('function _feedAdoptBootPrefetch(') == 0)

    print()
    print('RESULT: ' + ('ALL PASS' if not fails else 'FAIL -> ' + ', '.join(fails)))
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
