// ui/controls.js — 패널 리사이저 + 날짜 네비 + 뱃지 + 스토리지 초기화 + 새로고침/리셋
// Phase 3 4차 분할 — 기존 static/js/ui.js 에서 이관

// --- Resizer ---
function addCustomResizers(element, appId) {
    ["t", "r", "b", "l", "tl", "tr", "bl", "br"].forEach(dir => {
        const resizer = document.createElement("div"); resizer.className = `pane-resizer ${dir}`; element.appendChild(resizer);
        resizer.addEventListener("mousedown", (e) => {
            e.preventDefault(); e.stopPropagation();
            const rect = element.getBoundingClientRect(); const startX = e.clientX, startY = e.clientY, startW = rect.width, startH = rect.height;
            const onMove = (e) => { let nw = startW, nh = startH; if (dir.includes("r")) nw = startW + (e.clientX - startX); if (dir.includes("l")) nw = startW - (e.clientX - startX); if (dir.includes("b")) nh = startH + (e.clientY - startY); if (dir.includes("t")) nh = startH - (e.clientY - startY); if (nw > 200) element.style.width = `${nw}px`; if (nh > 150) element.style.height = `${nh}px`; };
            const onUp = () => { document.removeEventListener("mousemove", onMove); document.removeEventListener("mouseup", onUp); localStorage.setItem(`imt_pane_width_${appId}`, element.style.width); localStorage.setItem(`imt_pane_height_${appId}`, element.style.height); };
            document.addEventListener("mousemove", onMove); document.addEventListener("mouseup", onUp);
        });
    });
}

// --- Other Controls ---
// [v6.7.741] 상태 대시보드 토글 삭제 - 사이드바·모바일 진입점과 헤더 상태 박스를 함께 지웠다(사용자 2026-10-24).

// [v6.7.731] shiftDate 삭제 - 이미 no-op(deprecated) 였고 호출처 0(레포 전체 grep).
function onDateInputChange(appId, value) {
    if (!value) { resetToToday(appId); return; }
    const todayStr = new Date().toLocaleDateString('sv', {timeZone: 'Asia/Seoul'});
    if (value >= todayStr) { resetToToday(appId); return; }
    const prevDate = tabDates[appId]; tabDates[appId] = value; _updateDateNavUI(appId);
    loadHistoryData(appId, value).then(result => { if (result && result.data && result.data.length === 0) { tabDates[appId] = prevDate; _updateDateNavUI(appId); alert('해당 날짜의 데이터가 없습니다.'); } }).catch(() => { tabDates[appId] = prevDate; _updateDateNavUI(appId); });
}
function resetToToday(appId) { tabDates[appId] = null; _updateDateNavUI(appId); loadPaneData(appId); }
function _updateDateNavUI(appId) {
    const di = document.getElementById(`date-input-${appId}`); if (di) di.value = tabDates[appId] || '';
    const dl = document.getElementById(`date-label-${appId}`); const rb = document.getElementById(`date-reset-${appId}`);
    const hasDate = !!tabDates[appId];
    if (dl) { dl.textContent = hasDate ? tabDates[appId].slice(5) : ''; dl.style.display = hasDate ? '' : 'none'; }
    if (rb) rb.style.display = hasDate ? '' : 'none';
}

// --- Badge ---
function incrementUnreadBadge(appId) { if (activeApps.includes(appId)) return; unreadCounts[appId] = (unreadCounts[appId] || 0) + 1; _renderBadge(appId); }
function _renderBadge(appId) {
    const count = unreadCounts[appId] || 0;
    const navItem = document.getElementById(`app-box-${appId}`);
    if (navItem) { let badge = navItem.querySelector('.nav-badge'); if (count <= 0) { if (badge) badge.remove(); } else { if (!badge) { badge = document.createElement('span'); badge.className = 'nav-badge'; navItem.appendChild(badge); } badge.textContent = count > 99 ? '99+' : count; } }
    const btabItem = document.getElementById(`btab-${appId}`);
    if (btabItem) { let badge = btabItem.querySelector('.nav-badge'); if (count <= 0) { if (badge) badge.remove(); } else { if (!badge) { badge = document.createElement('span'); badge.className = 'nav-badge'; btabItem.appendChild(badge); } badge.textContent = count > 99 ? '99+' : count; } }
}
function _clearBadge(appId) { unreadCounts[appId] = 0; _renderBadge(appId); }

function clearLocalStorage() {
    if (!confirm('화면을 새로고침하시겠습니까?\n\n개인 필터 설정, 탭 크기 조정, 확인된 글 기록이 초기화되고 랜딩 페이지에서 다시 시작합니다.')) return;
    try {
        localStorage.removeItem('acknowledgedArticlesTotal');
        localStorage.removeItem('acknowledgedArticlesGames');
        localStorage.removeItem('gameOrder');
        localStorage.removeItem('foldedGames');
        localStorage.removeItem('imt_display_settings');
        // [v6.6.303] 랜딩 플래그도 함께 제거 → 재접속 시 랜딩 페이지 다시 표시
        localStorage.removeItem('imt_landing_completed');
        // [v6.6.306] 탭별 드래그 리사이즈 값(imt_pane_width_* / imt_pane_height_*) 일괄 제거 →
        //   코드 기본값(tab-manager.js:256-259) 복원. 예: games 와 community 가 동일 52% 로 보이도록.
        const _keysToRemove = [];
        for (let i = 0; i < localStorage.length; i++) {
            const k = localStorage.key(i);
            if (k && (k.startsWith('imt_pane_width_') || k.startsWith('imt_pane_height_'))) {
                _keysToRemove.push(k);
            }
        }
        _keysToRemove.forEach(k => localStorage.removeItem(k));
        trendGamesList.forEach(game => localStorage.removeItem(`trendState_${game}`));
        showToast('초기화 완료 — 페이지를 새로고침합니다.');
        setTimeout(() => { location.reload(); }, 1000);
    } catch (error) {
        showToast('새로고침 실패: ' + error, true);
    }
}
function refreshAll() { showToast('데이터를 새로고침합니다...'); updateCountdowns(); updateDashboard(); }
async function resetCrawler(target) {
    if (!confirm('정말 초기화하시겠습니까?\n메모리 데이터와 저장된 엑셀 파일이 모두 삭제됩니다.')) return;
    try {
        const resp = await fetch(`${API_BASE}/crawler/reset`, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({target: target}) });
        const result = await resp.json();
        if (result.success) {
            showToast(result.message);
            if (target === 'account_trade') { document.getElementById('account-trade-results-body').innerHTML = ''; document.querySelector('#account-trade-results-container table').style.display = 'none'; document.getElementById('account-trade-empty-state').style.display = 'block'; const sm = document.getElementById('account-trade-status-message'); if(sm) sm.style.display = 'none'; document.getElementById('copy-account-trade-results-btn').disabled = true; document.getElementById('download-account-trade-excel-btn').disabled = true; updateStatusBadge('account-trade-crawler-status', '대기'); }
            else if (target === 'odin_global') { document.getElementById('g2g-results-body').innerHTML = ''; document.querySelector('#g2g-results-container table').style.display = 'none'; document.getElementById('g2g-empty-state').style.display = 'block'; clearOdinGlobalStatusDisplay(); document.getElementById('copy-g2g-results-btn').disabled = true; document.getElementById('download-g2g-excel-btn').disabled = true; updateStatusBadge('odin-global-crawler-status', '대기'); }
            else if (target === 'jasa_games') { jasaResultsData = []; renderJasaResults(); const sm = document.getElementById('jasa-status-message'); if(sm) sm.style.display = 'none'; updateStatusBadge('jasa-crawler-status', '대기'); }
            else if (target === 'tasa_games') { tasaResultsData = []; renderTasaResults(); const sm = document.getElementById('tasa-status-message'); if(sm) sm.style.display = 'none'; updateStatusBadge('tasa-crawler-status', '대기'); }
        } else { showToast('초기화 실패: ' + result.message, true); }
    } catch (error) { showToast('초기화 요청 오류: ' + error, true); }
}
