// ui/settings-modal.js — 설정 모달 (Config_* 편집 + 로컬 필터링)
// Phase 3 4차 분할 — 기존 static/js/ui.js 에서 이관
// 의존: ui/common.js 의 showToast, escapeHtml — 먼저 로드되어야 함.

// --- Settings Modal ---
const SETTINGS_TAB_LABELS = { 'Config_Keywords': '키워드', 'Config_Games': '메인 크롤러', 'Config_SubCrawler': '서브 크롤러', '__local__': '필터링' };
let _currentSettingsTopTab = 'Config_Keywords';
let _localSettingsData = null;
let _filteringOnlyMode = false; // [v6.6.235] 비관리자용 필터링 전용 모달 모드 플래그

// [v6.6.243] 서버 응답(games/limits/visibility/comment_limit) + localStorage(commentVisibility 등 개인 필터) 머지
// 서버 fetch 후 이 함수로 감싸야 모달 재진입 시 개인 설정이 초기화되지 않음.
function _mergeLocalSettingsWithStorage(serverData) {
    try {
        const saved = localStorage.getItem('imt_display_settings');
        if (saved) {
            const parsed = JSON.parse(saved);
            return { ...serverData, ...parsed };
        }
    } catch (e) {
        console.warn('[_mergeLocalSettingsWithStorage] localStorage JSON.parse 실패, 서버값만 사용:', e);
        try { localStorage.removeItem('imt_display_settings'); } catch (_) {}
    }
    return serverData;
}

// [v6.6.244] display-settings 안전 fetch — HTML 응답/네트워크 실패 모두 방어
// 실패 시 이전 _localSettingsData(games 포함) + localStorage 개인필터 로 fallback.
// [v6.7.97] 성공 시 imt_settings_games_cache 영속 캐시 갱신 + 실패 시 캐시 fallback 추가
//           (boot.js 가 다음 세션 부트 시 즉시 hydrate 가능 → 필터 모달 첫 진입 빈 카드 회피).
async function _safeFetchDisplaySettings() {
    try {
        const resp = await fetch(`${API_BASE}/config/display-settings`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const ct = resp.headers.get('Content-Type') || '';
        if (!ct.includes('application/json')) {
            const head = (await resp.text()).slice(0, 60);
            throw new Error(`Non-JSON 응답: ${head}...`);
        }
        const serverData = await resp.json();
        // [v6.7.97] 서버 응답의 games/limits/visibility/comment_limit 만 영속 캐시 저장.
        //   개인 필터(imt_display_settings) 와 분리 — 캐시 키는 imt_* prefix 규칙 준수.
        try {
            localStorage.setItem('imt_settings_games_cache', JSON.stringify({
                games: Array.isArray(serverData.games) ? serverData.games : [],
                limits: serverData.limits || {},
                visibility: serverData.visibility || {},
                comment_limit: serverData.comment_limit !== undefined ? serverData.comment_limit : 100,
                ts: Date.now()
            }));
        } catch (e) { /* quota / private mode — silent */ }
        return _mergeLocalSettingsWithStorage(serverData);
    } catch (e) {
        console.warn('[display-settings] 서버 fetch 실패, fallback 사용:', e);
        // 1순위: 이전 세션에 받아둔 _localSettingsData (games 포함) + localStorage
        if (_localSettingsData && Array.isArray(_localSettingsData.games) && _localSettingsData.games.length > 0) {
            if (typeof showToast === 'function') showToast('서버 연결 불안정 — 최근 설정 표시', true);
            return _mergeLocalSettingsWithStorage(_localSettingsData);
        }
        // [v6.7.97] 2순위: imt_settings_games_cache 영속 캐시 hydrate (이전 세션 서버 응답).
        try {
            const cached = localStorage.getItem('imt_settings_games_cache');
            if (cached) {
                const parsed = JSON.parse(cached);
                if (parsed && Array.isArray(parsed.games) && parsed.games.length > 0) {
                    if (typeof showToast === 'function') showToast('서버 연결 불안정 — 최근 게임 목록 표시', true);
                    return _mergeLocalSettingsWithStorage({
                        games: parsed.games,
                        limits: parsed.limits || {},
                        visibility: parsed.visibility || {},
                        comment_limit: parsed.comment_limit !== undefined ? parsed.comment_limit : 100
                    });
                }
            }
        } catch (_) {}
        // 3순위: localStorage 만이라도 (games 비어있으면 카드 안 그려지지만 적어도 모달은 뜸)
        const fallback = _mergeLocalSettingsWithStorage({ games: [], limits: {}, visibility: {}, comment_limit: 100 });
        if (typeof showToast === 'function') showToast('설정 로드 실패 — 로컬 저장값으로 표시', true);
        return fallback;
    }
}

async function openSettingsModal() {
    _filteringOnlyMode = false;
    // 필터링 전용 모드로 닫혔던 경우 대비 — 헤더/탭바/저장버튼 원복
    const _header = document.querySelector('#settingsModal .modal-header h2');
    if (_header) _header.textContent = '설정 관리';
    const _topTabs = document.getElementById('settingsTopTabs');
    if (_topTabs) _topTabs.style.display = '';
    const _btnSave = document.getElementById('btnSaveSettings');
    if (_btnSave) _btnSave.style.display = 'inline-block';
    const _modal = document.getElementById('settingsModal');
    if (_modal) _modal.classList.remove('filtering-only'); // [v6.6.241] 풀 너비 복귀
    _modal.style.display = 'block';
    currentConfigTab = '';
    _currentSettingsTopTab = localStorage.getItem('imt_settings_last_tab') || 'Config_Keywords';
    try {
        const cfgResp = await fetch(`${API_BASE}/config`);
        currentConfigData = await cfgResp.json();
    } catch (e) { alert('설정 로드 실패: ' + e); closeSettingsModal(); return; }
    // [v6.6.244] display-settings 는 safe fetch 로 실패해도 fallback 가능
    _localSettingsData = await _safeFetchDisplaySettings();
    _renderSettingsTopTabs();
    _switchSettingsTopTab(_currentSettingsTopTab);
}

// [v6.6.235] 비관리자 포함 누구나 접근 가능한 필터링 전용 진입점
// 설정 모달 구조를 재사용하되 상단 탭바/저장 버튼 숨기고 __local__ 패널만 표시
async function openFilteringModal() {
    _filteringOnlyMode = true;
    const modal = document.getElementById('settingsModal');
    if (!modal) return;
    modal.classList.add('filtering-only'); // [v6.6.241] 너비 축소용 클래스
    // 헤더 타이틀 교체
    const header = modal.querySelector('.modal-header h2');
    if (header) header.textContent = '필터링';
    // 상단 탭바 숨김
    const topTabs = document.getElementById('settingsTopTabs');
    if (topTabs) topTabs.style.display = 'none';
    // 행추가/서버저장 숨김, 즉시적용만 노출
    const btnAddRow = document.getElementById('btnAddRow');
    if (btnAddRow) btnAddRow.style.display = 'none';
    const btnSave = document.getElementById('btnSaveSettings');
    if (btnSave) btnSave.style.display = 'none';
    const btnApply = document.getElementById('btnApplyLocal');
    if (btnApply) btnApply.style.display = 'inline-block';
    // [v6.6.255] 헤더에서 이관된 초기화 버튼 — 필터링 모달에서만 노출
    const btnClear = document.getElementById('btnClearStorage');
    if (btnClear) btnClear.style.display = 'inline-block';
    // Config 패널 숨기고 로컬(필터링) 패널만 표시
    const configPanel = document.getElementById('settingsConfigPanel');
    const localPanel = document.getElementById('settingsLocalPanel');
    if (configPanel) configPanel.style.display = 'none';
    if (localPanel) { localPanel.style.display = 'flex'; localPanel.style.flexDirection = 'column'; }
    modal.style.display = 'block';
    _currentSettingsTopTab = '__local__';
    // [v6.7.97] 캐시 hydrate 가 있으면 즉시 렌더 (체감 0ms) — fetch 응답 기다리는 동안 빈 모달 회피.
    if (_localSettingsData && Array.isArray(_localSettingsData.games) && _localSettingsData.games.length > 0) {
        _renderLocalSettings();
    } else {
        // boot.js 가 아직 hydrate 못 한 edge 케이스 — localStorage 캐시 직접 시도.
        try {
            const cached = localStorage.getItem('imt_settings_games_cache');
            if (cached) {
                const parsed = JSON.parse(cached);
                if (parsed && Array.isArray(parsed.games) && parsed.games.length > 0) {
                    _localSettingsData = _mergeLocalSettingsWithStorage({
                        games: parsed.games,
                        limits: parsed.limits || {},
                        visibility: parsed.visibility || {},
                        comment_limit: parsed.comment_limit !== undefined ? parsed.comment_limit : 100
                    });
                    _renderLocalSettings();
                }
            }
        } catch (_) {}
    }
    // [v6.6.244] safe fetch — HTML/네트워크 실패 시에도 fallback 경로로 모달 정상 오픈
    _localSettingsData = await _safeFetchDisplaySettings();
    _renderLocalSettings();
}

// [v6.7.189 A11Y-03] 설정/필터 모달 ESC 닫기 (openSettingsModal/openFilteringModal 둘 다 display='block').
document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    var m = document.getElementById('settingsModal');
    if (m && m.style.display === 'block' && typeof closeSettingsModal === 'function') closeSettingsModal();
});

function closeSettingsModal() {
    const modal = document.getElementById('settingsModal');
    if (modal) {
        modal.style.display = 'none';
        modal.classList.remove('filtering-only'); // [v6.6.241] 너비 축소 클래스 원복
    }
    // [v6.6.255] 필터링 모달 전용 초기화 버튼 원복
    const btnClear = document.getElementById('btnClearStorage');
    if (btnClear) btnClear.style.display = 'none';
    if (_filteringOnlyMode) {
        // 필터링 전용 모드로 열었던 DOM 상태 원복 (다음 '설정' 진입을 위한 준비)
        _filteringOnlyMode = false;
        const header = document.querySelector('#settingsModal .modal-header h2');
        if (header) header.textContent = '설정 관리';
        const topTabs = document.getElementById('settingsTopTabs');
        if (topTabs) topTabs.style.display = '';
    }
}

function _renderSettingsTopTabs() {
    const container = document.getElementById('settingsTopTabs'); container.innerHTML = '';
    ['Config_Keywords', 'Config_Games', 'Config_SubCrawler', '__local__'].forEach(key => {
        const btn = document.createElement('button');
        btn.className = 'settings-tab-btn' + (key === _currentSettingsTopTab ? ' active' : '');
        btn.textContent = SETTINGS_TAB_LABELS[key] || key;
        btn.onclick = () => _switchSettingsTopTab(key);
        container.appendChild(btn);
    });
}

function _switchSettingsTopTab(key) {
    if (_currentSettingsTopTab !== '__local__') saveCurrentTableToMemory();
    _currentSettingsTopTab = key; localStorage.setItem('imt_settings_last_tab', key); _renderSettingsTopTabs();
    const configPanel = document.getElementById('settingsConfigPanel'); const localPanel = document.getElementById('settingsLocalPanel');
    const btnAddRow = document.getElementById('btnAddRow'); const btnSave = document.getElementById('btnSaveSettings'); const btnApply = document.getElementById('btnApplyLocal');
    const searchInput = document.getElementById('settingsSearchInput');
    if (key === '__local__') {
        configPanel.style.display = 'none'; localPanel.style.display = 'flex'; localPanel.style.flexDirection = 'column';
        if (btnAddRow) btnAddRow.style.display = 'none'; if (btnSave) btnSave.style.display = 'none'; if (btnApply) btnApply.style.display = 'inline-block';
        _renderLocalSettings();
    } else {
        configPanel.style.display = 'flex'; localPanel.style.display = 'none';
        if (btnAddRow) btnAddRow.style.display = 'inline-block'; if (btnSave) btnSave.style.display = 'inline-block'; if (btnApply) btnApply.style.display = 'none';
        if (searchInput) searchInput.value = '';
        currentConfigTab = key; renderConfigTable();
    }
}

function renderConfigTable() {
    const container = document.getElementById('configContent');
    const data = currentConfigData[currentConfigTab] || [];
    const headers = TAB_COLUMN_ORDER[currentConfigTab] || (data.length > 0 ? Object.keys(data[0]) : []);
    let html = '<table class="config-table"><thead><tr>';
    headers.forEach(h => { const hL = h.toLowerCase(); let w = ''; if (hL.includes('game')) w = 'width:190px;'; else if (hL.includes('active')) w = 'width:80px;'; else if (hL.includes('type') || hL.includes('category')) w = 'width:110px;'; else if (hL.includes('unit')) w = 'width:120px;'; html += `<th style="${w}">${h}</th>`; });
    html += '<th style="width:48px;">삭제</th></tr></thead><tbody>';
    if (data.length === 0) { html += `<tr><td colspan="${headers.length + 1}" style="text-align:center;padding:24px;color:#666;">데이터가 없습니다. 행을 추가해주세요.</td></tr>`; }
    else { data.forEach((row, idx) => { html += '<tr>'; headers.forEach(key => { const val = row[key] !== undefined ? row[key] : ''; if (key === 'Active') { const isTrue = String(val).toUpperCase() === 'TRUE'; html += `<td><select data-key="${key}"><option value="TRUE"${isTrue?' selected':''}>TRUE</option><option value="FALSE"${!isTrue?' selected':''}>FALSE</option></select></td>`; } else if (key === 'Type' && currentConfigTab === 'Config_Keywords') { const isSuper = String(val).toUpperCase() === 'SUPER'; html += `<td><select data-key="${key}"><option value="SUPER"${isSuper?' selected':''}>SUPER</option><option value="NORMAL"${!isSuper?' selected':''}>NORMAL</option></select></td>`; } else { html += `<td><input type="text" value="${String(val).replace(/&/g,'&amp;').replace(/"/g,'&quot;')}" data-key="${key}"></td>`; } }); html += `<td><button class="row-action-btn" onclick="deleteConfigRow(${idx})">X</button></td></tr>`; }); }
    html += '</tbody></table>'; container.innerHTML = html;
}

function filterSettingsTable(text) { const q = text.toLowerCase(); document.querySelectorAll('#configContent tbody tr').forEach(tr => { const match = Array.from(tr.querySelectorAll('input,select')).some(el => el.value.toLowerCase().includes(q)); tr.style.display = (!q || match) ? '' : 'none'; }); }
// [v6.7.731] 검색으로 숨긴 행(display:none)도 보존한다. 종전엔 숨긴 행을 건너뛰어 currentConfigData 를 가시 행만으로
//   덮었고, 그 상태로 /config/update 가 POST 되면 검색에 안 걸린 크롤러 설정·키워드·구글 시트 행이 통째로 사라졌다.
//   또한 deleteConfigRow 가 render 시점 전체 인덱스로 splice 하는데, 숨긴 행이 빠지면 배열이 collapse 돼 엉뚱한 행이
//   지워지던 것도 함께 해소된다(DOM 순서=전체 데이터 순서 유지).
function saveCurrentTableToMemory() { if (!currentConfigTab) return; const rows = document.querySelectorAll('#configContent tbody tr'); if (!rows.length) return; const newData = []; rows.forEach(tr => { const rowObj = {}; tr.querySelectorAll('input,select').forEach(el => { if (el.dataset.key) rowObj[el.dataset.key] = el.value; }); if (Object.keys(rowObj).length) newData.push(rowObj); }); currentConfigData[currentConfigTab] = newData; }
function addConfigRow() { saveCurrentTableToMemory(); if (!currentConfigData[currentConfigTab]) currentConfigData[currentConfigTab] = []; const data = currentConfigData[currentConfigTab]; const template = {}; const cols = TAB_COLUMN_ORDER[currentConfigTab] || (data.length > 0 ? Object.keys(data[0]) : []); cols.forEach(k => { template[k] = ''; }); if ('Active' in template) template['Active'] = 'TRUE'; if (currentConfigTab === 'Config_Keywords') template['Type'] = 'SUPER'; data.push(template); renderConfigTable(); setTimeout(() => { const c = document.getElementById('configContent'); if (c) c.scrollTop = c.scrollHeight; }, 0); }
function deleteConfigRow(index) { saveCurrentTableToMemory(); currentConfigData[currentConfigTab].splice(index, 1); renderConfigTable(); }
async function saveSettings() { if (document.activeElement) document.activeElement.blur(); saveCurrentTableToMemory(); if (!confirm('설정을 저장하시겠습니까? (구글 시트에 반영됩니다)')) return; try { const resp = await fetch(`${API_BASE}/config/update`, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(currentConfigData) }); const res = await resp.json(); if (res.success) { alert('설정이 저장되었습니다: ' + res.message); closeSettingsModal(); } else { alert('저장 실패: ' + res.message); } } catch (e) { alert('저장 요청 오류: ' + e); } }

// --- Local Display Settings ---
function _escId(s) { return s.replace(/[^a-zA-Z0-9가-힣]/g, '_'); }
function _escAttr(s) { return s.replace(/"/g, '&quot;'); }

// [v6.6.238] 필터링 모달: 글/댓글 서브탭 분리
let _currentLocalSubTab = 'posts'; // 'posts' | 'comments'

function _switchLocalSubTab(key) {
    _currentLocalSubTab = key;
    // 서브탭 버튼 active 토글
    document.querySelectorAll('#localSubTabs .settings-tab-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.localSub === key);
    });
    const postsPanel = document.getElementById('localPostsPanel');
    const commentsPanel = document.getElementById('localCommentsPanel');
    // [v6.7.741] 디스코드 서브탭(v6.7.412 채널별 표시 수) 삭제 - 디스코드 탭과 함께 지웠다.
    if (postsPanel) postsPanel.style.display = (key === 'posts') ? '' : 'none';
    if (commentsPanel) commentsPanel.style.display = (key === 'comments') ? '' : 'none';
    if (key === 'posts') _renderPostFilters();
    else if (key === 'comments') _renderCommentFilters();
}

// [v6.6.447] batchInput 표시값 결정 — 저장된 limits dict 에서 추론.
//   모든 게임 한도가 동일하면 그 값, 아니면 max 값 사용. limits 비어있으면 default (v6.7.133: 50 → 10).
function _inferBatchValue(limitsDict, defaultVal) {
    const vals = Object.values(limitsDict || {}).map(v => parseInt(v) || 0);
    if (!vals.length) return defaultVal;
    const allSame = vals.every(v => v === vals[0]);
    if (allSame) return vals[0];
    return Math.max.apply(null, vals);
}

// 글 탭: 게임별 visibility 토글 + limit 입력
function _renderPostFilters() {
    const container = document.getElementById('localPostsCards');
    if (!container || !_localSettingsData) return;
    const { games = [], limits = {}, visibility = {} } = _localSettingsData;
    // [v6.6.447] 하드코딩 50 폐지 — limits 값으로부터 추론하여 사용자 직전 일괄값 복원.
    const batchInput = document.getElementById('batchLimitInput');
    if (batchInput) batchInput.value = _inferBatchValue(limits, 10);
    let html = '';
    games.forEach(game => {
        const vis = visibility[game] !== undefined ? visibility[game] : true;
        // [v6.6.326 → v6.7.133] 개별 게임 기본 한도 100 → 50 → 10 (사용자 정책).
        const lim = limits[game] !== undefined ? limits[game] : 10;
        html += `<div class="local-settings-card"><label class="toggle-switch"><input type="checkbox" data-game="${_escAttr(game)}" class="vis-toggle"${vis ? ' checked' : ''} onchange="_onVisToggle(this)"><span class="toggle-slider"></span></label><span class="card-label${vis ? '' : ' dim'}" id="lsc-label-${_escId(game)}">${game}</span><input type="number" min="0" max="10000" step="50" value="${lim}" data-game="${_escAttr(game)}" class="lsc-limit-input" title="0 = 무제한"></div>`;
    });
    container.innerHTML = html;
    _attachWheelToNumberInputs(container);
    if (batchInput) _attachWheelToNumberInputs(batchInput.parentElement || batchInput);
}

// 댓글 탭: 게임별 visibility 토글 + limit 입력 (v6.6.352 — 글 탭과 동일 구조)
function _renderCommentFilters() {
    const container = document.getElementById('localCommentsCards');
    if (!container || !_localSettingsData) return;
    const { games = [] } = _localSettingsData;
    const commentVis = (_localSettingsData.commentVisibility) || {};
    const commentLimits = (_localSettingsData.commentLimits) || {};
    // [v6.6.447] 하드코딩 50 폐지 — commentLimits 값으로부터 추론. (v6.7.133: default 50 → 10)
    const batchInput = document.getElementById('batchCommentLimitInput');
    if (batchInput) batchInput.value = _inferBatchValue(commentLimits, 10);
    let html = '';
    games.forEach(game => {
        const vis = commentVis[game] !== undefined ? commentVis[game] : true;
        const lim = commentLimits[game] !== undefined ? commentLimits[game] : 10;
        html += `<div class="local-settings-card"><label class="toggle-switch"><input type="checkbox" data-game="${_escAttr(game)}" class="cvc-vis-toggle"${vis ? ' checked' : ''} onchange="_onCommentVisToggle(this)"><span class="toggle-slider"></span></label><span class="card-label${vis ? '' : ' dim'}" id="cvc-label-${_escId(game)}">${game}</span><input type="number" min="0" max="10000" step="50" value="${lim}" data-game="${_escAttr(game)}" class="cvc-limit-input" title="0 = 무제한"${vis ? '' : ' disabled'}></div>`;
    });
    container.innerHTML = html;
    _attachWheelToNumberInputs(container);
    if (batchInput) _attachWheelToNumberInputs(batchInput.parentElement || batchInput);
}

// [v6.6.447] number input wheel 증감 — focus 된 상태에서만 동작(스크롤 오작동 방지).
//   step 단위로 ± / min/max 클램프 / oninput 이벤트 dispatch 로 기존 핸들러 재사용.
function _attachWheelToNumberInputs(rootEl) {
    if (!rootEl) return;
    const inputs = rootEl.tagName === 'INPUT' ? [rootEl] : rootEl.querySelectorAll('input[type="number"]');
    inputs.forEach(el => {
        if (el.dataset._wheelBound === '1') return;
        el.dataset._wheelBound = '1';
        el.addEventListener('wheel', function (e) {
            // focus 시에만 동작 — hover 만으로는 페이지/모달 스크롤 우선.
            if (document.activeElement !== el) return;
            e.preventDefault();
            const step = parseInt(el.step) || 1;
            const min = el.min !== '' ? parseInt(el.min) : NaN;
            const max = el.max !== '' ? parseInt(el.max) : NaN;
            const cur = parseInt(el.value) || 0;
            const delta = e.deltaY < 0 ? step : -step;
            let next = cur + delta;
            if (!isNaN(min) && next < min) next = min;
            if (!isNaN(max) && next > max) next = max;
            el.value = next;
            el.dispatchEvent(new Event('input', { bubbles: true }));
        }, { passive: false });
    });
}

// 기존 _renderLocalSettings 는 서브탭 스위칭 엔트리포인트로 유지 (openFilteringModal 등 호환성)
function _renderLocalSettings() {
    _switchLocalSubTab(_currentLocalSubTab || 'posts');
}
function _onVisToggle(checkbox) { const game = checkbox.dataset.game; const label = document.getElementById(`lsc-label-${_escId(game)}`); const limitInput = document.querySelector(`.lsc-limit-input[data-game="${_escAttr(game)}"]`); if (label) label.className = 'card-label' + (checkbox.checked ? '' : ' dim'); if (limitInput) limitInput.disabled = !checkbox.checked; }
// [v6.6.238] 댓글 서브탭 visibility 토글 핸들러 (v6.6.352 — limit 입력도 함께 disable)
function _onCommentVisToggle(checkbox) {
    const game = checkbox.dataset.game;
    const label = document.getElementById(`cvc-label-${_escId(game)}`);
    const limitInput = document.querySelector(`.cvc-limit-input[data-game="${_escAttr(game)}"]`);
    if (label) label.className = 'card-label' + (checkbox.checked ? '' : ' dim');
    if (limitInput) limitInput.disabled = !checkbox.checked;
}
function onBatchLimitChange(value) { const v = parseInt(value) || 0; document.querySelectorAll('.lsc-limit-input').forEach(el => { el.value = v; }); }
// [v6.6.352] 댓글 탭 일괄 개수 적용
function onBatchCommentLimitChange(value) { const v = parseInt(value) || 0; document.querySelectorAll('.cvc-limit-input').forEach(el => { el.value = v; }); }

// [v6.7.98] 활성 탭 판정 helper — settings-modal.js 가 tab-manager/state.js 의
//   `let activeApps` 글로벌에 의존. 초기화 전 또는 missing 시 안전 fallback (모두 활성으로 간주 → 회귀 방지).
function _isAppActive(appId) {
    try {
        if (typeof activeApps === 'undefined' || !Array.isArray(activeApps)) return true;
        return activeApps.includes(appId);
    } catch (_) { return true; }
}

async function applyLocalSettings() {
    // 글 탭 수집
    const limits = {}, visibility = {};
    document.querySelectorAll('.lsc-limit-input').forEach(el => { limits[el.dataset.game] = parseInt(el.value) || 0; });
    document.querySelectorAll('.vis-toggle').forEach(el => { visibility[el.dataset.game] = el.checked; });
    // [v6.6.238] 댓글 탭 수집 (v6.6.352 — limit 추가)
    const commentVisibility = {}, commentLimits = {};
    document.querySelectorAll('.cvc-vis-toggle').forEach(el => { commentVisibility[el.dataset.game] = el.checked; });
    document.querySelectorAll('.cvc-limit-input').forEach(el => { commentLimits[el.dataset.game] = parseInt(el.value) || 0; });

    // [v6.6.235] localStorage 는 항상 저장 (개인 필터). 화면 즉시 갱신.
    _localSettingsData = { ..._localSettingsData, limits, visibility, commentVisibility, commentLimits };
    localStorage.setItem('imt_display_settings', JSON.stringify({ limits, visibility, commentVisibility, commentLimits }));

    // [v6.7.98] 모달 즉시 닫기 — 사용자 체감 latency 0. 이후 작업은 백그라운드.
    //   기존엔 fetch 6종(loadXxx) 직렬 await 후 toast 만 표시 → 모달이 1~3s hang.
    //   캐시 hit 인 탭 (feed/comments) 은 fetch 없이 재렌더만 수행 → 즉시 적용.
    //   비활성 탭은 호출 자체 skip → 네트워크 부담 0.
    if (typeof closeSettingsModal === 'function') closeSettingsModal();

    // [v6.7.98] 활성 탭만 갱신. legacy 단독 탭(total/games/community) 은 통합 환경에선
    //   `body.layout-cards` 카드 UI 가 비활성이라 사용자 사이드바 토글 시에만 활성.
    //   `feed` 가 활성이면 통합 환경 단일 스트림 → legacy 탭은 호출 안 됨 (사용자가 별도 활성 안 한 한).
    if (_isAppActive('games') && typeof loadGamesSections === 'function') loadGamesSections(null);
    if (_isAppActive('total') && typeof loadTotalArticles === 'function') loadTotalArticles(null);
    if (_isAppActive('community') && typeof loadCommunitySections === 'function') loadCommunitySections(null);
    // [v6.7.731] ai-analysis 는 visibility 필터 미적용(ai-tab.js:76) - 표시설정 저장 후 재fetch 가 무의미해 제거.
    //   대신 표시설정(가시 게임 집합)을 쓰는 ai-summary 는 재렌더 목록에 빠져 있어 추가한다(함수는 AI 요약 탭 영역이 제공, 가드 no-op).
    if (typeof window._rerenderAiSummary === 'function') window._rerenderAiSummary();

    // [v6.7.98] feed pane — 캐시 hit 시 fetch skip + renderFeedPane 직접 호출 (체감 0ms).
    //   _feedTotalCache 는 feed-tab.js module top-level let 글로벌 — typeof 가드.
    // [v6.7.491] 캐시 hit 이어도 **가시 게임 집합이 바뀌었으면 refetch** 한다.
    //   v6.7.172 의 `&games=` 도입으로 캐시 내용이 "어떤 게임으로 받아왔는가" 에 종속됐는데,
    //   위 v6.7.98 최적화는 그걸 모르고 무조건 재렌더했다 → 글로벌 캐시에 없는(활발한 게임에
    //   밀려난) 조용한 게임만 켜면 **0건**. 게임 집합이 그대로면(limits 만 조정 등) 기존대로 0ms 재렌더.
    if (_isAppActive('feed')) {
        const _feedStale = (typeof window._feedCacheIsStaleForFilters === 'function')
            ? window._feedCacheIsStaleForFilters() : false;
        if (!_feedStale && typeof _feedTotalCache !== 'undefined' && _feedTotalCache !== null && typeof renderFeedPane === 'function') {
            renderFeedPane();
        } else if (typeof loadFeedPane === 'function') {
            loadFeedPane();
        }
    }

    // [v6.7.98] comments pane — allComments 캐시 hit 시 fetch skip + renderComments 직접 호출.
    if (_isAppActive('comments')) {
        if (typeof allComments !== 'undefined' && Array.isArray(allComments) && allComments.length > 0 && typeof renderComments === 'function') {
            renderComments(allComments);
        } else if (typeof loadComments === 'function') {
            loadComments();
        }
    }

    // [v6.7.426] ai-summary pane - visibility 즉시 반영. 이 디스패치 목록에서 홀로 누락돼(소켓 재연결 목록
    //   v6.7.425 와 동형 갭) 필터 변경이 다음 "데이터 변화" 렌더까지 미적용이었음 - 게다가 _ingest 시그니처
    //   스킵(v6.7.330 P2)이 무변화 push 렌더를 걸러서 새 요약 도착(수 분+)까지 제외 게임이 잔존.
    //   filterAiSummary 가 STATE 캐시로 renderAiSummary 직접 재호출 (fetch 없음, 시그니처 게이트 비경유).
    if (_isAppActive('ai-summary') && typeof window.filterAiSummary === 'function') {
        try { window.filterAiSummary(); } catch (e) { console.warn('ai-summary 재렌더 실패', e); }
    }

    // [v6.7.451] 이상감지 pane - visibility 즉시 반영. 이 목록에서 홀로 누락돼 있었다(ai-summary 가
    //   v6.7.426 에서 겪은 것과 동형 갭). 이 탭은 폴링이 300초라 누락 시 체감 지연이 특히 크다.
    //   캐시(STATE.data)로만 재렌더 — fetch 0.
    if (_isAppActive('anomaly') && typeof window.anomalyTab_rerender === 'function') {
        try { window.anomalyTab_rerender(); } catch (e) { console.warn('anomaly 재렌더 실패', e); }
    }

    // [v6.7.628] 상황판 카드 재렌더 훅 2종(renderDashboardSituation / situationTab_rerender) 제거 - 카드 목록 은퇴.
    //   대시보드에 남은 그래프 2종은 아래 renderGraph7days 경로가 visibility 를 반영한다.
    // [v6.7.310] 대시보드 하단 7일 그래프도 visibility 즉시 반영 — 캐시 데이터 재렌더 (fetch 없음).
    //   캔버스가 DOM 에 없으면 _renderMount 가 자동 skip 하므로 pane 비활성 시에도 무해.
    if (typeof renderGraph7days === 'function' && typeof _graph7daysData !== 'undefined' && _graph7daysData) {
        try { renderGraph7days(); } catch (e) { console.warn('graph7days 재렌더 실패', e); }
    }
    // [v6.7.738] 통과율 그래프(게임별 모드)도 같은 visibility 를 쓴다 - 위 게시물 그래프만 다시 그리던 형제
    //   누락(PATTERN-72). 모듈 캐시로만 재렌더(fetch 0), 데이터·캔버스가 없으면 renderPassRate 가 바로 반환한다.
    if (typeof window.renderPassRate === 'function') {
        try { window.renderPassRate(); } catch (e) { console.warn('pass-rate 재렌더 실패', e); }
    }

    // 관리자 + 설정 모달 경로에서만 서버 전역 기본값까지 갱신 (글 탭 limits/visibility 만 — 댓글 visibility 는 서버 API 부재로 localStorage only).
    // [v6.7.98] 모달은 이미 닫혀있으므로 서버 응답은 toast/alert 으로만 통지.
    if (window._isAdmin && !_filteringOnlyMode) {
        try {
            const resp = await fetch(`${API_BASE}/config/display-settings`, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ limits, visibility }) });
            const res = await resp.json();
            if (res.success) showToast('필터가 적용되었습니다 (서버 기본값 갱신).');
            else showToast('서버 저장 실패: ' + res.message, true);
        } catch (e) { showToast('서버 저장 요청 오류: ' + e, true); }
    } else {
        showToast('필터가 내 화면에만 적용되었습니다.');
    }
    // [v6.7.377] 상단 필터링(개인 게임 on/off)을 이 기기의 푸시 구독에도 동기화 → 폰 푸시도 같은 필터.
    //   상단/설정 모달 어느 경로든 적용 시 반영. 구독 없으면 내부에서 조용히 스킵.
    try { if (window.imtNotifInbox && window.imtNotifInbox.syncPushFilter) window.imtNotifInbox.syncPushFilter(); } catch (_) {}
}
