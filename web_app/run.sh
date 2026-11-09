#!/bin/bash
# Flask 웹 크롤러 - Linux/macOS 시작 스크립트

# 스크립트가 있는 디렉토리로 이동합니다.
# 이렇게 하면 다른 위치에서 스크립트를 실행해도 항상 올바르게 작동합니다.
cd "$(dirname "$0")"

echo "========================================"
echo "Flask 웹 크롤러 시작"
echo "========================================"

# 가상 환경이 있다면 활성화 (권장)
# if [ -d "venv" ]; then
#     echo "가상 환경(venv) 활성화..."
#     source venv/bin/activate
# fi

echo ""
echo "[1/2] 라이브러리 확인 및 설치..."
python3 -m pip install -r requirements.txt

if [ $? -ne 0 ]; then
    echo "오류: 라이브러리 설치 실패"
    echo "Python3와 pip가 설치되어 있는지, C++ 빌드 도구가 필요한 경우 설치되었는지 확인하세요."
    exit 1
fi

# Flask 앱 실행
echo ""
echo "[2/2] Flask 앱 시작..."
echo "서버가 127.0.0.1:5001 에서 실행됩니다. (로컬 전용, 외부 접속 불가)"
echo "중지하려면 Ctrl+C를 누르세요."
echo ""

# --- 방법 1: Python 직접 실행 (기존 방식) ---
python3 app.py

# --- 방법 2: Docker로 실행 (권장) ---
# 아래 주석을 해제하여 Docker로 빌드하고 실행할 수 있습니다.
# Dockerfile이 현재 디렉토리에 있어야 합니다.

# echo "Docker 이미지 빌드 중..."
# docker build -t imt-app .

# echo "Docker 컨테이너 실행 중... (http://localhost:5001)"
# # -d: 백그라운드 실행, --rm: 컨테이너 종료 시 자동 삭제, -p: 포트 연결
# docker run -d --rm -p 5001:5001 --name imt-container imt-app
