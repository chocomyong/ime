"""test_default_tabs_ui.py - 첫 진입 기본 탭 = 피드 + AI 요약, 옛 기본 구성 1회 이전 (v6.7.725 신설)

**왜 있나**: 기본값만 바꾸면 **이미 저장된 구성이 있는 사람(= 실제 사용자 전원)** 은 계속 옛 기본(피드+대시보드)을 본다.
그래서 state.js 가 "저장된 구성이 옛 기본값 그대로" 인 경우만 1회 옮긴다. 이 검사는 그 갈래를 브라우저에서 태운다.
- 옛 기본은 옮겨지고 **재로드 뒤에도 유지**된다(플래그만 서고 저장이 빠지면 재로드에 옛 구성이 되살아난다 - 재주입으로 확인함)
- 사용자가 직접 바꾼 구성 · 이전 뒤 되돌린 구성은 건드리지 않는다(대조군)

실행: cd web_app && python test_default_tabs_ui.py   종료코드 0 = PASS / 1 = FAIL / 3 = playwright 없음(PASS 아님)
"""
import os
import sys

os.chdir(os.path.dirname(os.path.abspath(__file__)))
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print('[SKIP] playwright 없음 - 환경 없음(PASS 아님)')
    sys.exit(3)

src = open('static/js/tab-manager/state.js', encoding='utf-8').read()
# [v6.7.741] 'discord' 는 은닉이 아니라 삭제됐다(web_lib/hidden_apps.py) - 은닉 목록 사본에서 빼고, 사용자 지정 사례를
#   지운 'naver-watcher' 대신 남은 'verdict' 로, 은닉 사례를 'ccu-grafana' 로 옮겼다. 지운 탭 저장분 사례를 하나 더 둔다.
HID = ["comments","ccu-grafana","account-trade","diamond-trade","trends"]
cases = [
 ("옛 기본(피드+대시보드)", {"imt_dashboard_config": ["feed","situation"], "imt_situation_backfill_v467": "1"}, ["feed","ai-summary"]),
 ("옛 기본(댓글 포함 저장)", {"imt_dashboard_config": ["feed","comments","situation"], "imt_situation_backfill_v467": "1"}, ["feed","ai-summary"]),
 ("사용자 지정 유지", {"imt_dashboard_config": ["feed","verdict"], "imt_situation_backfill_v467": "1"}, ["feed","verdict"]),
 ("은닉 탭 저장분 제거", {"imt_dashboard_config": ["feed","ccu-grafana","ai-summary"], "imt_situation_backfill_v467": "1"}, ["feed","ai-summary"]),
 ("지운 탭 저장분 제거(v6.7.741)", {"imt_dashboard_config": ["feed","naver-watcher","discord","update-history","ai-summary"], "imt_situation_backfill_v467": "1"}, ["feed","ai-summary"]),
 ("첫 방문(저장 없음)", {}, ["feed","ai-summary"]),
 ("이미 이전한 뒤 대시보드로 되돌린 사용자", {"imt_dashboard_config": ["feed","situation"], "imt_situation_backfill_v467": "1", "imt_default_ai_summary_v725": "1"}, ["feed","situation"]),
]
ok = True
with sync_playwright() as p:
    b = p.chromium.launch()
    for name, ls, want in cases:
        pg = b.new_page()
        pg.route("http://t.local/", lambda r: r.fulfill(body="<html><body></body></html>", content_type="text/html"))
        pg.goto("http://t.local/")
        pg.evaluate("""([ls, hid]) => { localStorage.clear(); for (const [k,v] of Object.entries(ls)) localStorage.setItem(k, typeof v==='string'? v : JSON.stringify(v)); window.IMT_HIDDEN_APPS = hid; }""", [ls, HID])
        pg.add_script_tag(content=src + "\nwindow.__a = activeApps; window.__l = lastCoreApps;")
        a = pg.evaluate("window.__a"); l = pg.evaluate("window.__l")
        saved = pg.evaluate("localStorage.getItem('imt_dashboard_config')")
        good = a == want
        ok &= good
        print("PASS" if good else "FAIL", name, "activeApps=", a, "lastCore=", l, "저장=", saved)
        # 재로드: 플래그가 선 뒤 다시 로드해도 결과가 유지되는가(저장 누락이면 옛 구성이 되살아난다)
        pg.reload(); pg.evaluate("hid => { window.IMT_HIDDEN_APPS = hid; }", HID)
        pg.add_script_tag(content=src + "\nwindow.__a = activeApps;")
        a2 = pg.evaluate("window.__a"); g2 = a2 == want; ok &= g2
        print("   ", "PASS" if g2 else "FAIL", "재로드 후", a2)
        pg.close()
    # [v6.7.741] lastCoreApps(유틸리티 탭에서 돌아올 때 복원할 코어 구성)는 위 validIds 필터를 안 탄다 - 지운 탭이 저장돼
    #   있으면 없는 pane 을 복원하려 들었다. state.js 가 CORE_APP_IDS 로 거르는지 본다(그 .filter 를 빼면 FAIL).
    pg = b.new_page()
    pg.route("http://t.local/", lambda r: r.fulfill(body="<html><body></body></html>", content_type="text/html"))
    pg.goto("http://t.local/")
    pg.evaluate("""hid => { localStorage.clear(); localStorage.setItem('imt_dashboard_config', JSON.stringify(['feed','ai-summary']));
        localStorage.setItem('imt_last_core_config', JSON.stringify(['feed','naver-watcher','discord','ai-summary']));
        localStorage.setItem('imt_situation_backfill_v467', '1'); localStorage.setItem('imt_default_ai_summary_v725', '1');
        window.IMT_HIDDEN_APPS = hid; }""", HID)
    pg.add_script_tag(content=src + "\nwindow.__l = lastCoreApps;")
    l = pg.evaluate("window.__l"); g = l == ["feed", "ai-summary"]; ok &= g
    print("PASS" if g else "FAIL", "지운 탭이 lastCoreApps 저장분에 있어도 복원 목록에서 빠진다", l)
    pg.close()
    b.close()
print("ALL", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
