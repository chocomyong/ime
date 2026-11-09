# -*- coding: utf-8 -*-
"""web_lib/bg_tasks.py — Phase 2 분할 후 facade.

기존 1,247 LOC 단일 파일이 `web_lib/bg/` 하위 sub-module 로 분할되었다
(분할 당시 6개, 2026-09-06 실측 18개. 개수는 `ls web_lib/bg/*.py | wc -l` 로 센다 - 여기 박으면 늙는다).
본 파일은 외부의 `from web_lib.bg_tasks import X` 호출을 유지하기 위한 재수출 전용 모듈이다.

**새 함수를 여기에 추가하지 말 것.** 반드시 `web_lib/bg/` 하위 적절한 sub-module
(기능 그룹에 해당) 에 작성하고, 하단 facade re-export 목록에도 추가하라.

분할 구조 (v6.6.375~380):
- bg/broadcast.py       : 상태 송출 (emit_status_update, build_status_payloads, update_client_status)
- bg/export.py          : Google Sheets 비동기 쓰기 (main_crawler_export_callback 외)
- bg/crawler_singleton.py: CrawlerManager 싱글톤 (get_crawler, init_crawler_async, _kill_zombie_chrome)
- bg/contents.py        : 댓글·본문 크롤러 래퍼 (run_contents_crawler 외)
- bg/sub_runners.py     : 서브 크롤러 4종 실행 + 배치 (run_*_background 외)
- bg/scheduler.py       : 주기 작업 + 메인 크롤러 헬스체크 (start_crawler_with_delay 외)
"""

# ====================================================================
# facade re-export (외부 호출 경로 유지용)
# ====================================================================
# 레거시: 구 bg_tasks.py 가 `from common import get_yesterday_str, format_iso_to_24h` 로
# 이 두 함수를 재노출하던 관행을 유지 — 라우트/소켓 모듈이 `from web_lib.bg_tasks import
# get_yesterday_str` 를 사용 중.
from common import get_yesterday_str, format_iso_to_24h  # noqa: F401

from web_lib.bg.crawler_singleton import (  # noqa: F401
    _kill_zombie_chrome,
    get_crawler,
    init_crawler_async,
)
from web_lib.bg.broadcast import (  # noqa: F401
    build_status_payloads,
    emit_status_update,
    update_client_status,
)
from web_lib.bg.export import (  # noqa: F401
    main_crawler_export_callback,
    _debounced_export,
    export_to_gsheet_background,
    upload_config_to_gsheet_background,
)
from web_lib.bg.contents import (  # noqa: F401
    _send_crawler_crash_alert,
    on_comment_received,
    run_contents_crawler_supervised,
    run_contents_crawler,
    _content_log_func,
    _start_content_extractor_thread,
    check_content_extractor_health,
    _restart_content_extractor,
)
from web_lib.bg.sub_runners import (  # noqa: F401
    _calculate_summary,
    _populate_status_from_gsheet,
    _ensure_sub_crawler_status_populated,
    _ensure_odin_global_status_populated,
    _ensure_account_trade_status_populated,
    _ensure_jasa_games_status_populated,
    _ensure_tasa_games_status_populated,
    _run_crawler_generic,
    _update_summary_after_crawl,
    run_odin_global_background,
    run_account_trade_background,
    run_jasa_games_background,
    run_tasa_games_background,
    _trigger_batch_run,
)
from web_lib.bg.scheduler import (  # noqa: F401
    refresh_main_viewer_data,
    cleanup_visitor_status,
    start_crawler_with_delay,
    check_main_crawler_health,
    _restart_main_crawler,
)
