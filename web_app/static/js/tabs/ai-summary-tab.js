// tabs/ai-summary-tab.js — AI 요약 탭 (코어 멀티액티브 페인, admin + manager)
//   데이터: GET /api/ai-summary — `_require_role('manager')` 가드 ([v6.7.386] 배너/보관소 개방 때 완화).
//     이 탭이 치는 API 는 이것 하나뿐이다. 자매 라우트 `/api/ai-summary/{request,label,labels,for-link}`
//     는 `_require_admin` 이지만 소비처가 feed-tab.js 라 이 탭의 매니저 동선에는 안 걸린다.
//   접근: [v6.7.313] manager 회수 → [v6.7.500] manager 재개방. 게이트 4계층 = 서버 API(analysis.py) ·
//     정적 JS 403(app.py `_ADMIN_MANAGER_JS`) · Jinja 은닉 5곳(index CSS/pane/script + sidebar + bottom_tabbar) ·
//     클라 role(ui-control.js `MANAGER_EXTRA_*`). 한 곳만 빠져도 "Tab not found" 나 렌더 함수 부재로 샌다(v6.7.430).
//     주의: 이 주석은 v6.7.386~499 동안 가드를 `admin` 이라 잘못 말하고 있었다 — 권한은 코드 게이트로 확인할 것.
//   렌더: 댓글 탭 1:1 미러 — 평면 리스트(게임별 섹션 그룹핑 안 함) + 글 탭 visibility 필터 적용(feed 동일 _localSettingsData.visibility).
//   표(.ai-summary-data-table) ↔ 카드(.feed-card.feed-card--ai) 둘 다 렌더, 댓글 탭처럼 layout-toggle(표/카드)로 전환
//   (body.ai-summary-mode-{table,cards}). 모바일(pointer:coarse)은 카드 강제. 기본 모드 = table.
//   [v6.7.313] 게임 pill(탭 로컬 필터) + 행 클릭 요약 전문 확장(원문=제목 링크) + 갱신 시각 + 검색 하이라이트.
//   [v6.7.315] 가독성 — 배지 2개(주의+부정) 나열 → 심각도 리딩 도트 1개 + 상단 심각도/감정 카운트 칩(클릭 필터).
//   [v6.7.316] P5 — critical 행만 좌측 1px 보더(.ai-sum-crit). PATTERN-26 승인 예외(2026-07-12 사용자 결정).
//   [v6.7.318] 확장 상세 필드 — 카카오 요약 알림 폼 구조 등가. [v6.7.329] 순서 = 게임/일시/게시판/요약/제목/내용(800자)/링크+복사.
//     상세는 확장 시 지연 주입(전 행 baked 시 내용 800자 × 500행 DOM 비대 회피) + 재렌더는 STATE._expanded 기준 baked.
//   전역 헬퍼 재사용(재정의 금지): escapeHtml / formatTime / window.getGameColor.
(function () {
    'use strict';

    const AI_ARRIVAL_FADE_S = 150;  // [v6.7.410] 도착 잔광 지속(초) — 전탭 절반 감축(300→150). AI 유입 ~5분+ 간격이라 여전히 최장(디스코드 30s 와 별도)

    // ── 모듈 캐시 (PATTERN-32 — fetch 실패 시 기존 데이터 보존) ──
    const STATE = {
        lastData: null,        // 마지막 성공 fetch 결과(원본 배열)
        _game: 'ALL',          // [v6.7.313] 게임 pill 선택 (탭 로컬 — 전역 visibility 설정 비접촉)
        _query: '',            // [v6.7.313] 검색어 (render 단일 경로 필터 + 하이라이트 원문)
        _fetchedAt: null,      // [v6.7.313] 마지막 성공 fetch 시각 (신선도 표시)
        _expanded: new Set(),  // [v6.7.313] 확장된 행 key (재렌더/소켓 push 에도 유지)
        _sev: 'ALL',           // [v6.7.315] 심각도/감정 칩 필터 (ALL|critical|warning|negative — 재클릭 해제)
        _lastSig: null,        // [v6.7.330 P2] 마지막 렌더 데이터 시그니처 — 소켓 재fetch 무변화 시 통째 재렌더 스킵
        _freshUntil: 0,        // [v6.7.350] 데이터 신선 창(ms epoch) — 서버 응답 캐시 TTL(30s)과 정렬. 창 내 재호출은 fetch 생략
        _prevLinks: new Set(), // [v6.7.387] 직전 렌더의 링크 집합 — 도착 잔광 diff(피드 _feedPrevLinks 패턴 이식)
        _flashArrivals: false, // [v6.7.387] 다음 렌더에서 신규 도착분 기록 여부 — _ingest(소켓/fetch 데이터 변화)에서만 set
        _arrivalAt: new Map(), // [v6.7.403] link → 화면 첫 관측 시각(ms) — 10s 잔광의 negative delay 기준(재렌더에도 이어짐)
        _backlog: null,        // [v6.7.686] 마지막 성공 backlog 응답({pending, oldest_lag_s, sample_age_s, threshold_s}) - PATTERN-32 캐시
    };

    /* ─── [v6.7.691] 진행형(청크) 렌더 상태 - 500행 동기 렌더(순단 주범)를 청크로 분할.
       feed 의 _initInfiniteScroll(highlight.js)는 sentinel 부모=관찰 root 가 한 요소로 묶여 있어
       여기선 못 쓴다: 이 탭의 스크롤러는 #ai-summary-tab 인데 행이 들어가는 곳은 #ai-summary-body 라
       root 와 sentinel 부모가 달라야 하고(피드 v6.7.168 sentinel 클리핑 회귀 방지), 앵커 복원을 위해
       "특정 인덱스까지 렌더"(_renderUpToIndex)가 필요하다. 그래서 이 탭 전용 최소 구현을 둔다. ─── */
    const AI_CHUNK = 50;            // 첫 페인트는 이만큼만 동기 렌더(40~60), 나머지는 스크롤 진입 시 증분
    let _io = null;                 // 진행형 렌더 IntersectionObserver (root=스크롤러)
    let _renderState = null;        // { sorted, rendered, insertFn }
    let _pendingNewCount = 0;       // [v6.7.691] 스크롤 중 도착한 신규 요약 누적 ('새 요약 N건' 배지)
    let _scrollHooked = false;      // 스크롤러 스크롤 리스너 1회 등록 가드

    /* ─── timestamp 파서 (댓글 탭 _parseCommentTs 동등 로직) ───
       풀 ISO("2026-05-14T13:00:00") 정확 파싱 + "HH:MM(:SS)" 부분 문자열은 오늘 가정,
       미래 시각이면 -1일(자정 횡단 케이스). 정렬용 ms 반환. */
    function _parseAiTs(ts) {
        if (!ts) return 0;
        const s = String(ts).trim();
        const d = new Date(s.replace(' ', 'T'));
        if (!isNaN(d.getTime())) return d.getTime();
        const m = s.match(/^(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?$/);
        if (m) {
            const now = new Date();
            const hh = +m[1], mm = +m[2], ss = m[3] ? +m[3] : 0;
            const dt = new Date(now.getFullYear(), now.getMonth(), now.getDate(), hh, mm, ss);
            if (dt.getTime() > now.getTime() + 60000) dt.setDate(dt.getDate() - 1);
            return dt.getTime();
        }
        return 0;
    }

    // 행에서 timestamp 후보 추출 (timestamp 우선, 없으면 date)
    function _rowTs(r) { return (r && (r.timestamp || r.date)) || ''; }
    // 행에서 원본 URL 추출
    function _rowHref(r) { return (r && r.link) || ''; }
    // 행에서 출처 키 추출 (data-source 부착용)
    function _rowSrcKey(r) {
        return String((r && (r.source_community || r.source)) || '').toUpperCase();
    }

    /* ─── [v6.7.313] 배지/하이라이트/확장 헬퍼 ─── */
    // 행 고유 key — link 우선, 없으면 제목+시간 (확장 상태 유지용).
    function _rowKey(r) { return _rowHref(r) || (((r && r.title) || '') + '|' + _rowTs(r)); }
    /* [v6.7.315] 심각도 리딩 도트 — 배지 2개(주의+부정) 나열이 대부분 행에서 동일 조합이라 노이즈 → 요약 앞 도트 1개.
       색 = severity 단일 기준(critical 적/warning 앰버/info 회색). sentiment 부정은 행 표기 생략 —
       상단 카운트 칩(부정 N) 필터로만 접근. PATTERN-26 준수: 인라인 소형 원 — 행 틴트·엣지스트립 아님. */
    function _sevDotHtml(r) {
        const sev = String((r && r.severity) || '').toLowerCase();
        const cls = (sev === 'critical') ? 'critical' : (sev === 'warning') ? 'warning' : 'info';
        const label = (cls === 'critical') ? '위험' : (cls === 'warning') ? '주의' : '일반';
        return '<span class="ai-sum-dot ai-sum-dot--' + cls + '" title="' + label + '"></span>';
    }
    // 검색어 하이라이트 - [v6.7.731] 원문 기준으로 매칭/분할한 뒤 조각별로 이스케이프한다 (XSS 안전).
    //   구 코드는 이미 이스케이프된 HTML 위에서 정규식을 돌려, 'amp' 같은 엔티티 조각(&amp;)을
    //   검색하면 엔티티 한가운데를 <mark> 로 갈라 "R&amp;D" 로 깨져 보였다(원문은 "R&D"). 이제
    //   매칭은 원문에서만 하고 이스케이프를 마지막에 조각 단위로 하므로 엔티티가 분리되지 않는다.
    function _hl(raw) {
        const s = String(raw == null ? '' : raw);
        const q = (STATE._query || '').trim();
        if (!q) return escapeHtml(s);
        const escQ = q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        let re;
        try { re = new RegExp(escQ, 'gi'); } catch (_) { return escapeHtml(s); }
        let out = '';
        let last = 0;
        let m;
        while ((m = re.exec(s)) !== null) {
            out += escapeHtml(s.slice(last, m.index)) + '<mark class="ai-sum-mark">' + escapeHtml(m[0]) + '</mark>';
            last = m.index + m[0].length;
            if (m.index === re.lastIndex) re.lastIndex++;   // zero-width 매치 무한루프 방어
        }
        return out + escapeHtml(s.slice(last));
    }
    // [v6.7.408] _isRecent(30분 파란 시간 강조) 제거 — 도착 잔광(300s 바이올렛)으로 recency 일원화.
    //   피드/댓글 freshness 제거(v6.7.407)와 동일 정신: 시간감쇠 색 신호는 잔광 하나로.

    /* ─── [v6.7.325] 출처 라벨/미니 배지 — 목록 단계에서 다음/DC/네이버/공식 식별 (사용자: "더보기 전엔 출처를 알 수 없음") ─── */
    function _srcLabel(r) {
        const u = _rowSrcKey(r);
        return u === 'DAUM' ? '다음' : (u === 'OFFICIAL_WEB' ? '공식' : (u === 'NAVER' ? '네이버' : (u === 'DC' ? 'DC' : '')));
    }
    // 미니 배지 — 색/배경은 공용 .feed-card-source--* 상속, 크기만 AI 밀도로 축소(.ai-sum-src).
    function _srcBadgeHtml(r) {
        const label = _srcLabel(r);
        if (!label) return '';
        return '<span class="feed-card-source feed-card-source--' + _rowSrcKey(r).toLowerCase() + ' ai-sum-src">' + label + '</span>';
    }

    /* ─── [v6.7.320] 더보기 버튼 — 피드 탭 인터랙션 통일 (행/카드 클릭=원문 이동, 이 버튼=상세 확장) ─── */
    //   구 v6.7.313 "행 클릭=확장" 폐지 — 피드(글 클릭=원문/우측 chevron=더보기)와 방향 반대라 사용자 혼란.
    function _aiMoreBtnHtml() {
        return '<button type="button" class="feed-more-btn ai-sum-more-btn" title="상세 보기" aria-label="상세 보기"><i class="fas fa-chevron-down" aria-hidden="true"></i></button>';
    }

    /* ─── [v6.7.318] 확장 상세 필드 — 카카오 요약 알림 폼 등가 ([v6.7.329] 요약도 그리드 필드로 편입 — 드래그 복사 안전 영역) ─── */
    const _AI_DETAIL_CONTENT_CAP = 800;  // 카카오 알림 preview_content 동일 상한
    // 본문 미수집 판정 — 피드 탭 _feedContentPending 동일 규칙 (content_extractor sentinel).
    function _aiContentPending(c) {
        const s = String(c || '').trim();
        if (!s) return true;
        return /^\[[A-Z_]+(:\d+)?\]$/.test(s);
    }
    // 일시 전체 표기 "YYYY-MM-DD HH:MM" — _parseAiTs(HH:MM 부분 문자열 오늘 가정 포함) 재사용.
    function _aiFullTs(ts) {
        const ms = _parseAiTs(ts);
        if (!ms) return String(ts || '-');
        const d = new Date(ms);
        const p = (n) => String(n).padStart(2, '0');
        return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
    }
    function _aiDetailFieldsHtml(r) {
        const href = _rowHref(r);
        let contentHtml;
        if (_aiContentPending(r && r.content)) {
            contentHtml = '<span class="ai-sum-detail-pending">본문 수집 중</span>';
        } else {
            let c = String(r.content).trim();
            if (c.length > _AI_DETAIL_CONTENT_CAP) c = c.slice(0, _AI_DETAIL_CONTENT_CAP) + ' … (이하 생략)';
            contentHtml = _hl(c);
        }
        // [v6.7.382] 요약 복사 버튼 — 링크 행에 '링크 복사'와 나란히([요약 복사][링크 복사], 사용자 선택 배치).
        //   좌우 너비 무증가(요약 행은 그대로) + 복사 액션 한 곳 집결. 스타일은 .ai-sum-copy-btn 공유,
        //   식별용 .ai-sum-summary-copy-btn 부여(클릭 핸들러가 링크 복사보다 먼저 분기). 요약 비면 미노출.
        const _sum = ((r && r.summary) || '').trim();
        const _sumCopyBtn = (_sum && _sum !== '-')
            ? '<button type="button" class="ai-sum-copy-btn ai-sum-summary-copy-btn" data-summary="' + escapeHtml(_sum) + '">요약 복사</button>'
            : '';
        const linkVal = href
            ? '<span class="ai-sum-detail-val ai-sum-detail-linkrow">'
                + '<a class="ai-sum-detail-link" href="' + escapeHtml(href) + '" target="_blank" rel="noopener" onclick="event.stopPropagation()">' + escapeHtml(href) + '</a>'
                + _sumCopyBtn
                + '<button type="button" class="ai-sum-copy-btn" data-link="' + escapeHtml(href) + '">링크 복사</button></span>'
            : (_sumCopyBtn
                ? '<span class="ai-sum-detail-val ai-sum-detail-linkrow">' + _sumCopyBtn + '</span>'
                : '<span class="ai-sum-detail-val">-</span>');
        // [v6.7.324] 게시판(board_name) 필드 추가 — 제목 위 (피드 상세 순서 통일: 게임/일시/게시판/제목/내용/링크).
        // [v6.7.325] 게시판 값에 출처 prefix — "다음 · 자유 게시판" / "공식 · 배그 플레이어" (피드 상세 동일 규칙).
        // [v6.7.329] 요약 필드 편입 — 순서 게임/일시/게시판/요약/제목/내용/링크 (사용자 지정). 목록 행에서 요약을
        //   드래그하면 클릭(원문 이동)으로 오발되기 쉬워, 클릭 무동작 가드가 있는 상세 그리드 안에 요약을 두어
        //   안전한 드래그 복사 영역 제공. 표 확장행 상단 요약 배너(ai-sum-expand-summary)는 중복이라 제거.
        const _srcT = _srcLabel(r);
        const _board = String((r && r.board_name) || '').trim();
        const _boardVal = (_srcT && _board) ? (_srcT + ' · ' + _board) : (_srcT || _board || '-');
        return '<div class="ai-sum-detail-grid">'
            + '<span class="ai-sum-detail-lbl">게임</span><span class="ai-sum-detail-val">' + escapeHtml((r && r.game) || '-') + '</span>'
            + '<span class="ai-sum-detail-lbl">일시</span><span class="ai-sum-detail-val">' + escapeHtml(_aiFullTs(_rowTs(r))) + '</span>'
            + '<span class="ai-sum-detail-lbl">게시판</span><span class="ai-sum-detail-val">' + escapeHtml(_boardVal) + '</span>'
            + '<span class="ai-sum-detail-lbl">요약</span><span class="ai-sum-detail-val ai-sum-detail-summary">' + _sevDotHtml(r) + _hl(((r && r.summary) || '-').trim()) + '</span>'
            + '<span class="ai-sum-detail-lbl">제목</span><span class="ai-sum-detail-val">' + _hl(((r && r.title) || '-').trim()) + '</span>'
            + '<span class="ai-sum-detail-lbl">내용</span><span class="ai-sum-detail-val ai-sum-detail-content">' + contentHtml + '</span>'
            + '<span class="ai-sum-detail-lbl">링크</span>' + linkVal
            + '</div>';
    }
    // [v6.7.731] 복사 버튼 처리 통합 - 구 _copyAiLink/_copyAiSummary 가 토스트·원복 라벨 문자열만
    //   다르고 동작이 동일했다. 요약 버튼(.ai-sum-summary-copy-btn)인지 class 로 가른다. 공용
    //   copyTextToClipboard(실패 토스트 내장) + 성공 토스트 + 1.5s '복사됨' 피드백.
    function _copyAiBtn(btn) {
        if (!btn || typeof copyTextToClipboard !== 'function') return;
        const isSummary = btn.classList.contains('ai-sum-summary-copy-btn');
        const text = (btn.dataset && (isSummary ? btn.dataset.summary : btn.dataset.link)) || '';
        if (!text) return;
        const toastMsg = isSummary ? 'AI 요약이 복사되었습니다.' : '링크가 복사되었습니다.';
        const label = isSummary ? '요약 복사' : '링크 복사';
        Promise.resolve(copyTextToClipboard(text)).then(function (ok) {
            if (!ok) return;
            if (typeof showToast === 'function') showToast(toastMsg);
            btn.textContent = '복사됨';
            btn.classList.add('copied');
            setTimeout(function () { btn.textContent = label; btn.classList.remove('copied'); }, 1500);
        });
    }

    /* ─── 표 마크업 (PC) — 4열: 게임 / 글 제목 / AI 요약 / 시간 ───
       [v6.7.313] 행 클릭 = 요약 전문 확장 토글(구 행 클릭 원문 새탭 폐지), 원문 이동 = 글 제목 링크로 분리.
       [v6.7.329] 확장행 = 상세 그리드 단독 — 구 상단 요약 배너(ai-sum-expand-summary)는 그리드 요약 필드 편입으로 제거. */
    // [v6.7.691] 표 셸(thead + 빈 tbody) - 행은 _aiSummaryRowHtml 로 청크 삽입한다(진행형 렌더).
    function _aiSummaryTableShellHtml() {
        return '<div class="ai-summary-data-table-wrap"><table class="ai-summary-data-table">'
            + '<thead><tr><th class="col-game">게임</th><th class="col-title">글 제목</th><th class="col-content">AI 요약</th><th class="col-time">시간</th><th class="col-more" aria-label="상세 보기"></th></tr></thead>'
            + '<tbody></tbody></table></div>';
    }
    // [v6.7.691] 행 1건(본행 + 확장행 쌍) - 구 _aiSummaryAsTableHtml 의 map 본문을 함수화(로직 동일).
    function _aiSummaryRowHtml(r) {
        const rawGame = r.game || '';
        const gameColor = rawGame && window.getGameColor ? window.getGameColor(rawGame) : 'var(--primary-color)';
        const title = (r.title || '').trim();
        const summary = (r.summary || '-').trim();
        const time = (typeof formatTimeFull === 'function')
            ? escapeHtml(formatTimeFull(_rowTs(r)) || '')
            : escapeHtml(_rowTs(r) || '');
        const href = _rowHref(r);
        const key = _rowKey(r);
        const expanded = STATE._expanded.has(key);
        const dot = _sevDotHtml(r);
        // [v6.7.316] critical 행 좌측 1px 보더 마커 - PATTERN-26 승인 예외(확장행 동반, 시각 연속성)
        const crit = String((r && r.severity) || '').toLowerCase() === 'critical' ? ' ai-sum-crit' : '';
        // [v6.7.320] 제목 링크 폐지 - 행 전체 클릭이 원문 이동(피드 통일). 원문 접근은 행 클릭 + 확장 상세 링크 2중.
        // [v6.7.325] 제목 앞 출처 미니 배지 - 좁은 폭에서 제목 열과 함께 숨음(그땐 더보기 상세로 확인).
        const titleCell = `<span class="col-title-text" title="${escapeHtml(title)}">${_srcBadgeHtml(r)}${_hl(title)}</span>`;
        return `<tr class="ai-sum-row${crit}${expanded ? ' expanded' : ''}" data-key="${escapeHtml(key)}" data-href="${escapeHtml(href)}" data-ts="${escapeHtml(_rowTs(r))}" data-source="${escapeHtml(_rowSrcKey(r))}">
                <td class="col-game"><span class="col-game-name" data-gc="${escapeHtml(rawGame)}" style="color:${gameColor};">${escapeHtml(rawGame)}</span></td>
                <td class="col-title">${titleCell}</td>
                <td class="col-content" title="${escapeHtml(summary)}"><span class="col-content-text">${dot}${_hl(summary)}</span></td>
                <td class="col-time">${time}</td>
                <td class="col-more">${_aiMoreBtnHtml()}</td>
            </tr>
            <tr class="ai-sum-expand${crit}" data-key="${escapeHtml(key)}"${expanded ? '' : ' style="display:none;"'}>
                <td colspan="5">${expanded ? _aiDetailFieldsHtml(r) : ''}</td>
            </tr>`;
    }

    /* ─── 카드 마크업 (모바일) — feed-card--ai (공용 클래스 재사용) ───
       [v6.7.313] 카드 클릭 = 요약 전문 확장(line-clamp 해제) 토글, 원문 이동 = 글 제목 링크로 분리. */
    // [v6.7.691] 카드 1건 - 구 _aiSummaryAsCardsHtml 의 map 본문을 함수화(로직 동일, 진행형 청크 삽입용).
    function _aiSummaryCardHtml(r) {
        const rawGame = r.game || '';
        const gameColor = rawGame && window.getGameColor ? window.getGameColor(rawGame) : 'var(--primary-color)';
        const gameSpan = rawGame
            ? `<span class="feed-card-game" data-gc="${escapeHtml(rawGame)}" style="color:${gameColor};">${escapeHtml(rawGame)}</span>`
            : '';
        const title = (r.title || '').trim();
        const href = _rowHref(r);
        // [v6.7.320] 제목 링크 폐지 - 카드 전체 클릭이 원문 이동(피드 통일).
        const titleSpan = title
            ? `<span class="feed-card-comment-title" title="${escapeHtml(title)}">${_hl(title)}</span>`
            : '';
        const summary = r.summary || '-';
        const time = (typeof formatTimeFull === 'function')
            ? escapeHtml(formatTimeFull(_rowTs(r)) || '')
            : escapeHtml(_rowTs(r) || '');
        const key = _rowKey(r);
        const expanded = STATE._expanded.has(key);
        const crit = String((r && r.severity) || '').toLowerCase() === 'critical' ? ' ai-sum-crit' : '';
        // [v6.7.321] 더보기 버튼 head wrapper 우측 세로 바 (피드 카드 통일 - 우측 끝 세로 전체 클릭 존).
        return `
                <div class="feed-card feed-card--ai${crit}${expanded ? ' expanded' : ''}" data-key="${escapeHtml(key)}" data-href="${escapeHtml(href)}" data-ts="${escapeHtml(_rowTs(r))}" data-source="${escapeHtml(_rowSrcKey(r))}">
                    <div class="feed-card-head">
                        <div class="feed-card-title" title="${escapeHtml(summary)}">${_sevDotHtml(r)}${_hl(summary)}</div>
                        <div class="feed-card-meta">
                            ${_srcBadgeHtml(r)}
                            ${gameSpan}
                            ${titleSpan}
                            <span class="feed-card-time">${time}</span>
                        </div>
                        ${_aiMoreBtnHtml()}
                    </div>
                    ${expanded ? '<div class="ai-sum-card-detail">' + _aiDetailFieldsHtml(r) + '</div>' : ''}
                </div>`;
    }

    /* ─── [v6.7.686] 요약 대기(밀림) 배지 - 헤더의 #ai-summary-backlog 에 STATE._backlog 캐시를 칠한다.
       요약 대기 = 워커가 아직 못 집은 글 수(표시 리스트=이미 요약된 글과 disjoint). 헤더는
       updateDashboard 가 재생성하므로, 이 함수를 renderAiSummary 안에서도 호출해 재도색한다.
       0건이면 조용히(muted), 가장 오래된 대기가 임계(기본 30분) 이상이면 경고색, 표본이 오래됐으면(3분+)
       그 사실을 함께 표기. STATE._backlog 가 없으면(첫 로드 전/측정 불가) 배지를 비운다. ─── */
    function _paintBacklog() {
        const el = document.getElementById('ai-summary-backlog');
        if (!el) return;
        const b = STATE._backlog;
        if (!b || b.pending == null) { el.textContent = ''; el.className = 'ai-sum-backlog'; el.removeAttribute('title'); return; }
        const pending = b.pending;
        const thr = (typeof b.threshold_s === 'number') ? b.threshold_s : 1800;
        const ageS = (typeof b.sample_age_s === 'number') ? b.sample_age_s : null;
        const stale = (ageS != null) && ageS >= 180;   // 3분+ (계측 주기 1분의 3배) = 신선하지 않음
        let cls = 'ai-sum-backlog';
        let txt;
        if (pending <= 0) {
            cls += ' ai-sum-backlog--idle';
            txt = '요약 대기 0';
        } else {
            const lagS = (typeof b.oldest_lag_s === 'number') ? b.oldest_lag_s : 0;
            const mins = Math.floor(Math.max(0, lagS) / 60);
            txt = '요약 대기 ' + pending + '건';
            if (mins > 0) txt += ' · 가장 오래된 ' + mins + '분';
            if (lagS >= thr) cls += ' ai-sum-backlog--warn';
        }
        if (stale) {
            const am = Math.floor(ageS / 60);
            txt += ' (' + (am >= 1 ? am + '분' : '방금') + ' 전 기준)';
            cls += ' ai-sum-backlog--stale';
        }
        el.textContent = txt;
        el.className = cls;
        el.title = 'AI 요약 대기(밀림) 현황 - 1분 주기 계측 기준';
    }

    /* fetch - /api/ai-summary/backlog (manager 게이트, /api/ai-summary 와 동일). PATTERN-32: 실패 시
       직전 값(STATE._backlog) 유지 + 1회 자동 재시도. 성공하면 캐시 갱신 후 재도색. */
    let _backlogRetried = false;
    let _backlogRetryTimer = null;
    async function _fetchBacklog() {
        try {
            const res = await fetch('/api/ai-summary/backlog', { credentials: 'same-origin' });
            if (res.status === 403) return;   // 권한 없음 - 폴링은 loadAiSummaryTab 쪽 403 처리가 정지시킨다
            if (!res.ok) throw new Error('HTTP ' + res.status);
            const j = await res.json();
            if (j && typeof j === 'object') {
                STATE._backlog = j;
                _backlogRetried = false;
                _paintBacklog();
            }
        } catch (e) {
            // 직전 값 유지(있으면 화면 그대로) + **연속 실패 구간당 1회만** 재시도 (PATTERN-32 (2)).
            //   플래그는 성공에서만 내린다 - 재시도 직전에 내리면 지속 실패(순단) 동안 5초 주기 무한
            //   재시도가 되어 NAS 가 멎은 순간 요청을 더 얹는다(커밋 전 리뷰 지적). 그 뒤는 60s 폴링 몫.
            //   탭을 닫으면(`_owners` 빔) 예약된 재시도도 취소한다.
            if (!_backlogRetried) {
                _backlogRetried = true;
                _backlogRetryTimer = setTimeout(function () {
                    _backlogRetryTimer = null;
                    if (_owners.size > 0) _fetchBacklog();
                }, 5000);
            }
        }
    }

    /* ─── [v6.7.691] 도착 잔광 적용 - 데이터 기록(_arrivalAt set)과 분리한다.
       진행형 렌더에선 뒤 청크가 나중에 붙으므로, 기록은 렌더당 1회(shown 전체 기준, _recordArrivals),
       클래스 부여는 붙은 DOM 마다(첫 청크 + 각 스크롤 청크) 호출한다. 피드 _applyFeedGlow 와 동형. ─── */
    function _applyAiArrivalGlow(scope) {
        if (!scope) return;
        const nowMs = Date.now();
        scope.querySelectorAll('tr.ai-sum-row[data-href], .feed-card--ai[data-href]').forEach(el => {
            const h = el.dataset.href;
            if (!h) return;
            const t0 = STATE._arrivalAt.get(h);
            if (t0 == null) return;
            const ageS = (nowMs - t0) / 1000;
            if (ageS < AI_ARRIVAL_FADE_S) {
                el.classList.add('imt-arrival-glow');
                el.style.setProperty('--glow-rgb', '139, 92, 246');   // [v6.7.406] AI 요약 잔광색(바이올렛)
                el.style.animationDuration = AI_ARRIVAL_FADE_S + 's';  // 유입 ~5분+ 간격 → 300s
                el.style.animationDelay = '-' + ageS.toFixed(2) + 's';
            } else {
                STATE._arrivalAt.delete(h);   // 만료 청소 (Map 무한 성장 방지)
            }
        });
    }

    /* ─── [v6.7.691] 진행형 청크 렌더 인프라 (root=스크롤러 #ai-summary-tab) ─── */
    function _teardownProgressive() {
        if (_io) { try { _io.disconnect(); } catch (_) {} _io = null; }
        _renderState = null;
    }
    // 다음 n건 렌더. 붙일 때마다 잔광 재적용(idempotent).
    function _renderAiChunk(n) {
        const st = _renderState;
        if (!st) return;
        const end = Math.min(st.rendered + n, st.sorted.length);
        if (end <= st.rendered) return;
        st.insertFn(st.sorted.slice(st.rendered, end));
        st.rendered = end;
        _applyAiArrivalGlow(document.getElementById('ai-summary-body'));
        if (st.rendered >= st.sorted.length && _io) { try { _io.disconnect(); } catch (_) {} _io = null; }
    }
    // 앵커 복원용 - idx 행이 실제 DOM 에 나올 때까지 청크를 채운다(이미 사용자가 스크롤로 봤던 범위 재현이라 추가 순단 아님).
    function _renderAiUpToIndex(idx) {
        const st = _renderState;
        if (!st) return;
        let guard = 0;
        while (st.rendered <= idx && st.rendered < st.sorted.length && guard++ < 1000) _renderAiChunk(AI_CHUNK);
    }

    /* ─── [v6.7.691] '새 요약 N건' 배지 - 스크롤을 내려 읽는 중 상단에 신규 요약이 도착하면
       앵커 복원이 위치를 지켜 주는 대신(=화면이 안 튐), 상단에 새 글이 생긴 사실을 배지로 알린다.
       클릭 = 맨 위로 스크롤(reduced-motion 존중). 요소는 템플릿의 #ai-summary-newpill(스크롤러 sticky). ─── */
    function _showNewPill(n) {
        const el = document.getElementById('ai-summary-newpill');
        if (!el) return;
        const c = document.getElementById('ai-summary-newpill-count');
        if (c) c.textContent = n;
        el.style.display = '';
    }
    function _hideNewPill() {
        const el = document.getElementById('ai-summary-newpill');
        if (el) el.style.display = 'none';
    }
    function _scrollAiToTop() {
        const sc = document.getElementById('ai-summary-tab');
        _pendingNewCount = 0;
        _hideNewPill();
        if (!sc) return;
        try { sc.scrollTo({ top: 0, behavior: _aiReducedMotion() ? 'auto' : 'smooth' }); }
        catch (_) { sc.scrollTop = 0; }
    }
    // 스크롤러가 최상단 근처로 오면 배지 자동 소멸(사용자가 직접 올려 확인한 경우). 1회 등록.
    function _hookAiScrollPill() {
        if (_scrollHooked) return;
        const sc = document.getElementById('ai-summary-tab');
        if (!sc) return;
        _scrollHooked = true;
        let raf = 0;
        sc.addEventListener('scroll', function () {
            if (raf) return;
            raf = requestAnimationFrame(function () {
                raf = 0;
                if (sc.scrollTop <= 8 && _pendingNewCount) { _pendingNewCount = 0; _hideNewPill(); }
            });
        }, { passive: true });
    }
    window._aiSummaryScrollToTop = _scrollAiToTop;   // 배지 onclick 위임(아래 document 클릭 핸들러)

    /* ─── 렌더 — 필터 4단(visibility → 검색 → 게임 pill → 심각도 칩) 단일 경로 + timestamp 내림차순 표/카드 삽입 ───
       [v6.7.313] 검색·pill 필터를 렌더 내부로 통합 - 소켓 push 재렌더에도 검색/게임 선택 유지(구: 재렌더 시 검색 소실).
       [v6.7.691] 진행형 렌더 - 첫 청크만 동기 렌더 후 스크롤 시 증분(500행 동기 렌더 = 순단 주범 제거) + 앵커 스크롤 복원. */
    function renderAiSummary(rows) {
        const body = document.getElementById('ai-summary-body');
        if (!body) return;
        const emptyState = document.getElementById('ai-summary-empty-state');
        const raw = Array.isArray(rows) ? rows : [];

        // [v6.7.691] 진행형 렌더 이전에 스크롤러/앵커를 먼저 읽는다(innerHTML 교체 전이라야 유효).
        //   앵커 = 현재 화면 최상단에 걸친 행의 href + 뷰포트 오프셋. 데이터 변화 렌더(_flashGate)에서만
        //   복원해, 신규 요약이 상단에 prepend 돼도 읽던 위치가 아래로 밀리지 않게 한다(구: raw scrollTop 복원은
        //   prepend 시 눈에 띄게 점프했다). 필터/검색/토글 렌더는 사용자 능동 조작이라 최상단으로 자연 리셋.
        const _flashGate = STATE._flashArrivals;
        const _scroller = document.getElementById('ai-summary-tab');
        let _anchor = null;
        if (_flashGate && _scroller && _scroller.scrollTop > 0) {
            const sTop = _scroller.getBoundingClientRect().top;
            const rowsEls = body.querySelectorAll('tr.ai-sum-row[data-href], .feed-card--ai[data-href]');
            for (let i = 0; i < rowsEls.length; i++) {
                const r = rowsEls[i].getBoundingClientRect();
                if (r.bottom > sTop + 1) { _anchor = { href: rowsEls[i].dataset.href, offset: r.top - sTop }; break; }
            }
        }

        // [필터 1] 게임 visibility — '글 탭' 가시성(feed 와 동일 모델 _localSettingsData.visibility).
        //   사용자가 끈 게임(visibility[game]===false)만 숨김. 미설정(undefined)은 기본 노출.
        const visSettings = (_localSettingsData && _localSettingsData.visibility) ? _localSettingsData.visibility : {};
        const list = raw.filter(r => {
            const g = (r && (r.game || r.game_name)) || '';
            return !(g && visSettings[g] === false);
        });

        // [필터 2] 검색어 — summary/title/game 부분 일치 (하이라이트는 _hl 이 담당).
        const q = (STATE._query || '').trim().toLowerCase();
        const searched = !q ? list : list.filter(r =>
            (r.summary && String(r.summary).toLowerCase().includes(q)) ||
            (r.title && String(r.title).toLowerCase().includes(q)) ||
            (r.game && String(r.game).toLowerCase().includes(q))
        );

        // [필터 3] 게임 pill — 탭 로컬 필터 (전역 visibility 설정 비접촉). pill 목록/건수는
        //   검색 전(list) 기준으로 고정 — 타이핑 중에도 pill 이 사라지지 않게.
        const games = [];
        const counts = {};
        list.forEach(r => {
            const g = (r && r.game) || '';
            if (!g) return;
            if (!(g in counts)) { counts[g] = 0; games.push(g); }
            counts[g]++;
        });
        games.sort((a, b) => (counts[b] - counts[a]) || a.localeCompare(b));
        if (STATE._game !== 'ALL' && games.indexOf(STATE._game) === -1) STATE._game = 'ALL';  // 데이터에서 사라진 게임 선택 자동 복귀
        const gameShown = (STATE._game === 'ALL') ? searched : searched.filter(r => ((r && r.game) || '') === STATE._game);

        // [필터 4·v6.7.315] 심각도/감정 카운트 칩 — 칩 건수는 게임 pill 과 동일하게 검색 전(list) 기준 고정.
        let nCrit = 0, nWarn = 0, nNeg = 0;
        list.forEach(r => {
            const sev = String((r && r.severity) || '').toLowerCase();
            if (sev === 'critical') nCrit++;
            else if (sev === 'warning') nWarn++;
            if (String((r && r.sentiment) || '').toLowerCase() === 'negative') nNeg++;
        });
        if (STATE._sev !== 'ALL') {
            const alive = (STATE._sev === 'critical') ? nCrit : (STATE._sev === 'warning') ? nWarn : nNeg;
            if (!alive) STATE._sev = 'ALL';  // 데이터에서 사라진 조건 선택 자동 복귀 (게임 pill 가드와 동일)
        }
        const shown = (STATE._sev === 'ALL') ? gameShown : gameShown.filter(r => (STATE._sev === 'negative')
            ? String((r && r.sentiment) || '').toLowerCase() === 'negative'
            : String((r && r.severity) || '').toLowerCase() === STATE._sev);

        // 건수 배지 (header + 탭 내) 갱신 — 최종 표시 개수
        // [v6.7.436] dataset.painted 마킹 - updateDashboard 가 pane 헤더를 새로 만들면 건수가 '0' 초기값으로
        //   되돌아가는데, _ingest 의 시그니처 스킵이 재렌더를 생략해 그 0 이 굳던 결함(아래 _headerRepainted).
        const countHeaderEl = document.getElementById('ai-summary-count-header');
        if (countHeaderEl) { countHeaderEl.textContent = shown.length; countHeaderEl.dataset.painted = '1'; }
        const countEl = document.getElementById('ai-summary-count');
        if (countEl) { countEl.textContent = shown.length; countEl.dataset.painted = '1'; }
        // [v6.7.686] 대기 배지 재도색 - updateDashboard 가 헤더를 재생성하면 #ai-summary-backlog 가
        //   비므로, 데이터 렌더(=헤더 재생성 직후 강제 렌더 포함)마다 STATE 캐시로 다시 칠한다.
        _paintBacklog();

        body.dataset.loadedOnce = '1';

        // 상단 바 — 게임 pill + [v6.7.315] 심각도/감정 카운트 칩(클릭 필터, 재클릭 해제) + 마지막 갱신 시각 (데이터 0건이면 바 생략)
        let pillBar = '';
        if (list.length) {
            const fresh = STATE._fetchedAt
                ? ('갱신 ' + String(STATE._fetchedAt.getHours()).padStart(2, '0') + ':' + String(STATE._fetchedAt.getMinutes()).padStart(2, '0'))
                : '';
            const sevChip = (k, label, n) => !n ? '' : '<button type="button" class="ai-sum-sevchip ai-sum-sevchip--' + k
                + (STATE._sev === k ? ' active' : '') + '" data-sev="' + k + '" title="' + label + '만 보기 (재클릭 해제)">'
                + label + ' ' + n + '</button>';
            const sevChips = sevChip('critical', '위험', nCrit) + sevChip('warning', '주의', nWarn) + sevChip('negative', '부정', nNeg);
            pillBar = '<div class="ai-sum-bar"><div class="ai-sum-pills">'
                + '<button type="button" class="ai-sum-pill' + (STATE._game === 'ALL' ? ' active' : '') + '" data-game="__ALL__">전체 ' + list.length + '</button>'
                + games.map(g => '<button type="button" class="ai-sum-pill' + (STATE._game === g ? ' active' : '')
                    + '" data-game="' + escapeHtml(g) + '">' + escapeHtml(g) + ' ' + counts[g] + '</button>').join('')
                + '</div>'
                + (sevChips ? '<div class="ai-sum-sevchips">' + sevChips + '</div>' : '')
                + (fresh ? '<span class="ai-sum-fresh">' + fresh + '</span>' : '') + '</div>';
        }

        if (shown.length === 0) {
            _teardownProgressive();   // [v6.7.691] 이전 진행형 observer 정리(빈 목록엔 sentinel 없음)
            _pendingNewCount = 0; _hideNewPill();
            body.innerHTML = pillBar;  // pill 은 유지 — 다른 게임/전체로 즉시 복귀 가능
            STATE._prevLinks = new Set(); STATE._flashArrivals = false;  // [v6.7.387] 표시행 0 — 도착 diff 리셋(플래그 소진)
            if (emptyState) {
                const p = emptyState.querySelector('p');
                if (p) p.textContent = q ? '검색 결과가 없습니다 · 다른 키워드로 시도해보세요'
                    : (STATE._sev !== 'ALL' ? '선택한 조건의 요약이 없습니다'
                        : (STATE._game !== 'ALL' ? '선택한 게임의 요약이 없습니다' : '표시할 AI 요약이 없습니다'));
                emptyState.style.display = 'block';
            }
            return;
        }
        if (emptyState) emptyState.style.display = 'none';

        // timestamp 내림차순 (댓글 탭 정렬 로직 동등 — ms 변환 후 숫자 비교)
        const sorted = [...shown].sort((a, b) => _parseAiTs(_rowTs(b)) - _parseAiTs(_rowTs(a)));

        // [v6.7.691] 도착 기록은 shown 전체(데이터) 기준으로 렌더당 1회 - 진행형 렌더로 뒤 청크가 나중에 붙어도
        //   신규 판정이 흔들리지 않게(구: DOM 순회 기록은 미렌더 행을 놓쳐 다음 렌더에 신규 오판). 잔광 '적용'은
        //   _applyAiArrivalGlow 가 붙은 DOM 마다(첫 청크 + 스크롤 청크) 담당. 기록은 데이터 변화 렌더(_flashGate)에서만.
        const _prev = STATE._prevLinks || new Set();
        const _record = _flashGate && _prev.size > 0;
        const _shownHrefs = [];
        const _nowMs = Date.now();
        let _newCount = 0;
        sorted.forEach(r => {
            const h = _rowHref(r);
            if (!h) return;
            _shownHrefs.push(h);
            if (!_prev.has(h)) {
                _newCount++;
                if (_record && !STATE._arrivalAt.has(h)) STATE._arrivalAt.set(h, _nowMs);
            }
        });
        STATE._prevLinks = new Set(_shownHrefs);

        // [v6.7.330 P1→688] 활성 모드만 + 진행형 청크 렌더. 구: 표+카드 동시 생성(최대 500행×2 DOM)을 통째 교체 →
        //   순단. 이제 셸만 먼저 그리고 첫 청크(50행)만 동기 렌더, 나머지는 스크롤 진입 시(IntersectionObserver) 증분.
        //   _effectiveLayoutMode: 모바일(pointer:coarse)은 CSS 가 카드 강제라 항상 cards, 데스크탑은 localStorage 모드.
        _teardownProgressive();
        const _mode = _effectiveLayoutMode();
        // sentinel 은 #ai-summary-body 안 맨 끝, 관찰 root 는 실제 스크롤러 #ai-summary-tab. 삽입은 **항상 sentinel 앞**
        //   (표=tbody 안, 카드=sentinel 앞) - sentinel 이 늘 목록 맨 아래여야 관찰이 성립(뒤에 붙이면 sentinel 이 위로
        //   올라와 상시 교차 → 청크가 통째 로드되는 회귀).
        const sentinel = document.createElement('div');
        sentinel.className = 'ai-sum-sentinel';
        sentinel.setAttribute('aria-hidden', 'true');
        let _insertFn;
        if (_mode === 'table') {
            body.innerHTML = pillBar + _aiSummaryTableShellHtml();
            body.appendChild(sentinel);
            const tbody = body.querySelector('.ai-summary-data-table tbody');
            _insertFn = function (batch) { if (tbody) tbody.insertAdjacentHTML('beforeend', batch.map(_aiSummaryRowHtml).join('')); };
        } else {
            body.innerHTML = pillBar;
            body.appendChild(sentinel);
            _insertFn = function (batch) { sentinel.insertAdjacentHTML('beforebegin', batch.map(_aiSummaryCardHtml).join('')); };
        }

        // [v6.7.691] sticky 바 높이 계측 → 표 thead sticky top 오프셋(--ai-sum-bar-h). 바가 wrap 해도 겹치지 않게.
        const _bar = body.querySelector('.ai-sum-bar');
        body.style.setProperty('--ai-sum-bar-h', (_bar ? _bar.offsetHeight : 0) + 'px');

        _renderState = { sorted, rendered: 0, insertFn: _insertFn };
        _renderAiChunk(AI_CHUNK);   // 첫 청크 동기 렌더

        // [v6.7.691] 앵커 스크롤 복원 - 데이터 변화 렌더에서만. 앵커 행이 나올 때까지 청크를 채운 뒤,
        //   그 행이 직전과 같은 뷰포트 오프셋에 오도록 scrollTop 을 미세 보정(prepend 로 인한 점프 상쇄).
        if (_anchor && _scroller) {
            const idx = sorted.findIndex(r => _rowHref(r) === _anchor.href);
            if (idx >= 0) {
                _renderAiUpToIndex(idx);
                const sel = 'tr.ai-sum-row[data-href], .feed-card--ai[data-href]';
                let target = null;
                const els = body.querySelectorAll(sel);
                for (let i = 0; i < els.length; i++) { if (els[i].dataset.href === _anchor.href) { target = els[i]; break; } }
                if (target) {
                    const delta = (target.getBoundingClientRect().top - _scroller.getBoundingClientRect().top) - _anchor.offset;
                    _scroller.scrollTop += delta;
                }
            }
        }

        // sentinel 관찰 - 뷰포트(스크롤러)에 들어오면 다음 청크 렌더.
        try {
            _io = new IntersectionObserver(function (entries) {
                if (entries.some(function (e) { return e.isIntersecting; })) _renderAiChunk(AI_CHUNK);
            }, { root: _scroller || null, rootMargin: '300px' });
            _io.observe(sentinel);
        } catch (_) {
            // [v6.7.731] 폴백은 rendered 를 0 으로 리셋하지 않는다 - 위에서 이미 첫 청크(+앵커 복원분)를
            //   DOM 에 넣었는데 0 으로 되돌려 다시 그리면 그만큼이 중복 삽입됐다(첫 50행 2중). 현재
            //   rendered 부터 남은 행만 이어서 그린다.
            while (_renderState.rendered < sorted.length) _renderAiChunk(AI_CHUNK);
        }

        // [v6.7.691] '새 요약 N건' 배지 - 스크롤을 내려 읽는 중(120px+) 신규가 상단에 도착했을 때만.
        //   최상단 근처면 배지 없이 조용히(사용자가 이미 봄). 누적 후 클릭/최상단 복귀로 소멸.
        const _wasScrolled = _scroller && _scroller.scrollTop > 120;
        if (_record && _newCount > 0 && _wasScrolled) {
            _pendingNewCount += _newCount;
            _showNewPill(_pendingNewCount);
        } else if (!_scroller || _scroller.scrollTop <= 8) {
            _pendingNewCount = 0;
            _hideNewPill();
        }

        STATE._flashArrivals = false;
    }

    /* ─── [v6.7.313] 위임 클릭 — 게임 pill / 행·카드 확장 (동적 렌더라 document 레벨 1회 등록) ─── */
    // [v6.7.385] reduced-motion 판정 (접힘 애니 스킵용).
    function _aiReducedMotion() {
        try { return window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (_) { return false; }
    }
    // [v6.7.385] 상세 그리드 접힘 애니메이션 후 hideFn 실행 (display none / .expanded 제거). display 토글이라 DOM 유지 —
    //   dataset.collapsing 으로 재확장 취소 감지(재확장 시 hideFn 이 옛 상세를 숨기는 레이스 차단).
    function _collapseGridThen(grid, hideFn) {
        if (!grid || _aiReducedMotion()) { hideFn(); return; }
        grid.dataset.collapsing = '1';
        grid.classList.remove('imt-detail-reveal');
        grid.classList.add('imt-detail-collapsing');
        const finish = () => {
            if (grid.dataset.collapsing !== '1') return;   // 재확장으로 취소됨
            delete grid.dataset.collapsing;
            grid.classList.remove('imt-detail-collapsing');
            hideFn();
        };
        grid.addEventListener('animationend', finish, { once: true });
        setTimeout(finish, 240);
    }
    function _animateAiCollapse(key) {
        document.querySelectorAll('#ai-summary-body [data-key]').forEach(el => {
            if (el.getAttribute('data-key') !== key) return;
            if (el.classList.contains('ai-sum-expand')) {
                _collapseGridThen(el.querySelector('.ai-sum-detail-grid'), function () { el.style.display = 'none'; });
            } else if (el.classList.contains('feed-card--ai')) {
                _collapseGridThen(el.querySelector('.ai-sum-card-detail .ai-sum-detail-grid'), function () { el.classList.remove('expanded'); });
            } else {
                el.classList.remove('expanded');   // 표 본행(.ai-sum-row) — 즉시 복귀(chevron/보더, 상세는 .ai-sum-expand 가 애니)
            }
        });
    }
    function _toggleExpand(key) {
        if (!key) return;
        const on = !STATE._expanded.has(key);
        if (on) STATE._expanded.add(key); else STATE._expanded.delete(key);
        if (!on) { _animateAiCollapse(key); return; }   // [v6.7.385] 접힘 = 애니메이션 후 hide
        // 재렌더 없이 DOM 직접 토글 — 표 행(.expanded) + 확장행(display) + 카드(.expanded) 동기.
        // [v6.7.318] 확장 시 상세 필드 지연 주입 (미주입 시 1회) — 렌더는 expanded 행만 baked 라 첫 확장은 여기서 채움.
        const row = (Array.isArray(STATE.lastData) ? STATE.lastData.find(r => _rowKey(r) === key) : null);
        document.querySelectorAll('#ai-summary-body [data-key]').forEach(el => {
            if (el.getAttribute('data-key') !== key) return;
            if (el.classList.contains('ai-sum-expand')) {
                el.style.display = '';
                if (row && !el.querySelector('.ai-sum-detail-grid')) {
                    const td = el.querySelector('td');
                    if (td) td.insertAdjacentHTML('beforeend', _aiDetailFieldsHtml(row));
                }
                // [v6.7.382/385] 펼침 reveal — 진행 중이던 접힘 취소(dataset/class 정리) 후 재생.
                const g = el.querySelector('.ai-sum-detail-grid');
                if (g) { delete g.dataset.collapsing; g.classList.remove('imt-detail-collapsing'); g.classList.add('imt-detail-reveal'); }
            } else {
                el.classList.add('expanded');
                if (row && el.classList.contains('feed-card--ai') && !el.querySelector('.ai-sum-card-detail')) {
                    el.insertAdjacentHTML('beforeend', '<div class="ai-sum-card-detail">' + _aiDetailFieldsHtml(row) + '</div>');
                }
                const g = el.querySelector('.ai-sum-card-detail .ai-sum-detail-grid');
                if (g) { delete g.dataset.collapsing; g.classList.remove('imt-detail-collapsing'); g.classList.add('imt-detail-reveal'); }
            }
        });
    }
    document.addEventListener('click', function (e) {
        if (!e.target.closest) return;
        // [v6.7.691] '새 요약 N건' 배지 클릭 = 맨 위로 스크롤(#ai-summary-tab, #ai-summary-body 밖이라 먼저 분기).
        if (e.target.closest('#ai-summary-newpill')) { _scrollAiToTop(); return; }
        // [v6.7.731] 복사 버튼 - 요약/링크 버튼 모두 .ai-sum-copy-btn 을 가지므로 한 분기로 처리하고
        //   _copyAiBtn 이 class(.ai-sum-summary-copy-btn)로 요약/링크를 가른다. 다른 분기보다 먼저.
        const copyBtn = e.target.closest('#ai-summary-body .ai-sum-copy-btn');
        if (copyBtn) { _copyAiBtn(copyBtn); return; }
        // [v6.7.320] 더보기 버튼(chevron) = 상세 확장 (피드 통일). 부모 행/카드의 data-key 로 토글.
        const moreBtn = e.target.closest('#ai-summary-body .ai-sum-more-btn');
        if (moreBtn) {
            const host = moreBtn.closest('[data-key]');
            if (host) _toggleExpand(host.getAttribute('data-key'));
            return;
        }
        if (e.target.closest('#ai-summary-body a')) return;  // 잔여 링크는 기본 동작
        // [v6.7.318] 상세 필드 영역 클릭은 무동작 — 본문 드래그 선택/복사 중 원문 오이동 방지.
        if (e.target.closest('#ai-summary-body .ai-sum-detail-grid')) return;
        const pill = e.target.closest('#ai-summary-body .ai-sum-pill');
        if (pill) {
            const g = pill.getAttribute('data-game') || '__ALL__';
            STATE._game = (g === '__ALL__') ? 'ALL' : g;
            renderAiSummary(STATE.lastData);
            return;
        }
        const sevChipEl = e.target.closest('#ai-summary-body .ai-sum-sevchip');
        if (sevChipEl) {
            const k = sevChipEl.getAttribute('data-sev') || 'ALL';
            STATE._sev = (STATE._sev === k) ? 'ALL' : k;  // 재클릭 = 해제
            renderAiSummary(STATE.lastData);
            return;
        }
        // [v6.7.320] 행/카드 본문 클릭 = 원문 이동 (피드 통일 — 구 "행 클릭=확장" 폐지). 확장행은 상세라 제외.
        const openRow = e.target.closest('#ai-summary-body tr.ai-sum-row, #ai-summary-body .feed-card--ai');
        if (openRow) {
            const href = openRow.getAttribute('data-href');
            if (href && typeof openArticle === 'function') openArticle(href);
        }
    });

    /* ─── [v6.7.330 P2 → 688] 데이터 변화 시그니처 - 소켓(ai_analysis_update) 재fetch 결과가 실제로 바뀐 경우만 재렌더 ───
       구: 행수 + 최신행(link+timestamp) 하나만 봐서, 최신행이 그대로인 채 중간/하위가 바뀌면(reanalyze, 하위
       삭제·정정) 스킵돼 화면이 stale 이었다. 이제 상위 N행(12)의 link+timestamp+severity+요약길이를 싼 해시로
       접는다 - 마크업에 영향 주는 필드(severity=도트, 요약길이=본문)까지 포함.
       **놓치는 것(의도)**: N행 이후(하위)에서만 일어나는 in-place 정정, sentiment 단독 변경(도트는 severity 기준,
       칩 카운트는 어차피 sig 변화 시 재계산). 이 관용은 구현과 동일 수준 - reanalyze 는 드물고 admin 수동. */
    function _dataSig(arr) {
        if (!Array.isArray(arr) || !arr.length) return '0';
        const N = Math.min(arr.length, 12);
        let h = 5381;
        for (let i = 0; i < N; i++) {
            const r = arr[i] || {};
            const s = (r.link || '') + '\x1f' + (r.timestamp || r.date || '') + '\x1f'
                + (r.severity || '') + '\x1f' + String((r.summary || '').length);
            for (let j = 0; j < s.length; j++) { h = ((h << 5) + h + s.charCodeAt(j)) | 0; }
        }
        return arr.length + '|' + N + '|' + (h >>> 0).toString(36);
    }

    /* ─── [v6.7.350] 공용 인제스트 — fetch 응답과 외부 주입(hydrate)의 단일 소화 경로 ───
       STATE 갱신 + 신선 창 마킹 후, 탭 활성 시에만 시그니처-스킵 렌더 (PATTERN-38 — 숨은 DOM 헛 렌더 금지).
       비활성 시 _lastSig 를 안 건드리므로 재진입 ingest 가 시그니처 불일치로 자연 렌더된다. */
    function _ingest(arr, isFreshData) {
        STATE.lastData = arr;
        if (isFreshData) {
            STATE._fetchedAt = new Date();  // [v6.7.313] 신선도 표시 (상단 바 '갱신 HH:MM')
            STATE._freshUntil = Date.now() + 30000;   // 서버 응답 캐시 TTL(30s) 정렬 — 창 내 재fetch 는 어차피 동일 응답
            window._aiSummaryRetryN = 0;   // [v6.7.425] 재시도 백오프 리셋 - 다음 일과성 실패는 다시 2.5s 부터
        }
        // isFreshData=false(캐시 재소화)는 시각/신선 창 미갱신 — 창이 미끄러지며 무한 연장되는 것 방지.
        const active = (typeof activeApps !== 'undefined' && Array.isArray(activeApps) && activeApps.includes('ai-summary'));
        if (!active) return;
        const body = document.getElementById('ai-summary-body');
        // [v6.7.330 P2] 무변화 스킵 — 첫 렌더(loadedOnce) 이후, 시그니처가 같으면 통째 재렌더(순단) 생략.
        //   소켓 푸시된 신규분이 visibility 필터로 다 걸러지거나 dedup 인 경우 등, 화면 변화 없는 재렌더 방지.
        // [v6.7.436] 단, 헤더가 새로 그려졌으면(=건수 배지 미마킹) 스킵 금지. updateDashboard(탭 열기/닫기·
        //   드래그·모바일 전환)는 pane 헤더를 통째 재생성해 건수를 '0' 초기값으로 되돌리는데, 본문 DOM
        //   (#ai-summary-tab)은 그대로 이동(appendChild)돼 loadedOnce·목록이 남는다. 그 상태에서 시그니처가
        //   같으면 재렌더가 생략돼 "목록은 있는데 헤더는 0건" 이 다음 신규 요약 도착 시점까지 굳었다
        //   (AI 유입은 5분+ 간격, 야간엔 수 시간 - 사용자 보고 "AI 요약 0 으로 바뀌고 갱신이 멈춤").
        const sig = _dataSig(arr);
        const _hdr = document.getElementById('ai-summary-count-header') || document.getElementById('ai-summary-count');
        const _headerRepainted = !!(_hdr && _hdr.dataset.painted === '1');
        if (_headerRepainted && body && body.dataset.loadedOnce === '1' && sig === STATE._lastSig) return;
        STATE._lastSig = sig;
        // [v6.7.691] 스크롤 위치 보존은 renderAiSummary 의 앵커 복원이 담당(구: raw scrollTop 복원은 상단 prepend 시
        //   눈에 띄게 점프). 여기선 데이터 변화 플래그만 세운다 - 앵커 캡처·복원은 _flashArrivals 게이트로 렌더 내부에서.
        // [v6.7.387] 데이터 변화(소켓 신규 저장/fetch)에서만 도착 플래시 요청 — 필터/검색/pill/토글 재렌더는 미설정 → false flash 없음.
        STATE._flashArrivals = true;
        renderAiSummary(arr);
    }

    /* [v6.7.350] 외부 주입 훅 — 배너 diff(_notifyNewAiSummaries, socket.js)가 이미 받아온 /api/ai-summary
       응답을 그대로 넘겨 탭의 중복 fetch 를 제거 (push 당 2회 → 1회, 제안서 B5).
       주의: 이 훅이 실패해도 소켓 리스너의 디바운스 로더가 폴백으로 남아 있음(리스너 무수정). */
    window.aiSummaryHydrate = function (arr) {
        if (!Array.isArray(arr)) return;
        try { _ingest(arr, true); } catch (_) {}
    };

    /* [v6.7.731] 설정에서 게임을 끄고 저장해도 AI 요약 행이 다음 신규 요약까지 남던 결함 수정.
       settings-modal.js 가 저장 직후 이 함수를 불러 현재 표시설정으로 다시 그린다 - renderAiSummary 가
       매 렌더 _localSettingsData.visibility 를 읽으므로, 마지막 데이터로 재렌더하면 끈 게임 행이 즉시
       사라진다. 탭이 비활성이거나 그려둔 데이터가 없으면 no-op(다음 활성화 때 _ingest 가 최신 설정으로
       렌더하므로 숨은 DOM 을 헛 렌더하지 않는다 - PATTERN-38). _flashArrivals 미설정이라 도착 잔광 없음. */
    window._rerenderAiSummary = function () {
        try {
            const active = (typeof activeApps !== 'undefined' && Array.isArray(activeApps) && activeApps.includes('ai-summary'));
            if (!active || !Array.isArray(STATE.lastData)) return;
            renderAiSummary(STATE.lastData);
        } catch (_) {}
    };

    /* [v6.7.386] focusAiSummaryItem(외부 focus) 제거 — AI 배너/보관소 클릭이 이 탭으로 이동하던 v6.7.383 동작을
       "원문(href)으로 이동"으로 변경(사용자 결정). 라우팅은 ui-control.js _routeNotifTo 가 AI 를 제외(false 반환)해
       호출측이 openArticle(href) 하는 경로로 단일화 → 이 함수는 소비처 0. .ai-sum-focus-flash CSS 동반 제거. */

    /* ─── fetch (AI 요약 데이터 — [v6.7.386] manager 개방 API. [v6.7.500] 탭 UI 도 manager 로드) ─── */
    window.loadAiSummaryTab = async function () {
        const body = document.getElementById('ai-summary-body');
        // [v6.7.436] 안전망 폴링 등록 (멱등 - 이미 돌고 있으면 no-op). 진입 경로가 loadPaneData 하나라 여기 1곳.
        try { if (typeof window.aiSummaryTab_register === 'function') window.aiSummaryTab_register(); } catch (_) {}
        // [v6.7.350] 신선 창 가드 — hydrate/직전 fetch 가 30s 내 같은 API 데이터를 이미 소화했으면 재fetch 생략.
        //   소켓 push 직후 디바운스 로더(중복 경로)와 탭 재진입(PATTERN-33)이 여기서 흡수된다.
        //   잔여 한계: push 직후 hydrate fetch 가 일과성 실패하고 직전 데이터가 30s 내면 이번 push 분이
        //   다음 push/창 만료까지 지연될 수 있음(협소 레이스, 자가 회복) — PATTERN-32 정신과 동일한 관용.
        if (Array.isArray(STATE.lastData) && Date.now() < STATE._freshUntil) {
            _ingest(STATE.lastData, false);
            return;
        }
        // [v6.7.419] 부트 프리페치 소화 — boot.js 가 부트 즉시 쏜 동일 요청을 기다렸다 사용(이중 fetch 없음).
        //   신선(<30s, 서버 TTL 과 동일)일 때만, 1회용(delete 선행 — 무효면 아래 정상 fetch 폴백).
        try {
            if (window._bootPrefetch && window._bootPrefetch.aiSummary) {
                const _bp = window._bootPrefetch.aiSummary; delete window._bootPrefetch.aiSummary;
                const got = await _bp;
                if (got && Array.isArray(got.data) && (Date.now() - got.ts) < 30000) { _ingest(got.data, true); return; }
            }
        } catch (_) { /* 프리페치 실패 → 정상 fetch 폴백 */ }
        try {
            // [v6.7.425] 15s 타임아웃 - 스톨 요청(새벽 크롤+AI 배치 포화, health 10s 실측)은 catch 미발화라
            //   재시도조차 안 돌고 '로드 중…' 영구 고착(PATTERN-40). abort → AbortError → 아래 catch 재시도 루프.
            let _sig, _tm;
            try { const c = new AbortController(); _tm = setTimeout(() => c.abort(), 15000); _sig = c.signal; } catch (_) { _sig = undefined; }
            let res;
            try { res = await fetch('/api/ai-summary', { credentials: 'same-origin', signal: _sig }); }
            finally { if (_tm) clearTimeout(_tm); }
            if (res.status === 403) {
                // [v6.7.738] 이 탭은 manager 개방(analysis.py _require_role('manager'))이라 403 은
                //   권한 부족이 아니라 비로그인/세션 만료뿐이다 - "관리자 권한" 문구는 오안내였다.
                //   판정·알림 탭이 쓰는 문구와 정확히 통일.
                if (body) body.innerHTML = '<div class="ai-summary-empty">로그인이 필요합니다. 세션이 만료됐다면 새로고침해 주세요.</div>';
                // [v6.7.436] 403 이면 폴링 정지 - 권한 없는 세션이 60s 마다 403 을 두드리는 스팸 차단
                //   (discord-tab _renderUnauthorized 선례, naver-watcher N-3 교훈).
                try { if (typeof window.unloadAiSummaryTab === 'function') window.unloadAiSummaryTab(); } catch (_) {}
                return;
            }
            if (!res.ok) throw new Error('HTTP ' + res.status);
            const data = await res.json();
            const arr = Array.isArray(data) ? data : [];
            _ingest(arr, true);
        } catch (e) {
            // PATTERN-32 — 캐시가 있으면 본문 유지(일과성 오류가 정상 데이터를 덮지 않게), 첫 로드 실패만 에러 표시.
            if (STATE.lastData) {
                console.warn('AI 요약 일시 로드 실패 — 기존 데이터 유지:', e);
            } else if (body) {
                // [v6.7.426] AbortError 원문("signal is aborted without reason")은 기술 용어 노출 - 사용자 문구로 치환.
                const _msg = (e && e.name === 'AbortError') ? '서버 응답 지연 (15초 초과)'
                    : (e && e.message ? e.message : String(e));
                body.innerHTML = '<div class="ai-summary-empty">데이터 로드 실패: '
                    + escapeHtml(_msg) + ' - 자동 재시도 중</div>';
                // [v6.7.423→425] PATTERN-32 (2) 확장 - 1회(2.5s)만으론 서버 바쁜 창(빌드/새벽 크롤+AI 포화)을 못 넘기고,
                //   푸시 전용 탭이라 실패 후 재트리거가 소켓 push 뿐이라 push 공백 시간대엔 무기한 고착(PATTERN-40).
                //   첫 성공까지 루프: 1차 2.5s, 이후 30s 간격(서버 TTL 정렬). 성공(_ingest fresh)이 백오프 리셋,
                //   이 분기 자체가 "캐시 없음" 한정이라 lastData 확보 시 자연 정지.
                if (!window._aiSummaryRetryScheduled) {
                    window._aiSummaryRetryScheduled = true;
                    window._aiSummaryRetryN = (window._aiSummaryRetryN || 0) + 1;
                    setTimeout(function () { window._aiSummaryRetryScheduled = false; window.loadAiSummaryTab(); },
                        window._aiSummaryRetryN === 1 ? 2500 : 30000);
                }
            }
        }
    };

    // 소켓(ai_analysis_update) 푸시 버스트 대비 디바운스 로더 — 서버 30s TTL + 클라 800ms (피드 선례).
    window.loadAiSummaryTabDebounced = (window._tabHelpers && window._tabHelpers.debounce)
        ? window._tabHelpers.debounce(window.loadAiSummaryTab, 800)
        : window.loadAiSummaryTab;

    /* ─── [v6.7.436] 안전망 폴링 - 이 탭은 v6.7.328 이후로도 '푸시 전용'이라 소켓 이벤트를 한 번만
       놓치면 무기한 stale 이었다(사용자 보고: "카카오워크 알림은 왔는데 AI 요약 탭 갱신이 안 됨").
       카카오는 서버→카카오 직송이라 항상 도착하고, 탭 갱신만 소켓 emit 에 걸려 있어 배달 경로가
       비대칭 - 폴링 탭(디스코드 7s·상황판 60s)에선 안 보이던 결함이다(PATTERN-40 서술의 그 비대칭).
       주기 60s = 서버 응답 캐시 TTL(30s)의 2배라 DB 부담 없음. 데이터 무변화면 _ingest 의 시그니처
       스킵이 재렌더를 생략하므로 순단 없음(PATTERN-38 준수). 생명주기는 discord/naver-watcher 패턴
       (_owners refcount) 이식 - pane 이 닫히면 타이머 정지. ─── */
    const AI_POLL_MS = 60000;
    const _owners = new Set();
    let _pollTimer = null;
    function _ensurePolling() {
        if (_pollTimer) return;
        _pollTimer = setInterval(function () {
            try {
                if (typeof activeApps !== 'undefined' && Array.isArray(activeApps) && !activeApps.includes('ai-summary')) return;
                // [v6.7.686] 대기 배지도 이 한 줄로 갱신된다 - `loadAiSummaryTab` 이 맨 위에서
                //   `aiSummaryTab_register` -> `_fetchBacklog` 를 부른다. 여기서 또 부르면 주기당 2요청.
                window.loadAiSummaryTab();
            } catch (e) { console.warn('[ai-summary] 폴링 실패', e); }
        }, AI_POLL_MS);
    }
    function _stopPollingIfIdle() {
        if (_owners.size === 0 && _pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
    }
    window.aiSummaryTab_register = function () { _owners.add('tab'); _ensurePolling(); _fetchBacklog(); _hookAiScrollPill(); };  // [v6.7.686] 탭 열자마자 1회 즉시 조회(60s 대기 없이) · [v6.7.691] 스크롤 배지 훅 등록
    window.unloadAiSummaryTab = function () { _owners.delete('tab'); _stopPollingIfIdle(); if (_owners.size === 0 && _backlogRetryTimer) { clearTimeout(_backlogRetryTimer); _backlogRetryTimer = null; } };

    /* ─── 검색 (댓글 filterComments 미러) — [v6.7.313] STATE._query 만 갱신, 필터·문구·하이라이트는 렌더 단일 경로 ─── */
    function filterAiSummary() {
        // 헤더 또는 탭 내 검색 input 중 값이 있는 쪽 사용 — 통합환경에선 render.js 가 헤더 input
        // (ai-summary-search-input-header) 만 생성하므로 탭 input 단독 의존 시 검색 무동작. (ai-tab.js 견고 패턴)
        const headerInput = document.getElementById('ai-summary-search-input-header');
        const tabInput = document.getElementById('ai-summary-search-input');
        STATE._query = ((headerInput && headerInput.value) || (tabInput && tabInput.value) || '').trim();
        renderAiSummary(STATE.lastData);
    }
    window.filterAiSummary = filterAiSummary;

    // 헤더 검색 input(ai-summary-search-input-header) 값을 탭 input 에 복사 후 필터 (댓글 filterCommentsHeader 미러)
    window.filterAiSummaryHeader = function () {
        const headerInput = document.getElementById('ai-summary-search-input-header');
        const tabInput = document.getElementById('ai-summary-search-input');
        if (headerInput && tabInput) tabInput.value = headerInput.value;
        filterAiSummary();
    };

    // [v6.7.731] window.filterAiSummaryDebounced 삭제 - 호출처 0(레포 전수 grep). 검색 input 의 oninput 은
    //   render.js 가 filterAiSummaryHeader() 를 직접 배선하므로 디바운스판은 미사용이었다.

    /* ─── PC 표 ↔ 모바일 카드 수동 토글 (댓글 탭 setCommentsLayoutMode 1:1 미러) ───
       기본 'table'(PC 표 우선). 표/카드 둘 다 렌더돼 있어 body 클래스로 CSS 표시 분기 — 재렌더 불요(검색 상태 보존). */
    const AI_SUMMARY_LAYOUT_LS_KEY = 'imt_layout_mode_ai-summary';
    function _getAiSummaryLayoutMode() {
        try {
            const v = localStorage.getItem(AI_SUMMARY_LAYOUT_LS_KEY);
            return (v === 'cards') ? 'cards' : 'table';
        } catch (_) { return 'table'; }
    }
    // [v6.7.330 P1] 실제 화면에 보이는 모드 — 모바일(pointer:coarse)은 ai-summary.css @media 가 카드 강제(표 display:none)라
    //   localStorage 모드가 table 이어도 카드만 보인다. 활성 모드만 렌더하므로 '보이는 모드' 를 정확히 판정해야
    //   모바일에서 표만 그려 빈 화면 되는 사고를 막는다. 데스크탑은 토글 모드 그대로.
    function _effectiveLayoutMode() {
        try {
            if (window.matchMedia && window.matchMedia('(pointer: coarse)').matches) return 'cards';
        } catch (_) {}
        return _getAiSummaryLayoutMode();
    }
    function _setAiSummaryLayoutMode(mode) {
        try { localStorage.setItem(AI_SUMMARY_LAYOUT_LS_KEY, mode); } catch (_) {}
    }
    function _applyAiSummaryLayoutBodyClass() {
        const mode = _getAiSummaryLayoutMode();
        document.body.classList.toggle('ai-summary-mode-cards', mode === 'cards');
        document.body.classList.toggle('ai-summary-mode-table', mode === 'table');
    }
    function _syncAiSummaryLayoutToggleUI(mode) {
        document.querySelectorAll('.layout-toggle[data-target="ai-summary"] .layout-toggle-btn').forEach(b => {
            b.classList.toggle('active', b.dataset.mode === mode);
        });
    }
    function _resizeAiSummaryPaneForMode(mode) {
        const pane = document.getElementById('ai-summary-tab');
        const win = pane ? pane.closest('.pane-window') : null;
        if (!win) return;
        win.style.width = (mode === 'table') ? '720px' : '360px';
    }
    window.setAiSummaryLayoutMode = function (mode) {
        if (mode !== 'table' && mode !== 'cards') return;
        _setAiSummaryLayoutMode(mode);
        _applyAiSummaryLayoutBodyClass();
        _syncAiSummaryLayoutToggleUI(mode);
        _resizeAiSummaryPaneForMode(mode);
        // [v6.7.330 P1] 단일 모드 렌더 — 토글 시 반대편이 DOM 에 없으므로 재렌더(STATE 보존이라 검색/필터/확장 유지).
        if (STATE.lastData) renderAiSummary(STATE.lastData);
    };

    // 토글 클릭 위임 (헤더 동적 렌더라 document 레벨 — 댓글 패턴 동일)
    document.addEventListener('click', function (e) {
        const btn = e.target.closest && e.target.closest('.layout-toggle[data-target="ai-summary"] .layout-toggle-btn');
        if (!btn) return;
        const mode = btn.dataset.mode;
        if (mode === 'table' || mode === 'cards') window.setAiSummaryLayoutMode(mode);
    }, true);

    function _initAiSummaryLayoutMode() {
        _applyAiSummaryLayoutBodyClass();
        const mode = _getAiSummaryLayoutMode();
        _syncAiSummaryLayoutToggleUI(mode);
        _resizeAiSummaryPaneForMode(mode);
    }
    window._initAiSummaryLayoutMode = _initAiSummaryLayoutMode;
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', _initAiSummaryLayoutMode);
    } else {
        _initAiSummaryLayoutMode();
    }
})();
