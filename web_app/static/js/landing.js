// static/js/landing.js
// [v6.6.294] IME 랜딩 페이지 — 첫 방문 온보딩 + IME 로고 클릭 재진입
//
// 역할:
//  - localStorage `imt_landing_completed` 플래그 없으면 오버레이 강제 표시
//  - 구글시트 Config_Games의 Active 게임만 GET /api/games 로 로드
//  - 체크박스로 모니터링할 게임을 고르면 기존 `imt_display_settings.visibility`
//    스키마에 덮어써 기존 필터링 모달과 호환
//  - 테마 5종 중 택1 → 기존 window.applyTheme() 재사용
//  - 시작 시 선택 게임 수에 따라 `imt_dashboard_config` 탭 배치 자동 결정
//
// 재사용 의존성:
//   window.getGameColor(name)      — game-color.js
//   window.applyTheme(themeKey)    — main.js
//   activeApps, updateDashboard    — tab-manager.js (전역 let/function)
//   _safeFetchDisplaySettings()    — ui.js (서버 fetch fallback)

(function () {
    'use strict';

    // ── 상수 ──────────────────────────────────────────────
    var LANDING_FLAG_KEY = 'imt_landing_completed';
    var DISPLAY_SETTINGS_KEY = 'imt_display_settings';
    var DASHBOARD_CONFIG_KEY = 'imt_dashboard_config';
    var LAST_CORE_CONFIG_KEY = 'imt_last_core_config';
    var THEME_KEY = 'imt_theme';

    // ── 상태 ──────────────────────────────────────────────
    var _cachedGames = [];               // [{name, has_cafe, has_dc} | string]
    var _selectedGames = new Set();      // 체크된 게임명 집합
    var _selectedTheme = null;           // 선택된 테마 키 (null → 기존 유지)
    var _isOpen = false;

    // ── localStorage 헬퍼 ──────────────────────────────────
    function _safeGet(key) {
        try { return localStorage.getItem(key); } catch (e) { return null; }
    }
    function _safeSet(key, value) {
        try { localStorage.setItem(key, value); } catch (e) { /* private mode */ }
    }
    function _readDisplaySettings() {
        try {
            var raw = _safeGet(DISPLAY_SETTINGS_KEY);
            if (!raw) return {};
            var parsed = JSON.parse(raw);
            return (parsed && typeof parsed === 'object') ? parsed : {};
        } catch (e) { return {}; }
    }
    function _writeDisplaySettings(obj) {
        _safeSet(DISPLAY_SETTINGS_KEY, JSON.stringify(obj));
    }

    // ── 판정 ──────────────────────────────────────────────
    function _shouldShowLandingOnBoot() {
        // L-04 마이그레이션: 기존 사용자는 랜딩 건너뜀.
        // [v6.6.304] 가드 범위 확장 — 필터링 모달 한 번도 안 연 이전 사용자도 갑자기
        // 랜딩을 보고 "글이 사라졌다" 고 느끼지 않도록, IME 사용 흔적이 있는 모든 키를 검사.
        var hasFlag = _safeGet(LANDING_FLAG_KEY) === 'true';
        if (hasFlag) return false;
        var legacyKeys = [
            'imt_display_settings',        // 필터링 모달 사용 흔적
            'imt_dashboard_config',         // 탭 배치 저장 (거의 모든 사용자)
            'imt_last_core_config',         // 이전 코어 탭 구성
            'imt_theme',                    // 테마 선택 흔적
            'acknowledgedArticlesTotal',    // 전체 탭 알림 확인 기록
            'acknowledgedArticlesGames',    // 게임 탭 알림 확인 기록
            'gameOrder',                    // 게임 드래그 순서 커스텀
            'foldedGames'                   // 게임 섹션 접기 상태
        ];
        for (var i = 0; i < legacyKeys.length; i++) {
            if (_safeGet(legacyKeys[i])) {
                _safeSet(LANDING_FLAG_KEY, 'true');
                return false;
            }
        }
        return true;
    }

    // ── 데이터 로딩 ────────────────────────────────────────
    function _loadActiveGames() {
        // [v6.6.316] index.html head 의 인라인 프리로드 (`window.__landingGamesPromise`) 가 있으면 재사용.
        //   landing.js 는 body 끝에서 실행되므로 그 시점엔 이미 응답이 도착해 즉시 렌더 가능.
        // /api/games 는 crawler.get_games() 를 반환하며 cafe_list + dc_galleries 중 하나라도
        // 설정된 게임만 반환하므로 이미 Active 필터링이 되어 있다.
        var basePromise = (window.__landingGamesPromise && typeof window.__landingGamesPromise.then === 'function')
            ? window.__landingGamesPromise
            : fetch('/api/games', { credentials: 'same-origin' }).then(function (r) {
                if (!r.ok) throw new Error('HTTP ' + r.status);
                return r.json();
            });
        return basePromise
            .then(function (data) {
                // 서버가 ["오딘", "아키에이지 워", ...] 문자열 배열로 반환
                var list = Array.isArray(data) ? data : [];
                return list.map(function (name) {
                    return (typeof name === 'string') ? { name: name } : name;
                }).filter(function (g) { return g && g.name; });
            })
            .catch(function () { return []; });
    }

    // ── 렌더 ──────────────────────────────────────────────
    function _renderGameGrid(games) {
        var grid = document.getElementById('landingGameGrid');
        if (!grid) return;
        grid.innerHTML = '';
        if (!games.length) {
            var empty = document.createElement('div');
            empty.className = 'landing-empty';
            empty.textContent = '운영 중인 게임 목록을 불러오지 못했습니다. 그대로 시작하면 전체 탭만 열립니다.';
            grid.appendChild(empty);
            return;
        }
        games.forEach(function (g) {
            var card = document.createElement('label');
            card.className = 'landing-game-card';
            var color = (typeof window.getGameColor === 'function') ? window.getGameColor(g.name) : '#3182f6';
            card.style.setProperty('--landing-accent', color);

            var cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.className = 'landing-game-checkbox';
            cb.value = g.name;
            cb.checked = _selectedGames.has(g.name);
            cb.addEventListener('change', function () {
                if (cb.checked) _selectedGames.add(g.name);
                else _selectedGames.delete(g.name);
                card.classList.toggle('selected', cb.checked);
                _updateSelectedCount();
            });

            var nameEl = document.createElement('span');
            nameEl.className = 'landing-game-name';
            nameEl.textContent = g.name;

            var dot = document.createElement('span');
            dot.className = 'landing-game-dot';
            dot.style.background = color;

            card.classList.toggle('selected', cb.checked);
            card.appendChild(cb);
            card.appendChild(dot);
            card.appendChild(nameEl);
            grid.appendChild(card);
        });
    }

    function _updateSelectedCount() {
        var el = document.getElementById('landingSelectedCount');
        if (el) el.textContent = _selectedGames.size + '개 선택';
    }

    function _renderThemeRow() {
        var row = document.getElementById('landingThemeRow');
        if (!row) return;
        var current = _selectedTheme || document.body.getAttribute('data-theme') || 'dark';
        row.querySelectorAll('.landing-theme').forEach(function (btn) {
            var key = btn.getAttribute('data-theme');
            btn.classList.toggle('active', key === current);
        });
    }

    // ── 초기 체크 상태 ────────────────────────────────────
    function _syncSelectedFromSettings() {
        var settings = _readDisplaySettings();
        var visibility = (settings && settings.visibility) || {};
        _selectedGames = new Set();
        // 저장된 visibility 가 true 인 게임만 초기 체크 (기존 사용자 재진입 시 현 상태 복원)
        Object.keys(visibility).forEach(function (name) {
            if (visibility[name] === true) _selectedGames.add(name);
        });
        // 재진입이 아닌 첫 방문이면 visibility 자체가 비어있을 수 있음 → 전부 체크된 기본 상태로 시작
        if (Object.keys(visibility).length === 0 && _cachedGames.length) {
            _cachedGames.forEach(function (g) { _selectedGames.add(g.name); });
        }
    }

    // ── 시작 (commit) ─────────────────────────────────────
    // [v6.6.300] async 로 전환 — _localSettingsData 메모리 객체를 서버 fetch 로 갱신해야
    //   탭 렌더 함수들이 최신 visibility 를 읽을 수 있음. localStorage 갱신만으론 부족.
    async function _commitAndEnterDashboard() {
        // 1. imt_display_settings.visibility / commentVisibility 갱신
        var settings = _readDisplaySettings();
        settings.visibility = settings.visibility || {};
        settings.commentVisibility = settings.commentVisibility || {};
        _cachedGames.forEach(function (g) {
            var on = _selectedGames.has(g.name);
            settings.visibility[g.name] = on;
            settings.commentVisibility[g.name] = on;
        });
        _writeDisplaySettings(settings);

        // 2. ui.js 모듈 전역 `_localSettingsData` 동기화.
        //    탭 렌더 함수 (total-tab:39, games-tab:69, community-tab:50, ai-tab:56, comments-tab:35) 는
        //    localStorage 를 직접 읽지 않고 `_localSettingsData.visibility/commentVisibility` 만 참조.
        //    _safeFetchDisplaySettings() 를 호출하면 서버(games 목록 포함) + localStorage(방금 저장한 visibility)
        //    를 머지해 올바른 최신 상태를 만든다. applyLocalSettings() (ui.js:430) 와 동일한 경로.
        try {
            if (typeof _safeFetchDisplaySettings === 'function') {
                _localSettingsData = await _safeFetchDisplaySettings();
            } else if (typeof _localSettingsData !== 'undefined' && _localSettingsData) {
                // fallback — 서버 fetch 유틸 없으면 최소한 visibility 만이라도 직접 밀어 넣음
                _localSettingsData.visibility = settings.visibility;
                _localSettingsData.commentVisibility = settings.commentVisibility;
            }
        } catch (e) {
            if (window.console && console.warn) console.warn('[Landing] _localSettingsData 갱신 실패:', e);
        }

        // 3. 테마 적용 (선택 안 했으면 현재 테마 유지)
        if (_selectedTheme && typeof window.applyTheme === 'function') {
            window.applyTheme(_selectedTheme);
        }

        // 4. 탭 배치: 통합 모니터링 환경 - 피드 + 댓글 (대시보드를 열 수 있는 role 이면 + 대시보드 - v6.7.713)
        //   [v6.6.317] 기본 탭을 games → comments 로 변경.
        //   [v6.6.431] total → feed (피드 합성 pane). 관리자(window._isAdmin) 면 situation(대시보드) 자동 추가.
        //   [v6.7.182] IA-01 — legacy 'dashboard' → 'situation' 교체. #dashboard-tab 은 v6.6.494 에 DOM 제거됨 →
        //     'dashboard' commit 시 admin first-run 에 대시보드 pane 이 통째로 안 떴음(render.js "Tab not found"). 'situation' 이 정식 id.
        //   [v6.7.677] **은닉 탭(app.py `HIDDEN_APP_IDS`)을 여기서도 거른다.** v6.7.638 이 막은 진입로는
        //     사이드바·하단탭바·설정모달·저장된 activeApps 복원 4곳이었는데, 이 랜딩 commit 은 localStorage 와
        //     전역 activeApps 를 **직접** 쓰기 때문에 그 문지기(toggleAppPane)를 한 번도 안 지난다 → 첫 방문자에게만
        //     댓글 pane 이 열렸다(새로고침하면 state.js 필터가 지워서 재현이 1회성이라 더 안 보였다). PATTERN-56/72.
        var hasSelection = _selectedGames.size > 0;
        // [v6.7.713] 대시보드 기본 배치 = 대시보드를 열 수 있는 role 전부(admin·manager - 사용자 2026-10-01 "매니저에게도 공유").
        //   판정은 ui-control.js `_canAccessApp`(MANAGER_EXTRA_APP_IDS 정본)으로 - role 목록을 여기 다시 적지 않는다.
        //   그 함수가 아직 없으면(로드 순서 이상) 옛 판정(admin)으로 떨어진다.
        // [v6.7.725] 대시보드 대신 AI 요약을 기본으로(사용자 지시 2026-10-11) - state.js DEFAULT_APP_IDS 와 같은 구성.
        var showAi = (typeof window._canAccessApp === 'function') ? !!window._canAccessApp('ai-summary') : !!window._isAdmin;
        var nextApps;
        if (hasSelection) {
            nextApps = showAi ? ['feed', 'comments', 'ai-summary'] : ['feed', 'comments'];
        } else {
            nextApps = showAi ? ['feed', 'ai-summary'] : ['feed'];
        }
        // 목록을 여기 다시 적지 않는다 - 서버가 주입한 값(index.html `window.IMT_HIDDEN_APPS`)만 읽는다.
        //   되돌리기는 여전히 app.py 한 곳에서 id 를 빼는 것 하나다.
        var hiddenApps = Array.isArray(window.IMT_HIDDEN_APPS) ? window.IMT_HIDDEN_APPS : [];
        nextApps = nextApps.filter(function (id) { return hiddenApps.indexOf(id) === -1; });
        _safeSet(DASHBOARD_CONFIG_KEY, JSON.stringify(nextApps));
        _safeSet(LAST_CORE_CONFIG_KEY, JSON.stringify(nextApps));
        // tab-manager.js 의 전역 activeApps/updateDashboard 를 직접 갱신해 즉시 반영
        try {
            if (typeof activeApps !== 'undefined') {
                activeApps.length = 0;
                nextApps.forEach(function (id) { activeApps.push(id); });
            }
            if (typeof lastCoreApps !== 'undefined') {
                lastCoreApps.length = 0;
                nextApps.forEach(function (id) { lastCoreApps.push(id); });
            }
            // [v6.7.731] '시작'은 activeApps 를 직접 교체하고 updateDashboard 만 불러 toggleAppPane/closeAppPane 의
            //   pane 정리를 거치지 않았다 - 직전 activeApps 에 ops/admin/situation 등 폴링 탭이 있었으면 그 폴링이
            //   새 레이아웃에서 계속 돌던 누수. 교체 후 한 번 정리한다(멱등).
            if (typeof _runPaneCleanups === 'function') _runPaneCleanups();
            if (typeof updateDashboard === 'function') updateDashboard();
        } catch (e) { /* 최초 로드 전이면 다음 페이지 로드 시 localStorage 에서 복원 */ }

        // 5. 플래그 저장 후 오버레이 닫기
        _safeSet(LANDING_FLAG_KEY, 'true');
        _hideOverlay();

        // 6. 기존 applyLocalSettings() (ui.js:420) 과 동일한 재렌더 함수 세트를 호출해
        //    필터링 모달의 "즉시 적용" 버튼과 동일한 효과를 낸다. 인자는 null (전체 재로드).
        try {
            if (typeof loadGamesSections === 'function') loadGamesSections(null);
            if (typeof loadTotalArticles === 'function') loadTotalArticles(null);
            if (typeof loadCommunitySections === 'function') loadCommunitySections(null);
            if (typeof loadAiAnalysisData === 'function') loadAiAnalysisData(null);
            if (typeof loadComments === 'function') loadComments();
        } catch (e) {
            // 초기화 전 호출 시 일부 함수 미정의 — 대시보드가 이어서 초기 렌더하면 반영됨
            if (window.console && console.warn) console.warn('[Landing] 재렌더 중 일부 함수 실패:', e);
        }
    }

    // ── 오버레이 표시/숨김 ─────────────────────────────────
    function _showOverlay(isReentry) {
        var overlay = document.getElementById('landingOverlay');
        if (!overlay) return;
        if (_isOpen) return;
        _isOpen = true;
        overlay.hidden = false;
        overlay.setAttribute('aria-hidden', 'false');
        document.body.classList.add('landing-active');
        var closeBtn = document.getElementById('landingCloseBtn');
        if (closeBtn) closeBtn.hidden = !isReentry; // 첫 방문엔 닫기 버튼 숨김 (강제 온보딩)
    }
    function _hideOverlay() {
        var overlay = document.getElementById('landingOverlay');
        if (!overlay) return;
        // [v6.6.321] aria-hidden 을 되돌리기 전에 오버레이 내부에 남아있는 포커스를 해제한다.
        //   `Blocked aria-hidden on an element because its descendant retained focus` 경고 방지.
        //   WAI-ARIA 스펙상 포커스된 요소의 조상에 aria-hidden='true' 를 다는 것은 금지.
        try {
            var active = document.activeElement;
            if (active && overlay.contains(active) && typeof active.blur === 'function') active.blur();
        } catch (e) { /* blur 실패는 치명적이지 않음 */ }
        _isOpen = false;
        overlay.hidden = true;
        overlay.setAttribute('aria-hidden', 'true');
        document.body.classList.remove('landing-active');
    }

    // ── 이벤트 바인딩 ──────────────────────────────────────
    function _bindEvents() {
        var startBtn = document.getElementById('landingStartBtn');
        if (startBtn) startBtn.addEventListener('click', _commitAndEnterDashboard);

        var selectAll = document.getElementById('landingSelectAllBtn');
        if (selectAll) selectAll.addEventListener('click', function () {
            _cachedGames.forEach(function (g) { _selectedGames.add(g.name); });
            _renderGameGrid(_cachedGames);
            _updateSelectedCount();
        });
        var clearAll = document.getElementById('landingClearAllBtn');
        if (clearAll) clearAll.addEventListener('click', function () {
            _selectedGames.clear();
            _renderGameGrid(_cachedGames);
            _updateSelectedCount();
        });

        var themeRow = document.getElementById('landingThemeRow');
        if (themeRow) {
            themeRow.addEventListener('click', function (e) {
                var btn = e.target.closest('.landing-theme');
                if (!btn) return;
                var key = btn.getAttribute('data-theme');
                if (!key) return;
                _selectedTheme = key;
                // 미리보기: 즉시 body[data-theme] 변경 (localStorage 쓰기는 commit 때만)
                document.body.setAttribute('data-theme', key);
                _renderThemeRow();
            });
        }

        var closeBtn = document.getElementById('landingCloseBtn');
        if (closeBtn) closeBtn.addEventListener('click', _hideOverlay);
    }

    // ── 진입점 ────────────────────────────────────────────
    function _bootstrapIfNeeded() {
        if (!_shouldShowLandingOnBoot()) return;
        window.openLandingOverlay();
    }

    // 재진입용 전역 함수 — IME 로고 onclick 에서 호출
    window.openLandingOverlay = function () {
        var alreadyLoaded = _cachedGames.length > 0;
        _showOverlay(true /* isReentry: 닫기 버튼 노출 */);
        _renderThemeRow();
        if (alreadyLoaded) {
            _syncSelectedFromSettings();
            _renderGameGrid(_cachedGames);
            _updateSelectedCount();
            return;
        }
        _loadActiveGames().then(function (games) {
            _cachedGames = games;
            _syncSelectedFromSettings();
            _renderGameGrid(_cachedGames);
            _updateSelectedCount();
        });
    };

    // 부트 시 한 번 — 첫 방문자에게 강제 랜딩
    window.initLanding = function () {
        _bindEvents();
        if (!_shouldShowLandingOnBoot()) return;
        _showOverlay(false /* 첫 방문: 닫기 버튼 숨김 */);
        _renderThemeRow();
        _loadActiveGames().then(function (games) {
            _cachedGames = games;
            _syncSelectedFromSettings();
            _renderGameGrid(_cachedGames);
            _updateSelectedCount();
        });
    };

    // DOM 준비 시 자동 실행
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () {
            window.initLanding();
        });
    } else {
        window.initLanding();
    }

    // 참고용 export
    window.__IME_LANDING__ = {
        flagKey: LANDING_FLAG_KEY,
        reset: function () {
            try { localStorage.removeItem(LANDING_FLAG_KEY); } catch (e) {}
        }
    };
})();
