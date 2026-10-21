"""抓取 6 个板块标的的近一年日线收盘价，生成 data/data.json。

来源：
- 新浪财经 K 线接口：银行(399986)、消费(000932)、芯片(980017)、5G通信ETF(515050)、半导体ETF(512480)
- 腾讯证券 newfqkline 接口：中证红利(000922)（新浪该代码数据停留在 2019 年，故用腾讯源）

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


def main():
    specs = [
        ("bank", "银行", "中证银行指数", "399986", "指数", lambda: sina("sz399986")),
        ("div", "红利", "中证红利指数", "000922", "指数", lambda: tencent_qfq("sh000922")),
        ("consume", "消费", "中证主要消费指数", "000932", "指数", lambda: sina("sh000932")),
        ("telecom", "通信", "5G通信ETF", "515050", "ETF·跟踪中证5G通信主题指数", lambda: sina("sh515050")),
        ("semi", "半导体", "半导体ETF", "512480", "ETF·跟踪中证全指半导体产品与设备指数", lambda: sina("sh512480")),
        ("chip", "芯片", "国证芯片指数", "980017", "指数", lambda: sina("sz980017")),
    ]
    out = {"updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "series": []}
    for key, short, name, code, typ, fn in specs:
        pts = fn()
        closes = [c for _, c in pts]
        last = closes[-1]

        def chg(back):
            if len(closes) > back:
                base = closes[-1 - back]
                return round((last - base) / base * 100, 2)
            return None

        out["series"].append({
            "key": key, "short": short, "name": name, "code": code, "type": typ,
            "dates": [d for d, _ in pts],
            "closes": [round(c, 2) for _, c in pts],
            "last": round(last, 2),
            "last_date": pts[-1][0],
            "chg_1m": chg(20), "chg_3m": chg(60), "chg_1y": chg(240),
            "count": len(pts),
        })
        print(f"{short} {name} n={len(pts)} {pts[0][0]}~{pts[-1][0]} last={last}")

    os.makedirs(os.path.join(BASE, "data"), exist_ok=True)
    with open(os.path.join(BASE, "data", "data.json"), "w") as f:
        json.dump(out, f, ensure_ascii=False)
    print("saved -> data/data.json")


if __name__ == "__main__":
    main()
