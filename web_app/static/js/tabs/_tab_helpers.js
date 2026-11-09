// static/js/tabs/_tab_helpers.js
// [v6.6.399] 커뮤니티 카테고리 탭 공통 유틸 (시각 디자인 무변경, UX/성능 개선)
//   - 검색 디바운스
//   - 탭 전환 시 스크롤 위치 보존/복원
//   - 검색 결과 0건 empty-state 토글 헬퍼
// 인용: PRODUCT_IMPROVEMENTS.md (현상유지 결정 후 본 세션 개선안 #1~#4, #10~#11)

(function (global) {
    'use strict';

    /** 디바운스 — 검색 입력 lag 방지. 기본 150ms. */
    function _debounce(fn, wait) {
        var t = null;
        var w = (typeof wait === 'number' && wait >= 0) ? wait : 150;
        return function debounced() {
            var args = arguments, ctx = this;
            if (t) clearTimeout(t);
            t = setTimeout(function () { t = null; fn.apply(ctx, args); }, w);
        };
    }

    /** 탭 별 스크롤 위치 메모리. tab-manager actions.js 의 toggleAppPane 에서 호출. */
    var _scrollMemory = {};

    function _persistScroll(appId) {
        if (!appId) return;
        try {
            var el = document.getElementById(appId + '-tab');
            if (!el) return;
            // tab-content 내부에서 실제 스크롤 컨테이너 탐색 (overflow-y:auto 인 요소)
            var scrollers = [el.firstElementChild, el].filter(Boolean);
            for (var i = 0; i < scrollers.length; i++) {
                var s = scrollers[i];
                if (s && (s.scrollTop || s.scrollLeft)) {
                    _scrollMemory[appId] = { top: s.scrollTop, left: s.scrollLeft };
                    return;
                }
            }
            // 모든 스크롤러가 0 이면 0 으로 기록 (사용자가 위로 올린 상태도 보존)
            _scrollMemory[appId] = { top: 0, left: 0 };
        } catch (_) { /* noop */ }
    }

    function _restoreScroll(appId) {
        if (!appId || !_scrollMemory[appId]) return;
        var saved = _scrollMemory[appId];
        try {
            var el = document.getElementById(appId + '-tab');
            if (!el) return;
            var scrollers = [el.firstElementChild, el].filter(Boolean);
            // 다음 frame 에 적용 (DOM 렌더 완료 후)
            requestAnimationFrame(function () {
                for (var i = 0; i < scrollers.length; i++) {
                    var s = scrollers[i];
                    if (s && typeof s.scrollTop !== 'undefined') {
                        s.scrollTop = saved.top || 0;
                        s.scrollLeft = saved.left || 0;
                    }
                }
            });
        } catch (_) { /* noop */ }
    }

    /**
     * 검색 결과 0건 empty-state 토글.
     * @param emptyEl       — 기존 empty-state DOM (없으면 noop)
     * @param visibleCount  — 보이는 항목 수
     * @param query         — 현재 검색어 (비어있으면 일반 empty 메시지, 있으면 검색 미스 메시지)
     * @param messages      — { empty: "...", searchMiss: "..." } 텍스트 (선택)
     */
    function _toggleEmptyState(emptyEl, visibleCount, query, messages) {
        if (!emptyEl) return;
        var hasResult = (visibleCount > 0);
        if (hasResult) {
            emptyEl.style.display = 'none';
            return;
        }
        // 0건 — 메시지 결정
        var msg = '';
        if (query && messages && messages.searchMiss) msg = messages.searchMiss;
        else if (messages && messages.empty) msg = messages.empty;
        else msg = '표시할 데이터가 없습니다';
        // 기존 텍스트 노드 우선 갱신 (구조 유지: <p> 또는 첫 자식 텍스트)
        var p = emptyEl.querySelector('p');
        if (p) p.textContent = msg;
        else if (emptyEl.firstChild && emptyEl.firstChild.nodeType === 3) emptyEl.firstChild.nodeValue = msg;
        else emptyEl.textContent = msg;
        emptyEl.style.display = 'block';
    }

    // [v6.7.6] 신선도 좌측 강조 바 — 타임스탬프 기반 3단계 분류
    // [v6.7.25] 시간 전용 포맷("HH:MM" / "HH:MM:SS") 도 처리 — 시트/SQLite 의 일부 레코드가
    //   날짜 없이 시간만 저장하는 케이스가 있어 NaN → age=0 → fresh 미분류 결함 (간헐 누락 원인) 차단.
    function _parseFeedTs(ts) {
        if (!ts) return 0;
        var s = String(ts).trim();
        var d = new Date(s.replace(' ', 'T'));
        if (!isNaN(d.getTime())) return d.getTime();
        // 시간 전용 fallback — "HH:MM" / "HH:MM:SS" 를 오늘 날짜와 결합.
        var m = s.match(/^(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?$/);
        if (m) {
            var now = new Date();
            var hh = +m[1], mm = +m[2], ss = m[3] ? +m[3] : 0;
            var dt = new Date(now.getFullYear(), now.getMonth(), now.getDate(), hh, mm, ss);
            // 자정 직후 케이스: 카드 ts="23:55" 인데 now="00:05" → 어제로 해석 (1분 grace).
            if (dt.getTime() > now.getTime() + 60000) dt.setDate(dt.getDate() - 1);
            return dt.getTime();
        }
        return 0;
    }

    // [v6.7.25] HTML 빌드 시점에 freshness 클래스를 마크업에 직접 주입 — post-process race 차단.
    //   mode='card' → 'feed-card--fresh' / 'feed-card--recent' / ''.
    //   mode='row'  → 'feed-row--fresh' / 'feed-row--recent' / ''.
    //   60초 인터벌의 _applyFreshness 는 그대로 — 시간 경과 시 재분류 (fresh→recent→none).
    // [v6.7.27] source 인자 추가 — 'cafe' 면 amber 변형 클래스 동시 부여(기본 cyan 위에 override).
    //   source: 'cafe' | 'dc' | undefined. 카페 source 카드 strip 색상이 디시(cyan)와 동일하던 결함 정공법 수정.
    // [v6.7.29] 미래 ts grace — 서버 클럭 skew 또는 분-단위 절단 ts ("15:16" → 오늘 15:16:00) 가
    //   클라이언트 시계보다 잠깐 앞설 때, age 가 음수가 되어 baking 시점 클래스 미부여 → 60초 인터벌까지 lag.
    //   age 가 -FRESH_MS (5분) 이내 미래면 fresh 로 분류. 5분 이상 미래는 데이터 에러로 간주, 무시.
    // [v6.7.407] freshness 엣지스트립/틴트 제거(사용자) — 두 함수는 no-op stub 로 강등.
    //   신선도 시간감쇠 신호는 도착 잔광(imt-arrival-glow, 각 탭 자체 관리)으로 일원화.
    //   호출부(feed-tab/comments-tab ~8곳)는 무수정 — freshnessClass 는 원래도 비-fresh 항목에 '' 반환이라
    //   전 호출부가 빈 문자열을 이미 안전 처리 중(concat/if(fc) 가드). export 유지(호출부 ReferenceError 방지).
    function _freshnessClass(ts, mode, source) {
        return '';
    }

    function _applyFreshness(containerEl) {
        return;
    }

    // 첫 조회에만 사용한다. 갱신 때는 기존 결과를 유지하고 별도 진행 상태를 표시한다.
    function _loadingHtml(label) {
        var title = document.createElement('strong');
        title.textContent = label || '데이터를 불러오는 중';
        return '<div class="ime-data-state" role="status" aria-live="polite" aria-busy="true">'
            + '<span class="ime-data-state__spinner" aria-hidden="true"></span><span>'
            + title.outerHTML + '<small>잠시만 기다려 주세요.</small></span></div>';
    }

    // export
    global._tabHelpers = {
        loadingHtml: _loadingHtml,
        debounce: _debounce,
        persistScroll: _persistScroll,
        restoreScroll: _restoreScroll,
        toggleEmptyState: _toggleEmptyState,
        applyFreshness: _applyFreshness,
        freshnessClass: _freshnessClass,  // [v6.7.25] HTML 빌드 시점 주입용
    };
})(window);
