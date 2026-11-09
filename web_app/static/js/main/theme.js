// main/theme.js — 테마 피커 (5종 드롭다운)
// Phase 3 3차 분할 — 기존 main.js 에서 이관

// ─────────────────────────────────────────────────────
// [v6.6.262] 테마 피커 — 5종 드롭다운.
// body[data-theme="..."] 속성으로 전환, theme-variants.css 가 selector 매칭.
// 테마 목록: dark (기본) / monitor / blue / amber / emerald.
// ─────────────────────────────────────────────────────
const THEME_LABELS = {
    dark: 'Steel Monitor Dark',
    monitor: 'Steel Monitor',
    blue: 'Cobalt Blue',
    amber: 'Amber Terminal',
    emerald: 'Emerald Focus',
    light: 'Steel Monitor Light'
};
const VALID_THEMES = Object.keys(THEME_LABELS);
// [v6.6.502] 라이트가 아닌(다크 5종) 테마 집합 — 라이트 토글 시 직전 다크 복귀에 사용.
const DARK_THEMES = new Set(['dark', 'monitor', 'blue', 'amber', 'emerald']);

(function applyStoredTheme() {
    try {
        let saved = localStorage.getItem('imt_theme');
        // 구버전 값 마이그레이션
        if (saved === 'phosphor') saved = 'monitor';
        if (saved === 'classic') saved = 'dark';
        if (!VALID_THEMES.includes(saved)) saved = 'dark';
        if (document.body) document.body.setAttribute('data-theme', saved);
    } catch (e) {
        if (document.body) document.body.setAttribute('data-theme', 'dark');
    }
})();

window.closeThemeMenu = function() {
    const menu = document.getElementById('theme-picker-menu');
    const backdrop = document.getElementById('theme-picker-backdrop');
    if (menu) {
        menu.removeAttribute('data-open');
        menu.setAttribute('aria-hidden', 'true');
    }
    if (backdrop) backdrop.removeAttribute('data-open');
};

window.applyTheme = function(theme) {
    if (!THEME_LABELS[theme]) return;
    document.body.setAttribute('data-theme', theme);
    try {
        localStorage.setItem('imt_theme', theme);
        // [v6.6.502] 직전 다크 테마 기억 — 라이트로 토글 갔다가 다시 토글로 복귀할 때 사용.
        if (DARK_THEMES.has(theme)) localStorage.setItem('imt_theme_last_dark', theme);
    } catch (e) {}
    // 체크 마크 갱신 — [v6.7.391] [data-theme] 한정: wake-lock 토글 등 비-테마 옵션의 active(on) 상태가 벗겨지지 않게.
    document.querySelectorAll('#theme-picker-menu .theme-option[data-theme]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.theme === theme);
    });
    window.closeThemeMenu();
    // [v6.6.502] 상단 바 토글 아이콘 동기화
    if (typeof window.syncLightDarkIcon === 'function') window.syncLightDarkIcon();
    // [v6.6.506] 글로벌 테마 변경 이벤트 — graph-tab.js 등 차트 모듈이 listen 해서 색 재적용 + redraw.
    try {
        window.dispatchEvent(new CustomEvent('imt-theme-changed', { detail: { theme } }));
    } catch (_) {}
    if (typeof showToast === 'function') {
        showToast(`테마: ${THEME_LABELS[theme]}`);
    }
};

// [v6.6.502] 상단 바 라이트/다크 토글 — 클릭 시 라이트 ↔ 직전 다크 스왑.
window.toggleLightDark = function(e) {
    if (e && typeof e.stopPropagation === 'function') e.stopPropagation();
    const current = document.body.getAttribute('data-theme') || 'dark';
    let next;
    if (current === 'light') {
        let lastDark = 'dark';
        try { lastDark = localStorage.getItem('imt_theme_last_dark') || 'dark'; } catch (_) {}
        if (!DARK_THEMES.has(lastDark)) lastDark = 'dark';
        next = lastDark;
    } else {
        next = 'light';
    }
    window.applyTheme(next);
};

// [v6.6.504] 상단 바 새로고침 — 클릭 시 sit-refresh-btn 의 회전 애니메이션 보여준 뒤 clearLocalStorage() 호출.
// sitSpin keyframe 은 theme-variants.css 에 정의(0.6s ease 1회 회전, v6.7.629 에 situation.css 에서 이동). spin 시각 보여주려 300ms 딜레이.
window.topbarRefreshClick = function(e) {
    if (e && typeof e.preventDefault === 'function') e.preventDefault();
    const btn = document.getElementById('topbarRefreshBtn');
    if (btn) {
        btn.classList.add('spinning');
        // 애니메이션 끝(600ms)에서 클래스 정리 — 단, clearLocalStorage 가 reload 하면 의미 없음(자연 정리).
        setTimeout(() => { try { btn.classList.remove('spinning'); } catch (_) {} }, 700);
    }
    // 회전이 시각적으로 한 박자 보이도록 짧게 지연 — 너무 길면 답답하므로 300ms.
    setTimeout(() => {
        try {
            if (typeof window.clearLocalStorage === 'function') window.clearLocalStorage();
        } catch (_) {}
    }, 300);
};

// [v6.6.506] 토글 아이콘 동기화 — fa-adjust 단일 아이콘 유지(좌우 반원 회전으로 토글 표현).
// 라이트 모드일 때만 버튼에 .light-active 클래스 부여 → CSS 에서 색·rotate 로 활성 표시.
window.syncLightDarkIcon = function() {
    const btn = document.getElementById('lightDarkToggleBtn');
    if (!btn) return;
    const current = document.body.getAttribute('data-theme') || 'dark';
    btn.classList.toggle('light-active', current === 'light');
};
// 초기 1회 동기화 (DOMContentLoaded 이후 안전 호출)
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => window.syncLightDarkIcon());
} else {
    window.syncLightDarkIcon();
}

window.toggleThemeMenu = function(e) {
    if (e && typeof e.stopPropagation === 'function') e.stopPropagation();
    const menu = document.getElementById('theme-picker-menu');
    const btn = document.getElementById('theme-toggle-btn');
    const backdrop = document.getElementById('theme-picker-backdrop');
    if (!menu || !btn) return;
    const isOpen = menu.getAttribute('data-open') === 'true';
    if (isOpen) {
        window.closeThemeMenu();
        return;
    }
    // 현재 선택 테마 체크 표시 갱신 — [v6.7.391] [data-theme] 한정(비-테마 옵션 active 보존)
    const currentTheme = document.body.getAttribute('data-theme') || 'dark';
    menu.querySelectorAll('.theme-option[data-theme]').forEach(b => {
        b.classList.toggle('active', b.dataset.theme === currentTheme);
    });
    // 백드롭 먼저 켜기 (메뉴 z-index 보다 낮음)
    if (backdrop) backdrop.setAttribute('data-open', 'true');
    // 버튼 하단에 우측 정렬하여 배치
    menu.setAttribute('data-open', 'true');
    menu.setAttribute('aria-hidden', 'false');
    // offsetWidth 는 display:flex 이후에 정확한 값 반환
    const rect = btn.getBoundingClientRect();
    const menuWidth = menu.offsetWidth || 228;
    const menuHeight = menu.offsetHeight || 220;
    // [v6.6.451] 버튼이 사이드바 안에 있으면 우측에 펼치고, 헤더(또는 오버플로우)이면 기존 우측 정렬 dropdown.
    const isInSidebar = !!btn.closest('.sidebar');
    let leftRaw, topRaw;
    if (isInSidebar) {
        leftRaw = rect.right + 6;
        topRaw = rect.top;
    } else {
        leftRaw = rect.right - menuWidth;
        topRaw = rect.bottom + 8;
    }
    const left = Math.max(8, Math.min(window.innerWidth - menuWidth - 8, leftRaw));
    const top = Math.max(8, Math.min(window.innerHeight - menuHeight - 8, topRaw));
    menu.style.left = `${left}px`;
    menu.style.top = `${top}px`;
};

// [v6.6.266] 메뉴 닫기 트리거 — 주경로는 투명 백드롭의 onclick=closeThemeMenu().
// capture-phase document listener 는 백드롭이 커버하지 못하는 엣지 케이스(SVG 포인터 등) 백업용.
function _closeThemeMenuOutside(e) {
    const menu = document.getElementById('theme-picker-menu');
    const btn = document.getElementById('theme-toggle-btn');
    if (!menu || menu.getAttribute('data-open') !== 'true') return;
    if (menu.contains(e.target)) return;
    if (btn && btn.contains(e.target)) return;
    if (typeof window.closeThemeMenu === 'function') window.closeThemeMenu();
}
document.addEventListener('pointerdown', _closeThemeMenuOutside, true);
document.addEventListener('click', _closeThemeMenuOutside, true);
document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    const menu = document.getElementById('theme-picker-menu');
    if (menu && menu.getAttribute('data-open') === 'true') {
        if (typeof window.closeThemeMenu === 'function') window.closeThemeMenu();
    }
}, true);

/* ─── [v6.7.391] 화면 항상 켜기 (Screen Wake Lock) — 벽/보조 모니터 상시표시 시 OS 절전 차단 ───
   테마 메뉴 하단 토글(#wake-lock-toggle). localStorage(imt_wake_lock) 영속.
   브라우저는 탭이 hidden 되면 wake lock 을 자동 release 하므로 visibilitychange 복귀 시 재획득.
   미지원(navigator.wakeLock 부재)/거부는 조용히 무시 — 토글은 뜨되 no-op(열화 없음). */
(function () {
    'use strict';
    var _wl = null;
    function _syncBtn(on) {
        var b = document.getElementById('wake-lock-toggle');
        if (!b) return;
        b.classList.toggle('active', on);
        b.setAttribute('aria-checked', on ? 'true' : 'false');
    }
    function _acquire() {
        if (!('wakeLock' in navigator) || _wl) return;
        navigator.wakeLock.request('screen').then(function (wl) {
            _wl = wl;
            wl.addEventListener('release', function () { _wl = null; });   // OS/탭전환 자동 해제 반영
        }).catch(function () { _wl = null; });   // 거부(배터리 세이버 등) 조용히 무시
    }
    function _release() {
        if (_wl) { try { _wl.release(); } catch (_) {} _wl = null; }
    }
    window.toggleWakeLock = function (e) {
        if (e && typeof e.stopPropagation === 'function') e.stopPropagation();   // 메뉴 닫힘 방지(테마 옵션과 달리 상주)
        var on = false;
        try { on = localStorage.getItem('imt_wake_lock') !== '1'; } catch (_) { on = true; }   // 현재값 반전
        try { localStorage.setItem('imt_wake_lock', on ? '1' : '0'); } catch (_) {}
        _syncBtn(on);
        if (on) { _acquire(); if (!('wakeLock' in navigator)) console.warn('[wakeLock] 이 브라우저는 Screen Wake Lock 미지원'); }
        else _release();
    };
    // 탭 가시성 복귀 시 재획득 (hidden 시 브라우저가 자동 release)
    document.addEventListener('visibilitychange', function () {
        var on = false;
        try { on = localStorage.getItem('imt_wake_lock') === '1'; } catch (_) {}
        if (document.visibilityState === 'visible' && on) _acquire();
    });
    // 초기 상태 반영 + 활성이면 획득
    document.addEventListener('DOMContentLoaded', function () {
        var on = false;
        try { on = localStorage.getItem('imt_wake_lock') === '1'; } catch (_) {}
        _syncBtn(on);
        if (on) _acquire();
    });
})();
