"""나라장터 경쟁·파트너 분석 — 유사 사업의 참여업체·낙찰업체·계약업체 수집

분석 조건은 analysis_config.json 에 있습니다.
과거 3년치를 처음 모을 때는 하루 호출 한도 때문에 여러 날에 걸쳐 이어서 수집하고,
이미 받은 데이터는 analysis/cache.json 에 쌓아 두었다가 다시 호출하지 않습니다.

흐름
  1) 낙찰 목록 (공고명 키워드 × 30일 구간)     as/ScsbidInfoService/getScsbidListSttus{Servc,Thng}PPSSrch
  2) 공고별 참여업체·순위·투찰금액·평가점수      as/ScsbidInfoService/getOpengResultListInfoOpengCompt
  3) 공고별 계약 (계약업체·공동수급·계약기간)     ao/CntrctInfoService/getCntrctInfoList{Servc,Thng}PPSSrch
  4) 업체별·공고별 집계 → analysis/result.xlsx, analysis/result.json, (선택) 슬랙 요약

환경변수
  G2B_SERVICE_KEY   : 공공데이터포털 인증키 (필수)
  SLACK_WEBHOOK_URL : 있으면 완료 후 슬랙에 요약 전송
  RUN_KIND          : manual(수동) | scheduled(예약). 예약 실행은 수집이 끝났으면 매월 1일에만 새로 조회
  KEYWORDS, YEARS, MIN_AMOUNT : 이번 실행만 조건을 바꿀 때 (비우면 analysis_config.json 값)
  MAX_CALLS         : 이번 실행에서 API별 최대 호출 수 (기본 900)
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from threading import Lock

KST = timezone(timedelta(hours=9))
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "analysis_config.json")
OUT_DIR = os.path.join(HERE, "analysis")
CACHE_PATH = os.path.join(OUT_DIR, "cache.json")
RESULT_JSON = os.path.join(OUT_DIR, "result.json")
RESULT_XLSX = os.path.join(OUT_DIR, "result.xlsx")
API_ROOT = "apis.data.go.kr/1230000"

AWARD_OPS = {
    "용역": "as/ScsbidInfoService/getScsbidListSttusServcPPSSrch",
    "물품": "as/ScsbidInfoService/getScsbidListSttusThngPPSSrch",
}
PARTICIPANT_OP = "as/ScsbidInfoService/getOpengResultListInfoOpengCompt"
CONTRACT_OPS = {
    "용역": "ao/CntrctInfoService/getCntrctInfoListServcPPSSrch",
    "물품": "ao/CntrctInfoService/getCntrctInfoListThngPPSSrch",
}

DEFAULT_CONFIG = {
    "keywords": ["상담", "채팅", "콜센터", "고객센터", "민원", "AICC"],
    "exclude_keywords": [],
    "years": 3,
    "min_amount": 100_000_000,
    "types": ["용역", "물품"],
    "slack_top_n": 10,
}

RESWEEP_DAYS = 150      # 최근 5개월 공고는 아직 낙찰·계약 전일 수 있어 매번 다시 확인
TIME_BUDGET = 50 * 60   # 한 번 실행 최대 50분 (남은 건 다음 실행이 이어서)
REQUEST_TIMEOUT = 30

LOG_LINES = []


def log(msg):
    line = f"[{datetime.now(KST):%H:%M:%S}] {msg}"
    LOG_LINES.append(line)
    print(line, flush=True)


# ---------------------------------------------------------------------------
# 파일
# ---------------------------------------------------------------------------
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, data, compact=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if compact:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(load_json(CONFIG_PATH, {}))
    kw = os.environ.get("KEYWORDS", "").strip()
    if kw:
        cfg["keywords"] = [k.strip() for k in re.split(r"[,\n]", kw) if k.strip()]
    if os.environ.get("YEARS", "").strip():
        cfg["years"] = float(os.environ["YEARS"])
    if os.environ.get("MIN_AMOUNT", "").strip():
        cfg["min_amount"] = to_int(os.environ["MIN_AMOUNT"])
    return cfg


# ---------------------------------------------------------------------------
# 유틸
# ---------------------------------------------------------------------------
def to_int(v):
    try:
        return int(float(str(v).replace(",", "").strip()))
    except (TypeError, ValueError):
        return 0


def to_float(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def s(v):
    return "" if v in (None, "null") else str(v).strip()


def first(item, fields):
    for f in fields:
        v = s(item.get(f))
        if v:
            return v
    return ""


def norm_bizno(v):
    return re.sub(r"\D", "", s(v))


def norm_corp(name):
    n = s(name)
    n = re.sub(r"\(주\)|㈜|주식회사|\(유\)|유한회사|\(사\)|사단법인|\(재\)|재단법인", "", n)
    return re.sub(r"\s+", "", n)


def norm_date(v):
    """'2025-03-04 10:00:00', '20250304', '2025/03/04' → '2025-03-04'"""
    d = re.sub(r"\D", "", s(v))[:8]
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else ""


def fmt_eok(n):
    if not n:
        return "-"
    return f"{n / 100_000_000:,.1f}억" if n >= 100_000_000 else f"{n / 10_000:,.0f}만"


def award_key(it):
    return "|".join([s(it.get("bidNtceNo")), s(it.get("bidNtceOrd")) or "000",
                     s(it.get("bidClsfcNo")) or "0", s(it.get("rbidNo")) or "000"])


def month_windows(start, end):
    """달력 기준 월 단위 구간 (API 조회기간 제한이 '1개월'이라 2월이 낀 30일 구간은 거절됨)"""
    cur = start
    while cur <= end:
        nxt_month = (cur.replace(day=1) + timedelta(days=32)).replace(day=1)
        last = min(nxt_month - timedelta(days=1), end)
        yield cur, last
        cur = last + timedelta(days=1)


def split_window(ws, we):
    mid = ws + (we - ws) // 2
    return [(ws, mid), (mid + timedelta(days=1), we)] if mid < we else [(ws, we)]


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
class ApiError(Exception):
    pass


class DailyLimit(ApiError):
    pass


class Api:
    """API 그룹(as=낙찰정보, ao=계약정보)별 호출 수를 세고 한도를 지킵니다."""

    def __init__(self, service_key, cache, max_calls):
        self.key = service_key
        self.cache = cache
        self.max_calls = max_calls
        self.run_calls = {"as": 0, "ao": 0}
        self.blocked = {}          # 그룹 → 사유 (한도 초과 등)
        self.lock = Lock()
        self.deadline = time.monotonic() + TIME_BUDGET
        self.samples = {}          # 오퍼레이션별 첫 응답 항목의 필드 목록 (진단용)

    def _group(self, path):
        return path.split("/")[0]

    def can_call(self, path):
        g = self._group(path)
        return (g not in self.blocked and self.run_calls[g] < self.max_calls
                and time.monotonic() < self.deadline)

    def _count(self, g):
        with self.lock:
            self.run_calls[g] += 1
            today = datetime.now(KST).strftime("%Y-%m-%d")
            daily = self.cache.setdefault("daily_calls", {})
            daily.setdefault(today, {"as": 0, "ao": 0})[g] += 1
            for d in list(daily):
                if d < (datetime.now(KST) - timedelta(days=7)).strftime("%Y-%m-%d"):
                    del daily[d]

    def get(self, path, params, attempts=3):
        g = self._group(path)
        if not self.can_call(path):
            raise ApiError(self.blocked.get(g) or "이번 실행 한도 도달")
        query = urllib.parse.urlencode({"serviceKey": self.key, "type": "json", **params})
        last = None
        for i in range(attempts):
            scheme = "http" if i % 2 else "https"
            self._count(g)
            try:
                req = urllib.request.Request(f"{scheme}://{API_ROOT}/{path}?{query}",
                                             headers={"User-Agent": "Mozilla/5.0 g2b-analysis"})
                with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                    text = r.read().decode("utf-8", errors="replace")
                return self._parse(path, text)
            except DailyLimit as e:
                self.blocked[g] = str(e)
                raise
            except ApiError:
                raise
            except Exception as e:  # 타임아웃·연결 오류
                last = str(e).replace(self.key, "***")
                if i < attempts - 1:
                    time.sleep(3 * (i + 1))
        raise ApiError(f"연결 실패({attempts}회): {last}")

    def _parse(self, path, text):
        text = text.strip()
        if "LIMITED_NUMBER_OF_SERVICE_REQUESTS" in text:
            raise DailyLimit("하루 호출 한도 초과 → 내일 이어서 수집")
        if not text.startswith("{"):
            m = re.search(r"<(?:returnAuthMsg|resultMsg|errMsg)>([^<]+)<", text)
            raise ApiError(m.group(1) if m else f"예상치 못한 응답: {text[:150]}")
        data = json.loads(text)
        if "OpenAPI_ServiceResponse" in data:
            h = data["OpenAPI_ServiceResponse"].get("cmmMsgHeader", {})
            msg = f"{h.get('returnReasonCode')} {h.get('errMsg')} {h.get('returnAuthMsg')}"
            if "LIMITED" in msg or str(h.get("returnReasonCode")) == "22":
                raise DailyLimit("하루 호출 한도 초과 → 내일 이어서 수집")
            raise ApiError(msg)
        resp = data.get("response") or data.get("nkoneps.com.response.ResponseError") or {}
        h = resp.get("header", {})
        if str(h.get("resultCode", "00")) not in ("00", "0", "000"):
            raise ApiError(f"{h.get('resultCode')} {h.get('resultMsg')}")
        body = resp.get("body") or {}
        items = body.get("items") or []
        if isinstance(items, dict):
            items = items.get("item") or []
        if isinstance(items, dict):
            items = [items]
        if items and path not in self.samples:
            self.samples[path] = sorted(items[0].keys())
        return items, to_int(body.get("totalCount"))

    def paged(self, path, params, rows=100, max_pages=20):
        out, page = [], 1
        while True:
            items, total = self.get(path, {**params, "pageNo": page, "numOfRows": rows})
            out.extend(items)
            if not items or page * rows >= total or page >= max_pages:
                return out
            page += 1


# ---------------------------------------------------------------------------
# 1) 낙찰 목록 수집
# ---------------------------------------------------------------------------
AWARD_FIELDS = ["bidNtceNo", "bidNtceOrd", "bidClsfcNo", "rbidNo", "bidNtceNm", "dminsttNm",
                "prtcptCnum", "bidwinnrNm", "bidwinnrBizno", "sucsfbidAmt", "sucsfbidRate",
                "rlOpengDt", "fnlSucsfDate", "bidNtceDtlUrl"]


def sweep_awards(api, cache, cfg, today):
    swept = cache.setdefault("swept", {})
    awards = cache.setdefault("awards", {})
    start = today - timedelta(days=int(365 * float(cfg["years"])))
    resweep_from = today - timedelta(days=RESWEEP_DAYS)
    todo = []
    for typ in cfg["types"]:
        for kw in cfg["keywords"]:
            for ws, we in month_windows(start, today):
                wid = f"{typ}|{kw}|{ws:%Y%m%d}"
                if swept.get(wid):
                    continue
                todo.append((typ, kw, ws, we, wid))
    log(f"1) 낙찰 목록: 조회할 구간 {len(todo)}개 (종류 {cfg['types']} × 키워드 {len(cfg['keywords'])}개 × 월 단위)")
    done = failed = new = 0
    for typ, kw, ws, we, wid in todo:
        if not api.can_call(AWARD_OPS[typ]):
            break
        def fetch(a, b, depth=0):
            params = {"inqryDiv": "1", "inqryBgnDt": f"{a:%Y%m%d}0000", "inqryEndDt": f"{b:%Y%m%d}2359",
                      "bidNtceNm": kw}
            try:
                return api.paged(AWARD_OPS[typ], params)
            except DailyLimit:
                raise
            except ApiError as e:
                # 기간 초과 오류면 구간을 반으로 나눠 다시 시도
                if ("입력범위" in str(e) or str(e).startswith("07")) and depth < 3 and a < b:
                    return [it for x, y in split_window(a, b) for it in fetch(x, y, depth + 1)]
                raise
        try:
            items = fetch(ws, we)
        except DailyLimit as e:
            log(f"  {e}")
            break
        except ApiError as e:
            failed += 1
            log(f"  [{typ}/{kw}] {ws:%Y-%m-%d}~{we:%m-%d} 실패: {e}")
            continue
        for it in items:
            name = s(it.get("bidNtceNm"))
            if kw.lower() not in name.lower():  # 서버 검색 결과를 한 번 더 확인
                continue
            k = award_key(it)
            rec = awards.get(k) or {"type": typ, "keywords": []}
            rec.update({f: s(it.get(f)) for f in AWARD_FIELDS if s(it.get(f))})
            if kw not in rec["keywords"]:
                rec["keywords"].append(kw)
            if k not in awards:
                new += 1
            awards[k] = rec
        if we < resweep_from:
            swept[wid] = 1
        done += 1
        if done % 20 == 0:  # 중간에 멈춰도 받은 만큼은 남도록
            save_json(CACHE_PATH, cache, compact=True)
    remaining = len(todo) - done - failed
    log(f"  완료 {done} · 실패 {failed} · 남음 {remaining} · 새 낙찰 {new}건 (누적 {len(awards)}건)")
    return remaining + failed


def selected_awards(cache, cfg):
    """현재 조건(키워드·금액·제외어·기간)에 맞는 낙찰 건"""
    kws = set(cfg["keywords"])
    start = (datetime.now(KST).date() - timedelta(days=int(365 * float(cfg["years"])))).isoformat()
    out = {}
    for k, a in cache.get("awards", {}).items():
        if a.get("type") not in cfg["types"] or not kws & set(a.get("keywords", [])):
            continue
        if to_int(a.get("sucsfbidAmt")) < to_int(cfg["min_amount"]):
            continue
        name = a.get("bidNtceNm", "")
        if any(x and x in name for x in cfg.get("exclude_keywords", [])):
            continue
        d = norm_date(a.get("rlOpengDt")) or norm_date(a.get("fnlSucsfDate"))
        if d and d < start:
            continue
        out[k] = a
    return out


# ---------------------------------------------------------------------------
# 2) 참여업체 · 3) 계약
# ---------------------------------------------------------------------------
PARTICIPANT_FIELDS = ["prcbdrNm", "prcbdrBizno", "opengRank", "bidprcAmt", "bidprcrt",
                      "techEvlVal", "bidPrceEvlVal", "totalEvlAmtVal", "rmrk"]


def fetch_participants(api, key, a):
    no, ord_ = a["bidNtceNo"], a.get("bidNtceOrd") or "000"
    items = api.paged(PARTICIPANT_OP, {"bidNtceNo": no, "bidNtceOrd": ord_}, rows=100, max_pages=3)
    # 같은 공고의 다른 분류·재입찰 건이 섞이면 이 낙찰 건에 해당하는 것만
    want_c, want_r = s(a.get("bidClsfcNo")), s(a.get("rbidNo"))
    def same(it):
        c, r = s(it.get("bidClsfcNo")), s(it.get("rbidNo"))
        return (not c or not want_c or c.lstrip("0") == want_c.lstrip("0")) and \
               (not r or not want_r or r.lstrip("0") == want_r.lstrip("0"))
    matched = [it for it in items if same(it)] or items
    return [{f: s(it.get(f)) for f in PARTICIPANT_FIELDS if s(it.get(f))} for it in matched]


CONTRACT_KEEP = ["untyCntrctNo", "dcsnCntrctNo", "cntrctNm", "cntrctCnclsDate", "totCntrctAmt",
                 "thtmCntrctAmt", "cntrctInsttNm", "corpList", "cntrctDtlInfoUrl", "cntrctCnclsMthdNm",
                 "cntrctPrd", "ttalScmpltDate", "thtmScmpltDate", "cntrctBgnDate", "cntrctEndDate",
                 "wkPrdEndDate", "stDate", "endDate"]


def fetch_contracts(api, a):
    items = api.paged(CONTRACT_OPS[a["type"]], {"inqryDiv": "4", "ntceNo": a["bidNtceNo"]},
                      rows=100, max_pages=2)
    out = []
    for it in items:
        rec = {f: s(it.get(f)) for f in CONTRACT_KEEP if s(it.get(f))}
        # 필드명을 모르는 기간·완료일 항목도 보존 (첫 실행 후 확정 예정)
        for f, v in it.items():
            if re.search(r"(Prd|Scmplt|EndDate|EndDt|BgnDate)", f) and s(v):
                rec[f] = s(v)
        out.append(rec)
    return out


def fetch_details(api, cache, sel):
    parts = cache.setdefault("participants", {})
    conts = cache.setdefault("contracts", {})
    order = sorted(sel.items(), key=lambda kv: -to_int(kv[1].get("sucsfbidAmt")))  # 큰 사업부터
    recent = (datetime.now(KST).date() - timedelta(days=RESWEEP_DAYS)).isoformat()  # 최근 낙찰은 계약 재확인

    def job_p(kv):
        k, a = kv
        try:
            parts[k] = fetch_participants(api, k, a)
        except ApiError as e:
            return str(e)

    def job_c(kv):
        k, a = kv
        try:
            conts[a["bidNtceNo"]] = fetch_contracts(api, a)
        except ApiError as e:
            return str(e)

    for label, todo, job, op in [
        ("2) 참여업체", [kv for kv in order if kv[0] not in parts], job_p, PARTICIPANT_OP),
        ("3) 계약", [kv for kv in order if kv[1]["bidNtceNo"] not in conts
                    or (not conts[kv[1]["bidNtceNo"]] and norm_date(kv[1].get("rlOpengDt")) >= recent)],
         job_c, CONTRACT_OPS["용역"]),
    ]:
        log(f"{label}: 조회할 공고 {len(todo)}건")
        g = op.split("/")[0]
        room = 0 if not api.can_call(op) else api.max_calls - api.run_calls[g]
        batch = todo[:max(room - 5, 0)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            errs = [e for e in pool.map(job, batch) if e]
        stop = api.blocked.get(g)
        real = [e for e in errs if e != stop and "한도" not in e]
        for e in sorted(set(real))[:5]:
            log(f"  오류: {e}")
        if stop:
            log(f"  {stop}")
        log(f"  {len(batch) - len(errs)}건 완료 · 실패 {len(real)} · 남음 {len(todo) - len(batch) + len(errs)}")


# ---------------------------------------------------------------------------
# 4) 집계
# ---------------------------------------------------------------------------
def parse_corp_list(raw):
    """'[1^주계약자^단독^업체명^대표자^...][2^...]' → [{'name','role','share','bizno'}]"""
    out = []
    for g in re.findall(r"\[([^\[\]]*)\]", s(raw)):
        f = g.split("^")
        if len(f) < 4:
            continue
        bizno = next((norm_bizno(x) for x in f if len(norm_bizno(x)) == 10), "")
        share = next((x for x in f[4:] if re.fullmatch(r"\d{1,3}(\.\d+)?", x.strip() or "x")
                      and 0 < float(x) <= 100 and x.strip() != f[0]), "")
        out.append({"name": f[3].strip(), "role": " ".join(x for x in f[1:3] if x).strip(),
                    "bizno": bizno, "share": share})
    return out


def contract_end(c):
    for f in ["ttalScmpltDate", "thtmScmpltDate", "cntrctEndDate", "wkPrdEndDate", "endDate"]:
        d = norm_date(c.get(f))
        if d:
            return d
    m = re.findall(r"\d{4}[-./]?\d{2}[-./]?\d{2}", c.get("cntrctPrd", ""))
    return norm_date(m[-1]) if m else ""


def build(cache, cfg):
    sel = selected_awards(cache, cfg)
    parts, conts = cache.get("participants", {}), cache.get("contracts", {})
    today = datetime.now(KST).date().isoformat()
    bids, rows_p, companies = [], [], {}

    # 이름 → 사업자번호 (낙찰 목록엔 사업자번호가 빠지는 경우가 있어 참여·계약 정보로 보완)
    name2biz = {}
    for plist in parts.values():
        for p in plist:
            if norm_bizno(p.get("prcbdrBizno")):
                name2biz.setdefault(norm_corp(p.get("prcbdrNm")), norm_bizno(p.get("prcbdrBizno")))
    for clist in conts.values():
        for ct in clist:
            for cp in parse_corp_list(ct.get("corpList")):
                if cp["bizno"]:
                    name2biz.setdefault(norm_corp(cp["name"]), cp["bizno"])

    def company(name, bizno):
        bizno = norm_bizno(bizno) or name2biz.get(norm_corp(name), "")
        cid = bizno or norm_corp(name)
        if not cid:
            return None
        c = companies.setdefault(cid, {"업체명": s(name), "사업자번호": norm_bizno(bizno), "참여": set(),
                                        "낙찰": set(), "낙찰금액": 0, "계약": set(), "진행중계약": set(),
                                        "기관": {}, "키워드": set(), "최근낙찰일": "", "공동수급": set()})
        if not c["사업자번호"] and norm_bizno(bizno):
            c["사업자번호"] = norm_bizno(bizno)
        return c

    for k, a in sorted(sel.items(), key=lambda kv: s(kv[1].get("rlOpengDt")), reverse=True):
        org = a.get("dminsttNm", "")
        amt = to_int(a.get("sucsfbidAmt"))
        opened = norm_date(a.get("rlOpengDt"))
        plist = parts.get(k)
        clist = conts.get(a["bidNtceNo"])

        def touch(c):
            c["기관"].setdefault(org, set()).add(k)
            c["키워드"].update(a.get("keywords", []))

        # 참여업체
        for p in plist or []:
            c = company(p.get("prcbdrNm"), p.get("prcbdrBizno"))
            if c:
                c["참여"].add(k)
                touch(c)
            rows_p.append({"공고번호": a["bidNtceNo"], "공고명": a.get("bidNtceNm", ""), "발주기관": org,
                           "개찰일": opened, "업체명": p.get("prcbdrNm", ""),
                           "사업자번호": norm_bizno(p.get("prcbdrBizno")),
                           "순위": to_int(p.get("opengRank")) or "", "투찰금액": to_int(p.get("bidprcAmt")) or "",
                           "투찰률": to_float(p.get("bidprcrt")) or "",
                           "기술점수": to_float(p.get("techEvlVal")) or "",
                           "가격점수": to_float(p.get("bidPrceEvlVal")) or "",
                           "종합점수": to_float(p.get("totalEvlAmtVal")) or "",
                           "비고": p.get("rmrk", ""),
                           "낙찰": "●" if (norm_bizno(p.get("prcbdrBizno")) and norm_bizno(p.get("prcbdrBizno")) == norm_bizno(a.get("bidwinnrBizno")))
                                   or norm_corp(p.get("prcbdrNm")) == norm_corp(a.get("bidwinnrNm")) else ""})
        # 낙찰사
        w = company(a.get("bidwinnrNm"), a.get("bidwinnrBizno"))
        if w:
            w["참여"].add(k)
            w["낙찰"].add(k)
            w["낙찰금액"] += amt
            w["최근낙찰일"] = max(w["최근낙찰일"], opened)
            touch(w)
        # 계약
        c_names, c_dates, c_amt, c_end = [], [], 0, ""
        for ct in clist or []:
            corps = parse_corp_list(ct.get("corpList"))
            end = contract_end(ct)
            c_end = max(c_end, end)
            c_dates.append(norm_date(ct.get("cntrctCnclsDate")))
            c_amt = max(c_amt, to_int(ct.get("totCntrctAmt")) or to_int(ct.get("thtmCntrctAmt")))
            for cp in corps:
                label = cp["name"] + (f"({cp['share']}%)" if cp["share"] and len(corps) > 1 else "")
                if label not in c_names:
                    c_names.append(label)
                c = company(cp["name"], cp["bizno"])
                if c:
                    c["계약"].add(k)
                    if not end or end >= today:
                        c["진행중계약"].add(k)
                    if len(corps) > 1:
                        c["공동수급"].update(x["name"] for x in corps if x["name"] != cp["name"])
                    touch(c)
        status = ""
        if clist is not None and clist:
            status = "진행중" if (c_end and c_end >= today) else ("종료" if c_end else "기간미상")
        bids.append({
            "공고번호": a["bidNtceNo"] + "-" + (a.get("bidNtceOrd") or "000"), "구분": a.get("type"),
            "공고명": a.get("bidNtceNm", ""), "키워드": ", ".join(a.get("keywords", [])),
            "발주기관": org, "개찰일": opened, "참가업체수": to_int(a.get("prtcptCnum")) or "",
            "낙찰업체": a.get("bidwinnrNm", ""), "낙찰금액": amt, "낙찰률": to_float(a.get("sucsfbidRate")) or "",
            "계약업체(공동수급)": ", ".join(c_names) if clist else ("조회 전" if clist is None else "계약정보 없음"),
            "계약일": ", ".join(sorted({d for d in c_dates if d})),
            "계약금액": c_amt or "", "계약완료일": c_end, "상태": status,
            "참여업체 수집": "완료" if plist is not None else "조회 전",
            "링크": a.get("bidNtceDtlUrl", ""),
        })

    comp_rows = []
    for c in companies.values():
        orgs = sorted(((o, len(ks)) for o, ks in c["기관"].items() if o), key=lambda x: -x[1])
        comp_rows.append({
            "업체명": c["업체명"], "사업자번호": c["사업자번호"], "참여건수": len(c["참여"]),
            "낙찰건수": len(c["낙찰"]), "수주율(%)": round(len(c["낙찰"]) / len(c["참여"]) * 100) if c["참여"] else "",
            "낙찰금액합계": c["낙찰금액"], "진행중계약": len(c["진행중계약"]), "최근낙찰일": c["최근낙찰일"],
            "주요발주기관": ", ".join(f"{o}({n})" for o, n in orgs[:3]),
            "관련키워드": ", ".join(sorted(c["키워드"])), "공동수급 파트너": ", ".join(sorted(c["공동수급"]))[:200],
        })
    comp_rows.sort(key=lambda r: (-r["낙찰금액합계"], -r["낙찰건수"], -r["참여건수"]))
    progress = {
        "공고": len(sel),
        "참여업체 수집": sum(1 for k in sel if k in parts),
        "계약 수집": sum(1 for a in sel.values() if a["bidNtceNo"] in conts),
    }
    return comp_rows, bids, rows_p, progress


# ---------------------------------------------------------------------------
# 출력
# ---------------------------------------------------------------------------
def write_xlsx(path, cfg, comp_rows, bids, rows_p, info):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    sheets = [("업체별 요약", comp_rows), ("공고별", bids), ("참여 상세", rows_p)]
    money = {"낙찰금액합계", "낙찰금액", "계약금액", "투찰금액"}
    head_fill = PatternFill("solid", fgColor="1F3A5F")
    win_fill = PatternFill("solid", fgColor="FFF4CC")
    for i, (title, rows) in enumerate(sheets):
        ws = wb.active if i == 0 else wb.create_sheet()
        ws.title = title
        cols = list(rows[0].keys()) if rows else ["(결과 없음)"]
        ws.append(cols)
        for r in rows:
            ws.append([r.get(c, "") for c in cols])
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = head_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")
        for j, c in enumerate(cols, 1):
            letter = get_column_letter(j)
            width = min(max([len(str(c))] + [len(str(r.get(c, ""))) for r in rows[:300]]) * 1.6 + 2, 60)
            ws.column_dimensions[letter].width = max(width, 8)
            if c in money:
                for cell in ws[letter][1:]:
                    cell.number_format = "#,##0"
            if c == "링크":
                for cell in ws[letter][1:]:
                    if str(cell.value).startswith("http"):
                        cell.hyperlink = cell.value
                        cell.value = "나라장터"
                        cell.font = Font(color="0563C1", underline="single")
        if title == "참여 상세" and rows:
            win_col = cols.index("낙찰") + 1
            for row in ws.iter_rows(min_row=2):
                if row[win_col - 1].value == "●":
                    for cell in row:
                        cell.fill = win_fill
        ws.freeze_panes = "A2"
        if rows:
            ws.auto_filter.ref = ws.dimensions
    ws = wb.create_sheet("조건·안내")
    for line in [
        ["분석 조건"], ["키워드", ", ".join(cfg["keywords"])], ["제외어", ", ".join(cfg.get("exclude_keywords", []))],
        ["기간", f"최근 {cfg['years']}년 (공고 게시일 기준)"], ["최소 금액", f"낙찰금액 {fmt_eok(cfg['min_amount'])} 이상"],
        ["종류", ", ".join(cfg["types"])], ["생성", info["created_at"]], [],
        ["수집 진행"], *[[k, v] for k, v in info["progress"].items()], ["상태", info["status"]], [],
        ["읽는 법"],
        ["업체별 요약", "낙찰금액합계가 큰 순. 진행중계약 = 계약완료일이 오늘 이후인 계약 수"],
        ["공고별", "상태: 진행중/종료 = 계약완료일 기준. '조회 전'은 다음 실행에서 채워짐"],
        ["참여 상세", "노란 줄 = 낙찰자. 협상계약은 기술·가격·종합점수 포함"],
        ["한계", "하도급·나라장터 외 자체조달(국방·일부 공기업)은 포함되지 않음. 수의계약은 참여업체 없이 계약만 있음"],
    ]:
        ws.append(line)
    ws.column_dimensions["A"].width = 16
    ws.column_dimensions["B"].width = 90
    for row in ws.iter_rows():
        if row and row[0].value in ("분석 조건", "수집 진행", "읽는 법"):
            row[0].font = Font(bold=True)
    wb.save(path)


def slack_summary(cfg, comp_rows, bids, info, new_awards):
    top = [r for r in comp_rows if r["낙찰건수"]][: int(cfg.get("slack_top_n", 10))]
    lines = [f":bar_chart: *나라장터 경쟁·파트너 분석* ({info['created_at'][:10]})",
             f"키워드 {', '.join(cfg['keywords'])} · 최근 {cfg['years']}년 · 낙찰 {fmt_eok(cfg['min_amount'])}↑",
             f"공고 {len(bids)}건 · 업체 {len(comp_rows)}곳 · {info['status']}", "", "*수주 상위 업체*"]
    for i, r in enumerate(top, 1):
        lines.append(f"{i}. {r['업체명']} — 낙찰 {r['낙찰건수']}건 · {fmt_eok(r['낙찰금액합계'])} · 참여 {r['참여건수']}건"
                     + (f" · 진행중 {r['진행중계약']}" if r["진행중계약"] else ""))
    if new_awards:
        lines += ["", f"*이번에 새로 확인된 낙찰 {len(new_awards)}건*"]
        for b in new_awards[:10]:
            lines.append(f"• {b['공고명']} — {b['낙찰업체']} · {fmt_eok(b['낙찰금액'])} · {b['발주기관']}")
    lines += ["", "_전체 결과: GitHub 저장소 analysis/result.xlsx_"]
    return "\n".join(lines)


def post_slack(webhook, text):
    data = json.dumps({"text": text, "unfurl_links": False}).encode("utf-8")
    req = urllib.request.Request(webhook, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------
def main():
    import signal
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))  # 취소 시에도 받은 데이터 저장
    key = os.environ.get("G2B_SERVICE_KEY", "").strip()
    if "%" in key:
        key = urllib.parse.unquote(key)
    if not key:
        sys.exit("G2B_SERVICE_KEY 가 비어 있습니다. GitHub Secrets 를 확인하세요.")
    run_kind = os.environ.get("RUN_KIND", "manual").strip() or "manual"
    cfg = load_config()
    cache = load_json(CACHE_PATH, {})
    now = datetime.now(KST)

    # 예약 실행: 수집이 끝난 상태면 매월 1일에만 새로 조회 (초기 수집 중이면 매일 이어서)
    if run_kind == "scheduled" and cache.get("complete") and now.day != 1 \
            and cache.get("config_sig") == json.dumps(cfg, sort_keys=True, ensure_ascii=False):
        log("수집 완료 상태 · 월 1회(매월 1일) 갱신일이 아니므로 종료")
        return
    if cache.get("config_sig") != json.dumps(cfg, sort_keys=True, ensure_ascii=False):
        cache["complete"] = False

    log(f"조건: 키워드 {cfg['keywords']} · {cfg['years']}년 · 낙찰 {fmt_eok(cfg['min_amount'])}↑ · {cfg['types']}")
    api = Api(key, cache, to_int(os.environ.get("MAX_CALLS") or 900))
    before = set(selected_awards(cache, cfg))

    try:
        pending_windows = sweep_awards(api, cache, cfg, now.date())
        save_json(CACHE_PATH, cache, compact=True)
        sel = selected_awards(cache, cfg)
        log(f"조건에 맞는 낙찰 공고 {len(sel)}건 (누적 수집 {len(cache.get('awards', {}))}건 중)")
        fetch_details(api, cache, sel)
    finally:
        save_json(CACHE_PATH, cache, compact=True)

    comp_rows, bids, rows_p, progress = build(cache, cfg)
    complete = (pending_windows == 0 and progress["참여업체 수집"] == progress["공고"]
                and progress["계약 수집"] == progress["공고"])
    status = "수집 완료" if complete else "수집 중 (남은 건은 다음 실행에서 이어서)"
    cache["complete"] = complete
    cache["config_sig"] = json.dumps(cfg, sort_keys=True, ensure_ascii=False)
    save_json(CACHE_PATH, cache, compact=True)

    info = {"created_at": now.isoformat(timespec="seconds"), "status": status,
            "progress": {"낙찰 목록 남은 구간": pending_windows, **progress},
            "calls": api.run_calls, "blocked": api.blocked}
    log(f"API 호출: 낙찰정보 {api.run_calls['as']}회 · 계약정보 {api.run_calls['ao']}회")
    for path, fields in api.samples.items():
        log(f"  응답 필드 [{path.split('/')[-1]}]: {', '.join(fields)}")
    log(f"결과: 공고 {len(bids)}건 · 업체 {len(comp_rows)}곳 · 참여기록 {len(rows_p)}줄 · {status}")

    write_xlsx(RESULT_XLSX, cfg, comp_rows, bids, rows_p, info)
    save_json(RESULT_JSON, {"info": info, "config": cfg, "companies": comp_rows, "bids": bids,
                            "participants": rows_p, "log": LOG_LINES})
    log("analysis/result.xlsx · result.json 저장")

    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    new_keys = set(selected_awards(cache, cfg)) - before
    if webhook and (run_kind == "manual" or complete):
        new_nos = {k.split("|")[0] for k in new_keys} if before else set()
        new_awards = [b for b in bids if b["공고번호"].split("-")[0] in new_nos]
        try:
            post_slack(webhook, slack_summary(cfg, comp_rows, bids, info, new_awards))
            log("슬랙 요약 전송")
        except Exception as e:
            log(f"슬랙 전송 실패: {e}")


if __name__ == "__main__":
    main()
