"""RuleDesk paper-trading risk engine — patched drop-in.

The live app at lab.williamchang.site is a Next.js client whose source lives in
the local `paper-trading-lab` folder, not in this repo. This module is a
faithful reconstruction of the equity risk engine recovered from the production
bundle, with the six audit fixes applied.

Copy `ruledesk/engine.ts` into paper-trading-lab (typically `lib/risk.ts`) and
wire the named exports in place of the minified helpers `R`, `O`, `C`, `M`,
`x`, `w`, and the month-lock pass inside `processTick`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Constants. DAILY_DRAWDOWN_PCT is the P2 recalibration (was 8).
# ---------------------------------------------------------------------------

DAILY_DRAWDOWN_PCT = 3.5
MONTHLY_DRAWDOWN_PCT = 10.0
MAX_EXPOSURE_PCT = 100.0
MAX_POSITION_PCT = 20.0
RISK_PER_TRADE_PCT = 0.5
STARTING_EQUITY = 1_000_000.0
MONTH_FLATTEN_CONFIRM_TICKS = 2
STORE_KEY = "ruledesk-account-v1"
AUTO_CLOSE_MAX_ADVERSE_MOVE = 0.35

LIVE = "live"
SIM = "sim"


@dataclass(frozen=True)
class Clock:
    """Injected so tests can freeze the NY session without waiting on the wall clock."""

    day: str
    month: str
    session_open: bool

    @classmethod
    def from_now(cls, now: datetime | None = None, session_open: bool | None = None):
        now = now or datetime.now(timezone.utc)
        # America/New_York via fixed offset is wrong around DST; the live app uses
        # Intl.DateTimeFormat. Callers that care should pass an explicit Clock.
        day = now.strftime("%Y-%m-%d")
        if session_open is None:
            weekday = now.weekday() < 5
            minutes = now.hour * 60 + now.minute
            session_open = weekday and 570 <= minutes < 960
        return cls(day=day, month=day[:7], session_open=session_open)


def _leverage(quote: dict | None) -> float:
    if not quote:
        return 1.0
    try:
        lev = float(quote.get("leverage") or 1)
    except (TypeError, ValueError):
        return 1.0
    return lev if lev > 0 else 1.0


def mark_price(quote: dict | None) -> float | None:
    """Live mark, or None. Never falls back to entry (that printed a fake 0% PnL)."""
    if not quote:
        return None
    if quote.get("source") != LIVE:
        return None
    try:
        price = float(quote.get("price") or 0)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def quote_source_label(quotes: dict) -> str:
    vals = [q for q in quotes.values() if q]
    if not vals:
        return "—"
    live = sum(1 for q in vals if q.get("source") == LIVE)
    if live == len(vals):
        return "LIVE"
    if live == 0:
        return "SIM"
    return "MIXED"


def _unrealized(side: str, entry: float, price: float, qty: float) -> float:
    return (price - entry) * qty if side == "long" else (entry - price) * qty


def mark_position(position: dict, quote: dict | None) -> dict:
    """Per-position valuation. Unreliable marks keep cost for capital, PnL is null."""
    price = mark_price(quote)
    qty = float(position["qty"])
    entry = float(position["entry"])
    lev = _leverage(quote)
    if price is None:
        return {
            "priced": False,
            "price": None,
            "unrealized": None,
            "notional": abs(entry * qty),
            "exposure": abs(entry * qty) * lev,
            "leverage": lev,
        }
    return {
        "priced": True,
        "price": price,
        "unrealized": _unrealized(position.get("side", "long"), entry, price, qty),
        "notional": abs(price * qty),
        "exposure": abs(price * qty) * lev,
        "leverage": lev,
    }


def equity_of(account: dict, quotes: dict) -> float:
    unrealized = 0.0
    for pos in account.get("positions") or []:
        mark = mark_position(pos, quotes.get(pos["symbol"]))
        if mark["unrealized"] is not None:
            unrealized += mark["unrealized"]
    return STARTING_EQUITY + float(account.get("realized") or 0) + unrealized


def gross_exposure(positions: Iterable[dict], quotes: dict) -> float:
    """Economic exposure: notional × leverage. Unpriced lots still count at cost."""
    total = 0.0
    for pos in positions:
        total += mark_position(pos, quotes.get(pos["symbol"]))["exposure"]
    return total


def position_snapshot(position: dict, quote: dict | None, equity: float) -> dict:
    mark = mark_position(position, quote)
    cost = float(position["qty"]) * float(position["entry"])
    snap = {
        **{k: position[k] for k in position},
        "priced": mark["priced"],
        "price": mark["price"],
        "unrealized_pnl": None if mark["unrealized"] is None else round(mark["unrealized"], 2),
        "unrealized_pnl_percent": (
            None
            if mark["unrealized"] is None or not cost
            else round(mark["unrealized"] / cost * 100, 2)
        ),
        "notional": round(mark["notional"], 2),
        "exposure": round(mark["exposure"], 2),
        "leverage": mark["leverage"],
        "weight_percent": round(mark["exposure"] / equity * 100, 2) if equity else 0.0,
    }
    return snap


def risk_state(account: dict, quotes: dict, clock: Clock) -> dict:
    """Day/month circuit breakers.

    Locks trigger only while the cash session is open, using live marks. A
    missing or sim quote on one name flags that lot unreliable — it does not
    freeze the rest of the book. Month lock is sticky until the NY month rolls,
    matching the existing day lock.
    """
    equity = equity_of(account, quotes)
    unreliable = [
        pos["symbol"]
        for pos in account.get("positions") or []
        if not mark_position(pos, quotes.get(pos["symbol"]))["priced"]
    ]

    month_peak = float(account.get("monthPeak") or equity)
    day_peak = float(account.get("dayPeak") or equity)
    day_start = float(account.get("dayStart") or equity)
    month_locked = bool(account.get("monthLocked"))
    day_locked = bool(account.get("dayLocked"))
    streak = int(account.get("monthFlattenStreak") or 0)

    if account.get("monthKey") != clock.month:
        month_locked = False
        streak = 0
        month_peak = equity
    elif clock.session_open:
        month_peak = max(month_peak, equity)

    if account.get("dayKey") != clock.day:
        day_locked = False
        day_start = equity
        day_peak = equity
    elif clock.session_open:
        day_peak = max(day_peak, equity)

    monthly_dd = max(0.0, (month_peak - equity) / month_peak * 100) if month_peak else 0.0
    daily_dd = max(0.0, (day_peak - equity) / day_peak * 100) if day_peak else 0.0
    daily_pnl = equity - day_start

    if clock.session_open:
        if monthly_dd >= MONTHLY_DRAWDOWN_PCT:
            month_locked = True
        if daily_dd >= DAILY_DRAWDOWN_PCT:
            day_locked = True

    exposure = gross_exposure(account.get("positions") or [], quotes)
    cash = max(0.0, equity - sum(
        mark_position(p, quotes.get(p["symbol"]))["notional"]
        for p in account.get("positions") or []
    ))
    locked = month_locked or day_locked
    lock_kinds = []
    if day_locked:
        lock_kinds.append("day")
    if month_locked:
        lock_kinds.append("month")

    return {
        "equity": equity,
        "monthPeak": month_peak,
        "dayPeak": day_peak,
        "dayStart": day_start,
        "monthKey": clock.month,
        "dayKey": clock.day,
        "monthLocked": month_locked,
        "dayLocked": day_locked,
        "monthFlattenStreak": streak,
        "monthlyDrawdown": monthly_dd,
        "dailyDrawdown": daily_dd,
        "dailyPnl": daily_pnl,
        "dailyPct": (daily_pnl / day_start * 100) if day_start else 0.0,
        "exposure": exposure,
        "exposurePct": (exposure / equity * 100) if equity else 0.0,
        "cash": cash,
        "cashPct": (cash / equity * 100) if equity else 0.0,
        "locked": locked,
        "lockKinds": lock_kinds,
        "sessionOpen": clock.session_open,
        "hygiene": clock.session_open,
        "sourceLabel": quote_source_label(quotes),
        "unreliableSymbols": sorted(set(unreliable)),
        "positions": [
            position_snapshot(p, quotes.get(p["symbol"]), equity)
            for p in account.get("positions") or []
        ],
    }


def persist_risk_fields(snap: dict) -> dict:
    return {
        "monthKey": snap["monthKey"],
        "dayKey": snap["dayKey"],
        "monthPeak": snap["monthPeak"],
        "dayPeak": snap["dayPeak"],
        "dayStart": snap["dayStart"],
        "monthLocked": snap["monthLocked"],
        "dayLocked": snap["dayLocked"],
        "monthFlattenStreak": snap["monthFlattenStreak"],
    }


def size_by_stop(
    *,
    equity: float,
    price: float,
    stop: float,
    side: str,
    existing_symbol_exposure: float,
    current_exposure: float,
    leverage: float = 1.0,
) -> float:
    if not (price > 0 and stop > 0):
        return 0.0
    if side == "long" and not (stop < price):
        return 0.0
    if side == "short" and not (stop > price):
        return 0.0
    stop_dist = abs(price - stop)
    if stop_dist <= 0:
        return 0.0
    lev = leverage if leverage > 0 else 1.0
    unit_exposure = price * lev
    by_position = max(0.0, MAX_POSITION_PCT * equity / 100 - existing_symbol_exposure) / unit_exposure
    by_book = max(0.0, MAX_EXPOSURE_PCT * equity / 100 - current_exposure) / unit_exposure
    by_risk = (RISK_PER_TRADE_PCT * equity / 100) / stop_dist
    return max(0.0, min(by_risk, by_position, by_book))


def cap_qty(
    *,
    equity: float,
    price: float,
    qty: float,
    existing_symbol_exposure: float,
    current_exposure: float,
    leverage: float = 1.0,
) -> float:
    if not (price > 0 and qty > 0):
        return 0.0
    lev = leverage if leverage > 0 else 1.0
    unit_exposure = price * lev
    return max(
        0.0,
        min(
            qty,
            max(0.0, MAX_POSITION_PCT * equity / 100 - existing_symbol_exposure) / unit_exposure,
            max(0.0, MAX_EXPOSURE_PCT * equity / 100 - current_exposure) / unit_exposure,
        ),
    )


def stop_side_ok(side: str, ref_price: float, stop: float) -> bool:
    return bool(stop > 0 and ref_price > 0) and (
        stop < ref_price if side == "long" else stop > ref_price
    )


def _reject(message: str) -> dict:
    return {"ok": False, "message": message}


def open_position(account: dict, quotes: dict, order: dict, clock: Clock) -> dict:
    snap = risk_state(account, quotes, clock)
    if snap["locked"]:
        return _reject("已触发回撤锁，禁止新增仓位。")
    if account.get("entriesPaused"):
        return _reject("只平不开已开启，禁止新增仓位。")

    symbol = order["symbol"]
    quote = quotes.get(symbol)
    if not quote:
        return _reject("没有可用报价。")
    if quote.get("source") != LIVE:
        return _reject(f"{symbol} 当前是模拟价，禁止开仓/加仓。请等 LIVE 行情。")

    fill = float(order["fillPrice"]) if order.get("fillPrice") else float(quote.get("price") or 0)
    if not (fill > 0):
        return _reject("成交价无效。")
    if mark_price(quote) is None and not order.get("fillPrice"):
        return _reject(f"{symbol} 无 LIVE 报价，禁止开仓/加仓。")

    stop = float(order["stop"]) if order.get("stop") and order["stop"] > 0 else 0.0
    side = order["side"]
    if stop > 0 and not stop_side_ok(side, fill, stop):
        return _reject("保护止损须低于成交价。" if side == "long" else "保护止损须高于成交价。")

    lev = _leverage(quote)
    existing_symbol_exposure = sum(
        mark_position(p, quotes.get(p["symbol"]))["exposure"]
        for p in account.get("positions") or []
        if p["symbol"] == symbol
    )
    qty_override = order.get("qtyOverride")
    if qty_override is not None and float(qty_override) > 0:
        raw = cap_qty(
            equity=snap["equity"],
            price=fill,
            qty=float(qty_override),
            existing_symbol_exposure=existing_symbol_exposure,
            current_exposure=snap["exposure"],
            leverage=lev,
        )
    else:
        raw = size_by_stop(
            equity=snap["equity"],
            price=fill,
            stop=stop,
            side=side,
            existing_symbol_exposure=existing_symbol_exposure,
            current_exposure=snap["exposure"],
            leverage=lev,
        )
    qty = round(max(0.0, raw), 8)
    if not (qty > 0):
        return _reject("股数或仓位上限不足，无法开仓。")

    opened_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    positions = list(account.get("positions") or [])
    existing = next((p for p in positions if p["symbol"] == symbol and p["side"] == side), None)

    if existing:
        new_qty = existing["qty"] + qty
        new_entry = (existing["entry"] * existing["qty"] + fill * qty) / new_qty
        new_stop = existing["stop"]
        if stop > 0:
            if order.get("source") == "auto":
                pct = abs(fill - stop) / fill
                new_stop = new_entry * (1 - pct) if side == "long" else new_entry * (1 + pct)
            else:
                new_stop = stop
        position_id = existing["id"]
        remaining = new_qty
        positions = [
            {
                **p,
                "qty": new_qty,
                "entry": new_entry,
                "stop": new_stop,
                "openedAt": opened_at,
                "takeProfitTiers": [],
                "peakReturnPct": 0,
                "timeStopReduced": False,
            }
            if p["id"] == existing["id"]
            else p
            for p in positions
        ]
    else:
        position_id = uuid.uuid4().hex[:12]
        remaining = qty
        positions = [
            {
                "id": position_id,
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "entry": fill,
                "stop": stop,
                "openedAt": opened_at,
                "source": order.get("source", "manual"),
                "sleeve": order.get("sleeve"),
                "takeProfitTiers": [],
                "peakReturnPct": 0,
            },
            *positions,
        ]

    notional = fill * qty
    entry = {
        "id": uuid.uuid4().hex[:12],
        "positionId": position_id,
        "symbol": symbol,
        "side": side,
        "action": "买入" if side == "long" else "卖空",
        "qty": qty,
        "price": fill,
        "amount": notional,
        "pnl": None,
        "equityPct": round(notional / snap["equity"] * 100, 2) if snap["equity"] else None,
        "reason": order.get("reason"),
        "occurredAt": opened_at,
        "remainingQty": remaining,
    }
    next_account = {
        **account,
        **persist_risk_fields(snap),
        "positions": positions,
        "ledger": [entry, *(account.get("ledger") or [])][:2000],
    }
    return {
        "ok": True,
        "qty": qty,
        "message": f"已模拟成交 {symbol} {qty} 股 @ {fill:.2f}",
        "account": next_account,
    }


def place_manual_order(account: dict, quotes: dict, spec: dict, clock: Clock) -> dict:
    qty = float(spec.get("qty") or 0)
    if not (qty > 0):
        return _reject("请输入有效股数。")
    stop = float(spec["stop"]) if spec.get("stop") and spec["stop"] > 0 else 0.0
    quote = quotes.get(spec["symbol"])
    if not quote:
        return _reject("没有可用报价。")
    # P6: SIM names cannot even rest a limit — FNGU/FNGD used to sneak onto the book.
    if quote.get("source") != LIVE:
        return _reject(f"{spec['symbol']} 当前是模拟价，禁止开仓/加仓。请等 LIVE 行情。")

    if spec.get("orderType", "market") == "market":
        return open_position(
            account,
            quotes,
            {
                "symbol": spec["symbol"],
                "side": spec["side"],
                "stop": stop,
                "source": "manual",
                "reason": "手动市价单",
                "qtyOverride": qty,
            },
            clock,
        )

    limit_price = float(spec.get("limitPrice") or 0)
    if not (limit_price > 0):
        return _reject("请输入有效限价。")
    if stop > 0 and not stop_side_ok(spec["side"], limit_price, stop):
        return _reject(
            "保护止损须低于限价。" if spec["side"] == "long" else "保护止损须高于限价。"
        )

    snap = risk_state(account, quotes, clock)
    if snap["locked"]:
        return _reject("已触发回撤锁，禁止新增仓位。")
    if account.get("entriesPaused"):
        return _reject("只平不开已开启，禁止新增仓位。")

    price = float(quote.get("price") or 0)
    crossed = price <= limit_price if spec["side"] == "long" else price >= limit_price
    if crossed:
        return open_position(
            account,
            quotes,
            {
                "symbol": spec["symbol"],
                "side": spec["side"],
                "stop": stop,
                "source": "manual",
                "reason": f"手动限价单即时成交（限价 {limit_price:.2f}）",
                "qtyOverride": qty,
                "fillPrice": price,
            },
            clock,
        )

    working = {
        "id": uuid.uuid4().hex[:12],
        "symbol": spec["symbol"],
        "side": spec["side"],
        "type": "limit",
        "qty": qty,
        "limitPrice": limit_price,
        "stop": stop,
        "createdAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dayKey": clock.day,
    }
    return {
        "ok": True,
        "message": (
            f"已挂限价{'买入' if spec['side'] == 'long' else '卖出'} "
            f"{spec['symbol']} {qty} 股 @ {limit_price:.2f}（当日有效）。"
        ),
        "account": {
            **account,
            **persist_risk_fields(snap),
            "openOrders": [working, *(account.get("openOrders") or [])],
        },
    }


def close_position(
    account: dict,
    quotes: dict,
    position_id: str,
    pct: float,
    reason: str,
    clock: Clock,
    patch: dict | None = None,
) -> dict:
    pos = next((p for p in account.get("positions") or [] if p["id"] == position_id), None)
    if not pos:
        return _reject("仓位不存在。")
    quote = quotes.get(pos["symbol"])
    looks_auto = any(
        token in reason
        for token in ("自动", "强制", "止盈", "止损", "回撤", "时间", "月回撤", "日回撤")
    )
    px = mark_price(quote)
    if px is None:
        return _reject(
            f"{pos['symbol']} 无 LIVE 报价，已跳过自动平仓（避免种子模拟价假成交）。"
            if looks_auto
            else f"{pos['symbol']} 无 LIVE 报价，暂不能平仓。请等行情恢复后再试。"
        )
    if looks_auto:
        entry = float(pos["entry"])
        adverse = (
            (entry - px) / entry if pos["side"] == "long" else (px - entry) / entry
        ) if entry > 0 else float("inf")
        if not (entry > 0) or adverse > AUTO_CLOSE_MAX_ADVERSE_MOVE:
            return _reject(
                f"{pos['symbol']} 成交价 {px:.2f} 相对成本 {pos['entry']:.2f} "
                "偏离过大，已拒绝自动平仓（疑似脏行情）。"
            )

    qty = pos["qty"] if pct >= 100 else round(max(1e-8 * pos["qty"], pos["qty"] * pct / 100), 6)
    pnl = _unrealized(pos["side"], float(pos["entry"]), px, qty)
    remaining = round(pos["qty"] - qty, 6)
    if remaining > 1e-10:
        positions = [
            {**p, "qty": remaining, **(patch or {})} if p["id"] == pos["id"] else p
            for p in account["positions"]
        ]
    else:
        positions = [p for p in account["positions"] if p["id"] != pos["id"]]

    snap = risk_state(account, quotes, clock)
    return {
        "ok": True,
        "account": {
            **account,
            **persist_risk_fields(snap),
            "realized": float(account.get("realized") or 0) + pnl,
            "positions": positions,
            "ledger": [
                {
                    "id": uuid.uuid4().hex[:12],
                    "positionId": pos["id"],
                    "symbol": pos["symbol"],
                    "side": pos["side"],
                    "action": "卖出" if pos["side"] == "long" else "回补",
                    "qty": qty,
                    "price": px,
                    "amount": px * qty,
                    "pnl": pnl,
                    "reason": reason,
                    "occurredAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "remainingQty": remaining if remaining > 1e-10 else 0,
                },
                *(account.get("ledger") or []),
            ][:2000],
        },
    }


def apply_month_lock_and_flatten(account: dict, quotes: dict, clock: Clock) -> dict:
    """Sticky month lock + 2-tick flatten. Recovery no longer auto-unlocks."""
    snap = risk_state(account, quotes, clock)
    next_account: dict[str, Any] = {**account, **persist_risk_fields(snap)}

    if clock.session_open and snap["monthlyDrawdown"] >= MONTHLY_DRAWDOWN_PCT:
        next_account["monthLocked"] = True
        next_account["monthFlattenStreak"] = int(next_account.get("monthFlattenStreak") or 0) + 1
    elif not clock.session_open:
        # Off-session ticks must not reset the streak or clear the lock.
        pass
    # Recovered monthly DD no longer clears monthLocked (P1). Streak holds.

    if (
        clock.session_open
        and int(next_account.get("monthFlattenStreak") or 0) >= MONTH_FLATTEN_CONFIRM_TICKS
        and snap["monthlyDrawdown"] >= MONTHLY_DRAWDOWN_PCT
    ):
        reason = f"月回撤 {MONTHLY_DRAWDOWN_PCT:g}% 强制平仓（连续 2 次盘中 LIVE 确认）"
        for pos in list(next_account.get("positions") or []):
            result = close_position(next_account, quotes, pos["id"], 100, reason, clock)
            if result.get("ok"):
                next_account = result["account"]
                next_account["monthLocked"] = True
    return next_account


def create_account(clock: Clock) -> dict:
    equity = STARTING_EQUITY
    return {
        "version": 1,
        "realized": 0.0,
        "monthPeak": equity,
        "monthKey": clock.month,
        "monthLocked": False,
        "dayLocked": False,
        "dayStart": equity,
        "dayPeak": equity,
        "dayKey": clock.day,
        "monthFlattenStreak": 0,
        "positions": [],
        "ledger": [],
        "openOrders": [],
        "entriesPaused": False,
        "coreHoldSymbols": [],
    }
