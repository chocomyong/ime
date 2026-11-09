// ui/common.js — 기사 액션 + 검색 필터 + 공통 유틸 + 서머리 테이블 + CCU
// Phase 3 4차 분할 — 기존 static/js/ui.js 에서 이관

// --- Article Actions ---
function openArticle(link) { window.open(link, '_blank'); }

function openCommunityLink(url) {
    event.stopPropagation();
    if (url && url !== 'null' && url !== 'undefined') window.open(url, '_blank');
    else showToast("커뮤니티 주소가 없습니다.", true);
}

// [v6.7.731] ackAllAlert 삭제 - 호출처 0(레포 전체 grep, onclick/window[] 포함). 알림 ack UI 는 은퇴.

// --- Search Filters ---
function filterTotalArticles() {
    const headerInput = document.getElementById('total-search-input-header');
    const mainInput = document.getElementById('total-search-input');
    const filter = (headerInput ? headerInput.value : (mainInput ? mainInput.value : '')).toLowerCase();
    const body = document.getElementById('totalArticlesBody'); if (!body) return;
    let visibleRows = 0;
    body.querySelectorAll('tr').forEach(row => {
        const match = (row.textContent || row.innerText).toLowerCase().indexOf(filter) > -1;
        row.style.display = match ? "" : "none";
        if (match) visibleRows++;
    });
    // [v6.6.399] 검색 결과 0건 처리
    const emptyEl = document.getElementById('total-empty-state');
    if (window._tabHelpers && emptyEl) {
        window._tabHelpers.toggleEmptyState(emptyEl, visibleRows, filter.trim(), {
            empty: '최신글이 없습니다',
            searchMiss: '검색 결과가 없습니다 — 다른 키워드로 시도해보세요',
        });
    }
}
function filterTotalArticlesHeader() { filterTotalArticles(); }
// [v6.6.399] 디바운스된 검색 (150ms) — 큰 리스트에서 타이핑 lag 방지
window.filterTotalArticlesDebounced = (window._tabHelpers && window._tabHelpers.debounce)
    ? window._tabHelpers.debounce(filterTotalArticles, 150)
    : filterTotalArticles;

// [v6.7.731] renderTrendingKeywords 삭제 - 호출처 0. 트렌딩 키워드 컨테이너는 은퇴(통합 피드로 대체).

// --- Common Utilities ---
// [v6.7.731] _repositionToasts 삭제 - 호출처 0. showToast 가 v6.7.373 에 no-op 化되며 토스트 DOM 을 더 안 그린다.
function showToast(message, isError = false) {
    // [v6.7.373] 토스트 전면 비활성화 (사용자 요청 "토스트는 다 삭제" — PC·모바일 공통).
    //   호출처 ~70곳(링크복사·크롤러·저장·연결 등)은 그대로 두고 이 단일 관문만 no-op 化 —
    //   개별 삭제 대비 안전 + 한 줄 복원. 성공/오류 피드백 토스트도 함께 사라진다(오류만 살리려면
    //   아래를 `if (isError) { ...기존 렌더... }` 로 조정). 실패 추적용으로 콘솔에는 남긴다.
    try { (isError ? console.warn : console.debug)('[toast:muted]', message); } catch (_) {}
}
// [v6.7.731] 따옴표(" ')도 이스케이프 - 기존 textContent->innerHTML 방식은 &<> 만 바꾸고 따옴표를 남겨
//   title=·data-* 속성값에 쓰일 때 속성 탈출(injection)이 가능했다. 속성/텍스트 양쪽에서 안전한 치환 체인으로 교체
//   (& 를 먼저 치환해 이중 이스케이프 방지). 텍스트 문맥에선 &quot;/&#39; 가 브라우저에서 " '로 렌더돼 시각 변화 없음.
function escapeHtml(text) { if (typeof text !== 'string') return ''; return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;'); }

// [v6.7.731] 원문에서 매칭하고 조각마다 이스케이프한다. 구 코드는 이스케이프된 문자열 위에서 키워드를 찾아
//   'amp'·'quot'·'39' 같은 키워드가 엔티티 안쪽에 <mark> 를 꽂아 화면이 깨졌고, 'mark'·'class' 키워드는
//   앞서 넣은 태그 안을 다시 매칭했다. 키워드는 한 정규식(긴 것 우선)으로 한 번에 찾는다.
function highlightKeywords(title, detectedKeywords, isSuper) {
    if (typeof title !== 'string') return '';
    if (!detectedKeywords) return escapeHtml(title);
    const kwStr = typeof detectedKeywords === 'string' ? detectedKeywords : String(detectedKeywords);
    const keywords = kwStr.split(',').map(k => k.trim()).filter(k => k.length > 0);
    if (keywords.length === 0) return escapeHtml(title);
    const hlClass = isSuper ? 'kw-highlight kw-highlight-super' : 'kw-highlight';
    const re = new RegExp(keywords.sort((a, b) => b.length - a.length).map(k => k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|'), 'gi');
    let out = '', last = 0, m;
    while ((m = re.exec(title)) !== null) {
        out += escapeHtml(title.slice(last, m.index)) + `<mark class="${hlClass}">${escapeHtml(m[0])}</mark>`;
        last = m.index + m[0].length;
    }
    return out + escapeHtml(title.slice(last));
}

/**
 * [v6.6.412] 디시 스타일 시간 포맷.
 *  - 오늘   → "HH:MM"  (예: "04:52")
 *  - 어제 이전 → "MM.DD" (예: "04.25")  ← 5자 동일, 칼럼 초과 없음
 *  - 입력 형식 호환: ISO 문자열 / Date 객체 / "HH:MM:SS" / "HH:MM" 부분 문자열
 *    - 날짜 정보가 없는 부분 문자열은 시간만 표시 (오늘/어제 판별 불가).
 *
 * 공식 사용처: 전체/게임/댓글/커뮤니티 탭 + AI 분석 탭 + 상황판 드로어.
 * (이전 형식 "MM-DD HH:MM" 8자는 폐기 — 칼럼 초과 우려 + 디시 친화 표기 통일)
 */
function formatTime(timestamp) {
    if (!timestamp) return '-';
    try {
        const pad = (n) => String(n).padStart(2, '0');
        // 시간만 있는 부분 문자열 우선 처리 (예: "08:30:00")
        const hhmmssOnly = String(timestamp).match(/^(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?$/);
        if (hhmmssOnly) {
            return `${pad(hhmmssOnly[1])}:${pad(hhmmssOnly[2])}`;
        }
        const d = new Date(timestamp);
        if (isNaN(d.getTime())) return timestamp;
        const now = new Date();
        const hhmm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
        if (d.toDateString() === now.toDateString()) return hhmm;
        // 어제 이전 — 디시 스타일 MM.DD
        return `${pad(d.getMonth() + 1)}.${pad(d.getDate())}`;
    } catch (e) { return timestamp; }
}

/**
 * [v6.7.667] 피드·AI요약 전용 시간 포맷 - 오늘이 아니면 **날짜와 시각을 같이** 보인다.
 *  - 오늘     -> "HH:MM"        (예: "04:52")
 *  - 어제 이전 -> "MM.DD HH:MM"  (예: "09.14 04:16")
 *
 * `formatTime` 을 안 고치는 이유: 그 함수는 8개 탭이 공유하는데(전체·게임·댓글·커뮤니티·
 * AI분석·상황판 포함) 주석이 *"MM-DD HH:MM 8자는 칼럼 초과 우려로 폐기"* 라고 이미
 * 한 번 되돌린 이력을 들고 있다. 같은 자리를 다시 밟지 않으려고 **쓰는 탭만** 넓힌다.
 * 사용자 요청(2026-09-15): *"언제 작성된 글인지 보려면 펼쳐야 돼서 불편하다."*
 *
 * 칼럼 폭은 쓰는 쪽 CSS 가 같이 넓혀야 한다(.feed-data-table .col-time / .ai-summary-data-table .col-time).
 */
function formatTimeFull(timestamp) {
    if (!timestamp) return '-';
    try {
        const pad = (n) => String(n).padStart(2, '0');
        // 시간만 있는 부분 문자열은 날짜를 알 수 없다 - formatTime 과 같은 규약.
        const hhmmssOnly = String(timestamp).match(/^(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?$/);
        if (hhmmssOnly) return `${pad(hhmmssOnly[1])}:${pad(hhmmssOnly[2])}`;
        const d = new Date(timestamp);
        if (isNaN(d.getTime())) return timestamp;
        const hhmm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
        if (d.toDateString() === new Date().toDateString()) return hhmm;
        return `${pad(d.getMonth() + 1)}.${pad(d.getDate())} ${hhmm}`;
    } catch (e) { return timestamp; }
}

// [v6.7.731] processArticleKeywords 삭제 - 호출처 0. 키워드 표시는 highlightKeywords 경로로 일원화됨.

async function copyTextToClipboard(text) {
    if (!text) return false;
    try {
        if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return true; }
        else { const ta = document.createElement("textarea"); ta.value = text; ta.style.position = "fixed"; ta.style.left = "-9999px"; document.body.appendChild(ta); ta.focus(); ta.select(); const ok = document.execCommand('copy'); document.body.removeChild(ta); if (!ok) throw new Error('Copy failed'); return true; }
    } catch (err) { showToast('클립보드 복사 실패: ' + err.message, true); return false; }
}

async function downloadExcelViaFetch(url, defaultFilename, btnId, loadingText = null) {
    const btn = document.getElementById(btnId); let originalText = '';
    if (btn) { originalText = btn.textContent; btn.disabled = true; if (loadingText) btn.textContent = loadingText; }
    try {
        const response = await fetch(url); if (!response.ok) throw new Error(`서버 응답 오류: ${response.status}`);
        const blob = await response.blob(); const downloadUrl = window.URL.createObjectURL(blob);
        const a = document.createElement('a'); a.href = downloadUrl;
        const cd = response.headers.get('Content-Disposition');
        a.download = cd && cd.includes('filename=') ? cd.split('filename=')[1].replace(/"/g, '') : defaultFilename;
        document.body.appendChild(a); a.click(); a.remove(); window.URL.revokeObjectURL(downloadUrl);
    } catch (error) { showToast(`다운로드 실패: ${error.message}`, true); }
    finally { if (btn) { btn.disabled = false; if (loadingText) btn.textContent = originalText; } }
}

async function postWithToast(url, btnId, loadingText = '요청 중...') {
    const btn = document.getElementById(btnId); let originalText = '';
    if (btn) { originalText = btn.textContent; btn.disabled = true; btn.textContent = loadingText; }
    try { const resp = await fetch(url, { method: 'POST' }); const result = await resp.json(); if (resp.ok) { showToast(result.message || '요청이 접수되었습니다.'); } else { throw new Error(result.message || '알 수 없는 오류'); } }
    catch (error) { showToast(`요청 실패: ${error.message}`, true); }
    finally { if (btn) { btn.disabled = false; btn.textContent = originalText; } }
}

// --- Summary Table ---
function renderSummaryTable(containerId, summaryData, orderList = []) {
    const container = document.getElementById(containerId); if (!container) return;
    let games = (orderList && orderList.length > 0) ? orderList : Object.keys(summaryData || {});
    if (games.length === 0) { container.style.display = 'none'; container.innerHTML = ''; return; }
    container.style.display = 'block';
    const getSummaryEntry = (gameName) => { if (!summaryData) return null; if (summaryData[gameName]) return summaryData[gameName]; const ct = gameName.replace(/\s+/g, '').toLowerCase(); for (const key in summaryData) { if (key.replace(/\s+/g, '').toLowerCase() === ct) return summaryData[key]; } return null; };
    let label1 = '1,000 다이아 기준', label2 = '거래 완료 건수';
    // [v6.7.418] null 가드 — orderList(game_list) 는 있는데 summaryData(summary) 가 null 인 경우
    //   (예: tasa 크롤 진행 중 게임목록만 먼저 도착) line 168 이 orderList 로 games 채워 169 early-return 을
    //   건너뛰고 여기서 Object.values(null) 크래시(diamond-tab pollTasaStatus 콘솔 스팸). getSummaryEntry(171)는 이미 가드.
    const sampleEntry = Object.values(summaryData || {}).find(e => e && e.label1);
    if (sampleEntry) { label1 = sampleEntry.label1; label2 = sampleEntry.label2; }
    let html = '<div class="summary-table-wrapper" style="overflow-x: auto;"><table class="summary-table" style="width: 100%; min-width: 600px; text-align: center; border-collapse: collapse; table-layout: fixed;"><colgroup><col style="width: 160px;">';
    const gameColWidth = Math.floor(840 / Math.max(games.length, 1));
    games.forEach(() => { html += `<col style="width: ${gameColWidth}px;">`; });
    html += '</colgroup><thead><tr><th style="width: 160px; text-align: center;">구분</th>';
    games.forEach(game => { html += `<th style="text-align: center;">${escapeHtml(game)}</th>`; });
    html += '</tr></thead><tbody><tr><td style="white-space: normal; vertical-align: middle; line-height: 1.5; text-align: center;"><div style="padding: 4px 0;">' + label1 + '</div><button class="copy-row-btn" style="margin: 4px 0 2px;" onclick="copyRowValues(this)">복사</button></td>';
    games.forEach(game => { const d = getSummaryEntry(game); let val = '-'; if (d && d.price_per_1k !== undefined && d.price_per_1k !== null) { val = (d.price_per_1k === '-' || d.price_per_1k === 0) ? '-' : d.price_per_1k.toLocaleString(); } html += `<td style="text-align: center; color: ${d ? 'inherit' : '#666'};">${val}</td>`; });
    html += '</tr><tr><td style="white-space: normal; vertical-align: middle; line-height: 1.5; text-align: center;"><div style="padding: 4px 0;">' + label2 + '</div><button class="copy-row-btn" style="margin: 4px 0 2px;" onclick="copyRowValues(this)">복사</button></td>';
    games.forEach(game => { const d = getSummaryEntry(game); let val = '0'; if (d && d.total_count !== undefined) { val = d.total_count === '-' ? '-' : d.total_count.toLocaleString(); } html += `<td style="text-align: center; color: ${d ? 'inherit' : '#666'};">${val}</td>`; });
    html += '</tr></tbody></table></div>';
    container.innerHTML = html;
}
async function copyRowValues(btn) { const row = btn.closest('tr'); if (!row) return; const text = Array.from(row.querySelectorAll('td')).slice(1).map(td => td.innerText.trim()).join('\t'); if (await copyTextToClipboard(text)) showToast('행 데이터가 복사되었습니다.'); }

// [v6.7.191 FEAT-01] CCU 액션 함수 폐기 — CCU 탭 마크업 제거와 동반.
