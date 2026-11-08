"""抓取 6 个板块标的近 5 年 + 14 个宽基指数近 10 年日线收盘价，生成 data/data.json。

来源：
- 新浪财经 K 线接口：银行(399986)、消费(000932)、芯片(980017)，以及宽基指数
  （上证/深成/沪深300/上证50/中证500/中证1000/中证A500/创业板指/创业板50/科创50/科创100/深证100/北证50）
- 腾讯证券 newfqkline 前复权接口：中证红利(000922，新浪该代码数据停留在 2019 年)、5G通信ETF(515050)、半导体ETF(512480)
  （两只 ETF 有份额拆分历史，新浪未复权数据会出现 -65%/-50% 的假跌幅，必须用前复权；
  腾讯前复权最多返回 640 个交易日，故两只 ETF 序列始于 2024-03，短于指数序列，前端对比图按日期对齐）
- 中证2000(932000) 在新浪/腾讯/东财均无可用公开日线源，用华夏中证2000ETF(159531) 前复权代理（腾讯，2023-09 起）

用法：python3 scripts/fetch_data.py
"""
import json
import urllib.request
import datetime
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sina(symbol, n=1300):
    url = ("http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen={n}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    d = json.load(urllib.request.urlopen(req, timeout=25))
    return [(r["day"], float(r["close"])) for r in d]


def tencent_qfq(code, n=1300):
    url = f"https://ifzq.gtimg.cn/appstock/app/newfqkline/get?param={code},day,,,{n},qfq"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    d = json.load(urllib.request.urlopen(req, timeout=30))
    node = d["data"][code]
    # 指数无复权问题；长周期请求时接口返回 day（未复权）而非 qfqday
    arr = node.get("qfqday") or node.get("day")
    return [(r[0], float(r[2])) for r in arr]


def yahoo_index(symbol):
    """Yahoo Finance chart 接口：美股指数 5 年日线（^IXIC/^NDX/^DJI/^GSPC）。
    时间戳按美东时区换算交易日；个别空值收盘跳过。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ny = ZoneInfo("America/New_York")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5y&interval=1d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    d = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())
    r = d["chart"]["result"][0]
    out = []
    for t, c in zip(r["timestamp"], r["indicators"]["quote"][0]["close"]):
        if c:
            out.append((datetime.fromtimestamp(t, ny).strftime("%Y-%m-%d"), float(c)))
    return out


def tencent_day(code, n=1300):
    """腾讯 fqkline 日线（港股指数 hkHSI / hkHSTECH 用此端点；指数无复权问题）。"""
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{n},qfq"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                "Referer": "https://gu.qq.com/"})
    d = json.load(urllib.request.urlopen(req, timeout=30))
    node = d["data"][code]
    arr = node.get("qfqday") or node.get("day")
    return [(r[0], float(r[2])) for r in arr]


def treasury_yields(n=1300):
    """中债国债收益率 10 年 / 30 年（东方财富国债收益率序列，单页上限 500 条，分页拉取）。
    字段：EMM00166466=10 年期，EMM00166469=30 年期。返回 {dates, y10, y30}（升序）。"""
    rows = {}
    for page in range(1, 6):
        url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPTA_WEB_TREASURYYIELD"
               "&columns=SOLAR_DATE,EMM00166466,EMM00166469"
               f"&sortColumns=SOLAR_DATE&sortTypes=-1&pageSize=500&pageNumber={page}")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        d = json.load(urllib.request.urlopen(req, timeout=30))
        for r in d["result"]["data"]:
            if r["EMM00166466"] is not None and r["EMM00166469"] is not None:
                rows[r["SOLAR_DATE"][:10]] = (float(r["EMM00166466"]), float(r["EMM00166469"]))
        if len(rows) >= n:
            break
    dates = sorted(rows)[-n:]
    return {"dates": dates,
            "y10": [round(rows[d][0], 4) for d in dates],
            "y30": [round(rows[d][1], 4) for d in dates]}


def chinabond_short(dates):
    """中债官网历史曲线里的 1 年 / 3 年 / 5 年国债收益率（与东财 10/30 年同一中债口径，
    2026-11-06 交叉验证：官网 10 年 1.5868 / 30 年 2.0275 与东财逐位一致）。
    接口单次最多约 1 年，按年分块拉取后对齐到 dates（缺失日向前填充）。
    失败返回 None，调用方回落到只有 10/30 年的旧结构。"""
    import re
    import time
    import datetime as _dt
    start = _dt.date.fromisoformat(dates[0])
    end = _dt.date.fromisoformat(dates[-1])
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
    if not got or dates[-1] not in got:
        return None
    out = {1: [], 3: [], 5: []}
    last = None
    for d in dates:
        if d in got:
            last = got[d]
        if last is None:  # dates 起点之前无官网数据时，用最早一条补齐
            last = got[min(got)]
        out[1].append(round(last[0], 4))
        out[3].append(round(last[1], 4))
        out[5].append(round(last[2], 4))
    return out


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
    specs = [
        ("bank", "银行", "中证银行指数", "399986", "指数", lambda: sina("sz399986")),
        ("div", "红利", "中证红利指数", "000922", "指数", lambda: tencent_qfq("sh000922")),
        ("consume", "消费", "中证主要消费指数", "000932", "指数", lambda: sina("sh000932")),
        ("telecom", "通信", "5G通信ETF", "515050", "ETF·跟踪中证5G通信主题指数", lambda: tencent_qfq("sh515050")),
        ("semi", "半导体", "半导体ETF", "512480", "ETF·跟踪中证全指半导体产品与设备指数", lambda: tencent_qfq("sh512480")),
        ("chip", "芯片", "国证芯片指数", "980017", "指数", lambda: sina("sz980017")),
        ("hsi", "恒生指数", "Hang Seng Index", "HSI", "港股指数", lambda: tencent_day("hkHSI")),
        ("hstech", "恒生科技", "Hang Seng TECH Index", "HSTECH", "港股指数", lambda: tencent_day("hkHSTECH")),
        ("ixic", "纳斯达克", "Nasdaq Composite", "IXIC", "美股指数", lambda: yahoo_index("%5EIXIC")),
        ("ndx", "纳斯达克100", "Nasdaq-100", "NDX", "美股指数", lambda: yahoo_index("%5ENDX")),
        ("dji", "道琼斯", "Dow Jones Industrial Average", "DJI", "美股指数", lambda: yahoo_index("%5EDJI")),
        ("spx", "标普500", "S&P 500 Index", "SPX", "美股指数", lambda: yahoo_index("%5EGSPC")),
    ]
    broad_specs = [
        ("sse", "上证指数", "上证综合指数", "000001", "指数", lambda: sina("sh000001", 2600)),
        ("szcomp", "深证成指", "深证成份指数", "399001", "指数", lambda: sina("sz399001", 2600)),
        ("csi300", "沪深300", "沪深300指数", "000300", "指数", lambda: sina("sh000300", 2600)),
        ("sse50", "上证50", "上证50指数", "000016", "指数", lambda: sina("sh000016", 2600)),
        ("csia500", "中证A500", "中证A500指数", "000510", "指数", lambda: sina("sh000510", 2600)),
        ("csi500", "中证500", "中证500指数", "000905", "指数", lambda: sina("sh000905", 2600)),
        ("csi1000", "中证1000", "中证1000指数", "000852", "指数", lambda: sina("sh000852", 2600)),
        ("csi2000", "中证2000", "中证2000指数", "159531", "ETF·跟踪中证2000指数", lambda: tencent_qfq("sz159531")),
        ("sz100", "深证100", "深证100指数", "399330", "指数", lambda: sina("sz399330", 2600)),
        ("cyb", "创业板指", "创业板指数", "399006", "指数", lambda: sina("sz399006", 2600)),
        ("cyb50", "创业板50", "创业板50指数", "399673", "指数", lambda: sina("sz399673", 2600)),
        ("kc50", "科创50", "科创50指数", "000688", "指数", lambda: sina("sh000688", 2600)),
        ("kc100", "科创100", "科创100指数", "000698", "指数", lambda: sina("sh000698", 2600)),
        ("bz50", "北证50", "北证50成份指数", "899050", "指数", lambda: sina("bj899050", 2600)),
    ]
    out = {"updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "series": []}
    for key, short, name, code, typ, fn in specs:
        out["series"].append(make_entry(key, short, name, code, typ, fn()))

    out["broad"] = []
    for key, short, name, code, typ, fn in broad_specs:
        out["broad"].append(make_entry(key, short, name, code, typ, fn()))

    yz = treasury_yields()
    tenor_specs = []
    try:
        cb = chinabond_short(yz["dates"])
    except Exception as e:
        cb = None
        print(f"中债官网 1/3/5 年拉取失败，仅保留 10/30 年: {e}")
    if cb:
        tenor_specs += [
            ("cn1y", "1年国债", "中债国债收益率 · 1年期", cb[1]),
            ("cn3y", "3年国债", "中债国债收益率 · 3年期", cb[3]),
            ("cn5y", "5年国债", "中债国债收益率 · 5年期", cb[5]),
        ]
    tenor_specs += [
        ("cn10y", "10年国债", "中债国债收益率 · 10年期", yz["y10"]),
        ("cn30y", "30年国债", "中债国债收益率 · 30年期", yz["y30"]),
    ]
    out["yields"] = []
    for key, short, name, vals in tenor_specs:
        last = vals[-1]

        def chg_bp(back):
            if len(vals) > back:
                return round((last - vals[-1 - back]) * 100, 1)  # 收益率变动以 bp 计
            return None

        out["yields"].append({
            "key": key, "short": short, "name": name,
            "dates": yz["dates"], "values": vals,
            "last": last, "last_date": yz["dates"][-1],
            "chg_1m_bp": chg_bp(20), "chg_3m_bp": chg_bp(60), "chg_1y_bp": chg_bp(240),
            "count": len(vals),
        })
        print(f"{short} n={len(vals)} {yz['dates'][0]}~{yz['dates'][-1]} last={last}%")

    os.makedirs(os.path.join(BASE, "data"), exist_ok=True)
    with open(os.path.join(BASE, "data", "data.json"), "w") as f:
        json.dump(out, f, ensure_ascii=False)
    print("saved -> data/data.json")


if __name__ == "__main__":
    main()
