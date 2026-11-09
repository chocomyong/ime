# -*- coding: utf-8 -*-
"""routes.health — gunicorn worker liveness probe (docker healthcheck 용).

[v6.7.96] PATTERN-29 의 워커 hang → 사이트 down 사이클 자동 복구 인프라.
docker-compose.yml 의 `healthcheck` 블록이 30s 주기로 이 endpoint 를 hit 해서
worker 가 응답 가능한지 확인 → 3회 연속 실패 시 docker 가 unhealthy 판정 →
`restart: always` 와 결합되어 자동 재시작.

내부 객체 의존 0 — DB / Selenium / sheet 단 hydrate 지연과 무관하게 즉답.
sheet 매니저 lock 이나 Selenium thread saturation 시에도 gunicorn worker 자체가
응답 가능하면 200 반환. (worker 가 hang 한 경우에만 timeout → unhealthy)
"""
import time

from flask import jsonify

from web_lib.routes import api_bp


@api_bp.route('/api/health', methods=['GET'])
def healthcheck():
    return jsonify({'ok': True, 'ts': time.time()}), 200
