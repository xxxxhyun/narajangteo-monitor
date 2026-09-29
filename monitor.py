"""나라장터(조달청 OpenAPI) 키워드 모니터링 → 슬랙 알림

공공데이터포털 API로 입찰공고 / 발주계획 / 사전규격을 조회하고,
사업명에 키워드가 포함된 건만 골라 슬랙 채널로 보고합니다.

필요한 환경변수 (GitHub Secrets 에 등록)
  G2B_SERVICE_KEY   : 공공데이터포털 인증키 (Decoding 키 권장, Encoding 키도 자동 처리)
  SLACK_WEBHOOK_URL : 슬랙 Incoming Webhook 주소
선택 환경변수
  DRY_RUN=1         : 슬랙으로 보내지 않고 화면에만 출력
  FORCE_RUN=1       : 오늘 이미 보고했어도 다시 실행
  LOOKBACK_HOURS=N  : 조회 기간을 최근 N시간으로 강제 지정 (테스트용)
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")

API_ROOT = "apis.data.go.kr/1230000"

# 조회 대상 정의 ------------------------------------------------------------
# name_fields : 키워드를 찾을 "사업명" 필드 (앞에서부터 존재하는 것 사용)
SOURCES = {
    "bid_servc": {
        "label": "입찰공고 · 용역",
        "path": "ad/BidPublicInfoService/getBidPblancListInfoServc",
        "id_fields": ["bidNtceNo", "bidNtceOrd"],
    },
    "bid_thng": {
        "label": "입찰공고 · 물품",
        "path": "ad/BidPublicInfoService/getBidPblancListInfoThng",
        "id_fields": ["bidNtceNo", "bidNtceOrd"],
    },
    "orderplan_servc": {
        "label": "발주계획 · 용역",
        "path": "ao/OrderPlanSttusService/getOrderPlanSttusListServc",
        "id_fields": ["orderPlanUntyNo", "orderPlanSno", "bizNm"],
    },
    "orderplan_thng": {
        "label": "발주계획 · 물품",
        "path": "ao/OrderPlanSttusService/getOrderPlanSttusListThng",
        "id_fields": ["orderPlanUntyNo", "orderPlanSno", "bizNm", "prdctClsfcNoNm"],
    },
    "prespec_servc": {
        "label": "사전규격 · 용역",
        "path": "ao/HrcspSsstndrdInfoService/getPublicPrcureThngInfoServc",
        "id_fields": ["bfSpecRgstNo"],
    },
    "prespec_thng": {
        "label": "사전규격 · 물품",
        "path": "ao/HrcspSsstndrdInfoService/getPublicPrcureThngInfoThng",
        "id_fields": ["bfSpecRgstNo"],
    },
}

NAME_FIELDS = ["bidNtceNm", "bizNm", "prdctClsfcNoNm", "bsnsNm", "orderPlanNm", "cntrctNm"]
ORG_FIELDS = ["ntceInsttNm", "orderInsttNm", "dminsttNm", "rlDminsttNm", "orderInsttNm"]
AMOUNT_FIELDS = ["asignBdgtAmt", "presmptPrce", "sumOrderAmt", "orderAmt", "bdgtAmt"]
URL_FIELDS = ["bidNtceDtlUrl", "bidNtceUrl"]
G2B_HOME = "https://www.g2b.go.kr"


# 공통 유틸 -----------------------------------------------------------------
def log(msg):
    print(f"[{datetime.now(KST):%H:%M:%S}] {msg}", flush=True)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def first(item, fields):
    for f in fields:
        v = item.get(f)
        if v not in (None, "", "null"):
            return str(v).strip()
    return ""


def fmt_amount(v):
    try:
        n = int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    if n >= 100_000_000:
        return f"{n / 100_000_000:,.1f}억원"
    return f"{n / 10_000:,.0f}만원"


# API 호출 ------------------------------------------------------------------
class ApiError(Exception):
    pass


ERROR_HINTS = {
    "SERVICE_KEY_IS_NOT_REGISTERED": "이 API의 활용신청이 안 됐거나 승인 직후 반영 대기 중(최대 1~2시간)",
    "SERVICE_ACCESS_DENIED": "이 API의 활용신청 여부를 확인하세요",
    "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS": "하루 호출 한도 초과",
    "INVALID_REQUEST_PARAMETER": "요청 파라미터 오류 - 관리자에게 로그 전달",
}


def http_get(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 g2b-monitor"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def parse_response(text):
    """JSON 응답에서 (items, totalCount) 추출. 오류면 ApiError."""
    text = text.strip()
    if not text.startswith("{"):
        # 인증키 오류 등은 XML로 옴
        for tag in ("returnAuthMsg", "resultMsg", "errMsg"):
            m = re.search(rf"<{tag}>([^<]+)<", text)
            if m:
                msg = m.group(1).strip()
                for code, hint in ERROR_HINTS.items():
                    if code in msg:
                        msg += f" ({hint})"
                raise ApiError(msg)
        raise ApiError(f"예상치 못한 응답: {text[:200]}")
    data = json.loads(text)
    resp = data.get("response", data)
    header = resp.get("header", {})
    code = str(header.get("resultCode", "00"))
    if code not in ("00", "0", "000"):
        raise ApiError(f"{code} {header.get('resultMsg', '')}")
    body = resp.get("body", {}) or {}
    items = body.get("items", [])
    if isinstance(items, dict):
        items = items.get("item", [])
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        items = []
    return items, int(body.get("totalCount") or 0)


def call_api(path, params, retries=3):
    query = urllib.parse.urlencode(params)
    last_err = None
    for attempt in range(1, retries + 1):
        for scheme in ("https", "http"):
            url = f"{scheme}://{API_ROOT}/{path}?{query}"
            try:
                return parse_response(http_get(url))
            except ApiError:
                raise  # 인증키/파라미터 오류는 재시도해도 동일
            except Exception as e:  # 타임아웃, 연결 오류 등
                last_err = e
        wait = 10 * attempt
        log(f"  연결 실패({last_err}), {wait}초 후 재시도 {attempt}/{retries}")
        time.sleep(wait)
    raise ApiError(f"연결 실패: {last_err}")


def fetch_source(key, service_key, start, end):
    src = SOURCES[key]
    all_items, page, rows = [], 1, 100
    while True:
        params = {
            "serviceKey": service_key,
            "pageNo": page,
            "numOfRows": rows,
            "type": "json",
            "inqryDiv": 1,
            "inqryBgnDt": start.strftime("%Y%m%d%H%M"),
            "inqryEndDt": end.strftime("%Y%m%d%H%M"),
        }
        items, total = call_api(src["path"], params)
        all_items.extend(items)
        if not items or page * rows >= total or page >= 100:
            break
        page += 1
    return all_items


# 필터 · 메시지 -------------------------------------------------------------
def match_keywords(name, keywords, excludes):
    if any(x and x in name for x in excludes):
        return []
    return [k for k in keywords if k in name]


def item_id(key, item):
    parts = [first(item, [f]) for f in SOURCES[key]["id_fields"]]
    return key + ":" + "-".join(p for p in parts if p)


def format_item(key, item, hits):
    name = first(item, NAME_FIELDS) or "(사업명 없음)"
    url = first(item, URL_FIELDS)
    title = f"<{url}|{name}>" if url.startswith("http") else f"*{name}*"
    info = []
    org = first(item, ORG_FIELDS)
    if org:
        info.append(org)
    amt = fmt_amount(first(item, AMOUNT_FIELDS))
    if amt:
        info.append(amt)
    if key.startswith("bid_"):
        no = first(item, ["bidNtceNo"])
        ord_ = first(item, ["bidNtceOrd"])
        if no:
            info.append(f"공고번호 {no}" + (f"-{ord_}" if ord_ else ""))
        clse = first(item, ["bidClseDt"])
        if clse:
            info.append(f"마감 {clse[:16]}")
    elif key.startswith("orderplan_"):
        ym = first(item, ["orderYm", "orderPlanYm"])
        if ym:
            info.append(f"발주시기 {ym[:4]}.{ym[4:6]}" if len(ym) >= 6 else f"발주시기 {ym}")
    elif key.startswith("prespec_"):
        no = first(item, ["bfSpecRgstNo"])
        if no:
            info.append(f"사전규격번호 {no}")
        clse = first(item, ["opninRgstClseDt"])
        if clse:
            info.append(f"의견마감 {clse[:16]}")
    tag = " ".join(f"`{h}`" for h in hits)
    return f"• {title} {tag}\n    {' · '.join(info)}"


def build_messages(results, errors, start, end):
    total = sum(len(v) for v in results.values())
    period = f"{start:%m/%d %H:%M} ~ {end:%m/%d %H:%M}"
    header = f":mag: *나라장터 키워드 모니터링* ({end:%Y-%m-%d})\n조회기간 {period} · 신규 {total}건"
    chunks, cur = [], header
    for key, lines in results.items():
        if not lines:
            continue
        block = f"\n\n*[{SOURCES[key]['label']}] {len(lines)}건*"
        for line in lines:
            if len(cur) + len(block) + len(line) > 3500:
                chunks.append(cur + block)
                cur, block = "(이어서)", ""
            block += "\n" + line
        cur += block
    if total == 0:
        cur += "\n\n오늘은 키워드에 맞는 신규 건이 없습니다."
    if errors:
        cur += "\n\n:warning: 조회 실패\n" + "\n".join(f"• {SOURCES[k]['label']}: {e}" for k, e in errors.items())
    cur += f"\n\n_나라장터 바로가기: {G2B_HOME}_"
    chunks.append(cur)
    return chunks


def post_slack(webhook, text):
    data = json.dumps({"text": text, "unfurl_links": False}).encode("utf-8")
    req = urllib.request.Request(webhook, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


# 메인 ----------------------------------------------------------------------
def main():
    dry = os.environ.get("DRY_RUN") == "1"
    force = os.environ.get("FORCE_RUN") == "1"
    service_key = os.environ.get("G2B_SERVICE_KEY", "").strip()
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if "%" in service_key:  # Encoding 키를 넣은 경우 Decoding 키로 변환
        service_key = urllib.parse.unquote(service_key)
    if not service_key:
        sys.exit("G2B_SERVICE_KEY 가 비어 있습니다. GitHub Secrets 를 확인하세요.")
    if not webhook and not dry:
        sys.exit("SLACK_WEBHOOK_URL 이 비어 있습니다. GitHub Secrets 를 확인하세요.")

    config = load_json(CONFIG_PATH, {})
    keywords = config.get("keywords", [])
    excludes = config.get("exclude_keywords", [])
    enabled = [k for k, on in config.get("sources", {k: True for k in SOURCES}).items() if on and k in SOURCES]
    state = load_json(STATE_PATH, {"seen": {}, "last_end": {}, "last_success_date": None})

    now = datetime.now(KST).replace(second=0, microsecond=0)
    today = now.strftime("%Y-%m-%d")
    if state.get("last_success_date") == today and not force:
        log("오늘 보고가 이미 완료되어 건너뜁니다. (백업 실행)")
        return

    ends = state.get("last_end") or {}
    if not isinstance(ends, dict):
        ends = {}

    def window_start(key):
        if os.environ.get("LOOKBACK_HOURS"):
            return now - timedelta(hours=int(os.environ["LOOKBACK_HOURS"]))
        if ends.get(key):
            # 지난 조회 종료시각에서 1시간 겹쳐 조회(누락 방지), 최대 7일
            s = datetime.fromisoformat(ends[key]) - timedelta(hours=1)
            return max(s, now - timedelta(days=7))
        return now - timedelta(hours=24)

    end = now
    start = min(window_start(k) for k in enabled) if enabled else now
    log(f"조회기간 {start:%Y-%m-%d %H:%M} ~ {end:%Y-%m-%d %H:%M}, 키워드 {keywords}")

    seen = state.get("seen", {})
    results, errors = {}, {}
    for key in enabled:
        label = SOURCES[key]["label"]
        try:
            items = fetch_source(key, service_key, window_start(key), end)
        except Exception as e:
            log(f"{label}: 실패 - {e}")
            errors[key] = str(e)[:200]
            continue
        lines = []
        for it in items:
            name = first(it, NAME_FIELDS)
            hits = match_keywords(name, keywords, excludes)
            if not hits:
                continue
            iid = item_id(key, it)
            if iid in seen:
                continue
            seen[iid] = today
            lines.append(format_item(key, it, hits))
        results[key] = lines
        ends[key] = end.isoformat()
        log(f"{label}: 전체 {len(items)}건 중 신규 매칭 {len(lines)}건")

    total = sum(len(v) for v in results.values())
    if total or errors or config.get("send_empty_report", True):
        for text in build_messages(results, errors, start, end):
            if dry:
                print("\n----- 슬랙 메시지 미리보기 -----\n" + text)
            else:
                post_slack(webhook, text)
        log("슬랙 전송 완료" if not dry else "DRY_RUN: 슬랙 전송 생략")

    # 오래된 기록(60일) 정리 후 저장
    cutoff = (now - timedelta(days=60)).strftime("%Y-%m-%d")
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    state["last_end"] = ends
    if not errors:
        state["last_success_date"] = today
    if not dry:
        save_json(STATE_PATH, state)
    if errors and len(errors) == len(enabled):
        sys.exit("모든 조회가 실패했습니다. 위 로그를 확인하세요.")


if __name__ == "__main__":
    main()
