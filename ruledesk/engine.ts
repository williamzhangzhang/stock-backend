/**
 * RuleDesk equity risk engine — patched drop-in for paper-trading-lab.
 *
 * Replace the minified helpers in the Next.js source:
 *   R  → markPrice
 *   O  → (deleted as a global freeze; use clock.sessionOpen)
 *   C  → grossExposure
 *   M  → equityOf
 *   x  → riskState
 *   w  → openPosition
 *   month-lock pass in processTick → applyMonthLockAndFlatten
 *
 * Fixes vs the production bundle (2026-08-27, lab.williamchang.site):
 *   1. Missing/sim quotes flag that lot unreliable; they no longer freeze the book.
 *   2. markPrice returns null instead of falling back to entry.
 *   3. Exposure is notional × leverage.
 *   4. Month lock is sticky until the NY month rolls (same as the day lock).
 *   5. Daily drawdown lock is 3.5% (was 8%) so it layers under the 10% month lock.
 *   6. SIM names cannot open, add, or rest a limit order.
 */

export const DAILY_DRAWDOWN_PCT = 3.5;
export const MONTHLY_DRAWDOWN_PCT = 10;
export const MAX_EXPOSURE_PCT = 100;
export const MAX_POSITION_PCT = 20;
export const RISK_PER_TRADE_PCT = 0.5;
export const STARTING_EQUITY = 1_000_000;
export const MONTH_FLATTEN_CONFIRM_TICKS = 2;
export const STORE_KEY = "ruledesk-account-v1";
export const AUTO_CLOSE_MAX_ADVERSE_MOVE = 0.35;

export type Quote = {
  symbol: string;
  price: number;
  source: "live" | "sim";
  leverage?: number;
};

export type Clock = {
  day: string;
  month: string;
  sessionOpen: boolean;
};

export function markPrice(quote?: Quote | null): number | null {
  if (!quote || quote.source !== "live") return null;
  return quote.price > 0 ? quote.price : null;
}

function leverageOf(quote?: Quote | null): number {
  const lev = quote?.leverage ?? 1;
  return lev > 0 ? lev : 1;
}

function unrealized(side: string, entry: number, price: number, qty: number): number {
  return (side === "long" ? price - entry : entry - price) * qty;
}

export function markPosition(position: any, quote?: Quote | null) {
  const price = markPrice(quote);
  const qty = Number(position.qty);
  const entry = Number(position.entry);
  const lev = leverageOf(quote);
  if (price == null) {
    return {
      priced: false,
      price: null as number | null,
      unrealized: null as number | null,
      notional: Math.abs(entry * qty),
      exposure: Math.abs(entry * qty) * lev,
      leverage: lev,
    };
  }
  return {
    priced: true,
    price,
    unrealized: unrealized(position.side ?? "long", entry, price, qty),
    notional: Math.abs(price * qty),
    exposure: Math.abs(price * qty) * lev,
    leverage: lev,
  };
}

export function equityOf(account: any, quotes: Record<string, Quote>): number {
  const u = (account.positions ?? []).reduce((sum: number, p: any) => {
    const mark = markPosition(p, quotes[p.symbol]);
    return sum + (mark.unrealized ?? 0);
  }, 0);
  return STARTING_EQUITY + Number(account.realized ?? 0) + u;
}

export function grossExposure(positions: any[], quotes: Record<string, Quote>): number {
  return positions.reduce((sum, p) => sum + markPosition(p, quotes[p.symbol]).exposure, 0);
}

export function quoteSourceLabel(quotes: Record<string, Quote>): string {
  const vals = Object.values(quotes);
  if (!vals.length) return "—";
  const live = vals.filter((q) => q.source === "live").length;
  return live === vals.length ? "LIVE" : live === 0 ? "SIM" : "MIXED";
}

export function riskState(account: any, quotes: Record<string, Quote>, clock: Clock) {
  const equity = equityOf(account, quotes);
  const unreliableSymbols = [
    ...new Set(
      (account.positions ?? [])
        .filter((p: any) => !markPosition(p, quotes[p.symbol]).priced)
        .map((p: any) => p.symbol)
    ),
  ].sort();

  let monthPeak = Number(account.monthPeak ?? equity);
  let dayPeak = Number(account.dayPeak ?? equity);
  let dayStart = Number(account.dayStart ?? equity);
  let monthLocked = !!account.monthLocked;
  let dayLocked = !!account.dayLocked;
  let monthFlattenStreak = Number(account.monthFlattenStreak ?? 0);

  if (account.monthKey !== clock.month) {
    monthLocked = false;
    monthFlattenStreak = 0;
    monthPeak = equity;
  } else if (clock.sessionOpen) {
    monthPeak = Math.max(monthPeak, equity);
  }

  if (account.dayKey !== clock.day) {
    dayLocked = false;
    dayStart = equity;
    dayPeak = equity;
  } else if (clock.sessionOpen) {
    dayPeak = Math.max(dayPeak, equity);
  }

  const monthlyDrawdown = monthPeak > 0 ? Math.max(0, ((monthPeak - equity) / monthPeak) * 100) : 0;
  const dailyDrawdown = dayPeak > 0 ? Math.max(0, ((dayPeak - equity) / dayPeak) * 100) : 0;
  const dailyPnl = equity - dayStart;

  if (clock.sessionOpen) {
    if (monthlyDrawdown >= MONTHLY_DRAWDOWN_PCT) monthLocked = true;
    if (dailyDrawdown >= DAILY_DRAWDOWN_PCT) dayLocked = true;
  }

  const exposure = grossExposure(account.positions ?? [], quotes);
  const cash = Math.max(
    0,
    equity -
      (account.positions ?? []).reduce(
        (s: number, p: any) => s + markPosition(p, quotes[p.symbol]).notional,
        0
      )
  );
  const locked = monthLocked || dayLocked;
  const lockKinds: Array<"day" | "month"> = [];
  if (dayLocked) lockKinds.push("day");
  if (monthLocked) lockKinds.push("month");

  return {
    equity,
    monthPeak,
    dayPeak,
    dayStart,
    monthKey: clock.month,
    dayKey: clock.day,
    monthLocked,
    dayLocked,
    monthFlattenStreak,
    monthlyDrawdown,
    dailyDrawdown,
    dailyPnl,
    dailyPct: dayStart > 0 ? (dailyPnl / dayStart) * 100 : 0,
    exposure,
    exposurePct: equity > 0 ? (exposure / equity) * 100 : 0,
    cash,
    cashPct: equity > 0 ? (cash / equity) * 100 : 0,
    locked,
    lockKinds,
    sessionOpen: clock.sessionOpen,
    hygiene: clock.sessionOpen,
    sourceLabel: quoteSourceLabel(quotes),
    unreliableSymbols,
    positions: (account.positions ?? []).map((p: any) => {
      const mark = markPosition(p, quotes[p.symbol]);
      const cost = Number(p.qty) * Number(p.entry);
      return {
        ...p,
        priced: mark.priced,
        price: mark.price,
        unrealized_pnl: mark.unrealized == null ? null : Number(mark.unrealized.toFixed(2)),
        unrealized_pnl_percent:
          mark.unrealized == null || !cost ? null : Number(((mark.unrealized / cost) * 100).toFixed(2)),
        notional: Number(mark.notional.toFixed(2)),
        exposure: Number(mark.exposure.toFixed(2)),
        leverage: mark.leverage,
        weight_percent: equity ? Number(((mark.exposure / equity) * 100).toFixed(2)) : 0,
      };
    }),
  };
}

export function persistRiskFields(snap: ReturnType<typeof riskState>) {
  return {
    monthKey: snap.monthKey,
    dayKey: snap.dayKey,
    monthPeak: snap.monthPeak,
    dayPeak: snap.dayPeak,
    dayStart: snap.dayStart,
    monthLocked: snap.monthLocked,
    dayLocked: snap.dayLocked,
    monthFlattenStreak: snap.monthFlattenStreak,
  };
}

export function capQty(args: {
  equity: number;
  price: number;
  qty: number;
  existingSymbolExposure: number;
  currentExposure: number;
  leverage?: number;
}): number {
  const { equity, price, qty, existingSymbolExposure, currentExposure, leverage = 1 } = args;
  if (!(price > 0 && qty > 0)) return 0;
  const unit = price * (leverage > 0 ? leverage : 1);
  return Math.max(
    0,
    Math.min(
      qty,
      Math.max(0, (MAX_POSITION_PCT * equity) / 100 - existingSymbolExposure) / unit,
      Math.max(0, (MAX_EXPOSURE_PCT * equity) / 100 - currentExposure) / unit
    )
  );
}

function reject(message: string) {
  return { ok: false as const, message };
}

export function openPosition(account: any, quotes: Record<string, Quote>, order: any, clock: Clock) {
  const snap = riskState(account, quotes, clock);
  if (snap.locked) return reject("已触发回撤锁，禁止新增仓位。");
  if (account.entriesPaused) return reject("只平不开已开启，禁止新增仓位。");

  const quote = quotes[order.symbol];
  if (!quote) return reject("没有可用报价。");
  if (quote.source !== "live") {
    return reject(`${order.symbol} 当前是模拟价，禁止开仓/加仓。请等 LIVE 行情。`);
  }

  const fill = order.fillPrice ?? quote.price;
  if (!(fill > 0)) return reject("成交价无效。");

  const stop = order.stop > 0 ? order.stop : 0;
  if (stop > 0) {
    const ok = order.side === "long" ? stop < fill : stop > fill;
    if (!ok) {
      return reject(order.side === "long" ? "保护止损须低于成交价。" : "保护止损须高于成交价。");
    }
  }

  const lev = leverageOf(quote);
  const existingSymbolExposure = (account.positions ?? [])
    .filter((p: any) => p.symbol === order.symbol)
    .reduce((s: number, p: any) => s + markPosition(p, quotes[p.symbol]).exposure, 0);

  const raw =
    order.qtyOverride != null && order.qtyOverride > 0
      ? capQty({
          equity: snap.equity,
          price: fill,
          qty: order.qtyOverride,
          existingSymbolExposure,
          currentExposure: snap.exposure,
          leverage: lev,
        })
      : 0;
  const qty = Number(Math.max(0, raw).toFixed(8));
  if (!(qty > 0)) return reject("股数或仓位上限不足，无法开仓。");

  const openedAt = new Date().toISOString();
  const existing = (account.positions ?? []).find(
    (p: any) => p.symbol === order.symbol && p.side === order.side
  );
  let positions: any[];
  let positionId: string;
  let remainingQty: number;

  if (existing) {
    const newQty = existing.qty + qty;
    const newEntry = (existing.entry * existing.qty + fill * qty) / newQty;
    positionId = existing.id;
    remainingQty = newQty;
    positions = account.positions.map((p: any) =>
      p.id === existing.id
        ? { ...p, qty: newQty, entry: newEntry, stop: stop > 0 ? stop : p.stop, openedAt }
        : p
    );
  } else {
    positionId = crypto.randomUUID();
    remainingQty = qty;
    positions = [
      {
        id: positionId,
        symbol: order.symbol,
        side: order.side,
        qty,
        entry: fill,
        stop,
        openedAt,
        source: order.source ?? "manual",
        sleeve: order.sleeve,
        takeProfitTiers: [],
        peakReturnPct: 0,
      },
      ...(account.positions ?? []),
    ];
  }

  return {
    ok: true as const,
    qty,
    message: `已模拟成交 ${order.symbol} ${qty} 股 @ ${fill.toFixed(2)}`,
    account: {
      ...account,
      ...persistRiskFields(snap),
      positions,
      ledger: [
        {
          id: crypto.randomUUID(),
          positionId,
          symbol: order.symbol,
          side: order.side,
          action: order.side === "long" ? "买入" : "卖空",
          qty,
          price: fill,
          amount: fill * qty,
          remainingQty,
          reason: order.reason,
          occurredAt: openedAt,
        },
        ...(account.ledger ?? []),
      ].slice(0, 2000),
    },
  };
}

export function placeManualOrder(account: any, quotes: Record<string, Quote>, spec: any, clock: Clock) {
  const qty = Number(spec.qty);
  if (!(qty > 0)) return reject("请输入有效股数。");
  const quote = quotes[spec.symbol];
  if (!quote) return reject("没有可用报价。");
  if (quote.source !== "live") {
    return reject(`${spec.symbol} 当前是模拟价，禁止开仓/加仓。请等 LIVE 行情。`);
  }
  if ((spec.orderType ?? "market") === "market") {
    return openPosition(
      account,
      quotes,
      {
        symbol: spec.symbol,
        side: spec.side,
        stop: spec.stop,
        source: "manual",
        reason: "手动市价单",
        qtyOverride: qty,
      },
      clock
    );
  }
  return reject("限价单路径请接回 paper-trading-lab 的 openOrders 队列。");
}

export function closePosition(
  account: any,
  quotes: Record<string, Quote>,
  positionId: string,
  pct: number,
  reason: string,
  clock: Clock
) {
  const pos = (account.positions ?? []).find((p: any) => p.id === positionId);
  if (!pos) return reject("仓位不存在。");
  const px = markPrice(quotes[pos.symbol]);
  const looksAuto = /自动|强制|止盈|止损|回撤|时间|月回撤|日回撤/.test(reason);
  if (px == null) {
    return reject(
      looksAuto
        ? `${pos.symbol} 无 LIVE 报价，已跳过自动平仓（避免种子模拟价假成交）。`
        : `${pos.symbol} 无 LIVE 报价，暂不能平仓。请等行情恢复后再试。`
    );
  }
  const qty = pct >= 100 ? pos.qty : Number(Math.max(1e-8 * pos.qty, (pos.qty * pct) / 100).toFixed(6));
  const pnl = unrealized(pos.side, pos.entry, px, qty);
  const remainingQty = Number((pos.qty - qty).toFixed(6));
  const positions =
    remainingQty > 1e-10
      ? account.positions.map((p: any) => (p.id === pos.id ? { ...p, qty: remainingQty } : p))
      : account.positions.filter((p: any) => p.id !== pos.id);
  const snap = riskState(account, quotes, clock);
  return {
    ok: true as const,
    account: {
      ...account,
      ...persistRiskFields(snap),
      realized: Number(account.realized ?? 0) + pnl,
      positions,
    },
  };
}

export function applyMonthLockAndFlatten(account: any, quotes: Record<string, Quote>, clock: Clock) {
  const snap = riskState(account, quotes, clock);
  let next = { ...account, ...persistRiskFields(snap) };
  if (clock.sessionOpen && snap.monthlyDrawdown >= MONTHLY_DRAWDOWN_PCT) {
    next.monthLocked = true;
    next.monthFlattenStreak = (next.monthFlattenStreak || 0) + 1;
  }
  if (
    clock.sessionOpen &&
    next.monthFlattenStreak >= MONTH_FLATTEN_CONFIRM_TICKS &&
    snap.monthlyDrawdown >= MONTHLY_DRAWDOWN_PCT
  ) {
    const reason = `月回撤 ${MONTHLY_DRAWDOWN_PCT}% 强制平仓（连续 2 次盘中 LIVE 确认）`;
    for (const p of [...(next.positions ?? [])]) {
      const res = closePosition(next, quotes, p.id, 100, reason, clock);
      if (res.ok) next = { ...res.account, monthLocked: true };
    }
  }
  return next;
}
