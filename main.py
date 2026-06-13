from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import yfinance as yf

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
    results = []
    try:
        symbols = " ".join(POPULAR_STOCKS)
        tickers = yf.Tickers(symbols)
        
        for symbol in POPULAR_STOCKS:
            try:
                ticker = tickers.tickers[symbol]
                info = ticker.fast_info
                price = info.last_price or 0
                volume = info.last_volume or 0
                prev_close = info.previous_close or 0
                change_pct = round((price - prev_close) / prev_close * 100, 2) if prev_close else 0
                amount = price * volume
                if amount <= 0:
                    continue
                results.append({
                    "symbol": symbol,
                    "name": symbol,
                    "price": round(float(price), 2),
                    "change": change_pct,
                    "volume": int(volume),
                    "amount": float(amount),
                    "sector": "美股",
                })
            except Exception:
                continue
    except Exception as e:
        return {"error": str(e)}

    results.sort(key=lambda x: x["amount"], reverse=True)
    return results

@app.get("/health")
def health():
    return {"status": "ok"}
