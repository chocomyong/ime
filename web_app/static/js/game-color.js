// static/js/game-color.js
// 게임 색상 팔레트 및 색상 조회 함수

const IME_GAME_COLORS = [
    '#3182f6', // 기본 보라
    '#03dac6', // 청록 (Teal)
    '#ff8a80', // 산호 (Coral)
    '#ffd54f', // 개나리 (Amber)
    '#81c784', // 연두 (Green)
    '#4fc3f7', // 하늘 (Light Blue)
    '#ba68c8', // 연보라 (Purple)
    '#ffb74d', // 귤색 (Orange)
    '#a1887f', // 갈색 (Brown)
    '#90a4ae'  // 회청색 (Blue Grey)
];

// 게임별 고정 테마 색상 (현재 구글 시트 등록 제품 리스트 기준)
const FIXED_GAME_THEMES = {
    '오딘': '#5ea4ff',              // 오딘: 조금 더 밝은 스카이 블루 (가시성 상향)
    '아키에이지': '#1de9b6',        // 아키워: 릴리안 민트
    '아레스': '#ff5252',             // 아레스: 강렬한 레드
    '우마무스메': '#f06292',         // 우마무스메: 소프트 핑크
    '프렌즈': '#fdd835'              // 프렌즈타운: 카카오 옐로우
};

const _gameColorCache = {};   // gameName -> 테마 무관 base hex (hue 배정)
const _lightColorCache = {};  // base hex -> 라이트 가독 hex (명도 하향 결과 memo)

// [v6.7.228] 라이트 테마(흰 배경 slate-50) 가독성 — 밝은 팔레트(#03dac6/#fdd835/#1de9b6 등)가
//   흰 배경서 워시아웃(대비 1.3~2:1). hue 는 유지하고 명도만 낮춰 ~3.2:1 이상 확보.
//   다크 5종(OLED 검정)은 변환 미적용 → 원본 색 그대로(회귀 0).
function _hexToRgb(hex) {
    const m = /^#?([0-9a-f]{6})$/i.exec(hex || '');
    if (!m) return null;
    const n = parseInt(m[1], 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}
function _rgbToHex(r, g, b) {
    const h = v => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, '0');
    return '#' + h(r) + h(g) + h(b);
}
function _relLum(r, g, b) {
    const f = c => { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
}
// 흰 배경(slate-50, 상대휘도 ~0.90) 대비 ~3.2:1 보장까지 RGB 를 검정 쪽으로 스케일(hue 보존).
function _darkenForLight(hex) {
    if (_lightColorCache[hex]) return _lightColorCache[hex];
    const rgb = _hexToRgb(hex);
    if (!rgb) return hex;
    const [r, g, b] = rgb;
    const BG_LUM = 0.90;
    const targetLum = (BG_LUM + 0.05) / 3.2 - 0.05;  // ≈ 0.247
    let k = 1;
    while (k > 0.1 && _relLum(r * k, g * k, b * k) > targetLum) k -= 0.04;
    const out = _rgbToHex(r * k, g * k, b * k);
    _lightColorCache[hex] = out;
    return out;
}
// data-theme="light" 일 때만 흰 배경 가독색으로 치환. 그 외(다크 5종)는 원본 hex 그대로.
function _themeColor(hex) {
    if (typeof hex === 'string' && hex.charAt(0) === '#' &&
        typeof document !== 'undefined' && document.body &&
        document.body.getAttribute('data-theme') === 'light') {
        return _darkenForLight(hex);
    }
    return hex;
}

// [v6.7.333] 테마 전환 시 "이미 렌더된" 게임명 색 재적용.
//   v6.7.228 이 getGameColor() 를 호출시점-테마 인식으로 만들었으나, 사용처가 반환값을 인라인
//   style="color:..." 로 굽는 구조라 전환 후에도 옛 색이 남는다 — v6.7.228 자신이 "테마 전환 시
//   다음 렌더부터 자동 반영" 으로 자인한 한계. 그런데 목록 탭(피드/댓글/AI/전체/커뮤니티/계정거래)은
//   imt-theme-changed 를 구독하지 않아(구독자는 차트 모듈뿐) 그 "다음 렌더"가 영영 안 올 수 있다.
//   라이트→다크 전환 시 _darkenForLight 로 검정 근처까지 내려간 색이 OLED 검정 위에 남아 불가시.
//   키를 textContent 가 아니라 data-gc(색 산출에 실제 쓴 원본 게임명)로 두는 이유:
//     - 댓글 탭은 표시명(_normalizeCommentGameName)과 색 키(_rawGame)가 다름 → 텍스트 기반이면 오색.
//     - .game-name 은 커뮤니티/게임 탭 섹션 헤더(건수 배지 포함)와 충돌 → 색 없던 곳에 색 주입 회귀.
//   재적용은 테마 전환(사용자 액션, 저빈도) 때만 → PATTERN-38(주기적 통째 재렌더) 비해당.
window.reapplyGameColors = function() {
    try {
        document.querySelectorAll('[data-gc]').forEach(function(el) {
            const name = el.getAttribute('data-gc');
            if (!name) return;   // 게임명 없는 행 — 인라인 색도 var(--primary-color) 폴백이라 손대지 않음
            el.style.color = window.getGameColor(name);
        });
    } catch (_) {}
};
window.addEventListener('imt-theme-changed', window.reapplyGameColors);

window.getGameColor = function(gameName) {
    if (!gameName) return 'var(--primary-color)';

    // 1. 고정 매핑 확인
    for (const [key, color] of Object.entries(FIXED_GAME_THEMES)) {
        if (gameName.includes(key)) return _themeColor(color);
    }

    // 2. 캐시 확인 (base hue 는 테마 무관 캐시, 라이트 변환은 _themeColor 가 매 호출 적용)
    if (_gameColorCache[gameName]) return _themeColor(_gameColorCache[gameName]);

    // 3. 해시 기반 랜덤 배정
    let hash = 0;
    for (let i = 0; i < gameName.length; i++) {
        hash = gameName.charCodeAt(i) + ((hash << 5) - hash);
    }
    const index = Math.abs(hash) % IME_GAME_COLORS.length;
    _gameColorCache[gameName] = IME_GAME_COLORS[index];
    return _themeColor(_gameColorCache[gameName]);
};
