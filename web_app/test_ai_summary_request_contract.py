# -*- coding: utf-8 -*-
"""수동 AI 요약 요청(`POST /api/ai-summary/request`) 계약 스냅샷 검사.

왜 있나: `post_ai_summary_request` 를 작은 함수들로 분해하는 리팩토링의 **동작 불변**을 고정한다.
이 라우트는 권한 가드 · 입력 검증 · 예산 쿼타 · Gemini 2차 강제 호출 · UPDATE/INSERT 분기 · 라벨
적재 · 캐시 무효화가 한 흐름에 묶여 있어, 분해가 조용히 한 분기를 빠뜨리거나 순서를 바꿔도 import
통과 · 크래시 부재로는 안 보인다(PATTERN-63/66). 그래서 **DB·Gemini 코어를 가짜로 바꿔 끼우고**
실제 라우트를 태워, 시나리오별 (응답 status/json + 외부 호출 트레이스)를 스냅샷과 비교한다.

라이브 키·쿼타를 절대 안 태운다(PATTERN-80): `web_lib.ai_gemma4_worker._get_core` 를 가짜로 덮어
실제 Gemini 호출 경로에 진입하지 않는다. DB 도 전부 가짜라 운영 데이터에 안 쓴다.

**라이브 컨테이너에서 실행 금지** - import app 이 운영 sqlite 에 초기화를 건드린다(개발 머신·CI 전용).

사용:
    IMT_DISABLE_BOOTSTRAP=1 python test_ai_summary_request_contract.py            # 스냅샷과 비교
    IMT_DISABLE_BOOTSTRAP=1 python test_ai_summary_request_contract.py --update   # 스냅샷 갱신

분기를 추가·삭제·순서 변경하면 FAIL 한다 - 의도한 변경이면 --update 로 갱신하고 그 diff 를 눈으로
확인할 것(그 diff 가 곧 "무엇이 바뀌었나" 의 기록이다).
"""
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

os.environ.setdefault('IMT_DISABLE_BOOTSTRAP', '1')  # ASSERT-01 - 크롤러/스케줄러 실기동 차단
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)
SNAPSHOT = os.path.join(HERE, 'tests', 'ai_summary_request_snapshot.json')
KST = ZoneInfo('Asia/Seoul')

import app as appmod  # noqa: E402
import sqlite_handler  # noqa: E402
import web_lib.ai_gemma4_worker as _worker  # noqa: E402
from web_lib.routes import analysis as ana  # noqa: E402

URL = '/api/ai-summary/request'
FEEDBACK_ID = 777  # 가짜 save_ai_feedback 가 돌려주는 고정 id (응답 결정성)

# 공통 픽스처 -------------------------------------------------------------
POST_OK = {
    'game': 'POE2', 'board_name': '자유게시판', 'title': '테스트 제목',
    'content': '본문 내용입니다.', 'source': 'DC',
    'timestamp': '2026-09-30 12:00:00', 'date': '2026-09-30',
    'detected_keywords': 'kw1,kw2', 'is_super_keyword': 0,
}
POST_EMPTY = dict(POST_OK, content='')            # -> state no_content
POST_BRACKET = dict(POST_OK, content='[본문 없음]')  # -> state no_content
AI_SUMMARIZED = {'summary': '기존 요약문', 'sentiment': 'positive', 'severity': 'warning'}
AI_EXCLUDED = {'summary': '분석 제외', 'sentiment': 'neutral', 'severity': 'info'}


class FakeDB:
    """sqlite_handler.db_handler 대역 - 호출을 recorder 에 남기고 설정값을 돌려준다."""
    def __init__(self, rec, *, post=None, ai_rows=(None,), update_result=True,
                 save_ai_analysis_result=True):
        self._rec = rec
        self._post = post
        self._ai_rows = list(ai_rows)
        self._ai_idx = 0
        self._update_result = update_result
        self._save_ai_analysis_result = save_ai_analysis_result

    def get_post_by_link(self, link):
        self._rec.append(['db.get_post_by_link', link])
        return self._post

    def get_ai_analysis_by_link(self, link):
        r = self._ai_rows[self._ai_idx] if self._ai_idx < len(self._ai_rows) else None
        self._ai_idx += 1
        self._rec.append(['db.get_ai_analysis_by_link', link])
        return r

    def update_ai_analysis_for_link(self, link, summary, sentiment, severity):
        self._rec.append(['db.update_ai_analysis_for_link', link, summary, sentiment, severity])
        return self._update_result

    def save_ai_analysis(self, records):
        self._rec.append(['db.save_ai_analysis', records])
        return self._save_ai_analysis_result

    def save_ai_feedback(self, record):
        self._rec.append(['db.save_ai_feedback', record])
        return FEEDBACK_ID


class FakeCore:
    """Gemma4SummaryCore 대역. mutate(article) 로 시나리오별 요약 결과를 심는다."""
    system_instruction = 'sys-instr'

    def __init__(self, rec, mutate=None, raise_msg=None):
        self._rec = rec
        self._mutate = mutate
        self._raise_msg = raise_msg

    def _generate_gemini_summaries(self, articles, system_instruction, arg3,
                                   bypass_pace=False, force_summary=False):
        self._rec.append([
            'core._generate_gemini_summaries',
            {'articles_in': [dict(a) for a in articles],
             'system_instruction': system_instruction, 'arg3': arg3,
             'bypass_pace': bypass_pace, 'force_summary': force_summary},
        ])
        if self._raise_msg is not None:
            raise RuntimeError(self._raise_msg)
        if self._mutate is not None:
            self._mutate(articles[0])


def _reset_quota_fresh():
    ana._manual_quota.update({'hour_key': None, 'hour_calls': 0, 'day_key': None, 'day_calls': 0})


def _set_quota_exhausted():
    now = datetime.now(KST)
    ana._manual_quota.update({
        'hour_key': (now.date(), now.hour), 'hour_calls': ana._MANUAL_SUMMARY_HOURLY_CAP,
        'day_key': now.date(), 'day_calls': ana._MANUAL_SUMMARY_DAILY_CAP,
    })


def _mk_summary(text, sentiment=None, severity=None):
    def _m(article):
        article['summary'] = text
        if sentiment is not None:
            article['sentiment'] = sentiment
        if severity is not None:
            article['severity'] = severity
    return _m


# 시나리오 정의 - 라우트 코드에서 발견되는 모든 분기 -----------------------
SCENARIOS = [
    # (name, role, body, quota, db_kwargs, core_spec)
    #   core_spec: None(코어 미도달) / {'mode':'load_error'|'none'|'obj', ...}
    ('anon_forbidden', 'anon', {'link': 'http://x/1'}, 'fresh', {}, None),
    ('manager_forbidden', 'manager', {'link': 'http://x/1'}, 'fresh', {}, None),
    ('missing_link', 'admin', {}, 'fresh', {}, None),
    ('blank_link', 'admin', {'link': '   '}, 'fresh', {}, None),
    ('post_not_found', 'admin', {'link': 'http://x/1'}, 'fresh',
     {'post': None}, None),
    ('no_content_empty', 'admin', {'link': 'http://x/1'}, 'fresh',
     {'post': POST_EMPTY, 'ai_rows': (None,)}, None),
    ('no_content_bracket', 'admin', {'link': 'http://x/1'}, 'fresh',
     {'post': POST_BRACKET, 'ai_rows': (AI_SUMMARIZED,)}, None),
    ('quota_exhausted', 'admin', {'link': 'http://x/1'}, 'exhausted',
     {'post': POST_OK, 'ai_rows': (AI_SUMMARIZED,)}, None),
    ('core_load_error', 'admin', {'link': 'http://x/1'}, 'fresh',
     {'post': POST_OK, 'ai_rows': (AI_SUMMARIZED,)}, {'mode': 'load_error'}),
    ('core_none', 'admin', {'link': 'http://x/1'}, 'fresh',
     {'post': POST_OK, 'ai_rows': (AI_SUMMARIZED,)}, {'mode': 'none'}),
    ('generate_raises', 'admin', {'link': 'http://x/1'}, 'fresh',
     {'post': POST_OK, 'ai_rows': (AI_SUMMARIZED,)},
     {'mode': 'obj', 'raise_msg': 'gen boom'}),
    ('budget_placeholder', 'admin', {'link': 'http://x/1'}, 'fresh',
     {'post': POST_OK, 'ai_rows': (AI_SUMMARIZED,)},
     {'mode': 'obj', 'mutate': _mk_summary('(예산 대기)')}),
    ('success_update', 'admin', {'link': 'http://x/1'}, 'fresh',
     {'post': POST_OK, 'ai_rows': (AI_SUMMARIZED,), 'update_result': True},
     {'mode': 'obj', 'mutate': _mk_summary('새 요약문', 'negative', 'critical')}),
    ('success_insert', 'admin', {'link': 'http://x/1'}, 'fresh',
     {'post': POST_OK, 'ai_rows': (None, AI_SUMMARIZED), 'save_ai_analysis_result': True},
     {'mode': 'obj', 'mutate': _mk_summary('신규 요약문')}),  # sentiment/severity 미설정 -> 기본값 분기
    ('success_still_excluded', 'admin', {'link': 'http://x/1'}, 'fresh',
     {'post': POST_OK, 'ai_rows': (AI_EXCLUDED,), 'update_result': True},
     {'mode': 'obj', 'mutate': _mk_summary('분석 제외 (사유: 반복 문의)')}),
    ('save_fail_update', 'admin', {'link': 'http://x/1'}, 'fresh',
     {'post': POST_OK, 'ai_rows': (AI_SUMMARIZED,), 'update_result': False},
     {'mode': 'obj', 'mutate': _mk_summary('새 요약문')}),
    ('save_fail_insert', 'admin', {'link': 'http://x/1'}, 'fresh',
     {'post': POST_OK, 'ai_rows': (None, None)},
     {'mode': 'obj', 'mutate': _mk_summary('신규 요약문')}),
]


def _install_core(rec, core_spec):
    """core_spec 에 맞춰 _get_core 를 덮는다. 반환: None(코어 미도달 시엔 안 쓰임)."""
    if core_spec is None:
        return lambda: (_ for _ in ()).throw(AssertionError('_get_core 가 불리면 안 되는 시나리오'))
    mode = core_spec['mode']
    if mode == 'load_error':
        def _gc():
            rec.append(['worker._get_core', 'raise'])
            raise RuntimeError('load boom')
        return _gc
    if mode == 'none':
        def _gc():
            rec.append(['worker._get_core', 'None'])
            return None
        return _gc
    core = FakeCore(rec, mutate=core_spec.get('mutate'), raise_msg=core_spec.get('raise_msg'))

    def _gc():
        rec.append(['worker._get_core', 'obj'])
        return core
    return _gc


def run_scenario(cfg):
    name, role, body, quota, db_kwargs, core_spec = cfg
    rec = []
    fake_db = FakeDB(rec, **db_kwargs)
    fake_gc = _install_core(rec, core_spec)

    saved_db = sqlite_handler.db_handler
    saved_gc = _worker._get_core
    sqlite_handler.db_handler = fake_db
    _worker._get_core = fake_gc
    if quota == 'exhausted':
        _set_quota_exhausted()
    else:
        _reset_quota_fresh()
    try:
        c = appmod.app.test_client()
        with c.session_transaction() as s:
            if role == 'admin':
                s['role'], s['is_admin'] = 'admin', True
            elif role == 'manager':
                s['role'], s['is_admin'] = 'manager', False
        resp = c.post(URL, json=body, headers={
            'X-Forwarded-For': 'test-ip', 'User-Agent': 'contract-harness'})
        try:
            payload = resp.get_json()
        except Exception:
            payload = {'_raw': resp.get_data(as_text=True)}
    finally:
        sqlite_handler.db_handler = saved_db
        _worker._get_core = saved_gc
    return {'status': resp.status_code, 'json': payload, 'calls': rec}


def collect():
    appmod.app.config['TESTING'] = True
    return {cfg[0]: run_scenario(cfg) for cfg in SCENARIOS}


def main():
    got = collect()
    text = json.dumps(got, ensure_ascii=False, indent=1, sort_keys=True, default=str) + "\n"
    if '--update' in sys.argv:
        os.makedirs(os.path.dirname(SNAPSHOT), exist_ok=True)
        with open(SNAPSHOT, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        print(f"[UPDATED] {SNAPSHOT} (scenarios={len(got)})")
        return 0
    if not os.path.exists(SNAPSHOT):
        print(f"[FAIL] 스냅샷 없음 - 먼저 --update 로 생성: {SNAPSHOT}")
        return 1
    with open(SNAPSHOT, encoding='utf-8') as f:
        want = f.read()
    if text == want:
        print(f"[PASS] 요약 요청 계약 동일 (scenarios={len(got)})")
        return 0
    import difflib
    print("[FAIL] 요약 요청 계약이 스냅샷과 다르다:")
    for line in difflib.unified_diff(want.splitlines(), text.splitlines(),
                                     'snapshot', 'now', lineterm='', n=1):
        print(line)
    return 1


if __name__ == '__main__':
    sys.exit(main())
