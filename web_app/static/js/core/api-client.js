// core/api-client.js — 서버 상태 조회, 크롤러 제어, 카운트다운

// [v6.7.741] updateStatus() 삭제 - /api/status 를 30초마다 불러 헤더 상태 대시보드(메인 크롤러 배지 · 마지막 갱신 시각)에만
//   쓰던 함수다. 상태 대시보드를 지우면서 결과를 쓰는 곳이 0 이 됐다. 서버 /api/status 라우트도 [v6.7.744] 에 지웠다.

async function syncDbFromSheets() {
    const btn = document.getElementById('syncDbBtn');
    if (!btn) return;
    const originalText = btn.innerHTML;

    if (!confirm('구글 시트의 최신 데이터를 SQLite DB로 다시 가져오시겠습니까?\n(터미널 앱이 켜져 있어야 하며, 약 10~30초가 소요됩니다.)')) {
        return;
    }

    try {
        btn.disabled = true;
        btn.innerHTML = '<span class="loading" style="display:inline-block; vertical-align:middle;"></span> 동기화 중...';

        const resp = await fetch(`${API_BASE}/sync_db`, { method: 'POST' });
        const result = await resp.json();

        if (result.success) {
            showToast(result.message);
        } else {
            showToast('동기화 명령 전송 실패: ' + result.message, true);
        }
    } catch (err) {
        console.error(err);
        showToast('서버 통신 오류가 발생했습니다.', true);
    } finally {
        setTimeout(() => {
            btn.disabled = false;
            btn.innerHTML = originalText;
        }, 3000);
    }
}

function displayTimeLeft() {}

async function updateCountdowns() {
    // no-op interval 제거 — displayTimeLeft가 구현되면 그때 interval 등록
}

async function startCrawler() { try { await fetch(`${API_BASE}/crawler/start`, { method: 'POST' }); showToast('크롤러가 시작되었습니다'); } catch (error) { showToast('시작 실패: ' + error, true); } }
async function stopCrawler() { try { await fetch(`${API_BASE}/crawler/stop`, { method: 'POST' }); showToast('크롤러가 중지되었습니다'); } catch (error) { showToast('중지 실패: ' + error, true); } }
