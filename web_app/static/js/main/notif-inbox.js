/* ════════════════════════════════════════════════════════════════
   main/notif-inbox.js — 상단 알림 보관소 (admin + manager) [v6.7.427]
   서버 공유 원장 방식. 서버(web_lib/bg/notif_store.py)가 배너(AI 요약/CCU 급감/이상감지/수집 정지 등)를
   발생 시점에 직접 적재하므로([v6.7.741] 워처 공지 생산자는 워처 종료로 삭제), 앱이 닫혀 있어(=인앱 배너 미발화) OS 푸시로만 알던 알림도 웹 종에
   남는다. 종 클릭 시 GET /api/notif-inbox 로 최신 목록을 받아 렌더.

   설계 결정(2026-07-22 사용자):
   - 서버 공유: 모든 admin/manager 세션이 동일 상태(개인별 아님 — 신원=역할, 계정 없음).
     구 localStorage per-device 방식 폐기.
   - 읽음/미확인 추적 없음: "어디까지 봤나는 안 중요, 최신 확인 가능 상태만 중요. 놓치는 건 앱이
     판단할 게 아니다." → 미확인 배지/lastSeen/PWA 앱아이콘·문서제목 미러 전부 제거. 알림 역할은
     배너(열림)/OS 푸시(닫힘), 종은 순수 아카이브.
   - 수동 삭제 없음: 공유 원장이라 한 명이 누르면 전체 폭파 — "전체 삭제" 버튼 제거. 서버 300 FIFO 자동.
   - 매니저도 AI 포함 3종 전부 열람(AI 요약 '탭'만 미개방). API가 role 무관 전건 반환.

   모바일 푸시 클릭 → 앱 열림 + 보관소 자동 오픈: sw.js notificationclick 가 url 에 notif=1 부착 →
   boot.js 가 pointer:coarse 에서 window.imtNotifInbox.open() 호출(탭 딥링크 대신 보관소만).
   ════════════════════════════════════════════════════════════════ */
(function () {
    'use strict';

    // [v6.7.345/346] 알림 유형·출처·변경유형 배지 (서버 record 필드와 대응). 피드/알림 탭 배지 체계 재사용.
    var KIND_BADGE = { ai: 'AI 요약', notice: '공지', ccu: 'CCU', anomaly: '이상', crawl: '수집' };   // [v6.7.739] crawl = 공식 게시판 연속 실패(admin 전용 - 서버가 min_role 로 거름)   // [v6.7.438] anomaly = 평시 대비 급증(admin 전용 - 서버가 min_role 로 거름).
    // [v6.7.742] dc(디스코드 욕설·비방 판정, v6.7.463~) 배지 삭제 - 생산자(bg/discord_judge.py)를 지웠다. 원장에 남은 옛 dc 항목은
    //   300 FIFO 로 밀려 나가고, 그동안은 배지 없이 제목·본문만 보인다(클릭 딥링크 ?tab=discord 는 app-box 가 없어 no-op).
    // [v6.7.741] 워처 전용 배지 삭제 - 출처 POE(워처 공지 전용 칩)와 변경유형(evt: 신규/제목 변경/점검 …, 알림 탭 배지 재사용).
    //   생산자(web_push 워처 공지 감시)를 워처 종료로 지웠다. 원장에 남은 옛 항목은 배지 없이 제목·본문만 보인다.
    //   NAVER 는 피드 출처(네이버 카페 수집)라 남긴다 - AI 요약 알림이 쓴다.
    var SRC_LABEL = { NAVER: '네이버', DAUM: '다음', OFFICIAL_WEB: '공식', DC: 'DC' };

    var _items = [];   // 서버 목록 캐시 (열 때 refetch)
    var _sig = '';     // [v6.7.643] (건수|최신 ts) - 안 바뀌었으면 재렌더를 건너뛴다
    var _pollTimer = null;
    var POLL_MS = 60000;   // [v6.7.643] 소켓을 놓쳤을 때의 폴백 주기 (아래 _startPolling 근거)
    // [v6.7.434] per-device 미확인 카운트 — 서버 공유 원장(내용은 admin/manager 공유)이되 '이 기기에서 마지막
    //   열람 시각'(localStorage) 이후 도착분 수를 종 배지로. v6.7.427 에서 폐기한 읽음 추적을 사용자 요청으로
    //   종 한정 재도입(피드/댓글은 여전히 앰비언트·무배지). 순수 클라 계산 — 서버 원장 스키마 무변경.
    var _LAST_SEEN_KEY = 'imt_notif_last_seen';
    function _getLastSeen() { try { return parseInt(localStorage.getItem(_LAST_SEEN_KEY), 10) || 0; } catch (_) { return 0; } }
    function _markSeen() { try { localStorage.setItem(_LAST_SEEN_KEY, String(Date.now())); } catch (_) {} }
    function _unreadCount() {
        var ls = _getLastSeen(), vis = _currentVisibility();
        return _items.reduce(function (n, x) {
            if (!x || !x.ts || x.ts <= ls) return n;   // 마지막 열람 이후 도착분만
            var g = x.game || '';
            if (g && vis[g] === false) return n;        // 개인 필터로 끈 게임은 제외(목록·배너 필터와 정합)
            return n + 1;
        }, 0);
    }
    function _updateBadge() {
        var b = _el('topbar-notif-badge');
        if (!b) return;
        var n = _unreadCount();
        if (n > 0) { b.textContent = n > 99 ? '99+' : String(n); b.style.display = ''; }
        else { b.textContent = ''; b.style.display = 'none'; }
    }

    // [v6.7.435] L2 뷰 상태 — 'unseen'(미확인, 기본) | 'all'(전체 원장). 종 상단 탭으로 전환.
    var _view = 'unseen';
    function _updateTabs() {
        var tu = _el('topbar-notif-tab-unseen'), ta = _el('topbar-notif-tab-all');
        var n = _unreadCount();
        if (tu) { tu.textContent = '미확인' + (n ? ' ' + (n > 99 ? '99+' : n) : ''); tu.classList.toggle('is-active', _view === 'unseen'); }
        if (ta) ta.classList.toggle('is-active', _view === 'all');
    }

    function _el(id) { return document.getElementById(id); }
    function _isOpen() {
        var p = _el('topbar-notif-panel');
        return !!(p && p.classList.contains('open'));
    }

    function _fmtTs(ms) {
        var d = new Date(ms), now = new Date();
        var hm = String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
        return (d.toDateString() === now.toDateString()) ? hm : ((d.getMonth() + 1) + '.' + d.getDate() + ' ' + hm);
    }

    function _render() {
        var list = _el('topbar-notif-list');
        if (!list) return;
        list.textContent = '';
        // [v6.7.429] 배너·푸시와 동일한 개인 visibility 필터(fail-open) - 이 기기의 상단 필터 결과를 표시 시점에 적용.
        //   서버 공유 원장은 전건 보유(_items 원본 불변), 렌더할 때만 걸러 개인화. 매번 _currentVisibility() 를 읽어
        //   캐시하지 않으므로 상단 필터를 바꾼 뒤 종을 다시 열면 최신 필터로 반영된다(_setOpen→_render 흐름).
        //   game 이 있고 visibility[game]===false(사용자가 명시적으로 끈 게임)만 제외 - game 빈 항목(워처 공지)은 통과.
        var vis = _currentVisibility();
        var ls = _getLastSeen();
        var items = _items.filter(function (x) {
            var g = (x && x.game) || '';
            if (g && vis[g] === false) return false;
            // [v6.7.435] 미확인 탭 = 이 기기 마지막 열람(lastSeen) 이후 도착분만. 전체 탭 = 원장 전건.
            if (_view === 'unseen' && !(x && x.ts && x.ts > ls)) return false;
            return true;
        });
        _updateTabs();
        if (!items.length) {
            var empty = document.createElement('div');
            empty.className = 'topbar-notif__empty';
            empty.textContent = (_view === 'unseen') ? '새 알림 없음 (모두 확인함)' : '알림이 없습니다';
            list.appendChild(empty);
            return;
        }
        items.forEach(function (x) {
            // 전 텍스트 textContent 주입(HTML 미해석) — 배너와 동일 방어 원칙.
            var row = document.createElement('div');
            row.className = 'topbar-notif__item';
            row.setAttribute('role', 'menuitem');
            // 배지 행: 유형(kind) + 출처(src) + 게임, 시각 우측.
            var chips = document.createElement('div');
            chips.className = 'topbar-notif__chips';
            if (x.kind && KIND_BADGE[x.kind]) {
                var kindEl = document.createElement('span');
                kindEl.className = 'topbar-notif__kind topbar-notif__kind--' + x.kind;
                kindEl.textContent = KIND_BADGE[x.kind];
                chips.appendChild(kindEl);
            }
            if (x.src && SRC_LABEL[x.src]) {
                var srcEl = document.createElement('span');
                // 네이버/다음/공식/DC = 피드 공용 출처 배지 색 상속.
                srcEl.className = 'feed-card-source feed-card-source--' + x.src.toLowerCase();
                srcEl.textContent = SRC_LABEL[x.src];
                chips.appendChild(srcEl);
            }
            if (x.game) {
                var gameEl = document.createElement('span');
                gameEl.className = 'topbar-notif__game';
                gameEl.textContent = x.game;
                try {
                    if (typeof window.getGameColor === 'function') gameEl.style.color = window.getGameColor(x.game);
                } catch (_) {}
                chips.appendChild(gameEl);
            }
            var timeEl = document.createElement('span');
            timeEl.className = 'topbar-notif__item-time';
            timeEl.textContent = x.ts ? _fmtTs(x.ts) : '';
            chips.appendChild(timeEl);
            row.appendChild(chips);
            if (x.title) {
                var titleLine = document.createElement('div');
                titleLine.className = 'topbar-notif__item-title';
                titleLine.textContent = x.title;
                row.appendChild(titleLine);
            }
            if (x.body) {
                var bodyLine = document.createElement('div');
                bodyLine.className = 'topbar-notif__item-body';
                bodyLine.textContent = x.body;
                row.appendChild(bodyLine);
            }
            // 행 클릭 → AI 는 원문 글로, 그 외(CCU/워처)는 내부 딥링크 탭(url 의 ?tab=).
            //   [v6.7.431] AI 요약 알림은 원문(x.link)으로 이동 — AI 요약 '탭'은 admin 전용이라 매니저가
            //   글을 못 봤다(v6.7.386 배너는 원문으로 갔으나 종 경로는 회귀 상태였음). 배너와 통일:
            //   kind==='ai' && link 이면 openArticle(원문 새탭). link 없는 옛 record 는 아래 딥링크 폴백(하위호환).
            //   그 외(CCU 등)는 계속 탭 딥링크 — 권한 없거나 탭이 없어 app-box 부재면 no-op(요약은 이미 표시).
            //   [v6.7.741] 옛 워처 항목의 ?tab=naver-watcher / 디코 판정의 ?tab=discord 도 app-box 가 없어 no-op 이다.
            row.addEventListener('click', function () {
                if (x.kind === 'ai' && x.link) {
                    try {
                        if (typeof openArticle === 'function') openArticle(x.link);
                        else window.open(x.link, '_blank', 'noopener');
                    } catch (_) {}
                    _setOpen(false);
                    return;
                }
                var m = (x.url || '').match(/[?&]tab=([a-z0-9-]+)/);
                if (m && document.getElementById('app-box-' + m[1]) && typeof ensureAppPane === 'function') {
                    try { ensureAppPane(m[1]); } catch (_) {}
                    _setOpen(false);
                }
            });
            list.appendChild(row);
        });
    }

    function _fetch() {
        return fetch('/api/notif-inbox?limit=200', { credentials: 'same-origin' })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (d) {
                if (d && Array.isArray(d.items)) {
                    /* [v6.7.643] **바뀐 게 없으면 다시 안 그린다.** 아래 폴백 폴링이 생기면서
                       이 함수가 조용한 시간대에도 1분마다 돈다 - 그때마다 300행을 재렌더하면
                       PATTERN-38(푸시 빈도 x 렌더 비용 = 주기적 freeze)을 폴링으로 다시 들여온다.
                       서버 원장은 append + 상한 300 FIFO 라 (건수, 최신 ts) 두 값이면 변화가 구별된다. */
                    var sig = d.items.length + '|' + ((d.items[0] && d.items[0].ts) || 0);
                    var changed = (sig !== _sig);
                    _sig = sig;
                    _items = d.items;
                    if (changed) {
                        if (_isOpen()) _render();   // [v6.7.435] 열람 표시는 '모두 확인' 버튼에서만(자동 확인 폐지) - 미확인 리스트가 열 때마다 보이도록
                        _updateBadge();
                    }
                }
            })
            .catch(function () {});
    }

    /* ─── [v6.7.643] 폴백 폴링 - **보관소가 소켓에만 매달려 있었다** ───
       종전 갱신 경로는 셋뿐이었다: 페이지 로드 · 종을 여는 순간 · `socket.js showInAppBanner`.
       셋째가 유일한 자동 경로인데 그건 **소켓 이벤트가 도착해야** 돈다 - 소켓이 끊기거나
       (백그라운드 탭 스로틀·네트워크 블립) 이벤트를 놓치면 종은 **다음에 사람이 열 때까지
       고착**된다. 폴링 탭에는 자가회복이 있는데 푸시 전용 표면에는 없는 비대칭이다(PATTERN-40).

       실사고(2026-09-06 사용자 관측): 02:23 게시 글의 요약을 서버가 02:24:38 에 원장에 적재하고
       카카오도 02:24 에 정상 도착했는데, **종에는 약 2시간 뒤에 나타났다.** 서버·카카오·DB 를
       전부 재봤을 때 어느 구간도 2분을 안 넘었으므로 남는 자리가 여기였다.

       60초인 이유: 서버 원장이 JSON 파일 하나라 조회가 싸고, 이 표면의 목적이 "자리를 비운 사이
       도착분"이라 분 단위면 충분하다. 위 변화 감지가 있어 렌더는 실제로 바뀔 때만 돈다.
       소켓 훅은 **더 빠른 경로로 그대로 남긴다**(둘 다 `_fetch` 로 수렴). */
    function _startPolling() {
        if (_pollTimer) return;
        _pollTimer = setInterval(function () {
            /* 숨은 탭에서는 건너뛴다 - 브라우저가 어차피 타이머를 조인다. 대신 아래
               visibilitychange 가 **돌아오는 즉시** 한 번 당겨서 공백을 메운다. */
            if (document.visibilityState === 'hidden') return;
            _fetch();
        }, POLL_MS);
        document.addEventListener('visibilitychange', function () {
            if (document.visibilityState === 'visible') _fetch();
        });
    }

    function _setOpen(on) {
        var p = _el('topbar-notif-panel');
        var btn = _el('topbar-notif-btn');
        if (!p) return;
        p.classList.toggle('open', on);
        p.setAttribute('aria-hidden', on ? 'false' : 'true');
        if (btn) btn.setAttribute('aria-expanded', on ? 'true' : 'false');
        if (on) {
            _view = 'unseen';    // [v6.7.435] 열 때마다 미확인 탭부터 (못 본 것 먼저). 확인은 '모두 확인' 버튼으로만 명시.
            _render();           // 캐시 즉시 렌더(빈 상태 포함)
            _fetch();            // 최신 refetch — 도착 시 재렌더 + 배지 갱신
            _refreshPushBtn();   // [v6.7.360] 타 경로(브라우저 설정 등) 구독 변경 반영
            _updateBadge();
        }
    }

    /* ─── [v6.7.360] PWA Web Push 토글 - 이 기기로 폰 푸시 받기 ───
       배너 3종(AI 요약/CCU 급감/워처 공지)의 폰 미러. 기기별 명시 옵트인 - 공유 PC 는
       구독하지 않는 것이 전제 (v6.7.335 OS 알림 폐기 결정과의 경계, bg/web_push.py docstring).
       iOS 는 16.4+ 에서 홈 화면에 설치한 PWA 여야 PushManager 가 노출된다. */
    function _pushSupported() {
        return ('serviceWorker' in navigator) && ('PushManager' in window) && ('Notification' in window);
    }
    function _urlB64ToUint8(s) {
        var pad = '='.repeat((4 - (s.length % 4)) % 4);
        var raw = atob((s + pad).replace(/-/g, '+').replace(/_/g, '/'));
        var arr = new Uint8Array(raw.length);
        for (var i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
        return arr;
    }
    function _setPushBtn(state) {   // 'on' | 'off' | 'busy'
        var b = _el('topbar-notif-push');
        if (!b) return;
        b.disabled = (state === 'busy');
        b.setAttribute('aria-pressed', state === 'on' ? 'true' : 'false');
        b.classList.toggle('on', state === 'on');
        // [v6.7.365] 라벨을 상태어(켜짐/꺼짐)로 — 'ON/OFF' 는 "현재 상태"인지 "누르면 켜라"인지
        //   모호하다는 사용자 피드백. 켜짐=현재 구독중(초록), 꺼짐=미구독. title 도 상태 반영.
        b.textContent = (state === 'on') ? '푸시 켜짐' : ((state === 'busy') ? '푸시 …' : '푸시 꺼짐');
        b.title = (state === 'on')
            ? '이 기기로 푸시 알림 받는 중 — 누르면 끔'
            : '이 기기로 푸시 알림 받기 (누르면 켬)';
    }
    function _refreshPushBtn() {
        if (!_pushSupported()) { _setPushBtn('off'); return; }
        navigator.serviceWorker.ready
            .then(function (reg) { return reg.pushManager.getSubscription(); })
            .then(function (sub) { _setPushBtn(sub ? 'on' : 'off'); })
            .catch(function () { _setPushBtn('off'); });
    }
    function _uaLabel() {
        return (navigator.userAgent.match(/(iPhone|iPad|Android|Windows|Macintosh)/) || [''])[0] || 'unknown';
    }
    // [v6.7.377] 이 기기의 현재 게임 필터(상단 필터링 결과) - 구독/필터변경 시 서버로 동기화해 푸시 게임 제어.
    function _currentVisibility() {
        try {
            if (typeof _localSettingsData !== 'undefined' && _localSettingsData && _localSettingsData.visibility) {
                return _localSettingsData.visibility;
            }
            var raw = JSON.parse(localStorage.getItem('imt_display_settings') || '{}');
            return (raw && raw.visibility) || {};
        } catch (_) { return {}; }
    }
    // 서버 등록 (add_sub 는 endpoint 로 dedupe — 멱등). silent 면 실패 시 alert 억제(자동 경로용).
    async function _postSubscription(sub, silent) {
        var res = await fetch('/api/push/subscribe', {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ subscription: sub.toJSON(), label: _uaLabel(), visibility: _currentVisibility() }),
        });
        if (!res.ok) throw new Error('subscribe HTTP ' + res.status);
    }
    // 구독 실행. silent=true 면 프롬프트/alert 없이 조용히(권한 이미 granted 인 자동 경로). Returns bool.
    async function _pushSubscribe(silent) {
        try {
            var reg = await navigator.serviceWorker.ready;
            var perm = await Notification.requestPermission();  // 이미 granted 면 프롬프트 없이 즉시 반환
            if (perm !== 'granted') {
                if (!silent) alert('알림 권한이 거부되어 푸시를 켤 수 없습니다. 브라우저(앱) 설정에서 알림을 허용한 뒤 다시 시도하세요.');
                _setPushBtn('off');
                return false;
            }
            var keyRes = await fetch('/api/push/vapid-key', { credentials: 'same-origin' });
            if (!keyRes.ok) throw new Error('vapid-key HTTP ' + keyRes.status);
            var key = (await keyRes.json()).key;
            var sub = await reg.pushManager.subscribe({
                userVisibleOnly: true,
                applicationServerKey: _urlB64ToUint8(key),
            });
            try {
                await _postSubscription(sub, silent);
            } catch (e) {
                try { await sub.unsubscribe(); } catch (_) {}   // 서버 미등록 구독은 로컬도 원복
                throw e;
            }
            _setPushBtn('on');
            return true;
        } catch (e) {
            _setPushBtn('off');
            if (!silent) alert('푸시 설정 실패: ' + (e && e.message ? e.message : e));
            return false;
        }
    }
    async function _togglePush() {
        if (!_pushSupported()) {
            alert('이 브라우저는 푸시를 지원하지 않습니다.\niOS 는 Safari 공유 → "홈 화면에 추가" 로 설치한 IME 앱에서만 구독할 수 있습니다 (iOS 16.4+).');
            return;
        }
        _setPushBtn('busy');
        var reg = await navigator.serviceWorker.ready;
        var sub = await reg.pushManager.getSubscription();
        if (sub) {
            // 해지: 서버 목록 제거 시도 후 로컬 unsubscribe (서버 실패해도 로컬 해지는 진행 -
            // 잔존 서버 항목은 다음 발송의 404/410 자동 정리가 회수)
            try {
                await fetch('/api/push/unsubscribe', {
                    method: 'POST', credentials: 'same-origin',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ endpoint: sub.endpoint }),
                });
            } catch (_) {}
            try { await sub.unsubscribe(); } catch (_) {}
            _setPushBtn('off');
            return;
        }
        await _pushSubscribe(false);
    }
    // [v6.7.363] 자동 ON (사용자 요청 "기본값 on") — 권한이 이미 허용된 기기는 로드 시 조용히 구독/재등록해
    //   매번 버튼 누를 필요 없앰. 최초 1회 권한 허용은 브라우저 정책상 사용자 제스처 필수라 그때만 버튼 탭
    //   (자동 프롬프트는 anti-pattern·영구거부 위험이라 안 함). 미허용/거부 상태는 자동 안 함(거부 존중).
    async function _autoSubscribeIfGranted() {
        if (!_pushSupported()) { _setPushBtn('off'); return; }
        if (Notification.permission !== 'granted') { _setPushBtn('off'); return; }
        try {
            var reg = await navigator.serviceWorker.ready;
            var sub = await reg.pushManager.getSubscription();
            if (!sub) { await _pushSubscribe(true); return; }   // granted 라 프롬프트 없이 조용히
            _setPushBtn('on');
            try { await _postSubscription(sub, true); } catch (_) {}   // 서버 목록 유실 대비 멱등 재등록(자가치유)
        } catch (_) { _setPushBtn('off'); }
    }
    // [v6.7.377] 상단 필터링 변경 시 호출(settings-modal 적용부) - 이 기기 구독의 게임 필터를 서버에 갱신.
    //   구독 없으면 조용히 스킵. "상단 필터에서 푸시 게임까지 제어" 정합성.
    async function syncPushFilter() {
        if (!_pushSupported()) return;
        try {
            var reg = await navigator.serviceWorker.ready;
            var sub = await reg.pushManager.getSubscription();
            if (!sub) return;
            await fetch('/api/push/visibility', {
                method: 'POST', credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ endpoint: sub.endpoint, visibility: _currentVisibility() }),
            });
        } catch (_) {}
    }

    function _init() {
        var btn = _el('topbar-notif-btn');
        var pushBtn = _el('topbar-notif-push');
        if (btn) {
            btn.addEventListener('click', function (e) {
                e.stopPropagation();
                _setOpen(!_isOpen());
            });
        }
        if (pushBtn) {
            pushBtn.addEventListener('click', function (e) { e.stopPropagation(); _togglePush(); });
            _autoSubscribeIfGranted();   // [v6.7.363] 권한 허용된 기기는 로드 시 자동 ON (기본값 on)
        }
        // [v6.7.435] 미확인/전체 탭 전환 + '모두 확인'(이 기기 lastSeen 리셋 — 서버 원장·타 기기 무영향).
        var tabU = _el('topbar-notif-tab-unseen'), tabA = _el('topbar-notif-tab-all');
        if (tabU) tabU.addEventListener('click', function (e) { e.stopPropagation(); _view = 'unseen'; _render(); });
        if (tabA) tabA.addEventListener('click', function (e) { e.stopPropagation(); _view = 'all'; _render(); });
        var markBtn = _el('topbar-notif-markread');
        if (markBtn) markBtn.addEventListener('click', function (e) {
            e.stopPropagation();
            _markSeen();           // 이 기기 '여기까지 봄' = now. L1 서버 원장·타 기기 무영향.
            _render(); _updateBadge();
        });
        // 바깥 클릭 / Escape 로 닫기 (테마 피커 관례)
        document.addEventListener('click', function (e) {
            if (!_isOpen()) return;
            if (e.target && e.target.closest && e.target.closest('#topbarNotif')) return;
            _setOpen(false);
        });
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && _isOpen()) _setOpen(false);
        });
        // [v6.7.427] 옛 미확인 배지 미러(PWA 앱아이콘) 잔재 정리 — 읽음 추적 폐기로 앱 배지는 이제 안 씀.
        try { if (navigator.clearAppBadge) navigator.clearAppBadge().catch(function () {}); } catch (_) {}
        // [v6.7.434] 로드 시 1회 미확인 카운트 populate — 자리비움 중 도착분을 종을 열지 않아도 배지로 표시(admin/manager 만 종 존재).
        if (btn) _fetch();
        if (btn) _startPolling();   // [v6.7.643] 종이 있는 역할(admin/manager)만 폴링한다
    }

    // open = 모바일 푸시 진입 시 boot.js 가 호출(보관소 자동 오픈). refresh = 열려 있을 때 최신 반영(socket.js 배너 훅).
    window.imtNotifInbox = { open: function () { _setOpen(true); }, refresh: _fetch, syncPushFilter: syncPushFilter };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', _init);
    } else {
        _init();
    }
})();
