// static/js/socket.js

function setupWebSocket() {
    const socket = io(); // 현재 호스트로 자동 연결

    // [v6.6.320] 비전공자 자동 하드리프레시 — 서버 재배포(BUILD_VERSION 갱신) 시 사용자가 Ctrl+Shift+R 없이
    // 자동으로 새 코드를 반영받도록 한다. 서버는 connect 직후 `server_version` 이벤트로 현재 BUILD_VERSION 을
    // emit. 클라이언트는 HTML `<meta name="imt-build-version">` 값과 비교해 다르면 자동 reload.
    //   - 최초 연결에선 미감지(같은 버전) 또는 최초 판정 후 2초 딜레이 동안 사용자 입력 허용.
    //   - 재연결(서버 재시작 후)에서 흔히 mismatch 발생 → 탭이 열려 있어도 자동 갱신.
    //   - Service Worker 와 무관하게 동작. SW 가 구버전 HTML 을 서빙해도 meta 값으로 판정됨.
    function _getPageBuildVersion() {
        try {
            var m = document.querySelector('meta[name="imt-build-version"]');
            return m ? (m.getAttribute('content') || '') : '';
        } catch (e) { return ''; }
    }
    var _reloadScheduled = false;
    // [v6.7.490] reload 직전 서버 준비 확인 - 배포 중 "서버 연결 끊김" 착지 방지.
    //   소켓은 gunicorn 이 커넥션을 받는 순간 붙지만 **HTTP 는 아직 못 받는 구간**이 있다
    //   (PATTERN-31/52 부팅 폭풍 — 부팅 직후 수십 초 CPU 포화). 그 구간에 location.reload() 가
    //   걸리면 브라우저가 역프록시 에러 페이지로 이동하고 **SPA 가 통째로 사라진다** → 사용자가
    //   수동 새로고침을 해야 한다(사용자 보고 2026-08-10). 화면을 유지한 채 서버가 실제로
    //   응답할 때까지 기다렸다가 reload 한다 — 기다리는 동안 기존 데이터로 계속 쓸 수 있다.
    var _READY_POLL_MS = 2000;
    var _READY_MAX_TRIES = 150;   // 약 5분 — 재빌드(3~5분)를 덮는다. 넘으면 포기하고 화면 유지.
    // [v6.7.501] 준비 판정을 **실질**로 바꾼다. v6.7.490 은 `/api/health` 로 판정했는데 그건
    //   `jsonify({'ok': True, 'ts': ...})` 한 줄이고 docstring 이 "내부 객체 의존 0" 을 명시한다 —
    //   즉 **gunicorn 이 bind 하는 순간 200** 이라, 부팅 폭풍(PATTERN-31/52)의 끝이 아니라 시작이다.
    //   그래서 reload 가 폭풍 한복판에 착지해 새 페이지가 1~2분간 스켈레톤이었다(사용자 2026-08-11).
    //   `/api/health` 자체는 못 고친다 — docker healthcheck + autoheal 이 물고 있어서 DB 를 태우게
    //   만들면 DB 가 느릴 때마다 컨테이너가 재시작된다(PATTERN-29 의 안전망이 PATTERN-31 의 오탐으로
    //   뒤집힌다). 그래서 **게이트만** 페이지가 실제로 필요로 하는 데이터 경로로 옮긴다.
    var _READY_URL = '/api/articles/all?limit=1';
    var _READY_STREAK = 2;   // 폭풍 중엔 간헐적으로 한 번씩 답한다 — 연속 2회여야 "났다" 로 본다

    // [v6.7.501] "아예 화면이 안 끊겼으면 좋겠다"(사용자 2026-08-11) — 준비가 확인돼도
    //   **보고 있는 동안에는 reload 하지 않는다.** reload 는 DOM 을 버리고 SW 캐시까지 날리므로
    //   (nukeSwCacheAndReload) 서버가 아무리 준비돼도 수 초의 빈 화면이 원리상 생긴다. 그 수 초를
    //   없앨 수는 없으니 **사용자가 안 보는 순간으로 옮긴다.** 탭이 숨겨졌거나(다른 창·최소화)
    //   15초간 조작이 없으면 지금 안 보고 있는 것으로 본다. 그동안 옛 페이지는 데이터를 그대로
    //   들고 계속 동작한다(PATTERN-32 캐시 보존) — 기다리는 것이 곧 무손실이다.
    //
    //   [v6.7.502] 숫자를 15초/60초로 줄였다(사용자 2026-08-11 "반영이 너무 늦는데"). 초안의
    //   60초/30분은 **화면을 종일 띄워 두는 사용자만 보고** 고른 값이라, **방금 배포하고 결과를
    //   확인하려는 사람**에게는 그 유예가 정확히 방해였다. 이 화면에는 그 두 부류가 같이 있다.
    //   상한 60초 = 계속 붙들고 있어도 1분 안에는 새 코드가 뜬다. 즉시 보려면 Ctrl+Shift+R 이
    //   언제나 더 빠른 길이며, 자동 reload 는 그걸 모르는 사용자를 위한 것이다.
    var _IDLE_MS = 15000;
    var _MAX_DEFER_MS = 60000;
    var _lastInputTs = Date.now();
    try {
        ['pointerdown', 'keydown', 'wheel', 'touchstart'].forEach(function (ev) {
            window.addEventListener(ev, function () { _lastInputTs = Date.now(); }, { passive: true });
        });
    } catch (e) { /* 리스너 등록 실패 = 유휴 판정 불가 → _MAX_DEFER_MS 상한이 폴백으로 남는다 */ }
    function _isUnattended() {
        try { if (document.hidden) return true; } catch (e) {}
        return (Date.now() - _lastInputTs) >= _IDLE_MS;
    }
    function _reloadWhenUnattended(deferStart) {
        if (_isUnattended()) {
            console.log('[auto-reload] 사용자 미주시 — reload');
            window.nukeSwCacheAndReload({ reason: 'server-version-mismatch' });
            return;
        }
        if ((Date.now() - deferStart) >= _MAX_DEFER_MS) {
            console.log('[auto-reload] 유예 상한(60초) 도달 — reload');
            window.nukeSwCacheAndReload({ reason: 'server-version-mismatch' });
            return;
        }
        setTimeout(function () { _reloadWhenUnattended(deferStart); }, 5000);
    }

    function _reloadWhenServerReady(tries, streak) {
        tries = tries || 0;
        streak = streak || 0;
        if (tries >= _READY_MAX_TRIES) {
            // 포기 = 화면 유지. 플래그를 풀어 다음 server_version 수신 때 다시 시도한다.
            console.warn('[auto-reload] 서버 준비 확인 실패 — 화면을 유지한다 (다음 재연결에서 재시도)');
            _reloadScheduled = false;
            return;
        }
        var ctl = (typeof AbortController === 'function') ? new AbortController() : null;
        var to = setTimeout(function () { try { ctl && ctl.abort(); } catch (e) {} }, 3000);
        fetch(_READY_URL, { cache: 'no-store', credentials: 'same-origin', signal: ctl ? ctl.signal : undefined })
            .then(function (r) {
                clearTimeout(to);
                if (!r.ok) throw new Error('HTTP ' + r.status);
                return r.json();   // 파싱까지 통과해야 "데이터를 낼 수 있다" 가 증명된다 — 상태코드만으론 부족
            })
            .then(function (d) {
                // **비었지만 빠른 응답**을 준비로 읽으면 안 된다. 이 엔드포인트는 메모리 스토어를 보는데,
                // 부팅 직후 hydrate 전에는 200 + `[]` 를 즉답한다(로컬 실측). 그 상태로 reload 하면
                // 스켈레톤 대신 "0건" 이 뜬다 — 로딩보다 더 나쁘게 읽힌다. 1건이라도 실려야 준비다.
                // (DB 가 진짜로 비어 있으면 게이트가 안 열리고 _READY_MAX_TRIES 뒤 화면 유지로 귀결 —
                //  옛 화면이 계속 살아 있으므로 안전한 실패 방향이다.)
                if (!Array.isArray(d) || d.length === 0) {
                    setTimeout(function () { _reloadWhenServerReady(tries + 1, 0); }, _READY_POLL_MS);
                    return;
                }
                var s = streak + 1;
                if (s < _READY_STREAK) {
                    setTimeout(function () { _reloadWhenServerReady(tries + 1, s); }, _READY_POLL_MS);
                    return;
                }
                console.log('[auto-reload] 서버 준비 확인 — 안 보는 순간을 기다린다');
                _reloadWhenUnattended(Date.now());
            })
            .catch(function () {
                clearTimeout(to);
                setTimeout(function () { _reloadWhenServerReady(tries + 1, 0); }, _READY_POLL_MS);
            });
    }
    function _maybeAutoReload(serverVersion) {
        if (_reloadScheduled) return;
        var pageVersion = _getPageBuildVersion();
        if (!pageVersion || !serverVersion) return;                 // meta 미부여 레거시 HTML — 스킵
        if (String(pageVersion) === String(serverVersion)) return;  // 동일 버전 — 정상
        _reloadScheduled = true;
        console.log('[auto-reload] 서버 버전 변경 감지', pageVersion, '->', serverVersion);
        // [v6.7.371] 조용한 업데이트 — 풀스크린 "서버 업데이트 중" 오버레이 폐지(사용자: 너무 거창/갑작).
        //   UI 없이 스크롤 보존 silent reload (nukeSwCacheAndReload 가 sessionStorage 로 scrollY 복원 → 티 안 남).
        // [v6.7.490] 3초 뒤 **무조건** reload 하던 것을 준비 확인 뒤 reload 로. 3초는 그대로 둔다
        //   (재연결 직후 다른 초기 emit 과 겹치지 않게 하는 기존 여유).
        setTimeout(function () { _reloadWhenServerReady(0); }, 3000);
    }

    // [v6.6.279] 재연결 시 여러 loadXxx 를 동시 호출 → DOM 조작 레이스 + 중복 요청 버스트.
    // debounce 로 300ms 내 요청 1회만, 순차 체인으로 호출해 DOM 순서 보장.
    let _refreshDebounceTimer = null;
    function refreshActivePanes() {
        if (_refreshDebounceTimer) clearTimeout(_refreshDebounceTimer);
        _refreshDebounceTimer = setTimeout(() => {
            _refreshDebounceTimer = null;
            // 순차 실행 — 각 함수 내부가 async 라면 자체 Promise 체인은 독립 유지
            // [v6.7.301] 재연결 시 비활성(숨김) 레거시 탭 무조건 fetch 제거 — feed/graph 처럼 activeApps 가드.
            //   repository 잔존 DOM 때문에 컨테이너 가드가 통과해 재연결 1회당 /api/articles/all?limit=10000 포함 대형 fetch 다발 발생하던 문제.
            const _refreshActive = (typeof activeApps !== 'undefined') ? activeApps : [];
            try { if (_refreshActive.includes('total') && typeof loadTotalArticles === 'function') loadTotalArticles(); } catch (e) { console.warn('[refresh] total', e); }
            try { if (_refreshActive.includes('games') && typeof loadGamesSections === 'function') loadGamesSections(); } catch (e) { console.warn('[refresh] games', e); }
            try { if (_refreshActive.includes('community') && typeof loadCommunitySections === 'function') loadCommunitySections(); } catch (e) { console.warn('[refresh] community', e); }
            try { if (_refreshActive.includes('ai-analysis') && typeof loadAiAnalysisData === 'function') loadAiAnalysisData(); } catch (e) { console.warn('[refresh] ai', e); }
            // [v6.7.425] ai-summary 재연결 디스패치 - 이 목록에서 홀로 누락돼(PATTERN-37 계열 부분 배선) 서버
            //   재시작(배포) 후 재연결 시 refetch 0. 신선 창(30s) 가드가 있어 직전 로드와 중복 없음.
            try { if (_refreshActive.includes('ai-summary') && typeof loadAiSummaryTab === 'function') loadAiSummaryTab(); } catch (e) { console.warn('[refresh] ai-summary', e); }
            // [v6.6.423] 통합 모니터링 환경 — feed/dashboard 합성 pane 추가 디스패치
            try { if (typeof loadFeedPane === 'function' && typeof activeApps !== 'undefined' && activeApps.includes('feed')) loadFeedPane(); } catch (e) { console.warn('[refresh] feed', e); }
            // [v6.6.469] 'dashboard' → 'situation' 교체 — v6.6.466 migration 누락. 그래프는 situation-tab 자체 폴링도 돌지만 재연결 직후 즉시 갱신 보장 위해 유지.
            try { if (typeof loadGraph7days === 'function' && typeof activeApps !== 'undefined' && activeApps.includes('situation')) loadGraph7days(); } catch (e) { console.warn('[refresh] graph', e); }
        }, 300);
    }

    // [v6.7.86] 배너 표시 지연 — 브리프 disconnect (워커 graceful rotation by --max-requests, 네트워크 jitter,
    //   engineio 폴링 일시 끊김) 이 < 2s 안에 reconnect 되면 사용자에게 안 보임. 진짜 장기 끊김만 배너 노출.
    //   v6.6.304 의 즉시 표시 정책은 PATTERN-29 의 --max-requests 1000 (v6.7.80) 와 결합 시 깜빡임 다발 →
    //   사용자가 "서버 재시작이 자주 뜬다" 로 오해. 실제 재시작 아닌 graceful 워커 로테이션.
    let _bannerShowTimer = null;
    function _scheduleBannerShow() {
        if (_bannerShowTimer) clearTimeout(_bannerShowTimer);
        _bannerShowTimer = setTimeout(() => {
            _bannerShowTimer = null;
            showServerOfflineBanner();
        }, 2000);
    }
    function _cancelBannerShow() {
        if (_bannerShowTimer) {
            clearTimeout(_bannerShowTimer);
            _bannerShowTimer = null;
        }
    }

    let _isFirstConnect = true;
    socket.on('connect', () => {
        console.log('WebSocket 서버에 연결되었습니다.');
        // [v6.6.304] 재연결 성공 시 서버 점검 배너 제거
        // [v6.7.86] 표시 예약된 배너도 함께 취소 (아직 안 뜬 경우 깜빡임 0)
        _cancelBannerShow();
        hideServerOfflineBanner();
        // [v6.7.2] 오버레이 예약 취소 + (mismatch 가 아닐 때만) 즉시 제거.
        //   mismatch 일 때는 _maybeAutoReload 가 다시 띄워서 reload 까지 유지 — race 회피 위해 cancel-only.
        cancelUpdateOverlaySchedule();
        if (!_reloadScheduled) hideUpdateOverlay();
        if (_isFirstConnect) {
            _isFirstConnect = false;
        } else {
            // 재연결 시 모든 탭 데이터 보충 — debounce 로 300ms 버퍼 후 1회만 실행
            refreshActivePanes();
            // [v6.7.371] "재연결되었습니다. 데이터를 갱신합니다" 토스트 폐지(사용자: 좌측하단 연결 알림 제거) — 갱신은 조용히.
            return;
        }
        // [v6.7.371] "실시간 서버에 연결되었습니다" 첫 연결 토스트도 폐지.
    });

    socket.on('disconnect', () => {
        console.log('WebSocket 서버와 연결이 끊겼습니다.');
        // [v6.7.383] 주황 상단 오프라인 배너 폐지 (사용자: 배포 중 화면 가림 - 자연스러운 무자각 업데이트 지향).
        //   v6.7.371 의 "조용한 업데이트"(오버레이·토스트 폐지) 연장선 - 마지막 침습 요소였던 disconnect 배너 제거.
        //   재연결은 socket.io 가 자동 재시도, 버전 변경 시 _maybeAutoReload 가 스크롤 보존 silent reload 로 마무리.
        //   → 배포 중 사용자는 기존 화면을 그대로 보다가 새 버전으로 조용히 갱신됨(끊김 표시 없음).
        // [v6.7.127] 시간 기반 trigger 폐기 (v6.7.2/122/126 사이클 종결) — 사용자 보고 "빌드 아닌데도 가끔 뜸".
        //   원인: 시간 기반은 transient (engineio polling jitter / Selenium CPU 포화 PATTERN-31) 가 우연히
        //   임계값 (1.5/5/10s) 도달 시 오버레이 발화. 빌드 외 운영 정상 시간에도 발화 가능.
        //   fix: socket.io 의 reconnect_attempt 이벤트 활용 — N회 시도 후도 실패 시만 overlay. 진짜 재연결
        //   불가 상태만 표시. 시간 기반 schedule 호출 제거.
    });

    // [v6.7.731] 빈 reconnect_attempt 핸들러 삭제 - v6.7.371 에 오버레이 로직이 빠져 console.log 만 남은 죽은 핸들러였다.
    //   socket.io 자동 재연결은 그대로 동작한다(핸들러는 표시 로직 전용이었고 그 로직은 이미 제거됨).

    // [v6.6.320] 서버가 connect/_send_initial_payload 에서 BUILD_VERSION 을 emit → 비교 후 mismatch 시 자동 reload.
    socket.on('server_version', (data) => {
        try { _maybeAutoReload(data && data.build_version); } catch (e) { console.warn('[auto-reload] 처리 실패', e); }
    });

    socket.on('logs_update', (data) => {
        if (data.reset || data.logs) {
            // 전체 재전송 (초기 로드 또는 로그 클리어 시)
            allLogs = data.logs || [];
            // [v6.7.731] 전체 교체 - 렌더 포인터 리셋 + 기존 DOM 비움(증분 append 중복 방지). renderLogs 가 전량 재구성.
            if (typeof _lastRenderedCount === 'number') _lastRenderedCount = 0;
            const _lo = document.getElementById('logOutput');
            if (_lo) _lo.innerHTML = '';
            console.log('WebSocket: 로그 전체 수신', allLogs.length, '개');
        } else if (data.delta) {
            // 증분 전송
            allLogs = allLogs.concat(data.delta);
            // 클라이언트 측 10000개 상한 유지
            if (allLogs.length > 10000) {
                const _dropped = allLogs.length - 10000;
                allLogs = allLogs.slice(-10000);
                // [v6.7.731] 앞에서 버린 만큼 렌더 포인터도 당긴다 - 안 그러면 버퍼 상한 도달 후
                //   slice(_lastRenderedCount) 인덱스가 밀려 신규 로그가 영구 미표시(로그 탭 정지)된다.
                if (typeof _lastRenderedCount === 'number') _lastRenderedCount = Math.max(0, _lastRenderedCount - _dropped);
            }
            console.log('WebSocket: 로그 증분 수신 +', data.delta.length, '개 (총', allLogs.length, ')');
        }
        if ((currentMainTab === 'logs' || activeApps.includes('log-viewer')) && typeof renderLogs === 'function') {
            renderLogs();
        }
    });

    // [v6.7.433] 경량 신호 핸들러 — 서버가 본문(무제한 ~1.22MB) 대신 빈 핑만 쏨(PATTERN-38 정상상태 순단 제거).
    //   옛 핸들러는 broadcast 본문을 total/games/community 에 직접 렌더 → 크롤 사이클마다 10000개를 메인스레드
    //   파싱. 이제 활성 뷰만 자체 refetch(각 로더 무인자 = self-fetch). 기본 뷰(feed)는 원래 payload 를 무시하고
    //   디바운스 refetch 했으므로 동작 무변화 — 낭비되던 1.22MB 파싱만 소멸. (옛 _groupByGame/_groupByCommunity
    //   헬퍼는 payload 그룹핑 전용이라 함께 제거; feed-tab.js 의 동명 헬퍼는 별개로 존치.)
    socket.on('raw_articles_update', () => {
        if (typeof incrementUnreadBadge === 'function') {
            const communityGroup = ['total', 'games', 'community'];
            const anyOpen = communityGroup.some(id => activeApps.includes(id));
            if (!anyOpen) {
                incrementUnreadBadge('total');
                incrementUnreadBadge('games');
                incrementUnreadBadge('community');
            }
        }
        console.log('WebSocket: raw_articles_update 신호 수신 (경량 - 자체 refetch)');
        // total/games/community 활성 시 자체 fetch (옛 broadcast 본문 직접 렌더 대체 — 무인자 = self-fetch)
        if (!tabDates['total'] && (currentMainTab === 'total' || activeApps.includes('total')) && typeof loadTotalArticles === 'function') {
            loadTotalArticles();
        }
        if (!tabDates['games'] && (currentMainTab === 'games' || activeApps.includes('games')) && typeof loadGamesSections === 'function') {
            loadGamesSections();
        }
        if (!tabDates['community'] && (currentMainTab === 'community' || activeApps.includes('community')) && typeof loadCommunitySections === 'function') {
            loadCommunitySections();
        }
        // [v6.6.469] feed pane 활성 시 디바운스 800ms refetch (feed 는 원래 payload 미사용 → 동작 무변화).
        if (typeof loadFeedPane === 'function' && activeApps.includes('feed')) {
            if (window._feedReloadDebounce) clearTimeout(window._feedReloadDebounce);
            window._feedReloadDebounce = setTimeout(function () {
                window._feedReloadDebounce = null;
                try { loadFeedPane(); } catch (e) { console.warn('[ws->feed] load 실패', e); }
            }, 800);
        }
    });

    // [v6.7.328] 서버(gemma4 워커)가 신규 요약 저장 시 {new_count: N} 경량 신호를 푸시 — 수신 시 자체 refetch.
    //   v6.7.256~327 동안 이 이벤트는 서버 emit 0건인 죽은 리스너였음(전체 리스트 payload 를 기대하던 옛 설계 잔재).
    //   갱신이 탭 재진입/재연결에만 의존해 카카오워크 AI 알림보다 웹 탭이 뒤처지던 증상의 근본 원인.
    //   AI 데이터 본문은 admin 가드 API 로만 수신(소켓 broadcast 는 익명 포함 전 클라 대상이라 본문 미포함).
    socket.on('ai_analysis_update', (data) => {
        if (typeof incrementUnreadBadge === 'function') {
            incrementUnreadBadge('ai-analysis');
            incrementUnreadBadge('ai-summary');
        }
        if (tabDates['ai-analysis'] != null) return; // 과거 날짜 조회 중 소켓 업데이트 무시
        const newCount = (data && data.new_count) || 0;
        console.log('WebSocket: AI 분석 업데이트 신호 수신 (+' + newCount + '건)');
        if ((currentMainTab === 'ai-analysis' || activeApps.includes('ai-analysis')) && typeof loadAiAnalysisData === 'function') {
            loadAiAnalysisData(); // null → 자체 fetch (서버 응답 캐시는 저장 시점에 무효화됨)
        }
        // AI 요약 탭 — 자체 fetch. 활성 시 디바운스 재로드 (푸시 버스트 흡수, feed 선례).
        if (activeApps.includes('ai-summary') && typeof loadAiSummaryTabDebounced === 'function') {
            loadAiSummaryTabDebounced();
        }
        // [v6.7.338] 새 AI 요약 항목별 인앱 배너. [v6.7.386] admin + manager 발화. [v6.7.500] 탭도 매니저 개방되어
        //   위 탭 로더(activeApps.includes('ai-summary'))가 매니저에게도 실제로 돈다 — 배너와 탭 재로드가 함께 뜬다.
        //   anon 은 _role 이 admin/manager 아니라 계속 제외.
        //   소켓 payload 는 count 신호만(PATTERN-37) — 본문은 함수 내부에서 API(manager 개방)로 수신.
        if ((window._role === 'admin' || window._role === 'manager') && newCount > 0) {
            _notifyNewAiSummaries();
        }
    });


    socket.on('status_update', (data) => {
        console.log('WebSocket: 상태 업데이트 수신', data);
        const mainCrawlerData = data.main_crawler || {};

        // [v6.7.741] 헤더 상태 대시보드(메인 크롤러 배지 · 서브 크롤러 헤더 pill · 본문/댓글 배지) 삭제 - 그 갱신 줄도 지웠다.
        // [v6.7.731] startBtn/stopBtn disabled 분기 삭제 - 그 DOM id 는 어느 템플릿에도 없다(getElementById 항상 null = 죽은 분기).
        //   (같은 꼴 sibling 이 core/api-client.js 에도 있으나 본 에이전트 범위 밖 - 부모 몫으로 보고.)
        nextCrawlTimes.cafe = mainCrawlerData.cafe;
        nextCrawlTimes.dc = mainCrawlerData.dc;
        if (typeof displayTimeLeft === 'function') displayTimeLeft(); 
        
        if (data.odin_global_crawler && typeof updateStatusBadge === 'function') {
            updateStatusBadge('odin-global-crawler-status', data.odin_global_crawler);
            if (!tabDates['diamond-trade'] && typeof handleOdinGlobalStatusUpdate === 'function') handleOdinGlobalStatusUpdate(data.odin_global_crawler);
        }
        if (data.account_trade_crawler && typeof updateStatusBadge === 'function') {
            updateStatusBadge('account-trade-crawler-status', data.account_trade_crawler);
            if (!tabDates['account-trade'] && typeof handleAccountTradeStatusUpdate === 'function') handleAccountTradeStatusUpdate(data.account_trade_crawler);
        }
        if (data.jasa_games_crawler && typeof updateStatusBadge === 'function') {
            updateStatusBadge('jasa-crawler-status', data.jasa_games_crawler);
            if (!tabDates['diamond-trade'] && typeof handleJasaStatusUpdate === 'function') handleJasaStatusUpdate(data.jasa_games_crawler);
        }
        if (data.tasa_games_crawler && typeof updateStatusBadge === 'function') {
            updateStatusBadge('tasa-crawler-status', data.tasa_games_crawler);
            if (!tabDates['diamond-trade'] && typeof handleTasaStatusUpdate === 'function') handleTasaStatusUpdate(data.tasa_games_crawler);
        }
        // [v6.7.731] batchRunBtn disabled/title 분기 삭제 - 일괄 추출 버튼(#batchRunBtn)은 v6.7.726 에 마크업째 삭제돼
        //   이 분기의 getElementById 는 항상 null(죽은 분기). anySubCrawlerRunning 집계도 이 분기 전용이라 함께 제거.
        //   (sibling 참조가 diamond-tab.js 에도 있으나 범위 밖 - 부모 몫으로 보고.)

        // [v6.7.628] 헬스 스트립(updateHealthStrip) 소비 제거 - 대시보드 헤더가 은퇴했다. data.health 는 서버가
        //   계속 실어 보내지만(broadcast.py:_build_health_summary) 클라이언트 소비처는 없다. 크롤러 상태는 관리자 탭
        //   crawlers-overview 가 보여 준다.
    });

    // [v6.7.628] 'situation_update' 리스너 제거 - 상황판 카드 폴링(situationTab_pull)이 사라졌다.
    // [v6.7.629] 서버 emit 도 제거됐다(bg/situation_aggregator 은퇴). 이 이벤트는 더 이상 존재하지 않는다.

    // [v6.7.442] anomaly_update — 서버 emit(bg/anomaly.py:239)만 있고 클라 리스너가 없어
    //   window.anomalyTabRefresh 가 호출처 0 의 죽은 함수였다(PATTERN-37 편측 배선). 폴링을 300s 로
    //   완화하면서 즉시성을 이 경로가 맡으므로 함께 배선. 페이로드는 경량 count 라 본문은 탭이 직접 받는다.
    socket.on('anomaly_update', () => {
        // [v6.7.451] 탭 갱신(열려 있을 때만) + 인앱 배너(탭과 무관, admin 한정 · visibility 필터).
        //   배너는 탭이 닫혀 있어도 떠야 하므로 탭 refresh 와 별 경로다.
        if (typeof window.anomalyTabRefresh === 'function') {
            try { window.anomalyTabRefresh(); } catch (e) { console.warn('[ws->anomaly]', e); }
        }
        try { _notifyNewAnomalies(); } catch (e) { console.warn('[ws->anomaly-banner]', e); }
    });

    // [v6.7.191 FEAT-01] socket 'config_updated' 핸들러 폐기 — ccu_games 만 처리하던 dead 경로.
    // 서버 emit 검색 결과 0 (crawler.py 의 config_updated 는 PySide6 Qt 시그널, socket.io 무관).

    socket.on('comments_update', (comments) => {
        if (typeof incrementUnreadBadge === 'function') incrementUnreadBadge('comments');
        if (tabDates['comments'] != null) return; // 과거 날짜 조회 중 소켓 업데이트 무시
        console.log('WebSocket: 댓글 업데이트 수신', comments.length, '건');
        if (Array.isArray(comments)) {
            allComments = comments;
            // [v6.7.24] Socket.IO push 경로 도착 플래시 — comments-tab.js 의 helper 호출 (REST 와 동일 추적).
            if (typeof window._trackCommentsArrival === 'function') {
                window._trackCommentsArrival(allComments);
            }
            if (typeof renderComments === 'function') {
                renderComments(allComments);
            }
        }
    });

    socket.on('toast', (data) => {
        if (typeof showToast === 'function') {
            showToast(data.message, data.is_error, data.duration);
        }
    });

    // [v6.7.731] admin_visit_update 는 서버가 "각 클라의 30초 heartbeat 를 받을 때마다" 전체 broadcast 한다.
    //   접속자 N명이면 30초당 N회가 쏟아져 admin pane 이 열린 클라는 매번 /admin/visit-logs(최대 2000행)를
    //   재요청했다(접속자 수에 비례한 증폭). 클라에서 throttle 로 묶어 접속자 수와 무관하게 최대 1회/주기로
    //   제한한다 - 실시간 접속 상태는 이 주기로 계속 갱신돼 UX 는 보존된다.
    let _visitLogLastFetch = 0;
    let _visitLogTrailingTimer = null;
    const _VISIT_LOG_MIN_INTERVAL = 20000;
    function _scheduleVisitLogRefetch() {
        if (!(activeApps.includes('admin') && typeof loadAdminVisitLogs === 'function')) return;
        const now = Date.now();
        const elapsed = now - _visitLogLastFetch;
        if (elapsed >= _VISIT_LOG_MIN_INTERVAL) {
            _visitLogLastFetch = now;
            loadAdminVisitLogs();
        } else if (_visitLogTrailingTimer === null) {
            // 주기 내 추가 이벤트는 묶어서 창 끝에 1회만 (최신 상태 반영 보장)
            _visitLogTrailingTimer = setTimeout(() => {
                _visitLogTrailingTimer = null;
                if (activeApps.includes('admin') && typeof loadAdminVisitLogs === 'function') {
                    _visitLogLastFetch = Date.now();
                    loadAdminVisitLogs();
                }
            }, _VISIT_LOG_MIN_INTERVAL - elapsed);
        }
    }
    socket.on('admin_visit_update', (data) => {
        console.log('WebSocket: 관리자 방문 로그 업데이트 수신');
        _scheduleVisitLogRefetch();
    });

    // [v6.6.279] admin 하트비트 — document.hidden 체크 + cleanup 가능하도록 id 저장.
    // 이전엔 setInterval 만 걸려 탭이 백그라운드에서도 30초마다 네트워크 송신 → 배터리/메모리 낭비.
    // 이제: 숨김 상태면 skip, 표시 상태로 돌아오면 즉시 한 번 송신, beforeunload 에서 clearInterval.
    if (window._imtAdminHeartbeatInterval) {
        clearInterval(window._imtAdminHeartbeatInterval);
    }
    window._imtAdminHeartbeatInterval = setInterval(() => {
        if (!document.hidden && socket.connected) {
            socket.emit('admin_heartbeat');
        }
    }, 30000);
    // 탭 가시성 전환 시 즉시 한 번 — 백그라운드 복귀 직후 상태 동기화
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden && socket.connected) {
            socket.emit('admin_heartbeat');
        }
    });
    // 페이지 언로드 시 타이머 정리
    window.addEventListener('beforeunload', () => {
        if (window._imtAdminHeartbeatInterval) {
            clearInterval(window._imtAdminHeartbeatInterval);
            window._imtAdminHeartbeatInterval = null;
        }
    });
}

// [v6.6.304] 서버 점검/재빌드 상황 가시화 배너 — disconnect 동안 상단 고정 노출, 재연결 시 자동 제거.
//   docker-compose up --build (서버 배포) 중 5~30초 다운타임 동안 글이 사라진 것처럼 보이는
//   UX 문제를 완화. 사용자가 "업데이트 중이구나" 라고 인지하게 되어 불안 해소.
function showServerOfflineBanner() {
    if (document.getElementById('server-offline-banner')) return;
    const banner = document.createElement('div');
    banner.id = 'server-offline-banner';
    banner.className = 'server-offline-banner';
    banner.innerHTML = `
        <i class="fas fa-cloud-upload-alt" aria-hidden="true"></i>
        <span>서버 연결이 잠시 끊겼어요. 앱 업데이트나 재시작 중일 수 있습니다. 자동으로 다시 연결합니다...</span>
    `;
    document.body.appendChild(banner);
}
function hideServerOfflineBanner() {
    const b = document.getElementById('server-offline-banner');
    if (b) b.remove();
}

// [v6.7.2] 풀스크린 업데이트 오버레이 — 비전공자 사용자가 깨진 화면을 "오류" 로 오해하지 않도록 차단.
//   상단 banner 만으론 카드/콘텐츠가 그대로 보여 시각 혼란 발생. 오버레이가 콘텐츠 위에 깔려 시선 가림 + 친근한 안내.
// [v6.7.3] 경과 카운터 + 단계별 메시지 + aria + 180초 fallback 수동 새로고침 버튼.
let _updateOverlayShowTimer = null;
let _overlayElapsedTimer = null;
let _overlayStartTs = 0;

function _tickOverlayElapsed() {
    const elap = document.getElementById('update-overlay-elapsed');
    const hint = document.getElementById('update-overlay-hint');
    const acts = document.getElementById('update-overlay-actions');
    if (!elap) return;
    const sec = Math.floor((Date.now() - _overlayStartTs) / 1000);
    const mm = String(Math.floor(sec / 60)).padStart(2, '0');
    const ss = String(sec % 60).padStart(2, '0');
    elap.textContent = mm + ':' + ss + ' 경과';
    if (sec >= 180) {
        if (hint) hint.textContent = '오래 걸리네요. 아래 버튼으로 직접 새로고침 시도해주세요.';
        if (acts) acts.style.display = 'flex';
    } else if (sec >= 60) {
        if (hint) hint.textContent = '평소보다 조금 오래 걸리네요. 1~2분 더 기다려주세요.';
    }
}

function showUpdateOverlay(message, title) {
    let ov = document.getElementById('update-overlay');
    if (!ov) {
        ov = document.createElement('div');
        ov.id = 'update-overlay';
        ov.className = 'update-overlay';
        // [v6.7.3] 접근성 — 스크린 리더에 변화 알림
        ov.setAttribute('role', 'status');
        ov.setAttribute('aria-live', 'polite');
        ov.setAttribute('aria-label', '서버 업데이트 안내');
        ov.innerHTML = ''
            + '<div class="update-overlay-card">'
            +   '<div class="update-overlay-spinner" aria-hidden="true"></div>'
            +   '<div class="update-overlay-title" id="update-overlay-title">서버 업데이트 중</div>'
            +   '<div class="update-overlay-msg" id="update-overlay-msg"></div>'
            +   '<div class="update-overlay-elapsed" id="update-overlay-elapsed">00:00 경과</div>'
            +   '<div class="update-overlay-hint" id="update-overlay-hint">자동으로 새로고침되니 잠시만 기다려주세요</div>'
            +   '<div class="update-overlay-actions" id="update-overlay-actions" style="display:none;">'
            +     '<button type="button" class="update-overlay-btn" onclick="location.reload();">지금 새로고침</button>'
            +   '</div>'
            + '</div>';
        document.body.appendChild(ov);
        _overlayStartTs = Date.now();
        if (_overlayElapsedTimer) clearInterval(_overlayElapsedTimer);
        _overlayElapsedTimer = setInterval(_tickOverlayElapsed, 1000);
        _tickOverlayElapsed();
        // [v6.7.3] PWA 배너 race 회피 글로벌 플래그 — pwa.js 가 이걸 보고 banner 띄우지 않음
        try { window.__updateOverlayActive = true; } catch (e) {}
    }
    // [v6.7.261] 타이틀 인자화 — reconnect_attempt(연결 끊김) 경로는 "서버 업데이트 중"이 아닌 재연결 의미로 표시.
    //   미지정 시 기본값 "서버 업데이트 중"(경로 A: server_version mismatch / 경로 B: stale SW = 진짜 배포).
    //   이미 떠 있던 오버레이도 갱신 → 연결끊김 오버레이가 (끊김→연결 후 mismatch 시) 배포 감지로 자연 업그레이드.
    const titleEl = document.getElementById('update-overlay-title');
    if (titleEl) titleEl.textContent = title || '서버 업데이트 중';
    const msgEl = document.getElementById('update-overlay-msg');
    if (msgEl && message) msgEl.textContent = message;
}
function hideUpdateOverlay() {
    const ov = document.getElementById('update-overlay');
    if (ov) ov.remove();
    if (_overlayElapsedTimer) {
        clearInterval(_overlayElapsedTimer);
        _overlayElapsedTimer = null;
    }
    try { window.__updateOverlayActive = false; } catch (e) {}
}
function scheduleUpdateOverlay(message, delayMs) {
    if (_updateOverlayShowTimer) return;
    _updateOverlayShowTimer = setTimeout(function () {
        _updateOverlayShowTimer = null;
        showUpdateOverlay(message);
    }, (delayMs == null ? 1500 : delayMs));
}
function cancelUpdateOverlaySchedule() {
    if (_updateOverlayShowTimer) {
        clearTimeout(_updateOverlayShowTimer);
        _updateOverlayShowTimer = null;
    }
}

// [v6.7.3] SW 캐시 nuke + unregister + reload — 글로벌 진입점 (boot.js stale 검사도 재사용).
//   reload 직전 sessionStorage 에 scrollY 저장 → 새 페이지 로드 후 boot.js 가 복원.
//   5초 fallback reload 로 nuke 가 멈춰도 강제 진행.
// [v6.7.5] idempotency lock — 30초 내 재호출 무시. boot.js stale 검사 + socket mismatch 가 동시에
//   호출하는 race 에서 두 번 reload + 두 번 SW reinstall(자산 burst 2배) 발생하던 위험 차단.
// [v6.7.449] 자동 새로고침 루프 차단기 — 페이지 로드를 넘어 지속되는 전역 안전망.
//   기존 가드는 전부 **한 페이지 안에서만** 유효했다: `_reloadScheduled`(socket) · `_reloading`(pwa) 는
//   변수라 reload 하면 사라지고, `imt_nuking_until`(30초) 도 nuke 경로 하나만 덮는다.
//   리로드 루프는 **각 로드에서 규칙상 정당한 판단**이 이어지며 생기므로 페이지-로컬 가드로는 못 끊는다.
//   그리고 루프에 걸리면 사용자는 앱을 아예 못 쓴다(화면이 계속 초기화). 그래서 로드를 가로질러 세는
//   카운터를 둔다 — 120초 안에 자동 reload 가 3회를 넘으면 **거부하고 콘솔에 원인 라벨을 남긴다**.
//   수동 새로고침(F5)·사용자 클릭 reload 는 이 경로를 안 타므로 영향 없다.
window.imtGuardAutoReload = function (reason) {
    try {
        const now = Date.now();
        const raw = sessionStorage.getItem('imt_autoreload_log');
        const hist = (raw ? JSON.parse(raw) : []).filter(t => (now - t) < 120000);
        if (hist.length >= 3) {
            console.warn('[auto-reload] 120초 내 ' + hist.length + '회 — 루프로 판단해 중단 (요청: ' + reason + '). '
                + '수동 새로고침으로 진행하세요. 원인 진단: 콘솔의 [stale-sw-cache] / [auto-reload] 로그 확인.');
            return false;
        }
        hist.push(now);
        sessionStorage.setItem('imt_autoreload_log', JSON.stringify(hist));
    } catch (e) { /* sessionStorage 불가 환경 — 가드 없이 통과(기존 동작) */ }
    return true;
};

window.nukeSwCacheAndReload = function (opts) {
    opts = opts || {};
    // [v6.7.449] 루프 차단기 — 이유 라벨은 호출처가 지정(없으면 'nuke').
    if (typeof window.imtGuardAutoReload === 'function'
        && !window.imtGuardAutoReload(opts.reason || 'nuke')) return;
    // [v6.7.5] race 가드 — 30초 내 이미 nuke 진행 중이면 skip.
    try {
        const lockUntil = parseInt(sessionStorage.getItem('imt_nuking_until') || '0', 10);
        if (Date.now() < lockUntil) {
            console.log('[nuke] idempotency lock active — skip');
            return;
        }
        sessionStorage.setItem('imt_nuking_until', String(Date.now() + 30000));
    } catch (e) {}
    const fallbackMs = opts.fallbackMs || 5000;
    const saveScroll = opts.saveScroll !== false;
    if (saveScroll) {
        try {
            sessionStorage.setItem('imt_pre_reload_scroll', String(window.scrollY || 0));
            sessionStorage.setItem('imt_pre_reload_ts', String(Date.now()));
        } catch (e) {}
    }
    let done = false;
    function _final() {
        if (done) return;
        done = true;
        try { location.reload(true); } catch (e) { location.reload(); }
    }
    setTimeout(_final, fallbackMs);
    try {
        const p1 = (typeof caches !== 'undefined' && caches && caches.keys)
            ? caches.keys().then(keys => Promise.all(keys.map(k => caches.delete(k)))).catch(() => {})
            : Promise.resolve();
        const p2 = ('serviceWorker' in navigator)
            ? navigator.serviceWorker.getRegistrations().then(regs => Promise.all(regs.map(r => r.unregister()))).catch(() => {})
            : Promise.resolve();
        Promise.all([p1, p2]).then(_final).catch(_final);
    } catch (e) { _final(); }
};

// [v6.7.335] 인앱 알림 배너 — OS 알림(Web Notification) 전면 대체.
//   폐기 이유: 여러 사람이 같이 쓰는 PC 에서 OS 트레이 알림은 관리자가 자리를 비운 사이 비관리자에게까지 노출되는
//   구조적 유출 경로. 대체 설계는 "admin 이 실제로 IME 화면을 보고 있는 순간에만" 페이지 내부에 렌더되는 DOM 배너 —
//   화면을 안 보면 배너도 없으므로 유출 경로가 원천 차단된다. Notification 권한 요청도 제거(권한 불요).
//   호출처: _notifyNewAiSummaries(AI 요약 항목별) / topbar-status.js pollGrafana(CCU 급감). 발화 게이트는 각 호출처가 담당.
//   opts: { title, body, targetApp(클릭 시 이동 탭 id), href(클릭 시 원문 새탭 — targetApp 보다 우선),
//           icon(FA6 클래스), iconText(텍스트 배지 — icon 보다 우선, 예: 'AI'),
//           variant('info'|'alert'), wide(bool — 넓은 본문형), timeout(ms), sound(bool) }
function showInAppBanner(opts) {
    opts = opts || {};
    var targetApp = opts.targetApp || null;
    var timeout = opts.timeout || 7000;

    // [v6.7.427] 보관소 적재는 서버 원천(notif_store.py)이 전담 — 배너는 전이 표시만. 종이 열려 있으면
    //   방금 서버 적재분이 반영되도록 최신 목록만 refetch (닫혀 있으면 다음에 열 때 어차피 fetch).
    if (window.imtNotifInbox && typeof window.imtNotifInbox.refresh === 'function') {
        try { window.imtNotifInbox.refresh(); } catch (_) {}
    }

    var host = document.getElementById('inapp-banner-host');
    if (!host) {
        host = document.createElement('div');
        host.id = 'inapp-banner-host';
        host.className = 'inapp-banner-host';
        document.body.appendChild(host);
    }
    var el = document.createElement('div');
    el.className = 'inapp-banner inapp-banner--' + (opts.variant || 'info') + (opts.wide ? ' inapp-banner--wide' : '');
    el.setAttribute('role', 'status');
    el.setAttribute('aria-live', 'polite');
    // [v6.7.340] iconText 우선 — FA 아이콘 대신 텍스트 배지(예: 'AI'). 값은 아래 textContent 로 주입(HTML 미해석).
    el.innerHTML = ''
        + (opts.iconText
            ? '<span class="inapp-banner__icon inapp-banner__icon--text" aria-hidden="true"></span>'
            : '<i class="fas ' + (opts.icon || 'fa-bell') + ' inapp-banner__icon" aria-hidden="true"></i>')
        + '<div class="inapp-banner__body">'
        +   '<div class="inapp-banner__title"></div>'
        +   '<div class="inapp-banner__text"></div>'
        + '</div>'
        + '<button type="button" class="inapp-banner__close" aria-label="닫기">&times;</button>';
    // title/body 는 서버 카운트·게임명 등 내부값이나 방어적으로 textContent 주입(HTML 미해석).
    el.querySelector('.inapp-banner__title').textContent = opts.title || '';
    el.querySelector('.inapp-banner__text').textContent = opts.body || '';
    if (opts.iconText) el.querySelector('.inapp-banner__icon--text').textContent = String(opts.iconText);

    // [v6.7.347] 구조화 배지 행 - 보관소(main/notif-inbox.js)와 동일 배지 세트를 배너 제목 위에도 표시.
    //   칩 행/배지 클래스는 v6.7.346 보관소 CSS(theme-variants.css) 재사용, 라벨 맵은 notif-inbox 미러.
    //   구조화 필드 보유 시 제목은 bareTitle(접두 없는 클린 제목)로 교체 - 접두 정보를 배지가 대체.
    (function () {
        // [v6.7.451] anomaly 추가 — 보관소(main/notif-inbox.js:23)엔 v6.7.438 부터 있었는데 배너 쪽 맵에만
        //   없어 이상감지 배너에 '이상' 배지가 안 붙는 비대칭이었다(두 맵은 미러 관계 - 한쪽만 고치지 말 것).
        var KIND_BADGE = { ai: 'AI 요약', notice: '공지', ccu: 'CCU', anomaly: '이상' };
        // [v6.7.741] 워처 공지 전용 배지(출처 POE · 변경유형 evt) 삭제 - 그 배너를 띄우던 topbar-status.js 워처 몫을 지웠다.
        var SRC_LABEL = { NAVER: '네이버', DAUM: '다음', OFFICIAL_WEB: '공식', DC: 'DC' };
        var srcKey = String(opts.src || '').toUpperCase();
        var game = String(opts.game || '');
        if (!((srcKey && SRC_LABEL[srcKey]) || game)) return;
        var chips = document.createElement('div');
        chips.className = 'topbar-notif__chips';
        if (opts.kind && KIND_BADGE[opts.kind]) {
            var kindEl = document.createElement('span');
            kindEl.className = 'topbar-notif__kind topbar-notif__kind--' + opts.kind;
            kindEl.textContent = KIND_BADGE[opts.kind];
            chips.appendChild(kindEl);
        }
        if (srcKey && SRC_LABEL[srcKey]) {
            var srcEl = document.createElement('span');
            srcEl.className = 'feed-card-source feed-card-source--' + srcKey.toLowerCase();
            srcEl.textContent = SRC_LABEL[srcKey];
            chips.appendChild(srcEl);
        }
        if (game) {
            var gameEl = document.createElement('span');
            gameEl.className = 'topbar-notif__game';
            gameEl.textContent = game;
            try {
                if (typeof window.getGameColor === 'function') gameEl.style.color = window.getGameColor(game);
            } catch (_) {}
            chips.appendChild(gameEl);
        }
        var bodyWrap = el.querySelector('.inapp-banner__body');
        bodyWrap.insertBefore(chips, bodyWrap.firstChild);
        if (opts.bareTitle) el.querySelector('.inapp-banner__title').textContent = String(opts.bareTitle);
    })();

    var _dismissed = false;
    function dismiss() {
        if (_dismissed) return;
        _dismissed = true;
        el.classList.add('inapp-banner--leaving');
        setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 260);
    }
    var _timer = setTimeout(dismiss, timeout);

    el.addEventListener('click', function (e) {
        clearTimeout(_timer);
        // 닫기 버튼은 이동 없이 소멸만.
        if (!(e.target && e.target.closest && e.target.closest('.inapp-banner__close'))) {
            // [v6.7.383] kind 라우팅 — 원천 탭으로 이동(CCU=그라파나. [v6.7.741] 공지→알림 탭 라우팅은 탭과 함께 삭제).
            //   권한 없는 탭은 열지 않고 IME 유지(_routeNotifTo 내부 _canAccessApp degrade). 소비되면 기존 원문 이동 생략.
            if (typeof window._routeNotifTo === 'function'
                && window._routeNotifTo({ kind: opts.kind, href: opts.href, src: opts.src })) {
                // routed 또는 degrade — 별도 동작 없음 (아래 dismiss 로 배너만 소멸)
            } else if (opts.href) {
                // [v6.7.338] 미지정 kind 폴백 — href 우선(원문 새탭).
                try {
                    if (typeof openArticle === 'function') openArticle(opts.href);
                    else window.open(opts.href, '_blank', 'noopener');
                } catch (_) {}
            } else if (targetApp && typeof toggleAppPane === 'function') {
                // 대상 탭이 아직 안 열려 있을 때만 전환 — 이미 활성 탭에 toggleAppPane 을 부르면 오히려 닫힘(유틸리티 토글 특성).
                try {
                    var _active = (typeof activeApps !== 'undefined' && Array.isArray(activeApps)) ? activeApps : [];
                    if (!_active.includes(targetApp)) toggleAppPane(targetApp);
                } catch (_) {}
            }
        }
        dismiss();
    });

    host.appendChild(el);
    if (opts.sound && typeof playNotificationSound === 'function') playNotificationSound();
    return el;
}
window.showInAppBanner = showInAppBanner;   // 타 스크립트(topbar-status.js IIFE)에서 호출

/* [v6.7.338] AI 요약 항목별 인앱 배너 — 구 건수 배너(triggerBrowserNotification, v6.7.335) 대체.
   왜 diff 기반인가: 소켓 new_count 는 "이번 사이클 저장 건수"라 재배포 직후 백로그(그동안 미요약이던
   과거 글) 소화분까지 세어 "추가된 게 없는데 17건" 오인을 유발. 여기서는 admin 가드 API(/api/ai-summary)를
   직접 fetch 해 직전 관측(seen set)과 diff — 실제 새로 나타난 항목만, 항목별 상세(게임/요약/구분/게시판)로
   배너를 띄운다. 같은 항목이 페이지 세션 내 두 번 배너되는 일 없음(카카오워크 항목별 알림과 동형).
   PATTERN-37 준수: 소켓 broadcast 는 count 신호만, 본문은 admin API 로만 수신. */
let _aiBannerSeen = null;          // null = 기준선 미수립. 첫 fetch 는 배너 없이 seed 만.
// [v6.7.380] 옛 _AI_BANNER_MAX(상한 4 + "N건 더" 집계) 폐기 - 신규 요약은 전건 개별 표시(사용자 결정).

async function _notifyNewAiSummaries() {
    if (window._role !== 'admin' && window._role !== 'manager') return;  // [v6.7.386] admin+manager (AI 배너 매니저 개방)
    const keyOf = (r) => (r && r.link) || (((r && r.title) || '') + '|' + ((r && r.timestamp) || ''));
    // [v6.7.738] 기준선(seed) 수립은 부트 프리페치(boot.js)를 공유한다 - 같은 /api/ai-summary 를 seed 가
    //   또 받던 중복(부트 1 + seed 2회 = 3회) 제거. **별칭 _bootPrefetch.aiSummarySeed 를 읽는다**: 탭 로더
    //   (ai-summary-tab.js)는 .aiSummary 를 소비하며 delete 하는데, 기본 구성(AI 요약 pane 열림)에선 그
    //   delete 가 init()->updateDashboard 동기 경로에서 이 4s seed 보다 먼저 일어나, seed 가 .aiSummary 를
    //   못 보고 자체 fetch 로 폴백하던 버그(요청 3->2 에 그침)가 있었다 - 별칭은 탭 로더가 지우지 않으므로
    //   늘 보이고 같은 Promise 라 추가 fetch 0. 신선(<30s, 서버 응답 캐시 TTL 과 정렬)할 때만 재사용. diff
    //   모드(_aiBannerSeen 이미 수립됨 = 소켓 ai_analysis_update 로 재호출)는 페이지 로드 스냅샷으로 신규를
    //   못 담으므로 이 블록을 건너뛰고 아래 정상 fetch 를 탄다. 프리페치가 없거나(익명·role 미판정) 실패면
    //   역시 아래로 폴백.
    if (_aiBannerSeen === null && window._bootPrefetch && window._bootPrefetch.aiSummarySeed) {
        try {
            const got = await window._bootPrefetch.aiSummarySeed;
            if (got && Array.isArray(got.data) && (Date.now() - got.ts) < 30000) {
                _aiBannerSeen = new Set(got.data.map(keyOf));   // 기준선만 수립(배너 없음) - 추가 fetch 0
                return;
            }
        } catch (_) { /* 프리페치 실패 → 아래 정상 fetch 폴백 */ }
    }
    let rows;
    try {
        const res = await fetch('/api/ai-summary', { credentials: 'same-origin' });
        if (!res.ok) return;
        rows = await res.json();
    } catch (_) { return; }
    if (!Array.isArray(rows)) return;
    // [v6.7.350] 같은 응답을 AI 요약 탭에 hydrate — 탭의 디바운스 로더가 신선 창(30s) 가드로 재fetch 를
    //   생략하게 되어 push 당 /api/ai-summary 2회 → 1회 (제안서 B5). 리스너의 디바운스 호출은 폴백으로 유지.
    try { if (typeof window.aiSummaryHydrate === 'function') window.aiSummaryHydrate(rows); } catch (_) {}
    if (_aiBannerSeen === null) {
        _aiBannerSeen = new Set(rows.map(keyOf));   // 페이지 로드 시점 기존분 = 기준선(배너 대상 아님)
        return;
    }
    const fresh = rows.filter((r) => !_aiBannerSeen.has(keyOf(r)));
    if (!fresh.length) return;
    fresh.forEach((r) => _aiBannerSeen.add(keyOf(r)));
    if (_aiBannerSeen.size > 3000) _aiBannerSeen = new Set(rows.map(keyOf));   // 장수명 세션 메모리 가드

    // [v6.7.347] 게임 visibility 필터 - AI 요약 탭 [필터 1] 미러(_localSettingsData.visibility,
    //   사용자가 끈 게임 visibility[g]===false 만 제외, 미설정은 노출). 사용자 보고: 오딘을 필터에서
    //   껐는데 오딘 AI 배너가 계속 옴. seen 마킹은 위에서 전건 선행 - 나중에 게임을 다시 켜도
    //   숨김 기간 분이 소급 폭주하지 않는다(그 분량은 AI 요약 탭에서 조회 가능).
    let visSettings = {};
    try {
        if (typeof _localSettingsData !== 'undefined' && _localSettingsData && _localSettingsData.visibility) {
            visSettings = _localSettingsData.visibility;
        }
    } catch (_) {}
    const visible = fresh.filter((r) => {
        const g = (r && (r.game || r.game_name)) || '';
        return !(g && visSettings[g] === false);
    });
    if (!visible.length) return;

    // 최신 글 우선 표시 (timestamp ISO 문자열 내림차순 — 파서 불요)
    visible.sort((a, b) => String((b && b.timestamp) || '').localeCompare(String((a && a.timestamp) || '')));
    const srcMap = { 'DAUM': '다음', 'OFFICIAL_WEB': '공식', 'NAVER': '네이버', 'DC': 'DC' };
    const toOpts = (r, withSound) => {
        const game = (r && r.game) || '';
        const srcKey = String((r && (r.source_community || r.source)) || '').toUpperCase();
        const src = srcMap[srcKey] || '';
        const board = String((r && r.board_name) || '').trim();
        const t = (typeof formatTime === 'function') ? (formatTime((r && (r.timestamp || r.date)) || '') || '') : '';
        const meta = [(src && board) ? (src + ' · ' + board) : (src || board), t].filter(Boolean).join(' · ');
        const articleTitle = String((r && r.title) || '').trim();
        return {
            title: (game ? '[' + game + '] ' : '') + String((r && r.summary) || '').trim(),
            // [v6.7.347] 본문 순서 스왑 - 글 제목 먼저, 메타(출처·게시판·시각)는 그 아래 (사용자 피드백:
            //   보관소 배지 행과 메타 줄 사이에 제목이 오는 게 읽기 순서상 자연스러움. 피드 카드 동형).
            body: [articleTitle, meta].filter(Boolean).join('\n'),
            href: (r && r.link) || '',
            iconText: 'AI',
            kind: 'ai',          // [v6.7.345] 알림 보관소 유형 배지 (AI 요약)
            src: srcKey,         // [v6.7.346] 보관소 출처 배지 (네이버/다음/공식/DC - feed-card-source 재사용)
            game: game,          // [v6.7.346] 보관소 게임명 색 표시 (getGameColor)
            bareTitle: String((r && r.summary) || '').trim(),   // [v6.7.346] 보관소용 클린 제목 (게임 접두는 배지 행이 대체)
            variant: (String((r && r.severity) || '').toLowerCase() === 'critical') ? 'alert' : 'info',
            wide: true,
            timeout: 10000,
            sound: withSound,    // 묶음당 1회만 (배너 수만큼 중복 재생 방지)
        };
    };
    // [v6.7.380] 상한/집계 폐기 (사용자: "다섯 개 쏟아지면 그만큼 급한 거지, 'N건 더'는 별로").
    //   신규 요약 전건을 개별 배너로 - showInAppBanner 가 각 배너를 보관소에도 적재(중복 적재 없음).
    //   소리는 묶음당 1회(i===0). '분석 제외'·빈 요약은 /api/ai-summary 가 서버에서 이미 제외(analysis.py) - 배너엔 실 요약만.
    visible.forEach((r, i) => showInAppBanner(toOpts(r, i === 0)));
}

// 기준선 선수립 — 소켓 첫 이벤트가 오기 전에 seed 를 끝내 첫 신규분부터 배너 가능하게.
//   admin 판정(window._isAdmin, checkAdminStatus 비동기)이 아직이면 함수가 조용히 반환하므로 2회 시도.
setTimeout(_notifyNewAiSummaries, 4000);
// [v6.7.738] 15s 재시도는 4s seed 가 실패했을 때만 발화한다(role 미판정/프리페치·fetch 실패로
//   _aiBannerSeen 미수립). 성공 시 _aiBannerSeen 이 수립돼 이 콜백이 /api/ai-summary 를 또 받지 않는다.
setTimeout(function () { if (_aiBannerSeen === null) _notifyNewAiSummaries(); }, 15000);

/* [v6.7.451] 이상감지 인앱 배너 — AI 요약 배너(_notifyNewAiSummaries)와 **같은 정책**으로 배선(사용자 지시).
   그동안 이상감지는 종(보관소)·폰 푸시·카카오워크만 있고 **브라우저 배너가 없었다** — 화면을 보고 있는데도
   급증을 즉시 못 봤다.

   알림 3경로의 필터 정책(사용자 확정 2026-07-28):
     · 카카오워크  = **전부** (서버→카카오 직송, 필터 개념 없음. `bg/anomaly.py` 무변경)
     · 브라우저 배너 = **visibility 필터 적용** (숨긴 게임은 배너 안 뜸) ← 이 함수
     · 종(보관소)  = 서버 원천 유지(min_role='admin'). 배너는 v6.7.427 부터 적재하지 않으므로 중복 없음.

   admin 전용 — 이상감지는 표면 전체가 admin 이다(사용자 재확인). AI 배너는 manager 도 받지만(v6.7.386)
   여기는 `_role === 'admin'` 만. API(`/api/anomaly/recent`)도 `_require_admin` 이라 이중 방어.

   seed 규율: 첫 fetch 는 배너 없이 기준선만 만든다. **v6.7.443 백필로 과거 38건이 들어있어**, seed 를
   빠뜨리면 페이지 열자마자 38개 배너가 쏟아진다. seen 마킹은 visibility 필터 **앞**에서 전건 수행 —
   숨긴 게임을 나중에 다시 켜도 그 기간분이 소급 폭주하지 않는다(AI 배너 v6.7.347 과 같은 규율). */
let _anomBannerSeen = null;          // null = 기준선 미수립

async function _notifyNewAnomalies() {
    if (window._role !== 'admin') return;
    let rows;
    try {
        const res = await fetch('/api/anomaly/recent?limit=30', { credentials: 'same-origin' });
        if (!res.ok) return;
        const j = await res.json();
        rows = j && j.events;
    } catch (_) { return; }
    if (!Array.isArray(rows)) return;
    const keyOf = (e) => [(e && e.bucket) || '', (e && e.game) || '', (e && e.signal) || ''].join('|');
    if (_anomBannerSeen === null) {
        _anomBannerSeen = new Set(rows.map(keyOf));
        return;
    }
    const fresh = rows.filter((e) => !_anomBannerSeen.has(keyOf(e)));
    if (!fresh.length) return;
    fresh.forEach((e) => _anomBannerSeen.add(keyOf(e)));   // 필터 앞에서 전건 마킹 (소급 폭주 차단)
    if (_anomBannerSeen.size > 3000) _anomBannerSeen = new Set(rows.map(keyOf));

    let visSettings = {};
    try {
        if (typeof _localSettingsData !== 'undefined' && _localSettingsData && _localSettingsData.visibility) {
            visSettings = _localSettingsData.visibility;
        }
    } catch (_) {}
    const visible = fresh.filter((e) => {
        const g = (e && e.game) || '';
        return !(g && visSettings[g] === false);
    });
    if (!visible.length) return;

    // 최신 우선 (bucket 은 'YYYY-MM-DDTHH' 라 문자열 내림차순 = 시간 내림차순)
    visible.sort((a, b) => String((b && b.bucket) || '').localeCompare(String((a && a.bucket) || '')));
    visible.forEach((e, i) => {
        const sig = (e.signal === 'volume') ? '게시량 급증' : '심각 급증';
        const kind = (e.kind === 'escalate') ? ' (확대 중)' : '';
        const hh = String(e.bucket || '').slice(11, 13);
        const ratio = (typeof e.ratio === 'number') ? e.ratio.toFixed(1) : e.ratio;
        const base = (typeof e.baseline === 'number') ? Math.round(e.baseline) : e.baseline;
        showInAppBanner({
            title: '[' + (e.game || '') + '] ' + sig + kind,
            bareTitle: sig + kind,          // 배지 행이 게임명을 대체 (AI 배너 동형)
            body: hh + '시 ' + e.value + '건 · 평시 ' + base + ' 대비 ' + ratio + '배',
            href: '/?tab=anomaly',
            iconText: '이상',
            kind: 'anomaly',
            game: e.game || '',
            variant: 'alert',               // 급증은 항상 경보 톤 (색 예산: 이상에만 앰버/레드)
            wide: true,
            timeout: 12000,
            sound: (i === 0),               // 묶음당 1회만
        });
    });
}

// 기준선 선수립 — AI 배너와 동일 2회 시도(admin 판정이 비동기라 첫 시도가 조용히 반환될 수 있다).
setTimeout(_notifyNewAnomalies, 5000);
setTimeout(_notifyNewAnomalies, 16000);

function playNotificationSound() {
    try {
        const AudioContext = window.AudioContext || window.webkitAudioContext;
        if (!AudioContext) return;
        
        const ctx = new AudioContext();
        const osc = ctx.createOscillator();
        const gainNode = ctx.createGain();
        
        osc.connect(gainNode);
        gainNode.connect(ctx.destination);
        
        // 맑고 경쾌한 알림음 (도-미) 패턴
        osc.type = 'sine';
        osc.frequency.setValueAtTime(523.25, ctx.currentTime); // C5
        osc.frequency.setValueAtTime(659.25, ctx.currentTime + 0.1); // E5
        
        gainNode.gain.setValueAtTime(0.1, ctx.currentTime);
        gainNode.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.3);
        
        osc.start();
        osc.stop(ctx.currentTime + 0.3);
        osc.onended = () => ctx.close();
    } catch(e) {
        console.error("오디오 재생 실패:", e);
    }
}