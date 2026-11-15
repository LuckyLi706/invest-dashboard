"""抓取全部标的近 10 年日线收盘价 + 中债国债收益率近 10 年，生成 data/data.json。

默认增量模式：先读现有 data/data.json，只向各数据源拉最近一段新数据，
按日期合并（新值覆盖旧值，覆盖数据源的修订），各序列保留最近 2600 条。
现有数据缺失/过旧（最后日期距今 >10 天）时该序列自动回落全量拉取；
命令行带 --full 时全部序列强制全量。增量一轮通常 <1 分钟，全量约 5 分钟。

增量安全阀：腾讯前复权序列（ETF）若遇分红重定基，历史值会整体平移——
合并前检查新旧重叠段，相对偏差 >0.5% 就判定重定基，该序列回落全量重拉，
避免拼接处出现假台阶。

来源：
- 新浪财经 K 线接口：银行(399986)、消费(000932)、芯片(980017)，以及宽基指数
  （上证/深成/沪深300/上证50/中证500/中证1000/中证A500/创业板指/创业板50/科创50/科创100/深证100/北证50）
  （芯片 980017 新浪源始于 2019-08，为该源上限）
- 腾讯证券 newfqkline 前复权接口：中证红利(000922，新浪该代码数据停留在 2019 年)、5G通信ETF(515050)、半导体ETF(512480)
  （两只 ETF 有份额拆分历史，新浪未复权数据会出现 -65%/-50% 的假跌幅，必须用前复权；
  腾讯单次最多返回 640 个交易日，全量时用 end 日期分页往前拼接：ETF 拼到上市日
  （512480 2019-06、515050 2019-10），红利指数拼满 2600 条）
- 中证2000(932000) 在新浪/腾讯/东财均无可用公开日线源，用华夏中证2000ETF(159531) 前复权代理（腾讯，2023-09 起）

用法：python3 scripts/fetch_data.py [--full]
"""
import json
import urllib.request
import datetime
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = 2600
FULL = "--full" in sys.argv


# ---------- 增量合并基础设施 ----------

def load_existing():
    """读现有 data.json，返回 {(section, key): [(date, close), ...]}，外加 yields 原始条目。"""
    path = os.path.join(BASE, "data", "data.json")
    try:
        with open(path) as f:
            j = json.load(f)
    except Exception:
        return {}, None
    out = {}
    for sec in ("series", "broad"):
        for e in j.get(sec, []):
            out[(sec, e["key"])] = list(zip(e["dates"], e["closes"]))
    return out, j.get("yields")


def fresh_enough(pts, days=10):
    if not pts:
        return False
    last = datetime.date.fromisoformat(pts[-1][0])
    return (datetime.date.today() - last).days <= days


def incremental_ok(old):
    """现有序列够新才走增量；条数不设下限（部分序列天然不足 2600 条，
    靠合并保留全部旧数据，条数只增不减）。"""
    return bool(old) and not FULL and fresh_enough(old)


def merge_pts(old, new, cap=TARGET):
    d = dict(old or [])
    for dt, v in new:
        d[dt] = v
    return sorted(d.items())[-cap:]


def qfq_consistent(old, new):
    """新旧重叠日期的相对偏差（前复权重定基会让整段历史平移）。"""
    if not old or not new:
        return True
    od = dict(old)
    worst = 0.0
    for dt, v in new:
        if dt in od and od[dt]:
            worst = max(worst, abs(v / od[dt] - 1))
    return worst <= 0.005


# ---------- 数据源 ----------

def sina(symbol, n=1300):
    url = ("http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen={n}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    d = json.load(urllib.request.urlopen(req, timeout=25))
    return [(r["day"], float(r["close"])) for r in d]


def _tencent_page(url, code):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    d = json.load(urllib.request.urlopen(req, timeout=30))
    node = d["data"][code]
    # 指数无复权问题；长周期请求时接口返回 day（未复权）而非 qfqday
    return node.get("qfqday") or node.get("day") or []


def tencent_qfq(code, target=2600):
    """腾讯 newfqkline 前复权：单次上限 640 条，用 end 日期分页往前拼接，
    直到攒够 target 条或没有更早数据（ETF 拼到上市日为止）。各页同为
    前复权口径（以最新日为锚），按日期去重后升序返回。"""
    got = {}
    end = ""
    while len(got) < target:
        arr = _tencent_page(
            f"https://ifzq.gtimg.cn/appstock/app/newfqkline/get?param={code},day,,{end},640,qfq",
            code)
        if not arr:
            break
        new = 0
        for r in arr:
            if r[0] not in got:
                got[r[0]] = float(r[2])
                new += 1
        if new <= 1:  # 只剩边界日，说明已到最早
            break
        end = arr[0][0]
    return sorted(got.items())[-target:]


def yahoo_index(symbol, rng="10y"):
    """Yahoo Finance chart 接口：美股指数日线（^IXIC/^NDX/^DJI/^GSPC）。
    时间戳按美东时区换算交易日；个别空值收盘跳过。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ny = ZoneInfo("America/New_York")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={rng}&interval=1d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    d = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())
    r = d["chart"]["result"][0]
    out = []
    for t, c in zip(r["timestamp"], r["indicators"]["quote"][0]["close"]):
        if c:
            out.append((datetime.fromtimestamp(t, ny).strftime("%Y-%m-%d"), float(c)))
    return out


def _tencent_day_page(code, end, n=640):
    param = f"param={code},day,,{end},{n},qfq"
    urls = [
        f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?{param}",
        f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?{param}",
        f"https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get?{param}",
    ]
    last_err = None
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                        "Referer": "https://gu.qq.com/"})
            d = json.load(urllib.request.urlopen(req, timeout=30))
            node = d["data"][code]
            arr = node.get("qfqday") or node.get("day")
            if arr:
                return arr
        except Exception as e:
            last_err = e
    raise RuntimeError(f"tencent_day {code} 三个主机均失败: {last_err}")


def tencent_day(code, target=2600):
    """腾讯 fqkline 日线（港股指数 hkHSI / hkHSTECH 用此端点；指数无复权问题）。
    web.ifzq.gtimg.cn 会被某些出口 IP 的 WAF 拦截（HTTP 501，本机 2026-11-10
    实测），故三个同源主机依次回落；响应结构一致。单次上限约 640 条，
    用 end 日期分页往前拼接至 target 条或无更早数据。"""
    got = {}
    end = ""
    while len(got) < target:
        arr = _tencent_day_page(code, end)
        if not arr:
            break
        new = 0
        for r in arr:
            if r[0] not in got:
                got[r[0]] = float(r[2])
                new += 1
        if new <= 1:
            break
        end = arr[0][0]
    return sorted(got.items())[-target:]


# ---------- 增量包装 ----------

def sina_inc(symbol, old):
    n = 300 if incremental_ok(old) else TARGET
    return merge_pts(old, sina(symbol, n))


def tencent_inc(code, old, day=False):
    fn = tencent_day if day else tencent_qfq
    if incremental_ok(old):
        new = fn(code, target=640)
        if qfq_consistent(old, new):
            return merge_pts(old, new)
        print(f"  {code} 前复权重定基（重叠段偏差 >0.5%），回落全量重拉")
    return merge_pts(old, fn(code, target=TARGET))


def yahoo_inc(symbol, old):
    rng = "1y" if incremental_ok(old) else "10y"
    return merge_pts(old, yahoo_index(symbol, rng=rng))


# ---------- 国债收益率 ----------

def treasury_rows(stop_date=None):
    """中债国债收益率 10 年 / 30 年（东方财富序列，单页上限 500 条，按日期倒序分页）。
    返回 {date: (y10, y30)}。增量时拉到覆盖 stop_date 即停（通常 1 页）。"""
    rows = {}
    for page in range(1, 13):
        url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPTA_WEB_TREASURYYIELD"
               "&columns=SOLAR_DATE,EMM00166466,EMM00166469"
               f"&sortColumns=SOLAR_DATE&sortTypes=-1&pageSize=500&pageNumber={page}")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        d = json.load(urllib.request.urlopen(req, timeout=30))
        for r in d["result"]["data"]:
            if r["EMM00166466"] is not None and r["EMM00166469"] is not None:
                rows[r["SOLAR_DATE"][:10]] = (float(r["EMM00166466"]), float(r["EMM00166469"]))
        if len(rows) >= TARGET:
            break
        if stop_date and rows and min(rows) <= stop_date:
            break
    return rows


def chinabond_fetch(start, end):
    """中债官网历史曲线里的 1 年 / 3 年 / 5 年国债收益率（与东财 10/30 年同一中债口径，
    2026-11-06 交叉验证：官网 10 年 1.5868 / 30 年 2.0275 与东财逐位一致）。
    接口单次最多约 1 年，按年分块拉取 [start, end]。返回 {date: (y1, y3, y5)}；
    任一块失败或最新日缺失返回 None。"""
    import re
    import time
    import datetime as _dt
    got = {}
    cur_end = end
    while cur_end >= start:
        cur_start = max(start, cur_end - _dt.timedelta(days=364))
        url = ("https://yield.chinabond.com.cn/cbweb-pbc-web/pbc/historyQuery"
               f"?startDate={cur_start}&endDate={cur_end}&gjqx=0&qxId=ycqx&locale=zh_CN")
        raw = None
        for attempt in range(3):  # 官网偶发断连，逐块重试
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://yield.chinabond.com.cn/"})
                raw = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")
                break
            except Exception:
                time.sleep(3 * (attempt + 1))
        if raw is None:
            return None  # 任一块缺失就整组回落，避免半截数据
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", raw, re.S):
            cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", c)).strip()
                     for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
            # 列：曲线名称, 日期, 3月, 6月, 1年, 3年, 5年, 7年, 10年, 30年
            if len(cells) >= 10 and cells[0] == "中债国债收益率曲线":
                try:
                    got[cells[1]] = (float(cells[4]), float(cells[5]), float(cells[6]))
                except ValueError:
                    continue
        cur_end = cur_start - _dt.timedelta(days=1)
        time.sleep(1.5)
    if not got or end.isoformat() not in got:
        return None
    return got


YIELD_TENORS = [
    ("cn1y", "1年国债", "中债国债收益率 · 1年期"),
    ("cn3y", "3年国债", "中债国债收益率 · 3年期"),
    ("cn5y", "5年国债", "中债国债收益率 · 5年期"),
    ("cn10y", "10年国债", "中债国债收益率 · 10年期"),
    ("cn30y", "30年国债", "中债国债收益率 · 30年期"),
]


def build_yields(old_entries):
    """合并新旧收益率：旧值打底，东财新行覆盖 10/30 年，中债官网新行覆盖 1/3/5 年，
    最后统一按日期升序、前向填充缺失（官网对最新日偶有延迟时用前值补齐）。"""
    old = {}  # date -> {tenor: value}
    if old_entries:
        for e in old_entries:
            for d, v in zip(e["dates"], e["values"]):
                old.setdefault(d, {})[e["key"]] = v
    old_dates = sorted(old)
    incr = bool(old_dates) and not FULL and fresh_enough([(old_dates[-1], 0)])
    cutoff = None
    if incr:
        cutoff = (datetime.date.fromisoformat(old_dates[-1])
                  - datetime.timedelta(days=45)).isoformat()

    rows = treasury_rows(stop_date=cutoff)
    for d, (a, b) in rows.items():
        old.setdefault(d, {}).update({"cn10y": round(a, 4), "cn30y": round(b, 4)})
    dates = sorted(old)[-TARGET:]
    if not dates:
        raise RuntimeError("收益率数据为空")

    cb = None
    try:
        cb_start = datetime.date.fromisoformat(cutoff) if incr else datetime.date.fromisoformat(dates[0])
        cb = chinabond_fetch(cb_start, datetime.date.fromisoformat(dates[-1]))
    except Exception as e:
        print(f"中债官网 1/3/5 年拉取失败: {e}")
    if cb:
        for d, (a, b, c) in cb.items():
            if d in old:
                old[d].update({"cn1y": round(a, 4), "cn3y": round(b, 4), "cn5y": round(c, 4)})
    elif not old_entries:
        print("中债官网 1/3/5 年不可用且无旧数据，本次仅保留 10/30 年")

    # 前向填充：carry 逐日结转；开头的缺口用全程最早一条补齐
    earliest = {}
    for d in dates:
        for k, v in old.get(d, {}).items():
            earliest.setdefault(k, v)
    series = {k: [] for k, _, _ in YIELD_TENORS}
    carry = dict(earliest)
    for d in dates:
        carry.update(old.get(d, {}))
        for k, _, _ in YIELD_TENORS:
            if k in carry:
                series[k].append(carry[k])

    out = []
    for key, short, name in YIELD_TENORS:
        vals = series[key]
        if not vals:
            continue
        last = vals[-1]

        def chg_bp(back, vals=vals, last=last):
            if len(vals) > back:
                return round((last - vals[-1 - back]) * 100, 1)  # 收益率变动以 bp 计
            return None

        out.append({
            "key": key, "short": short, "name": name,
            "dates": dates[-len(vals):], "values": vals,
            "last": last, "last_date": dates[-1],
            "chg_1m_bp": chg_bp(20), "chg_3m_bp": chg_bp(60), "chg_1y_bp": chg_bp(240),
            "count": len(vals),
        })
        print(f"{short} n={len(vals)} {dates[-len(vals)]}~{dates[-1]} last={last}%")
    return out


# ---------- 主流程 ----------

def make_entry(key, short, name, code, typ, pts):
    closes = [c for _, c in pts]
    last = closes[-1]

    def chg(back):
        if len(closes) > back:
            base = closes[-1 - back]
            return round((last - base) / base * 100, 2)
        return None

    print(f"{short} {name} n={len(pts)} {pts[0][0]}~{pts[-1][0]} last={last}")
    nd = 3 if last < 10 else 2  # ETF 价格不足 1 元时保留 3 位小数，避免曲线呈台阶状
    return {
        "key": key, "short": short, "name": name, "code": code, "type": typ,
        "dates": [d for d, _ in pts],
        "closes": [round(c, nd) for _, c in pts],
        "last": round(last, nd),
        "last_date": pts[-1][0],
        "chg_1m": chg(20), "chg_3m": chg(60), "chg_1y": chg(240),
        "count": len(pts),
    }


def main():
    existing, old_yields = load_existing()
    mode = "全量" if FULL or not existing else "增量"
    print(f"模式：{mode}")

    def S(sec, key):
        return existing.get((sec, key))

    specs = [
        ("bank", "银行", "中证银行指数", "399986", "指数", lambda: sina_inc("sz399986", S("series", "bank"))),
        ("div", "红利", "中证红利指数", "000922", "指数", lambda: tencent_inc("sh000922", S("series", "div"))),
        ("consume", "消费", "中证主要消费指数", "000932", "指数", lambda: sina_inc("sh000932", S("series", "consume"))),
        ("telecom", "通信", "5G通信ETF", "515050", "ETF·跟踪中证5G通信主题指数", lambda: tencent_inc("sh515050", S("series", "telecom"))),
        ("semi", "半导体", "半导体ETF", "512480", "ETF·跟踪中证全指半导体产品与设备指数", lambda: tencent_inc("sh512480", S("series", "semi"))),
        ("chip", "芯片", "国证芯片指数", "980017", "指数", lambda: sina_inc("sz980017", S("series", "chip"))),
        ("hsi", "恒生指数", "Hang Seng Index", "HSI", "港股指数", lambda: tencent_inc("hkHSI", S("series", "hsi"), day=True)),
        ("hstech", "恒生科技", "Hang Seng TECH Index", "HSTECH", "港股指数", lambda: tencent_inc("hkHSTECH", S("series", "hstech"), day=True)),
        ("ixic", "纳斯达克", "Nasdaq Composite", "IXIC", "美股指数", lambda: yahoo_inc("%5EIXIC", S("series", "ixic"))),
        ("ndx", "纳斯达克100", "Nasdaq-100", "NDX", "美股指数", lambda: yahoo_inc("%5ENDX", S("series", "ndx"))),
        ("dji", "道琼斯", "Dow Jones Industrial Average", "DJI", "美股指数", lambda: yahoo_inc("%5EDJI", S("series", "dji"))),
        ("spx", "标普500", "S&P 500 Index", "SPX", "美股指数", lambda: yahoo_inc("%5EGSPC", S("series", "spx"))),
    ]
    broad_specs = [
        ("sse", "上证指数", "上证综合指数", "000001", "指数", lambda: sina_inc("sh000001", S("broad", "sse"))),
        ("szcomp", "深证成指", "深证成份指数", "399001", "指数", lambda: sina_inc("sz399001", S("broad", "szcomp"))),
        ("csi300", "沪深300", "沪深300指数", "000300", "指数", lambda: sina_inc("sh000300", S("broad", "csi300"))),
        ("sse50", "上证50", "上证50指数", "000016", "指数", lambda: sina_inc("sh000016", S("broad", "sse50"))),
        ("csia500", "中证A500", "中证A500指数", "000510", "指数", lambda: sina_inc("sh000510", S("broad", "csia500"))),
        ("csi500", "中证500", "中证500指数", "000905", "指数", lambda: sina_inc("sh000905", S("broad", "csi500"))),
        ("csi1000", "中证1000", "中证1000指数", "000852", "指数", lambda: sina_inc("sh000852", S("broad", "csi1000"))),
        ("csi2000", "中证2000", "中证2000指数", "159531", "ETF·跟踪中证2000指数", lambda: tencent_inc("sz159531", S("broad", "csi2000"))),
        ("sz100", "深证100", "深证100指数", "399330", "指数", lambda: sina_inc("sz399330", S("broad", "sz100"))),
        ("cyb", "创业板指", "创业板指数", "399006", "指数", lambda: sina_inc("sz399006", S("broad", "cyb"))),
        ("cyb50", "创业板50", "创业板50指数", "399673", "指数", lambda: sina_inc("sz399673", S("broad", "cyb50"))),
        ("kc50", "科创50", "科创50指数", "000688", "指数", lambda: sina_inc("sh000688", S("broad", "kc50"))),
        ("kc100", "科创100", "科创100指数", "000698", "指数", lambda: sina_inc("sh000698", S("broad", "kc100"))),
        ("bz50", "北证50", "北证50成份指数", "899050", "指数", lambda: sina_inc("bj899050", S("broad", "bz50"))),
    ]
    out = {"updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "series": []}
    for key, short, name, code, typ, fn in specs:
        out["series"].append(make_entry(key, short, name, code, typ, fn()))

    out["broad"] = []
    for key, short, name, code, typ, fn in broad_specs:
        out["broad"].append(make_entry(key, short, name, code, typ, fn()))

    out["yields"] = build_yields(old_yields)

    os.makedirs(os.path.join(BASE, "data"), exist_ok=True)
    with open(os.path.join(BASE, "data", "data.json"), "w") as f:
        json.dump(out, f, ensure_ascii=False)
    print("saved -> data/data.json")


if __name__ == "__main__":
    main()
