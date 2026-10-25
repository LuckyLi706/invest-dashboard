"""抓取 6 个板块标的 + 14 个宽基指数的近 5 年日线收盘价，生成 data/data.json。

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
    ]
    broad_specs = [
        ("sse", "上证指数", "上证综合指数", "000001", "指数", lambda: sina("sh000001")),
        ("szcomp", "深证成指", "深证成份指数", "399001", "指数", lambda: sina("sz399001")),
        ("csi300", "沪深300", "沪深300指数", "000300", "指数", lambda: sina("sh000300")),
        ("sse50", "上证50", "上证50指数", "000016", "指数", lambda: sina("sh000016")),
        ("csia500", "中证A500", "中证A500指数", "000510", "指数", lambda: sina("sh000510")),
        ("csi500", "中证500", "中证500指数", "000905", "指数", lambda: sina("sh000905")),
        ("csi1000", "中证1000", "中证1000指数", "000852", "指数", lambda: sina("sh000852")),
        ("csi2000", "中证2000", "中证2000指数", "159531", "ETF·跟踪中证2000指数", lambda: tencent_qfq("sz159531")),
        ("sz100", "深证100", "深证100指数", "399330", "指数", lambda: sina("sz399330")),
        ("cyb", "创业板指", "创业板指数", "399006", "指数", lambda: sina("sz399006")),
        ("cyb50", "创业板50", "创业板50指数", "399673", "指数", lambda: sina("sz399673")),
        ("kc50", "科创50", "科创50指数", "000688", "指数", lambda: sina("sh000688")),
        ("kc100", "科创100", "科创100指数", "000698", "指数", lambda: sina("sh000698")),
        ("bz50", "北证50", "北证50成份指数", "899050", "指数", lambda: sina("bj899050")),
    ]
    out = {"updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "series": []}
    for key, short, name, code, typ, fn in specs:
        out["series"].append(make_entry(key, short, name, code, typ, fn()))

    out["broad"] = []
    for key, short, name, code, typ, fn in broad_specs:
        out["broad"].append(make_entry(key, short, name, code, typ, fn()))

    yz = treasury_yields()
    out["yields"] = []
    for key, short, name, vals in [
        ("cn10y", "10年国债", "中债国债收益率 · 10年期", yz["y10"]),
        ("cn30y", "30年国债", "中债国债收益率 · 30年期", yz["y30"]),
    ]:
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
