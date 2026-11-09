// core/highlight.js — 무한 스크롤 공통 유틸
// [v6.7.455] is_new 칼럼 은퇴 — 신규 표시는 클라이언트 도착 잔광(각 탭 자체 관리)이 담당.

// ── 무한 스크롤 (Infinite Scroll) ──
const _infiniteScroll = {};

function _initInfiniteScroll(containerId, allData, pageSize, renderBatchFn) {
    if (_infiniteScroll[containerId]?.observer) {
        _infiniteScroll[containerId].observer.disconnect();
    }
    _infiniteScroll[containerId] = { data: allData, rendered: 0, pageSize, renderBatch: renderBatchFn, observer: null };

    const container = document.getElementById(containerId);
    if (!container) return;

    let sentinel = container.querySelector('.infinite-scroll-sentinel');
    if (!sentinel) {
        sentinel = document.createElement('div');
        sentinel.className = 'infinite-scroll-sentinel';
        sentinel.style.height = '1px';
        container.appendChild(sentinel);
    }

    _renderNextBatch(containerId);

    const observer = new IntersectionObserver((entries) => {
        if (entries[0].isIntersecting) {
            _renderNextBatch(containerId);
        }
    }, { root: container, rootMargin: '200px' });
    observer.observe(sentinel);
    _infiniteScroll[containerId].observer = observer;
}

function _renderNextBatch(containerId) {
    const state = _infiniteScroll[containerId];
    if (!state || state.rendered >= state.data.length) return;

    const end = Math.min(state.rendered + state.pageSize, state.data.length);
    const batch = state.data.slice(state.rendered, end);
    state.renderBatch(batch, containerId);
    state.rendered = end;
}

// [v6.7.169] 피드/댓글 서버 fetch budget 공통 계산 — feed-tab / comments-tab / boot.js 4중 중복 제거.
//   가시 게임(visKey 가 false 아닌)의 per-game limit(limitsKey) 합 × 1.2, floor 500, ceil maxView.
//   limit 0(무제한) 게임이 하나라도 있으면 maxView 반환. settings 는 _localSettingsData 또는
//   localStorage(imt_display_settings) 파싱 객체 — 둘 다 동일 스키마(limits/visibility/commentLimits/commentVisibility).
function _imtFetchBudget(settings, limitsKey, visKey, maxView) {
    const cap = maxView || 10000;
    try {
        const limits = (settings && settings[limitsKey]) || {};
        const vis = (settings && settings[visKey]) || {};
        const games = Object.keys(limits).filter(g => vis[g] !== false);
        if (games.some(g => (parseInt(limits[g], 10) || 0) === 0)) return cap;  // 무제한 게임 → 상한까지
        const sum = games.reduce((a, g) => a + (parseInt(limits[g], 10) || 0), 0);
        return Math.min(Math.max(Math.ceil(sum * 1.2), 500), cap);
    } catch (_) { return 500; }
}
