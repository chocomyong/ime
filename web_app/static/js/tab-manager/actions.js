// tab-manager/actions.js — 모바일 사이드바 토글 + 패널 열기/닫기/스왑 + 데이터 로드 디스패치
// Phase 3 2차 분할 — 기존 tab-manager.js 에서 이관
//
// 외부 공개 심볼:
//   toggleMobileSidebar, closeMobileSidebar, toggleMobileOverflow, closeMobileOverflow
//   toggleAppPane, closeAppPane, swapAppPanes, loadPaneData
// 의존:
//   state.js (activeApps, lastCoreApps, CORE_APP_IDS, _currentDragPaneId — state 먼저 로드)
//   render.js (updateDashboard — 런타임 호출이라 로드 순서 무관)
//   외부: _clearBadge (ui.js), showToast (ui.js), 각 탭별 load*/switch* (tabs/*.js)

// --- Mobile UI Controls ---
function toggleMobileSidebar() {
    document.body.classList.toggle('mobile-sidebar-open');
    document.body.classList.remove('controls-overflow-open');
}

function closeMobileSidebar() { document.body.classList.remove('mobile-sidebar-open'); }

function toggleMobileOverflow() {
    document.body.classList.toggle('controls-overflow-open');
    document.body.classList.remove('mobile-sidebar-open');
}

function closeMobileOverflow() { document.body.classList.remove('controls-overflow-open'); }

// 외부 클릭 시 오버플로우 닫기
document.addEventListener('click', (e) => {
    if (!e.target.closest('#controls-overflow-panel') && !e.target.closest('#mobile-overflow-btn')) {
        document.body.classList.remove('controls-overflow-open');
    }
});

// [v6.7.186 FE-01] situation pane 이 activeApps 에서 빠지면 폴링(60s situation + 5분 graph/passrate) 정리.
//   멱등 — situation 이 여전히 활성이면 unregister/stop 이 no-op. updateDashboard 호출 직전, situation 부재 시에만
//   발화하므로 재마운트·재register 와 race 없음. 옛 legacy 'dashboard' 가드(미발화 dead) 대체.
function _cleanupSituationIfGone() {
    try {
        if (Array.isArray(activeApps) && !activeApps.includes('situation') && typeof unloadSituationTab === 'function') {
            unloadSituationTab();
        }
    } catch (_) {}
}

// [v6.7.215] ccu-grafana pane 이 activeApps 에서 빠지면 30s 폴링 + 차트 + [급감] 타이틀 prefix 정리.
//   situation 패턴 동일 — 멱등(여전히 활성이면 no-op).
function _cleanupCcuGrafanaIfGone() {
    try {
        if (Array.isArray(activeApps) && !activeApps.includes('ccu-grafana') && typeof unloadCcuGrafanaTab === 'function') {
            unloadCcuGrafanaTab();
        }
    } catch (_) {}
}

// [v6.7.741] 알림(워처) · 디스코드 pane 폴링 정리 함수 2종 삭제 - 탭째 지웠다(워처 종료, 사용자 2026-10-24).

// [v6.7.700] verdict pane 이 activeApps 에서 빠지면 30s 폴링 정리 (anomaly 동일 패턴, 멱등).
function _cleanupVerdictIfGone() {
    try {
        if (Array.isArray(activeApps) && !activeApps.includes('verdict') && typeof unloadVerdictTab === 'function') {
            unloadVerdictTab();
        }
    } catch (_) {}
}

// [v6.7.439] anomaly pane 이 activeApps 에서 빠지면 60s 폴링 정리 (멱등).
function _cleanupAnomalyIfGone() {
    try {
        if (Array.isArray(activeApps) && !activeApps.includes('anomaly') && typeof unloadAnomalyTab === 'function') {
            unloadAnomalyTab();
        }
    } catch (_) {}
}

// [v6.7.436] ai-summary pane 이 activeApps 에서 빠지면 60s 안전망 폴링 정리 (anomaly 와 같은 패턴, 멱등).
function _cleanupAiSummaryIfGone() {
    try {
        if (Array.isArray(activeApps) && !activeApps.includes('ai-summary') && typeof unloadAiSummaryTab === 'function') {
            unloadAiSummaryTab();
        }
    } catch (_) {}
}

// [v6.7.301] admin pane 이 activeApps 에서 빠지면 헬스 폴링(30s×7 fetch + Chart 6개 재생성) 정리.
//   startHealthPolling(loadPaneData 'admin')은 진입 시 켜지나 stopHealthPolling 외부 호출처가 없어
//   탭을 닫아도 #health-grid 가 repository(display:none)에 잔존해 세션 내내 폴링 지속하던 누수. 멱등(여전히 활성이면 no-op).
function _cleanupAdminIfGone() {
    try {
        if (Array.isArray(activeApps) && !activeApps.includes('admin')) {
            if (typeof stopHealthPolling === 'function') stopHealthPolling();
            // [v6.7.731] 알림 트레이스 5초 폴링도 admin pane 이탈 시 정지(헬스 폴링과 동형). 멱등.
            if (typeof stopNotificationTracePolling === 'function') stopNotificationTracePolling();
        }
    } catch (_) {}
}

// [v6.7.731] ops pane 이 activeApps 에서 빠지면 크롤러 종합현황 30초 폴링 정리 (admin 동일 패턴, 멱등).
function _cleanupOpsIfGone() {
    try {
        if (Array.isArray(activeApps) && !activeApps.includes('ops') && typeof stopCrawlerOverviewPolling === 'function') {
            stopCrawlerOverviewPolling();
        }
    } catch (_) {}
}

// [v6.7.731] pane 이탈 시 폴링 정리 호출을 한 곳으로 - 종전엔 아래 3개 분기(모바일 단일탭·코어 토글·닫기)에
//   같은 _cleanupXxxIfGone 묶음이 반복 나열돼 새 탭 추가 시 한 자리를 빠뜨리기 쉬웠다(PATTERN-72). 랜딩 '시작'도 이걸 쓴다.
function _runPaneCleanups() {
    _cleanupSituationIfGone();
    _cleanupCcuGrafanaIfGone();
    _cleanupAiSummaryIfGone();
    _cleanupAnomalyIfGone();
    _cleanupVerdictIfGone();
    _cleanupAdminIfGone();
    _cleanupOpsIfGone();
}

function toggleAppPane(appId) {
    // [v6.7.638] 은닉 탭은 여기서 막는다 - 진입로가 사이드바/하단탭바/단축키/구 DOM 으로 여럿이라
    //   각 진입로마다 조건을 적으면 하나가 빠진다(PATTERN-56). 문지기를 하나로 둔다.
    if (typeof HIDDEN_APP_IDS !== 'undefined' && HIDDEN_APP_IDS.includes(appId)) return;
    // [v6.6.399] 탭 전환 전 현재 활성 탭들의 스크롤 위치 저장 (display:none 시 위치 손실 방지)
    if (window._tabHelpers && Array.isArray(activeApps)) {
        activeApps.forEach(id => { try { window._tabHelpers.persistScroll(id); } catch (_) {} });
    }
    // 모바일 사이드바/오버플로우 닫기
    document.body.classList.remove('mobile-sidebar-open');
    document.body.classList.remove('controls-overflow-open');

    // 모바일(pointer:coarse): 단일 탭 모드 — 선택한 탭 하나만 표시, 나머지 모두 닫음
    if (window.matchMedia('(pointer: coarse)').matches) {
        activeApps = [appId];
        localStorage.setItem(DASHBOARD_CONFIG_KEY, JSON.stringify(activeApps));
        _runPaneCleanups();   // [v6.7.731] 모바일 단일탭 전환 시 빠진 pane 폴링 일괄 정리
        updateDashboard();
        _clearBadge(appId);
        return;
    }

    const isCore = CORE_APP_IDS.includes(appId);
    const hasUtility = activeApps.some(id => !CORE_APP_IDS.includes(id));

    if (isCore) {
        if (hasUtility) {
            // 유틸리티 모드에서 코어 클릭 시: 유틸 닫고 코어 세션 복구 + 신규 탭 추가
            activeApps = lastCoreApps.includes(appId) ? [...lastCoreApps] : [...lastCoreApps, appId];
        } else {
            // 일반 코어 모드 토글
            const index = activeApps.indexOf(appId);
            if (index === -1) activeApps.push(appId);
            else if (activeApps.length > 1) activeApps.splice(index, 1);
            else { showToast('최소 한 개의 모니터링 앱은 활성화되어야 합니다.', true); return; }
        }
        lastCoreApps = [...activeApps];
        localStorage.setItem(LAST_CORE_CONFIG_KEY, JSON.stringify(lastCoreApps));
    } else {
        // 유틸리티 앱 클릭 시
        if (activeApps.includes(appId)) {
            // 이미 보고 있는 유틸리티를 다시 누르면 코어로 복귀
            activeApps = [...lastCoreApps];
        } else {
            // 새 유틸리티 활성화: 현재 코어 상태 저장 후 단독 모드 진입
            if (!hasUtility) {
                lastCoreApps = [...activeApps];
                localStorage.setItem(LAST_CORE_CONFIG_KEY, JSON.stringify(lastCoreApps));
            }
            activeApps = [appId];
        }
    }

    localStorage.setItem(DASHBOARD_CONFIG_KEY, JSON.stringify(activeApps));
    // 활성화된 탭의 뱃지 클리어
    if (activeApps.includes(appId)) {
        _clearBadge(appId);
        // 전체/게임/커뮤니티는 동일 데이터 — 셋 중 하나를 열면 셋 다 읽음 처리
        if (['total', 'games', 'community'].includes(appId)) {
            _clearBadge('total');
            _clearBadge('games');
            _clearBadge('community');
        }
    }
    _runPaneCleanups();   // [v6.7.731] 코어 토글로 빠진 pane 폴링 일괄 정리
    updateDashboard();

    // [v6.6.399] updateDashboard 후 활성 탭들의 스크롤 위치 복원
    if (window._tabHelpers && Array.isArray(activeApps)) {
        // 다음 frame 에 적용 (DOM 보장 후)
        setTimeout(() => {
            activeApps.forEach(id => { try { window._tabHelpers.restoreScroll(id); } catch (_) {} });
        }, 0);
    }

    // 포커스 이동 애니메이션
    setTimeout(() => {
        const pane = document.getElementById(`pane-${appId}`);
        if (pane) {
            pane.scrollIntoView({ behavior: 'smooth', block: 'center' });
            pane.style.boxShadow = '0 0 40px rgba(var(--primary-rgb), 0.5)';
            pane.style.borderColor = 'var(--primary-color)';
            setTimeout(() => { pane.style.boxShadow = ''; pane.style.borderColor = ''; }, 1000);
        }
    }, 150);
}

// [v6.7.349] "활성 아니면만 전환" 가드 - toggleAppPane 은 순수 토글이라 이미 활성인 탭에 부르면
//   오히려 닫힌다(코어 토글/유틸리티 복귀 특성). 인앱 배너(socket.js:632)가 이 가드를 인라인으로
//   먼저 썼고, 단축키(main/shortcuts.js)·?tab= 딥링크가 3번째 사용처가 되면서 공용 함수로 추출.
function ensureAppPane(appId) {
    try {
        if (Array.isArray(activeApps) && activeApps.includes(appId)) return;
    } catch (_) {}
    toggleAppPane(appId);
}

function closeAppPane(appId) {
    const isCore = CORE_APP_IDS.includes(appId);
    if (!isCore) {
        // 유틸리티 닫기 버튼 클릭 -> 코어 복구
        activeApps = [...lastCoreApps];
    } else if (activeApps.length > 1) {
        const index = activeApps.indexOf(appId);
        if (index > -1) {
            activeApps.splice(index, 1);
            lastCoreApps = [...activeApps];
            localStorage.setItem(LAST_CORE_CONFIG_KEY, JSON.stringify(lastCoreApps));
        }
    } else {
        showToast('최소 한 개의 모니터링 앱은 활성화되어야 합니다.', true);
        return;
    }
    localStorage.setItem(DASHBOARD_CONFIG_KEY, JSON.stringify(activeApps));
    _runPaneCleanups();   // [v6.7.731] 닫힘/코어 복구 시 빠진 pane 폴링 일괄 정리
    updateDashboard();
}

// [수정] 드래그 앤 드롭 시 DOM 직접 스왑 + activeApps 배열 동기화
function swapAppPanes(id1, id2) {
    const pane1 = document.getElementById(`pane-${id1}`);
    const pane2 = document.getElementById(`pane-${id2}`);
    const grid = document.getElementById('dashboard-grid');
    if (!pane1 || !pane2 || !grid) return;

    // DOM 위치 스왑 (placeholder 사용)
    const placeholder = document.createElement('div');
    grid.insertBefore(placeholder, pane1);
    grid.insertBefore(pane1, pane2);
    grid.insertBefore(pane2, placeholder);
    grid.removeChild(placeholder);

    // activeApps 배열도 동기화 (있는 경우만)
    const idx1 = activeApps.indexOf(id1);
    const idx2 = activeApps.indexOf(id2);
    if (idx1 > -1 && idx2 > -1) {
        activeApps[idx1] = id2;
        activeApps[idx2] = id1;
        localStorage.setItem('imt_dashboard_config', JSON.stringify(activeApps));
    }

    // 시각적 피드백
    pane1.style.boxShadow = '0 0 30px rgba(49, 130, 246, 0.4)';
    setTimeout(() => pane1.style.boxShadow = '', 800);
}

function loadPaneData(appId) {
    // 과거 날짜 조회 모드이면 history API 사용
    if (tabDates[appId] != null) {
        loadHistoryData(appId, tabDates[appId]);
        return;
    }
    if (appId === 'total') loadTotalArticles();
    else if (appId === 'ai-analysis') loadAiAnalysisData();
    else if (appId === 'games') loadGamesSections();
    else if (appId === 'community') loadCommunitySections();
    // [v6.6.422] 통합 모니터링 환경 — feed 합성 pane (전체/게임/커뮤니티 세그먼트)
    else if (appId === 'feed') { if (typeof loadFeedPane === 'function') loadFeedPane(); }
    // [v6.6.494] dashboard 합성 pane 분기 제거 — '대시보드' 메뉴는 #app-box-situation 으로 매핑되어 'situation' 분기 사용.
    else if (appId === 'account-trade') {
        switchAccountTradeGameTab(currentAccountTradeGameTab);
        pollAccountTradeStatus();
    }
    else if (appId === 'diamond-trade') switchDiamondCompanyTab(currentDiamondCompanyTab);
    else if (appId === 'comments') loadComments();
    else if (appId === 'log-viewer') loadLogs(true);
    else if (appId === 'admin') {
        if (typeof loadAdminVisitLogs === 'function') loadAdminVisitLogs();
        if (typeof loadGsheetWriteStatus === 'function') loadGsheetWriteStatus();
        // [v6.7.70] F-7 시스템 건강도 — admin 탭 진입 시 자동 1회 로드 + 30초 폴링.
        if (typeof loadHealthDashboard === 'function') loadHealthDashboard();
        if (typeof startHealthPolling === 'function') startHealthPolling();
        // [v6.7.731] 알림 트레이스 5초 폴링 - admin pane 진입 시 시작(이탈 시 _cleanupAdminIfGone 가 정지). 멱등.
        if (typeof startNotificationTracePolling === 'function') startNotificationTracePolling();
    }
    else if (appId === 'ops') { if (typeof loadAdminDashboard === 'function') loadAdminDashboard(); }
    else if (appId === 'situation') { if (typeof loadSituationTab === 'function') loadSituationTab(); }
    // [v6.7.215] 그라파나 CCU 미러 — 관리자 전용. 진입 시 즉시 1회 fetch + 30s 폴링 시작.
    else if (appId === 'ccu-grafana') { if (typeof loadCcuGrafanaTab === 'function') loadCcuGrafanaTab(); }
    // AI 요약 - 관리자 전용. 진입 시 자체 fetch + [v6.7.436] 60s 안전망 폴링(소켓 emit 유실 대비, cleanup 필요).
    else if (appId === 'ai-summary') { if (typeof loadAiSummaryTab === 'function') loadAiSummaryTab(); }
    // [v6.7.439] 이상감지 - admin 전용. 진입 시 즉시 1회 fetch + 60s 폴링(cleanup 필요).
    else if (appId === 'anomaly') { if (typeof loadAnomalyTab === 'function') loadAnomalyTab(); }
    // [v6.7.700] 판정 - admin 전용. 진입 시 즉시 1회 fetch + 30s 폴링(cleanup 필요).
    else if (appId === 'verdict') { if (typeof loadVerdictTab === 'function') loadVerdictTab(); }
    // [v6.7.69] F-4 통합 검색/필터 — 관리자 전용. 첫 진입 시 toolbar 만 표시 (자동 검색 안 함, 사용자 트리거).
    else if (appId === 'search') { if (typeof loadSearchTab === 'function') loadSearchTab(); }
    else if (appId === 'sql-console') { if (typeof loadDbTables === 'function') loadDbTables(); }
    else if (appId === 'trends') { setTimeout(async () => { if (typeof initTrendTabs === 'function') { await initTrendTabs(); if (typeof switchTrendGameTab === 'function' && trendGamesList.length > 0) switchTrendGameTab(currentTrendGameTab || trendGamesList[0]); } }, 100); }
}
