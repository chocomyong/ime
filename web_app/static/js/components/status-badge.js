// components/status-badge.js — 크롤러 상태 배지 업데이트

function updateStatusBadge(elementId, status) {
    const el = document.getElementById(elementId);
    if (!el) return;

    let message = typeof status === 'string' ? status : (status.status_message || '대기');
    let isRunning = typeof status === 'object' ? status.running : message.includes('실행 중');
    let isError = typeof status === 'object' ? !!status.error : false;

    el.textContent = message;
    el.className = 'status-badge';

    if (isRunning) el.classList.add('running');
    else if (isError) el.classList.add('error');
    else if (message.includes('완료')) el.classList.add('complete');
    else if (message.includes('로드됨')) el.classList.add('loaded');
    else el.classList.add('waiting');
}
