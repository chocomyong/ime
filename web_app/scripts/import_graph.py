#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
web_app/ import 그래프 덤프 도구.

리팩토링 전/후 import DAG 변화 검증용.
- 실행: (web_app/ 에서) `python scripts/import_graph.py`
- 출력:
    - `docs/import_graph_before.json`  — 모듈별 의존성 JSON
    - `docs/import_graph_before.md`    — 사람 읽기용 목록
    - `docs/import_graph_before.dot`   — Graphviz DOT (렌더링: `dot -Tsvg ...`)

Phase 0 안전망 스크립트 (v6.6.365 도입). 현재는 `REFACTOR_NEXT.md §10 분할 작업 실행 레시피` 참조.
"""
import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # web_app/

# 분석 대상 경로 (Flask 웹앱 범위만 — crawlers/, ai_lib/, gui_lib/ 등은 범위 외)
TARGETS = [
    'app.py',
    'web_lib',
]

# 1st-party 로 인식할 최상위 모듈 이름 (그래프 노드 필터용)
INTERNAL_PREFIXES = (
    'web_lib',
    'crawlers',
    'utils',
    'gui_lib',
    'ai_lib',
    'sqlite_handler',
    'google_sheets_handler',
    'notification_manager',
    'common',
    'config',
    'logger_config',
    'content_extractor',
    'contents_crawler',
    'account_trade',
)


def extract_imports(py_file: Path):
    """한 파일의 import 명을 평탄하게 반환."""
    try:
        src = py_file.read_text(encoding='utf-8')
        tree = ast.parse(src, filename=str(py_file))
    except (SyntaxError, UnicodeDecodeError) as e:
        return {'__error__': f'{type(e).__name__}: {e}'}

    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module)
    return imports


def collect_files():
    files = []
    for target in TARGETS:
        path = ROOT / target
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(sorted(path.rglob('*.py')))
    return files


def to_graph_key(py_file: Path) -> str:
    return str(py_file.relative_to(ROOT)).replace('\\', '/')


def is_internal(name: str) -> bool:
    return name.startswith(INTERNAL_PREFIXES) or any(
        name == prefix or name.startswith(f'{prefix}.') for prefix in INTERNAL_PREFIXES
    )


def build():
    files = collect_files()
    print(f"[import_graph] 분석 대상 파일 수: {len(files)}")

    graph_all = {}
    graph_internal = {}

    for f in files:
        key = to_graph_key(f)
        result = extract_imports(f)
        if isinstance(result, dict) and '__error__' in result:
            graph_all[key] = {'error': result['__error__']}
            continue
        ordered = sorted(result)
        graph_all[key] = ordered
        graph_internal[key] = [m for m in ordered if is_internal(m)]

    return graph_all, graph_internal


def write_json(path: Path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def write_md(path: Path, graph_all, graph_internal):
    lines = ['# Import Graph (before refactoring)', '']
    lines.append(f'- 분석 대상: {", ".join(TARGETS)}')
    lines.append(f'- 모듈 수: {len(graph_all)}')
    lines.append('')
    lines.append('> **internal** 열 = imt_tools 내부 모듈만 필터링. DAG 검증은 이쪽만 보면 충분.')
    lines.append('')
    lines.append('## Internal Dependencies')
    lines.append('')
    for module, deps in sorted(graph_internal.items()):
        if not deps:
            continue
        lines.append(f'### `{module}`')
        lines.append('')
        for d in deps:
            lines.append(f'- `{d}`')
        lines.append('')

    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def write_dot(path: Path, graph_internal):
    lines = ['digraph import_graph {', '    rankdir=LR;',
             '    node [shape=box, fontname="Consolas", fontsize=9];', '']
    for module, deps in sorted(graph_internal.items()):
        mod_label = module.replace('"', '\\"')
        for d in deps:
            d_label = d.replace('"', '\\"')
            lines.append(f'    "{mod_label}" -> "{d_label}";')
    lines.append('}')
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main():
    graph_all, graph_internal = build()

    docs_dir = ROOT / 'docs'
    docs_dir.mkdir(exist_ok=True)

    out_json = docs_dir / 'import_graph_before.json'
    out_md = docs_dir / 'import_graph_before.md'
    out_dot = docs_dir / 'import_graph_before.dot'

    write_json(out_json, {
        'all': graph_all,
        'internal': graph_internal,
    })
    write_md(out_md, graph_all, graph_internal)
    write_dot(out_dot, graph_internal)

    print(f"[import_graph] 저장: {out_json}")
    print(f"[import_graph] 저장: {out_md}")
    print(f"[import_graph] 저장: {out_dot}  (렌더: `dot -Tsvg {out_dot} -o {out_dot.with_suffix('.svg')}`)")

    # DAG 위반 (자기 자신 -> 자기 자신) 간단 검사
    violations = []
    for module, deps in graph_internal.items():
        mod_base = module.split('/')[0].replace('.py', '')
        for d in deps:
            if d == mod_base:
                violations.append((module, d))
    if violations:
        print('\n[import_graph] WARNING: 자기 참조 의심')
        for m, d in violations:
            print(f'  {m} -> {d}')


if __name__ == '__main__':
    main()
