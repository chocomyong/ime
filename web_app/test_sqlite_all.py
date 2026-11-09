# -*- coding: utf-8 -*-
"""SQLite 스키마 검증 게이트 (web_app/CLAUDE.md 'DB 검증' / .rules/verification_checklist.md).

[v6.7.461] **판정 도구로 승격.** 구 버전은 어떤 경로로도 실패할 수 없었다 -
  DB 부재는 안내 1줄 후 return, 테이블 부재/쿼리 예외는 outer except 가 삼키고, 정상도 종료.
  세 갈래 전부 exit 0 이라 "PASS 확인" 이라는 체크리스트 문구가 가리킬 PASS 가 정의된 적이 없었다.
  실제로 CHANGELOG v6.7.70 에 이 스크립트를 근거로 한 허위 검증 보고가 박제돼 있다
  ("sqlite_master 조회 시 검출" - 그 쿼리는 결과를 fetch 한 적이 없었다. 다음 버전에서 손으로
  테이블을 추가해야 했다).

수정 3종:
  1. 경로 - `os.getcwd()` 의존 제거. `DB_DIR` 환경변수 + 파일 위치 기준으로 `sqlite_handler` 와
     같은 규칙을 쓴다(컨테이너 `DB_DIR=/app/data` vs 로컬 web_app 루트). 구 코드는 컨테이너 안에서
     돌리면 `/app/crawled_data.sqlite` 를 찾아 "DB 없음" 을 냈는데, 그것이 "경로가 틀렸다" 인지
     "DB 가 날아갔다" 인지 출력으로 구분되지 않았다.
  2. 기대 목록 - 하드코딩 12개를 버리고 `sqlite_handler.SCHEMA_TABLES`(20종) 를 읽는다.
     CREATE TABLE 을 추가하는 커밋이 게이트를 자동으로 넓힌다.
  3. 판정 - 실패를 모아 exit 1. **DB 부재는 SKIP(exit 0)** 으로 따로 뺀다 - `crawled_data.sqlite` 는
     gitignore 라 모든 fresh clone 에 없고, 이걸 FAIL 로 만들면 정상 clone 에서 매번 빨간불이 떠
     게이트가 즉시 무시된다(그러면 고친 의미가 없다). 대신 해석한 경로를 함께 출력해 '경로 착오'와
     '진짜 유실'을 사람이 구분할 수 있게 한다.

판정 토큰은 `ALL TESTS PASSED` - `.rules/evaluator_assertions.md` 가 정의한 문자열에 맞춘다
  (구 스크립트는 이 문자열을 한 번도 출력하지 않아 ASSERT-02 가 판정 불능이었다).

사용:
  python test_sqlite_all.py; echo $?     # 0 = PASS 또는 SKIP, 1 = FAIL
  DB_DIR=/app/data python test_sqlite_all.py
"""
import ast
import os
import sqlite3
import sys
from datetime import datetime

_ROOT = os.path.dirname(os.path.abspath(__file__))
# sqlite_handler.__init__ 과 동일한 해석 규칙 (DB_DIR override, 기본은 web_app 루트).
# 단 handler 의 os.makedirs 는 따라 하지 않는다 - 검증 도구가 경로를 만들면 부재를 은폐한다.
DB_PATH = os.path.join(os.environ.get('DB_DIR', _ROOT), 'crawled_data.sqlite')
_HANDLER = os.path.join(_ROOT, 'utils', 'sqlite_handler.py')


def _load_schema_tables():
    """`utils/sqlite_handler.py` 의 SCHEMA_TABLES 를 **import 없이** AST 로 읽는다.

    import 하면 안 되는 이유 (첫 실행에서 실측): 그 모듈은 맨 끝에
    `db_handler = SQLiteHandler()` 로 **모듈 레벨에서 싱글톤을 만든다**. 즉 import 하는 순간
    `__init__` 이 돌아 **DB 파일과 테이블이 생성된다** - 검증 도구가 자기가 점검할 대상을
    만들어버리는 셈이고, 그러면 'DB 부재'를 영원히 관측할 수 없다(부재를 은폐한다).
    실제로 이 스크립트의 첫 판(import 방식)이 fresh clone 에 없던 crawled_data.sqlite 를
    새로 만들어 냈다. AST 는 코드를 실행하지 않으므로 부작용이 0이다.
    (레포 관행과도 정합 - portfolio/07 Stage 1 이 'AST 로 실제 코드를 읽는' 방식을 쓴다.)
    """
    tree = ast.parse(open(_HANDLER, encoding='utf-8').read())
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if isinstance(tgt, ast.Name) and tgt.id == 'SCHEMA_TABLES':
                return tuple(ast.literal_eval(node.value))
    raise ValueError('SCHEMA_TABLES 를 찾지 못했다 (utils/sqlite_handler.py 모듈 레벨에 있어야 한다)')


try:
    SCHEMA_TABLES = _load_schema_tables()
    _IMPORT_ERROR = None
except Exception as _imp_err:  # 목록을 못 읽는 것 자체가 FAIL 신호
    SCHEMA_TABLES = None
    _IMPORT_ERROR = _imp_err


_SKIPPED = False


def check_sqlite_data():
    """실패 목록을 반환한다 (빈 리스트 = PASS 또는 SKIP - `_SKIPPED` 로 구분)."""
    failures = []
    print(f"=== SQLite 스키마 점검 ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')}) ===")
    print(f"DB 경로: {DB_PATH}  (DB_DIR={os.environ.get('DB_DIR', '<미설정 - web_app 루트>')})")

    if SCHEMA_TABLES is None:
        failures.append(f"기대 목록 import 실패 - utils/sqlite_handler.py 확인 필요: {_IMPORT_ERROR}")
        return failures

    if not os.path.exists(DB_PATH):
        # FAIL 이 아니라 SKIP - 이유는 모듈 docstring 3번 참조.
        # **판정 토큰은 PASS 와 다르게 찍는다** - 아무것도 검사하지 않았는데 'ALL TESTS PASSED' 를
        # 출력하면 이 게이트가 고치려던 결함(검사 안 하고 통과 선언)을 그대로 재생산한다.
        globals()['_SKIPPED'] = True
        print("[SKIP] 이 경로에 DB 파일이 없다 - 스키마 점검을 건너뛴다.")
        print("       crawled_data.sqlite 는 gitignore 이므로 **fresh clone 에서는 정상**이다.")
        print("       서버/컨테이너에서 이 메시지가 나오면 그건 진짜 신호다 (DB_DIR 확인 → 유실 조사).")
        return failures

    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # 실재 테이블을 **실제로 fetch** 한다 (구 코드는 execute 만 하고 결과를 버렸다).
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
        actual = {row['name'] for row in cur.fetchall()}

        missing = [t for t in SCHEMA_TABLES if t not in actual]
        extra = sorted(actual - set(SCHEMA_TABLES))

        print(f"\n기대 {len(SCHEMA_TABLES)}종 / 실재 {len(actual)}종")
        if missing:
            failures.append(f"테이블 부재 {len(missing)}종: {', '.join(missing)}")
        if extra:
            # FAIL 아님 - 목록 갱신 누락 신호로만 알린다 (마이그레이션 잔재일 수도 있다).
            print(f"[WARN] SCHEMA_TABLES 에 없는 테이블 {len(extra)}종: {', '.join(extra)}")
            print("       신규 테이블이면 utils/sqlite_handler.py 의 SCHEMA_TABLES 에 추가할 것.")
        print("-" * 60)

        # 각 테이블 건수 + 샘플. 하나가 깨져도 나머지를 다 본다 (구 코드는 첫 예외에서 중단).
        for table in SCHEMA_TABLES:
            if table not in actual:
                print(f"[{table}] - FAIL: 테이블 없음")
                continue
            try:
                cur.execute(f"SELECT COUNT(*) as count FROM {table}")
                count = cur.fetchone()['count']
                print(f"[{table}] - {count}건")
                if count > 0:
                    cur.execute(f"SELECT * FROM {table} LIMIT 1")
                    sample = dict(cur.fetchone())
                    for key, val in sample.items():
                        if isinstance(val, str) and len(val) > 50:
                            sample[key] = val[:50] + "..."
                    print(f"  - 샘플: {sample}")
            except Exception as e:
                failures.append(f"{table} 조회 실패: {e}")
                print(f"[{table}] - FAIL: {e}")

    except Exception as e:
        failures.append(f"DB 연결/조회 실패: {e}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    return failures


if __name__ == "__main__":
    _failures = check_sqlite_data()
    print("-" * 60)
    if _failures:
        print(f"FAIL ({len(_failures)}건):")
        for f in _failures:
            print(f"  - {f}")
        sys.exit(1)
    if _SKIPPED:
        print("SKIPPED - DB 파일이 없어 스키마를 검사하지 않았다 (PASS 아님)")
        sys.exit(0)
    print("ALL TESTS PASSED")
    sys.exit(0)
