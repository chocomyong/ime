# -*- coding: utf-8 -*-
"""
web_lib.routes — Flask Blueprint 패키지 (Phase 1 완료)

기존 `web_lib/web_routes.py` 에 집중돼 있던 67개 라우트를 기능 축으로 분할한다.
외부에서 `api_bp` 를 import 하는 경로(`web_lib.web_routes.api_bp` / `web_lib.routes.api_bp`)
는 모두 동일 객체를 가리키도록 유지. app.py / app_viewer.py 는 변경 불필요.

Phase 1 완료 시점 분할 구조:
- admin.py          : /api/admin/* (20개) — v6.6.367
- pages.py          : /, /reset, /sw.js (4개) — v6.6.368
- articles.py       : /api/articles/*, /api/comments (7개) — v6.6.369
- crawler_control.py: /api/crawler/* (6개) — v6.6.370
- config_api.py     : /api/config/*, /api/games, /api/galleries, /api/night-report-games,
                      /api/community-urls - v6.6.371 ([v6.7.741] /api/updates 삭제)
- trends.py         : /api/night-trends/*, /api/preview-trend, /api/price-trend (4개) — v6.6.372
- analysis.py       : /api/ai-analysis, /api/data/history (2개) — v6.6.373
- extension.py      : /api/logs*, /api/extension/super-alerts, /api/test-alert,
                      /api/server-start-time — v6.6.374 ([v6.7.744] /api/status 삭제)
- sub_crawlers.py   : 서브 크롤러 4종 × 5 라우트 팩토리 (20개) — v6.6.374
"""
from flask import Blueprint

api_bp = Blueprint('api_bp', __name__)

# 서브 모듈 로드 → 데코레이터 실행 → api_bp 에 라우트 등록
# (웹_routes 의 back-import 와 순환 문제 없음: api_bp 는 위에서 이미 정의됨)
from . import _helpers  # noqa: F401
from . import admin  # noqa: F401
from . import pages  # noqa: F401
from . import articles  # noqa: F401
from . import crawler_control  # noqa: F401
from . import config_api  # noqa: F401
from . import trends  # noqa: F401
from . import analysis  # noqa: F401
from . import extension  # noqa: F401
from . import sub_crawlers  # noqa: F401
from . import anomaly  # noqa: F401  # [v6.7.629] 이상감지 API (구 situation.py 의 anomaly 절만 남겨 분리)
from . import search  # noqa: F401  # [v6.7.69] F-4 통합 검색/필터 (관리자 전용)
from . import health  # noqa: F401  # [v6.7.96] /api/health — docker healthcheck 용 liveness probe
from . import grafana  # noqa: F401  # [v6.7.215] 그라파나 CCU 미러 (admin 전용 read — Config_Grafana 탭)
# [v6.7.741] 워처 관제 라우트 3종(naver_watcher · poe_watcher · discord_monitor + 팩토리 _watcher_routes) 삭제 -
#   워처 종료(사용자 2026-10-24)로 "알림" 탭 · 디스코드 탭 · 상단 상태등과 함께 지웠다.
from . import verdict  # noqa: F401  # [v6.7.700] 판정 탭 - 1차 분류기 그림자 운영(imt-fp) fp.sqlite RO read (admin 전용)
from . import push_api  # noqa: F401  # [v6.7.360] PWA Web Push 구독 관리 (admin) - bg/web_push.py 페어
