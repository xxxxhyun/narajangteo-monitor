"""나라장터(조달청 OpenAPI) 키워드 모니터링 → 슬랙 알림  (v2)

모든 설정은 config.json 에 있고, 설정 웹페이지(admin/index.html)에서 수정합니다.

실행 모드 (환경변수 MODE)
  scheduled : (기본) 매시간 실행되며 config.schedule 의 요일·시각에만 실제 발송
  preview   : 실제 데이터로 조회만 하고 결과를 previews/latest.json 에 저장 (슬랙 전송 X)
  send      : 실제 데이터로 조회해 슬랙에 [테스트] 메시지 전송 (발송 기록에는 반영 X)

환경변수
  G2B_SERVICE_KEY   : 공공데이터포털 인증키 (필수)
  SLACK_WEBHOOK_URL : 슬랙 Incoming Webhook 주소 (preview 모드 외 필수)
  LOOKBACK_HOURS    : 조회 기간을 최근 N시간으로 지정 (preview/send 기본 24)
  REQUEST_ID        : 설정 웹페이지가 테스트 결과를 찾기 위한 식별자
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")
PREVIEW_PATH = os.path.join(HERE, "previews", "latest.json")
API_ROOT = "apis.data.go.kr/1230000"

# ---------------------------------------------------------------------------
# 조회 대상 정의
# ---------------------------------------------------------------------------
SOURCES = {
    "bid_servc": {"label": "입찰공고 · 용역", "kind": "bid",
                  "path": "ad/BidPublicInfoService/getBidPblancListInfoServc",
                  "id_fields": ["bidNtceNo", "bidNtceOrd"]},
    "bid_thng": {"label": "입찰공고 · 물품", "kind": "bid",
                 "path": "ad/BidPublicInfoService/getBidPblancListInfoThng",
                 "id_fields": ["bidNtceNo", "bidNtceOrd"]},
    "orderplan_servc": {"label": "발주계획 · 용역", "kind": "orderplan",
                        "path": "ao/OrderPlanSttusService/getOrderPlanSttusListServc",
                        "id_fields": ["orderPlanUntyNo", "orderPlanSno", "bizNm"]},
    "orderplan_thng": {"label": "발주계획 · 물품", "kind": "orderplan",
                       "path": "ao/OrderPlanSttusService/getOrderPlanSttusListThng",
                       "id_fields": ["orderPlanUntyNo", "orderPlanSno", "bizNm", "prdctClsfcNoNm"]},
    "prespec_servc": {"label": "사전규격 · 용역", "kind": "prespec",
                      "path": "ao/HrcspSsstndrdInfoService/getPublicPrcureThngInfoServc",
                      "id_fields": ["bfSpecRgstNo"]},
    "prespec_thng": {"label": "사전규격 · 물품", "kind": "prespec",
                     "path": "ao/HrcspSsstndrdInfoService/getPublicPrcureThngInfoThng",
                     "id_fields": ["bfSpecRgstNo"]},
}

NAME_FIELDS = ["bidNtceNm", "bizNm", "prdctClsfcNoNm", "bsnsNm", "orderPlanNm", "cntrctNm"]
ORG_FIELDS = ["ntceInsttNm", "orderInsttNm", "dminsttNm", "rlDminsttNm"]
AMOUNT_FIELDS = ["asignBdgtAmt", "presmptPrce", "sumOrderAmt", "orderAmt", "bdgtAmt"]
URL_FIELDS = ["bidNtceDtlUrl", "bidNtceUrl"]

DEFAULT_CONFIG = {
    "keywords": [],
    "exclude_keywords": [],
    "filters": {"min_amount": 0, "max_amount": 0, "include_orgs": [], "exclude_orgs": []},
    "sources": {k: True for k in SOURCES},
    "schedule": {"days": [0, 1, 2, 3, 4, 5, 6], "hours": [20]},
    "send_empty_report": True,
    "message": {
        "header": ":mag: *나라장터 키워드 모니터링* ({날짜})\n조회기간 {기간} · 신규 {총건수}건",
        "section": "*[{구분}] {건수}건*",
        "item": "• {제목} {키워드}\n    {기관} · {금액} · {번호} · {마감} · {발주시기}",
        "empty": "오늘은 키워드에 맞는 신규 건이 없습니다.",
        "footer": "_나라장터 바로가기: https://www.g2b.go.kr_",
    },
}

ERROR_HINTS = {
    "SERVICE_KEY_IS_NOT_REGISTERED": "이 API의 활용신청이 안 됐거나 승인 직후 반영 대기 중(최대 1~2시간)",
    "SERVICE_ACCESS_DENIED": "이 API의 활용신청 여부를 확인하세요",
    "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS": "하루 호출 한도 초과",
    "INVALID_REQUEST_PARAMETER": "요청 파라미터 오류 - 로그를 Claude에게 전달",
}

LOG_LINES = []


def log(msg):
    line = f"[{datetime.now(KST):%H:%M:%S}] {msg}"
    LOG_LINES.append(line)
    print(line, flush=True)


# ---------------------------------------------------------------------------
# 파일 입출력
# ---------------------------------------------------------------------------
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_config():
    cfg = load_json(CONFIG_PATH, {})
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    for k, v in cfg.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k].update(v)
        else:
            merged[k] = v
    return merged


# ---------------------------------------------------------------------------
# 공통 유틸
# ---------------------------------------------------------------------------
def first(item, fields):
    for f in fields:
        v = item.get(f)
        if v not in (None, "", "null"):
            return str(v).strip()
    return ""


def to_int(v):
    try:
        return int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return 0


def fmt_amount(n):
    if n <= 0:
        return ""
    if n >= 100_000_000:
        return f"{n / 100_000_000:,.1f}억원"
    return f"{n / 10_000:,.0f}만원"


# ---------------------------------------------------------------------------
# API 호출
# ---------------------------------------------------------------------------
class ApiError(Exception):
    pass


REQUEST_TIMEOUT = 30      # 요청 1건 대기 시간(초)
TIME_BUDGET = 9 * 60      # 전체 조회 제한 시간(초). 넘으면 남은 조회는 실패 처리 후 결과 보고
_deadline = [None]


def http_get(url, timeout=REQUEST_TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 g2b-monitor"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def parse_response(text):
    """JSON 응답에서 (items, totalCount) 추출. 오류면 ApiError."""
    text = text.strip()
    if not text.startswith("{"):
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
    return items, to_int(body.get("totalCount"))


def call_api(path, params, attempts=3):
    """1회 30초 대기, 최대 3회(https→http→https) 시도."""
    query = urllib.parse.urlencode(params)
    last_err = None
    for i in range(attempts):
        if _deadline[0] and time.monotonic() > _deadline[0]:
            raise ApiError("전체 제한 시간 초과 - 조회 기간을 줄여 다시 시도하세요")
        scheme = "http" if i % 2 else "https"
        try:
            return parse_response(http_get(f"{scheme}://{API_ROOT}/{path}?{query}"))
        except ApiError:
            raise  # 인증키/파라미터 오류는 재시도해도 동일
        except Exception as e:  # 타임아웃, 연결 오류 등
            last_err = e
            if i < attempts - 1:
                time.sleep(5 * (i + 1))
    raise ApiError(f"연결 실패({attempts}회): {last_err}")


def day_chunks(start, end, hours=24):
    """긴 조회기간을 하루 단위로 쪼갬 (한 번에 긴 기간을 요청하면 서버 응답이 매우 느림)"""
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(hours=hours), end)
        yield cur, nxt
        cur = nxt


def fetch_pages(path, base_params, label):
    items, page, rows = [], 1, 100
    while True:
        chunk, total = call_api(path, {**base_params, "pageNo": page, "numOfRows": rows})
        items.extend(chunk)
        if not chunk or page * rows >= total or page >= 100:
            return items
        page += 1


def search_keywords(keywords):
    """서버 검색용 키워드: 다른 키워드를 포함하는 키워드는 생략 (예: '상담'이 있으면 '상담톡' 생략)"""
    ks = sorted({k for k in keywords if k}, key=len)
    return [k for i, k in enumerate(ks) if not any(o in k for o in ks[:i])]


def fetch_source(key, service_key, start, end, keywords):
    src = SOURCES[key]
    label = src["label"]
    base = {"serviceKey": service_key, "type": "json", "inqryDiv": 1}
    rng = lambda s, e: {"inqryBgnDt": s.strftime("%Y%m%d%H%M"), "inqryEndDt": e.strftime("%Y%m%d%H%M")}

    # 입찰공고: 조달청 서버에서 사업명 키워드로 검색 (받는 데이터가 훨씬 적어 빠름)
    if src["kind"] == "bid" and keywords:
        try:
            found = {}
            for kw in search_keywords(keywords):
                for s, e in day_chunks(start, end, hours=24 * 7):
                    for it in fetch_pages(src["path"] + "PPSSrch", {**base, **rng(s, e), "bidNtceNm": kw}, label):
                        found[(it.get("bidNtceNo"), it.get("bidNtceOrd"))] = it
            return list(found.values())
        except ApiError as e:
            if "연결 실패" in str(e) or "제한 시간" in str(e):
                raise
            log(f"  {label}: 키워드 검색 미지원({e}) → 전체 목록 조회로 전환")

    items = []
    for s, e in day_chunks(start, end):
        items.extend(fetch_pages(src["path"], {**base, **rng(s, e)}, label))
    return items


# ---------------------------------------------------------------------------
# 필터
# ---------------------------------------------------------------------------
def match(item, cfg):
    """조건에 맞으면 매칭된 키워드 목록, 아니면 빈 리스트."""
    name = first(item, NAME_FIELDS)
    if any(x and x in name for x in cfg["exclude_keywords"]):
        return []
    hits = [k for k in cfg["keywords"] if k and k in name]
    if not hits:
        return []
    f = cfg["filters"]
    amount = to_int(first(item, AMOUNT_FIELDS))
    if amount and f.get("min_amount") and amount < to_int(f["min_amount"]):
        return []
    if amount and f.get("max_amount") and amount > to_int(f["max_amount"]):
        return []
    orgs = " ".join(str(item.get(k, "")) for k in ORG_FIELDS)
    if f.get("include_orgs") and not any(o and o in orgs for o in f["include_orgs"]):
        return []
    if any(o and o in orgs for o in f.get("exclude_orgs", [])):
        return []
    return hits


def item_id(key, item):
    parts = [first(item, [f]) for f in SOURCES[key]["id_fields"]]
    return key + ":" + "-".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# 메시지 템플릿  (admin/index.html 의 render 함수와 동일한 규칙)
#   - {변수} 를 값으로 치환
#   - " · " 로 구분된 항목 중 빈 값은 자동 제거, 빈 줄도 제거
# ---------------------------------------------------------------------------
SEPARATORS = (" · ", " | ", " / ")


def esc(v):
    """슬랙 제어문자(& < >) 이스케이프 - 공고명·기관명 등 값에만 적용"""
    return str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def to_mrkdwn(t):
    """일반 마크다운으로 쓴 서식을 슬랙 서식(mrkdwn)으로 변환"""
    t = re.sub(r"\*\*(.+?)\*\*", r"*\1*", t)                       # **굵게** → *굵게*
    t = re.sub(r"__(.+?)__", r"_\1_", t)                             # __기울임__ → _기울임_
    t = re.sub(r"~~(.+?)~~", r"~\1~", t)                             # ~~취소~~ → ~취소~
    t = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", t)  # [글자](주소) → <주소|글자>
    t = re.sub(r"(?m)^(\s*)#{1,6}\s+(.+)$", r"\1*\2*", t)           # # 제목 → *제목*
    t = re.sub(r"(?m)^(\s*)[-*]\s+", r"\1• ", t)                    # - 항목 → • 항목
    return t


def escape_template(t):
    """템플릿에 직접 쓴 & < > 를 슬랙용으로 이스케이프 (링크 <주소|글자> 와 줄 앞 인용 > 는 유지)"""
    keep = []
    t = re.sub(r"<(?:https?:|mailto:|[@#!])[^<>\n]*>", lambda m: keep.append(m.group(0)) or f"\x00{len(keep) - 1}\x00", t)
    lines = []
    for line in t.split("\n"):
        q = re.match(r"^>{1,3}", line)
        head, body = (q.group(0), line[q.end():]) if q else ("", line)
        body = re.sub(r"&(?!amp;|lt;|gt;)", "&amp;", body).replace("<", "&lt;").replace(">", "&gt;")
        lines.append(head + body)
    return re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m.group(1))], "\n".join(lines))


def render(template, values):
    """템플릿 → 슬랙 메시지 (admin/index.html 의 render 와 동일한 규칙)
    1) 마크다운 → 슬랙 서식 변환  2) {변수} 치환
    3) 구분자( · , | , / )로 나뉜 빈 항목 제거  4) 줄 앞 공백은 슬랙에서 사라지지 않게 고정폭 공백으로"""
    text = re.sub(r"\{([^{}\n]+)\}", lambda m: str(values.get(m.group(1), m.group(0))), escape_template(to_mrkdwn(template)))
    out = []
    for line in text.split("\n"):
        sep = next((x for x in SEPARATORS if x in line), None)
        if sep:
            prefix = re.match(r"^\s*(?:>\s*|[•\-]\s+)?", line).group(0)
            parts = [p.strip() for p in line[len(prefix):].split(sep)]
            line = prefix + sep.join(p for p in parts if p)
        line = line.rstrip()
        if not line.strip() or line.strip() in (">", "•", "-"):
            continue
        out.append(re.sub(r"^ +", lambda m: "\u00a0" * len(m.group(0)), line))
    return "\n".join(out)


def item_values(key, item, hits):
    kind = SOURCES[key]["kind"]
    name = esc(first(item, NAME_FIELDS) or "(사업명 없음)")
    url = esc(first(item, URL_FIELDS))
    v = {
        "구분": SOURCES[key]["label"],
        "사업명": name,
        "링크": url,
        "제목": f"<{url}|{name}>" if url.startswith("http") else f"*{name}*",
        "기관": esc(first(item, ORG_FIELDS)),
        "금액": fmt_amount(to_int(first(item, AMOUNT_FIELDS))),
        "키워드": " ".join(f"`{h}`" for h in hits),
        "번호": "", "마감": "", "발주시기": "",
    }
    if kind == "bid":
        no, ord_ = first(item, ["bidNtceNo"]), first(item, ["bidNtceOrd"])
        if no:
            v["번호"] = f"공고번호 {no}" + (f"-{ord_}" if ord_ else "")
        clse = first(item, ["bidClseDt"])
        if clse:
            v["마감"] = f"마감 {clse[:16]}"
    elif kind == "orderplan":
        ym = first(item, ["orderYm", "orderPlanYm"])
        if ym:
            v["발주시기"] = f"발주시기 {ym[:4]}.{ym[4:6]}" if len(ym) >= 6 else f"발주시기 {ym}"
    elif kind == "prespec":
        no = first(item, ["bfSpecRgstNo"])
        if no:
            v["번호"] = f"사전규격번호 {no}"
        clse = first(item, ["opninRgstClseDt"])
        if clse:
            v["마감"] = f"의견마감 {clse[:16]}"
    return v


def build_messages(cfg, results, errors, start, end, prefix=""):
    """results: {source_key: [item_values, ...]} → 슬랙 메시지 문자열 리스트 (3,500자 단위 분할)"""
    tpl = cfg["message"]
    total = sum(len(v) for v in results.values())
    head = render(tpl["header"], {
        "날짜": end.strftime("%Y-%m-%d"),
        "기간": f"{start:%m/%d %H:%M} ~ {end:%m/%d %H:%M}",
        "총건수": total,
    })
    blocks = []
    for key, items in results.items():
        if not items:
            continue
        lines = [render(tpl["section"], {"구분": SOURCES[key]["label"], "건수": len(items)})]
        lines += [render(tpl["item"], v) for v in items]
        blocks.append(lines)
    if total == 0 and tpl.get("empty"):
        blocks.append([render(tpl["empty"], {})])
    if errors:
        blocks.append([":warning: 조회 실패"] + [f"• {SOURCES[k]['label']}: {esc(e)}" for k, e in errors.items()])
    if tpl.get("footer"):
        blocks.append([render(tpl["footer"], {})])

    chunks, cur = [], prefix + head
    for lines in blocks:
        sep = "\n\n"
        for line in lines:
            if len(cur) + len(sep) + len(line) > 3500:
                chunks.append(cur)
                cur, sep = "(이어서)", "\n"
            cur += sep + line
            sep = "\n"
    chunks.append(cur)
    return chunks


def post_slack(webhook, text):
    data = json.dumps({"text": text, "unfurl_links": False}).encode("utf-8")
    req = urllib.request.Request(webhook, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


# ---------------------------------------------------------------------------
# 발송 시각 판단
# ---------------------------------------------------------------------------
def due_slot(cfg, state, now):
    """지금 발송해야 할 슬롯('YYYY-MM-DD HH') 또는 None.
    GitHub 예약 실행이 늦거나 실패해도 3시간 안에는 다음 실행이 이어서 처리."""
    sch = cfg["schedule"]
    if now.weekday() not in [int(d) for d in sch.get("days", [])]:
        return None
    done = set(state.get("done_slots", []))
    for h in sorted((int(x) for x in sch.get("hours", [])), reverse=True):
        slot = f"{now:%Y-%m-%d} {h:02d}"
        if h <= now.hour < h + 3 and slot not in done:
            return slot
    return None


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------
def main():
    mode = (os.environ.get("MODE") or "scheduled").strip()
    service_key = os.environ.get("G2B_SERVICE_KEY", "").strip()
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if "%" in service_key:  # Encoding 키를 넣은 경우 Decoding 키로 변환
        service_key = urllib.parse.unquote(service_key)
    if not service_key:
        sys.exit("G2B_SERVICE_KEY 가 비어 있습니다. GitHub Secrets 를 확인하세요.")
    if mode != "preview" and not webhook:
        sys.exit("SLACK_WEBHOOK_URL 이 비어 있습니다. GitHub Secrets 를 확인하세요.")

    cfg = load_config()
    state = load_json(STATE_PATH, {})
    state.setdefault("seen", {})
    state.setdefault("done_slots", [])
    ends = state.get("last_end") if isinstance(state.get("last_end"), dict) else {}

    now = datetime.now(KST).replace(second=0, microsecond=0)
    today = now.strftime("%Y-%m-%d")
    test = mode in ("preview", "send")

    slot = None
    if not test:
        slot = due_slot(cfg, state, now)
        if not slot:
            log(f"발송 시각이 아닙니다 (설정: 요일 {cfg['schedule']['days']}, 시각 {cfg['schedule']['hours']}시). 종료.")
            return

    lookback = os.environ.get("LOOKBACK_HOURS", "").strip()
    if test and not lookback:
        lookback = "24"

    def window_start(key):
        if lookback:
            return now - timedelta(hours=int(lookback))
        if ends.get(key):
            s = datetime.fromisoformat(ends[key]) - timedelta(hours=1)  # 1시간 겹쳐서 누락 방지
            return max(s, now - timedelta(days=7))
        return now - timedelta(hours=24)

    enabled = [k for k in SOURCES if cfg["sources"].get(k)]
    end = now
    start = min((window_start(k) for k in enabled), default=now)
    log(f"모드 {mode} · 조회기간 {start:%Y-%m-%d %H:%M} ~ {end:%Y-%m-%d %H:%M} · 키워드 {cfg['keywords']}")

    seen = state["seen"]
    results, errors, counts = {}, {}, {}
    _deadline[0] = time.monotonic() + TIME_BUDGET
    t0 = time.monotonic()

    def run(key):
        return key, fetch_source(key, service_key, window_start(key), end, cfg["keywords"])

    fetched = {}
    with ThreadPoolExecutor(max_workers=len(enabled) or 1) as pool:  # 6개 대상을 동시에 조회
        futures = [pool.submit(run, k) for k in enabled]
        for fut in as_completed(futures):
            try:
                key, items = fut.result()
                fetched[key] = items
            except Exception as e:
                key = enabled[futures.index(fut)]
                errors[key] = str(e)[:200]
                log(f"{SOURCES[key]['label']}: 실패 - {e}")

    for key in enabled:
        if key not in fetched:
            continue
        items = fetched[key]
        matched = []
        for it in items:
            hits = match(it, cfg)
            if not hits:
                continue
            iid = item_id(key, it)
            if not test:
                if iid in seen:
                    continue
                seen[iid] = today
            matched.append(item_values(key, it, hits))
        results[key] = matched
        counts[key] = {"total": len(items), "matched": len(matched)}
        if not test:
            ends[key] = end.isoformat()
        log(f"{SOURCES[key]['label']}: 조회 {len(items)}건 중 매칭 {len(matched)}건")
    log(f"조회 소요 {time.monotonic() - t0:.0f}초")

    prefix = "[테스트] " if test else ""
    messages = build_messages(cfg, results, errors, start, end, prefix)
    total = sum(len(v) for v in results.values())

    if mode == "preview":
        save_json(PREVIEW_PATH, {
            "request_id": os.environ.get("REQUEST_ID", ""),
            "created_at": datetime.now(KST).isoformat(timespec="seconds"),
            "period": f"{start:%Y-%m-%d %H:%M} ~ {end:%Y-%m-%d %H:%M}",
            "counts": counts, "errors": errors, "messages": messages, "log": LOG_LINES,
        })
        log("미리보기 저장 완료 (슬랙 전송 안 함)")
        return

    if total or errors or cfg.get("send_empty_report", True):
        for text in messages:
            post_slack(webhook, text)
        log(f"슬랙 전송 완료 ({len(messages)}개 메시지)")

    if test:
        return
    all_failed = enabled and len(errors) == len(enabled)
    if not all_failed:
        state["done_slots"] = sorted(set(state["done_slots"]) | {slot})[-60:]
    cutoff = (now - timedelta(days=60)).strftime("%Y-%m-%d")
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    state["last_end"] = ends
    save_json(STATE_PATH, state)
    if all_failed:
        sys.exit("모든 조회가 실패했습니다. 다음 시간에 다시 시도합니다.")


if __name__ == "__main__":
    main()
