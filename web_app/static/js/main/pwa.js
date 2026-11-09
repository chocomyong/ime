// main/pwa.js — Service Worker 등록 + PWA 업데이트 배너 + 설치 프롬프트
// Phase 3 3차 분할 — 기존 main.js 에서 이관

// PWA — Service Worker 등록 + 새 버전 감지 알림 (v6.6.279)
if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
        navigator.serviceWorker.register('/sw.js', { scope: '/' })
            .then(reg => {
                // updatefound: 새 SW가 install 중
                reg.addEventListener('updatefound', () => {
                    const newSW = reg.installing;
                    if (!newSW) return;
                    newSW.addEventListener('statechange', () => {
                        // 기존 SW 가 이미 페이지를 제어 중이고 새 SW 가 installed 상태면 업데이트 가능
                        if (newSW.state === 'installed' && navigator.serviceWorker.controller) {
                            // [v6.7.371] 조용한 업데이트 — 상단 배너/10초 카운트다운 폐지(사용자: 너무 거창).
                            //   새 SW 즉시 활성화 → controllerchange 리스너가 silent reload. (showPwaUpdateBanner 미사용 잔존)
                            newSW.postMessage({ type: 'SKIP_WAITING' });
                        }
                    });
                });
                // [v6.7.5] 10s/15min → 30s/30min — v6.7.3 boot.js 의 _checkStaleSwCacheOnBoot 가 첫 진입 즉시
                //   stale SW 를 잡으므로 10초 폴링 의미 없음. 짧은 주기 폴링은 네트워크 보안 장비가 이상 패턴으로 오탐해 막을 수 있어 늘렸다.
                //   30초 첫 체크 + 30분 주기로도 socket mismatch path 와 결합 시 충분.
                setTimeout(() => reg.update().catch(() => {}), 30 * 1000);
                setInterval(() => reg.update().catch(() => {}), 30 * 60 * 1000);
            })
            .catch(err => console.warn('SW registration failed:', err));

        // 새 SW 가 controller 로 전환되면 한 번만 자동 reload
        // [v6.7.449] **최초 install 의 claim 은 reload 대상이 아니다** — 사용자 보고 "접근 시 초기에
        //   새로고침이 여러 번" 의 두 번째 새로고침이 여기였다.
        //   경위: sw.js `activate` 가 `clients.claim()` 을 부르므로 **SW 가 처음 설치될 때도**
        //   controllerchange 가 발화한다. 그런데 최초 install 은 '업데이트' 가 아니다 — 페이지는 이미
        //   방금 네트워크에서 받은 최신 HTML 이고(내비게이션은 network-first, 캐시 안 함), 자산 URL 도
        //   `?v=BUILD_VERSION` 이라 바꿔 받을 것이 없다. 새로고침해도 **똑같은 페이지를 다시 그릴 뿐**이다.
        //   특히 `nukeSwCacheAndReload` 가 SW 를 unregister 하므로 그 직후 로드는 항상 '최초 install'
        //   상태다 → stale 캐시 nuke reload(1회) 직후 claim reload(2회)가 연쇄했다.
        //   판별 = **페이지 로드 시점에 controller 가 있었는가**. 있었으면 진짜 SW 교체(→ reload),
        //   없었으면 최초 install 의 claim(→ 무시). 하드 새로고침(Shift+Reload)도 uncontrolled 로
        //   시작하므로 이 가드가 "새로고침했더니 또 새로고침" 도 함께 막는다.
        const _hadController = !!navigator.serviceWorker.controller;
        let _reloading = false;
        navigator.serviceWorker.addEventListener('controllerchange', () => {
            if (!_hadController) {
                console.log('[sw] 최초 install claim — reload 생략');
                return;
            }
            if (_reloading) return;
            if (typeof window.imtGuardAutoReload === 'function' && !window.imtGuardAutoReload('sw-controllerchange')) return;
            _reloading = true;
            window.location.reload();
        });
    });
}

// PWA 업데이트 알림 배너 — 사용자가 "지금 업데이트" 클릭 시 skipWaiting + 자동 reload
// [v6.6.299] 비전공자 사용자 대응: 10초 카운트다운 후 자동 업데이트.
//            닫기를 눌러도 그 세션에선 더 이상 방해하지 않고, 다음 reg.update() 주기에 재노출.
function showPwaUpdateBanner(waitingWorker) {
    // [v6.7.3] socket.js 풀스크린 오버레이가 이미 처리 중이면 PWA 배너 띄우지 않음 (race 회피).
    //   다만 SW 활성화는 시켜야 하므로 skipWaiting 만 발화 — controllerchange 가 reload 트리거.
    if (window.__updateOverlayActive) {
        if (waitingWorker) waitingWorker.postMessage({ type: 'SKIP_WAITING' });
        return;
    }
    if (document.getElementById('pwa-update-banner')) return;  // 중복 방지
    const banner = document.createElement('div');
    banner.id = 'pwa-update-banner';
    banner.className = 'pwa-update-banner';
    banner.innerHTML = `
        <span id="pwa-update-label">앱이 업데이트됐어요 · <b><span id="pwa-countdown">10</span>초</b> 후 화면이 새로고침됩니다 <small>(작업 중이면 닫기 눌러주세요)</small></span>
        <button type="button" id="pwa-update-btn">지금 새로고침</button>
        <button type="button" id="pwa-update-dismiss" aria-label="닫기">×</button>
    `;
    document.body.appendChild(banner);

    let remaining = 10;
    let applied = false;
    function applyUpdate() {
        if (applied) return;
        applied = true;
        clearInterval(timer);
        if (waitingWorker) waitingWorker.postMessage({ type: 'SKIP_WAITING' });
        const label = document.getElementById('pwa-update-label');
        if (label) label.textContent = '업데이트 적용 중...';
        // controllerchange 이벤트가 자동 reload 를 트리거한다 (main.js 상단 리스너 참조)
    }
    const timer = setInterval(() => {
        remaining -= 1;
        const cd = document.getElementById('pwa-countdown');
        if (cd) cd.textContent = String(Math.max(remaining, 0));
        if (remaining <= 0) applyUpdate();
    }, 1000);

    document.getElementById('pwa-update-btn').addEventListener('click', applyUpdate);
    document.getElementById('pwa-update-dismiss').addEventListener('click', () => {
        clearInterval(timer);
        banner.remove();
    });
}

// PWA — 홈화면 추가 프롬프트
window.addEventListener('beforeinstallprompt', (e) => {
    e.preventDefault();
    _pwaInstallPrompt = e;
    const btn = document.getElementById('pwa-install-btn');
    if (btn) btn.style.display = '';
});
window.addEventListener('appinstalled', () => {
    _pwaInstallPrompt = null;
    const btn = document.getElementById('pwa-install-btn');
    if (btn) btn.style.display = 'none';
});

function installPWA() {
    if (!_pwaInstallPrompt) return;
    _pwaInstallPrompt.prompt();
    _pwaInstallPrompt.userChoice.then(() => {
        _pwaInstallPrompt = null;
        const btn = document.getElementById('pwa-install-btn');
        if (btn) btn.style.display = 'none';
    });
}
