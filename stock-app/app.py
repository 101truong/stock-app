
"""
Ứng dụng báo cáo phân tích cổ phiếu.
 
- Cổ phiếu Việt Nam: dữ liệu từ thư viện vnstock (nguồn VCI)
- Cổ phiếu quốc tế: dữ liệu từ yfinance (Yahoo Finance)
- Tin tức 24 giờ qua: Google News RSS
- Tâm lý thị trường: Claude (nếu có ANTHROPIC_API_KEY), nếu không thì dùng quy tắc đơn giản
 
Chạy: uvicorn app:app --reload
"""
import json
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote
from xml.etree import ElementTree as ET
 
import pandas as pd
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
 
load_dotenv()
 
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "45"))
_cache = {}
 
app = FastAPI(title="Phân tích cổ phiếu")
 
 
# ---------------------------------------------------------------- chỉ báo kỹ thuật
def calc_rsi(closes, n=14):
    s = pd.Series(closes, dtype="float64")
    if len(s) < n + 1:
        return None
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    last_dn = dn.iloc[-1]
    if last_dn == 0:
        return 100.0
    rs = up.iloc[-1] / last_dn
    return float(100 - 100 / (1 + rs))
 
 
def sma(closes, n):
    s = pd.Series(closes, dtype="float64").rolling(n).mean()
    return [None if math.isnan(x) else float(x) for x in s]
 
 
def rule_trend(price, sma5, sma20, rsi_v):
    """Nhận định xu hướng ngắn hạn theo quy tắc đơn giản (dự phòng khi không có AI)."""
    pts, notes = 0.0, []
    if sma5 is not None and sma20 is not None:
        if sma5 > sma20:
            pts += 1
            notes.append("SMA5 nằm trên SMA20, đà tăng ngắn hạn còn")
        else:
            pts -= 1
            notes.append("SMA5 nằm dưới SMA20, đà giảm ngắn hạn chiếm ưu thế")
    if price is not None and sma20 is not None:
        if price > sma20:
            pts += 1
            notes.append("giá đang trên đường trung bình 20 phiên")
        else:
            pts -= 1
            notes.append("giá đang dưới đường trung bình 20 phiên")
    if rsi_v is not None:
        if rsi_v >= 70:
            pts -= 0.5
            notes.append(f"RSI {rsi_v:.0f} ở vùng quá mua, dễ có nhịp điều chỉnh")
        elif rsi_v <= 30:
            pts += 0.5
            notes.append(f"RSI {rsi_v:.0f} ở vùng quá bán, có thể xuất hiện nhịp hồi")
        else:
            notes.append(f"RSI {rsi_v:.0f} ở vùng trung tính")
    label = "Tích cực" if pts >= 1.5 else "Tiêu cực" if pts <= -1.5 else "Trung lập"
    text = (notes[0][0].upper() + notes[0][1:] if notes else "Chưa đủ dữ liệu") + (
        "; " + "; ".join(notes[1:]) if len(notes) > 1 else ""
    ) + ". Chỉ mang tính tham khảo."
    return label, text
 
 
POS_WORDS = ["tăng", "lãi", "lợi nhuận", "kỷ lục", "vượt", "tích cực", "mua ròng", "khuyến nghị mua",
             "surge", "beat", "record", "upgrade", "rally", "growth", "gain", "profit", "strong"]
NEG_WORDS = ["giảm", "lỗ", "bán ròng", "sụt", "rủi ro", "điều tra", "phạt", "hạ", "nợ xấu",
             "plunge", "miss", "downgrade", "lawsuit", "probe", "fall", "loss", "weak", "cut"]
 
 
def keyword_sentiment(titles):
    score = 50
    for t in titles:
        low = t.lower()
        score += 8 * sum(w in low for w in POS_WORDS)
        score -= 8 * sum(w in low for w in NEG_WORDS)
    score = max(0, min(100, score))
    label = "Tích cực" if score >= 60 else "Tiêu cực" if score <= 40 else "Trung lập"
    return score, label
 
 
# ---------------------------------------------------------------- dữ liệu giá
VND_API = "https://finfo-api.vndirect.com.vn/v4"
VND_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Referer": "https://dstock.vndirect.com.vn/",
    "Origin": "https://dstock.vndirect.com.vn",
}
 
 
def _vn_prices_vndirect(sym):
    r = requests.get(
        f"{VND_API}/stock_prices",
        params={"sort": "date", "size": 150, "page": 1, "q": f"code:{sym}"},
        headers=VND_HEADERS,
        timeout=6,
    )
    r.raise_for_status()
    rows = []
    for d in r.json().get("data", []):
        close = d.get("close") or d.get("adClose")
        if not close:
            continue
        rows.append(
            {
                "date": str(d["date"])[:10],
                "close": float(close),
                "high": float(d.get("high") or close),
                "low": float(d.get("low") or close),
                "volume": float(d.get("nmVolume") or d.get("volume") or 0),
            }
        )
    rows.sort(key=lambda x: x["date"])
    return rows
 
 
def _vn_prices_vci(sym):
    now = int(time.time())
    headers = dict(VND_HEADERS)
    headers["Referer"] = "https://trading.vietcap.com.vn/"
    headers["Origin"] = "https://trading.vietcap.com.vn"
    headers["Content-Type"] = "application/json"
    variants = [
        {"timeFrame": "ONE_DAY", "symbols": [sym], "to": now, "countBack": 120},
        {"timeFrame": "ONE_DAY", "symbols": [sym], "to": now + 86400, "countBack": 120},
        {"timeFrame": "ONE_DAY", "symbols": [sym], "to": now, "countBack": 100},
        {"timeFrame": "ONE_DAY", "symbols": [sym], "to": now, "countBack": 60},
        {"timeFrame": "ONE_DAY", "symbols": [sym], "to": str(now), "countBack": 120},
    ]
    last = ""
    for payload in variants:
        r = requests.post(
            "https://trading.vietcap.com.vn/api/chart/OHLCChart/gap",
            json=payload,
            headers=headers,
            timeout=8,
        )
        if r.status_code != 200:
            last = f"HTTP {r.status_code}: {r.text[:150]}"
            continue
        data = r.json()
        d = data[0] if isinstance(data, list) and data else data
        rows = []
        for t, o, h, l, c, v in zip(d["t"], d["o"], d["h"], d["l"], d["c"], d["v"]):
            rows.append(
                {
                    "date": datetime.fromtimestamp(int(t)).strftime("%Y-%m-%d"),
                    "close": float(c),
                    "high": float(h),
                    "low": float(l),
                    "volume": float(v),
                }
            )
        rows.sort(key=lambda x: x["date"])
        if rows:
            return rows
        last = "phản hồi rỗng"
    raise RuntimeError(last or "không rõ lỗi")
 
 
def _vn_fundamentals(sym, price):
    """Lấy P/E, EPS, cổ tức, vốn hóa từ VNDirect (khớp theo tên chỉ số)."""
    out = {"pe": None, "eps": None, "dividend_yield": None, "market_cap": None}
    try:
        r = requests.get(
            f"{VND_API}/ratios/latest",
            params={"order": "reportDate", "where": f"code:{sym}", "size": 300},
            headers=VND_HEADERS,
            timeout=6,
        )
        r.raise_for_status()
        for it in r.json().get("data", []):
            name = f"{it.get('itemName', '')} {it.get('itemVnName', '')}".lower()
            val = it.get("value")
            if not isinstance(val, (int, float)):
                continue
            if out["pe"] is None and ("p/e" in name or "price to earning" in name) and "diluted" not in name:
                out["pe"] = float(val)
            elif out["eps"] is None and "eps" in name and "growth" not in name and "tăng" not in name:
                out["eps"] = float(val)
            elif out["dividend_yield"] is None and ("dividend yield" in name or "tỷ suất cổ tức" in name):
                out["dividend_yield"] = float(val)
            elif out["market_cap"] is None and ("market cap" in name or "vốn hóa" in name):
                out["market_cap"] = float(val)
        if out["market_cap"] is not None and out["market_cap"] < 1e10:
            out["market_cap"] *= 1e9  # nguồn trả theo đơn vị tỷ đồng
        if out["pe"] is None and out["eps"]:
            out["pe"] = price / out["eps"]
    except Exception:
        pass
    return out
 
 
def _vn_prices_tcbs(sym):
    now = int(time.time())
    r = requests.get(
        "https://apipubaws.tcbs.com.vn/stock-insight/v1/stock/bars-long-term",
        params={
            "ticker": sym,
            "type": "stock",
            "resolution": "D",
            "from": now - 150 * 86400,
            "to": now + 86400,
        },
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json",
                 "Origin": "https://tcinvest.tcbs.com.vn", "Referer": "https://tcinvest.tcbs.com.vn/"},
        timeout=6,
    )
    r.raise_for_status()
    rows = []
    for d in r.json().get("data", []):
        if not d.get("close"):
            continue
        rows.append(
            {
                "date": str(d.get("tradingDate"))[:10],
                "close": float(d["close"]),
                "high": float(d.get("high") or d["close"]),
                "low": float(d.get("low") or d["close"]),
                "volume": float(d.get("volume") or 0),
            }
        )
    rows.sort(key=lambda x: x["date"])
    return rows
 
 
FIREANT_BASE = "https://restv2.fireant.vn"
 
 
def _fireant_headers():
    token = os.getenv("FIREANT_TOKEN", "").strip().strip('"')
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        raise RuntimeError("chưa đặt FIREANT_TOKEN")
    return {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
 
 
def _vn_prices_fireant(sym):
    end = datetime.now().date()
    start = end - timedelta(days=200)
    r = requests.get(
        f"{FIREANT_BASE}/symbols/{sym}/historical-quotes",
        params={"startDate": start.isoformat(), "endDate": end.isoformat(), "offset": 0, "limit": 150},
        headers=_fireant_headers(),
        timeout=10,
    )
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:120]}")
    rows = []
    for d in r.json():
        close = d.get("priceClose") or d.get("close")
        if not close:
            continue
        rows.append(
            {
                "date": str(d.get("date"))[:10],
                "close": float(close),
                "high": float(d.get("priceHigh") or d.get("high") or close),
                "low": float(d.get("priceLow") or d.get("low") or close),
                "volume": float(d.get("totalVolume") or d.get("dealVolume") or d.get("volume") or 0),
            }
        )
    rows.sort(key=lambda x: x["date"])
    return rows
 
 
def _vn_fundamentals_fireant(sym):
    out = {"pe": None, "eps": None, "dividend_yield": None, "market_cap": None}
    name = None
    try:
        r = requests.get(
            f"{FIREANT_BASE}/symbols/{sym}/fundamental",
            headers=_fireant_headers(),
            timeout=10,
        )
        if r.status_code != 200:
            return out, None
        d = r.json()
        name = d.get("companyName") or d.get("name")
        for k, v in d.items():
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                continue
            kl = k.lower()
            if kl == "pe" and out["pe"] is None:
                out["pe"] = float(v)
            elif kl == "eps" and out["eps"] is None:
                out["eps"] = float(v)
            elif "dividend" in kl and "yield" in kl and out["dividend_yield"] is None:
                out["dividend_yield"] = float(v)
            elif kl == "marketcap" and out["market_cap"] is None:
                out["market_cap"] = float(v)
    except Exception:
        pass
    return out, name
 
 
def fetch_vn(sym):
    rows, errors = [], []
    for name, getter in (
        ("FireAnt", _vn_prices_fireant),
        ("VCI", _vn_prices_vci),
        ("VNDirect", _vn_prices_vndirect),
        ("TCBS", _vn_prices_tcbs),
    ):
        try:
            rows = getter(sym)
            if rows:
                break
            errors.append(f"{name}: không có dữ liệu")
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__} {str(e)[:120]}")
    if not rows:
        raise HTTPException(
            404,
            f"Không lấy được dữ liệu giá cho mã {sym}. Chi tiết: " + " | ".join(errors),
        )
    # Một số nguồn trả giá theo đơn vị nghìn đồng
    if max(r["close"] for r in rows) < 1000:
        for r in rows:
            for k in ("close", "high", "low"):
                r[k] *= 1000
    price = rows[-1]["close"]
    fund, name = _vn_fundamentals_fireant(sym)
    if not any(v is not None for v in fund.values()):
        fund = _vn_fundamentals(sym, price)
    return {"rows": rows, "fund": fund, "name": name or sym, "currency": "VND"}
 
 
def fetch_us(sym):
    import yfinance as yf
 
    t = yf.Ticker(sym)
    df = t.history(period="6mo", interval="1d")
    if df is None or df.empty:
        raise HTTPException(404, f"Không tìm thấy dữ liệu giá cho mã {sym}")
    rows = [
        {
            "date": idx.strftime("%Y-%m-%d"),
            "close": float(r["Close"]),
            "high": float(r["High"]),
            "low": float(r["Low"]),
            "volume": float(r["Volume"]),
        }
        for idx, r in df.iterrows()
    ]
    info = {}
    try:
        info = t.info or {}
    except Exception:
        pass
    div = info.get("trailingAnnualDividendYield")
    fund = {
        "pe": info.get("trailingPE"),
        "eps": info.get("trailingEps"),
        "dividend_yield": div * 100 if isinstance(div, (int, float)) else None,
        "market_cap": info.get("marketCap"),
    }
    return {
        "rows": rows,
        "fund": fund,
        "name": info.get("longName") or info.get("shortName") or sym,
        "currency": info.get("currency", "USD"),
    }
 
 
# ---------------------------------------------------------------- tin tức
def fetch_news(sym, market):
    if market == "vn":
        q, hl, gl, ceid = f"cổ phiếu {sym} when:1d", "vi", "VN", "VN:vi"
    else:
        q, hl, gl, ceid = f"{sym} stock when:1d", "en-US", "US", "US:en"
    url = f"https://news.google.com/rss/search?q={quote(q)}&hl={hl}&gl={gl}&ceid={ceid}"
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        src_el = it.find("source")
        source = src_el.text.strip() if src_el is not None and src_el.text else ""
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate"))
            if pub.tzinfo is None:
                pub = pub.replace(tzinfo=timezone.utc)
        except Exception:
            pub = None
        if pub and pub < cutoff:
            continue
        if title:
            items.append(
                {
                    "title": title,
                    "link": link,
                    "source": source,
                    "published": pub.isoformat() if pub else None,
                    "summary": "",
                }
            )
        if len(items) >= 15:
            break
    return items
 
 
# ---------------------------------------------------------------- phân tích bằng Claude
def llm_analyze(sym, name, news, tech):
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key or not news:
        return None
    try:
        import anthropic
 
        client = anthropic.Anthropic(api_key=key)
        headlines = "\n".join(
            f"{i}. {n['title']} (nguồn: {n['source'] or 'không rõ'})" for i, n in enumerate(news)
        )
        prompt = f"""Bạn là chuyên viên phân tích chứng khoán. Mã: {sym} ({name}).
Chỉ báo kỹ thuật hiện tại: {json.dumps(tech, ensure_ascii=False)}
 
Các tiêu đề tin trong 24 giờ qua:
{headlines}
 
Nhiệm vụ:
1. Chọn đúng 3 tin quan trọng nhất ảnh hưởng đến giá mã này (bỏ tin trùng lặp hoặc không liên quan trực tiếp).
2. Với mỗi tin, viết 1 câu tóm tắt tác động bằng lời của bạn. Chỉ dựa trên tiêu đề, không suy diễn chi tiết không có trong tiêu đề.
3. Đánh giá tâm lý thị trường từ 0 (rất tiêu cực) đến 100 (rất tích cực).
4. Viết nhận định xu hướng ngắn hạn 2-3 câu, kết hợp tin tức và chỉ báo kỹ thuật, nêu rõ chỉ mang tính tham khảo.
 
Chỉ trả về JSON thuần, đúng dạng:
{{"top": [{{"index": 0, "summary": "..."}}, ...], "sentiment_score": 55, "sentiment_label": "Tích cực|Trung lập|Tiêu cực", "trend_view": "..."}}"""
        msg = client.messages.create(
            model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
            max_tokens=900,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        data = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
        picked = []
        for t in data.get("top", [])[:3]:
            idx = int(t["index"])
            item = dict(news[idx])
            item["summary"] = str(t.get("summary", ""))
            picked.append(item)
        return {
            "news": picked,
            "score": max(0, min(100, int(data["sentiment_score"]))),
            "label": str(data["sentiment_label"]),
            "trend_view": str(data["trend_view"]),
        }
    except Exception:
        return None
 
 
# ---------------------------------------------------------------- API
def build_report(sym, market):
    data = fetch_vn(sym) if market == "vn" else fetch_us(sym)
    rows = data["rows"]
    closes = [r["close"] for r in rows]
    s5, s20 = sma(closes, 5), sma(closes, 20)
    rsi_v = calc_rsi(closes)
    last = rows[-1]
    prev_close = rows[-2]["close"] if len(rows) > 1 else last["close"]
    change = last["close"] - prev_close
 
    tech = {
        "gia_dong_cua": last["close"],
        "sma5": s5[-1],
        "sma20": s20[-1],
        "rsi14": rsi_v,
        "bien_dong_phien_pct": round(change / prev_close * 100, 2) if prev_close else None,
    }
    all_news = fetch_news(sym, market)
    ai = llm_analyze(sym, data["name"], all_news, tech)
    if ai:
        news, score, label, view, method = ai["news"], ai["score"], ai["label"], ai["trend_view"], "claude"
    else:
        news = all_news[:3]
        score, label = keyword_sentiment([n["title"] for n in news])
        _, view = rule_trend(last["close"], s5[-1], s20[-1], rsi_v)
        method = "quy-tac"
 
    n_chart = 60
    chart = [
        {"date": r["date"], "close": r["close"], "sma5": a, "sma20": b}
        for r, a, b in list(zip(rows, s5, s20))[-n_chart:]
    ]
    return {
        "symbol": sym,
        "market": market,
        "name": data["name"],
        "currency": data["currency"],
        "price": last["close"],
        "change": change,
        "change_pct": (change / prev_close * 100) if prev_close else None,
        "prev_close": prev_close,
        "day_high": last["high"],
        "day_low": last["low"],
        "volume": last["volume"],
        "as_of": last["date"],
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "indicators": {"rsi14": rsi_v, "sma5": s5[-1], "sma20": s20[-1]},
        "fundamentals": data["fund"],
        "chart": chart,
        "news": news,
        "sentiment": {"score": score, "label": label, "trend_view": view, "method": method},
    }
 
 
@app.get("/api/report/{symbol}")
def report(symbol: str, market: str = "auto"):
    sym = symbol.strip().upper()
    if not re.fullmatch(r"[A-Z0-9.\-]{1,10}", sym):
        raise HTTPException(400, "Mã cổ phiếu không hợp lệ")
    if market not in ("auto", "vn", "us"):
        raise HTTPException(400, "market phải là auto, vn hoặc us")
    if market == "auto":
        market = "vn" if re.fullmatch(r"[A-Z]{3}", sym) else "us"
 
    key = (sym, market)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    try:
        result = build_report(sym, market)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Không lấy được dữ liệu từ nguồn: {e}")
    _cache[key] = (time.time(), result)
    return result
 
 
@app.get("/")
def index():
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))
 
 
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
 
