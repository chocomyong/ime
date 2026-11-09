# -*- coding: utf-8 -*-
"""routes.articles — 게시물·댓글 조회 엔드포인트.

- `/api/articles/by-game`        : 게임별 게시물 (웹 enrich 포함)
- `/api/articles/by-community`   : 카페/디시 커뮤니티별 게시물
- `/api/articles/all`            : 최근 전체 게시물
- `/api/comments`                : 댓글 (메모리 캐시 + SQLite 폴백)

Phase 1 3차 분할 대상. 기존 `web_lib/web_routes.py` 에서 이관.
[v6.7.731] 호출 0(레포 전체 grep - webview_app/chrome_extension/jira_register 포함) 확인 후
`/api/articles/new`, `/api/articles/keyword-alerts`, `/api/articles/all/ack`(무가드 쓰기) 3종 삭제.

`/api/extension/super-alerts` 는 articles 에서 파생되지만 extension 관련 그룹이라
향후 `routes/extension.py` 이관 예정 — 본 커밋에서는 이동하지 않음.
"""
import logging
import time

from flask import jsonify, request

import web_lib.globals as gl
from web_lib.bg_tasks import get_crawler
from web_lib.routes import api_bp
from web_lib.routes._helpers import _make_ttl_cache

logger = logging.getLogger(__name__)

# [v6.7.169] 표시 상한 — crawler MAX_MEMORY_ARTICLES(10000) 와 동일. 사용자 요청: 피드/댓글/게임별/커뮤니티
#   모든 뷰의 글 개수 제한을 풀되, 메모리 보유분(최대 10000) 까지만 표시 (게임 1개만 켜도 10000 도달 가능).
_MAX_VIEW = 10000


# [v6.6.499] /api/articles/all 응답 캐시 — 컨테이너 CPU 만렙 시 1.22MB JSON 재직렬화 비용 절감.
# 5초 TTL = 클라이언트 폴링 사이클(보통 5~10s) 안에서 동일 응답 재사용. 신규 글 지연은 최대 5초.
_feed_cache, _feed_cache_lock = _make_ttl_cache(5.0)
# [v6.6.500] /api/articles/by-game 응답 캐시 — 동일 패턴. 측정값 0.136s × CPU 폭증 시 1~3초로 늘어 LCP 영향.
_by_game_cache, _by_game_cache_lock = _make_ttl_cache(5.0)
# [v6.7.61] /api/articles/by-community 응답 캐시 — by-game 동일 패턴. 캐시 미적용 hot 라우트.
_by_community_cache, _by_community_cache_lock = _make_ttl_cache(5.0)


def invalidate_article_caches():
    """글 cycle 완료 시 모든 article 캐시 강제 만료. bg 모듈(contents/scheduler)에서 호출."""
    for _c in (_feed_cache, _by_game_cache, _by_community_cache):
        _c['ts'] = 0.0


@api_bp.route('/api/articles/by-game', methods=['GET'])
def get_articles_by_game():
    now = time.time()
    cached = _by_game_cache.get('data')
    if cached is not None and (now - _by_game_cache['ts']) < _by_game_cache['ttl']:
        return jsonify(cached)
    with _by_game_cache_lock:
        now = time.time()
        cached = _by_game_cache.get('data')
        if cached is not None and (now - _by_game_cache['ts']) < _by_game_cache['ttl']:
            return jsonify(cached)
        crawler = get_crawler()
        # [v6.7.169] PER_GAME_LIMIT(10) override — 메모리 보유분 전량(<=10000)을 클라로 전송, 표시 개수는
        #   games-tab.js 의 displayLimit(사용자 limits[게임]) 이 제어. 라우트가 10/소스 로 막던 구조 해소.
        data = crawler.get_articles_by_game(limit=_MAX_VIEW)
        if hasattr(crawler, '_enrich_game_articles_for_web'):
            data = crawler._enrich_game_articles_for_web(data)
        _by_game_cache['data'] = data
        _by_game_cache['ts'] = now
    return jsonify(data)


@api_bp.route('/api/articles/by-community', methods=['GET'])
def get_articles_by_community():
    now = time.time()
    cached = _by_community_cache.get('data')
    if cached is not None and (now - _by_community_cache['ts']) < _by_community_cache['ttl']:
        return jsonify(cached)
    with _by_community_cache_lock:
        now = time.time()
        cached = _by_community_cache.get('data')
        if cached is not None and (now - _by_community_cache['ts']) < _by_community_cache['ttl']:
            return jsonify(cached)
        crawler = get_crawler()
        # [v6.7.169] 게임별 PER_GAME_LIMIT(10) override — 전량(<=10000) 노출. community-tab.js 의
        #   communityLimits(사용자 limits[게임]) 가 표시 제어. 라우트 내부 10/게임 캡 해소.
        data = crawler.get_articles_by_community(per_game_limit=_MAX_VIEW)
        if hasattr(crawler, '_enrich_articles_for_web'):
            data = {'daum': crawler._enrich_articles_for_web(data.get('daum', [])), 'dc': crawler._enrich_articles_for_web(data.get('dc', []))}
        _by_community_cache['data'] = data
        _by_community_cache['ts'] = now
    return jsonify(data)


def _mark_judge_pending(rows):
    """[v6.7.738] 각 글에 `judge_pending`(bool)을 싣는다 - 화면 '판정 대기' 표식의 유일한 근거.

    판정 잡이 실제로 판정할 글인지는 잡의 후보 술어로만 정한다(post_judge.pending_links).
    종전엔 클라가 "공식이고 판정 행 없음" 으로 정해, 잡이 영영 안 보는 공지·소식·패치노트·이벤트
    게시판과 7일 지난 글에 점이 상시 찍혔다. 두 조회 경로(메모리 캐시 / SQLite 직결)가 모두 부른다.
    조회가 실패하면 전부 False(점 없음) - 피드 자체를 500 으로 떨구지 않는다(PATTERN-66).
    """
    pend = set()
    try:
        from sqlite_handler import db_handler as _db
        from web_lib.bg import post_judge
        off = [r.get('link') for r in rows
               if r.get('link') and str(r.get('source_community') or r.get('source') or '').upper() == 'OFFICIAL_WEB']
        if off:
            pend = post_judge.pending_links(_db._get_connection(), off)
    except Exception as e:
        # 피드를 500 으로 떨구지 않으려고 넓게 받는다 - 대신 스택을 남겨 DB 일시 오류와 코드 결함을 가른다(PATTERN-66).
        logger.exception(f"[articles/all] 판정 대기 조회 실패 - 표식 없이 반환: {e}")
    for r in rows:
        r['judge_pending'] = r.get('link') in pend


@api_bp.route('/api/articles/all', methods=['GET'])
def get_all_articles():
    # [v6.7.76] LCP 29s 회귀 해소 — 서버 응답 cap.
    # v6.6.335 의 "무제한 반환" 정책은 메모리가 stale (138건) 일 때 무해했으나,
    # v6.7.74 가 memory ↔ SQLite sync 를 정상화하면서 메모리가 수천~만 건으로 팽창 →
    # 1.22MB+ JSON 직렬화 / 전송 / 파싱 / 렌더가 모두 비대화 → LCP 29s.
    # 기본 500 cap. 클라이언트 limits 기본 50/게임 × 게임 수 < 500 이라 UI 영향 0.
    # 풀 페이로드 필요 시 `?limit=0` 으로 무제한 옵트인.
    DEFAULT_LIMIT = 500
    try:
        _lim = int(request.args.get('limit', DEFAULT_LIMIT))
    except Exception:
        _lim = DEFAULT_LIMIT
    if _lim < 0:
        _lim = DEFAULT_LIMIT

    # [v6.7.172] ?games=a,b 지정 시 SQLite 직접 조회 (해당 게임 전량). 글로벌 메모리(최신 10000 합본)는
    #   활발한 게임(우마무스메 등)이 최신 타임라인을 점령해 busy 게임(POE2 등)의 과거 글을 못 담음 →
    #   필터링한 게임만 WHERE game IN (...) 으로 직접 가져와 그 게임 전량(보관 7일 / 최대 _MAX_VIEW) 노출.
    #   캐시/메모리 우회 (필터 디버그 경로). 클라(feed-tab.js)는 가시 게임이 전체의 부분집합일 때만 games 전달.
    # [v6.7.664] source / flagged 도 같은 SQLite 직결 규약에 합류시킨다.
    #   캐시(_feed_cache)는 **파라미터를 무시하는 전역 캐시**라 여기에 파라미터를 태우면 조용히 틀린
    #   데이터가 나간다(PATTERN-55). games 가 이미 캐시를 우회하고 있으므로 같은 문을 쓴다.
    #   - source: DC 가 유입의 88% 라 클라 필터로는 공식이 표본에서 사라진다(13일 실측).
    #   - flagged: 제재 판정(post_verdicts)은 DB 에만 있어 메모리 캐시로는 조회 자체가 불가능하다.
    _SRC_ALLOWED = ('OFFICIAL_WEB', 'NAVER', 'DAUM', 'DC')
    _games_raw = (request.args.get('games') or '').strip()
    _src = (request.args.get('source') or '').strip().upper()
    if _src and _src not in _SRC_ALLOWED:
        _src = ''   # 미지의 값은 "필터 없음" 으로 떨어뜨린다 - 오타가 0건 화면이 되지 않게.
    _flagged = (request.args.get('flagged') or '').strip() in ('1', 'true', 'True')
    if _games_raw or _src or _flagged:
        _games = [g.strip() for g in _games_raw.split(',') if g.strip()]
        if _games or _src or _flagged:
            _g_lim = _lim if (0 < _lim <= _MAX_VIEW) else _MAX_VIEW
            try:
                from sqlite_handler import db_handler as _db
                rows = _db.get_latest_posts(limit=_g_lim, games=(_games or None),
                                            source=(_src or None), flagged=_flagged)
                crawler = get_crawler()
                if hasattr(crawler, '_enrich_articles_for_web'):
                    rows = crawler._enrich_articles_for_web(rows)
                # SQLite detected_keywords 는 콤마 문자열 → 리스트 정규화 (피드 하이라이트 호환).
                for r in rows:
                    kw = r.get('detected_keywords')
                    if isinstance(kw, str):
                        r['detected_keywords'] = [k.strip() for k in kw.split(',') if k.strip()]
                    elif not isinstance(kw, list):
                        r['detected_keywords'] = []
                _mark_judge_pending(rows)
                return jsonify(rows)
            except Exception as e:
                logger.error(f"[articles/all games={_games} source={_src or '-'} flagged={int(_flagged)}] SQLite 조회 실패: {e}")
                # [v6.7.489] 실패를 200+[] 로 위장하지 않는다 — 클라(feed-tab.js)는 resp.ok 만 보므로
                #   200 이면 조회 실패를 "글이 0건" 으로 받아 _feedTotalCache 를 [] 로 덮는다.
                #   v6.7.95 가 만든 "일시 실패 시 캐시 보존 + 2.5s 재시도" 방어(PATTERN-32)가
                #   !resp.ok / throw 에만 걸려 있어 이 경로에서 통째로 우회됐다. 500 이면 그 방어가 붙는다.
                return jsonify({'error': 'sqlite_query_failed'}), 500

    now = time.time()
    cached = _feed_cache.get('data')
    if cached is not None and (now - _feed_cache['ts']) < _feed_cache['ttl']:
        return jsonify(cached if _lim == 0 else cached[:_lim])
    with _feed_cache_lock:
        now = time.time()
        cached = _feed_cache.get('data')
        if cached is not None and (now - _feed_cache['ts']) < _feed_cache['ttl']:
            return jsonify(cached if _lim == 0 else cached[:_lim])
        crawler = get_crawler()
        data = crawler.get_all_latest_articles()
        if hasattr(crawler, '_enrich_articles_for_web'):
            data = crawler._enrich_articles_for_web(data)
        # [v6.7.738] 기본 보기(메모리 캐시 경로)에도 제재 판정을 붙인다 - 위 SQLite 직결 경로
        #   (games/source/flagged)의 LEFT JOIN 과 **같은 필드·같은 의미**로: verdict_kind /
        #   verdict_target / verdict_at, 판정이 없으면 None(= LEFT JOIN 의 NULL). v6.7.664 가
        #   문서화한 비대칭(판정이 SQLite 경로에만 실려 기본 보기엔 제재 칩·대기 표식이 안 뜸)을 해소.
        #   조회는 **이 응답에 실린 공식 게시물 link 로만** 한정한다 - post_verdicts 는 공식만 담고
        #   (post_judge), link 이 PK 라 IN 조회가 자동 인덱스 SEARCH 다(SCAN 금지, PATTERN-70).
        #   클라(feed-tab.js)는 공식 게시물에만 판정/대기 표식을 그리므로(src 가드) 공식 link 로
        #   좁혀도 화면 의미가 동일하고, IN 변수 수가 공식 비율(~1.5%)로 작아진다.
        #   판정 조인 실패가 피드 전체를 500 으로 떨구지 않게 분리한 except 로 감싼다(PATTERN-66).
        try:
            from sqlite_handler import db_handler as _db
            _off_links = [a.get('link') for a in data
                          if a.get('link') and str(a.get('source_community') or a.get('source') or '').upper() == 'OFFICIAL_WEB']
            _vmap = _db.get_verdicts_for_links(_off_links) if _off_links else {}
            for a in data:
                _v = _vmap.get(a.get('link'))
                a['verdict_kind'] = _v['verdict_kind'] if _v else None
                a['verdict_target'] = _v['verdict_target'] if _v else None
                a['verdict_at'] = _v['verdict_at'] if _v else None
        except Exception as e:
            logger.exception(f"[articles/all] 기본 보기 판정 조인 실패 - 판정 없이 반환: {e}")
        _mark_judge_pending(data)
        _feed_cache['data'] = data
        _feed_cache['ts'] = now
    return jsonify(data if _lim == 0 else data[:_lim])


@api_bp.route('/api/comments', methods=['GET'])
def get_comments():
    """댓글 데이터를 메모리 캐시에서 읽어 반환합니다. 캐시가 비어있으면 SQLite 직접 조회."""
    try:
        # [v6.6.279] 스냅샷 복사로 순회 중 RuntimeError 방지
        with gl._comment_lock:
            records = list(gl.comment_data_status.get('latest_results', []))
        _comment_limit = 500  # [v6.6.238] 메모리 전송 풀 크기 고정
        limit = request.args.get('limit', _comment_limit, type=int)
        # [v6.7.169] 댓글 표시 상한 _MAX_VIEW(10000) 클램프 (사용자 요청). 0/음수/초과 요청 모두 10000 으로.
        if limit <= 0 or limit > _MAX_VIEW:
            limit = _MAX_VIEW

        # [FIX] 메모리 캐시가 비어있으면 SQLite에서 직접 조회 (앱 시작 직후 레이스 컨디션 방어)
        if not records:
            try:
                from sqlite_handler import db_handler as _db
                records = _db.get_comments(limit=max(limit, _comment_limit))
                if records:
                    for r in records:
                        lnk = str(r.get('link', '') or r.get('comment_link', ''))
                        r['source_community'] = 'DAUM' if 'cafe.daum.net' in lnk else 'DC'
                    # [v6.6.279] key 재할당 → atomic swap 으로 변경
                    # [v6.7.167] 메모리 캐시는 _comment_limit(500) 으로 cap — 비대 리스트 swap/emit 방지.
                    with gl._comment_lock:
                        gl.comment_data_status = {
                            'latest_results': records[:_comment_limit],
                            'last_updated': gl.comment_data_status.get('last_updated'),
                            'total_count': len(records),
                        }
            except Exception as _e:
                logger.debug(f"[comments] SQLite fallback 실패: {_e}")
        # [v6.7.167] 요청 limit 이 메모리 캐시 상한(_comment_limit=500)을 초과하면 SQLite 직접 보충.
        #   증상: 필터에서 댓글 한도를 500 초과로 올려도 메모리 캐시(매 cycle 500 atomic swap)에 갇혀 안 늘어남.
        #   hot path(기본 limit<=500)는 메모리 그대로 — 비대 응답만 SQLite. 메모리 스왑은 하지 않음.
        elif limit > _comment_limit:
            try:
                from sqlite_handler import db_handler as _db
                sql_records = _db.get_comments(limit=limit)
                if sql_records and len(sql_records) > len(records):
                    for r in sql_records:
                        lnk = str(r.get('link', '') or r.get('comment_link', ''))
                        r['source_community'] = 'DAUM' if 'cafe.daum.net' in lnk else 'DC'
                    records = sql_records
            except Exception as _e:
                logger.debug(f"[comments] SQLite 보충 실패: {_e}")

        data = records[:limit]

        # 댓글 전용: source_community 만 주입
        # [v6.6.238] game 폴백 — DB 에 저장 안 됐으면 comment_link 의 grpid 파싱으로 즉석 주입
        _crawler_for_game = get_crawler()
        for d in data:
            lnk = str(d.get('link', '') or d.get('comment_link', ''))
            d['source_community'] = 'DAUM' if 'cafe.daum.net' in lnk else 'DC'
            if not d.get('game') and _crawler_for_game:
                try:
                    d['game'] = _crawler_for_game.extract_game_from_comment_link(d.get('comment_link'))
                except Exception:
                    d['game'] = None

        return jsonify(data)
    except Exception as e:
        logger.error(f"댓글 API 오류: {e}")
        # [v6.7.489] 위 articles/all 과 같은 이유로 500. comments-tab.js 는 `if (!response.ok) return;`
        #   이라 500 이면 UI 를 안 건드리고 기존 목록이 남는다 — 200+[] 였을 때는 목록이 비워졌다.
        return jsonify({'error': 'comments_query_failed'}), 500
