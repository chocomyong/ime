// main/fetch-patch.js — 전역 fetch wrapper (관리자 세션 자동 revoke)
// Phase 3 3차 분할 — 기존 main.js 에서 이관

const API_BASE = '/api';

// [v6.6.328] 관리자 전용 엔드포인트가 403 + admin_required 응답을 돌려주면, 지금 창도 관리자 UI 를
//   즉시 무음 revoke. 창 2개를 켜두고 다른 창에서 로그아웃한 직후 이 창이 어떤 관리자 API 를 호출하면
//   본 wrapper 가 즉시 _revokeAdminUI() 를 호출해 사이드바/탭/버튼을 숨기고, 현재 열려 있는 관리자
//   전용 탭은 total 로 전환한다. 토스트/alert 없음 (사용자 요구: "관리자 권한이 필요하다는 문구도
//   안 보여야").
(function () {
    if (window.__imtFetchPatched) return;
    window.__imtFetchPatched = true;
    const _origFetch = window.fetch;
    window.fetch = function (input, init) {
        const p = _origFetch.apply(this, arguments);
        try {
            p.then(function (resp) {
                try {
                    if (resp && resp.status === 403 && window._isAdmin) {
                        resp.clone().json().then(function (j) {
                            if (j && j.admin_required && typeof _revokeAdminUI === 'function') {
                                _revokeAdminUI();
                            }
                        }).catch(function () { /* JSON 아님 — 무시 */ });
                    }
                } catch (e) { /* noop */ }
            }).catch(function () { /* 네트워크 에러는 본 로직과 무관 */ });
        } catch (e) { /* noop */ }
        return p;
    };
})();
