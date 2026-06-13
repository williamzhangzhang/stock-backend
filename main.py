from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import httpx

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

POPULAR_STOCKS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA",
    "META", "TSLA", "BRK-B", "JPM", "V",
    "UNH", "XOM", "LLY", "JNJ", "WMT",
    "MA", "PG", "HD", "CVX", "MRK",
    "ABBV", "PEP", "KO", "COST", "AVGO",
    "BAC", "PFE", "TMO", "CSCO", "ACN",
    "MCD", "CRM", "ABT", "NFLX", "LIN",
    "DHR", "AMD", "TXN", "NEE", "PM",
    "ORCL", "QCOM", "UPS", "MS", "INTC",
    "INTU", "RTX", "AMGN", "GS", "CAT"
]

@app.get("/api/stocks")
async def get_stocks():
    symbols = ",".join(POPULAR_STOCKS)
    url = f"https://query1.finance.yahoo.com/v7/finance/quote"
    params = {"symbols": symbols}
    headers = {
        "User-Agent": "Mozilla/5.0"
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, params=params, headers=headers)
        data = resp.json()

    quotes = data.get("quoteResponse", {}).get("result", [])
    results = []
    for q in quotes:
        price = q.get("regularMarketPrice", 0)
        volume = q.get("regularMarketVolume", 0)
        change_pct = q.get("regularMarketChangePercent", 0)
        amount = price * volume
        results.append({
            "symbol": q.get("symbol", ""),
            "name": q.get("shortName", q.get("symbol", "")),
            "price": round(price, 2),
            "change": round(change_pct, 2),
            "volume": int(volume),
            "amount": amount,
            "sector": q.get("sector", "美股"),
        })

    results.sort(key=lambda x: x["amount"], reverse=True)
    return results

@app.get("/health")
def health():
    return {"status": "ok"}
