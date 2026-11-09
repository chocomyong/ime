# 1. 기본 이미지 설정
# Python 3.11 버전을 기반으로 하는 슬림 버전을 사용하여 이미지 크기를 최적화합니다.
FROM python:3.11-slim-bookworm

# 2. 작업 디렉토리 설정
# 컨테이너 내에서 작업할 기본 경로를 설정합니다.
WORKDIR /app

# 3. Google Chrome 및 드라이버 설치 (Selenium용)
# Debian/Ubuntu 기반 이미지에 Google Chrome을 설치합니다.
# [v6.7.736] chromedriver 는 platform == linux64 로 고른다. 목록에 linux-arm64 가 생겨 chromedriver[0] 이 arm64 zip 을
#   가리키게 됐고(2026-10-22 NAS 재빌드 실측: unzip 이 linux64.zip 을 못 찾아 exit 9), 같은 빌드 번호의 패치가 여럿이라
#   tail -n 1 로 가장 새 것 하나만 받는다. 비면 다음 줄 [ -n ] 에서 멈춘다(빈 wget 이 엉뚱한 단계에서 터지지 않게).
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    unzip \
    jq \
    curl \
    libgomp1 \
    && wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | apt-key add - \
    && sh -c 'echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" >> /etc/apt/sources.list.d/google-chrome.list' \
    && apt-get update \
    && apt-get install -y google-chrome-stable \
    && rm -rf /var/lib/apt/lists/* \
    && CHROME_VERSION=$(google-chrome --version | cut -f 3 -d ' ' | cut -d '.' -f 1-3) \
    && DRIVER_VERSION=$(curl -s "https://googlechromelabs.github.io/chrome-for-testing/known-good-versions-with-downloads.json" | jq -r ".versions[] | select(.version | startswith(\"${CHROME_VERSION}\")) | .downloads.chromedriver[]? | select(.platform == \"linux64\") | .url" | tail -n 1) \
    && [ -n "${DRIVER_VERSION}" ] \
    && wget -q --continue -P /tmp/ ${DRIVER_VERSION} \
    && unzip /tmp/chromedriver-linux64.zip -d /tmp \
    && mv /tmp/chromedriver-linux64/chromedriver /usr/local/bin/chromedriver \
    && chmod +x /usr/local/bin/chromedriver \
    && rm -rf /tmp/chromedriver-linux64*
 
# [추가] pip 최신 버전으로 업데이트 (의존성 해결 문제 방지)
RUN pip install --upgrade pip



# 5. Python 라이브러리 설치
# requirements.txt 파일을 사용하여 필요한 라이브러리를 설치합니다.
# [수정] requirements.txt를 먼저 복사하여 Docker 캐시를 활용합니다.
COPY requirements.txt /app/
# [중요] windows 로컬 환경과의 호환성을 위해 requirements.txt에서 torch 관련 패키지 제외 후 설치 진행
RUN grep -vE "torch|torchaudio|torchvision" requirements.txt > requirements_filtered.txt && \
    pip install --no-cache-dir gunicorn eventlet && \
    pip install --no-cache-dir -r requirements_filtered.txt && \
    rm requirements_filtered.txt
# 4. 필요한 파일 복사
# .dockerignore에 명시된 파일을 제외하고 현재 디렉토리의 모든 파일을 /app으로 복사합니다.
COPY . /app/

# [v6.6.229] 호스트 파일시스템(홈 서버 btrfs/ACL)의 mode=000 전파 방어 — 서비스 계정/설정 JSON 권한 정규화
RUN chmod 644 /app/*.json 2>/dev/null || true

# [v6.7.342] PATTERN-22 확장 — 정적 자산(.js/.css 등) 000 권한 전파 방어.
#   홈 서버 worktree 가 당일 수정된 .js 를 000 으로 pull → COPY 가 000 으로 baked (실측 2026-07-16:
#   notif-inbox.js·topbar-status.js). 위 chmod 644 는 *.json 만 커버해 .js 는 방치됐다. 현재는 gunicorn=root 라
#   root 가 000 파일도 읽어 서빙되지만(latent), 비-root 전환 시 즉시 static 서빙이 깨진다. a+rX = 디렉토리엔
#   순회(+x), 파일엔 읽기(+r)만 부여(대문자 X — .js/.css 에 불필요한 실행권한 안 붙음).
#   스코프는 /app/static 한정 — /app 전체로 넓히면 service_account.json 등 크리덴셜에 a+r 이 붙어 posture 악화(위 *.json 644 유지).
RUN chmod -R a+rX /app/static 2>/dev/null || true

# [v6.7.261] 이미지-불변 BUILD_VERSION — 빌드 시각을 1회 굽어 런타임(app.py _resolve_build_version)이 읽는다.
#   같은 이미지 재시작(autoheal / 워커 SIGKILL 재기동 / 컨테이너 restart, PATTERN-29/30/31)에서는 값이 불변 →
#   클라이언트의 false "서버 업데이트 중" 오버레이 + 강제 reload 가 사라진다. docker-compose up --build(새 이미지)
#   에서만 값이 갱신 → 정당한 자동 reload 유지. COPY 직후 레이어라 빌드 컨텍스트가 바뀐 빌드에서만 date 가 새로
#   평가됨(무변경 빌드는 레이어 캐시 → 불필요한 전(全)클라 reload 없음). 컨테이너 내부 RUN 산출 파일이라 PATTERN-22
#   (호스트 000 권한 전파) 무관. chmod 644 동반.
RUN date +%s > /app/.build_version && chmod 644 /app/.build_version

# 6. 포트 노출
# Flask 앱이 실행될 5001번 포트를 외부로 노출합니다.
# 컨테이너가 5001번 포트를 사용함을 명시
EXPOSE 5001

# 7. 애플리케이션 실행
# [v6.6.229] worker-class: eventlet → gthread
#   - eventlet 의 소켓 monkey patching 이 google-genai(gRPC)의 sendmsg 를 깨뜨려 Gemma4 클라이언트 초기화 실패
#   - gthread 는 표준 threading 풀이라 monkey patching 없음. Flask-SocketIO(async_mode='threading')와 궁합 일치
#   - WebSocket 은 long-polling 으로 자동 fallback (실시간성 유지)
# [v6.7.79] threads 8 → 4 — 홈 서버(2코어 2스레드, AVX 없음) 하드웨어 매칭.
#   8 threads 는 GIL 경합으로 AI 추론 시 모든 HTTP 워커 같이 느려짐. 4 = 코어 수 × 2 균형점.
#   -w 1 유지 (APScheduler 중복 등록 방지 — 다중 워커는 별도 분리 필요).
# [v6.7.80] --timeout 30(기본) → 120, --graceful-timeout 30 명시.
#   사용자 보고 502 폭주 (정적 JS 까지 502) = 단일 워커가 느린 요청 (AI / Sheets) 에서
#   gunicorn 기본 30s timeout 으로 SIGKILL → 그동안 모든 요청 502 → 재시작 → 반복.
#   timeout 4배로 늘려 워커 강제 종료 빈도 감소.
#   --max-requests 1000 + --max-requests-jitter 100: 메모리 누수 방어 (워커가 1000~1100 요청 처리 후 graceful 재시작).
# [v6.7.84] threads 4 → 8 (v6.7.79 결정 일부 회귀).
#   v6.7.83 에서 Gemma4 60s → 180s 로 GIL 압박이 1/3 로 줄어 v6.7.79 의 "threads=4 로 GIL 경합 완화" 전제가 약해짐.
#   사용자 보고: LCP 27s + LCP element `h3.pane-header-title` (정적 헤더) = render-blocking CSS 6개 (head 안) 가
#   threads=4 concurrent 한계에 queue 됨. LCP 가 CSS 마지막 도달 시점을 기다림.
#   threads=8 로 CSS + JS + favicon 등 초기 자산 30+개를 더 많이 동시 serving → 첫 paint 단축.
#   GIL 부담은 Gemma4 180s + Selenium subprocess + 대부분 IO bound 라 8 thread 도 안전.
# [v6.7.87] --timeout 120 → 240. 사용자 보고: docker stats 에서 imt CPU 90% (MEM 1GB/10GB = OOM 아님).
#   SIGKILL 원인이 OOM 이 아닌 CPU 포화 → 느린 요청이 120s 도 못 채우고 timeout. 240s 로 늘려 SIGKILL 빈도 추가 ↓.
#   CPU 포화 자체는 코드 외 운영 영역 (Selenium Chrome 부담 / 크롤러 주기 / 홈 서버 2 코어 한계) — timeout 만 완화.
# [v6.7.88] --max-requests 1000 + --max-requests-jitter 100 제거.
#   사용자 로그 패턴 분석:
#     Worker exiting (워커 자가 종료) → 정확히 3:32 hang → SIGKILL → 새 워커 → 1:14 만에 또 exiting
#   = --max-requests rotation 이 trigger → APScheduler Gemma4 in-flight 배치가 graceful shutdown 을 hang →
#     master --timeout 으로 SIGKILL → 재시작 후 클라이언트 reconnect burst 로 즉시 1000 도달 → 반복.
#   사용자 환경 MEM 1GB / 10GB = 누수 없음 → rotation 의 메모리 보호막 의미 없고 cycle 만 유발. 제거.
#   실제 메모리 leak 우려는 docker stats 메모리 그래프 + 주기 재시작 (월 1회 등) 으로 대체 가능.
CMD ["gunicorn", "--worker-class", "gthread", "-w", "1", "--threads", "8", "--timeout", "240", "--graceful-timeout", "30", "--bind", "0.0.0.0:5001", "app:app"]