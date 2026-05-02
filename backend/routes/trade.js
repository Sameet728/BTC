const express = require("express");
const router = express.Router();
const Trade = require("../models/Trade");
const ExchangeAccount = require("../models/ExchangeAccount");
const BybitClient = require("../services/bybitClient");
const { getPrice, ACTIVE_EXCHANGE } = require("../services/exchange");
const { sendTelegramAlert } = require("../services/telegram");

const REQUIRED_FIELDS = ["symbol", "side", "entry", "sl", "tp", "position_size"];

// ── Helper: build a BybitClient from a DB account document ──────────────────
function buildBybitClient(acct) {
  const { apiKey, apiSecret } = acct.getDecryptedKeys();
  return new BybitClient({ apiKey, apiSecret, isTestnet: acct.isTestnet });
}

// ── Helper: get ALL active accounts and aggregate data across them ──────────
async function getActiveAccounts() {
  return await ExchangeAccount.find({ isActive: true });
}

async function getAutoTradeAccounts() {
  return await ExchangeAccount.find({ isActive: true, autoTrade: true });
}

// ── Helper: aggregate closed PnL across all active accounts ─────────────────
async function aggregateClosedPnl(symbol, limit) {
  const accounts = await getActiveAccounts();
  const allClosed = [];
  for (const acct of accounts) {
    if (acct.exchange !== "bybit") continue;
    try {
      const client = buildBybitClient(acct);
      const closed = await client.getClosedPnl(symbol, limit);
      for (const t of closed) {
        t._accountNickname = acct.nickname;
        t._isTestnet = acct.isTestnet;
      }
      allClosed.push(...closed);
    } catch (err) {
      console.error(`[AGGREGATE] Closed PnL failed for "${acct.nickname}":`, err.message);
    }
  }
  // Sort by updatedTime descending (newest first)
  allClosed.sort((a, b) => parseInt(b.updatedTime) - parseInt(a.updatedTime));
  return allClosed.slice(0, limit);
}

// ── Helper: aggregate open positions across all active accounts ─────────────
async function aggregateOpenPositions(symbol) {
  const accounts = await getActiveAccounts();
  const allOpen = [];
  for (const acct of accounts) {
    if (acct.exchange !== "bybit") continue;
    try {
      const client = buildBybitClient(acct);
      const positions = await client.getOpenPositions(symbol);
      for (const p of positions) {
        p._accountNickname = acct.nickname;
        p._accountId = acct._id.toString();
        p._isTestnet = acct.isTestnet;
      }
      allOpen.push(...positions);
    } catch (err) {
      console.error(`[AGGREGATE] Open positions failed for "${acct.nickname}":`, err.message);
    }
  }
  return allOpen;
}


// ─── POST /api/trade — Execute on all auto-trade accounts ───────────────────
router.post("/", async (req, res) => {
  const body = req.body;
  const missing = REQUIRED_FIELDS.filter((f) => body[f] === undefined);
  if (missing.length > 0)
    return res.status(400).json({ error: `Missing fields: ${missing.join(", ")}` });
  if (!["BUY", "SELL"].includes(body.side))
    return res.status(400).json({ error: "side must be BUY or SELL" });
  if (body.position_size <= 0)
    return res.status(400).json({ error: "position_size must be > 0" });

  const existing = await Trade.findOne({ symbol: body.symbol, status: "OPEN" });
  if (existing)
    return res.status(409).json({ error: `Open position already exists for ${body.symbol}`, trade_id: existing._id });

  // ── Fetch all active auto-trade accounts ─────────────────────────────────
  const accounts = await getAutoTradeAccounts();
  if (accounts.length === 0) {
    console.warn("[TRADE] No auto-trade accounts found. Signal will only be saved to DB.");
  }

  const executionResults = [];

  for (const acct of accounts) {
    const mode = acct.isTestnet ? "DEMO" : "LIVE";
    const label = `${acct.nickname} [${mode}]`;
    const result = { account: acct.nickname, exchange: acct.exchange, mode, success: false, orderId: null, tpsl: false, error: null };

    try {
      if (acct.exchange === "bybit") {
        const client = buildBybitClient(acct);

        // FIXED-RISK MODEL: Risk exactly 1.9% of balance per trade (matches backtest)
        const RISK_PER_TRADE = 0.019;
        let qty = 0.001; // absolute minimum fallback
        
        try {
          const wallet = await client.getWalletBalance("UNIFIED");
          const unified = (wallet.list || [])[0] || {};
          const availableUSDT = parseFloat(unified.totalAvailableBalance || 0);
          const slDist = Math.abs(body.entry - body.sl); // already ATR × SL_MULT from signal
          
          if (availableUSDT > 0 && slDist > 0) {
            const riskUSDT = availableUSDT * RISK_PER_TRADE;
            const calculatedQty = riskUSDT / slDist;
            // Round to 3 decimal places for BTC
            qty = parseFloat(calculatedQty.toFixed(3));
            console.log(`[TRADE] ${label} — Fixed-risk sizing: riskUSDT=$${riskUSDT.toFixed(2)}, slDist=${slDist.toFixed(2)}, qty=${qty}`);
          }
        } catch (e) {
          console.error(`[TRADE] ${label} — Failed to calculate fixed-risk qty, using minimum:`, e.message);
        }

        // Ensure qty is > 0
        if (qty <= 0) qty = 0.001;
        
        // Set leverage high enough that margin is never the constraint, but sizing drives risk
        try {
          await client.setLeverage(body.symbol, 20);
          console.log(`[TRADE] ${label} — Successfully set 20x leverage for ${body.symbol}`);
        } catch (levErr) {
          console.warn(`[TRADE] ${label} — Leverage set warning: ${levErr.message}`);
        }

        // 2. Place market order
        const order = await client.placeMarketOrder(body.symbol, body.side, qty);
        result.orderId = order.orderId;
        result.qty = qty;
        console.log(`[TRADE] ${label} — Market order placed: ${order.orderId} (qty: ${qty})`);

        // 2. Set position TP/SL (Trading Stop — appears under TP/SL tab)
        try {
          await client.setTradingStop(body.symbol, body.sl, body.tp);
          result.tpsl = true;
          console.log(`[TRADE] ${label} — TP/SL set — SL: ${body.sl}, TP: ${body.tp}`);
        } catch (tpslErr) {
          const detail = tpslErr.response ? JSON.stringify(tpslErr.response.data) : tpslErr.message;
          console.error(`[TRADE] ${label} — TP/SL failed: ${detail}`);
          result.tpslError = detail;
        }

        result.success = true;
      } else {
        console.warn(`[TRADE] ${label} — Binance auto-trade not yet implemented, skipping.`);
        result.error = "Binance auto-trade not implemented";
      }
    } catch (err) {
      const detail = err.response ? JSON.stringify(err.response.data) : err.message;
      console.error(`[TRADE] ${label} — Execution FAILED: ${detail}`);
      result.error = detail;
    }

    executionResults.push(result);
  }

  // ── Save trade to DB ───────────────────────────────────────────────────────
  try {
    const firstSuccess = executionResults.find(r => r.success);
    const trade = await Trade.create({
      symbol: body.symbol,
      side: body.side,
      entry: body.entry,
      sl: body.sl,
      tp: body.tp,
      position_size: body.position_size,
      rsi: body.rsi || null,
      atr: body.atr || null,
      exchange: "bybit",
      bybit_order_id: firstSuccess ? firstSuccess.orderId : null,
      status: "OPEN",
    });
    console.log(`[TRADE] Saved trade ${trade._id} | ${trade.symbol} ${trade.side}`);

    // ── Telegram alert ─────────────────────────────────────────────────────
    let execLines = "";
    for (const r of executionResults) {
      const status = r.success ? "✅" : "❌";
      const tpsl = r.tpsl ? "TP/SL ✅" : "TP/SL ❌";
      execLines += `\n${status} <b>${r.account}</b> [${r.mode}] — ${r.success ? `Qty: ${r.qty} | Order: ${r.orderId} | ${tpsl}` : r.error}`;
    }
    if (executionResults.length === 0) {
      execLines = "\n⚠️ No auto-trade accounts configured";
    }

    const tgMessage = `🤖 <b>SIGNAL: ${body.side} ${body.symbol}</b> 🤖\n<i>(Generated by main.py)</i>\n\n<b>Target Entry:</b> ${body.entry}\n<b>Take Profit:</b> ${body.tp}\n<b>Stop Loss:</b> ${body.sl}\n<b>Position Size:</b> ${body.position_size}\n<b>Indicators:</b> RSI ${body.rsi || 'N/A'}, ATR ${body.atr || 'N/A'}\n\n━━━━━━━━━━━━━━━━━━\n\n🚨 <b>EXECUTION (${executionResults.length} accounts)</b> 🚨${execLines}`;
    sendTelegramAlert(tgMessage);

    return res.status(201).json({ success: true, trade_id: trade._id, executions: executionResults });
  } catch (err) {
    return res.status(500).json({ error: "DB save failed", detail: err.message });
  }
});

// ─── GET /api/trade — List 50 trades (from DB) ─────────────────────────────
router.get("/", async (req, res) => {
  const trades = await Trade.find({ exchange: "bybit" }).sort({ createdAt: -1 }).limit(50);
  res.json(trades);
});

// ─── GET /api/trade/stats — Aggregate performance stats (all accounts) ──────
router.get("/stats", async (req, res) => {
  try {
    const allClosed = await aggregateClosedPnl("BTCUSDT", 100);
    const closed = allClosed.map(t => ({ pnl: parseFloat(t.closedPnl) }));
    
    // Fetch live open positions across all accounts
    const allOpen = await aggregateOpenPositions();
    const open = allOpen.length;
    
    const wins = closed.filter(t => t.pnl > 0);
    const losses = closed.filter(t => t.pnl <= 0);

    const totalPnl = closed.reduce((sum, t) => sum + t.pnl, 0);
    const grossWin = wins.reduce((sum, t) => sum + t.pnl, 0);
    const grossLoss = Math.abs(losses.reduce((sum, t) => sum + t.pnl, 0));
    const profitFactor = grossLoss > 0 ? parseFloat((grossWin / grossLoss).toFixed(2)) : grossWin > 0 ? Infinity : 0;
    const expectancy = closed.length > 0 ? parseFloat((totalPnl / closed.length).toFixed(2)) : 0;
    const winRate = closed.length > 0 ? parseFloat(((wins.length / closed.length) * 100).toFixed(1)) : 0;

    // Best / worst trades
    const pnls = closed.map(t => t.pnl);
    const bestTrade = pnls.length > 0 ? Math.max(...pnls) : 0;
    const worstTrade = pnls.length > 0 ? Math.min(...pnls) : 0;

    // Max drawdown (sequential equity-based)
    let peak = 0;
    let maxDrawdown = 0;
    let equity = 0;
    const sortedPnls = [...pnls].reverse();
    for (const p of sortedPnls) {
      equity += p;
      if (equity > peak) peak = equity;
      const dd = peak > 0 ? ((peak - equity) / peak) * 100 : 0;
      if (dd > maxDrawdown) maxDrawdown = dd;
    }

    res.json({
      totalTrades: closed.length,
      openTrades: open,
      winRate,
      totalWins: wins.length,
      totalLosses: losses.length,
      totalPnl: parseFloat(totalPnl.toFixed(2)),
      bestTrade: parseFloat(bestTrade.toFixed(2)),
      worstTrade: parseFloat(worstTrade.toFixed(2)),
      avgHoldTime: 0,
      profitFactor,
      expectancy,
      maxDrawdown: parseFloat(maxDrawdown.toFixed(1)),
      grossWin: parseFloat(grossWin.toFixed(2)),
      grossLoss: parseFloat(grossLoss.toFixed(2)),
      activeExchange: "bybit",
    });
  } catch (err) {
    console.error("[STATS]", err.message);
    res.status(500).json({ error: "Stats calculation failed" });
  }
});

// ─── GET /api/trade/equity — Equity curve data (all accounts) ───────────────
router.get("/equity", async (req, res) => {
  try {
    const allClosed = await aggregateClosedPnl("BTCUSDT", 100);
    // Sort oldest first for equity curve
    const chronological = [...allClosed].sort((a, b) => parseInt(a.updatedTime) - parseInt(b.updatedTime));
    const startingBalance = 1000;
    let equity = startingBalance;
    let peak = startingBalance;
    const points = [];

    for (let i = 0; i < chronological.length; i++) {
      const t = chronological[i];
      const pnl = parseFloat(t.closedPnl);
      equity += pnl;
      if (equity > peak) peak = equity;
      const drawdown = peak > 0 ? ((peak - equity) / peak) * 100 : 0;
      points.push({
        date: new Date(parseInt(t.updatedTime)),
        equity: parseFloat(equity.toFixed(2)),
        drawdown: parseFloat(drawdown.toFixed(2)),
        tradeIndex: i + 1,
        pnl: pnl,
        closeReason: pnl > 0 ? "TP" : "SL",
        account: t._accountNickname || "Unknown",
      });
    }

    res.json(points);
  } catch (err) {
    console.error("[EQUITY]", err.message);
    res.status(500).json({ error: "Equity curve failed" });
  }
});

// ─── GET /api/trade/open — Open positions across ALL accounts ───────────────
router.get("/open", async (req, res) => {
  try {
    const allOpen = await aggregateOpenPositions();
    const trades = allOpen.map(p => ({
      _id: (p._accountId || "") + "_" + p.symbol + "_" + p.side,
      symbol: p.symbol,
      side: p.side.toUpperCase(),
      exchange: "Bybit",
      account: p._accountNickname || "Unknown",
      isTestnet: p._isTestnet || false,
      entry: parseFloat(p.avgPrice),
      sl: parseFloat(p.stopLoss || 0),
      tp: parseFloat(p.takeProfit || 0),
      position_size: parseFloat(p.size),
      pnl: parseFloat(p.unrealisedPnl),
      createdAt: new Date(parseInt(p.createdTime))
    }));
    res.json(trades);
  } catch (err) {
    console.error("[OPEN POSITIONS]", err.message);
    res.status(500).json({ error: "Failed to fetch open trades" });
  }
});

// ─── GET /api/trade/history — Closed trade history (all accounts) ───────────
router.get("/history", async (req, res) => {
  try {
    const limit = Math.min(100, Math.max(1, parseInt(req.query.limit) || 20));
    const allClosed = await aggregateClosedPnl("BTCUSDT", limit);
    
    const trades = allClosed.map(t => {
      const positionSide = t.side === "Buy" ? "SELL" : "BUY";
      const pnl = parseFloat(t.closedPnl);
      
      return {
        _id: t.orderId,
        symbol: t.symbol,
        side: positionSide,
        exchange: "Bybit",
        account: t._accountNickname || "Unknown",
        isTestnet: t._isTestnet || false,
        entry: parseFloat(t.avgEntryPrice),
        close_price: parseFloat(t.avgExitPrice),
        sl: 0,
        tp: 0,
        position_size: parseFloat(t.closedSize),
        pnl: pnl,
        close_reason: pnl > 0 ? "TP" : "SL",
        createdAt: new Date(parseInt(t.createdTime)),
        closedAt: new Date(parseInt(t.updatedTime)),
      };
    });

    res.json({
      trades,
      total: trades.length,
      page: 1,
      pages: 1,
    });
  } catch (err) {
    console.error("[HISTORY]", err.message);
    res.status(500).json({ error: "History fetch failed" });
  }
});

// ─── GET /api/trade/monthly — Monthly PnL breakdown (all accounts) ──────────
router.get("/monthly", async (req, res) => {
  try {
    const allClosed = await aggregateClosedPnl("BTCUSDT", 100);
    const months = {};

    for (const t of allClosed) {
      const d = new Date(parseInt(t.updatedTime));
      const key = `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`;
      if (!months[key]) months[key] = { month: key, trades: 0, wins: 0, losses: 0, pnl: 0, best: -Infinity, worst: Infinity };
      months[key].trades++;
      const pnl = parseFloat(t.closedPnl);
      months[key].pnl += pnl;
      if (pnl > 0) months[key].wins++;
      if (pnl <= 0) months[key].losses++;
      if (pnl > months[key].best) months[key].best = pnl;
      if (pnl < months[key].worst) months[key].worst = pnl;
    }

    const result = Object.values(months)
      .sort((a, b) => b.month.localeCompare(a.month))
      .map(m => ({
        ...m,
        winRate: m.trades > 0 ? parseFloat(((m.wins / m.trades) * 100).toFixed(1)) : 0,
        pnl: parseFloat(m.pnl.toFixed(2)),
        best: m.best === -Infinity ? 0 : parseFloat(m.best.toFixed(2)),
        worst: m.worst === Infinity ? 0 : parseFloat(m.worst.toFixed(2)),
      }));

    res.json(result);
  } catch (err) {
    console.error("[MONTHLY]", err.message);
    res.status(500).json({ error: "Monthly breakdown failed" });
  }
});

// ─── GET /api/trade/live-price/:symbol — Current price from Bybit ───────────
router.get("/live-price/:symbol", async (req, res) => {
  try {
    const symbol = req.params.symbol.toUpperCase();
    const price = await getPrice(symbol);
    res.json({
      symbol,
      price,
      timestamp: new Date(),
      exchange: "bybit",
    });
  } catch (err) {
    console.error("[LIVE-PRICE]", err.message);
    res.status(502).json({ error: "Price fetch failed" });
  }
});

// ─── DELETE /api/trade/:id/close — Manually close a position on ALL accounts ─
router.delete("/:id/close", async (req, res) => {
  try {
    const idParts = req.params.id.split("_");
    // Format: accountId_SYMBOL_Side  or  SYMBOL_Side (legacy)
    let targetAccountId = null;
    let symbol, side;

    if (idParts.length === 3) {
      targetAccountId = idParts[0];
      symbol = idParts[1];
      side = idParts[2];
    } else if (idParts.length === 2) {
      symbol = idParts[0];
      side = idParts[1];
    } else {
      return res.status(400).json({ error: "Invalid trade ID format. Expected: SYMBOL_SIDE or ACCOUNTID_SYMBOL_SIDE" });
    }

    const closeResults = [];
    const accounts = await getActiveAccounts();

    for (const acct of accounts) {
      if (acct.exchange !== "bybit") continue;
      if (targetAccountId && acct._id.toString() !== targetAccountId) continue;

      const mode = acct.isTestnet ? "DEMO" : "LIVE";
      const label = `${acct.nickname} [${mode}]`;

      try {
        const client = buildBybitClient(acct);
        const positions = await client.getOpenPositions(symbol);
        const position = positions.find(p => p.side.toUpperCase() === side);

        if (!position) {
          closeResults.push({ account: acct.nickname, mode, success: false, error: "No matching position" });
          continue;
        }

        const closeSide = side === "BUY" ? "SELL" : "BUY";
        const order = await client.placeMarketOrder(symbol, closeSide, parseFloat(position.size));
        console.log(`[MANUAL-CLOSE] ${label} — Closed ${symbol} ${side}, order: ${order.orderId}`);
        closeResults.push({ account: acct.nickname, mode, success: true, orderId: order.orderId });
      } catch (err) {
        const detail = err.response ? JSON.stringify(err.response.data) : err.message;
        console.error(`[MANUAL-CLOSE] ${label} — Failed: ${detail}`);
        closeResults.push({ account: acct.nickname, mode, success: false, error: detail });
      }
    }

    // Housekeeping: update local MongoDB
    await Trade.updateMany(
      { symbol: symbol, side: side, status: "OPEN" },
      { $set: { status: "CLOSED", close_reason: "MANUAL", closedAt: new Date() } }
    );

    const successCount = closeResults.filter(r => r.success).length;
    let execLines = "";
    for (const r of closeResults) {
      execLines += `\n${r.success ? "✅" : "❌"} ${r.account} [${r.mode}] — ${r.success ? r.orderId : r.error}`;
    }

    const tgMessage = `📤 <b>MANUAL CLOSE: ${symbol}</b>\n\n<b>Closed on ${successCount}/${closeResults.length} accounts</b>${execLines}`;
    sendTelegramAlert(tgMessage);

    res.json({ success: successCount > 0, message: `Closed on ${successCount} account(s)`, results: closeResults });
  } catch (err) {
    console.error("[MANUAL-CLOSE]", err.message);
    res.status(500).json({ error: "Manual close failed" });
  }
});

module.exports = router;
