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

API_KEY = "PwEfeVcpHgkUrcIxzdo91HfBcHTnsNWR"

@app.get("/api/stocks")
async def get_stocks():
    url = "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers"
    params = {
        "apiKey": API_KEY,
        "include_otc": "false"
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, params=params)
        data = resp.json()

    tickers = data.get("tickers", [])
    results = []

    for t in tickers:
        day = t.get("day", {})
        volume = day.get("v", 0)
        close = day.get("c", 0)
        change_pct = t.get("todaysChangePerc", 0)
        amount = volume * close

        if amount <= 0:
            continue

        results.append({
            "symbol": t.get("ticker", ""),
            "name": t.get("ticker", ""),
            "price": round(close, 2),
            "change": round(change_pct, 2),
            "volume": int(volume),
            "amount": amount,
            "sector": "美股",
        })

    results.sort(key=lambda x: x["amount"], reverse=True)
    return results[:500]

@app.get("/health")
def health():
    return {"status": "ok"}
