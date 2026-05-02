const express = require("express");
const router = express.Router();
const Heartbeat = require("../models/Heartbeat");
const Trade = require("../models/Trade");
const { getLogs, clearLogs } = require("../services/logger");
const { sendTelegramAlert } = require("../services/telegram");

// POST /api/health/heartbeat — Python VPS pings this every loop cycle
router.post("/heartbeat", async (req, res) => {
  try {
    const { symbol, interval, lastSignal, atrPct, rsi, logs, lastTradeDetails } = req.body;
    await Heartbeat.create({
      timestamp: new Date(),
      symbol: symbol || "BTCUSDT",
      interval: interval || "1h",
      lastSignal: lastSignal || null,
      atrPct: atrPct || null,
      rsi: rsi || null,
      logs: logs || [],
      lastTradeDetails: lastTradeDetails || null,
    });
    res.json({ ok: true });
  } catch (err) {
    console.error("[HEALTH] Heartbeat save failed:", err.message);
    res.status(500).json({ error: "Heartbeat save failed" });
  }
});

// GET /api/health/bot — Frontend polls for VPS status
router.get("/bot", async (req, res) => {
  try {
    const latest = await Heartbeat.findOne().sort({ timestamp: -1 });

    if (!latest) {
      return res.json({
        status: "OFFLINE",
        lastHeartbeat: null,
        isAlive: false,
        uptimeHours: 0,
        symbol: null,
        interval: null,
        rsi: null,
        atrPct: null,
        lastSignal: null,
        logs: [],
        lastTradeDetails: null,
      });
    }

    const now = new Date();
    const diffMs = now - new Date(latest.timestamp);
    const isAlive = diffMs < 5 * 60 * 1000; // alive if heartbeat < 5 min ago

    // Approximate uptime: time from the oldest heartbeat still in DB to now
    const oldest = await Heartbeat.findOne().sort({ timestamp: 1 });
    const uptimeMs = oldest ? now - new Date(oldest.timestamp) : 0;
    const uptimeHours = parseFloat((uptimeMs / (1000 * 60 * 60)).toFixed(2));

    // ── Fallback: if heartbeat has no lastTradeDetails, pull from latest trade ──
    let tradeDetails = latest.lastTradeDetails || null;
    if (!tradeDetails || !tradeDetails.entry) {
      const lastTrade = await Trade.findOne().sort({ createdAt: -1 });
      if (lastTrade) {
        tradeDetails = {
          signal:        lastTrade.side,
          entry:         lastTrade.entry,
          sl:            lastTrade.sl,
          tp:            lastTrade.tp,
          rsi:           lastTrade.rsi,
          atr:           lastTrade.atr,
          position_size: lastTrade.position_size,
          time:          lastTrade.createdAt,
        };
      } else {
        tradeDetails = null; // Discard incomplete heartbeat data if no trades exist
      }
    }

    res.json({
      status: isAlive ? "ONLINE" : "OFFLINE",
      lastHeartbeat: latest.timestamp,
      isAlive,
      uptimeHours,
      symbol: latest.symbol,
      interval: latest.interval,
      rsi: latest.rsi,
      atrPct: latest.atrPct,
      lastSignal: latest.lastSignal,
      logs: latest.logs || [],
      lastTradeDetails: tradeDetails,
    });
  } catch (err) {
    console.error("[HEALTH] Bot status check failed:", err.message);
    res.status(500).json({ error: "Status check failed" });
  }
});

// GET /api/health/logs — Serve Render server logs from memory buffer
router.get("/logs", (req, res) => {
  const level = req.query.level || null;                  // info | error | warn
  const n     = Math.min(200, parseInt(req.query.n) || 200);
  const logs  = getLogs(n, level);
  res.json({ count: logs.length, logs });
});

// POST /api/health/logs/clear — Reset the in-memory log buffer
router.post("/logs/clear", (req, res) => {
  clearLogs();
  res.json({ ok: true, msg: "Log buffer cleared" });
});

// POST /api/health/alert — Receive alert from Python bot and forward to Telegram
router.post("/alert", async (req, res) => {
  try {
    const { message } = req.body;
    if (!message) return res.status(400).json({ error: "Missing 'message' field" });
    await sendTelegramAlert(message);
    console.log(`[ALERT] Forwarded to Telegram: ${message.substring(0, 80)}...`);
    res.json({ ok: true });
  } catch (err) {
    console.error("[ALERT] Failed to forward:", err.message);
    res.status(500).json({ error: "Alert forwarding failed" });
  }
});

module.exports = router;
