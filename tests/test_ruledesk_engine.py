import pytest

from ruledesk.engine import (
    DAILY_DRAWDOWN_PCT,
    MONTHLY_DRAWDOWN_PCT,
    STARTING_EQUITY,
    Clock,
    apply_month_lock_and_flatten,
    cap_qty,
    create_account,
    equity_of,
    gross_exposure,
    mark_price,
    open_position,
    place_manual_order,
    risk_state,
)

SESSION = Clock(day="2026-08-27", month="2026-08", session_open=True)
OFF_HOURS = Clock(day="2026-08-27", month="2026-08", session_open=False)


def live(symbol, price, leverage=1):
    return {"symbol": symbol, "price": price, "source": "live", "leverage": leverage}


def sim(symbol, price, leverage=1):
    return {"symbol": symbol, "price": price, "source": "sim", "leverage": leverage}


def long_lot(symbol, qty, entry, lot_id="p1", stop=0):
    return {
        "id": lot_id,
        "symbol": symbol,
        "side": "long",
        "qty": qty,
        "entry": entry,
        "stop": stop,
        "source": "manual",
    }


def account_with(*positions, **kwargs):
    clock = kwargs.pop("clock", SESSION)
    acc = create_account(clock)
    acc["positions"] = list(positions)
    acc.update(kwargs)
    return acc


def test_mark_price_never_falls_back_to_entry():
    assert mark_price(live("CRM", 250)) == 250
    assert mark_price(sim("FNGU", 420)) is None
    assert mark_price(live("CRM", 0)) is None
    assert mark_price(None) is None


def test_sim_position_is_flagged_unreliable_not_marked_at_cost():
    acc = account_with(long_lot("FNGU", 10, 100))
    snap = risk_state(acc, {"FNGU": sim("FNGU", 55, leverage=3)}, SESSION)
    pos = snap["positions"][0]
    assert pos["priced"] is False
    assert pos["price"] is None
    assert pos["unrealized_pnl"] is None
    assert snap["unreliableSymbols"] == ["FNGU"]
    # Unknown PnL does not print a fake 0% loss as a "recovery".
    assert snap["equity"] == STARTING_EQUITY
    assert snap["dailyDrawdown"] == 0


def test_scenario_a_live_3x_loss_trips_both_locks_and_scales_exposure():
    # 3,000 shares bought at 100, now 55. Cash notional 165k; economic 495k.
    acc = account_with(long_lot("TQQQ", 3_000, 100))
    quotes = {"TQQQ": live("TQQQ", 55, leverage=3)}
    snap = risk_state(acc, quotes, SESSION)

    assert snap["equity"] == 865_000
    assert snap["dailyDrawdown"] == pytest.approx(13.5)
    assert snap["monthlyDrawdown"] == pytest.approx(13.5)
    assert snap["dayLocked"] is True
    assert snap["monthLocked"] is True
    assert snap["exposure"] == pytest.approx(165_000 * 3)
    assert snap["exposurePct"] == pytest.approx(495_000 / 865_000 * 100)
    assert snap["positions"][0]["priced"] is True


def test_scenario_c_one_sim_lot_does_not_freeze_the_circuit_breaker():
    # Live book down 20% (800k notional → 640k) plus 10 sim shares of FNGU.
    acc = account_with(
        long_lot("SPY", 8_000, 100, lot_id="spy"),
        long_lot("FNGU", 10, 400, lot_id="fngu"),
    )
    quotes = {
        "SPY": live("SPY", 80),
        "FNGU": sim("FNGU", 12, leverage=3),
    }
    snap = risk_state(acc, quotes, SESSION)

    assert snap["equity"] == 840_000
    assert snap["dailyDrawdown"] == pytest.approx(16.0)
    assert snap["dayLocked"] is True
    assert snap["monthLocked"] is True
    assert snap["unreliableSymbols"] == ["FNGU"]
    assert snap["hygiene"] is True  # session open; no global freeze


def test_scenario_d_full_3x_book_reports_300_percent_economic_exposure():
    acc = account_with(long_lot("SOXL", 10_000, 100))
    quotes = {"SOXL": live("SOXL", 100, leverage=3)}
    snap = risk_state(acc, quotes, SESSION)

    assert snap["exposure"] == pytest.approx(3_000_000)
    assert snap["exposurePct"] == pytest.approx(300)
    assert snap["locked"] is False  # flat PnL, over-limit is a sizing issue not a DD lock


def test_daily_lock_layers_below_the_monthly_lock():
    # 4% live loss: trips the new 3.5% day lock, stays under the 10% month lock.
    acc = account_with(long_lot("CRM", 4_000, 100))
    quotes = {"CRM": live("CRM", 90)}
    snap = risk_state(acc, quotes, SESSION)
    assert snap["equity"] == 960_000
    assert snap["dailyDrawdown"] == pytest.approx(4.0)
    assert snap["dayLocked"] is True
    assert snap["monthLocked"] is False
    assert DAILY_DRAWDOWN_PCT == 3.5
    assert MONTHLY_DRAWDOWN_PCT == 10


def test_three_percent_daily_loss_does_not_trip_the_day_lock():
    acc = account_with(long_lot("CRM", 3_000, 100))
    quotes = {"CRM": live("CRM", 90)}
    snap = risk_state(acc, quotes, SESSION)
    assert snap["dailyDrawdown"] == pytest.approx(3.0)
    assert snap["dayLocked"] is False


def test_month_lock_stays_on_after_a_recovery():
    acc = account_with(long_lot("CRM", 2_000, 100), monthLocked=True, monthFlattenStreak=2)
    quotes = {"CRM": live("CRM", 99)}  # ~0.2% DD, well under 10%
    snap = risk_state(acc, quotes, SESSION)
    assert snap["monthlyDrawdown"] < 10
    assert snap["monthLocked"] is True


def test_sim_tick_cannot_clear_a_month_lock():
    acc = account_with(
        long_lot("FNGU", 3_000, 100),
        monthLocked=True,
        monthFlattenStreak=2,
        monthPeak=STARTING_EQUITY,
    )
    quotes = {"FNGU": sim("FNGU", 55, leverage=3)}
    next_account = apply_month_lock_and_flatten(acc, quotes, SESSION)
    assert next_account["monthLocked"] is True
    # Sim names still cannot be flattened at the seed price.
    assert len(next_account["positions"]) == 1


def test_two_live_confirming_ticks_flatten_a_month_breach():
    acc = account_with(long_lot("CRM", 20_000, 50, stop=1), monthFlattenStreak=1)
    quotes = {"CRM": live("CRM", 40)}  # -200k, DD 20%
    next_account = apply_month_lock_and_flatten(acc, quotes, SESSION)
    assert next_account["monthLocked"] is True
    assert next_account["positions"] == []
    assert next_account["realized"] == pytest.approx(-200_000)


def test_off_session_ticks_do_not_arm_locks():
    acc = account_with(long_lot("CRM", 20_000, 50))
    quotes = {"CRM": live("CRM", 40)}
    snap = risk_state(acc, quotes, OFF_HOURS)
    assert snap["dailyDrawdown"] == pytest.approx(20.0)
    assert snap["dayLocked"] is False
    assert snap["monthLocked"] is False
    assert snap["hygiene"] is False


def test_open_position_rejects_sim_quotes():
    acc = create_account(SESSION)
    result = open_position(
        acc,
        {"FNGD": sim("FNGD", 12, leverage=3)},
        {"symbol": "FNGD", "side": "long", "qtyOverride": 10, "stop": 10, "source": "manual"},
        SESSION,
    )
    assert result["ok"] is False
    assert "模拟价" in result["message"]


def test_limit_orders_cannot_rest_on_sim_quotes():
    acc = create_account(SESSION)
    result = place_manual_order(
        acc,
        {"FNGU": sim("FNGU", 420, leverage=3)},
        {
            "symbol": "FNGU",
            "side": "long",
            "qty": 10,
            "orderType": "limit",
            "limitPrice": 400,
            "stop": 380,
        },
        SESSION,
    )
    assert result["ok"] is False
    assert "模拟价" in result["message"]
    assert not (result.get("account") or {}).get("openOrders")


def test_live_market_order_fills_and_counts_leverage_against_the_book_cap():
    acc = create_account(SESSION)
    quotes = {"SOXL": live("SOXL", 100, leverage=3)}
    result = open_position(
        acc,
        quotes,
        {
            "symbol": "SOXL",
            "side": "long",
            "qtyOverride": 10_000,
            "stop": 90,
            "source": "manual",
        },
        SESSION,
    )
    assert result["ok"] is True
    # 20% economic cap / 3x = 66,666.66 cash → 666.6667 shares, not 10,000.
    assert result["qty"] == pytest.approx(STARTING_EQUITY * 0.20 / (100 * 3), rel=1e-4)
    snap = risk_state(result["account"], quotes, SESSION)
    assert snap["exposurePct"] == pytest.approx(20.0, rel=1e-3)


def test_cap_qty_shrinks_3x_names_to_the_economic_book_limit():
    qty = cap_qty(
        equity=STARTING_EQUITY,
        price=100,
        qty=10_000,
        existing_symbol_exposure=0,
        current_exposure=0,
        leverage=3,
    )
    assert qty == pytest.approx(STARTING_EQUITY * 0.20 / 300)


def test_month_lock_clears_when_the_ny_month_rolls():
    acc = account_with(long_lot("CRM", 100, 100), monthLocked=True, monthKey="2026-07")
    quotes = {"CRM": live("CRM", 100)}
    snap = risk_state(acc, quotes, SESSION)
    assert snap["monthLocked"] is False
    assert snap["monthKey"] == "2026-08"


def test_equity_ignores_unknown_marks_but_keeps_live_losses():
    acc = account_with(
        long_lot("CRM", 1_000, 100, lot_id="a"),
        long_lot("FNGU", 1_000, 100, lot_id="b"),
    )
    quotes = {"CRM": live("CRM", 90), "FNGU": sim("FNGU", 1, leverage=3)}
    assert equity_of(acc, quotes) == 990_000
    assert gross_exposure(acc["positions"], quotes) == pytest.approx(90_000 + 100_000 * 3)
