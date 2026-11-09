// IME Service Worker — App Shell Cache Only
// [v6.6.227] CACHE_VERSION과 SHELL_ASSETS의 ?v= 플레이스홀더는 Flask /sw.js 라우트가
// BUILD_VERSION(부팅 시점 timestamp)으로 치환한다. 컨테이너 재시작 = 강제 캐시 무효화.
// [v6.6.400] Phase 3 4차(v6.6.377-380) 에서 분할된 옛 단일 파일 4개 참조 제거. 신규 sub-file 들 등록.
const CACHE_VERSION = 'imt-__BUILD_VERSION__';
const SHELL_ASSETS = [
    '/',
    // [v6.7.392] style.css 8분할 — index.html link 순서와 동일 (facade style.css 는 미링크라 셸 제외)
    '/static/css/style-core.css?v=__BUILD_VERSION__',
    '/static/css/style-components.css?v=__BUILD_VERSION__',
    '/static/css/style-tables.css?v=__BUILD_VERSION__',
    '/static/css/style-results.css?v=__BUILD_VERSION__',
    '/static/css/style-modals.css?v=__BUILD_VERSION__',
    '/static/css/style-responsive.css?v=__BUILD_VERSION__',
    '/static/css/style-misc.css?v=__BUILD_VERSION__',
    '/static/css/style-feed.css?v=__BUILD_VERSION__',
    // [v6.7.394] situation.css 셸 제외 — admin 전용 링크(v6.7.309 은닉)인데 셸에 있어 익명도 74KB 다운로드
    //   + sw.js 소스에 파일명 힌트 노출. admin 은 ?v= immutable 브라우저 캐시로 충분(오프라인 대시보드는 데이터도 없어 무의미).
    '/static/css/theme-phosphor.css?v=__BUILD_VERSION__',
    '/static/css/theme-variants.css?v=__BUILD_VERSION__',
    '/static/css/components.css?v=__BUILD_VERSION__',
    '/static/css/landing.css?v=__BUILD_VERSION__',
    '/static/js/game-color.js?v=__BUILD_VERSION__',
    '/static/js/core/highlight.js?v=__BUILD_VERSION__',
    '/static/js/core/api-client.js?v=__BUILD_VERSION__',
    '/static/js/components/status-badge.js?v=__BUILD_VERSION__',
    // ui/ 3 sub-file (v6.6.380 — 기존 ui.js 552 LOC 분할)
    '/static/js/ui/common.js?v=__BUILD_VERSION__',
    '/static/js/ui/settings-modal.js?v=__BUILD_VERSION__',
    '/static/js/ui/controls.js?v=__BUILD_VERSION__',
    // tabs
    '/static/js/tabs/_tab_helpers.js?v=__BUILD_VERSION__',
    '/static/js/tabs/total-tab.js?v=__BUILD_VERSION__',
    '/static/js/tabs/comments-tab.js?v=__BUILD_VERSION__',
    '/static/js/tabs/games-tab.js?v=__BUILD_VERSION__',
    '/static/js/tabs/community-tab.js?v=__BUILD_VERSION__',
    // [v6.7.394] _ADMIN_ONLY_JS 6종(ai/diamond/account/trend/log/situation-tab) 셸 제외 — 서버 403 게이트(app.py)라
    //   비관리자 SW 설치마다 403 헛요청 + 파일명 힌트. admin 은 브라우저 캐시로 커버.
    //   [v6.7.711] situation-tab 은 이제 admin·manager(_ADMIN_MANAGER_JS) - 익명 403 은 그대로라 셸 제외 유지가 맞다.
    //   주의: tabs/admin/ 7종은 이름과 달리 전 사용자 공용(ui-control.js = role 게이트 로직 자체)이라 셸 잔류가 정답.
    // tabs/admin/ 7 sub-file (v6.6.377 — 기존 admin-tab.js 711 LOC 분할)
    '/static/js/tabs/admin/ui-control.js?v=__BUILD_VERSION__',
    '/static/js/tabs/admin/notifications.js?v=__BUILD_VERSION__',
    '/static/js/tabs/admin/visit-logs.js?v=__BUILD_VERSION__',
    '/static/js/tabs/admin/db-console.js?v=__BUILD_VERSION__',
    '/static/js/tabs/admin/history-data.js?v=__BUILD_VERSION__',   // [v6.7.741] updates-history.js 삭제 - loadHistoryData 만 이사
    '/static/js/tabs/admin/system.js?v=__BUILD_VERSION__',
    '/static/js/tabs/admin/dashboard.js?v=__BUILD_VERSION__',
    // socket
    '/static/js/socket.js?v=__BUILD_VERSION__',
    // tab-manager/ 3 sub-file (v6.6.378)
    '/static/js/tab-manager/state.js?v=__BUILD_VERSION__',
    '/static/js/tab-manager/actions.js?v=__BUILD_VERSION__',
    '/static/js/tab-manager/render.js?v=__BUILD_VERSION__',
    // main/ 5 sub-file (v6.6.379)
    '/static/js/main/fetch-patch.js?v=__BUILD_VERSION__',
    '/static/js/main/theme.js?v=__BUILD_VERSION__',
    '/static/js/main/app-state.js?v=__BUILD_VERSION__',
    '/static/js/main/pwa.js?v=__BUILD_VERSION__',
    '/static/js/main/boot.js?v=__BUILD_VERSION__',
    '/static/js/main/shortcuts.js?v=__BUILD_VERSION__',
    '/static/js/landing.js?v=__BUILD_VERSION__',
    '/static/icons/icon.svg',
    '/static/offline.html'
];

// 네트워크 전용 패턴 (절대 캐시 금지)
const NETWORK_ONLY = ['/api/', '/socket.io/', '/sw.js'];

self.addEventListener('install', (event) => {
    // [v6.6.400] addAll 은 단 1개 실패 시 전체 트랜잭션 롤백 → 부분 캐싱 위해 개별 add 로 전환.
    // 한 자산이 404/네트워크 에러여도 나머지는 정상 캐시.
    // [v6.7.5] 37개 병렬 fetch → 5개씩 chunk 순차 - 한 클라이언트가 초당 요청을 몰아 보내면 네트워크 보안 장비가 이상 트래픽으로 오탐해 막을 수 있어 나눠 받는다.
    //   `cache: 'reload'` → 'default' — `?v={{build_version}}` 가 이미 cache-bust 보장하므로 브라우저 HTTP 캐시 활용 OK.
    //   UX 동일 — install 은 백그라운드, 사용자 체감 0. 1~2초 추가는 무관.
    // [v6.7.82] CHUNK 5 → 3 + 1회 retry — gunicorn `-w 1 --threads 4` (v6.7.79) 환경에서 5 parallel 의
    //   4~5번째가 queue 대기 → server busy moment (Gemma4/크롤러 사이클) 에 timeout/drop → 일관 fail.
    //   실제 사례: chunk 3 의 games-tab.js (idx 18) / community-tab.js (idx 19) 가 매 deploy 마다 cache.add 실패.
    //   CHUNK=3 → 4-thread 예산 내 모두 즉시 처리. 13 chunks 순차 = 총 install 시간 1~2초 (체감 0).
    //   1회 retry (300ms backoff) — 서버 일시 부하 변동을 흡수.
    const CHUNK = 3;
    async function _cacheAddWithRetry(cache, url) {
        try {
            await cache.add(new Request(url, { cache: 'default' }));
        } catch (err1) {
            // 일시 부하 변동 흡수 — 300ms 후 1회 재시도. 그래도 실패면 warning 로그만.
            await new Promise(r => setTimeout(r, 300));
            try {
                await cache.add(new Request(url, { cache: 'default' }));
            } catch (err2) {
                console.warn('[SW] cache.add 실패:', url, err2 && err2.message);
            }
        }
    }
    async function _chunkedInstall() {
        const cache = await caches.open(CACHE_VERSION);
        for (let i = 0; i < SHELL_ASSETS.length; i += CHUNK) {
            const slice = SHELL_ASSETS.slice(i, i + CHUNK);
            await Promise.allSettled(slice.map(url => _cacheAddWithRetry(cache, url)));
        }
    }
    event.waitUntil(_chunkedInstall());
    // [v6.6.279] 자동 skipWaiting 제거 — 대신 클라이언트가 "지금 업데이트" 버튼 눌러야 적용.
});

// [v6.6.279] 클라이언트로부터 SKIP_WAITING 메시지 수신 → 즉시 새 SW 활성화
self.addEventListener('message', (event) => {
    if (event.data && event.data.type === 'SKIP_WAITING') {
        self.skipWaiting();
    }
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys()
            .then(keys => Promise.all(
                keys.filter(k => k !== CACHE_VERSION).map(k => caches.delete(k))
            ))
            .then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', (event) => {
    const url = new URL(event.request.url);

    // 네트워크 전용 — API, 소켓, SW 자신
    if (NETWORK_ONLY.some(p => url.pathname.startsWith(p))) {
        return; // 브라우저 기본 처리에 위임
    }

    // GET 요청만 캐시 처리
    if (event.request.method !== 'GET') return;

    // [v6.6.400] 모든 fetch 응답이 반드시 Response 객체여야 함.
    // 이전엔 caches.match 가 undefined 반환 시 SW 가 undefined 를 리턴 →
    // "Failed to convert value to 'Response'" 오류. 최종 fallback Response 추가.
    const _fallbackError = (msg) => new Response(
        '<!doctype html><meta charset="utf-8"><title>네트워크 오류</title>' +
        '<body style="font-family:system-ui;padding:2rem;background:#0b0b0f;color:#fafafa;">' +
        '<h2>일시적인 네트워크 오류</h2><p>' + (msg || '연결을 다시 시도하세요.') + '</p>' +
        '</body>',
        { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } }
    );

    // [v6.7.81] MIME-aware fallback — 자산 타입별로 적절한 MIME 의 빈/오류 응답 반환.
    // 이전엔 모든 실패 (JS/CSS/이미지/HTML) 에 HTML fallback 반환 → JS script 태그가 HTML body 받아
    //   "Refused to execute script: MIME type ('text/html') is not executable" + 함수 정의 실패.
    //   (실제 사례: v6.7.74~80 deploy 윈도우 502 storm 동안 actions.js 가 HTML fallback 받아 toggleAppPane undefined.)
    // 본 fix 는 script 요청엔 빈 JS, style 엔 빈 CSS, 이미지엔 1x1 SVG 반환 — MIME 오류 자체 차단 +
    //   console.warn 로깅으로 진단 가시성 확보. 함수 미정의 자체는 서버 회복까지 해결 불가 (해당 reload 필요).
    const _mimeFallback = (request, msg) => {
        const dest = (request && request.destination) || '';
        const safeUrl = JSON.stringify(request && request.url || '');
        if (dest === 'script') {
            return new Response(
                'console.warn("[SW] script load failed:", ' + safeUrl + ');',
                { status: 503, headers: { 'Content-Type': 'application/javascript; charset=utf-8' } }
            );
        }
        if (dest === 'style') {
            return new Response(
                '/* [SW] stylesheet load failed: ' + (request && request.url || '') + ' */',
                { status: 503, headers: { 'Content-Type': 'text/css; charset=utf-8' } }
            );
        }
        if (dest === 'image') {
            return new Response(
                '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>',
                { status: 503, headers: { 'Content-Type': 'image/svg+xml' } }
            );
        }
        return _fallbackError(msg);
    };

    // 네비게이션 요청(페이지 로드)에서 네트워크 실패 시 오프라인 페이지 → 그것도 없으면 합성 fallback
    // [v6.7.91] 5xx transient (PATTERN-29 워커 SIGKILL 윈도우 / 호스트 프록시 일시 unavailable) 시 1회 retry.
    //   사용자 보고: `/?login=KEY` 가 503 으로 떨어져 관리자 진입 실패. 워커 재시작은 <2s 라 800ms 백오프면 충분.
    if (event.request.mode === 'navigate') {
        const _navigateWithRetry = async () => {
            try {
                const resp = await fetch(event.request);
                if (resp && resp.status >= 500 && resp.status < 600) {
                    // transient 5xx — 800ms 후 1회 재시도. 그래도 5xx 면 그대로 반환.
                    await new Promise(r => setTimeout(r, 800));
                    try {
                        const resp2 = await fetch(event.request);
                        return resp2 || resp;
                    } catch (_) {
                        return resp;
                    }
                }
                return resp;
            } catch (_) {
                // 진짜 네트워크 끊김 — 오프라인 페이지 or 합성 fallback.
                const cached = await caches.match('/static/offline.html');
                return cached || _fallbackError('오프라인 상태입니다.');
            }
        };
        event.respondWith(_navigateWithRetry());
        return;
    }

    // 정적 자산: 캐시 우선, 없으면 네트워크
    event.respondWith(
        caches.match(event.request).then(cached => {
            if (cached) return cached;
            return fetch(event.request)
                .then(response => {
                    if (response && response.status === 200) {
                        const clone = response.clone();
                        caches.open(CACHE_VERSION).then(c => c.put(event.request, clone));
                        return response;
                    }
                    // [v6.7.81] 5xx 서버 오류는 script/style 등 자산에 그대로 전달하지 않고 MIME-aware fallback 반환.
                    //   브라우저가 502/500 응답을 script 태그에 전달받으면 "Failed to load resource" + 함수 미정의.
                    //   MIME-aware 503 빈 응답은 적어도 MIME 오류 차단 + 명확한 console 로그 + 다음 reload 시 재시도 정상.
                    if (response && response.status >= 500) {
                        const dest = (event.request && event.request.destination) || '';
                        if (dest === 'script' || dest === 'style' || dest === 'image') {
                            return _mimeFallback(event.request, '서버 ' + response.status + ': ' + url.pathname);
                        }
                    }
                    return response;
                })
                .catch(() => _mimeFallback(event.request, '자산 로드 실패: ' + url.pathname));
        })
    );
});

// [v6.7.360] Web Push 수신 - 배너 3종(AI 요약/CCU 급감/워처 공지)의 폰 미러.
//   payload = {title, body, url, tag} (서버 bg/web_push.py 발송 계약). 구독은 admin 이
//   보관소 패널에서 기기별 명시 옵트인 (main/notif-inbox.js). data.json() 실패는 기본값 표시.
self.addEventListener('push', (event) => {
    let d = {};
    try { d = event.data ? event.data.json() : {}; } catch (_) { d = {}; }
    const opts = {
        body: d.body || '',
        icon: '/static/icons/icon-192.png',
        badge: '/static/icons/icon-192.png',
        data: { url: d.url || '/' },
    };
    // [v6.7.375] 동일 tag = 알림판에 쌓지 않고 기존 알림을 교체(갱신). renotify=true 라야 교체 시
    //   소리/진동으로 다시 알린다(tag 없이 renotify 는 에러라 tag 있을 때만). 사용자 요청:
    //   "알림 리스트가 계속 늘어남 → 추가 말고 갱신으로".
    if (d.tag) { opts.tag = d.tag; opts.renotify = true; }
    event.waitUntil(self.registration.showNotification(d.title || 'IME 알림', opts));
});

self.addEventListener('notificationclick', (event) => {
    event.notification.close();
    let url = (event.notification.data && event.notification.data.url) || '/';
    // [v6.7.427] notif=1 마커 - boot.js 가 모바일(pointer:coarse)에서 탭 딥링크 대신 알림 보관소를 연다.
    //   데스크탑은 마커 무시하고 기존 ?tab= 딥링크 유지(사용자 요청: "모바일의 경우 보관소만").
    url += (url.indexOf('?') >= 0 ? '&' : '?') + 'notif=1';
    // 열린 IME 창이 있으면 focus + 이동, 없으면 새 창 (딥링크 ?tab= 은 boot.js v6.7.352 처리)
    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
            for (const c of list) {
                if ('focus' in c) {
                    if ('navigate' in c) { try { c.navigate(url); } catch (_) {} }
                    return c.focus();
                }
            }
            return clients.openWindow(url);
        })
    );
});
