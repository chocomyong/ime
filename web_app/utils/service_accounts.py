# -*- coding: utf-8 -*-
"""
공통 서비스 계정 관리 모듈 (service_accounts.py)
Google Sheets API 429 쿼타 에러 방어를 위한 다중 서비스 계정 풀(Pool)

[v6.7.745] 키를 코드에 두지 않는다 - 계정마다 키 파일 하나(web_app/ 루트)를 읽는다.
  전에는 같은 키가 이 파일의 딕셔너리와 JSON 파일 두 곳에 있었다(정본 표: docs/GCP_ACCOUNTS.md).
  바꾸기 전 대조: 두 계정이 두 파일과 dict 단위로 같았다(2026-10-26).
  순서 = 풀 번호(#1 imt-ai-processor, #2 culsuckapp). 두 파일은 배포 묶음이 보내지 않는다
  (scripts/ime-deploy/apply.sh path_ok) - 운영 서버에 이미 있는 사본을 그대로 쓴다.
"""
import json
import logging
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_KEY_FILES = (
    "imt-ai-processor-915bcd4c6469.json",   # 계정 1: imt-ai-processor (기본 풀)
    "service_account.json",                 # 계정 2: culsuckapp (보조 풀)
)


def _load():
    log = logging.getLogger(__name__)
    out = []
    for name in _KEY_FILES:
        try:
            with open(os.path.join(_HERE, "..", name), encoding="utf-8") as f:
                out.append(json.load(f))
        except (OSError, ValueError) as e:
            log.error("[FAIL] 서비스 계정 키 파일을 못 읽었다: %s (%s)", name, type(e).__name__)
    if not out:
        log.error("[FAIL] 서비스 계정 0개 - 구글 시트 읽기·쓰기가 전부 실패한다")
    else:
        # 배포 뒤 확인 마커 - 키 파일은 배포 묶음 밖이라 "서버에 있다" 를 이 줄로만 볼 수 있다(값은 찍지 않는다)
        log.info("[BOOT] 서비스 계정 %d개 로드 (%s)", len(out), ", ".join(a.get("project_id", "?") for a in out))
    return out


SERVICE_ACCOUNTS = _load()
