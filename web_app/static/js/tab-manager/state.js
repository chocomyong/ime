// tab-manager/state.js — 전역 상태 + ResizeObserver + 초기 복원
// Phase 3 2차 분할 — 기존 tab-manager.js 에서 이관
//
// 외부 공개 심볼:
//   상수: EXTERNAL_SCROLL_APPS, CORE_APP_IDS, DASHBOARD_CONFIG_KEY, LAST_CORE_CONFIG_KEY
//   상태: activeApps, lastCoreApps, _currentDragPaneId, globalPaneObserver
// 의존:
//   loaded 순서: 본 파일이 actions.js / render.js 보다 먼저 로드되어야 함.

// [v6.6.290] 외부(main-wrapper) 스크롤 방식 탭 — ops/admin/sql-console 은 내부 스크롤로 전환되어 제외.
// 실제 외부 스크롤 유지 탭: games / account-trade / diamond-trade (원래부터 긴 테이블).
const EXTERNAL_SCROLL_APPS = ['games', 'account-trade', 'diamond-trade'];

// [v6.6.290] 1회성 cleanup — v6.6.287 로 clear 된 높이값 외에도 admin/ops/sql-console 의 이전
// 저장값이 남았으면 정리 (플래그 v290 재발급). 이 탭들은 이제 기본 800px/savedH 적용.
(function _cleanupObsoletePaneHeights() {
    const FLAG = 'imt_pane_height_cleanup_v290';
    if (localStorage.getItem(FLAG)) return;
    ['games', 'account-trade', 'diamond-trade', 'admin', 'ops', 'sql-console'].forEach(id => {
        try { localStorage.removeItem(`imt_pane_height_${id}`); } catch (_) {}
    });
    try { localStorage.setItem(FLAG, '1'); } catch (_) {}
})();

// [v6.6.433] 1회성 cleanup — v432 가 너무 좁다는 피드백 → default 댓글 360/320, min-width 320.
//   새 default 가 반영되도록 한 번 더 wipe. 사용자 resize 한 값만 다시 저장됨.
(function _cleanupObsoletePaneWidths() {
    const FLAG = 'imt_pane_width_cleanup_v433';
    if (localStorage.getItem(FLAG)) return;
    ['feed', 'comments', 'dashboard', 'total', 'games', 'community'].forEach(id => {
        try { localStorage.removeItem(`imt_pane_width_${id}`); } catch (_) {}
    });
    try { localStorage.setItem(FLAG, '1'); } catch (_) {}
})();

// [v6.6.435] dashboard pane 의 default 높이를 800→900px 로 변경 — 상황판 + 그래프 양쪽 노출 보장.
//   기존 사용자가 800px 또는 다른 값으로 저장해뒀으면 wipe.
(function _cleanupObsoleteDashboardHeight() {
    const FLAG = 'imt_pane_height_dashboard_cleanup_v435';
    if (localStorage.getItem(FLAG)) return;
    try { localStorage.removeItem('imt_pane_height_dashboard'); } catch (_) {}
    try { localStorage.setItem(FLAG, '1'); } catch (_) {}
})();

// --- Individual Resize Management (독립적인 패널 크기 관리) ---
const globalPaneObserver = new ResizeObserver(entries => {
    for (let entry of entries) {
        let newHeight = entry.target.style.height;
        let newWidth = entry.target.style.width;

        if (newHeight || newWidth) {
            const appId = entry.target.id.replace('pane-', '');
            // [v6.6.287] 외부 스크롤 탭은 height 저장 제외 (재저장 방지)
            const saveHeight = !EXTERNAL_SCROLL_APPS.includes(appId);
            if (newHeight && saveHeight) localStorage.setItem(`imt_pane_height_${appId}`, newHeight);
            if (newWidth) localStorage.setItem(`imt_pane_width_${appId}`, newWidth);
        }
    }
});
// [제거] mouseup 시 모든 패널 크기 초기화 로직 삭제 (독립적 크기 조절 보장)

// --- App Groups & Session Management ---
// [v6.6.422] 'feed' / 'dashboard' 합성 pane 추가. 'total/games/community' 는 호환용으로 유지.
// [v6.6.466] CORE_APP_IDS — 'dashboard'(legacy) → 'situation' 교체.
//   사이드바 "대시보드" 메뉴는 'situation' app 을 활성화하며, situation 도 코어로 변경되어
//   feed/comments 와 함께 multi-active 모드로 통합 환경에 표시됨.
// [v6.7.219] 'ccu-grafana'(그라파나) 코어 편입 — 유틸리티 단독 모드(activeApps=[appId]) 탈피.
//   피드/댓글과 공존(multi-active). 관리자 전용 가드는 ADMIN_ONLY_IDS/ADMIN_ONLY_APP_IDS 가 유지
//   (비관리자는 사이드바 숨김 + activeApps 자동 제거) — 코어 편입과 admin-only 는 독립 속성(situation 선례 역방향).
// [v6.7.278] 'naver-watcher'(알림) 코어 편입 — 유틸리티 단독 모드(activeApps=[appId]) 탈피.
//   피드/댓글/그라파나와 공존(multi-active). admin-only 가드는 ADMIN_ONLY_IDS/사이드바 {% if is_admin %} 가 유지
//   (코어 편입과 admin-only 는 독립 속성 — ccu-grafana v6.7.219 선례 동일). pane 폭은 render.js:108 에 이미 정의.
// [v6.7.638] **은닉 탭** - 정본은 서버(app.py `HIDDEN_APP_IDS`)이고 index.html 이 주입한다.
//   여기서 목록을 다시 적지 않는다 - 사본을 두면 서버와 갈라져 "사이드바엔 없는데 복원되는" 상태가 된다.
//   저장된 activeApps 에 남아 있어도 걸러내므로, 이미 열어둔 사람도 새로고침하면 사라진다.
const HIDDEN_APP_IDS = Array.isArray(window.IMT_HIDDEN_APPS) ? window.IMT_HIDDEN_APPS : [];
const _visible = (ids) => ids.filter(id => !HIDDEN_APP_IDS.includes(id));

// [v6.7.725] 첫 진입 기본 구성 = 피드 + AI 요약 (구 v6.6.466 피드/댓글/대시보드). landing.js 첫 방문 commit 도 같은 구성을 쓴다.
const DEFAULT_APP_IDS = ['feed', 'ai-summary'];
// [v6.7.741] 'naver-watcher'(알림) · 'discord' 삭제 - 탭째 지웠다(워처 종료, 사용자 2026-10-24).
const CORE_APP_IDS = ['total', 'games', 'community', 'comments', 'feed', 'situation', 'ccu-grafana', 'ai-summary', 'anomaly', 'verdict'];   // [v6.7.439] anomaly = 이상감지(admin)   // [v6.7.700] verdict = 판정(admin)
const DASHBOARD_CONFIG_KEY = 'imt_dashboard_config';
const LAST_CORE_CONFIG_KEY = 'imt_last_core_config';

// [개인화] 로컬 스토리지 키 변경 및 첫 방문 시 기본값(통합 모니터링) 설정
let activeApps = [];
let _defaultMigrated = false;   // [v6.7.725] 옛 기본 구성을 새 기본값으로 옮겼으면 lastCoreApps 도 같이 옮긴다
let _currentDragPaneId = null; // 드래그 중인 패널 ID (전역 상태)
try {
    const saved = localStorage.getItem(DASHBOARD_CONFIG_KEY);
    if (saved) {
        activeApps = JSON.parse(saved);
        // [v6.6.424] total/games/community 단독 탭은 피드(feed) 합성 pane 으로 통합.
        //   기존 사용자 localStorage 잔재를 자동 마이그레이션. 중복은 dedup 후 정렬.
        const migrationMap = {
            'totalArticles': 'feed', 'total': 'feed', 'games': 'feed', 'community': 'feed',
            'aiAnalysis': 'ai-analysis', 'accountTrade': 'account-trade', 'diamondTrade': 'diamond-trade',
            'logs': 'log-viewer'
        };
        activeApps = activeApps.map(id => migrationMap[id] || id);
        // 중복 제거 (3개 단독 탭이 모두 등록돼 있던 사용자 → feed 1개로 통합)
        activeApps = Array.from(new Set(activeApps));
        // [v6.7.191 FEAT-01] 'ccu' 제거 — CCU 탭 마크업 폐기.
        // [v6.7.219] 'ccu-grafana' 추가 — 코어 편입으로 상시 공존 가능해진 탭이라 복원 화이트리스트 포함 필수
        //   (누락 시 새로고침마다 필터링 제거됨). 비관리자 잔재는 checkAdminStatus→_revokeAdminUI 가 후속 제거.
        // [v6.7.278] 'naver-watcher' 추가 — 코어 편입으로 상시 공존 가능해진 탭이라 복원 화이트리스트 포함 필수
        //   (누락 시 새로고침마다 알림 탭이 제거됨 — ccu-grafana 와 동일). 비관리자 잔재는 checkAdminStatus→_revokeAdminUI 가 후속 제거.
        // [v6.7.741] 'update-history' · 'naver-watcher' · 'discord' 삭제 - 탭째 지웠다. 저장된 구성에 남아 있어도 여기서 걸러진다
        //   (옛 저장 키 'updates' 도 마이그레이션 맵에서 빼 같은 필터로 떨어진다).
        const validIds = ['ai-analysis', 'account-trade', 'diamond-trade', 'comments', 'log-viewer', 'admin', 'ops', 'sql-console', 'trends', 'situation', 'feed', 'dashboard', 'search', 'ccu-grafana', 'ai-summary', 'anomaly', 'verdict'];   // [v6.7.439] anomaly = 이상감지(admin)   // [v6.7.700] verdict = 판정(admin)
        activeApps = activeApps.filter(id => validIds.includes(id));
        activeApps = _visible(activeApps);          // [v6.7.638] 은닉 탭은 저장돼 있어도 복원하지 않는다
        // [v6.6.466] dashboard → situation 마이그레이션 — 기존 saved 의 'dashboard' 를 'situation' 으로 교체.
        //   ('dashboard' app id 는 legacy 폐기, situation 이 코어로 통합 환경에 표시)
        activeApps = activeApps.map(id => id === 'dashboard' ? 'situation' : id);
        activeApps = Array.from(new Set(activeApps));   // 마이그레이션 후 중복 제거
        // [v6.6.467] situation backfill — v466 이전 ['feed','comments'] 만 저장돼있던 사용자에겐
        //   dashboard→situation 매핑이 안 걸려 통합 환경에서 대시보드가 빠져 있음. 1회성으로 보충.
        const SIT_BACKFILL_FLAG = 'imt_situation_backfill_v467';
        if (!localStorage.getItem(SIT_BACKFILL_FLAG)) {
            const hasFeedOrComments = activeApps.includes('feed') || activeApps.includes('comments');
            if (hasFeedOrComments && !activeApps.includes('situation')) {
                activeApps.push('situation');
            }
            try { localStorage.setItem(SIT_BACKFILL_FLAG, '1'); } catch (_) {}
        }
    }
    // [v6.7.725] 기본 구성을 피드 + 대시보드 -> 피드 + AI 요약으로(사용자 지시 2026-10-11).
    //   이미 저장된 구성이 **옛 기본값 그대로**(피드+대시보드, 댓글 은닉 전 피드+댓글+대시보드 포함)면 1회 새 기본값으로 옮긴다.
    //   사용자가 직접 바꾼 구성은 건드리지 않는다. AI 요약을 못 여는 role(anon)은 _revokeAdminUI 가 후속 제거한다.
    const DEFAULT_AI_SUMMARY_FLAG = 'imt_default_ai_summary_v725';
    if (!localStorage.getItem(DEFAULT_AI_SUMMARY_FLAG)) {
        const _cur = _visible(activeApps).slice().sort().join(',');
        if (_cur === _visible(['feed', 'comments', 'situation']).sort().join(',')) {
            activeApps = DEFAULT_APP_IDS.slice();
            _defaultMigrated = true;
        }
        try { localStorage.setItem(DEFAULT_AI_SUMMARY_FLAG, '1'); } catch (_) {}
    }
    if (activeApps.length === 0) activeApps = _visible(DEFAULT_APP_IDS);
} catch (e) {
    activeApps = _visible(DEFAULT_APP_IDS);
}
activeApps = _visible(activeApps);   // [v6.7.638] 위 어느 갈래로 왔든 마지막에 한 번 더 거른다

// 이전에 보던 코어 탭 구성을 로드하거나 현재 상태에서 추출
let lastCoreApps = [];
try {
    const savedLastCore = localStorage.getItem(LAST_CORE_CONFIG_KEY);
    lastCoreApps = savedLastCore ? JSON.parse(savedLastCore) : activeApps.filter(id => CORE_APP_IDS.includes(id));
    // [v6.6.424] lastCoreApps 도 동일 마이그레이션 + dedup
    const _migMap = { 'totalArticles': 'feed', 'total': 'feed', 'games': 'feed', 'community': 'feed' };
    lastCoreApps = Array.from(new Set(lastCoreApps.map(id => _migMap[id] || id)));
} catch (e) { }
// [v6.7.741] 코어 목록 밖 id 도 거른다 - lastCoreApps 는 위 validIds 필터를 안 탄다. 지운 탭('naver-watcher' · 'discord')이
//   저장돼 있던 사용자는 유틸리티 탭에서 돌아올 때 없는 pane 을 복원하려 들었다(discord 는 그동안 은닉 필터가 대신 걸렀다).
lastCoreApps = _visible(lastCoreApps).filter(id => CORE_APP_IDS.includes(id));
if (_defaultMigrated) {
    // 플래그가 이미 섰으므로 여기서 저장하지 않으면 다음 로드에 옛 구성이 그대로 복원된다.
    lastCoreApps = activeApps.filter(id => CORE_APP_IDS.includes(id));
    try {
        localStorage.setItem(DASHBOARD_CONFIG_KEY, JSON.stringify(activeApps));
        localStorage.setItem(LAST_CORE_CONFIG_KEY, JSON.stringify(lastCoreApps));
    } catch (_) {}
}
if (lastCoreApps.length === 0) lastCoreApps = _visible(DEFAULT_APP_IDS);
