# Gemma4 프롬프트 저장소

## Source of Truth 규칙

| 항목 | 역할 |
|---|---|
| **Google Sheets `Config_LLM!A2`** | **런타임 소스 (유일한 진실)** |
| `gemma4_system_instruction.txt` | 레포 백업·이력용 스냅샷 |

`gemma4_summary_core.py:_load_system_instruction()` 은 **시트 A2 만** 읽는다. 이 폴더의 텍스트 파일은 런타임에 로드되지 않는다.

## 이 폴더가 존재하는 이유

1. **시트 사고 대비** — 조건부 서식 누적(PATTERN-19), 권한 실수, 실수 삭제 등으로 시트 장애 시 복구본
2. **git 이력** — 프롬프트 변경 이력 추적·롤백 가능
3. **코드 리뷰** — 프롬프트 수정도 diff 로 검토 가능
4. **세션 시작 시 참조** — 새 세션/AI 가 현재 프롬프트를 코드 접근만으로 읽을 수 있음 (시트 접근 없이)

## 동기화 규칙

시트에서 프롬프트를 수정하면 **이 파일도 같이 갱신**할 것. 원칙적으로 시트가 진실이므로 파일이 stale 해도 동작엔 영향 없으나, 위 4가지 이유가 무효화됨.

**갱신 절차**:
1. 시트 `Config_LLM!A2` 에서 신규 프롬프트 확인
2. `gemma4_system_instruction.txt` 덮어쓰기
3. `git diff` 로 변경 의도 확인
4. `[vX.X.X] DOCS: prompts 갱신 — <변경 요점>` 으로 커밋

## 관련 파일

- `ai_lib/gemma4_summary_core.py:91-106` — 실제 시트 로드 코드
- `web_app/PRODUCT_IMPROVEMENTS.md §1-3 Phase A` — 감성/severity 필드 추가 시 본 프롬프트 확장 예정
