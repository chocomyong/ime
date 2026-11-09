// [v6.7.349] 전역 키보드 단축키 - 데스크탑 모니터링 조작 가속 (2026-07-15 앱다운 제안서 A4, 1차 배치).
// 키맵 (물리 키 e.code 매칭 - 한글 자판 상태에서 e.key 는 'ㄱ' 류 조합문자가 와서 부적합):
//   1 / 2      피드 / 댓글 pane 전환 (전 사용자). 은닉 탭(app.py `HIDDEN_APP_IDS`)이면 무동작 +
//              도움말에서도 그 줄이 빠진다 - 2026-09-18 현재 '댓글' 이 은닉이라 2 는 아무 일도 안 한다.
//   3          대시보드(situation) pane 전환 (admin·manager - [v6.7.711] _canAccessApp 판정, 그 외 무동작)
//   R          데이터 새로고침 - refreshAll(controls.js) 위임.
//              주의: 상단바 버튼(topbarRefreshClick)은 clearLocalStorage(개인 설정 초기화 + reload)라
//              단축키 위임 대상이 아님 - 비파괴 refreshAll 이 올바른 시맨틱.
//   /          통합 검색 탭 + 검색어 입력(#searchQ) 포커스 (admin 전용)
//   ? (Shift+/) 단축키 도움말 오버레이 토글
// 가드: 입력 요소 포커스 / IME 조합 중(isComposing) / 수정자 키(Ctrl·Alt·Meta) / 모달 열림 = 무동작.
// admin 전용 탭은 window._isAdmin === true 명시 비교 (manager 는 false - v6.7.335 배너 게이트와 동형).
(function () {
    'use strict';

    var HELP_ID = 'shortcuts-help-overlay';

    function _isTypingTarget(el) {
        if (!el || !el.tagName) return false;
        var tag = el.tagName.toUpperCase();
        return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable === true;
    }

    // 설정 모달 등 .modal(style.css:1995, display 토글식)이 하나라도 열려 있으면 단축키 보류 -
    // 모달 뒤에서 pane 이 바뀌는 혼란 방지.
    function _anyModalOpen() {
        var modals = document.querySelectorAll('.modal');
        for (var i = 0; i < modals.length; i++) {
            try { if (getComputedStyle(modals[i]).display !== 'none') return true; } catch (_) {}
        }
        return false;
    }

    function _go(appId, adminOnly) {
        if (adminOnly && window._isAdmin !== true) return;
        if (typeof ensureAppPane === 'function') ensureAppPane(appId);
    }

    // [v6.7.711] role 별 개방 탭은 adminOnly 불리언으로 못 적는다 - ui-control.js 의 _canAccessApp 이 정본
    //   (MANAGER_EXTRA_APP_IDS). 대시보드 manager 개방 때 이 파일이 PATTERN-56 표 밖 9번째 게이트였다.
    function _canOpen(appId) {
        return typeof window._canAccessApp === 'function' && window._canAccessApp(appId);
    }

    function _focusSearch() {
        if (window._isAdmin !== true) return;
        _go('search', true);
        // 탭 렌더 직후 입력 필드가 생기므로 다음 틱에 포커스 (search-tab.js #searchQ)
        setTimeout(function () {
            var q = document.getElementById('searchQ');
            if (q) { try { q.focus(); q.select(); } catch (_) {} }
        }, 120);
    }

    function _closeHelp() {
        var ex = document.getElementById(HELP_ID);
        if (ex && ex.parentNode) ex.parentNode.removeChild(ex);
        return !!ex;
    }

    function _toggleHelp() {
        if (_closeHelp()) return;
        // [v6.7.677] 은닉 탭(app.py `HIDDEN_APP_IDS`)의 줄은 빼고 그린다 - 키 자체는 toggleAppPane 문지기가
        //   이미 무동작으로 만들지만, 도움말만 남으면 "안내가 가리키는 것이 안 열리는" 상태가 된다(PATTERN-74).
        //   줄이 조건부로 빠지므로 splice 인덱스(구 `splice(2, ...)`)로 끼워 넣지 않는다 - 앞 줄 하나가
        //   사라지면 '3' 이 'R' 뒤로 밀려 순서가 조용히 어긋난다. 순서대로 push 한다.
        var _hiddenApps = Array.isArray(window.IMT_HIDDEN_APPS) ? window.IMT_HIDDEN_APPS : [];
        var rows = [['1', '피드']];
        if (_hiddenApps.indexOf('comments') === -1) rows.push(['2', '댓글']);
        if (_canOpen('situation')) rows.push(['3', '대시보드']);
        rows.push(['R', '데이터 새로고침']);
        if (window._isAdmin === true) rows.push(['/', '통합 검색']);
        rows.push(['?', '이 도움말']);
        var el = document.createElement('div');
        el.id = HELP_ID;
        el.setAttribute('role', 'dialog');
        el.setAttribute('aria-label', '키보드 단축키');
        var html = '<div class="sc-help-title">키보드 단축키</div>';
        for (var i = 0; i < rows.length; i++) {
            html += '<div class="sc-help-row"><kbd>' + rows[i][0] + '</kbd><span>' + rows[i][1] + '</span></div>';
        }
        html += '<div class="sc-help-hint">클릭 또는 Esc 로 닫기</div>';
        el.innerHTML = html;
        el.addEventListener('click', _closeHelp);
        document.body.appendChild(el);
    }

    document.addEventListener('keydown', function (e) {
        if (e.defaultPrevented || e.isComposing) return;
        if (e.ctrlKey || e.altKey || e.metaKey) return;
        if (e.code === 'Escape') { _closeHelp(); return; }   // 도움말만 닫음 (기존 Escape 핸들러들과 무충돌)
        if (_isTypingTarget(e.target)) return;
        if (_anyModalOpen()) return;

        switch (e.code) {
            case 'Digit1': case 'Numpad1': _go('feed', false); break;
            case 'Digit2': case 'Numpad2': _go('comments', false); break;
            case 'Digit3': case 'Numpad3': if (_canOpen('situation')) _go('situation', false); break;
            case 'KeyR':
                if (e.shiftKey) return;
                if (typeof refreshAll === 'function') { e.preventDefault(); refreshAll(); }
                break;
            case 'Slash':
                e.preventDefault();
                if (e.shiftKey) _toggleHelp();
                else _focusSearch();
                break;
        }
    });
})();
