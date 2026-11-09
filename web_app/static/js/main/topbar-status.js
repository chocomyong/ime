/* ════════════════════════════════════════════════════════════════
   main/topbar-status.js — 최상단 툴 상태등 (admin + manager) [v6.7.278]
   그라파나 CCU(POE2/POE1/배틀그라운드) 상태를 탭을 열지 않아도 상단 바에서 확인. 단일 60s 폴러(탭 자체 폴링과 독립).
   - 그라파나: window.getCcuStatusSnapshot() (ccu-grafana-tab.js, evaluateDrop 재사용)
   [v6.7.741] 워처 상태등(네이버 · POE · 디코)과 워처 공지 배너 삭제 - 워처 종료(사용자 2026-10-24).
     워처 관제 라우트 3종도 함께 지웠다. 그라파나가 은닉(hidden_apps)인 동안엔 상태등도 폴링도 없다.
   가시성은 .topbar-admin-only CSS 가드(body:not(.is-admin))가 담당.
   폴링은 is-admin 일 때만(manager/anon 은 폴링 자체 미발화 — 403 스팸 방지).
   ════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var POLL_MS = 60000;        // 상태등은 글랜스용 — 탭(7s/15s)보다 느슨하게.
  var FIRST_DELAY_MS = 2500;  // 첫 paint 지연 — 로그인/admin 판정 + 초기 LCP cascade 회피.
  var _timer = null;

  // [v6.7.383] 상태등·CCU 배너 접근 = admin + manager. AI 배너만 socket.js 에서 admin 게이트 유지 → 매니저에겐 AI 미노출.
  //   [v6.7.741] 워처 몫이 사라져 옛 이름이 거짓이 돼 _toolAccess 로 바꿨다.
  function _toolAccess() {
    var b = document.body;
    return !!(b && (b.classList.contains('is-admin') || b.classList.contains('is-manager')));
  }

  function setDot(id, state, title) {
    var lamp = document.getElementById(id);
    if (!lamp) return;
    var dot = lamp.querySelector('.topbar-lamp__dot');
    if (dot) dot.setAttribute('data-state', state || 'unknown');
    if (title != null) lamp.setAttribute('title', title);
  }

  function fmtNum(n) {
    return (n == null || isNaN(n)) ? '' : Number(n).toLocaleString();
  }

  var CCU_LAMP_IDS = { 'POE2': 'topbar-lamp-ccu-poe2', 'POE1': 'topbar-lamp-ccu-poe1', '배틀그라운드': 'topbar-lamp-ccu-pubg' };

  /* [v6.7.608] 램프 옆 **현재 동접 + 증감칩**. 종전에는 점 색만 있고 숫자는 `title`(마우스를
     올려야 보인다)에만 있었다 - 상태등의 목적이 "탭을 안 열어도 안다" 인데, 정작 얼마나
     늘고 줄었는지는 열어야 알 수 있었다. AME 가 같은 스냅샷으로 상단 스트립을 먼저 만들었고
     (AME v0.9.15), 그 화면을 이쪽에 맞춰 되가져온다.

     **증감은 스냅샷이 준 값을 그대로 쓴다** - 여기서 다시 계산하면 카드와 갈라진다
     (`ccu-grafana-tab.js` 의 `latestDelta` 가 단일 진원지). 칩 색은 `ccu-grafana.css` 의
     `.ccu-delta-chip--up/down/flat` 재사용 - 그 파일은 `index.html` 이 전역 로드하므로
     그라파나 탭을 안 열어도 색이 먹는다.

     값이 없으면(nodata·조회 실패) **빈 문자열**로 둔다 - `—` 같은 자리표를 넣으면 세 램프가
     늘 넓어져 상단 바를 먹는다. 폭은 있을 때만 쓴다. */
  function setCcuValue(lampId, s) {
    var lamp = document.getElementById(lampId);
    if (!lamp) return;
    var slot = lamp.querySelector('.topbar-lamp__ccu');
    if (!slot) return;
    if (!s || s.current == null) { slot.textContent = ''; return; }
    var html = '<span class="topbar-lamp__ccu-now">' + fmtNum(s.current) + '</span>';
    if (s.delta) {
      var cls = s.delta.d > 0 ? 'up' : (s.delta.d < 0 ? 'down' : 'flat');
      var txt = s.delta.d === 0 ? '±0'
        : ((s.delta.d > 0 ? '▲' : '▼') + fmtNum(Math.abs(s.delta.d)));
      html += '<span class="ccu-delta-chip ccu-delta-chip--' + cls + '">' + txt + '</span>';
    }
    slot.innerHTML = html;
  }

  // [v6.7.335] 게임별 직전 폴링 상태 캐시 — CCU 급감 배너 엣지 트리거용.
  //   red 유지 중(60초마다 red 반복)이면 배너가 계속 떠 스팸이 되므로, red 가 아니었다가 red 로 전이한 순간만 1회 발화.
  var _lastCcuState = {};

  async function pollGrafana() {
    if (typeof window.getCcuStatusSnapshot !== 'function') return;  // ccu-grafana-tab.js 미로드 — 다음 tick 재시도.
    var snap = null;
    try { snap = await window.getCcuStatusSnapshot(); } catch (_) { snap = null; }
    if (!snap) {
      Object.keys(CCU_LAMP_IDS).forEach(function (g) {
        setDot(CCU_LAMP_IDS[g], 'unknown', g + ' 동접 — 상태 조회 실패');
        setCcuValue(CCU_LAMP_IDS[g], null);   // [v6.7.608] 낡은 숫자를 남기지 않는다
      });
      return;
    }
    var byName = {};
    snap.forEach(function (s) { byName[s.name] = s; });
    Object.keys(CCU_LAMP_IDS).forEach(function (g) {
      var s = byName[g];
      if (!s) {
        setDot(CCU_LAMP_IDS[g], 'unknown', g + ' 동접 — 데이터 없음');
        setCcuValue(CCU_LAMP_IDS[g], null);
        return;
      }
      var cur = (s.current != null) ? (' (' + fmtNum(s.current) + '명)') : '';
      var title;
      if (s.state === 'red') {
        // [v6.7.312] 판정 절대 200명 기준 전환 — 감소 인원 우선 표기, %는 보조.
        title = g + ' 동접 급감' + (s.dropAbs != null ? ' -' + fmtNum(s.dropAbs) + '명' : '')
          + (s.dropPct != null ? '(-' + s.dropPct.toFixed(1) + '%)' : '') + cur;
      } else if (s.state === 'stale') {
        title = g + ' 동접 — 수집 지연' + cur;
      } else if (s.state === 'nodata') {
        title = g + ' 동접 — 데이터 없음';
      } else {
        title = g + ' 동접 정상' + cur;
      }
      setDot(CCU_LAMP_IDS[g], s.state, title);
      setCcuValue(CCU_LAMP_IDS[g], s);
    });

    // [v6.7.335] 급감 엣지 배너 — 직전 폴링이 red 가 아니었는데 이번에 red 로 전이한 게임만 1회 발화.
    //   · 엣지 트리거: red 유지 중 반복(레벨)이면 60초마다 스팸 → 전이 순간만.
    //   · 첫 관측(_lastCcuState 미기록)엔 '직전' 이 없어 무발화 — 페이지 로드 시 이미 red 여도 안 뜸(의도).
    //   · [v6.7.383] CCU 배너 admin+manager 개방 (매니저는 그라파나 탭 접근권 보유 → 급감 배너도 정합).
    //     구 v6.7.357 의 window._isAdmin===true 좁힘을 _toolAccess()(admin OR manager)로 완화.
    //     (tick() 폴링도 _toolAccess 로 admin+manager 허용 — 발화 지점 이중 방어 동일.)
    Object.keys(CCU_LAMP_IDS).forEach(function (g) {
      var s = byName[g];
      var newState = s ? s.state : 'unknown';
      var prev = _lastCcuState[g];
      if (newState === 'red' && prev && prev !== 'red' && _toolAccess()
          && typeof window.showInAppBanner === 'function') {
        var dropTxt = (s && s.dropAbs != null) ? ('-' + fmtNum(s.dropAbs) + '명 급감') : '동접 급감';
        var curTxt = (s && s.current != null) ? (' · 현재 ' + fmtNum(s.current) + '명') : '';
        window.showInAppBanner({
          title: g + ' 동접 급감',
          body: dropTxt + curTxt,
          targetApp: 'ccu-grafana',
          icon: 'fa-arrow-trend-down',
          kind: 'ccu',   // [v6.7.345] 알림 보관소 유형 배지 (CCU)
          game: g,                  // [v6.7.346] 보관소 게임명 색 표시 (getGameColor)
          bareTitle: '동접 급감',   // [v6.7.346] 게임명은 배지 행이 대체
          variant: 'alert',
          sound: true,
        });
      }
      _lastCcuState[g] = newState;
    });
  }

  function tick() {
    if (document.hidden) return;   // 백그라운드 탭은 폴링 스킵(절전).
    if (!_toolAccess()) return;   // [v6.7.383] admin+manager 폴링(anon 만 미발화 — 403 스팸 방지).
    // [v6.7.725] 은닉 탭(app.py HIDDEN_APP_IDS)은 상태등을 렌더하지 않으므로 폴링도 하지 않는다(API 를 치지 않는다).
    var hidden = Array.isArray(window.IMT_HIDDEN_APPS) ? window.IMT_HIDDEN_APPS : [];
    if (hidden.indexOf('ccu-grafana') === -1) pollGrafana();
  }

  function start() {
    if (_timer) return;
    setTimeout(tick, FIRST_DELAY_MS);
    _timer = setInterval(tick, POLL_MS);
    document.addEventListener('visibilitychange', function () { if (!document.hidden) tick(); });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
