// tab-manager/render.js — updateDashboard (전체 대시보드 재렌더링)
// Phase 3 2차 분할 — 기존 tab-manager.js 에서 이관
//
// 외부 공개 심볼: updateDashboard
// 의존:
//   state.js (activeApps, CORE_APP_IDS, EXTERNAL_SCROLL_APPS, globalPaneObserver, _currentDragPaneId)
//   actions.js (swapAppPanes, loadPaneData, closeAppPane — 런타임 참조)
//   외부: addCustomResizers (ui.js), tabDates (main.js 등)

function updateDashboard() {
    const launcher = document.querySelector('.sidebar-menu');
    if (!launcher) return;

    // 1. 앱 런처 버튼 상태 동기화 (ON/OFF 토글 시각화)
    launcher.querySelectorAll('.nav-item').forEach(box => box.classList.remove('active'));
    // [v6.7.311] data-appbox 보조 진입점 — id 중복 불가로 app-box-* id 를 못 쓰는 요소(예: 사이드바 로그 #side-log-viewer,
    //   탑바에 동일 논리 id 의 #app-box-log-viewer 가 이미 존재)의 active 동기화. launcher 밖 요소도 커버.
    document.querySelectorAll('[data-appbox]').forEach(el => el.classList.remove('active'));
    activeApps.forEach(appId => {
        const box = document.getElementById(`app-box-${appId}`);
        if (box) box.classList.add('active');
        document.querySelectorAll(`[data-appbox="${appId}"]`).forEach(el => el.classList.add('active'));
    });

    // 하단 탭바 active 동기화 (모바일 <768px)
    document.querySelectorAll('.btab-item').forEach(item => item.classList.remove('active'));
    activeApps.forEach(appId => {
        const btab = document.getElementById(`btab-${appId}`);
        if (btab) btab.classList.add('active');
    });

    // 2. 탭 요소 안전 보관 및 그리드 초기화
    const grid = document.getElementById('dashboard-grid');
    const repository = document.getElementById('pane-repository');
    if (!grid || !repository) return;

    // [중요] innerHTML을 비우기 전에 모든 .tab-content를 보관소(repository)로 대피시켜야 소멸되지 않음!
    document.querySelectorAll('.tab-content').forEach(tab => {
        tab.style.display = 'none';
        repository.appendChild(tab);
    });

    // ResizeObserver 누수 방지: 기존 pane-window 요소를 unobserve 후 그리드 비우기
    grid.querySelectorAll('.pane-window').forEach(el => globalPaneObserver.unobserve(el));

    // [v6.7.443] 헤더 검색어 보존 — 아래 innerHTML='' 이 pane 헤더(=검색 input)를 파괴하는데
    //   **본문 .tab-content 는 repository 로 대피해 살아남는다.** 그래서 재생성 후 "검색창은 비었는데
    //   목록은 여전히 필터된" 불일치가 남았다 — 탭을 하나 열거나 닫기만 해도(updateDashboard 는 전
    //   pane 을 다시 만든다) 발생. 방향은 탭마다 다르지만 원인은 하나다:
    //     · 피드   = DOM hide 상태가 대피분에 그대로 남음
    //     · AI요약/디스코드 = 모듈 STATE._query 가 살아 있어 다음 렌더가 다시 필터
    //   값만 되살려 양쪽을 다시 맞춘다(필터 재실행 불요 — 상태가 이미 필터된 쪽이므로).
    const _savedQueries = {};
    grid.querySelectorAll('.pane-header-search input[id$="-search-input-header"]').forEach(el => {
        if (el.value) _savedQueries[el.id] = el.value;
    });

    // 이제 그리드를 비워도 탭 요소는 안전함
    grid.innerHTML = '';

    // 3. 선택된 앱들을 2열 그리드에 순차 배치 (Z-패턴)
    const appLabels = {
        'total': { title: '전체', icon: 'list-ul' },
        'ai-analysis': { title: 'AI', icon: '' },
        'games': { title: '게임', icon: 'gamepad' },
        'community': { title: '커뮤니티', icon: 'users' },
        'account-trade': { title: '계정 거래', icon: 'user-tag' },
        'diamond-trade': { title: '다이아 시세', icon: 'gem' },
        'trends': { title: '야간동향', icon: 'moon' },
        'comments': { title: '댓글', icon: 'comments' },
        'log-viewer': { title: '로그', icon: 'terminal' },
        'ops': { title: '운영', icon: 'tachometer-alt' },
        'sql-console': { title: 'SQL 콘솔', icon: 'database' },
        'admin': { title: '관리자', icon: 'user-shield' },
        // [v6.6.466] 'situation' app id 는 그대로 사용하되 title/icon 을 '대시보드' 로 통일.
        //   사이드바 "대시보드" 메뉴(`app-box-dashboard`) 가 toggleAppPane('situation') 호출 → pane-window 헤더에 '대시보드' 표시.
        //   (구 '상황판' 명칭 폐기)
        'situation': { title: '대시보드', icon: 'chart-line' },
        'feed': { title: '피드', icon: 'stream' },             // [v6.6.422] 전체/게임/커뮤니티 세그먼트 합성
        'dashboard': { title: '대시보드', icon: 'chart-line' }, // [v6.6.422] (legacy — default activeApps 에서 제거됨, 호환 보존)
        'search': { title: '검색', icon: 'search' },             // [v6.7.69] F-4 통합 검색/필터 (관리자 전용)
        'ccu-grafana': { title: '그라파나', icon: 'chart-area' }, // [v6.7.215] 그라파나 CCU 미러 (관리자 전용) / [v6.7.219] 라벨 'CCU 그라파나'→'그라파나'
        'ai-summary': { title: '요약', icon: '', iconText: 'AI' }, // AI 요약 탭 (헤더 마크=AI 텍스트, 이름=요약 — 중복 회피). 관리자 전용 코어 멀티액티브
        // [v6.7.741] 'update-history'(업데이트) · 'naver-watcher'(알림) · 'discord'(디스코드) 삭제 - 탭째 지웠다(사용자 2026-10-24).
        'verdict': { title: 'Verdict', icon: 'scale-balanced' }, // [v6.7.700] 1차 분류기 그림자 운영 관제 (admin+manager [v6.7.725])
        'anomaly': { title: '이상감지', icon: 'wave-square' } // [v6.7.439] 평시 대비 급증 관제 (admin 전용) // [v6.7.395] 배그 디스코드 멤버모집 채팅 관제 — admin 전용(제재 모니터링). fab(브랜드) 아이콘
    };

    let renderedCount = 0;
    activeApps.forEach((appId) => {
        const tab = document.getElementById(`${appId}-tab`);
        if (tab) {
            // 패널 윈도우(Wrapper) 생성
            const windowDiv = document.createElement('div');
            windowDiv.className = 'pane-window';
            windowDiv.id = `pane-${appId}`;

            // 유틸리티 앱인 경우 1000px 기본 너비 + localStorage 복원
            const isUtility = !CORE_APP_IDS.includes(appId);
            if (isUtility) {
                windowDiv.classList.add('utility-mode');
                const savedW = localStorage.getItem(`imt_pane_width_${appId}`);
                const savedH = localStorage.getItem(`imt_pane_height_${appId}`);
                windowDiv.style.width = savedW || '1200px';
                windowDiv.style.height = savedH || '85vh';
            } else {
                // [개인화] 코어 탭별 저장된 크기 로드 (독립 조절 지원)
                const savedW = localStorage.getItem(`imt_pane_width_${appId}`);
                const savedH = localStorage.getItem(`imt_pane_height_${appId}`);

                if (savedW) windowDiv.style.width = savedW;
                else {
                    // [v6.6.433] v432 댓글 폭이 너무 좁다는 피드백 → 댓글 default 살짝 확대.
                    //   2 pane: 피드 440 / 댓글 360 = ~800px
                    // [v6.6.495] 3 pane 합산 1420px → 1260px 로 축소. 1366 모니터에서도 줄바꿈 없이 한 줄.
                    //   3 pane: 피드 380 / 댓글 320 / situation 560 = 1260px (기존 720 → 560)
                    const activeCoreCount = activeApps.filter(id => CORE_APP_IDS.includes(id)).length;
                    const has3Pane = activeCoreCount >= 3;
                    if (appId === 'dashboard') windowDiv.style.width = has3Pane ? '540px' : '600px';
                    // [v6.6.495] situation 폭 축소 — 3 pane 720→560, 단독 850→720. 컴팩트 모드 클래스 동시 부여.
                    else if (appId === 'situation') windowDiv.style.width = has3Pane ? '560px' : '720px';
                    // [v6.7.219] 그라파나 코어 편입 — 차트(3시리즈 멀티라인) 가독성 위해 situation 급 폭(3pane 560 / 단독 760).
                    else if (appId === 'ccu-grafana') windowDiv.style.width = has3Pane ? '560px' : '760px';
                    else if (appId === 'ai-summary') windowDiv.style.width = has3Pane ? '480px' : '720px';
                    else if (appId === 'verdict') windowDiv.style.width = has3Pane ? '520px' : '760px';  // [v6.7.700] 행 5열 - 좁으면 CSS container query 가 3줄로 접는다
                    else if (appId === 'anomaly') windowDiv.style.width = has3Pane ? '460px' : '620px';  // [v6.7.439] 카드 2열이 들어가는 최소 폭  // [v6.7.413] 480→400 — 짧은 모집글엔 480 과함(사용자). 요약보다 좁게 유지
                    else if (['total', 'games', 'community'].includes(appId)) windowDiv.style.width = '700px';
                    else if (appId === 'comments') windowDiv.style.width = has3Pane ? '320px' : '360px';
                    else if (appId === 'feed') windowDiv.style.width = has3Pane ? '420px' : '480px';  // [v6.7.413] 380→420 — "너무 좁다"(사용자) + 우측 거터 상쇄
                    else windowDiv.style.width = has3Pane ? '380px' : '440px';
                }
                // [v6.6.495] 3 pane 시 situation pane 에 컴팩트 클래스 부여 (단독 풀뷰는 풍부 카드 그대로).
                //   클래스는 항상 갱신 — pane 추가/제거로 has3Pane 변동 시 즉시 반영.
                if (appId === 'situation') {
                    const _ac = activeApps.filter(id => CORE_APP_IDS.includes(id)).length;
                    if (_ac >= 3) windowDiv.classList.add('situation-compact-pane');
                    else windowDiv.classList.remove('situation-compact-pane');
                }

                // 게임/계정거래/다이아시세: 외부 스크롤(콘텐츠가 가변적으로 매우 길어 내부 스크롤 부적합)
                if (EXTERNAL_SCROLL_APPS.includes(appId)) {
                    windowDiv.style.setProperty('height', 'auto', 'important');
                    windowDiv.style.setProperty('max-height', 'none', 'important');
                } else if (appId === 'community') {
                    // [v6.6.466] 모든 코어 pane height 900px 로 통일 — situation 과 동일.
                    windowDiv.style.height = savedH || '900px';
                    windowDiv.style.maxHeight = 'none';
                } else if (appId === 'dashboard' || appId === 'situation') {
                    // [v6.6.435] dashboard pane — 상황판 + 그래프 둘 다 한 화면에 노출하려면 900px+ 필요
                    // [v6.6.466] situation 코어 통합 — 동일 height 적용 (카드 2x2 + 그래프 동시 노출)
                    windowDiv.style.height = savedH || '900px';
                    windowDiv.style.maxHeight = 'none';
                } else {
                    // [v6.6.466] feed/comments/total/games 등 — 모든 코어 pane height 900px 통일.
                    if (savedH) windowDiv.style.height = savedH;
                    else windowDiv.style.height = '900px';
                }
            }

            // 헤더 및 컨트롤(닫기 버튼) 추가
            const info = appLabels[appId] || { title: appId, icon: 'desktop' };
            const isSpecialHeader = ['total', 'ai-analysis', 'comments', 'feed', 'ai-summary'].includes(appId);  // [v6.6.422] feed 도 검색·카운트 헤더 사용 / [v6.7.741] 디스코드 탭 삭제로 그 헤더 분기도 뺐다

            let headerContent = '';
            if (isSpecialHeader) {
                // [개선형 헤더] 이름 + 건수 + 검색창을 한 줄에 배치
                let countId = '';
                let searchId = '';
                let searchHandler = '';
                let placeholder = '검색어...';

                if (appId === 'total') {
                    countId = 'total-article-count';
                    searchId = 'total-search-input-header';
                    searchHandler = 'filterTotalArticlesHeader()';
                } else if (appId === 'ai-analysis') {
                    countId = 'ai-analysis-count';
                    searchId = 'ai-analysis-search-input-header';
                    searchHandler = 'filterAiAnalysisArticlesHeader()';
                } else if (appId === 'comments') {
                    countId = 'comment-count';
                    searchId = 'comments-search-input-header';
                    searchHandler = 'filterCommentsHeader()';
                } else if (appId === 'feed') {
                    countId = 'feed-count';
                    searchId = 'feed-search-input-header';
                    searchHandler = 'filterFeedHeader()';
                } else if (appId === 'ai-summary') {
                    countId = 'ai-summary-count';
                    searchId = 'ai-summary-search-input-header';
                    searchHandler = 'filterAiSummaryHeader()';
                }

                // [v6.7.16] PC 표 ↔ 모바일 카드 토글 — 피드/댓글 한정 (webview_app v7.0.0-alpha.15 등가).
                //   .layout-toggle 마크업은 .pane-controls 직전에 삽입. 클릭 핸들러는 feed-tab.js 의 위임.
                //   재렌더 시 활성 모드 class 가 inline 으로 박혀 시각 동기화 보장.
                // [v6.7.48] 피드 탭 한정 — 분류 토글 (시간순 / 게임별 / 출처별). webview v0.1.30 등가.
                //   imt_feed_view_mode ∈ {'timeline','by-game','by-community'} (default 'timeline').
                //   클릭 핸들러는 feed-tab.js 의 document level 위임.
                let viewToggleHtml = '';
                if (appId === 'feed') {
                    let _vmode = 'timeline';
                    try {
                        const _v = localStorage.getItem('imt_feed_view_mode');
                        if (_v === 'by-game' || _v === 'by-community') _vmode = _v;
                    } catch (_) {}
                    const _cT = _vmode === 'timeline' ? ' active' : '';
                    const _cG = _vmode === 'by-game' ? ' active' : '';
                    const _cC = _vmode === 'by-community' ? ' active' : '';
                    viewToggleHtml = `
                            <div class="feed-view-toggle" title="피드 분류 보기">
                                <button type="button" class="feed-view-toggle-btn${_cT}" data-mode="timeline" aria-label="시간순"><i class="fas fa-stream"></i></button>
                                <button type="button" class="feed-view-toggle-btn${_cG}" data-mode="by-game" aria-label="게임별"><i class="fas fa-gamepad"></i></button>
                                <button type="button" class="feed-view-toggle-btn${_cC}" data-mode="by-community" aria-label="출처별"><i class="fas fa-layer-group"></i></button>
                            </div>`;
                }

                // [v6.7.664] 피드 한정 - 출처 pill + 제재 토글 (2축 직교). 디스코드 탭의 채널 pill +
                //   제재 토글(v6.7.466) 과 같은 문법이되, 피드는 #feed-body 가 무한스크롤 observer 의
                //   root 이고 renderFeedPane 이 매 렌더 innerHTML 을 비우므로 **body 가 아니라 헤더
                //   둘째 줄**에 둔다. 디스코드처럼 body 안에 넣으면 렌더마다 증발한다.
                //   출처 필터가 서버 파라미터인 이유는 feed-tab.js `_feedQueryKey` 주석 참조
                //   (DC 가 유입의 88% 라 클라 필터로는 공식이 표본에서 사라진다).
                let feedSourceBarHtml = '';
                if (appId === 'feed') {
                    const _SRC = [['', '전체'], ['OFFICIAL_WEB', '공식'], ['NAVER', '네이버'], ['DAUM', '다음'], ['DC', 'DC']];
                    let _src = '';
                    let _flag = false;
                    try {
                        const _v = localStorage.getItem('imt_feed_source');
                        if (_SRC.some(s => s[0] === _v) && _v) _src = _v;
                        _flag = localStorage.getItem('imt_feed_flagged') === '1';
                    } catch (_) {}
                    const _pills = _SRC.map(s =>
                        `<button type="button" class="feed-srcpill${s[0] === _src ? ' active' : ''}" data-source="${s[0]}">${s[1]}</button>`
                    ).join('');
                    // 제재 토글은 pill 줄 우측에 분리 배치 - 디스코드 v6.7.466 과 같은 이유(출처 '전체'와
                    //   모드 '전체'가 나란히 놓여 '전체가 두 개'로 보이는 것을 피한다).
                    const _flagPill = `<button type="button" class="feed-flagpill${_flag ? ' active' : ''}" data-flagtoggle="1" title="삭제 대상으로 판정된 글만 보기">제재</button>`;
                    feedSourceBarHtml = `
                    <div class="feed-src-bar">
                        <div class="feed-srcpills">${_pills}${_flagPill}</div>
                    </div>`;
                }

                let layoutToggleHtml = '';
                if (appId === 'feed' || appId === 'comments' || appId === 'ai-summary') {
                    // [ai-summary] 기본 'table'(PC 표 우선). feed/comments 는 기본 'cards'.
                    let _mode = (appId === 'ai-summary') ? 'table' : 'cards';
                    try {
                        const _v = localStorage.getItem('imt_layout_mode_' + appId);
                        if (_v === 'table') _mode = 'table';
                        else if (_v === 'cards') _mode = 'cards';
                    } catch (_) {}
                    const _tableActive = _mode === 'table' ? ' active' : '';
                    const _cardsActive = _mode === 'cards' ? ' active' : '';
                    layoutToggleHtml = `
                            <div class="layout-toggle" data-target="${appId}" title="PC 표 / 모바일 카드 전환">
                                <button type="button" class="layout-toggle-btn${_tableActive}" data-mode="table" aria-label="표 모드"><i class="fas fa-table"></i></button>
                                <button type="button" class="layout-toggle-btn${_cardsActive}" data-mode="cards" aria-label="카드 모드"><i class="fas fa-th-large"></i></button>
                            </div>`;
                }
                // [v6.7.686] AI 요약 탭 한정 - '요약 대기 N건' 밀림 배지 placeholder. 값 주입은
                //   ai-summary-tab.js (_fetchBacklog/_paintBacklog, 폴링 타이머에 얹음). 헤더는
                //   updateDashboard 가 통째로 재생성하므로 빈 채로 두고 탭 렌더가 STATE 캐시로 다시 칠한다.
                let backlogHtml = '';
                if (appId === 'ai-summary') {
                    backlogHtml = '<span id="ai-summary-backlog" class="ai-sum-backlog" role="status" aria-live="polite" style="flex-shrink:0;"></span>';
                }
                headerContent = `
                    <div class="pane-header-top">
                        <h3 class="pane-header-title">${info.iconText ? `<span class="pane-header-mark">${info.iconText}</span> ` : (info.icon ? `<i class="${info.iconBrand ? 'fab' : 'fas'} fa-${info.icon}"></i> ` : '')}${info.title}</h3>
                        <span class="header-badge" role="status" aria-live="polite" style="flex-shrink:0;"><strong id="${countId}-header">0</strong>건</span>
                        ${backlogHtml}
                        <div class="header-search-container pane-header-search">
                            <i class="fas fa-search"></i>
                            <input type="text" id="${searchId}" placeholder="${placeholder}" onkeyup="${searchHandler}">
                        </div>
                        ${viewToggleHtml}
                        ${layoutToggleHtml}
                        <div class="pane-controls">
                            ${['account-trade', 'diamond-trade'].includes(appId) ? `
                            <div class="date-nav-bar" id="date-nav-${appId}" style="display:flex; align-items:center; gap:4px; flex-shrink:0;">
                                <i class="fas fa-times" id="date-reset-${appId}" style="font-size:0.7rem; color:#666; cursor:pointer; display:none;" onclick="resetToToday('${appId}')" title="오늘로 돌아가기"></i>
                                <span id="date-label-${appId}" style="font-size:0.75rem; color:var(--primary-color); display:none;"></span>
                                <input type="date" id="date-input-${appId}"
                                       max="${new Date(Date.now()-86400000).toLocaleDateString('sv', {timeZone:'Asia/Seoul'})}"
                                       style="position:absolute; opacity:0; width:0; height:0; pointer-events:none;"
                                       onchange="onDateInputChange('${appId}', this.value)">
                                <i class="fas fa-calendar-alt" style="color:var(--text-muted); font-size:0.85rem; cursor:pointer;" onclick="document.getElementById('date-input-${appId}').showPicker()" title="날짜 선택"></i>
                            </div>
                            ` : ''}
                            <button class="pane-close-btn" onclick="closeAppPane('${appId}')" title="이 탭 닫기">
                                <i class="fas fa-times"></i>
                            </button>
                        </div>
                    </div>
                    ${feedSourceBarHtml}
                `;
            } else {
                // [기본 헤더]
                headerContent = `
                    <div class="pane-header-top">
                        <h3 class="pane-header-title">${info.iconText ? `<span class="pane-header-mark">${info.iconText}</span> ` : (info.icon ? `<i class="${info.iconBrand ? 'fab' : 'fas'} fa-${info.icon}"></i> ` : '')}${info.title}</h3>
                        <div class="pane-controls">
                            ${['account-trade', 'diamond-trade'].includes(appId) ? `
                            <div class="date-nav-bar" id="date-nav-${appId}" style="display:flex; align-items:center; gap:4px;">
                                <i class="fas fa-times" id="date-reset-${appId}" style="font-size:0.7rem; color:#666; cursor:pointer; display:none;" onclick="resetToToday('${appId}')" title="오늘로 돌아가기"></i>
                                <span id="date-label-${appId}" style="font-size:0.75rem; color:var(--primary-color); display:none;"></span>
                                <input type="date" id="date-input-${appId}"
                                       max="${new Date(Date.now()-86400000).toLocaleDateString('sv', {timeZone:'Asia/Seoul'})}"
                                       style="position:absolute; opacity:0; width:0; height:0; pointer-events:none;"
                                       onchange="onDateInputChange('${appId}', this.value)">
                                <i class="fas fa-calendar-alt" style="color:var(--text-muted); font-size:0.85rem; cursor:pointer;" onclick="document.getElementById('date-input-${appId}').showPicker()" title="날짜 선택"></i>
                            </div>
                            ` : ''}
                            <button class="pane-close-btn" onclick="closeAppPane('${appId}')" title="이 탭 닫기">
                                <i class="fas fa-times"></i>
                            </button>
                        </div>
                    </div>
                `;
            }

            windowDiv.innerHTML = `
                <div class="pane-header">
                    ${headerContent}
                </div>
            `;

            // [드래그앤드롭] 제목 + 헤더 상단 전체를 드래그 핸들로 사용 (6차 수정)
            windowDiv.setAttribute('draggable', 'false'); // 패널 본체 드래그 방지
            const header = windowDiv.querySelector('.pane-header');
            const headerTop = header ? header.querySelector('.pane-header-top') : null;
            const titleEl = header ? header.querySelector('.pane-header-title') : null;

            // 드래그 제외 선택자 (인터랙티브 자식)
            const NON_DRAG_SELECTOR = 'input, button, textarea, select, a, .pane-controls, .pane-close-btn, .header-search-container, .date-nav-bar';

            const dragStartHandler = (e) => {
                if (e.target.closest(NON_DRAG_SELECTOR)) {
                    e.preventDefault();
                    return;
                }
                e.stopPropagation();
                _currentDragPaneId = appId;
                windowDiv.classList.add('dragging');
                if (e.dataTransfer) {
                    e.dataTransfer.effectAllowed = 'move';
                    try { e.dataTransfer.setData('text/plain', appId); } catch (_) { }
                }
            };
            const dragEndHandler = () => {
                windowDiv.classList.remove('dragging');
                _currentDragPaneId = null;
                document.querySelectorAll('.pane-window.drag-over').forEach(p => p.classList.remove('drag-over'));
            };

            // 두 핸들에 동일 핸들러 중복 등록 — 이벤트 버블링 무관하게 확실히 발화
            [titleEl, headerTop].forEach(el => {
                if (!el) return;
                el.setAttribute('draggable', 'true');
                el.style.cursor = 'grab';
                el.style.userSelect = 'none';
                el.addEventListener('dragstart', dragStartHandler);
                el.addEventListener('dragend', dragEndHandler);
            });

            // [모바일] 터치 드래그 지원
            let _touchDragging = false;
            const touchHandle = headerTop || header;
            if (touchHandle) {
                touchHandle.addEventListener('touchstart', (e) => {
                    if (e.target.closest(NON_DRAG_SELECTOR)) return;
                    _touchDragging = true;
                    _currentDragPaneId = appId;
                    windowDiv.classList.add('dragging');
                }, { passive: true });
                touchHandle.addEventListener('touchmove', (e) => {
                    if (!_touchDragging) return;
                    e.preventDefault();
                    const touch = e.touches[0];
                    const el = document.elementFromPoint(touch.clientX, touch.clientY);
                    const target = el?.closest?.('.pane-window');
                    document.querySelectorAll('.pane-window').forEach(p => p.classList.remove('drag-over'));
                    if (target && target !== windowDiv) target.classList.add('drag-over');
                }, { passive: false });
                touchHandle.addEventListener('touchend', (e) => {
                    if (!_touchDragging) return;
                    _touchDragging = false;
                    windowDiv.classList.remove('dragging');
                    _currentDragPaneId = null;
                    const touch = e.changedTouches[0];
                    const el = document.elementFromPoint(touch.clientX, touch.clientY);
                    const target = el?.closest?.('.pane-window');
                    document.querySelectorAll('.pane-window').forEach(p => p.classList.remove('drag-over'));
                    if (target && target !== windowDiv) {
                        const targetId = target.id.replace('pane-', '');
                        if (targetId && targetId !== appId) swapAppPanes(appId, targetId);
                    }
                });
                touchHandle.addEventListener('touchcancel', () => {
                    if (!_touchDragging) return;
                    _touchDragging = false;
                    windowDiv.classList.remove('dragging');
                    _currentDragPaneId = null;
                    document.querySelectorAll('.pane-window.drag-over').forEach(p => p.classList.remove('drag-over'));
                });
            }

            // 내용물(Tab content)을 윈도우에 삽입
            tab.style.display = 'block';
            windowDiv.appendChild(tab);

            // [수정] 모든 탭에 커스텀 리사이저 적용 (유틸리티 포함)
            addCustomResizers(windowDiv, appId);

            // 그리드에 추가
            grid.appendChild(windowDiv);

            // [v6.7.443] 파괴 전에 보존한 검색어 복원 — **loadPaneData 보다 먼저** 해야 한다(동기 렌더
            //   경로가 값을 못 보고 그리면 다시 불일치가 된다).
            windowDiv.querySelectorAll('.pane-header-search input[id$="-search-input-header"]').forEach(el => {
                if (_savedQueries[el.id]) el.value = _savedQueries[el.id];
            });

            // [추가] 리사이즈 옵저버 연결
            globalPaneObserver.observe(windowDiv);

            // 고정 그리드 배치 로직 (순차적 배치, CSS가 그리드 열 개수를 결정함)
            renderedCount++;

            // 데이터 로드 호출
            loadPaneData(appId);
        } else {
            console.warn(`Tab not found for appId: ${appId}`);
        }
    });

    // [v6.7.191 IA-10] 렌더 결과 0 → 'feed' fallback. 옛 'total' 은 v6.6.426 사이드바 진입점 제거됨 — 진입점 없는 ID 로 복구하면 빈 화면.
    if (renderedCount === 0 && activeApps.length > 0) {
        console.error("No valid tabs rendered. Resetting to feed.");
        activeApps = ['feed'];
        localStorage.setItem('imt_dashboard_config', JSON.stringify(activeApps));
        updateDashboard();
    }

    // [드래그앤드롭] 그리드 레벨 이벤트 위임 (개별 pane 대신 그리드에서 처리)
    grid.ondragover = (e) => {
        e.preventDefault();
        if (!_currentDragPaneId) return;
        const target = e.target.closest('.pane-window');
        // 이전 drag-over 클래스 모두 제거 후 현재 대상에만 적용
        grid.querySelectorAll('.pane-window.drag-over').forEach(p => p.classList.remove('drag-over'));
        if (target && target.id !== `pane-${_currentDragPaneId}`) {
            target.classList.add('drag-over');
        }
    };
    grid.ondrop = (e) => {
        e.preventDefault();
        grid.querySelectorAll('.pane-window.drag-over').forEach(p => p.classList.remove('drag-over'));
        if (!_currentDragPaneId) return;
        const target = e.target.closest('.pane-window');
        if (target) {
            const targetId = target.id.replace('pane-', '');
            if (targetId && targetId !== _currentDragPaneId) {
                swapAppPanes(_currentDragPaneId, targetId);
            }
        }
        _currentDragPaneId = null;
    };
}
