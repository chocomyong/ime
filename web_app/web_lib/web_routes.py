# -*- coding: utf-8 -*-
"""web_routes — Phase 1 분할 후 facade.

기존 1,601 LOC 단일 파일이 `web_lib/routes/` 하위 9개 sub-module 로 분할되었다.
본 파일은 `app.py` / `app_viewer.py` 의 `from web_lib.web_routes import api_bp` 를
계속 유효하게 유지하기 위한 재수출 전용 모듈이다.

**새 라우트를 여기에 추가하지 말 것.** 반드시 `web_lib/routes/` 하위
적절한 sub-module (기능 그룹에 해당) 에 작성하라. 상세 매핑은
`web_lib/routes/__init__.py` 모듈 docstring 참조.
"""
from web_lib.routes import api_bp  # noqa: F401
