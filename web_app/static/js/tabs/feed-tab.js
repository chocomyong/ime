// tabs/feed-tab.js — 통합 모니터링 환경 피드 pane (단일 스트림)
// [v6.6.424] 단순화 — 게임별/커뮤니티별 세그먼트 폐지. 단일 [전체] 스트림만.
//   폭주 게임(예: 우마무스메 디시) 점유 문제는 사용자가 사이드바 설정의 limits[게임]=N 으로 해결.
// [v6.7.16] PC 표 ↔ 모바일 카드 토글 — webview_app v7.0.0-alpha.15 등가.
//   localStorage `imt_layout_mode_feed` ∈ {'table', 'cards'} (default 'cards').

let _feedTotalCache = null;
let _feedPrevLinks = new Set();
// [v6.7.738] 마지막 렌더가 센 '전체 건수'. 검색 중에는 헤더가 '보이는 개수' 를 쓰고, 검색을 비우면
//   이 값으로 되돌린다 - 무한 스크롤이라 DOM 카드 수로는 전체를 복원할 수 없다(승인 개선 #10).
let _feedRenderCount = 0;

// [v6.7.407] 도착 잔광 — 구 2s 플래시(feed-card--arriving) + freshness 엣지스트립 대체.
//   링크별 "화면 첫 관측 시각"을 Map 에 기록 → 렌더마다 경과초를 negative animation-delay 로 부여해
//   소켓 재렌더에도 이어서 페이드(imt-arrival-glow, both). 색 cyan / 지속 60s ([v6.7.410] 전탭 절반 감축 120→60).
//   AI 요약(STATE._arrivalAt) / 디스코드 선례 동형. Map 은 record 단계에서 만료·이탈 정리 → 무한 성장 방지.
const _FEED_GLOW_S = 60;
const _FEED_GLOW_RGB = '37, 99, 235';   // [v6.7.422] cyan(34,211,238)→blue-600 — cyan 은 고휘도라 흰 글씨 가독 저하(사용자). 저휘도 파랑으로 대비 확보, 파랑 정체성 유지.
let _feedArrivalAt = new Map();           // link → 최초 관측 ms
function _recordFeedArrivals(newLinks, prevLinks) {
    const now = Date.now();
    for (const [l, t0] of _feedArrivalAt) {   // 피드에서 사라졌거나 120s 초과 → 제거
        if (!newLinks.has(l) || (now - t0) / 1000 >= _FEED_GLOW_S) _feedArrivalAt.delete(l);
    }
    if (prevLinks.size === 0) return;         // 첫 렌더 전량 신규 판정 방지 (구 arrival 가드 동일)
    newLinks.forEach(l => { if (!prevLinks.has(l) && !_feedArrivalAt.has(l)) _feedArrivalAt.set(l, now); });
}
function _applyFeedGlow(container) {
    if (!container) return;
    const now = Date.now();
    container.querySelectorAll('.feed-card[data-href], .feed-data-table tbody tr[data-href]').forEach(el => {
        const t0 = _feedArrivalAt.get(el.dataset.href);
        if (t0 == null) return;
        const ageS = (now - t0) / 1000;
        if (ageS >= _FEED_GLOW_S) return;
        el.classList.add('imt-arrival-glow');
        el.style.setProperty('--glow-rgb', _FEED_GLOW_RGB);
        el.style.animationDuration = _FEED_GLOW_S + 's';
        el.style.animationDelay = '-' + ageS.toFixed(2) + 's';
    });
}

// [v6.7.317] 더보기(상세 확장) — 카드/표/분류 6형식 공통. 키 = link. 재렌더(소켓 push)에도 유지
//   (AI 요약 탭 STATE._expanded 선례). 상세 = 게임/구분/제목/일시(전체)/본문/링크+복사.
let _feedExpandedKeys = new Set();
const _FEED_DETAIL_CONTENT_CAP = 4000;  // 본문 표시 상한 (초장문 글 DOM 방어)

// 본문 미수집 판정 — content_extractor 지연 파이프라인: 신규 글 content='' + sentinel
//   '[FAILED:N]'/'[FAILED]'/'[NOT_NOTICE]'/'[DELETED]' (전체 문자열이 단일 브라켓 마커일 때만).
function _feedContentPending(c) {
    const s = String(c || '').trim();
    if (!s) return true;
    return /^\[[A-Z_]+(:\d+)?\]$/.test(s);
}

// 일시 전체 표기 "YYYY-MM-DD HH:MM" — formatTime(오늘 HH:MM / 이전 MM.DD)의 상세 확장판.
function _feedFullTs(ts) {
    if (!ts) return '-';
    const d = new Date(String(ts).replace(' ', 'T'));
    if (isNaN(d.getTime())) return String(ts);
    const p = (n) => String(n).padStart(2, '0');
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}

// 더보기 버튼 — 카드 meta 행 끝 / 표 col-more 셀 공용. 링크 없는 행은 상세 열람 가치 없음 → 미노출.
function _feedMoreBtnHtml(link) {
    if (!link) return '';
    return '<button type="button" class="feed-more-btn" title="상세 보기" aria-label="상세 보기" onclick="event.stopPropagation(); toggleFeedDetail(this)"><i class="fas fa-chevron-down" aria-hidden="true"></i></button>';
}

// ====================================================================
// [v6.7.664] 제재 판정 표식 - post_verdicts 조인 결과(verdict_kind / verdict_target)
// ====================================================================
// **레인이 둘이고 기대치가 다르다** (사용자 2026-09-15 실무 기준):
//   act(조치) 개인정보·불법거래·홍보 = 예외 없는 삭제 대상. 판정이 곧 결론에 가깝다.
//   rev(검토) 욕설·비방        = 사람이 맥락을 본다. "게임사의 잘못 등으로 삭제가 안 되는
//                                 경우도 많음" - 그래서 기계가 삭제를 결정하지 않는다.
// 그래서 두 레인은 **색을 달리한다**. 한 색으로 묶으면 물량이 많은 rev 가 act 신호를 덮는다
// (디코 실측: 욕설+비방이 전체 flag 의 69%).
//
// **빨간 테두리를 안 쓴 이유** - 피드탭의 빨강(#ef4444)은 이미 `.feed-card-source--official_web`
// (공식 배지, v6.7.109 사용자 요청)이 "중요 출처, 반드시 봐라"로 쓰고 있다. 판정 대상이 공식
// 게시물뿐이라 **판정된 카드는 100% 그 배지를 달고 있고**, 같은 빨강이 "봐라"와 "버려라"를 동시에
// 뜻하게 된다. 그래서 색이 아니라 **형태(좌측 바)** 를 새 축으로 쓰고, 표식이 붙은 카드에서는
// 중복 정보인 공식 배지를 생략한다(_feedSourceBadgeHtml).
const FEED_VERDICT_LANE = {
    // [v6.7.670] 도배 누락 수정 - 판정 잡 `post_judge.ACT_KINDS` 에는 처음부터 있었는데
    //   이 표에만 빠져 있었다. 결과는 **조용한 소실**이다: 서버 제재 필터(`kind <> 'clean'`)는
    //   도배를 목록에 넣는데 `_feedVerdictKind` 가 미지 라벨로 보고 표식을 안 붙여, 제재 목록에
    //   근거 없는 카드로 섞여 나왔다. 라이브 실측 2026-09-15: act 79건 중 **64건(81%)이 도배** -
    //   핵심 신호가 통째로 안 보이고 있었다. 아래 ASSERT 가 이 표와 판정 잡의 드리프트를 잠근다.
    '개인정보': 'act', '불법거래': 'act', '홍보': 'act', '도배': 'act',
    '욕설': 'rev', '비방': 'rev',
};
function _feedVerdictKind(a) {
    const k = String((a && a.verdict_kind) || '').trim();
    if (!k || k === 'clean') return '';
    return FEED_VERDICT_LANE[k] ? k : '';   // 미지의 라벨은 표식 없음 - 모르는 값을 강조로 승격하지 않는다
}
function _feedVerdictCls(a) {
    const k = _feedVerdictKind(a);
    return k ? (' feed-flagged feed-flagged--' + FEED_VERDICT_LANE[k]) : '';
}
// [v6.7.666] **판정 대기 표식.** "검토했고 문제없음(clean)" 과 "아직 안 봄" 이 화면에서
//   똑같이 보이던 것을 가른다 - 사용자가 두 번 물었고(2026-09-15), 리더에게 "N건 중 M건" 을
//   보이려면 분모가 보여야 한다. **표식은 소수인 '대기' 쪽에 붙인다** - 다수인 clean 에
//   붙이면 거의 전건이 표시돼 신호가 죽는다.
// [v6.7.738] **대기 여부는 서버가 정한다**(`judge_pending`, routes/articles.py). 판정 잡의 후보 술어
//   (post_judge._candidate_where - 유저 게시판·최근 7일·실패 한도)를 그대로 쓴다. 종전 클라 판정
//   ("공식이고 판정 없음")은 잡이 영영 안 보는 공지·소식·패치노트·이벤트와 7일 지난 글에도 점을 찍었다.
function _feedJudgePending(a) {
    return !!(a && a.judge_pending === true);
}

function _feedVerdictChipHtml(a) {
    const k = _feedVerdictKind(a);
    if (!k) {
        return _feedJudgePending(a)
            ? '<span class="feed-judge-pending" title="제재 판정 대기"></span>' : '';
    }
    const lane = FEED_VERDICT_LANE[k];
    const tgt = String((a && a.verdict_target) || '').trim();
    // target 은 욕설/비방에서만 의미를 갖는다 - 사람이 "게임사 대상" 을 3초에 거르라고 있는 축이다.
    const tgtHtml = (lane === 'rev' && tgt && tgt !== 'none')
        ? '<span class="feed-flag-target">' + escapeHtml(tgt) + '</span>' : '';
    return '<span class="feed-flag-chip feed-flag-chip--' + lane + '" title="삭제 대상 판정">'
        + escapeHtml(k) + tgtHtml + '</span>';
}
// [v6.7.731] 출처 라벨 - 표/카드 3곳의 동일 매핑을 1곳으로. (상세(_feedDetailInnerHtml)는 미지
//   출처에 '' 를 쓰는 변형이라 별도 유지 - 합치면 미지 출처의 출력이 바뀐다.)
function _feedSourceLabel(upper) {
    return upper === 'DAUM' ? '다음' : (upper === 'OFFICIAL_WEB' ? '공식' : (upper === 'NAVER' ? '네이버' : 'DC'));
}
// 출처 배지 - 판정 표식이 붙었으면 생략한다(위 주석의 빨강 중복 회피). 4개 렌더 경로 공용.
function _feedSourceBadgeHtml(a, sourceUpper, sourceText, extraCls) {
    if (_feedVerdictKind(a)) return '';
    return '<span class="feed-card-source ' + (extraCls || ('feed-card-source--' + sourceUpper.toLowerCase()))
        + '">' + sourceText + '</span>';
}

function _feedDetailInnerHtml(a) {
    const link = a.link || '';
    let contentHtml;
    if (_feedContentPending(a.content)) {
        contentHtml = '<span class="feed-detail-pending">본문 수집 중 — 수집되면 자동 표시됩니다</span>';
    } else {
        let c = String(a.content).trim();
        if (c.length > _FEED_DETAIL_CONTENT_CAP) c = c.slice(0, _FEED_DETAIL_CONTENT_CAP) + ' … (이하 생략)';
        contentHtml = escapeHtml(c);
    }
    // [v6.7.319] 내용/링크도 그리드 행 편입 — 라벨-값 들여쓰기 6필드 전부 정렬 (사용자 피드백, AI 요약 탭 구조 동일).
    const linkVal = link
        ? '<span class="feed-detail-val feed-detail-linkrow">'
            + '<a class="feed-detail-link" href="' + escapeHtml(link) + '" target="_blank" rel="noopener">' + escapeHtml(link) + '</a>'
            + '<button type="button" class="feed-copy-btn" data-link="' + escapeHtml(link) + '" onclick="copyFeedLink(this)">링크 복사</button>'
            + '</span>'
        : '<span class="feed-detail-val">-</span>';
    // [v6.7.324] 순서 AI 요약 탭 통일 + 구분(source) → 게시판(board_name) 교체, 게시판을 제목 위 (사용자 요청).
    // [v6.7.325] 게시판 값에 출처 prefix — "다음 · 자유 게시판" / "공식 · 배그 플레이어" (AI 상세 동일 규칙).
    const _sU = (a.source_community || a.source || '').toUpperCase();
    const _sT = _sU === 'DAUM' ? '다음' : (_sU === 'OFFICIAL_WEB' ? '공식' : (_sU === 'NAVER' ? '네이버' : (_sU === 'DC' ? 'DC' : '')));
    const _board = String(a.board_name || '').trim();
    const _boardVal = (_sT && _board) ? (_sT + ' · ' + _board) : (_sT || _board || '-');
    // [v6.7.459] AI 요약 행 — admin 에게만. 상태는 온디맨드 조회(_feedAiFetch)라 최초엔 placeholder.
    //   피드 payload 에 요약을 조인하지 않는 이유: /api/articles/all 은 500건 1.22MB 라 여기에
    //   요약을 얹으면 PATTERN-29/41 경로가 다시 열린다. 상세는 사용자가 펼칠 때만 열리므로 1건 조회가 맞다.
    const aiRow = window._isAdmin
        ? '<span class="feed-detail-lbl">AI 요약</span>'
            + '<span class="feed-detail-val feed-detail-ai" data-ai-link="' + escapeHtml(link) + '">'
            + (link ? '<span class="feed-ai-pending">확인 중…</span>' : '-')
            + '</span>'
        : '';
    // [2026-09-16] **제목이 원본에서 바뀌었으면 말한다.** 의미 있는 제목을 `..` 같은 무의미
    //   문자열로 바꾸는 것은 글을 묻으려는 행동이라 그 자체가 신호다 - 덮어쓰고 조용하면
    //   게시판에서 본 글을 여기서 못 찾는다(실사고 2026-09-16, POE1 도움말 4005967).
    //   `title_prev` 는 **최초 제목**이다(그 컬럼 주석). 판정 컬럼과 같은 경로로 실린다.
    const _prevT = String(a.title_prev || '').trim();
    const titleChangedRow = (_prevT && _prevT !== String(a.title || '').trim())
        ? '<span class="feed-detail-lbl">제목 변경</span>'
            + '<span class="feed-detail-val feed-detail-titlechg">'
            + escapeHtml(_prevT) + ' <span class="feed-titlechg-arrow">-&gt;</span> '
            + escapeHtml(String(a.title || '')) + '</span>'
        : '';
    return '<div class="feed-detail-grid">'
        + '<span class="feed-detail-lbl">게임</span><span class="feed-detail-val">' + escapeHtml(a.game || a.game_name || '-') + '</span>'
        + '<span class="feed-detail-lbl">일시</span><span class="feed-detail-val">' + escapeHtml(_feedFullTs(a.timestamp || a.date)) + '</span>'
        + '<span class="feed-detail-lbl">게시판</span><span class="feed-detail-val">' + escapeHtml(_boardVal) + '</span>'
        + '<span class="feed-detail-lbl">제목</span><span class="feed-detail-val">' + escapeHtml(a.title || '-') + '</span>'
        + '<span class="feed-detail-lbl">내용</span><span class="feed-detail-val feed-detail-content">' + contentHtml + '</span>'
        + titleChangedRow
        + aiRow
        + '<span class="feed-detail-lbl">링크</span>' + linkVal
        + '</div>';
}

// ====================================================================
// [v6.7.459] 수동 AI 요약 요청 — 상세 안의 'AI 요약' 행
// ====================================================================
// 4상태(summarized / excluded / unanalyzed / no_content)를 서버가 판정해 내려주고 여기선 렌더만 한다.
// 상태 캐시를 두는 이유: 상세는 소켓 재렌더(raw_articles_update)마다 다시 그려지는데, 그때마다
// 재조회하면 크롤 사이클 주기로 불필요한 요청이 나간다. 요청 결과도 같은 캐시에 눌러써서
// 재렌더 후에도 방금 받은 요약이 그대로 보이게 한다(PATTERN-38 계열 — 재렌더가 상태를 지우는 문제).
const _feedAiCache = new Map();   // link -> {state, summary, gemma_verdict, feedback_id}
const _feedAiInflight = new Set();  // 진행 중인 조회 (재렌더 중복 요청 차단)

function _feedAiLabelChipsHtml(fid) {
    if (!fid) return '';
    const chips = [['patch', '패치'], ['maintenance', '점검'], ['issue', '이슈'], ['sentiment', '여론'], ['etc', '기타']];
    return '<div class="feed-ai-chips" data-fid="' + fid + '">'
        + '<span class="feed-ai-chips-lbl">왜 요약했나 (선택)</span>'
        + chips.map(c => '<button type="button" class="feed-ai-chip" data-label="' + c[0] + '"'
            + ' onclick="feedAiLabel(this)">' + c[1] + '</button>').join('')
        + '</div>';
}

function _feedAiStateHtml(st) {
    if (!st) return '<span class="feed-ai-pending">확인 중…</span>';
    if (st.state === 'error') {
        // 실패도 '상태'로 캐시한다 — 지우면 재렌더(소켓 800ms 디바운스)마다 재요청이 나가 폭주한다.
        // 재시도는 사용자가 상세를 다시 펼칠 때만(_feedAiFetch force).
        return '<span class="feed-ai-none">AI 상태 조회 실패 — 상세를 다시 펼치면 재시도합니다</span>';
    }
    if (st.state === 'no_content') {
        return '<span class="feed-ai-none">본문 미수집 — 요약 불가</span>';
    }
    if (st.state === 'summarized') {
        return '<span class="feed-ai-summary">' + escapeHtml(st.summary || '') + '</span>'
            + '<button type="button" class="feed-ai-btn feed-ai-btn--again" data-link="' + escapeHtml(st.link || '') + '"'
            + ' onclick="feedAiRequest(this)">다시 요약</button>'
            + _feedAiLabelChipsHtml(st.feedback_id);
    }
    // [v6.7.460] 방금 요청했는데 모델이 끝내 거절한 경우 — 요청 버튼으로 되돌리지 않는다.
    //   되돌리면 사용자가 같은 요청을 반복하고 그때마다 Gemini 콜이 나간다(수동 예산 낭비).
    //   라벨은 이미 result='excluded' 로 남았으므로 프롬프트 개선 표본으로는 수집된 상태다.
    if (st.declined) {
        return '<span class="feed-ai-none">AI 가 요약 가치 없음으로 재판정했습니다 (요청은 라벨로 기록됨)</span>'
            + (st.summary ? '<span class="feed-ai-summary">' + escapeHtml(st.summary) + '</span>' : '')
            + _feedAiLabelChipsHtml(st.feedback_id);
    }
    // excluded / unanalyzed — 요청 버튼
    const why = st.state === 'excluded'
        ? '<span class="feed-ai-none">1차에서 요약 대상 아님으로 분류됨</span>'
        : '<span class="feed-ai-none">아직 요약되지 않음</span>';
    return why
        + '<button type="button" class="feed-ai-btn" data-link="' + escapeHtml(st.link || '') + '"'
        + ' onclick="feedAiRequest(this)">AI 요약 요청</button>';
}

function _feedAiRender(link) {
    const st = _feedAiCache.get(link);
    document.querySelectorAll('.feed-detail-ai[data-ai-link="' + CSS.escape(link) + '"]').forEach(el => {
        el.innerHTML = _feedAiStateHtml(st);
    });
}

// 재렌더 후 AI 행 복원. 재렌더는 상세를 placeholder('확인 중…') 상태로 다시 굽기 때문에, 이걸
// 안 걸면 크롤 사이클마다 방금 받은 요약이 사라진다(PATTERN-38 (5) 와 같은 계열의 상태 소실).
// 호출 지점을 `_reapplyFeedSearch` 와 **같은 자리**로 맞춘 이유: 그 목록이 v6.7.443 에서 누락
// 경로(소켓 디바운스 + 무한스크롤 batch)까지 실측으로 감사된 목록이라, 새 훅을 따로 배선하면
// 같은 누락을 처음부터 다시 겪는다.
function _feedAiRepaint() {
    if (!window._isAdmin) return;
    document.querySelectorAll('.feed-detail-ai[data-ai-link]').forEach(el => {
        const link = el.dataset.aiLink || '';
        if (!link) return;
        if (_feedAiCache.has(link)) el.innerHTML = _feedAiStateHtml(_feedAiCache.get(link));
        else _feedAiFetch(link);   // 캐시 없는 채로 baked 된 상세(재렌더 중 펼침 유지) 보충
    });
}

// 상세 펼침 시 1건 상태 조회. 캐시 hit 이면 요청 없이 렌더만.
//   force=true (사용자가 직접 펼친 경로) 면 실패 캐시만 걷어내고 재시도한다. 성공 상태는 그대로
//   재사용해 불필요한 요청을 막는다.
async function _feedAiFetch(link, force) {
    if (!link || !window._isAdmin) return;
    if (force && (_feedAiCache.get(link) || {}).state === 'error') _feedAiCache.delete(link);
    if (_feedAiCache.has(link)) { _feedAiRender(link); return; }
    // in-flight 중복 요청 방지 — 재렌더가 응답 전에 또 들어와도 요청은 1개만 나간다.
    if (_feedAiInflight.has(link)) return;
    _feedAiInflight.add(link);
    try {
        const resp = await fetch(`${API_BASE}/ai-summary/for-link?link=` + encodeURIComponent(link));
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const d = await resp.json();
        d.link = link;
        _feedAiCache.set(link, d);
    } catch (e) {
        // 실패를 캐시한다 — 지우면 재렌더마다 재요청이 나가 요청 폭주가 된다(PATTERN-40 의 '403 폴링 정지' 정신).
        _feedAiCache.set(link, { link, state: 'error' });
    } finally {
        _feedAiInflight.delete(link);
    }
    _feedAiRender(link);
}

window.feedAiRequest = async function (btn) {
    const link = (btn && btn.dataset && btn.dataset.link) || '';
    if (!link) return;
    btn.disabled = true;
    btn.textContent = '요약 중…';
    try {
        const resp = await fetch(`${API_BASE}/ai-summary/request`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ link })
        });
        const d = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            // 429(한도)·503(예산)·422(본문없음) 전부 사유를 그대로 보여준다 — '실패' 한 단어로
            // 뭉뚱그리면 사용자가 재시도 여부를 판단할 수 없다.
            const prev = _feedAiCache.get(link) || { link, state: 'unanalyzed' };
            if (d.feedback_id) prev.feedback_id = d.feedback_id;   // 예산 실패여도 라벨은 남았다
            _feedAiCache.set(link, prev);
            _feedAiRender(link);
            document.querySelectorAll('.feed-detail-ai[data-ai-link="' + CSS.escape(link) + '"]').forEach(el => {
                el.insertAdjacentHTML('beforeend',
                    '<span class="feed-ai-err">' + escapeHtml(d.error || `요청 실패 (HTTP ${resp.status})`) + '</span>');
            });
            return;
        }
        // [v6.7.460] 상태는 서버가 확정해 내려준다(추정 금지). force 로도 모델이 '분석 제외' 를
        //   고집하면 state='excluded' 로 오는데, 그때 '요청' 버튼으로 되돌리면 사용자가 같은 요청을
        //   반복해 쿼타만 태운다 — 거절 사실을 그대로 보여준다.
        _feedAiCache.set(link, {
            link, state: d.state || 'summarized', summary: d.summary || '',
            gemma_verdict: d.gemma_verdict || '', feedback_id: d.feedback_id || null,
            declined: d.state === 'excluded',
        });
        _feedAiRender(link);
    } catch (e) {
        btn.disabled = false;
        btn.textContent = 'AI 요약 요청';
        console.error('AI 요약 요청 실패:', e);
    }
};

window.feedAiLabel = async function (btn) {
    const wrap = btn.closest('.feed-ai-chips');
    const fid = wrap && wrap.dataset ? wrap.dataset.fid : '';
    const label = (btn && btn.dataset && btn.dataset.label) || '';
    if (!fid || !label) return;
    try {
        const resp = await fetch(`${API_BASE}/ai-summary/label`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ feedback_id: Number(fid), label })
        });
        if (!resp.ok) return;
        wrap.querySelectorAll('.feed-ai-chip').forEach(b => b.classList.remove('selected'));
        btn.classList.add('selected');
    } catch (_) { /* 라벨은 선택 사항이라 실패해도 조용히 넘어간다 */ }
};
// 카드용 — 카드 전체가 openArticle 클릭 영역이라 컨테이너에서 버블 차단 (내부 드래그 선택/복사 보호).
function _feedCardDetailHtml(a) {
    return '<div class="feed-card-detail" onclick="event.stopPropagation()">' + _feedDetailInnerHtml(a) + '</div>';
}
// 표용 — 본 행 뒤 삽입되는 상세 행. colspan = 해당 표 열 수 (메인 5 / 분류 4).
function _feedDetailRowHtml(a, colspan) {
    return '<tr class="feed-detail-row"><td colspan="' + colspan + '">' + _feedDetailInnerHtml(a) + '</td></tr>';
}

// 더보기 토글 — 전체 재렌더 없이 DOM 직접 삽입/제거 (무한 스크롤 위치·스크롤 보존).
//   Set 갱신으로 다음 재렌더(소켓 push)에서도 baked 유지.
// [v6.7.385] reduced-motion 판정 (접힘 애니 스킵용).
function _feedReducedMotion() {
    try { return window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (_) { return false; }
}
// [v6.7.385] 상세 접힘 애니메이션 후 DOM 제거 — 내부 그리드에 페이드아웃(표 tr transform 회피), animationend/안전망 후 컨테이너 제거.
function _collapseFeedDetail(detailEl) {
    if (!detailEl) return;
    if (_feedReducedMotion()) { detailEl.remove(); return; }
    const grid = detailEl.querySelector('.feed-detail-grid') || detailEl;
    let done = false;
    const finish = () => { if (done) return; done = true; if (detailEl.parentNode) detailEl.remove(); };
    grid.classList.remove('imt-detail-reveal');
    grid.classList.add('imt-detail-collapsing');
    grid.addEventListener('animationend', finish, { once: true });
    setTimeout(finish, 240);   // animationend 미발화(display none 등) 안전망
}
window.toggleFeedDetail = function (btn) {
    const host = btn.closest('[data-href]');
    if (!host) return;
    const key = host.dataset.href || '';
    if (!key) return;
    const isRow = host.tagName === 'TR';
    if (_feedExpandedKeys.has(key)) {
        _feedExpandedKeys.delete(key);
        host.classList.remove('expanded');
        // [v6.7.385] 즉시 제거 → 접힘 애니메이션 후 제거.
        if (isRow) {
            const next = host.nextElementSibling;
            if (next && next.classList.contains('feed-detail-row')) _collapseFeedDetail(next);
        } else {
            _collapseFeedDetail(host.querySelector('.feed-card-detail'));
        }
        return;
    }
    const a = (_feedTotalCache || []).find(x => x && x.link === key);
    if (!a) return;  // 캐시 miss (드문 race) — 다음 재렌더가 baked 처리
    _feedExpandedKeys.add(key);
    host.classList.add('expanded');
    // [v6.7.385] 접힘 애니 진행 중이던 상세 잔재 제거 (rapid re-expand 중복 삽입 방지)
    if (isRow) { const _n = host.nextElementSibling; if (_n && _n.classList.contains('feed-detail-row')) _n.remove(); }
    else { const _old = host.querySelector('.feed-card-detail'); if (_old) _old.remove(); }
    if (isRow) host.insertAdjacentHTML('afterend', _feedDetailRowHtml(a, host.children.length));
    else host.insertAdjacentHTML('beforeend', _feedCardDetailHtml(a));
    // [v6.7.382] 펼침 애니메이션 — 사용자 클릭 경로에서만 상세 그리드에 reveal 클래스 부여.
    //   재렌더(소켓 push)의 bake 경로는 미부여 → 열려있던 상세가 갱신마다 깜빡이지 않음(PATTERN-38).
    const _revealHost = isRow ? host.nextElementSibling : host;
    const _g = _revealHost && _revealHost.querySelector('.feed-detail-grid');
    if (_g) _g.classList.add('imt-detail-reveal');
    // [v6.7.459] AI 상태 조회 — 사용자가 펼친 순간에만. 캐시 hit 이면 요청 없이 렌더만 한다.
    _feedAiFetch(key, true);   // force — 실패 캐시가 있으면 이때만 재시도
};

// 링크 복사 — 공용 copyTextToClipboard(ui/common.js, clipboard+execCommand 폴백+실패 토스트) 재사용.
window.copyFeedLink = async function (btn) {
    const link = (btn && btn.dataset && btn.dataset.link) || '';
    if (!link) return;
    const ok = (typeof copyTextToClipboard === 'function') ? await copyTextToClipboard(link) : false;
    if (!ok) return;
    if (typeof showToast === 'function') showToast('링크가 복사되었습니다.');
    btn.textContent = '복사됨';
    btn.classList.add('copied');
    setTimeout(() => { btn.textContent = '링크 복사'; btn.classList.remove('copied'); }, 1500);
};

// [v6.7.48] 피드 분류 모드 — 'timeline' (default) | 'by-game' | 'by-community'. webview v0.1.30 등가.
//   Phase A: 토글 UI + LS 영구화 + timeline 만 구현, by-game/by-community 는 placeholder TODO.
const FEED_VIEW_MODE_LS_KEY = 'imt_feed_view_mode';
function _getFeedViewMode() {
    try {
        const v = localStorage.getItem(FEED_VIEW_MODE_LS_KEY);
        if (v === 'by-game' || v === 'by-community') return v;
    } catch (_) {}
    return 'timeline';
}
function _setFeedViewMode(mode) {
    try { localStorage.setItem(FEED_VIEW_MODE_LS_KEY, mode); } catch (_) {}
}
function _syncFeedViewToggleUI(mode) {
    document.querySelectorAll('.feed-view-toggle .feed-view-toggle-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.mode === mode);
    });
}
window.setFeedViewMode = function (mode) {
    if (mode !== 'timeline' && mode !== 'by-game' && mode !== 'by-community') return;
    _setFeedViewMode(mode);
    _syncFeedViewToggleUI(mode);
    // [v6.7.49] viewMode 별 pane 폭 동적 조정 + layout toggle (PC표/모바일카드) 가시성.
    //   by-game/by-community 는 카드 기반 마크업만 의미 있음 → layout toggle 숨김 + 폭 확장.
    _applyFeedPaneWidthForViewAndLayout(mode, _getFeedLayoutMode());
    _applyFeedClassifyBodyClass(mode);
    if (_feedTotalCache !== null) renderFeedPane();
    if (typeof filterFeedHeader === 'function') filterFeedHeader();
};

// [v6.7.49] view × layout 6 조합 폭 매핑. webview v0.1.33 _panelWidth('total-by-game'/'-community') 등가.
//   timeline + cards = 420 / timeline + table = 720 / by-game = 720 / by-community = 800.
//   by-game/by-community 모드는 layout 무관하게 폭 결정 (카드 마크업 강제).
function _applyFeedPaneWidthForViewAndLayout(viewMode, layoutMode) {
    const pane = document.getElementById('feed-tab');
    const win = pane ? pane.closest('.pane-window') : null;
    if (!win) return;
    let w = '420px';
    if (viewMode === 'by-game') w = '720px';
    else if (viewMode === 'by-community') w = '800px';
    else if (layoutMode === 'table') w = '720px';
    win.style.width = w;
}

// [v6.7.49 / v6.7.56] layout mode 를 LS 기반 본래대로 적용 - 분류 모드든 timeline 이든 사용자가 선택한 cards/table 존중.
// [v6.7.731] feed-classify-active 토글 삭제 - 그 클래스를 쓰는 CSS 는 선언이 비어 있던 룰 하나뿐(동반 삭제)이라
//   토글이 아무 효과도 없었다. 함수는 _applyFeedLayoutBodyClass 호출만 남긴다(호출부 2곳 무수정, viewMode 인자는 무시).
function _applyFeedClassifyBodyClass(viewMode) {
    _applyFeedLayoutBodyClass();
}

// [v6.7.16] 피드 레이아웃 모드 — 'cards' (default) | 'table'
const FEED_LAYOUT_LS_KEY = 'imt_layout_mode_feed';
function _getFeedLayoutMode() {
    try {
        const v = localStorage.getItem(FEED_LAYOUT_LS_KEY);
        return (v === 'table') ? 'table' : 'cards';
    } catch (_) { return 'cards'; }
}
function _setFeedLayoutMode(mode) {
    try { localStorage.setItem(FEED_LAYOUT_LS_KEY, mode); } catch (_) {}
}
function _applyFeedLayoutBodyClass() {
    const mode = _getFeedLayoutMode();
    document.body.classList.toggle('feed-mode-cards', mode === 'cards');
    document.body.classList.toggle('feed-mode-table', mode === 'table');
}
function _syncLayoutToggleUI(target, mode) {
    document.querySelectorAll('.layout-toggle[data-target="' + target + '"] .layout-toggle-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.mode === mode);
    });
}
// [v6.7.191 FE-08] _resizeFeedPaneForMode 제거 — 외부 호출처 0 + pane 폭은 _applyFeedPaneWidthForViewAndLayout 이 담당.
window.setFeedLayoutMode = function (mode) {
    if (mode !== 'table' && mode !== 'cards') return;
    _setFeedLayoutMode(mode);
    _syncLayoutToggleUI('feed', mode);
    // [v6.7.56] 분류 모드에서도 layout 변경 즉시 반영 — table/cards 마크업 분기 재렌더.
    const _vm = _getFeedViewMode();
    _applyFeedLayoutBodyClass();
    _applyFeedPaneWidthForViewAndLayout(_vm, mode);
    // 마크업 재렌더 — timeline 의 표/카드 토글 + 분류 모드의 표/카드 분기 모두 즉시 반영.
    if (_feedTotalCache !== null) renderFeedPane();
    // [v6.7.26] 토글 후 헤더 검색 query 재적용 — renderFeedPane 이 마크업 새로 생성하면서 hide 상태 증발 방지
    if (typeof filterFeedHeader === 'function') filterFeedHeader();
};

// ====================================================================
// [v6.7.664] 출처 필터 + 제재 토글 - 2축 직교 (디스코드 탭 채널×제재 v6.7.466 등가)
// ====================================================================
// **둘 다 서버 파라미터다. 클라 필터가 아니다.** 이유가 각각 다르다:
//   - 출처: DC 가 전체 유입의 88%(13일 실측 - DC 3,716 / NAVER 269 / DAUM 163 / OFFICIAL_WEB 64 건·일).
//     최신 N건을 받아 클라에서 거르면 500건 중 공식은 7~8건뿐이라 "공식만 보기"가 빈 화면이 된다.
//   - 제재: 판정(post_verdicts)은 DB 에만 있고 /api/articles/all 페이로드에 없다. 서버가 조인해야 한다.
const FEED_SOURCE_LS_KEY = 'imt_feed_source';
const FEED_FLAGGED_LS_KEY = 'imt_feed_flagged';
const FEED_SOURCES = ['OFFICIAL_WEB', 'NAVER', 'DAUM', 'DC'];
function _getFeedSource() {
    try {
        const v = localStorage.getItem(FEED_SOURCE_LS_KEY);
        if (FEED_SOURCES.indexOf(v) >= 0) return v;
    } catch (_) {}
    return '';   // '' = 전체
}
// [v6.7.673] **서버가 거르는 축이 켜져 있나.** `source`·`flagged` 는 URL 파라미터로 서버가
//   거르므로 **클라에 되거르는 경로가 없다**(`_applyFeedFilters` 는 acked·visibility·limits 만
//   본다). 그래서 이 두 축은 "필터 없는 응답을 받아 클라에서 자르면 된다" 가 성립하지 않는다 -
//   boot 하이드레이션과 빈 상태 문구가 둘 다 이 사실을 알아야 한다. 판별기를 한 곳에 두는 이유는
//   두 벌이 되면 축이 늘 때 한쪽만 늘어나기 때문이다(v6.7.670 이 레인 표에서 겪은 그것).
function _feedServerAxisOn() {
    return !!_getFeedSource() || !!_getFeedFlagged();
}
window._feedServerAxisOn = _feedServerAxisOn;


function _getFeedFlagged() {
    try { return localStorage.getItem(FEED_FLAGGED_LS_KEY) === '1'; } catch (_) { return false; }
}
// [v6.7.731] 선택자를 **피드 pill 로만** 한정한다 - 판정 탭(verdict.html)도 .feed-srcpills/
//   .feed-srcpill/.feed-flagpill 을 공유하는데, data-source / data-flagtoggle 은 피드만 쓴다(판정은
//   data-vd-* 를 쓴다). 범위를 안 좁히면 ① 피드 출처가 '' (전체)일 때 data-source 없는 판정 pill 이
//   ('' === '') 로 전부 active 로 칠해지고 ② setFeedSource 가 판정 pill 클릭을 먹었다.
function _syncFeedSourceBarUI() {
    const src = _getFeedSource();
    document.querySelectorAll('.feed-srcpills .feed-srcpill[data-source]').forEach(b => {
        b.classList.toggle('active', (b.dataset.source || '') === src);
    });
    const fp = document.querySelector('.feed-srcpills .feed-flagpill[data-flagtoggle]');
    if (fp) fp.classList.toggle('active', _getFeedFlagged());
}
// 축이 바뀌면 **재렌더가 아니라 refetch** 다 - 서버 파라미터이기 때문이다.
//
// [v6.7.669] 두 가지를 고친다 (사용자 보고: *"토글 변경할때마다 잔광이 다시 적용되고 글이
// 늦게 불러와짐"*). 둘 다 v6.7.664 가 만든 회귀다.
//
//  ① **잔광 초기화** - `_feedPrevLinks` 는 **이전 축의 링크 집합**을 들고 있다. 전체 -> 공식으로
//     바꾸면 새 목록의 대부분이 "prev 에 없는 링크" 라 전건이 신규 도착으로 오판돼 목록 전체가
//     번쩍인다. v6.7.422 가 뷰 모드 전환에서 같은 증상을 고쳤지만(diff 소스를 전체 캐시로),
//     그 처방은 **캐시 자체가 바뀌는** 이 경우를 못 덮는다. 여기서는 prev 를 비워 다음 렌더를
//     첫 렌더로 만든다(`_recordFeedArrivals` 가 `prevLinks.size === 0` 이면 전량 신규 판정을
//     건너뛴다 - 이미 있는 가드를 쓴다).
//  ② **캐시를 비우지 않는다** - 구 구현은 `null` 로 밀고 스켈레톤을 띄워서, 응답이 올 때까지
//     화면이 통째로 비었다. 서버가 SQLite 직결로 가는 경로라 그 공백이 눈에 띈다. 옛 목록을
//     그대로 두고 새 응답이 오면 갈아끼운다(소켓 갱신이 이미 그렇게 동작한다). 축이 어긋난
//     옛 데이터를 잠깐 보는 값은 `_feedQueryKey` 가 이미 막고 있다 - 그 키가 다르면 다음
//     `loadFeedPane` 이 반드시 fetch 한다.
//
// [v6.7.672] ③ **갈아끼우는 중이라고 말한다.** ②가 남긴 구멍이다 - 옛 목록을 두기로 하면서
//     화면에 진행 신호가 **하나도 없게** 됐다. pill 만 즉시 활성으로 바뀌고 목록은 옛 축 그대로라,
//     응답이 느리면 "토글이 안 먹었다" 로 읽힌다(구 스켈레톤은 반대 극단 - 화면을 통째로 비웠다).
//     둘 다 피하는 자리는 하나뿐이다: **옛 목록을 두되 그것이 옛것임을 표시**한다.
//     dim/진행바는 0.18초 지연 후에 나타나므로 빠른 응답(웜 0.007초)에서는 아예 안 보인다 -
//     '빠를 때 조용하고 느릴 때만 말하는' 신호여야 매번 깜빡이지 않는다.
function _setFeedBusy(on) {
    // [v6.7.731] 피드 소스바만 - document 첫 .feed-src-bar 를 잡으면 판정 탭 툴바(.feed-src-bar
    //   vd-toolbar)가 DOM 앞에 있을 때 진행바가 거기 떴다. 피드 전용 pill(data-source)로 역추적한다.
    const _srcpill = document.querySelector('.feed-srcpills .feed-srcpill[data-source]');
    const bar = _srcpill ? _srcpill.closest('.feed-src-bar') : null;
    if (bar) bar.classList.toggle('is-busy', !!on);
    const body = document.getElementById('feed-body');
    if (body) {
        body.classList.toggle('feed-body--swapping', !!on);
        body.setAttribute('aria-busy', on ? 'true' : 'false');
    }
}

function _feedRefetchForQueryChange() {
    _syncFeedSourceBarUI();
    window._feedBootHydrated = false;
    _feedPrevLinks = new Set();   // ① 축이 바뀌었으니 '직전 화면' 은 비교 대상이 아니다
    _setFeedBusy(true);           // ③ 옛 목록 위에 '갱신 중' 을 얹는다
    loadFeedPane();               // ② 캐시는 유지 - 새 응답이 오면 갈아끼운다
}
window.setFeedSource = function (src) {
    const v = (FEED_SOURCES.indexOf(src) >= 0) ? src : '';
    try { localStorage.setItem(FEED_SOURCE_LS_KEY, v); } catch (_) {}
    _feedRefetchForQueryChange();
};
window.setFeedFlagged = function (on) {
    try { localStorage.setItem(FEED_FLAGGED_LS_KEY, on ? '1' : '0'); } catch (_) {}
    _feedRefetchForQueryChange();
};

// [v6.7.46] empty state 자연화 — 4 케이스 분리: loading 스켈레톤 / 에러 / no-data / filtered.
//   FA 6 아이콘 + title + desc + (옵션) action 버튼 — components.css .empty-state-* 클래스 활용.
// [v6.7.48] Phase D — mode 인자 추가 (timeline | by-game | by-community). empty-no-data / empty-filtered 메시지를 모드별로 분기.
function _renderFeedEmptyState(kind, mode) {
    const body = document.getElementById('feed-body');
    const empty = document.getElementById('feed-empty-state');
    if (!body || !empty) return;
    // [v6.7.688] 스켈레톤/빈/에러 화면은 '칠해진 목록' 이 아니다 - 마커를 걷어 다음 refetch 가
    //   무변화 스킵으로 이 빈 화면 위에 굳지 않게 한다.
    delete body.dataset.painted;
    _feedLastRenderSig = null;
    _feedHideNewPill();
    if (kind === 'loading') {
        // 스켈레톤 카드 5개 — 댓글 탭 v6.6.424 패턴 차용. 빈 박스 메시지보다 자연스러움.
        const skel = `<div class="feed-card skeleton-row">
            <div class="feed-card-title"><span class="skeleton-bar skeleton-bar--lg"></span></div>
            <div class="feed-card-meta"><span class="skeleton-bar skeleton-bar--sm"></span></div>
        </div>`;
        body.innerHTML = window._tabHelpers.loadingHtml('피드를 불러오는 중') + skel.repeat(5);
        empty.style.display = 'none';
        return;
    }
    body.innerHTML = '';
    empty.style.display = 'flex';
    const _mode = (mode === 'by-game' || mode === 'by-community') ? mode : 'timeline';
    if (kind === 'error') {
        empty.innerHTML =
            '<i class="fas fa-cloud-slash empty-state-icon"></i>' +
            '<p class="empty-state-title">데이터를 불러올 수 없습니다</p>' +
            '<p class="empty-state-desc">네트워크 또는 서버 일시 장애로 추정됩니다. 잠시 후 자동으로 다시 시도됩니다.</p>' +
            '<button class="secondary small-button" onclick="loadFeedPane()" style="margin-top:14px"><i class="fas fa-sync-alt"></i> 다시 시도</button>';
    } else if (kind === 'empty-filtered') {
        let title = '필터 조건에 맞는 글이 없습니다';
        let desc = '사이드바 설정에서 게임 가시성 또는 limits 를 조정해보세요.';
        // [v6.7.673] **켜져 있는 축을 그대로 말한다.** 사이드바를 아무리 봐도 머리의 pill 은
        //   안 풀리므로, 원인이 그쪽이면 그쪽을 가리켜야 사람이 한 번에 푼다.
        // [v6.7.731] **축 문구가 우선이다.** 종전엔 by-game/by-community 문구가 이 블록 뒤에서
        //   무조건 덮어써, 출처·제재 pill 이 켜진 분류 모드의 0건에서 사용자가 원인(머리 pill)을
        //   못 봤다. 축이 켜져 있으면 그걸 가리키고, 아니면 모드별 문구로 떨어진다.
        if (_feedServerAxisOn()) {
            const _on = [];
            if (_getFeedSource()) _on.push('출처=' + _getFeedSource());
            if (_getFeedFlagged()) _on.push('제재');
            title = '이 필터에 맞는 글이 없습니다';
            desc = '상단의 ' + _on.join(' + ') + ' 필터가 켜져 있습니다. 해제하면 전체가 보입니다.';
        } else if (_mode === 'by-game') {
            title = '게임별 분류로 표시할 글이 없습니다';
            desc = '필터 조건을 완화하거나 시간순(timeline) 모드로 전환해보세요.';
        } else if (_mode === 'by-community') {
            // [v6.7.731] "DAUM 카페 / 디시 양쪽" 은 출처 2종 시절 화석(현재 4종) - 일반 문구로 교체.
            title = '출처별 분류로 표시할 글이 없습니다';
            desc = '출처 4종 모두 0건입니다. 시간순(timeline) 모드로 전환해보세요.';
        }
        empty.innerHTML =
            '<i class="fas fa-filter empty-state-icon"></i>' +
            '<p class="empty-state-title">' + title + '</p>' +
            '<p class="empty-state-desc">' + desc + '</p>';
    } else {
        let title = '아직 표시할 글이 없습니다';
        let desc = '크롤러가 새 글을 수집하면 자동으로 이 자리에 표시됩니다.';
        if (_mode === 'by-game') {
            title = '게임별로 표시할 글이 없습니다';
            desc = '크롤러가 새 글을 수집하면 게임별 카드로 자동 분류됩니다.';
        } else if (_mode === 'by-community') {
            title = '출처별로 표시할 글이 없습니다';
            // [v6.7.731] "DAUM 카페 / 디시" 는 출처 2종 시절 화석(현재 4종) - 일반 문구로 교체.
            desc = '크롤러가 새 글을 수집하면 출처별 frame 으로 자동 분류됩니다.';
        }
        empty.innerHTML =
            '<i class="fas fa-inbox empty-state-icon"></i>' +
            '<p class="empty-state-title">' + title + '</p>' +
            '<p class="empty-state-desc">' + desc + '</p>';
    }
}

// [v6.7.55] 피드 공통 필터 — acked + visibility + limits + 정렬. timeline / by-game / by-community 모두 동일 정책.
//   사용자 요구 — 분류 모드도 사이드바 필터 (visibility/limits) 영향 받게 (이전 webview v0.1.30 raw 정책 폐기).
function _applyFeedFilters(articles) {
    const ackedSet = new Set(JSON.parse(localStorage.getItem('acknowledgedArticlesTotal') || '[]'));
    const visSettings = (_localSettingsData && _localSettingsData.visibility) ? _localSettingsData.visibility : {};
    const limits = (_localSettingsData && _localSettingsData.limits) ? _localSettingsData.limits : {};
    // [v6.7.738] 출처 우선순위 - 공식 3 > 카페(다음·네이버) 2 > DC 1 (승인 개선 #10).
    //   종전 DAUM 2 / DC 1 / 나머지 0 은 공식(OFFICIAL_WEB)·네이버(NAVER)를 DC 아래로 떨궜다.
    //   서버는 ORDER BY timestamp DESC 만 하고 출처 동률을 가르지 않으므로 이 비교자가 유일 정본이다.
    const _srcPrio = (s) => { const u = String(s || '').toUpperCase(); return u === 'OFFICIAL_WEB' ? 3 : ((u === 'DAUM' || u === 'NAVER') ? 2 : (u === 'DC' ? 1 : 0)); };
    // [v6.7.738] **분(minute) 단위로 먼저 비교한다.** 크롤러가 '초' 를 출처별로 다르게 쓴다 -
    //   DC·다음 = `second = 59 - crawl_idx`(:59,:58...), 네이버·배그공식 = :00, POE공식 = 실제 초.
    //   full timestamp DESC 로 먼저 가르면 같은 분의 DC/다음(:5x)이 공식·네이버(:00) 위로 올라가
    //   _srcPrio 에 영영 닿지 못한다 - 승인 개선 #10 이 라이브에서 안 먹던 원인(PATTERN-57).
    //   초는 실시간이 아니라 크롤 순서라, 분 비교 -> 출처 -> (같은 출처 안에서만) full ts DESC
    //   로 크롤 순서를 보존한다. timestamp 는 전 출처 isoformat 이라 slice(0,16)=분까지(tz 무관).
    const sorted = (articles || []).slice().sort((a, b) => {
        const ta = (a && a.timestamp) || '';
        const tb = (b && b.timestamp) || '';
        const mcmp = tb.slice(0, 16).localeCompare(ta.slice(0, 16));
        if (mcmp !== 0) return mcmp;
        const pa = _srcPrio(a && (a.source_community || a.source));
        const pb = _srcPrio(b && (b.source_community || b.source));
        if (pa !== pb) return pb - pa;
        const tcmp = tb.localeCompare(ta);
        if (tcmp !== 0) return tcmp;
        return ((a && a.link) || '').localeCompare((b && b.link) || '');
    });
    const gameCount = {};
    return sorted.filter(a => {
        if (!a || ackedSet.has(a.link)) return false;
        const game = a.game || a.game_name || '';
        if (game && visSettings[game] === false) return false;
        const lim = (limits[game] !== undefined && limits[game] !== null) ? parseInt(limits[game], 10) : 10;
        if (lim > 0) {
            gameCount[game] = (gameCount[game] || 0) + 1;
            if (gameCount[game] > lim) return false;
        }
        return true;
    });
}

// [v6.7.48] Phase B/C — by-game / by-community 분류 헬퍼 + 렌더러. webview v0.1.30 등가.
//   [v6.7.55] 정책 변경 — 사이드바 필터 (visibility/limits) 영향 받음. _applyFeedFilters 거친 list 전달.
//   [v6.7.123] visibleGames 인자 신규 — 사용자 보고 (2026-05-22): "필터링 다 ON 하면 8 게임인데 6 만 보임".
//     글 0 건인 visibility ON 게임이 grouped 안 박혀서 render 누락 결함. visibleGames pre-populate 으로
//     0 건 게임도 empty list 박제 → renderFeedByGame 가 "0건" section 표시. 사용자 의도 정확 매치.
function _groupByGame(articles, visibleGames) {
    const grouped = {};
    // visibility ON 게임 사전 채움 — 글 0건 게임도 section 박힘.
    (visibleGames || []).forEach(g => {
        if (g) grouped[g] = [];
    });
    (articles || []).forEach(a => {
        if (!a) return;
        const g = a.game || a.game_name || '(미분류)';
        if (!grouped[g]) grouped[g] = [];
        grouped[g].push(a);
    });
    return grouped;
}

// [v6.7.54] 출처별 분류 - 단일 flat list per source. webview v0.1.30 동등.
//   사용자 의도: "다음 카페 글 모아놓는, 디시 모아놓는 것 이렇게 큰 범주 별로" — 게시판 분리 X.
// [v6.7.664] **결함 수정 - 출처가 2종이던 시절(v6.7.54)의 화석이라 NAVER·OFFICIAL_WEB 이 조용히
//   전량 탈락하고 있었다.** 라이브 실측(13일): 출처별 보기에서 네이버 약 3,500건·공식 약 833건이
//   화면에서 사라졌다. 소스 4종은 DB distinct 로 확정 - DC / NAVER / DAUM / OFFICIAL_WEB (오타·NULL 0건).
//   순서는 헤더 출처 pill 과 같게 둔다(공식·네이버·다음·DC) - 두 곳이 어긋나면 사용자가 매번 재탐색한다.
const FEED_COMMUNITY_ORDER = ['OFFICIAL_WEB', 'NAVER', 'DAUM', 'DC'];
const FEED_COMMUNITY_LABEL = {
    OFFICIAL_WEB: '공식 홈페이지', NAVER: '네이버 카페', DAUM: '다음 카페', DC: '디시인사이드',
};
function _groupByCommunity(articles) {
    const grouped = {};
    FEED_COMMUNITY_ORDER.forEach(s => { grouped[s] = []; });
    (articles || []).forEach(a => {
        if (!a) return;
        const src = String(a.source_community || a.source || '').toUpperCase();
        if (!grouped[src]) return;   // 미지의 출처는 버리지 않고 그냥 프레임을 안 만든다(기존 동작 유지)
        grouped[src].push(a);
    });
    return grouped;
}

// [v6.7.54] 출처별 frame 렌더 — DAUM/DC 2 frame × 단일 통합 list. 게시판 분리 X (webview v0.1.30 동등).
// [v6.7.56] PC 표 모드 분기 — _getFeedLayoutMode() === 'table' 시 table 마크업.
function renderFeedByCommunity(grouped, body) {
    const layoutMode = _getFeedLayoutMode();
    let total = 0;
    let html = '';
    FEED_COMMUNITY_ORDER.forEach(src => {
        const label = FEED_COMMUNITY_LABEL[src] || src;
        // [v6.7.731] 재정렬 제거 - 입력(_groupByCommunity(_applyFeedFilters(...)))이 이미 같은 비교자로
        //   정렬돼 있고 그룹핑이 순서를 보존하므로(안정 정렬) 결과가 동일하다. 비교자는 _applyFeedFilters 1곳.
        const arr = (grouped[src] || []);
        const frameCount = arr.length;
        total += frameCount;
        // [v6.7.169] 프레임당 200 캡 제거 → 상한(MAX_FEED_DISPLAY=10000) 까지. _applyFeedFilters per-game limit 이 선제 축소.
        const limited = arr.slice(0, MAX_FEED_DISPLAY);
        let bodyHtml;
        if (frameCount === 0) {
            bodyHtml = '<div class="feed-community-empty">글 없음</div>';
        } else if (layoutMode === 'table') {
            bodyHtml = _feedClassifyTableCommunityHtml(limited);
        } else {
            bodyHtml = limited.map(a => _feedArticleCard(a)).join('');
        }
        const frameCls = 'feed-community-frame feed-community-frame--' + src.toLowerCase();
        html +=
            '<div class="' + frameCls + '" data-sec="' + escapeHtml('c:' + src) + '">' +
                '<div class="feed-community-frame-header">' +
                    '<span class="feed-community-frame-name">' + escapeHtml(label) + '</span>' +
                    '<span class="feed-community-frame-count">' + frameCount + '건</span>' +
                '</div>' +
                '<div class="feed-community-frame-body">' + bodyHtml + '</div>' +
            '</div>';
    });
    body.innerHTML = html;
    _feedRenderCount = total;   // [v6.7.738] 검색 해제 시 복원용 전체 건수
    _updateFeedCount(total);
}

// [v6.7.56] 분류 모드 표 마크업 — by-game / by-community 의 PC 표 옵션.
//   by-game section 안 — 3 col (구분 / 제목 / 시간). 게임은 section header 가 표시하므로 cell 불필요.
//   by-community frame 안 — 3 col (게임 / 제목 / 시간). 출처는 frame header 가 표시.
function _feedClassifyTableGameHtml(rows) {
    if (!rows || rows.length === 0) return '';
    const trs = rows.map(a => {
        const sourceUpper = (a.source_community || a.source || '').toUpperCase();
        const sourceText = _feedSourceLabel(sourceUpper);
        const sourceCls = 'feed-card-source--' + sourceUpper.toLowerCase();
        const title = (typeof highlightKeywords === 'function')
            ? highlightKeywords(a.title || '', a.detected_keywords, a.is_super_keyword)
            : escapeHtml(a.title || '');
        const time = (typeof formatTimeFull === 'function') ? escapeHtml(formatTimeFull(a.timestamp || a.date)) : escapeHtml(a.timestamp || a.date || '');
        // [v6.7.731] freshnessClass no-op 블록 삭제 - _tab_helpers._freshnessClass 가 v6.7.407 이후 항상 '' 반환.
        let rowCls = '';
        // [v6.7.317] 더보기 확장 — col-more 버튼 + expanded 시 상세 행 baked (colspan 4).
        const _key = a.link || '';
        const _exp = _key && _feedExpandedKeys.has(_key);
        if (_exp) rowCls += ' expanded';
        rowCls += _feedVerdictCls(a);   // [v6.7.664] 제재 판정
        return `<tr class="${rowCls.trim()}" onclick="openArticle(this.dataset.href)" data-href="${escapeHtml(a.link || '')}" data-ts="${escapeHtml(a.timestamp || a.date || '')}" data-source="${escapeHtml(sourceUpper)}">
            <td class="col-source">${_feedVerdictChipHtml(a)}${_feedSourceBadgeHtml(a, sourceUpper, sourceText, sourceCls)}</td>
            <td class="col-title" title="${escapeHtml(a.title || '')}"><span class="col-title-text">${title}</span></td>
            <td class="col-time">${time}</td>
            <td class="col-more">${_feedMoreBtnHtml(_key)}</td>
        </tr>` + (_exp ? _feedDetailRowHtml(a, 4) : '');
    }).join('');
    return `<div class="feed-data-table-wrap"><table class="feed-data-table feed-data-table--classify-game">
        <thead><tr><th class="col-source">구분</th><th class="col-title">제목</th><th class="col-time">시간</th><th class="col-more" aria-label="상세 보기"></th></tr></thead>
        <tbody>${trs}</tbody>
    </table></div>`;
}
function _feedClassifyTableCommunityHtml(rows) {
    if (!rows || rows.length === 0) return '';
    const trs = rows.map(a => {
        const sourceUpper = (a.source_community || a.source || '').toUpperCase();
        const gameColor = (typeof window.getGameColor === 'function') ? window.getGameColor(a.game) : 'var(--primary-color)';
        const title = (typeof highlightKeywords === 'function')
            ? highlightKeywords(a.title || '', a.detected_keywords, a.is_super_keyword)
            : escapeHtml(a.title || '');
        const time = (typeof formatTimeFull === 'function') ? escapeHtml(formatTimeFull(a.timestamp || a.date)) : escapeHtml(a.timestamp || a.date || '');
        // [v6.7.731] freshnessClass no-op 블록 삭제 - _tab_helpers._freshnessClass 가 v6.7.407 이후 항상 '' 반환.
        let rowCls = '';
        // [v6.7.317] 더보기 확장 — col-more 버튼 + expanded 시 상세 행 baked (colspan 4).
        const _key = a.link || '';
        const _exp = _key && _feedExpandedKeys.has(_key);
        if (_exp) rowCls += ' expanded';
        rowCls += _feedVerdictCls(a);   // [v6.7.664] 제재 판정
        return `<tr class="${rowCls.trim()}" onclick="openArticle(this.dataset.href)" data-href="${escapeHtml(a.link || '')}" data-ts="${escapeHtml(a.timestamp || a.date || '')}" data-source="${escapeHtml(sourceUpper)}">
            <td class="col-game"><span class="col-game-name" data-gc="${escapeHtml(a.game || '')}" style="color:${gameColor};">${escapeHtml(a.game || '')}</span></td>
            <td class="col-title" title="${escapeHtml(a.title || '')}">${_feedVerdictChipHtml(a)}<span class="col-title-text">${title}</span></td>
            <td class="col-time">${time}</td>
            <td class="col-more">${_feedMoreBtnHtml(_key)}</td>
        </tr>` + (_exp ? _feedDetailRowHtml(a, 4) : '');
    }).join('');
    return `<div class="feed-data-table-wrap"><table class="feed-data-table feed-data-table--classify-community">
        <thead><tr><th class="col-game">게임</th><th class="col-title">제목</th><th class="col-time">시간</th><th class="col-more" aria-label="상세 보기"></th></tr></thead>
        <tbody>${trs}</tbody>
    </table></div>`;
}

// 게임별 section card 렌더 — 각 section header + 그 아래 _feedArticleCard 재사용. timestamp DESC + source priority tiebreak.
// [v6.7.56] PC 표 모드 분기 — layoutMode === 'table' 시 table 마크업.
function renderFeedByGame(grouped, body) {
    const layoutMode = _getFeedLayoutMode();
    const gameNames = Object.keys(grouped);
    gameNames.sort((a, b) => (grouped[b] || []).length - (grouped[a] || []).length);  // 섹션 순서 = 글 많은 게임 먼저 (per-game 정렬 아님)
    let html = '';
    let total = 0;
    gameNames.forEach(gameName => {
        // [v6.7.731] 재정렬 제거 - 입력(_groupByGame(_applyFeedFilters(...)))이 이미 같은 비교자로
        //   정렬돼 있고 그룹핑이 순서를 보존하므로(안정 정렬) 결과가 동일하다. 비교자는 _applyFeedFilters 1곳.
        const arr = (grouped[gameName] || []);
        // [v6.7.52→169] 섹션당 30 캡 제거 → 상한(MAX_FEED_DISPLAY=10000) 까지. section body 는 max-height(CSS)+자체 스크롤.
        //   디버깅 시 게임 1개 섹션에서 전량(<=10000) 확인 가능. 평소엔 _applyFeedFilters per-game limit 이 선제 축소.
        const limited = arr.slice(0, MAX_FEED_DISPLAY);
        total += arr.length;
        const gameColor = (typeof window.getGameColor === 'function')
            ? window.getGameColor(gameName) : 'var(--primary-color)';
        // [v6.7.56] layout mode 분기 — cards (default) vs table.
        // [v6.7.124] 글 0건 section "글 없음" 메시지 — by-community frame (line 247) 동일 패턴.
        //   v6.7.123 의 visibility pre-populate 으로 0건 게임도 section 박힘 → 빈 영역만 노출되던 결함 fix.
        //   .feed-community-empty 클래스 재사용 (CSS 박제 추가 없음, 동일 visual).
        const sectionBodyHtml = (arr.length === 0)
            ? '<div class="feed-community-empty">글 없음</div>'
            : (layoutMode === 'table'
                ? _feedClassifyTableGameHtml(limited)
                : limited.map(a => _feedArticleCard(a)).join(''));
        html +=
            '<div class="feed-game-section" data-sec="' + escapeHtml('g:' + gameName) + '" style="border-left:3px solid ' + gameColor + ';">' +
                '<div class="feed-game-section-header">' +
                    '<span class="feed-game-section-name" style="color:' + gameColor + ';">' + escapeHtml(gameName) + '</span>' +
                    '<span class="feed-game-section-count">' + arr.length + '건</span>' +
                '</div>' +
                '<div class="feed-game-section-body">' + sectionBodyHtml + '</div>' +
            '</div>';
    });
    body.innerHTML = html;
    _feedRenderCount = total;   // [v6.7.738] 검색 해제 시 복원용 전체 건수
    _updateFeedCount(total);
}

// [v6.7.168→169] 사이드바 per-game limits 합계 → 서버 fetch limit 계산. v6.7.167 의 renderCap(2000) 폐지
//   (렌더는 renderFeedPane 무한 스크롤). 이 값은 "서버에서 몇 건 받아올지" 만 결정.
//   [v6.7.169] 상한 MAX_FEED_DISPLAY(10000) — 무제한(0) 게임이면 상한까지 요청. floor 500 + sum×1.2 = 필터
//   허용량 + 여유. 평소(게임별 10)엔 ~500 만 받아 1.22MB JSON(PATTERN-29) 회피, 디버깅 시 상한까지 확장.
const MAX_FEED_DISPLAY = 10000;  // 피드 표시 상한 (= 서버 MAX_MEMORY_ARTICLES). 게임 1개만 켜도 10000 도달 가능.
function _feedFetchLimit() {
    // [v6.7.169] 공통 헬퍼 _imtFetchBudget (core/highlight.js) 위임 — boot.js / comments-tab 와 동일 공식.
    return (typeof _imtFetchBudget === 'function')
        ? _imtFetchBudget(_localSettingsData, 'limits', 'visibility', MAX_FEED_DISPLAY)
        : 500;
}

// [v6.7.172] 가시 게임이 전체의 부분집합(필터링)이면 그 게임만 SQLite 직접 조회 (&games).
//   글로벌 최신 스트림은 활발한 게임(우마무스메 등)이 점령 → busy 게임(POE2)의 과거 글 누락.
//   필터 시 per-game 직접 조회로 그 게임 전량(보관 7일 / 최대 limit) 노출. 전체 게임 가시면 글로벌 유지.
// [v6.7.491] 그 키를 함수로 빼고 캐시에 기록한다 — 아래 _feedCacheIsStaleForFilters 참조.
function _feedGamesKey() {
    try {
        const _allG = (_localSettingsData && Array.isArray(_localSettingsData.games)) ? _localSettingsData.games : [];
        const _visCfg = (_localSettingsData && _localSettingsData.visibility) ? _localSettingsData.visibility : {};
        const _visG = _allG.filter(g => _visCfg[g] !== false);
        if (_allG.length > 0 && _visG.length > 0 && _visG.length < _allG.length) return _visG.join(',');
    } catch (_) {}
    return '';   // '' = 글로벌 스트림(전체 가시 또는 판정 불가)
}
// [v6.7.491] 현재 캐시가 **지금 필터로 받아온 것인지**. 다르면 재렌더가 아니라 refetch 해야 한다.
//   왜 필요한가: v6.7.98 이 "필터 변경 시 캐시 있으면 fetch skip + 재렌더" 최적화를 넣었는데,
//   74 버전 뒤 v6.7.172 가 `&games=` per-game 조회를 도입하면서 **그 전제가 깨졌다.**
//   글로벌 캐시에는 활발한 게임이 최신 창을 점령해 조용한 게임 글이 아예 없을 수 있고
//   (그게 v6.7.172 를 만든 이유다), 그 상태로 조용한 게임만 켜면 **클라 필터 결과가 0건**이 된다.
//   fetch 를 아예 안 하므로 서버 로그에도 흔적이 없다. 증상 = "필터 조정 중 간헐적 0건,
//   다시 적용하면 복구"(사용자 보고 2026-08-10).
// [v6.7.664] 키에 출처·제재를 합류시킨다. 이 둘도 **서버 파라미터**라, 빼먹으면 v6.7.491 이 games
//   에서 고친 것과 정확히 같은 결함이 다시 열린다 - 축을 바꿨는데 fetch 를 안 해 옛 응답을 재렌더하고,
//   서버 로그에도 흔적이 없다(PATTERN-55: 캐시 재사용 판정에 캐시 키가 없으면 조용히 틀린 데이터).
//   **앞으로 서버 파라미터를 추가하면 이 함수에 반드시 합류시킬 것.**
function _feedQueryKey() {
    return _feedGamesKey() + '|' + _getFeedSource() + '|' + (_getFeedFlagged() ? '1' : '0');
}
let _feedCacheGamesKey = null;   // null = 아직 한 번도 안 받아옴. 값은 _feedQueryKey() 결과다.
window._feedCacheIsStaleForFilters = function () {
    return _feedCacheGamesKey !== _feedQueryKey();
};

// [v6.7.731] 부트 프리페치(boot.js)가 받아온 **글로벌 스트림**을 피드 캐시로 채택할지 결정한다.
//   프리페치 fetch 는 games/source/flagged 를 안 붙인 **기본(글로벌) 질의**다. 세 가지가 전부여야 쓴다:
//     ① _feedServerAxisOn() 이 꺼짐(출처·제재 없음)  ② _feedGamesKey() 가 빈값(게임 숨김 없음)
//     ③ _feedCacheGamesKey === null (loadFeedPane 이 아직 제 축으로 안 받아옴)
//   종전 boot.js 는 ①만 봤다 - 게임 숨긴 사용자는 ②를 못 걸러 첫 화면이 글로벌 스트림이었고,
//   게다가 캐시키를 안 찍어 _feedCacheIsStaleForFilters() 가 'null !== 게임키' 로는 맞았지만
//   채택하면서 키를 안 찍으면 다음 refetch 가 자기 축으로 못 돌아오는 구간이 생겼다.
//   채택 시 질의 키를 찍어(PATTERN-55) 캐시가 제 축으로 인식되게 한다. 판별기는 여기(feed-tab.js)에만
//   둔다 - boot 에 사본을 만들면 서버 축이 늘 때 한쪽만 늘어난다(v6.7.670 레인 표가 겪은 그것).
function _feedAdoptBootPrefetch(articles) {
    if (!Array.isArray(articles)) return false;
    if (_feedCacheGamesKey !== null) return false;            // 이미 제 축으로 받아왔다
    if (_feedServerAxisOn() || _feedGamesKey()) return false;  // 기본(글로벌) 질의가 아니다
    _feedTotalCache = articles;
    _feedCacheGamesKey = _feedQueryKey();   // 기본 질의 키를 찍는다
    window._feedBootHydrated = true;
    return true;
}
window._feedAdoptBootPrefetch = _feedAdoptBootPrefetch;

async function loadFeedPane() {
    // [v6.7.46] 첫 호출 시 스켈레톤 표시 — 빈 박스 + "로드 중…" 텍스트 대신 카드 형태 시각 신호.
    // [v6.7.100 → v6.7.104] Phase D 캐시 hit skip — boot.js Promise.all 직후 1회만 적용.
    //   v6.7.100 의 무조건 캐시 hit skip 은 socket raw_articles_update push 시에도 fetch
    //   skip 시켜 새 글 영구 미갱신 결함 발생 → 사용자가 수동 새로고침 강제. v6.7.104:
    //   window._feedBootHydrated flag 로 1회만 skip (boot.js 가 Promise.all 응답 직후 set).
    //   그 후 모든 호출 (socket push / 재연결 refresh / 다시 시도) 은 정상 fetch.
    if (window._feedBootHydrated === true && _feedTotalCache !== null && Array.isArray(_feedTotalCache)) {
        window._feedBootHydrated = false;  // 1회 소비
        _feedRefetchRender();   // [v6.7.688] refetch 경로 - 무변화 스킵 + 스크롤 앵커 보존
        return;
    }
    // [v6.7.672] **질의 키를 fetch 전에 박는다.** 구 코드는 await 뒤에 `_feedQueryKey()` 를 다시
    //   불러 캐시에 찍었다 - 그 사이에 사용자가 토글하면 **옛 축의 응답에 새 축의 키가 찍힌다.**
    //   그러면 `_feedCacheIsStaleForFilters()` 가 "맞는 캐시" 라고 답해 refetch 도 안 걸리고,
    //   화면은 조용히 틀린 목록을 보여준다 - v6.7.491 이 games 에서 막은 PATTERN-55 가
    //   **시간 축(응답 역전)으로 다시 열린 것**이다. 재현 조건: 빠른 연속 토글 + 느린 응답
    //   (제재 단독이 콜드 16.8초였으니 실제로 재현 가능한 창이었다).
    const _reqKey = _feedQueryKey();
    try {
        if (_feedTotalCache === null) _renderFeedEmptyState('loading');
        // [v6.7.133] 동적 fetch limit — 사용자 박제 limits 합계 × 1.2 + 최소 500.
        // 이전: limit param 미전달 → 서버 default 500 cap 적용 → 사용자 박제 5000 무관 77건 (v6.7.131/132 사례).
        // 클라 측 _applyFeedFilters 가 visibility/limits 박제대로 재 cut.
        // [v6.7.168] _feedFetchLimit — 0(무제한) 게임이면 fetchLimit=0 (서버 무제한 옵트인).
        const fetchLimit = _feedFetchLimit();
        // [v6.7.172] 가시 게임이 전체의 부분집합(필터링)이면 그 게임만 SQLite 직접 조회 (&games).
        //   글로벌 최신 스트림은 활발한 게임(우마무스메 등)이 점령 → busy 게임(POE2)의 과거 글 누락.
        //   필터 시 per-game 직접 조회로 그 게임 전량(보관 7일 / 최대 limit) 노출. 전체 게임 가시면 글로벌 유지.
        let _feedUrl = `${API_BASE}/articles/all?limit=${fetchLimit}`;
        const _gamesKey = _feedGamesKey();
        if (_gamesKey) _feedUrl += '&games=' + _gamesKey.split(',').map(encodeURIComponent).join(',');
        // [v6.7.664] 출처·제재 (서버 파라미터). 지정 시 서버가 캐시를 우회해 SQLite 직결로 간다.
        const _srcKey = _getFeedSource();
        if (_srcKey) _feedUrl += '&source=' + encodeURIComponent(_srcKey);
        if (_getFeedFlagged()) _feedUrl += '&flagged=1';
        // [v6.7.501] 15s 타임아웃 — 무타임아웃 fetch 는 스톨 시 catch 가 안 떠서 재시도조차 못 돌고
        //   스켈레톤이 영구 고착한다(PATTERN-40). v6.7.425 가 AI요약·이상감지·부트 프리페치에 같은
        //   가드를 넣을 때 **피드만 빠졌는데**, 정작 피드가 가장 큰 응답(1.22MB)을 받아 부팅 폭풍에
        //   제일 먼저 걸린다(사용자 2026-08-11 배포 직후 스켈레톤 고착).
        let _sig, _tm;
        try { const c = new AbortController(); _tm = setTimeout(() => c.abort(), 15000); _sig = c.signal; } catch (_) { _sig = undefined; }
        let resp;
        try { resp = await fetch(_feedUrl, { signal: _sig }); }
        finally { if (_tm) clearTimeout(_tm); }
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const _data = await resp.json();
        if (_reqKey !== _feedQueryKey()) {
            // 이미 다음 축으로 넘어갔다 - 이 응답은 **버린다**. 캐시도 키도 건드리지 않는다
            //   (busy 표시도 끄지 않는다 - 더 최신 요청이 화면을 책임지고 있다).
            console.debug('피드 - 지난 축의 응답 폐기:', _reqKey, '->', _feedQueryKey());
            return;
        }
        _feedTotalCache = _data;
        _feedCacheGamesKey = _reqKey;      // [v6.7.491→662→672] **요청 시점의** 질의로 찍는다
        window._feedRetryN = 0;           // [v6.7.501] 성공이 백오프를 리셋한다
        _setFeedBusy(false);
        // [v6.7.731] 렌더는 fetch 와 **분리된 try** 다(PATTERN-66). 종전엔 _feedRefetchRender 예외가
        //   아래 fetch catch 로 흘러 **fetch 실패로 오인**됐고, 그 catch 가 재시도를 걸어 10초마다
        //   1.22MB 재요청이 pane 이 닫혀도 무한 반복됐다. 렌더 결함은 재요청 대상이 아니다.
        try {
            _feedRefetchRender();   // [v6.7.688] refetch 경로 - 무변화 스킵 + 스크롤 앵커 보존 + "새 글 N건" pill
        } catch (_re) {
            console.error('피드 렌더 실패(재요청 안 함):', _re);
        }

    } catch (e) {
        console.error('피드 로드 실패(fetch):', e);
        // [v6.7.95] 캐시 보존 — 일시 fetch 실패 (PATTERN-29 워커 rotation / 네트워크 jitter) 에
        //   본문을 에러 화면으로 덮어 '피드 125건 + 데이터를 불러올 수 없습니다' 모순이 되던 결함 차단.
        //   댓글 탭 (catch 시 UI 미터치) 와 정합. 캐시 0일 때만 에러 화면. 캐시 있으면 본문 유지 +
        //   2.5s 후 1회 자동 재시도 — empty-state desc '잠시 후 자동으로 다시 시도됩니다' 약속 이행.
        // [v6.7.501] 캐시가 없을 때야말로 재시도가 필요하다 — 화면에 아무것도 없는 상태이기 때문이다.
        //   구 코드는 여기서 그냥 `return` 해 재시도를 **캐시가 있을 때만** 걸었다(정확히 반대 방향).
        //   배포 직후 빈 페이지가 스스로 못 낫던 원인 — 첫 로드가 실패하면 재트리거가 소켓 push 뿐인데
        //   그 시각엔 서버가 push 를 못 보낸다(AI요약 v6.7.423→425 가 같은 결함을 이미 겪었다).
        // 실패해도 busy 는 반드시 걷는다 - 안 걷으면 목록이 영구히 흐린 채로 남는다.
        //   단 **내 요청이 아직 현재 축일 때만** (지난 축의 실패가 최신 요청의 표시를 지우면 안 된다).
        if (_reqKey === _feedQueryKey()) _setFeedBusy(false);
        if (_feedTotalCache === null) _renderFeedEmptyState('error');
        // [v6.7.731] 피드 pane 이 닫혀 있으면 재시도하지 않는다 - 닫힌 pane 에도 10초마다 1.22MB
        //   재요청이 영구히 나가던 경로 차단. 사용자가 탭을 다시 열면 actions.js 가 loadFeedPane 을
        //   다시 부르므로 자가회복은 유지된다. (활성 pane 의 첫 로드 실패 재시도는 그대로다.)
        const _feedActive = (typeof activeApps !== 'undefined' && Array.isArray(activeApps) && activeApps.includes('feed'));
        if (_feedActive && !window._feedRetryScheduled) {
            window._feedRetryScheduled = true;
            // 1차 2.5s, 이후 10s 간격. 성공(_feedRetryN=0)이 백오프를 리셋한다.
            window._feedRetryN = (window._feedRetryN || 0) + 1;
            setTimeout(() => {
                window._feedRetryScheduled = false;
                loadFeedPane();
            }, window._feedRetryN === 1 ? 2500 : 10000);
        }
    }
}

// ====================================================================
// [v6.7.688] 피드 순단 제거 - 무변화 스킵 + 스크롤 보존 + "새 글 N건" pill
// ====================================================================
// PATTERN-38: 소켓 raw_articles_update(800ms 디바운스)가 크롤 사이클마다 loadFeedPane 을
//   태우고, renderFeedPane 은 body.innerHTML 을 통째로 갈아엎어 스크롤이 top 으로 튕기고
//   메인스레드가 블로킹된다. AI 요약 탭(_dataSig+_ingest, PATTERN-38 (2)(4))은 이미 무변화
//   스킵 + 스크롤 보존을 갖췄는데 피드에만 없었다.
//
//   **경로 구분이 핵심이다.** refetch(소켓/부트)만 스킵·앵커 대상이고, 필터·토글(setFeedViewMode/
//   setFeedLayoutMode/setFeedSource…)이 부르는 renderFeedPane 은 **항상 렌더**한다(그 경로들은
//   renderFeedPane 을 직접 부르고, 스킵 판정은 _feedRefetchRender 안에만 있다).
//
//   PATTERN-38 (4): 스킵 조건은 "데이터 무변화" 만이 아니라 "데이터·입력 무변화 AND DOM 무변화"다.
//   updateDashboard(render.js)가 pane 헤더(#feed-count-header)를 통째 재생성해 건수를 초기값으로
//   되돌리는데 본문(#feed-body)은 appendChild 로 이동해 살아남는다. 헤더 미마킹(=재생성)이면
//   스킵 금지 - 안 그러면 "목록은 있는데 헤더는 0건" 이 다음 데이터 변화까지 굳는다.

// 렌더 시그니처 - 데이터(link+timestamp+제재판정+본문수집상태+제목) + 렌더 좌우 입력(뷰/레이아웃/
//   질의키/acked/가시성/limits). _feedVerdictChipHtml·_feedContentPending 이 실제로 보는 필드만 센다.
function _feedRenderSig() {
    const data = Array.isArray(_feedTotalCache) ? _feedTotalCache : [];
    let h = 5381;
    for (let i = 0; i < data.length; i++) {
        const a = data[i] || {};
        const s = (a.link || '') + '\x1f' + (a.timestamp || a.date || '') + '\x1f'
            + (a.verdict_kind || '') + '\x1f' + (a.verdict_target || '') + '\x1f' + (a.judge_pending ? 'J' : '') + '\x1f'
            + (_feedContentPending(a.content) ? 'P' : 'C') + '\x1f' + (a.title || '');
        for (let j = 0; j < s.length; j++) h = ((h << 5) + h + s.charCodeAt(j)) | 0;
    }
    const vis = (_localSettingsData && _localSettingsData.visibility) ? _localSettingsData.visibility : {};
    const lim = (_localSettingsData && _localSettingsData.limits) ? _localSettingsData.limits : {};
    let ackedN = 0;
    try { ackedN = (JSON.parse(localStorage.getItem('acknowledgedArticlesTotal') || '[]') || []).length; } catch (_) {}
    return data.length + '|' + (h >>> 0) + '|' + _getFeedViewMode() + '|' + _getFeedLayoutMode()
        + '|' + _feedQueryKey() + '|' + ackedN + '|' + JSON.stringify(vis) + '|' + JSON.stringify(lim);
}
let _feedLastRenderSig = null;
// [v6.7.694] 시간순 보기 "아직 못 본 새 글" 집합. v6.7.688 은 pill 건수를 **이번 렌더에서 새로 붙은 글**로만 셌다 -
//   새 글 도착 직후 본문 수집(content)·다른 크롤 레인 신호로 한 번 더 refetch 렌더가 돌면 그 렌더엔 새 글이 0 이라
//   pill 을 숨겼다(라이브 보고 2026-09-26: 새 글 잔광은 보이는데 pill 없음). 이제 사용자가 스크롤로 그 글을 화면에
//   넣거나 맨 위로 갈 때까지 집합에 남긴다. 필터·토글 렌더(renderFeedPane 직접 호출)는 집합을 비운다.
const _feedUnseen = new Set();
let _feedRestore = null;   // refetch 렌더 1회분 스크롤 앵커 (필터/토글 렌더에서는 null)
// [v6.7.731] 마지막으로 **실제로 그린** 질의 키. refetch(소켓/부트) 가 들어왔을 때 이 키와
//   현재 캐시 키(_feedCacheGamesKey)가 다르면 '같은 목록의 갱신' 이 아니라 '다른 목록'(축 토글·
//   게임 필터 변경)이므로 앵커 복원·unseen 적립을 하지 않는다 - 안 그러면 스크롤 내린 채 토글 시
//   새 목록의 윗글 전부가 가짜 '새 글 N건' 으로 집계된다.
let _feedRenderedQueryKey = null;

function _feedMarkPainted() {
    const body = document.getElementById('feed-body');
    if (body) body.dataset.painted = '1';
    _feedLastRenderSig = _feedRenderSig();
    _feedRenderedQueryKey = _feedCacheGamesKey;   // [v6.7.731] 이 질의로 그렸음을 기록
}

// "새 글 N건" pill - #feed-body 는 렌더마다 innerHTML 이 비워지므로 pane(#feed-tab)에 붙여 살린다.
function _feedNewPillEl(create) {
    let el = document.getElementById('feed-newpill');
    if (!el && create) {
        const tab = document.getElementById('feed-tab');
        if (!tab) return null;
        el = document.createElement('button');
        el.id = 'feed-newpill';
        el.type = 'button';
        el.className = 'feed-newpill';
        el.setAttribute('aria-live', 'polite');
        el.style.display = 'none';
        el.addEventListener('click', _feedScrollToTop);
        tab.appendChild(el);
        const body = document.getElementById('feed-body');
        if (body && !body.dataset.newpillBound) {
            body.dataset.newpillBound = '1';   // 맨 위로 오면 자동 숨김 (1회만 배선)
            let _raf = 0;   // [v6.7.694] 스크롤로 새 글이 화면에 들어오면 건수를 줄이고, 다 보면 pill 을 걷는다
            body.addEventListener('scroll', function () {
                if (_raf) return;
                _raf = requestAnimationFrame(function () {
                    _raf = 0;
                    if (_feedUnseen.size) _feedSyncUnseenPill();
                    else if (body.scrollTop <= 4) _feedHideNewPill();
                });
            }, { passive: true });
        }
    }
    return el;
}
function _feedShowNewPill(n) {
    const el = _feedNewPillEl(true);
    if (!el) return;
    el.innerHTML = '<i class="fas fa-arrow-up" aria-hidden="true"></i> 새 글 ' + n + '건';
    el.style.display = '';
}
function _feedHideNewPill() {
    const el = document.getElementById('feed-newpill');
    if (el) el.style.display = 'none';
}
function _feedScrollToTop() {
    const body = document.getElementById('feed-body');
    if (!body) return;
    // [v6.7.692] 분류 모드 - 새 글이 있는 섹션들을 각자 맨 위로.
    body.querySelectorAll('.feed-sec-newchip').forEach(chip => {
        const sec = chip.closest('[data-sec]');
        const sb = sec && sec.querySelector(_FEED_SEC_BODY_SEL);
        if (sb) _feedScrollSecTop(sb);
        chip.remove();
    });
    try { body.scrollTo({ top: 0, behavior: _feedReducedMotion() ? 'auto' : 'smooth' }); }
    catch (_) { body.scrollTop = 0; }
    _feedUnseen.clear();
    _feedHideNewPill();
}

// [v6.7.694] 못 본 새 글 정리 + pill 동기화. 집합에서 빼는 조건: 목록에서 사라짐(ack·한도 탈락) 또는 카드가
//   화면 위쪽 경계 아래로 들어옴(=사용자가 봤다). 맨 위(<=4px)면 전부 본 것으로 친다.
function _feedSyncUnseenPill() {
    const body = document.getElementById('feed-body');
    if (!body || !_feedUnseen.size) { if (body && !body.querySelector('.feed-sec-newchip')) _feedHideNewPill(); return; }
    if (body.scrollTop <= 4) { _feedUnseen.clear(); _feedHideNewPill(); return; }
    const top = body.getBoundingClientRect().top;
    for (const link of Array.from(_feedUnseen)) {
        const el = body.querySelector('[data-href="' + CSS.escape(link) + '"]:not(.feed-detail-row)');
        if (!el || el.getBoundingClientRect().bottom > top + 8) _feedUnseen.delete(link);
    }
    if (_feedUnseen.size) _feedShowNewPill(_feedUnseen.size); else _feedHideNewPill();
}

// [v6.7.692] 분류 모드 섹션별 앵커 캡처/복원 + 섹션 헤더 "새 N" 칩.
const _FEED_SEC_BODY_SEL = '.feed-game-section-body, .feed-community-frame-body';
const _FEED_ITEM_SEL = '[data-href]:not(.feed-detail-row)';
function _feedCaptureSections(body) {
    const out = {};
    body.querySelectorAll('[data-sec]').forEach(sec => {
        const sb = sec.querySelector(_FEED_SEC_BODY_SEL);
        if (!sb) return;
        const items = Array.from(sb.querySelectorAll(_FEED_ITEM_SEL));
        const top = sb.getBoundingClientRect().top;
        let anchor = null;
        for (const el of items) { if (el.getBoundingClientRect().bottom > top + 1) { anchor = el; break; } }
        out[sec.dataset.sec] = {
            st: sb.scrollTop,
            link: anchor ? anchor.dataset.href : null,
            delta: anchor ? (anchor.getBoundingClientRect().top - top) : 0,
            links: new Set(items.map(e => e.dataset.href).filter(Boolean)),
            // 렌더 전에 이미 떠 있던 칩의 누적분 - 다음 사이클 재렌더가 칩을 지워도 건수를 이어간다.
            carried: parseInt((sec.querySelector('.feed-sec-newchip') || {}).dataset?.n || '0', 10) || 0,
        };
    });
    return out;
}
function _feedRestoreSections(body, r) {
    // 섹션 높이는 _setupFeedGameSectionResize 가 rAF 로 늦게 박는다 - 그 전엔 섹션이 스크롤 불가라
    //   scrollTop 대입이 0 으로 잘린다. 복원 전에 높이를 동기로 먼저 적용한다(멱등).
    if (typeof window.updateFeedGameSectionHeights === 'function') window.updateFeedGameSectionHeights();
    body.scrollTop = r.scrollTop;
    let total = 0;
    body.querySelectorAll('[data-sec]').forEach(sec => {
        const p = r.secs && r.secs[sec.dataset.sec];
        const sb = sec.querySelector(_FEED_SEC_BODY_SEL);
        if (!p || !sb || p.st <= 4) return;   // 새 섹션이거나 맨 위를 보던 섹션 - 새 글이 위에 그대로 보인다
        const items = Array.from(sb.querySelectorAll(_FEED_ITEM_SEL));
        const anchorEl = p.link ? items.find(e => e.dataset.href === p.link) : null;
        let n = 0;
        for (const el of items) {
            if (el === anchorEl) break;
            if (!p.links.has(el.dataset.href)) n++;
        }
        if (anchorEl) sb.scrollTop += (anchorEl.getBoundingClientRect().top - sb.getBoundingClientRect().top) - p.delta;
        else { sb.scrollTop = p.st; n = 0; }
        n += p.carried;
        if (n > 0) { _feedSecChip(sec, n); total += n; }
    });
    if (total > 0) _feedShowNewPill(total); else _feedHideNewPill();
    _feedBindSecScroll(body);
}
function _feedSecChip(sec, n) {
    const hdr = sec.querySelector('.feed-game-section-header, .feed-community-frame-header');
    if (!hdr) return;
    let chip = hdr.querySelector('.feed-sec-newchip');
    if (!chip) {
        chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'feed-sec-newchip';
        chip.title = '이 섹션 맨 위로';
        const cnt = hdr.querySelector('.feed-game-section-count, .feed-community-frame-count');
        hdr.insertBefore(chip, cnt);   // 이름 · [새 글 N] 건수 순 (cnt 가 없으면 끝에 붙는다)
    }
    chip.dataset.n = String(n);
    chip.textContent = '새 글 ' + n;
}
function _feedClearSecChip(sec) {
    const chip = sec && sec.querySelector('.feed-sec-newchip');
    if (chip) chip.remove();
    const body = document.getElementById('feed-body');
    if (body && !body.querySelector('.feed-sec-newchip')) _feedHideNewPill();
    else if (body) {
        let t = 0; body.querySelectorAll('.feed-sec-newchip').forEach(c => { t += parseInt(c.dataset.n || '0', 10) || 0; });
        _feedShowNewPill(t);
    }
}
function _feedScrollSecTop(sb) {
    try { sb.scrollTo({ top: 0, behavior: _feedReducedMotion() ? 'auto' : 'smooth' }); }
    catch (_) { sb.scrollTop = 0; }
}
// 섹션 스크롤은 bubble 하지 않으므로 capture 로 1회 배선 - 섹션을 맨 위로 올리면 그 섹션 칩이 사라진다.
function _feedBindSecScroll(body) {
    if (body.dataset.secScrollBound) return;
    body.dataset.secScrollBound = '1';
    body.addEventListener('scroll', function (e) {
        const t = e.target;
        if (!t || t === body || !t.matches || !t.matches(_FEED_SEC_BODY_SEL)) return;
        if (t.scrollTop <= 4) {
            const sec = t.closest('[data-sec]');
            if (sec && sec.querySelector('.feed-sec-newchip')) _feedClearSecChip(sec);
        }
    }, { capture: true, passive: true });
    body.addEventListener('click', function (e) {
        const chip = e.target && e.target.closest && e.target.closest('.feed-sec-newchip');
        if (!chip) return;
        e.stopPropagation();
        const sec = chip.closest('[data-sec]');
        const sb = sec && sec.querySelector(_FEED_SEC_BODY_SEL);
        if (sb) _feedScrollSecTop(sb);
        _feedClearSecChip(sec);
    });
}

// refetch(소켓/부트) 전용 렌더 진입점 - 무변화면 스킵, 아니면 앵커 잡고 renderFeedPane 후 복원.
function _feedRefetchRender() {
    const body = document.getElementById('feed-body');
    if (!body) { renderFeedPane(); return; }

    // [v6.7.731] 질의 키가 바뀐 응답(축 토글·게임 필터 변경·첫 로드)이면 이것은 '같은 목록의
    //   갱신' 이 아니라 '다른 목록' 이다. 앵커 복원·unseen 적립은 의미가 없고, 스크롤 내린 채
    //   토글하면 새 목록의 윗글 전부가 가짜 '새 글 N건' 으로 집계된다. 새로 그리고 맨 위로 보낸다.
    //   (renderFeedPane 을 _feedRestore=null 로 부르면 그쪽이 '필터·토글 렌더' 로 처리해 unseen 을
    //   비우고 앵커 복원을 건너뛴다. _feedMarkPainted 가 _feedRenderedQueryKey 를 이 키로 갱신한다.)
    if (_feedCacheGamesKey !== _feedRenderedQueryKey) {
        _feedRenderedQueryKey = _feedCacheGamesKey;   // 결과가 0건(빈 상태)이어도 이 질의로 그렸음을 기록
        _feedUnseen.clear();
        _feedHideNewPill();
        _feedRestore = null;
        renderFeedPane();
        try { body.scrollTop = 0; } catch (_) {}
        return;
    }

    const sig = _feedRenderSig();
    const painted = body.dataset.painted === '1';
    const hdr = document.getElementById('feed-count-header') || document.getElementById('feed-count');
    const hdrPainted = !!(hdr && hdr.dataset.painted === '1');
    if (painted && hdrPainted && sig === _feedLastRenderSig) return;   // 데이터·입력·DOM 무변화 → 순단 없이 생략

    const viewMode = _getFeedViewMode();
    const isClassify = (viewMode === 'by-game' || viewMode === 'by-community');
    const st = body.scrollTop;
    if (isClassify) {
        // [v6.7.692] 분류 모드는 **섹션(게임/출처)마다 자체 스크롤**이다(.feed-game-section-body /
        //   .feed-community-frame-body). v6.7.688 은 바깥 #feed-body scrollTop 만 보존해서, 데이터가
        //   바뀌면 innerHTML 재생성으로 **모든 섹션 스크롤이 맨 위로 튕기고** "새 글" 표시도 없었다
        //   (사용자 라이브 보고 2026-09-26). 섹션 키(data-sec)별로 앵커를 잡는다.
        _feedRestore = { classify: true, scrollTop: st, secs: _feedCaptureSections(body) };
    } else {
        const cards = Array.from(body.querySelectorAll('.feed-card[data-href], .feed-data-table tbody tr[data-href]:not(.feed-detail-row)'));
        const bodyTop = body.getBoundingClientRect().top;
        let anchor = null;   // 첫 '보이는' 카드 (아래로 넘어간 첫 카드)
        for (const el of cards) {
            const r = el.getBoundingClientRect();
            if (r.bottom > bodyTop + 1) { anchor = el; break; }
        }
        _feedRestore = {
            classify: false,
            atTop: st <= 4,
            scrollTop: st,
            link: anchor ? anchor.dataset.href : null,
            topDelta: anchor ? (anchor.getBoundingClientRect().top - bodyTop) : 0,
            domLinksBefore: new Set(cards.map(c => c.dataset.href).filter(Boolean)),
            prevRendered: (_infiniteScroll['feed-body'] && _infiniteScroll['feed-body'].rendered) || 0,
        };
    }
    renderFeedPane();
    _feedApplyRestore();
    _feedRestore = null;
}

// renderFeedPane 직후 스크롤 앵커 복원 + "새 글 N건" pill 판정. getBoundingClientRect 델타로
//   복원하므로 offsetParent 가 무엇이든 정확하다.
function _feedApplyRestore() {
    const r = _feedRestore;
    const body = document.getElementById('feed-body');
    if (!r || !body) return;
    if (r.classify) { _feedRestoreSections(body, r); return; }
    if (r.atTop) { _feedUnseen.clear(); _feedHideNewPill(); return; }   // 맨 위면 새 글이 위에 붙어도 그대로 보이게 둔다

    // 카드(무한스크롤) 모드 - 앵커가 첫 50 batch 밖이면 나올 때까지 batch 확보(이전 렌더 수 이상).
    const _sel = r.link ? ('[data-href="' + CSS.escape(r.link) + '"]:not(.feed-detail-row)') : null;
    let anchorEl = _sel ? body.querySelector(_sel) : null;
    const iss = _infiniteScroll['feed-body'];
    if (iss) {
        let guard = 0;
        while (iss.rendered < iss.data.length && guard < 500) {
            const enough = iss.rendered >= r.prevRendered;
            // [v6.7.731] enough(이전 렌더 수 이상)에 닿으면 **앵커를 못 찾았어도** 멈춘다.
            //   종전 `(anchorEl || !_sel) && enough` 는 앵커가 소실(ack·필터 탈락·질의 변경)되면
            //   영영 거짓이라 데이터 전량을 동기로 렌더해 프리즈했다(한도 0 이면 최대 1만 장).
            //   앵커는 이전에 '보이던' 첫 카드라 enough 범위 안에서 다 그려지므로, 살아있으면
            //   이 시점에 이미 잡혀 있다. 못 잡았으면 아래 else 가 근사 복원으로 떨어진다.
            if (enough) break;
            _renderNextBatch('feed-body');
            guard++;
            if (_sel && !anchorEl) anchorEl = body.querySelector(_sel);
        }
    }
    if (anchorEl) {
        const cur = anchorEl.getBoundingClientRect().top - body.getBoundingClientRect().top;
        body.scrollTop += (cur - r.topDelta);
        const cards = body.querySelectorAll('.feed-card[data-href], .feed-data-table tbody tr[data-href]:not(.feed-detail-row)');
        for (const el of cards) {   // 앵커 위로 새로 들어온 글(렌더 전 DOM 에 없던 링크)을 "못 본 글" 에 적립
            if (el === anchorEl) break;
            if (!r.domLinksBefore.has(el.dataset.href)) _feedUnseen.add(el.dataset.href);
        }
    } else {
        body.scrollTop = r.scrollTop;   // 앵커 소실(ack/필터 탈락) - 최선의 근사로 이전 위치 유지
    }
    _feedSyncUnseenPill();
}

function renderFeedPane() {
    const body = document.getElementById('feed-body');
    const empty = document.getElementById('feed-empty-state');
    if (!body) return;
    _feedHideNewPill();   // 실제 렌더 시 stale pill 제거 (refetch 는 _feedApplyRestore 가 다시 띄운다)
    if (!_feedRestore) _feedUnseen.clear();   // [v6.7.694] 필터·토글 렌더는 목록 구성이 바뀌므로 못 본 글 집합도 리셋
    const data = _feedTotalCache;

    if (data === null) {
        _renderFeedEmptyState('loading');
        loadFeedPane();
        return;
    }

    // [v6.7.48] 분류 모드 dispatch. body 클래스 토글 — grid-2col 활성/비활성.
    // [v6.7.55] 분류 모드도 _applyFeedFilters 거친 list 사용 — 사이드바 필터 (visibility/limits) 영향 적용.
    const _viewMode = _getFeedViewMode();
    body.classList.toggle('feed-body--grid-2col', _viewMode === 'by-game' || _viewMode === 'by-community');
    if (_viewMode === 'by-game' || _viewMode === 'by-community') {
        if (empty) empty.style.display = 'none';
        const rawHasData = Array.isArray(data) && data.length > 0;
        if (!rawHasData) {
            // [v6.7.673] 분류 모드도 같다 - 서버 축이 켜져 있으면 필터 쪽 문구다(위 주석).
            _renderFeedEmptyState(_feedServerAxisOn() ? 'empty-filtered' : 'empty-no-data', _viewMode);
            return;
        }
        const filteredArticles = _applyFeedFilters(data);
        if (filteredArticles.length === 0) {
            _renderFeedEmptyState('empty-filtered', _viewMode);
            return;
        }
        if (_viewMode === 'by-game') {
            // [v6.7.123] visibility ON 게임 list 박제 + _groupByGame 에 전달 — 글 0건 게임도 section 표시.
            const visSettings = (_localSettingsData && _localSettingsData.visibility) ? _localSettingsData.visibility : {};
            const limitsCfg = (_localSettingsData && _localSettingsData.limits) ? _localSettingsData.limits : {};
            // visibility 키 + limits 키 union — 사이드바 노출 게임 전체. visibility false 명시 제외.
            const allKeys = new Set([...Object.keys(visSettings), ...Object.keys(limitsCfg)]);
            const visibleGames = Array.from(allKeys).filter(g => visSettings[g] !== false);
            renderFeedByGame(_groupByGame(filteredArticles, visibleGames), body);
        } else {
            renderFeedByCommunity(_groupByCommunity(filteredArticles), body);
        }
        // [v6.7.407→422] 도착 잔광 — diff 소스를 filtered→**전체 캐시(_feedTotalCache)** 로 (댓글 탭 allComments 방식 정합).
        //   filtered 기준이면 필터 한도 변경·timeline↔분류 모드 전환 시 집합이 달라져 옛 글이 "신규 도착" 오판
        //   → 대량 스퓨리어스 잔광(사용자 "60초 맞아?" 재검토에서 발견). 전체 기준이면 뷰 구성 무관 데이터셋-레벨 감지.
        const newLinksC = new Set((Array.isArray(_feedTotalCache) ? _feedTotalCache : []).map(a => a && a.link).filter(Boolean));
        _recordFeedArrivals(newLinksC, _feedPrevLinks);
        _feedPrevLinks = newLinksC;
        _applyFeedGlow(body);
        // [v6.7.200] FIX — by-game/by-community 렌더 직후 section height 재박제.
        //   _setupFeedGameSectionResize 가 노출돼 있으나(line 799) 어떤 render 경로도 호출 안 해
        //   ResizeObserver/MutationObserver 가 timeline 부팅 시점엔 grid 부재로 미부착 → 데이터 갱신/
        //   표↔카드 토글 시 새 section 에 inline height 미적용 → CSS min-height fallback 만 남아 전체
        //   글 길이로 무한 확장("너무 김"). 창 높이 살짝 조절 시 pane ResizeObserver 가 뒤늦게 재계산해
        //   복구되던 증상. 모든 재렌더가 renderFeedPane 경유 + renderFeedByGame/Community 가 여기서만
        //   호출되므로 이 한 곳이 전 경로(socket 갱신/부팅/필터/탭 전환/뷰·레이아웃 토글) 커버.
        //   _setupFeedGameSectionResize: (1) grid 존재 시점에 observer 멱등 부착 (2) rAF 로 height 적용.
        if (typeof window._setupFeedGameSectionResize === 'function') window._setupFeedGameSectionResize();
        _reapplyFeedSearch();   // [v6.7.443] 검색 hide 상태 재적용 (아래 함수 주석 참조)
        _feedAiRepaint();   // [v6.7.459] AI 요약 행 복원 (같은 누락 감사 목록에 합류)
        _feedMarkPainted();   // [v6.7.688] 무변화 스킵용 시그니처·DOM 마킹
        return;
    }

    // [v6.7.731] timeline 필터·정렬은 분류 모드와 **같은 정책**이다 - 복사본이던 이 블록을
    //   _applyFeedFilters 호출로 교체(동작 불변: 정렬 비교자·필터 조건이 글자 단위 동일했다).
    //   정책이 한 곳이 되어 축이 늘 때 갈라지지 않는다.
    const filtered = _applyFeedFilters(data);
    _feedRenderCount = filtered.length;   // [v6.7.738] 검색 해제 시 복원용 전체 건수
    _updateFeedCount(filtered.length);
    if (filtered.length === 0) {
        // [v6.7.46] 0건 케이스 — raw 데이터 자체가 0 vs 필터로 0 분리 (UX 메시지 정확화).
        // [v6.7.673] **서버 축이 켜져 있으면 `data` 가 이미 걸러진 결과다.** 그때의 0건은
        //   "수집이 없다" 가 아니라 "이 필터에 맞는 글이 없다" 인데, 종전에는 `data.length===0`
        //   하나로 갈라 *"크롤러가 새 글을 수집하면 자동으로 표시됩니다"* 를 띄웠다 - 수집은
        //   멀쩡한데 멎은 것처럼 읽힌다. `네이버 + 제재` 처럼 **항상 0건인 조합**(판정은
        //   공식 게시판만 한다)에서는 그 거짓말이 상시로 뜬다.
        const rawHasData = Array.isArray(data) && data.length > 0;
        _renderFeedEmptyState((rawHasData || _feedServerAxisOn()) ? 'empty-filtered' : 'empty-no-data');
        return;
    }
    if (empty) empty.style.display = 'none';

    // [v6.7.6→422] 도착 감지 — diff 소스를 filtered→전체 캐시(_feedTotalCache)로 (classify 경로와 동일 사유:
    //   필터 한도 변경·모드 전환 시 filtered 집합 변화가 옛 글을 신규 오판 → 스퓨리어스 잔광 차단).
    const prevLinks = _feedPrevLinks;
    const newLinks = new Set((Array.isArray(_feedTotalCache) ? _feedTotalCache : []).map(a => a && a.link).filter(Boolean));
    _feedPrevLinks = newLinks;

    // [v6.7.168→171] 무한 스크롤 — 하드코딩 렌더 캡(slice) 폐지. 필터 허용량 전부, DOM 은 스크롤 시 50 장씩.
    //   [v6.7.171] FIX 2건: (1) observer root 를 실제 스크롤러 #feed-body(overflow-y:auto) 로 교정 —
    //   v6.7.168 은 #feed-tab(.tab-content, overflow:hidden) 을 root 로 써서 sentinel 이 클리핑→카드가 첫 50 에서
    //   멈췄음("무한 드래그 안됨"). (2) 표·카드 동시 렌더(숨김 표 최대 1만행) 오버헤드 제거 — 활성 레이아웃만 렌더.
    _recordFeedArrivals(newLinks, prevLinks);   // [v6.7.407] 신규 링크 최초관측 기록 (glow 는 렌더 후 _applyFeedGlow)

    const layoutMode = _getFeedLayoutMode();
    body.innerHTML = '';
    if (layoutMode === 'table') {
        // 표 모드 — 전체 1회 렌더 (opt-in; 고건수 시 무거울 수 있음 — 향후 lazy 화 후보).
        body.insertAdjacentHTML('beforeend', _feedAsTableHtml(filtered));
        _applyFeedGlow(body);   // [v6.7.407] 표 행 도착 잔광
    } else {
        // 카드 모드 — 무한 스크롤. container = #feed-body(실제 스크롤러). 카드는 sentinel(맨 끝) 앞에 삽입.
        _initInfiniteScroll('feed-body', filtered, 50, (batch, cid) => {
            const _s = body.querySelector('.infinite-scroll-sentinel');
            const _html = batch.map(a => _feedArticleCard(a)).join('');
            if (_s) _s.insertAdjacentHTML('beforebegin', _html);
            else body.insertAdjacentHTML('beforeend', _html);
            _applyFeedGlow(body);   // [v6.7.407] 무한스크롤 batch 별 도착 잔광 (Map 기반 idempotent)
            _reapplyFeedSearch();   // [v6.7.443] 새로 붙은 batch 에도 검색 hide 적용 (미적용 시 스크롤할수록 비대상 글이 섞여 나옴)
            _feedAiRepaint();   // [v6.7.459] AI 요약 행 복원 (같은 누락 감사 목록에 합류)
        });
    }
    _reapplyFeedSearch();   // [v6.7.443] 검색 hide 상태 재적용 (아래 함수 주석 참조)
    _feedAiRepaint();   // [v6.7.459] AI 요약 행 복원 (같은 누락 감사 목록에 합류)
    _feedMarkPainted();   // [v6.7.688] 무변화 스킵용 시그니처·DOM 마킹
}

// [v6.7.443] 피드 검색은 데이터 필터가 아니라 **DOM hide/show** 다(_filterFeedHeaderImpl). 그래서
//   renderFeedPane 이 body.innerHTML 을 새로 그리면 hide 상태가 통째로 증발한다 - 검색창엔 글자가
//   남아 있는데 목록은 전건이 되는 불일치. 수동 토글 경로(setFeedLayoutMode/setFeedViewMode)는
//   v6.7.26 부터 재적용을 붙여 뒀지만, **정작 가장 빈번한 경로인 소켓 재렌더**(raw_articles_update →
//   800ms 디바운스 loadFeedPane, 크롤 사이클마다)와 무한스크롤 batch 추가엔 없었다. 즉 검색해 놓고
//   보는 중에 크롤이 한 번 돌면 필터가 조용히 풀렸다(디스코드 탭이 "피드탭 옛 검색 취소 재발 방지"
//   라고 주석에 남긴 그 증상의 잔존분). 디바운스 없는 impl 직접 호출 - 렌더 직후 같은 프레임에서
//   맞춰야 깜빡임이 없다.
function _reapplyFeedSearch() {
    const el = document.getElementById('feed-search-input-header');
    if (!el || !el.value || !el.value.trim()) return;   // 검색 중이 아니면 비용 0
    try { _filterFeedHeaderImpl(); } catch (e) { console.warn('[feed] 검색 재적용 실패', e); }
}

// [v6.6.424] 슬림 카드 마크업 — 모바일 카드보다 한 단계 더 컴팩트.
function _feedArticleCard(a) {
    const sourceUpper = (a.source_community || a.source || '').toUpperCase();
    const sourceText = _feedSourceLabel(sourceUpper);
    const gameColor = (typeof window.getGameColor === 'function')
        ? window.getGameColor(a.game) : 'var(--primary-color)';
    let cardClass = 'feed-card';
    // [v6.7.26] AI Pick 분기 제거 — ai_processor 가 시트에 'AI 픽' 키워드를 더 이상 쓰지 않음. dead branch 청소.
    // [v6.7.731] freshnessClass no-op 블록 삭제 (v6.7.407 이후 항상 '' 반환).

    const title = (typeof highlightKeywords === 'function')
        ? highlightKeywords(a.title || '', a.detected_keywords, a.is_super_keyword)
        : escapeHtml(a.title || '');
    const time = (typeof formatTimeFull === 'function')
        ? escapeHtml(formatTimeFull(a.timestamp || a.date))
        : escapeHtml(a.timestamp || a.date || '');

    // [v6.7.317] 더보기 확장 — expanded 면 클래스 + 상세 baked (재렌더에도 유지).
    const _key = a.link || '';
    const _exp = _key && _feedExpandedKeys.has(_key);
    if (_exp) cardClass += ' expanded';
    cardClass += _feedVerdictCls(a);   // [v6.7.664] 제재 판정 좌측 바

    // [v6.7.321] 더보기 버튼을 head wrapper 우측 세로 바로 분리 — 우측 끝 세로 전체가 클릭 존(오클릭 방지).
    //   detail 은 head 밖(카드 직속)이라 존이 상세 링크복사 버튼을 덮지 않음.
    return `
        <div class="${cardClass}" onclick="openArticle(this.dataset.href)" data-href="${escapeHtml(a.link || '')}" data-ts="${escapeHtml(a.timestamp || a.date || '')}" data-source="${escapeHtml(sourceUpper)}">
            <div class="feed-card-head">
                <div class="feed-card-title" title="${escapeHtml(a.title || '')}">${title}</div>
                <div class="feed-card-meta">
                    ${_feedVerdictChipHtml(a)}
                    ${_feedSourceBadgeHtml(a, sourceUpper, sourceText)}
                    ${a.game ? `<span class="feed-card-game" data-gc="${escapeHtml(a.game)}" style="color:${gameColor};">${escapeHtml(a.game)}</span>` : ''}
                    <span class="feed-card-time">${time}</span>
                </div>
                ${_feedMoreBtnHtml(_key)}
            </div>
            ${_exp ? _feedCardDetailHtml(a) : ''}
        </div>`;
}

// [v6.7.17] PC 표 마크업 — 4열(게임/구분/제목/시간). 컬럼 순서 사용자 요구 반영.
//   뱃지: 카드 모드의 .feed-card-source 클래스 그대로 재사용 — 톤·border-radius 통합.
function _feedAsTableHtml(rows) {
    if (!rows || rows.length === 0) return '<div class="feed-data-table-wrap"></div>';
    const trs = rows.map(a => {
        const sourceUpper = (a.source_community || a.source || '').toUpperCase();
        const sourceText = _feedSourceLabel(sourceUpper);
        const sourceCls = 'feed-card-source--' + sourceUpper.toLowerCase();
        const gameColor = (typeof window.getGameColor === 'function') ? window.getGameColor(a.game) : 'var(--primary-color)';
        const title = (typeof highlightKeywords === 'function')
            ? highlightKeywords(a.title || '', a.detected_keywords, a.is_super_keyword)
            : escapeHtml(a.title || '');
        const time = (typeof formatTimeFull === 'function')
            ? escapeHtml(formatTimeFull(a.timestamp || a.date))
            : escapeHtml(a.timestamp || a.date || '');
        // [v6.7.731] freshnessClass no-op 블록 삭제 (v6.7.407 이후 항상 '' 반환).
        let rowCls = '';
        // [v6.7.317] 더보기 확장 — col-more 버튼 + expanded 시 본 행 뒤 상세 행 baked (colspan 5).
        const _key = a.link || '';
        const _exp = _key && _feedExpandedKeys.has(_key);
        if (_exp) rowCls += ' expanded';
        rowCls += _feedVerdictCls(a);   // [v6.7.664] 제재 판정
        return `<tr class="${rowCls.trim()}" onclick="openArticle(this.dataset.href)" data-href="${escapeHtml(a.link || '')}" data-ts="${escapeHtml(a.timestamp || a.date || '')}" data-source="${escapeHtml(sourceUpper)}">
            <td class="col-game"><span class="col-game-name" data-gc="${escapeHtml(a.game || '')}" style="color:${gameColor};">${escapeHtml(a.game || '')}</span></td>
            <td class="col-source">${_feedVerdictChipHtml(a)}${_feedSourceBadgeHtml(a, sourceUpper, sourceText, sourceCls)}</td>
            <td class="col-title" title="${escapeHtml(a.title || '')}"><span class="col-title-text">${title}</span></td>
            <td class="col-time">${time}</td>
            <td class="col-more">${_feedMoreBtnHtml(_key)}</td>
        </tr>` + (_exp ? _feedDetailRowHtml(a, 5) : '');
    }).join('');
    return `<div class="feed-data-table-wrap"><table class="feed-data-table">
        <thead><tr><th class="col-game">게임</th><th class="col-source">구분</th><th class="col-title">제목</th><th class="col-time">시간</th><th class="col-more" aria-label="상세 보기"></th></tr></thead>
        <tbody>${trs}</tbody>
    </table></div>`;
}

function _updateFeedCount(n) {
    // [v6.7.688] dataset.painted 마킹 - render.js 가 헤더를 재생성하면 이 마커가 사라져
    //   무변화 스킵이 금지된다(PATTERN-38 (4)). 미마킹이면 "목록은 있는데 헤더 0건" 이 굳는다.
    const a = document.getElementById('feed-count');
    const b = document.getElementById('feed-count-header');
    if (a) { a.textContent = n; a.dataset.painted = '1'; }
    if (b) { b.textContent = n; b.dataset.painted = '1'; }
}

function _filterFeedHeaderImpl() {
    const headerInput = document.getElementById('feed-search-input-header');
    const q = (headerInput ? headerInput.value : '').trim().toLowerCase();
    let visible = 0;   // [v6.7.738] 검색 중 '보이는 개수' 집계 (카드·표 공통, 상세·스켈레톤 제외)
    document.querySelectorAll('#feed-body .feed-card').forEach(card => {
        const txt = (card.textContent || '').toLowerCase();
        const show = (!q || txt.includes(q));
        card.style.display = show ? '' : 'none';
        if (show && !card.classList.contains('skeleton-row')) visible++;   // 로딩 스켈레톤은 건수 대상 아님
    });
    // [v6.7.22] 표 모드도 동일 hide/show — 카드 ↔ 표 토글에 무관하게 검색 일관 동작
    // [v6.7.317] 상세 행(.feed-detail-row)은 자체 매칭 제외 — 본 행 표시 상태를 그대로 따라감.
    document.querySelectorAll('#feed-body .feed-data-table tbody tr').forEach(tr => {
        if (tr.classList.contains('feed-detail-row')) return;
        const txt = (tr.textContent || '').toLowerCase();
        const show = (!q || txt.includes(q));
        tr.style.display = show ? '' : 'none';
        const next = tr.nextElementSibling;
        if (next && next.classList.contains('feed-detail-row')) next.style.display = show ? '' : 'none';
        if (show) visible++;
    });
    // [v6.7.738] 검색어가 있는 동안은 헤더 건수를 '보이는 개수' 로(승인 개선 #10). 비우면 렌더가 센
    //   전체 건수(_feedRenderCount)로 되돌린다 - 무한 스크롤이라 DOM 카드 수로는 전체 복원 불가.
    _updateFeedCount(q ? visible : _feedRenderCount);
}
// [v6.6.423] 검색 디바운스 150ms — 다른 탭과 일관.
let _feedFilterTimer = null;
function filterFeedHeader() {
    if (_feedFilterTimer) clearTimeout(_feedFilterTimer);
    _feedFilterTimer = setTimeout(() => { _feedFilterTimer = null; _filterFeedHeaderImpl(); }, 150);
}

// [v6.7.407] 구 60초 freshness 재분류 인터벌 제거 — 엣지스트립/틴트 폐지로 applyFreshness no-op 화.
//   도착 잔광은 CSS animation-fill-mode:both 로 스스로 만료 유지(JS 주기 재적용 불요), Map 정리는 렌더 시점 _recordFeedArrivals 가 담당.

window.loadFeedPane = loadFeedPane;
window.filterFeedHeader = filterFeedHeader;

// [v6.7.16] 토글 클릭 위임 + 부트 시 모드 적용. 헤더가 동적 렌더되므로 document level 위임.
document.addEventListener('click', function (e) {
    const btn = e.target.closest && e.target.closest('.layout-toggle[data-target="feed"] .layout-toggle-btn');
    if (!btn) return;
    const mode = btn.dataset.mode;
    if (mode === 'table' || mode === 'cards') window.setFeedLayoutMode(mode);
}, true);

// [v6.7.48] 분류 토글 클릭 위임 — timeline / by-game / by-community.
document.addEventListener('click', function (e) {
    const btn = e.target.closest && e.target.closest('.feed-view-toggle .feed-view-toggle-btn');
    if (!btn) return;
    const mode = btn.dataset.mode;
    if (mode === 'timeline' || mode === 'by-game' || mode === 'by-community') {
        window.setFeedViewMode(mode);
    }
}, true);

// [v6.7.664] 출처 pill + 제재 토글 클릭 위임. 둘 다 같은 줄에 있으므로 한 리스너에서 가른다.
document.addEventListener('click', function (e) {
    if (!e.target.closest) return;
    // [v6.7.731] 피드 pill 로만 한정 - 판정 탭이 같은 .feed-srcpills/.feed-srcpill 클래스를 쓰므로
    //   data-source / data-flagtoggle(피드 전용 속성)로 가른다. 안 가르면 이 document capture 위임이
    //   판정 pill 클릭을 먼저 받아 setFeedSource('') 로 피드 필터를 풀고 LS 를 덮었다.
    const fp = e.target.closest('.feed-srcpills .feed-flagpill[data-flagtoggle]');
    if (fp) { window.setFeedFlagged(!_getFeedFlagged()); return; }
    const sp = e.target.closest('.feed-srcpills .feed-srcpill[data-source]');
    if (sp) window.setFeedSource(sp.dataset.source || '');
}, true);

// 부트 / 헤더 재렌더 후 모드 동기화 — body class 적용 + 활성 버튼 갱신.
function _initFeedLayoutMode() {
    const layoutMode = _getFeedLayoutMode();
    const viewMode = _getFeedViewMode();
    _syncLayoutToggleUI('feed', layoutMode);
    _syncFeedViewToggleUI(viewMode);
    _syncFeedSourceBarUI();   // [v6.7.664] 헤더가 동적 렌더라 부트·재렌더마다 활성 pill 재도포
    // [v6.7.49] view × layout 협력 — viewMode 가 classify 면 카드 강제 + 폭 viewMode 우선.
    _applyFeedClassifyBodyClass(viewMode);
    _applyFeedPaneWidthForViewAndLayout(viewMode, layoutMode);
}
// [v6.7.731] window._initFeedLayoutMode export 삭제 - 외부 호출처 0(레포 전체 grep). 내부 부트 배선만 쓴다.
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _initFeedLayoutMode);
} else {
    _initFeedLayoutMode();
}

// [v6.7.120] by game / by community grid section height 동적 박제 — JS ResizeObserver.
//   CSS only 한계: 부모 pane height chain 전파 부분 박제 안 됨 + clamp vh 는 pane 분리.
//   JS 가 pane.clientHeight 직접 측정 + section_height = max(MIN_5_ROWS, pane_inner / row_count) 박제.
//   사용자 의도 (2026-05-22): pane 늘리면 section 비례 grow + 5 row 기본 보장 + pane 줄이면 부분 가려짐.
(function () {
    let _resizeObserver = null;
    let _mutationObserver = null;
    let _observedPane = null;
    let _observedGrid = null;
    // [v6.7.731] rAF 재시도 상한 - 피드 pane 이 끝내 안 뜨는 페이지(탭 미개방)에서 아래 재시도가
    //   매 프레임 영구히 돌던 것(진입점 3개 × 무한)을 막는다. pane 을 찾으면 리셋되고, 나중에 탭을
    //   열면 renderFeedPane 의 window._setupFeedGameSectionResize 호출이 다시 건다.
    let _resizeRetries = 0;
    const _RESIZE_RETRY_MAX = 150;   // 약 2.5초치 프레임 (기존 0.5s/2s 보강 타이머와 같은 창)

    function _calcFeedGameSectionHeights() {
        const grid = document.querySelector('#feed-tab .feed-body--grid-2col');
        if (!grid) return;
        const sections = grid.querySelectorAll('.feed-game-section, .feed-community-frame');
        if (!sections.length) return;
        const parent = grid.parentElement;
        if (!parent) return;
        const innerH = parent.clientHeight;
        if (innerH <= 0) return;

        const isCards = document.body.classList.contains('feed-mode-cards');
        const MIN = isCards ? 340 : 230;  // 5 row 보장 (table 28×5+thead+header+padding / cards 50×5+gap+header+padding)

        const rowCount = Math.ceil(sections.length / 2);
        const gap = 12;
        const computed = Math.floor((innerH - gap * (rowCount - 1)) / rowCount);
        const h = Math.max(MIN, computed);

        sections.forEach(function (s) {
            s.style.height = h + 'px';
            s.style.maxHeight = h + 'px';
        });
    }

    function _setupFeedGameSectionResize() {
        const grid = document.querySelector('#feed-tab .feed-body--grid-2col');
        const pane = document.querySelector('#feed-tab')?.closest('.pane-window');

        // grid / pane 가 늦게 mount 될 수 있음 - 미발견 시 다음 frame 재시도(상한까지).
        if (!grid && !pane) {
            if (_resizeRetries++ < _RESIZE_RETRY_MAX) requestAnimationFrame(_setupFeedGameSectionResize);
            return;
        }
        _resizeRetries = 0;   // [v6.7.731] pane 을 찾았으니 다음 부재 구간을 위해 예산 리셋

        if (pane && pane !== _observedPane) {
            if (_resizeObserver) _resizeObserver.disconnect();
            _resizeObserver = new ResizeObserver(function () {
                requestAnimationFrame(_calcFeedGameSectionHeights);
            });
            _resizeObserver.observe(pane);
            _observedPane = pane;
        }

        if (grid && grid !== _observedGrid) {
            if (_mutationObserver) _mutationObserver.disconnect();
            _mutationObserver = new MutationObserver(function () {
                requestAnimationFrame(_calcFeedGameSectionHeights);
            });
            _mutationObserver.observe(grid, { childList: true, subtree: false });
            _observedGrid = grid;
        }

        requestAnimationFrame(_calcFeedGameSectionHeights);
    }

    // 외부 호출용 — feed-tab.js 의 render 함수에서 명시 호출 가능.
    window.updateFeedGameSectionHeights = _calcFeedGameSectionHeights;
    window._setupFeedGameSectionResize = _setupFeedGameSectionResize;

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', _setupFeedGameSectionResize);
    } else {
        _setupFeedGameSectionResize();
    }
    // grid 가 더 늦게 mount 되는 시나리오 보강 — 0.5s / 2s retry.
    setTimeout(_setupFeedGameSectionResize, 500);
    setTimeout(_setupFeedGameSectionResize, 2000);
})();
