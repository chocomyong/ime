// main/app-state.js — 전역 AppState + window 프록시
// Phase 3 3차 분할 — 기존 main.js 에서 이관

// ─────────────────────────────────────────────────────
// AppState — 전역 애플리케이션 상태 단일 관리 객체
// Object.defineProperty로 window에 프록시 등록하여
// 기존 코드(api.js, socket.js, ui.js 등)에서 직접
// 변수명으로 접근해도 투명하게 AppState를 읽고 씁니다.
// ─────────────────────────────────────────────────────
const _serverDataEl = document.getElementById('server-data');
const _rawGamesData = _serverDataEl ? _serverDataEl.getAttribute('data-night-report-games') : '[]';
const _trendGamesList = JSON.parse(_rawGamesData || '[]');

const AppState = {
    // 탭 / 화면 상태
    currentMainTab: 'total',
    currentAccountTradeGameTab: '전체',

    // 인터벌 핸들
    updateInterval: undefined,
    countdownInterval: undefined,

    // 크롤 타이밍
    nextCrawlTimes: {},

    // 데이터 캐시
    allLogs: [],
    currentLogLevel: 'all',
    allComments: [],

    // 탭별 날짜 상태: null = 오늘(실시간), "YYYY-MM-DD" = 과거 날짜
    tabDates: {},
    // 탭별 미읽음 카운터 {appId: number}
    unreadCounts: {},

    // Account Trade
    accountTradePollingInterval: undefined,
    isAccountTradeCrawling: false,

    // Odin Global Diamond Trading
    odinGlobalPollingInterval: undefined,
    isOdinGlobalCrawling: false,

    // Jasa/Tasa Diamond Trading
    currentDiamondCompanyTab: 'jasa',
    currentDiamondJasaTab: 'all',
    jasaResultsData: [],
    tasaResultsData: [],
    isJasaCrawling: false,
    jasaPollingInterval: undefined,
    isTasaCrawling: false,
    tasaPollingInterval: undefined,

    // 야간 동향 탭
    trendGamesList: _trendGamesList,
    currentTrendGameTab: (_trendGamesList && _trendGamesList.length > 0) ? _trendGamesList[0] : '',
    trendGameStates: {},

    // 설정 모달
    currentConfigData: {},
    currentConfigTab: '',
    TAB_COLUMN_ORDER: {
        'Config_Games': ['GameName', 'Cafe_URL', 'DC_URL', 'Active'],
        'Config_Keywords': ['Type', 'Keyword', 'Active'],
        'Config_SubCrawler': ['Category', 'GameName', 'Unit', 'URL', 'Active']
    },

    // 일괄 추출
    batchRunInterval: null,
    batchCheckSkipCount: 0,
    batchRunStartTime: null,

    // Game Section Drag & Drop
    currentDraggedGame: null,

    // PWA 설치 프롬프트
    _pwaInstallPrompt: null,
};

// AppState 프로퍼티를 window에 프록시 등록
// → 기존 모든 파일에서 변수명 직접 접근 시 AppState를 투명하게 읽고 씀
for (const key of Object.keys(AppState)) {
    Object.defineProperty(window, key, {
        get() { return AppState[key]; },
        set(v) { AppState[key] = v; },
        configurable: true,
        enumerable: true,
    });
}
