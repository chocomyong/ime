// main/boot.js — 앱 초기화 + 접근 락 가드 + Cloudflare 522 위장
// Phase 3 3차 분할 — 기존 main.js 에서 이관

// [v6.7.3] 페이지 첫 진입 시 SW 캐시가 옛 build_version 인지 검사. 불일치면 nuke + reload.
//   기존 mismatch 자동 reload(socket.js) 는 server_version 비교 기반인데, 새 PC 가 옛 SW 캐시 가지고
//   첫 진입할 때 페이지 build_version 과 server_version 은 동일해 mismatch 가 안 잡힘. SW 캐시 키로
//   감지하는 별도 가드 — 이게 사용자 본 회귀(드로어 padding 옛 36px)의 진짜 원인.
async function _checkStaleSwCacheOnBoot() {
    try {
        if (!('caches' in window)) return;
        if (!('serviceWorker' in navigator) || !navigator.serviceWorker.controller) return;
        const meta = document.querySelector('meta[name="imt-build-version"]');
        const pageVer = meta ? meta.getAttribute('content') : '';
        if (!pageVer) return;
        const keys = await caches.keys();
        const imtKeys = keys.filter(k => k.startsWith('imt-'));
        if (!imtKeys.length) return;  // SW 첫 install 직전 — 정상
        const expected = 'imt-' + pageVer;
        if (imtKeys.includes(expected)) return;  // 일치 — 정상
        console.log('[stale-sw-cache] page=' + pageVer + ' caches=' + JSON.stringify(imtKeys) + ' → nuke + reload');
        // [v6.7.371] 조용한 업데이트 — 첫 진입 stale 캐시 오버레이 폐지. 콘텐츠 렌더 전이라 조용한 재로드로 충분.
        setTimeout(function () {
            if (typeof window.nukeSwCacheAndReload === 'function') window.nukeSwCacheAndReload({ reason: 'stale-sw-cache' });
            else if (typeof window.imtGuardAutoReload !== 'function' || window.imtGuardAutoReload('stale-sw-cache-fallback')) location.reload();
        }, 1000);
    } catch (e) {
        console.warn('[stale-sw-cache] check 실패', e);
    }
}

// [v6.7.3] reload 직후 스크롤 위치 복원 — sessionStorage 에 nukeSwCacheAndReload 가 저장한 scrollY 사용.
//   30초 이내 reload 만 적용 (오래된 값은 무시).
function _restoreScrollAfterReload() {
    try {
        const saved = sessionStorage.getItem('imt_pre_reload_scroll');
        const ts = parseInt(sessionStorage.getItem('imt_pre_reload_ts') || '0', 10);
        sessionStorage.removeItem('imt_pre_reload_scroll');
        sessionStorage.removeItem('imt_pre_reload_ts');
        if (!saved) return;
        if (Date.now() - ts > 30000) return;  // 30초 초과 — 의도된 자동 reload 가 아닐 가능성
        const y = parseInt(saved, 10);
        if (y > 0) {
            // 카드 그리드 비동기 렌더 후 적용 — 1.5초 지연
            setTimeout(function () {
                window.scrollTo({ top: y, behavior: 'auto' });
            }, 1500);
        }
    } catch (e) {}
}

function init() {
    // [v6.7.3] 첫 진입 stale SW 캐시 검사 (비동기, 본 init 흐름 막지 않음) + 스크롤 복원
    _checkStaleSwCacheOnBoot();
    _restoreScrollAfterReload();

    // [v6.7.100] Phase D — 핵심 데이터 fetch 즉시 병렬 발사 (랜딩 LCP 단축).
    //   기존 흐름: setupWebSocket → updateDashboard → 각 loadXxx 가 fetch (동기 cascade).
    //   socket connect race + 단일 워커 직렬 처리로 첫 paint 까지 10~20s 가능.
    //   본 변경: init() 첫 단계에서 articles/comments 를 Promise.all 로 발사 → 응답 도착 시
    //   _feedTotalCache / allComments hydrate + 활성 탭이면 render 트리거.
    //   후속 updateDashboard 의 loadFeedPane / loadComments 는 캐시 hit 으로 fetch skip
    //   (feed-tab.js / comments-tab.js 의 캐시 hit 가드와 정합).
    // [v6.7.169] 부팅 hydrate 도 저장된 필터 한도 존중 — 첫 페인트가 서버 default(500)로 캡되지 않도록.
    //   _localSettingsData 는 아직 미hydrate 가능 → localStorage(imt_display_settings) 직접 read.
    //   공식은 _imtFetchBudget (core/highlight.js) 공용 — 평소(미설정)엔 500, 디버깅(고한도/무제한)엔 상한(10000)까지.
    const _bootSettings = (function () { try { return JSON.parse(localStorage.getItem('imt_display_settings') || '{}'); } catch (_) { return {}; } })();
    const _bootFeedLim = (typeof _imtFetchBudget === 'function') ? _imtFetchBudget(_bootSettings, 'limits', 'visibility', 10000) : 500;
    const _bootCmtLim = (typeof _imtFetchBudget === 'function') ? _imtFetchBudget(_bootSettings, 'commentLimits', 'commentVisibility', 10000) : 500;
    Promise.all([
        fetch(`${API_BASE}/articles/all?limit=${_bootFeedLim}`).then(r => r.ok ? r.json() : null).catch(() => null),
        fetch(`${API_BASE}/comments?limit=${_bootCmtLim}`).then(r => r.ok ? r.json() : null).catch(() => null)
    ]).then(([articles, comments]) => {
        if (Array.isArray(articles)) {
            try {
                // [v6.7.731] 위 fetch 는 games/source/flagged 를 **안 붙인 글로벌 질의**다. 이 응답을
                //   피드 캐시로 쓸 수 있는지(출처·제재뿐 아니라 **게임 숨김**까지 꺼져 있고, loadFeedPane
                //   이 아직 제 축으로 안 받아왔는지) 판단하고, 쓴다면 질의 키를 찍는 일을 feed-tab.js 의
                //   `_feedAdoptBootPrefetch` 가 한 곳에서 한다. 종전엔 boot 가 출처·제재만 보고 games 를
                //   놓쳐 게임 숨긴 사용자가 글로벌 스트림을 첫 화면으로 봤고, 캐시키도 안 찍었다(PATTERN-55).
                //   판별기 사본을 boot 에 두지 않는다 - 서버 축이 늘 때 한쪽만 늘어난다.
                //   채택 안 되면 loadFeedPane 이 제 축으로 받아온다(첫 페인트가 조금 늦는 게 틀린 목록보다 낫다).
                if (typeof window._feedAdoptBootPrefetch === 'function'
                    && window._feedAdoptBootPrefetch(articles)
                    && typeof activeApps !== 'undefined' && Array.isArray(activeApps)
                    && activeApps.includes('feed') && typeof renderFeedPane === 'function') {
                    renderFeedPane();
                }
            } catch (e) { console.warn('[boot] articles hydrate 실패:', e); }
        }
        if (Array.isArray(comments)) {
            try {
                if (typeof allComments !== 'undefined') {
                    allComments = comments;
                    // [v6.7.104] flag set — 후속 updateDashboard → loadComments 가 1회만 fetch skip.
                    window._commentsBootHydrated = true;
                    if (typeof activeApps !== 'undefined' && Array.isArray(activeApps) &&
                        activeApps.includes('comments') && typeof renderComments === 'function') {
                        renderComments(allComments);
                    }
                }
            } catch (e) { console.warn('[boot] comments hydrate 실패:', e); }
        }
    });

    // [v6.7.419] admin pane 프리페치 — AI 요약/디스코드는 부트 cascade 끝(updateDashboard→loadPaneData)에서야
    //   fetch 가 시작돼, HTTP/1.1 동시 6커넥션 큐에서 대형 articles 전송 뒤로 밀림(사용자: "가장 늦게 업로드").
    //   피드/댓글(위 v6.7.100)과 같은 지위로 부트 즉시 발사 — 응답 Promise 를 window._bootPrefetch 에 1회용
    //   보관, 각 탭 로더가 이 Promise 를 기다렸다 소화(같은 요청이므로 이중 fetch 없음). 비인가(403/락 522)는
    //   null 로 귀결 → 로더가 정상 fetch 로 자연 폴백(비 admin 은 탭 자체가 없어 무해). C(localStorage 스냅샷)는
    //   하루 걸러 접속 패턴에서 stale 첫화면 오판 위험만 있어 기각(사용자 2026-07-21).
    window._bootPrefetch = {};
    try {
        // [v6.7.425] 15s 타임아웃 - 스톨 요청(빌드창 프록시 대기 등)은 catch 미발화라 소비측(탭 로더)
        //   await 가 무한 블록돼 '로드 중…' 고착(PATTERN-40). 타임아웃 abort → null 귀결 → 로더 정상 fetch 폴백.
        const _pfFetch = (url) => {
            let _sig;
            try { const c = new AbortController(); setTimeout(() => c.abort(), 15000); _sig = c.signal; } catch (_) { _sig = undefined; }
            return fetch(url, { credentials: 'same-origin', signal: _sig })
                .then(r => r.ok ? r.json() : null).catch(() => null)
                .then(d => ({ data: d, ts: Date.now() }));
        };
        // [v6.7.738] 익명은 /api/ai-summary 가 403(manager 게이트)이고 이 응답을 쓰는 탭·배너도 없다 -
        //   역할을 아는 경우(window._role 은 _guardAdminOnlyAccess 가 init 전에 설정) 익명이면 프리페치를
        //   건너뛴다(쓸모없는 403 1건 제거). role 미판정(admin/check 실패)이면 기존대로 쏜다 - 관리자
        //   세션의 첫화면 지연을 늘리지 않기 위함(seed/탭 로더는 프리페치 부재 시 정상 fetch 로 폴백).
        if (window._role !== 'anon') {
            // [v6.7.738] 배너 seed(socket.js)가 읽는 별칭 aiSummarySeed 를 함께 건다 - 탭 로더
            //   (ai-summary-tab.js)는 .aiSummary 를 소비하며 delete 하는데, 기본 구성(DEFAULT_APP_IDS=
            //   ['feed','ai-summary'])에선 그 delete 가 init()->updateDashboard 동기 경로에서 4s seed 보다
            //   먼저 일어나, seed 가 .aiSummary 를 못 보고 자체 fetch 로 폴백했다(요청 3->1 이 아니라 3->2).
            //   seed 는 지워지지 않는 이 별칭을 읽는다(같은 Promise = 추가 fetch 0, 둘 다 같은 1건을 await).
            window._bootPrefetch.aiSummary = window._bootPrefetch.aiSummarySeed = _pfFetch(`${API_BASE}/ai-summary`);
        }
        // [v6.7.741] 디스코드 메시지 프리페치 삭제 - 디스코드 탭 · 라우트 제거.
    } catch (_) { /* 프리페치는 최적화일 뿐 — 실패해도 기존 경로 무손상 */ }

    // [v6.7.731] _restoreAdvancedSection 호출 삭제 - #sidebar-advanced 마크업은 레포에 없어(grep 0) 상시 no-op.

    // [v6.6.423] 카드 강제 모드 자동 토글 — 통합 모니터링 환경(3열).
    //   v6.6.422 의 1100px 임계값은 사용자 의도("PC도 모바일 UI") 미달 → PC 도 기본 ON.
    //   사용자가 명시적으로 imt_layout_cards=0 으로 두면 OFF 존중. resize 는 200ms throttle.
    _applyLayoutCardsClass();
    let _layoutResizeTimer = null;
    window.addEventListener('resize', () => {
        if (_layoutResizeTimer) return;
        _layoutResizeTimer = setTimeout(() => {
            _layoutResizeTimer = null;
            _applyLayoutCardsClass();
        }, 200);
    });

    if (typeof setupWebSocket === 'function') setupWebSocket();

    // 화면 표시 설정 초기 로드 (localStorage 우선, 서버 fallback)
    // [v6.7.97] imt_settings_games_cache (games/limits/visibility 서버 응답 캐시) 즉시 hydrate.
    //   imt_display_settings (개인 필터) 와는 분리 — 전자는 서버 응답 캐시, 후자는 클라이언트 토글 상태.
    //   목적: 필터 모달 첫 진입 시 빈 게임 카드 회피 (이전 세션 캐시 hit).
    if (typeof _localSettingsData !== 'undefined') {
        let baseData = { games: [], limits: {}, visibility: {}, comment_limit: 100 };
        try {
            const cached = localStorage.getItem('imt_settings_games_cache');
            if (cached) {
                const parsed = JSON.parse(cached);
                if (parsed && typeof parsed === 'object') {
                    baseData = {
                        games: Array.isArray(parsed.games) ? parsed.games : [],
                        limits: parsed.limits || {},
                        visibility: parsed.visibility || {},
                        comment_limit: parsed.comment_limit !== undefined ? parsed.comment_limit : 100
                    };
                }
            }
        } catch (e) {
            console.warn('[init] imt_settings_games_cache JSON.parse 실패, 무시:', e);
            try { localStorage.removeItem('imt_settings_games_cache'); } catch (_) {}
        }
        let personal = null;
        try {
            const saved = localStorage.getItem('imt_display_settings');
            if (saved) personal = JSON.parse(saved);
        } catch (e) {
            console.warn('[init] imt_display_settings JSON.parse 실패:', e);
            try { localStorage.removeItem('imt_display_settings'); } catch (_) {}
        }
        _localSettingsData = personal ? { ...baseData, ...personal } : baseData;
    }
    fetch(`${API_BASE}/config/display-settings`)
        .then(r => r.json())
        .then(data => {
            if (typeof _localSettingsData !== 'undefined') {
                // [v6.6.236] JSON.parse 가드 — localStorage 손상 시 init 체인 중단 방지
                let parsedSaved = null;
                try {
                    const saved = localStorage.getItem('imt_display_settings');
                    if (saved) parsedSaved = JSON.parse(saved);
                } catch (e) {
                    console.warn('[init] imt_display_settings JSON.parse 실패, 서버값만 사용:', e);
                    localStorage.removeItem('imt_display_settings');
                }
                _localSettingsData = parsedSaved ? { ...data, ...parsedSaved } : data;
                // [v6.7.97] 서버 응답으로 games_cache 영속 캐시 갱신.
                try {
                    localStorage.setItem('imt_settings_games_cache', JSON.stringify({
                        games: Array.isArray(data.games) ? data.games : [],
                        limits: data.limits || {},
                        visibility: data.visibility || {},
                        comment_limit: data.comment_limit !== undefined ? data.comment_limit : 100,
                        ts: Date.now()
                    }));
                } catch (_) {}
                // [v6.7.97] 모달이 이미 열려있는 경우 (boot 와 사용자 클릭 race) 재렌더로 서버 최신값 반영.
                const _modalEl = document.getElementById('settingsModal');
                if (_modalEl && _modalEl.style.display !== 'none' && typeof _renderLocalSettings === 'function') {
                    try { _renderLocalSettings(); } catch (_) {}
                }
            }
        })
        .catch(() => {});

    if (typeof updateDashboard === 'function') updateDashboard();

    // [v6.7.352] ?tab= 딥링크 - manifest shortcuts(설치형 홈 아이콘 점프리스트)/외부 공유 링크로 특정 pane 직행.
    //   유효성 = 사이드바 nav 항목(app-box-<id>) 존재로 판정 - admin 전용 탭은 비관리자 HTML 에 서버
    //   조건부(v6.7.254)로 아예 없으므로 권한 검증이 함께 된다. 무효/비인가 값은 조용히 무시(기본 뷰 유지).
    // [v6.7.427] 모바일 푸시 클릭(sw.js notif=1) → 탭 딥링크 대신 알림 보관소만 오픈(사용자: "모바일은 보관소만").
    //   데스크탑/비푸시 진입은 기존 ?tab= 딥링크 유지. 판정 = pointer:coarse.
    try {
        const _params = new URLSearchParams(location.search);
        const _fromPush = _params.get('notif') === '1';
        const _isMobile = window.matchMedia && window.matchMedia('(pointer: coarse)').matches;
        if (_fromPush && _isMobile && window.imtNotifInbox && typeof window.imtNotifInbox.open === 'function') {
            window.imtNotifInbox.open();
        } else {
            const _deepTab = _params.get('tab');
            if (_deepTab && /^[a-z0-9-]+$/.test(_deepTab)
                && document.getElementById('app-box-' + _deepTab)
                && typeof ensureAppPane === 'function') {
                ensureAppPane(_deepTab);
            }
        }
    } catch (_) {}

    // [v6.7.741] updateStatus() 호출 삭제 - 함수째 지웠다(헤더 상태 대시보드 전용, core/api-client.js 참조).
    if (typeof updateCountdowns === 'function') updateCountdowns();

    if (typeof pollJasaStatus === 'function') pollJasaStatus();
    if (typeof pollTasaStatus === 'function') pollTasaStatus();
    if (typeof pollAccountTradeStatus === 'function') pollAccountTradeStatus();
    if (typeof pollOdinGlobalStatus === 'function') pollOdinGlobalStatus();

    // [추가] 관리자 권한 확인 (IP 체크)
    if (typeof checkAdminStatus === 'function') checkAdminStatus();

    // [v6.6.328] 관리자 세션 변동 자동 감지 — 창 2개를 같은 프로필로 열어두고 한쪽에서 로그아웃 시
    //   다른 창도 60초 이내에 자동으로 관리자 UI 를 무음 revoke. 토스트/알림 없음 (사용자 요구).
    setInterval(() => { if (typeof checkAdminStatus === 'function') checkAdminStatus(); }, 60000);

    updateInterval = setInterval(() => {
        if (typeof updateCountdowns === 'function') updateCountdowns();
    }, 30000);

    const trendDataEl = document.getElementById('trendData');
    if (trendDataEl) {
        trendDataEl.addEventListener('paste', typeof handleTrendDataPaste === 'function' ? handleTrendDataPaste : null);
        trendDataEl.addEventListener('input', typeof saveCurrentTrendState === 'function' ? saveCurrentTrendState : null);
    }

    const trendSheetLinkEl = document.getElementById('trendSheetLink');
    if (trendSheetLinkEl) {
        trendSheetLinkEl.addEventListener('input', typeof saveCurrentTrendState === 'function' ? saveCurrentTrendState : null);
    }

    // [v6.7.191 FEAT-01] ccuDashboard / ccuTemplate 초기화 폐기 — CCU 탭 마크업 제거와 동반.

    // [v6.7.191 IA-02] 하단 탭바 가로 스크롤 시 .scrollable 클래스 토글 → CSS mask-image 우측 fade 발화.
    //   admin 모드 진입 시 5→13탭 동적 갱신을 MutationObserver 가 자동 감지 (탭 마크업 변경 호출처마다 hook 추가 필요 없음).
    const _bottomTabbar = document.querySelector('.bottom-tabbar');
    if (_bottomTabbar) {
        const _updateScrollable = () => {
            const scrollable = _bottomTabbar.scrollWidth > _bottomTabbar.clientWidth + 1;
            _bottomTabbar.classList.toggle('scrollable', scrollable);
        };
        _updateScrollable();
        window.addEventListener('resize', _updateScrollable);
        new MutationObserver(_updateScrollable).observe(_bottomTabbar, { childList: true, subtree: false });
    }

    // [v6.7.731] beforeunload saveScrollPosition 배선 삭제 - 저장하던 sessionStorage 키(windowScrollY /
    //   totalArticlesScrollTop / logOutputScrollTop)를 읽는 코드가 레포에 없어(grep 0) dead write 였다.
}

// [v6.6.422] 통합 모니터링 환경 — body.layout-cards 자동 토글.
//   조건: pointer:coarse(터치) OR viewport <= 1100px OR localStorage 사용자 토글.
//   사용자 토글(이번 라운드 미설치, 후속 라운드 설정 모달에서 추가 예정).
// [v6.7.731] toggleAdvancedSection / _restoreAdvancedSection 삭제 - #sidebar-advanced 마크업이 레포에
//   없어(grep 0) 호출 0 + 상시 no-op 이었다(고급 섹션 UI 는 은퇴).
function _applyLayoutCardsClass() {
    // [v6.6.423] 기본 ON. 사용자가 imt_layout_cards='0' 으로 명시한 경우만 OFF.
    let want = true;
    try {
        const userPref = localStorage.getItem('imt_layout_cards');
        if (userPref === '0') want = false;
    } catch (e) {}
    document.body.classList.toggle('layout-cards', want);
    // [v6.6.434] 모바일(pointer:coarse) 매칭 시 7일 그래프 5분 폴링 중단 — leak 방지.
    //   dashboard pane 이 모바일에서 노출되어도 폴링 자체는 데이터 절약.
    try {
        if (window.matchMedia && window.matchMedia('(pointer: coarse)').matches) {
            if (typeof stopGraph7daysPolling === 'function') stopGraph7daysPolling();
        }
    } catch (_) {}
}


// [v6.6.353] 관리자 외 접근 차단 가드 — load 이벤트 시점에 최우선으로 실행.
//   서버 SQLite(system_settings.access_lock) 가 단일 source-of-truth. 기존 localStorage 경로 폐지.
//   `/api/admin/check` 응답에 `access_lock` 가 동봉되어 단일 호출로 락 + 관리자 여부를 판단.
//   락 ON 이면서 비관리자면 페이지 본문을 Cloudflare Error 522 위장 화면으로 통째 교체.
//   "관리자/권한/로그인" 같은 기능 존재 힌트 문자열 일절 노출 금지 — 관리자 기능 은폐 목적.
async function _guardAdminOnlyAccess() {
    try {
        const resp = await fetch(`${API_BASE}/admin/check`, { cache: 'no-store' });
        if (!resp.ok) return true; // admin/check 자체가 실패 → 락 상태 판단 불가, 통과 (네트워크 문제는 다른 경로에서 드러남)
        const data = await resp.json();
        // 전역 캐시 — 이후 checkAdminStatus 중복 호출 최소화
        // [v6.7.248] role 동봉 — manager 등급 인지. is_admin 은 role 파생(admin 만 true).
        const _role = (data && data.role) || (data && data.is_admin ? 'admin' : 'anon');
        try {
            window._role = _role;
            window._isAdmin = _role === 'admin';
        } catch (e) {}
        // [v6.7.263] access_lock 시 manager(중간 관리자)도 통과 — anon 만 차단 (사용자 정책 변경).
        if (data && data.access_lock && _role !== 'admin' && _role !== 'manager') {
            _renderCloudflareErrorPage();
            return false;
        }
        return true;
    } catch (e) {
        // 네트워크 오류 시에는 통과 — 실제 네트워크 장애까지 차단 화면으로 덮으면 정상 사용자 경험을 해침
        return true;
    }
}

function _renderCloudflareErrorPage() {
    // 기존 DOM 을 전부 제거 — 탭/사이드바/크롤러 폴링 등 아무 것도 실행되지 않도록.
    try { document.documentElement.innerHTML = ''; } catch (e) {}
    const now = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    const ts = `${now.getUTCFullYear()}-${pad(now.getUTCMonth()+1)}-${pad(now.getUTCDate())} ${pad(now.getUTCHours())}:${pad(now.getUTCMinutes())}:${pad(now.getUTCSeconds())} UTC`;
    // Ray ID: 12자리 hex + '-' + 데이터센터 3글자 코드 (실제 CF 포맷). ICN=서울 기본.
    const rayHex = Array.from({length:12}, () => '0123456789abcdef'[Math.floor(Math.random()*16)]).join('');
    const rayId = `${rayHex}-ICN`;
    const host = (location && location.hostname) ? location.hostname : 'imt-tools.local';
    // 도시 라벨 — 실제 CF 에지 POP. ICN 이면 "Seoul".
    const edgeCity = 'Seoul';

    // 3개 상태 아이콘 — 브라우저(working) / Cloudflare(working) / Host(error) 픽토그램.
    const iconBrowser = `<svg xmlns="http://www.w3.org/2000/svg" width="110" height="110" viewBox="0 0 110 110" aria-hidden="true"><rect x="8" y="18" width="94" height="70" rx="4" fill="none" stroke="#a7acb1" stroke-width="3"/><rect x="8" y="18" width="94" height="14" fill="#a7acb1"/><circle cx="16" cy="25" r="2" fill="#fff"/><circle cx="24" cy="25" r="2" fill="#fff"/><circle cx="32" cy="25" r="2" fill="#fff"/><circle cx="86" cy="84" r="14" fill="#8bc53f"/><path d="M80 84 l4 4 l10 -10" fill="none" stroke="#fff" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
    const iconCloud = `<svg xmlns="http://www.w3.org/2000/svg" width="110" height="110" viewBox="0 0 110 110" aria-hidden="true"><path d="M30 75 q-18 0 -18 -16 q0 -14 14 -16 q3 -14 18 -14 q14 0 18 12 q4 -2 8 -2 q14 0 14 14 q10 2 10 12 q0 10 -14 10 z" fill="#a7acb1"/><circle cx="88" cy="84" r="14" fill="#8bc53f"/><path d="M82 84 l4 4 l10 -10" fill="none" stroke="#fff" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
    const iconHost = `<svg xmlns="http://www.w3.org/2000/svg" width="110" height="110" viewBox="0 0 110 110" aria-hidden="true"><rect x="18" y="22" width="74" height="22" rx="3" fill="none" stroke="#a7acb1" stroke-width="3"/><rect x="18" y="52" width="74" height="22" rx="3" fill="none" stroke="#a7acb1" stroke-width="3"/><circle cx="28" cy="33" r="3" fill="#a7acb1"/><circle cx="28" cy="63" r="3" fill="#a7acb1"/><line x1="40" y1="33" x2="80" y2="33" stroke="#a7acb1" stroke-width="2"/><line x1="40" y1="63" x2="80" y2="63" stroke="#a7acb1" stroke-width="2"/><circle cx="88" cy="84" r="14" fill="#d9534f"/><path d="M82 78 l12 12 M94 78 l-12 12" fill="none" stroke="#fff" stroke-width="3" stroke-linecap="round"/></svg>`;
    const iconArrow = `<svg xmlns="http://www.w3.org/2000/svg" width="60" height="20" viewBox="0 0 60 20" aria-hidden="true"><path d="M6 10 l8 -6 v4 h32 v-4 l8 6 l-8 6 v-4 h-32 v4 z" fill="#a7acb1"/></svg>`;

    const html = `
<head><meta charset="utf-8"><title>${host} | 522: Connection timed out</title><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#fff;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Oxygen,Ubuntu,Helvetica,Arial,sans-serif;color:#404041;min-height:100vh;">
  <div style="max-width:960px;margin:0 auto;padding:40px 24px 0;">
    <div style="display:flex;justify-content:space-between;align-items:center;padding-bottom:18px;border-bottom:1px solid #e5e8eb;margin-bottom:38px;">
      <div style="font-size:13px;color:#68737d;">${host}</div>
      <div style="font-size:13px;color:#68737d;">Performance &amp; security by <strong style="color:#f6821f;">Cloudflare</strong></div>
    </div>
    <div style="margin-bottom:12px;">
      <div style="display:flex;align-items:baseline;flex-wrap:wrap;gap:18px;">
        <div style="font-size:72px;font-weight:400;color:#404041;line-height:1;letter-spacing:-0.02em;">Error 522</div>
        <div style="font-family:'Courier New',Courier,monospace;font-size:13px;color:#404041;">Ray ID: ${rayId} &nbsp;&bull;&nbsp; ${ts}</div>
      </div>
      <div style="font-size:34px;font-weight:300;color:#a7acb1;margin-top:6px;line-height:1.1;">Connection timed out</div>
    </div>
  </div>
  <div style="background:#ededed;padding:56px 24px 60px;">
    <div style="max-width:960px;margin:0 auto;display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:nowrap;">
      <div style="flex:0 0 auto;text-align:center;">
        <div>${iconBrowser}</div>
        <div style="font-size:15px;color:#404041;margin-top:8px;">You</div>
        <div style="font-size:22px;font-weight:300;color:#a7acb1;margin-top:14px;">Browser</div>
        <div style="font-size:18px;color:#8bc53f;margin-top:2px;">Working</div>
      </div>
      <div style="flex:0 0 auto;margin-top:-48px;">${iconArrow}</div>
      <div style="flex:0 0 auto;text-align:center;">
        <div>${iconCloud}</div>
        <div style="font-size:15px;color:#404041;margin-top:8px;">${edgeCity}</div>
        <div style="font-size:22px;font-weight:300;color:#a7acb1;margin-top:14px;">Cloudflare</div>
        <div style="font-size:18px;color:#8bc53f;margin-top:2px;">Working</div>
      </div>
      <div style="flex:0 0 auto;margin-top:-48px;">${iconArrow}</div>
      <div style="flex:0 0 auto;text-align:center;">
        <div>${iconHost}</div>
        <div style="font-size:15px;color:#404041;margin-top:8px;">${host}</div>
        <div style="font-size:22px;font-weight:300;color:#a7acb1;margin-top:14px;">Host</div>
        <div style="font-size:18px;color:#d9534f;margin-top:2px;">Error</div>
      </div>
    </div>
  </div>
  <div style="max-width:960px;margin:0 auto;padding:36px 24px 40px;display:flex;gap:40px;flex-wrap:wrap;">
    <div style="flex:1;min-width:280px;">
      <h2 style="font-size:18px;font-weight:600;color:#404041;margin:0 0 12px;">What happened?</h2>
      <p style="font-size:14px;line-height:1.65;color:#404041;margin:0;">The initial connection between Cloudflare's network and the origin web server timed out. As a result, the web page can not be displayed.</p>
    </div>
    <div style="flex:1;min-width:280px;">
      <h2 style="font-size:18px;font-weight:600;color:#404041;margin:0 0 12px;">What can I do?</h2>
      <p style="font-size:14px;line-height:1.65;color:#404041;margin:0 0 12px;"><strong>If you're a visitor of this website:</strong><br>Please try again in a few minutes.</p>
      <p style="font-size:14px;line-height:1.65;color:#404041;margin:0;"><strong>If you're the owner of this website:</strong><br>Contact your hosting provider letting them know your web server is not responding. <a href="#" onclick="location.reload();return false;" style="color:#2c7cb0;text-decoration:none;">Additional troubleshooting information</a>.</p>
    </div>
  </div>
  <div style="max-width:960px;margin:0 auto;padding:20px 24px 40px;border-top:1px solid #e5e8eb;font-size:12px;color:#98a0a6;display:flex;justify-content:space-between;flex-wrap:wrap;gap:10px;">
    <div>Cloudflare Ray ID: <span style="font-family:'Courier New',Courier,monospace;">${rayId}</span></div>
    <div>Your IP: <span style="font-family:'Courier New',Courier,monospace;">—</span> &nbsp;&bull;&nbsp; Performance &amp; security by <strong style="color:#f6821f;">Cloudflare</strong></div>
  </div>
</body>`;
    try { document.documentElement.innerHTML = html; } catch (e) { document.body.innerHTML = html; }
    try { document.title = `${host} | 522: Connection timed out`; } catch (e) {}
}

window.addEventListener('load', async () => {
    const ok = await _guardAdminOnlyAccess();
    if (!ok) return; // 차단 — init 및 모든 폴링 스킵
    init();
    _initNavA11y();   // [v6.7.188 A11Y-01] 내비 키보드 접근성 부여
});
window.addEventListener('beforeunload', () => clearInterval(updateInterval));

// [v6.7.188 A11Y-01] 사이드바/하단탭 내비 div(.nav-item/.btab-item) 키보드 접근성.
//   마크업 28개 개별 수정 대신 JS 1곳에서 role="button" + tabindex="0" 일괄 부여(저churn, 정적 DOM 이라 1회로 충분).
//   Enter/Space → click 위임. input/textarea/select/button/a/contenteditable 은 제외(설정모달·SQL·trendData 충돌 방지).
function _initNavA11y() {
    try {
        document.querySelectorAll('.nav-item, .btab-item').forEach(function (el) {
            if (!el.getAttribute('role')) el.setAttribute('role', 'button');
            if (el.getAttribute('tabindex') === null) el.setAttribute('tabindex', '0');
        });
    } catch (_) {}
}
document.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter' && e.key !== ' ' && e.key !== 'Spacebar') return;
    var t = e.target;
    if (!t || typeof t.closest !== 'function') return;
    var tag = (t.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select' || tag === 'button' || tag === 'a' || t.isContentEditable) return;
    var nav = t.closest('.nav-item, .btab-item');
    if (!nav) return;
    e.preventDefault();
    nav.click();
});
