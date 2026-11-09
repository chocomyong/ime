"""
알림 매니저 (Notification Manager)
- 역할: 카카오워크, Watchtower 등 알림 채널 통합 관리 및 전송
- 기능: 구글 시트 설정 로드(캐싱), 메시지 전송(스레드 비동기)
"""
import os
import re
import time
import requests
import threading
import json
from google_sheets_handler import get_handler, CONFIG_SHEET_URL

# [v6.7.9] 앱 출처 식별자 — 각 진입점에서 set_app_source() 로 설정.
# 빈 문자열이면 출처 블록 미삽입 (미설정 앱 호환).
_APP_SOURCE: str = ''

def set_app_source(name: str) -> None:
    """앱 시작 시 1회 호출. 이후 send_kakaowork 블록 알림에 '출처' 필드 자동 삽입."""
    global _APP_SOURCE
    _APP_SOURCE = name


# [v6.7.729] **외부 알림 스위치 - 기본 OFF**(사용자 2026-10-13 지시 - 앱 밖 알림을 더 볼 필요가 없다).
#   IME 가 앱 밖으로 내보내는 알림(카카오워크·텔레그램·크래시·Watchtower·폰 웹푸시)을 전부 멈춘다.
#   **앱 안은 그대로다** - 알림 보관소(종)·인앱 배너·AI 요약 탭은 이 스위치와 무관(사용자 "AI요약 알림은 웹앱에서는 볼 수 있게 유지").
#   다시 켜기 = web_app/.env 에 IMT_EXTERNAL_ALERTS=1 + recreate. 같은 env 를 bg/web_push.py 도 본다(관문 2곳, 값 1곳).
def external_alerts_enabled():
    return os.environ.get("IMT_EXTERNAL_ALERTS", "0").strip() == "1"


# [v6.7.727] (2026-10-12) 개인 텔레그램이 잡히면 **카카오워크 대신 거기로** 보낸다(사용자 지시
#   "카카오워크로 알림보내지말고 내 개인 텔레그램으로"). env 두 개는 web_app/.env -> compose(imt-host tg-personal-install).
#   호출할 때마다 읽는다(테스트가 켜고 끌 수 있게). 하나라도 비면 종전 카카오워크 경로 그대로 - 되돌리기 = env 삭제.
def _tg_personal():
    tok = os.environ.get("IMT_TELEGRAM_PERSONAL_TOKEN", "").strip()
    chat = os.environ.get("IMT_TELEGRAM_PERSONAL_CHAT_ID", "").strip()
    return (tok, chat) if tok and chat else None


def _blocks_to_plain(text, blocks):
    """카카오워크 블록 -> 텔레그램 평문. 블록이 본문을 들고 있어서(text 는 푸시 미리보기용 한 줄) 버리면 내용이 빈다.
    헤더는 20자로 잘린 사본이라 text 에 이미 담겼으면 뺀다. 링크 버튼은 본문에 같은 URL 이 없을 때만 줄로 남긴다."""
    lines = [text] if text else []
    for b in blocks or []:
        t = b.get('type')
        if t == 'header':
            h = (b.get('text') or '').rstrip('…')
            if h and h not in (text or ''):
                lines.insert(0, b.get('text'))
        elif t == 'text':
            lines.append(b.get('text') or '')
        elif t == 'description':
            lines.append(f"{b.get('term', '')}: {((b.get('content') or {}).get('text') or '')}")
        elif t == 'button':
            v = b.get('value') or ''
            if v and v not in '\n'.join(lines):
                lines.append(f"{b.get('text', '링크')}: {v}")
    out = '\n'.join(l for l in lines if l is not None)
    return re.sub(r'\*\*(.+?)\*\*', r'\1', out)[:4096]   # 카카오워크 굵게 표기는 텔레그램 평문에서 별표로 남는다


def _post_tg_personal(tg, text):
    tok, chat = tg
    return requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                         json={"chat_id": chat, "text": text, "disable_web_page_preview": True}, timeout=10)


class NotificationManager:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if not cls._instance:
            with cls._lock:
                if not cls._instance:
                    cls._instance = super(NotificationManager, cls).__new__(cls)
                    cls._instance._init()
        return cls._instance

    def _init(self):
        self.config = {
            "webhook_url": None,
            "webhook_url_test": None,
            "crash_webhook_url": None,
            "app_key": None,
            "conv_id": None,
            "watchtower_url": None,
            "watchtower_group_id": None,
            "last_checked": 0,
        }
        self._kill_signals = {}

    def load_config(self, force=False):
        """구글 시트에서 알림 설정을 로드합니다. (10분 캐싱)"""
        # [수정] 설정 값 유무와 상관없이 캐시 시간(10분)을 준수하여 불필요한 API 호출 방지
        if not force and time.time() - self.config["last_checked"] < 600:
            return

        try:
            handler = get_handler()
            if not handler.gc: return

            sh = handler.gc.open_by_url(CONFIG_SHEET_URL)
            ws = sh.worksheet("Config_Settings")
            records = ws.get_all_records()

            new_kill_signals = {}
            for row in records:
                key = row.get('Key')
                val = str(row.get('Value')).strip()
                if key == 'KAKAOWORK_WEBHOOK_URL':
                    self.config["webhook_url"] = val
                elif key == 'KAKAOWORK_WEBHOOK_URL_TEST':
                    self.config["webhook_url_test"] = val if val and val.upper() != 'NONE' else None
                elif key == 'KAKAOWORK_CRASH_WEBHOOK_URL':
                    if val and val.upper() != 'NONE':
                        self.config["crash_webhook_url"] = val
                elif key == 'KAKAOWORK_APP_KEY':
                    self.config["app_key"] = val
                elif key == 'KAKAOWORK_CONV_ID':
                    self.config["conv_id"] = val
                elif key == 'WATCHTOWER_URL':
                    if val: self.config["watchtower_url"] = val
                elif key == 'WATCHTOWER_GROUP_ID':
                    try:
                        if val: self.config["watchtower_group_id"] = int(val)
                    except (ValueError, TypeError):
                        print(f">>> [설정] Watchtower Group ID는 숫자여야 합니다: {val}")
                elif key and key.startswith('REMOTE_KILL'):
                    new_kill_signals[key] = val.upper()
            self._kill_signals = new_kill_signals


            self.config["last_checked"] = time.time()
        except Exception as e:
            print(f">>> [설정] 알림 설정 로드 실패: {e}")

    def check_remote_kill(self, app_type):
        """원격 킬 신호를 확인합니다. (항상 최신 값 로드)
        app_type: 'WEB', 'TERMINAL', 'AI', 'DESKTOP', 'VIEWER'
                  ('WEB' 이 실제 호출값인데 목록에서 빠져 있었다 - 2026-09-06 실측)
        반환: True이면 종료해야 함
        """
        self.load_config()
        if self._kill_signals.get('REMOTE_KILL') == 'KILL':
            return True
        if self._kill_signals.get(f'REMOTE_KILL_{app_type}') == 'KILL':
            return True
        return False

    # --- [v6.6.343] 카카오워크 알림 카테고리별 ON/OFF 토글 ---

    def is_enabled(self, category: str) -> bool:
        """카테고리별 알림 토글 체크. `web_lib.globals.notification_toggles` 참조.
        등록되지 않은 카테고리는 **기본 True (ON)** — 신규 카테고리가 추가돼도 자동 켜짐.
        globals import 실패(독립 스크립트 실행 등) 시에도 True 반환해 호환성 유지.
        """
        if not category:
            return True
        try:
            import web_lib.globals as _gl
            with _gl.notification_toggles_lock:
                return bool(_gl.notification_toggles.get(category, True))
        except Exception:
            return True

    def send_categorized(self, category: str, text: str, blocks=None, trace_meta=None):
        """카테고리 가드 적용 카카오워크 전송. `is_enabled(category)` False 면 조용히 스킵."""
        if not self.is_enabled(category):
            # [v6.6.527] 토글 OFF 로 차단된 경우도 trace 에 'blocked' 1건 기록 — 운영자가 "왜 안 갔지?" 추적용.
            if trace_meta:
                try:
                    from utils import notification_trace as _ntrace
                    _ntrace.record(
                        'send',
                        kind=trace_meta.get('kind'),
                        game=trace_meta.get('game'),
                        title=trace_meta.get('title'),
                        link=trace_meta.get('link'),
                        http_status=None, ok=False,
                        error_msg=f"toggle OFF ({category})",
                    )
                except Exception:
                    pass
            return False
        self.send_kakaowork(text, blocks, trace_meta=trace_meta)
        return True

    # 편의 메서드 (각 호출처에서 명시적 시맨틱 사용 가능)
    def send_ai_summary(self, text, blocks=None):
        return self.send_categorized('ai_summary', text, blocks)

    def send_keyword_alert(self, text, blocks=None, trace_meta=None):
        return self.send_categorized('keyword_alert', text, blocks, trace_meta=trace_meta)

    def send_official_alert(self, text, blocks=None, trace_meta=None):
        # [v6.7.7] official_alert 독립 토글 — keyword_alert(SUPER) 와 분리해 웹앱 기본 ON 허용.
        return self.send_categorized('official_alert', text, blocks, trace_meta=trace_meta)

    def send_nightly_digest(self, text, blocks=None):
        return self.send_categorized('nightly_digest', text, blocks)

    def send_ccu_alert(self, text, blocks=None):
        return self.send_categorized('ccu_alert', text, blocks)

    def send_crawler_healing(self, text, blocks=None):
        return self.send_categorized('crawler_healing', text, blocks)

    def send_crawler_restart(self, text, blocks=None):
        return self.send_categorized('crawler_restart', text, blocks)

    def send_kakaowork(self, text, blocks=None, trace_meta=None):
        """카카오워크로 메시지를 전송합니다.
        [v6.6.527] trace_meta 가 dict 면 응답 도착 후 utils.notification_trace 에 'send' 이벤트 기록.
                   None 이면 trace 무동작 (기존 호출처 무수정).
        """
        if not external_alerts_enabled():   # [v6.7.729] 외부 알림 OFF - 시트도 안 읽는다
            return
        self.load_config()

        webhook_url_test = self.config.get("webhook_url_test")
        webhook_url = webhook_url_test or self.config.get("webhook_url")
        app_key = self.config.get("app_key")
        conv_id = self.config.get("conv_id")

        tg = _tg_personal()
        # [v6.7.728] 텔레그램이 잡히면 이 줄은 거짓말이다(실제론 텔레그램으로 간다) - 라이브 시험에서 오해를 부른 줄.
        #   웹훅 URL 도 발신권이라 통째로 찍지 않는다(지문만).
        if webhook_url_test and not tg:
            print(f">>> [알림] 테스트 웹훅으로 전송: ...{webhook_url_test[-6:]}")
        if not tg and not webhook_url and (not app_key or not conv_id):
            return

        payload = {"text": text}
        if blocks:
            _blk = list(blocks)
            if _APP_SOURCE:
                _src_blk = {
                    "type": "description",
                    "term": "출처",
                    "content": {"type": "text", "text": _APP_SOURCE, "markdown": False},
                    "accent": False,
                }
                _ins = next((i for i, b in enumerate(_blk) if b.get('type') in ('divider', 'button')), len(_blk))
                _blk.insert(_ins, _src_blk)
            payload["blocks"] = _blk

        def _send():
            resp = None
            err = None
            try:
                # [개선] 스레드 내에서 payload 복사본을 사용하여 동시성 문제 방지
                local_payload = payload.copy()
                if tg:   # [v6.7.727] 카카오워크 대신 본인 텔레그램
                    resp = _post_tg_personal(tg, _blocks_to_plain(text, local_payload.get("blocks")))
                elif webhook_url:
                    resp = requests.post(webhook_url, json=local_payload, timeout=10)
                else:
                    headers = {"Authorization": f"Bearer {app_key}", "Content-Type": "application/json"}
                    local_payload["conversation_id"] = conv_id
                    resp = requests.post("https://api.kakaowork.com/v1/messages.send", headers=headers, json=local_payload, timeout=10)

                if resp is not None and not resp.ok:
                    # 텔레그램 실패 응답엔 토큰이 없지만 URL 이 섞일 수 있는 예외 문구는 아래 except 가 받는다.
                    print(f">>> [알림] {'텔레그램' if tg else '카카오워크'} 전송 실패 ({resp.status_code}): {resp.text[:200]}")
            except Exception as e:
                err = re.sub(r'bot\d+:[A-Za-z0-9_-]+', 'bot<숨김>', str(e))   # requests 예외 문구에 URL(=토큰)이 들어간다
                print(f">>> [알림] 전송 중 오류: {err}")
            # 트레이스 — meta 가 있을 때만 1건 기록.
            if trace_meta:
                try:
                    from utils import notification_trace as _ntrace
                    _ntrace.record(
                        'send',
                        kind=trace_meta.get('kind'),
                        game=trace_meta.get('game'),
                        title=trace_meta.get('title'),
                        link=trace_meta.get('link'),
                        http_status=(resp.status_code if resp is not None else None),
                        ok=(bool(resp.ok) if resp is not None else False),
                        error_msg=err,
                    )
                except Exception:
                    pass

        threading.Thread(target=_send, daemon=True).start()

    def send_crash_report(self, app_name, error_text):
        """크래시 발생 시 카카오워크로 직접 전송. 시트 캐시 우선, 없으면 env `IMT_CRASH_WEBHOOK_URL`.
        [v6.6.343] `crash_report` 토글 OFF 면 전송 스킵.
        [v6.7.745] 코드에 박아 둔 폴백 webhook 원문을 지웠다 - 비밀을 코드에 두지 않는다. 둘 다 없고
          개인 텔레그램도 없으면 보내지 않는다(외부 알림은 v6.7.729 부터 기본 OFF 라 운영 동작은 같다).
        """
        if not external_alerts_enabled() or not self.is_enabled('crash_report'):   # [v6.7.729]
            return
        import socket
        from datetime import datetime
        hostname = socket.gethostname()
        now = datetime.now().strftime("%Y/%m/%d %H:%M")
        if len(error_text) > 500:
            error_text = "..." + error_text[-500:]
        text = (
            f"IMT {app_name} crash\n"
            f"시간: {now}\n"
            f"기기: {hostname}\n"
            f"\n"
            f"{error_text}"
        )
        webhook_url = self.config.get("crash_webhook_url") or os.environ.get("IMT_CRASH_WEBHOOK_URL", "").strip()
        tg = _tg_personal()
        def _send():
            try:
                if tg:   # [v6.7.727] 카카오워크 크래시 방 대신 본인 텔레그램
                    _post_tg_personal(tg, text)
                    return
                if webhook_url:
                    requests.post(webhook_url, json={"text": text}, timeout=10)
            except Exception:
                pass
        threading.Thread(target=_send, daemon=True).start()

    def send_watchtower(self, message):
        """Watchtower(카카오톡)로 메시지를 전송합니다."""
        if not external_alerts_enabled():   # [v6.7.729]
            return
        self.load_config()
        url = self.config.get("watchtower_url")
        group_id = self.config.get("watchtower_group_id")

        if not url or not group_id:
            return

        def _send():
            try:
                headers = {"Content-Type": "application/json"}
                payload = {"to": group_id, "msg": message}
                resp = requests.post(url, json=payload, headers=headers, timeout=5)
                
                if not resp.ok:
                    print(f">>> [경고] Watchtower 알림 전송 실패 ({resp.status_code}): {resp.text}")
            except Exception as e:
                print(f">>> [경고] Watchtower 알림 전송 중 오류: {e}")
        
        threading.Thread(target=_send, daemon=True).start()
