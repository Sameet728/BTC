/**
 * Exchange Accounts API
 *
 * CRUD for managed exchange accounts + live balance/position fetching.
 * Keys are stored AES-256-GCM encrypted in MongoDB.
 */

const express = require("express");
const router = express.Router();
const ExchangeAccount = require("../models/ExchangeAccount");
const BybitClient = require("../services/bybitClient");
const BinanceClient = require("../services/binanceClient");

// ── Helper: build exchange client from account doc ─────────────────────────
function buildClient(account) {
  const { apiKey, apiSecret } = account.getDecryptedKeys();
  if (account.exchange === "bybit") {
    return new BybitClient({ apiKey, apiSecret, isTestnet: account.isTestnet });
  } else if (account.exchange === "binance") {
    return new BinanceClient({ apiKey, apiSecret, isTestnet: account.isTestnet });
  }
  throw new Error(`Unsupported exchange: ${account.exchange}`);
}

// ── POST /api/accounts — Add a new exchange account ────────────────────────
router.post("/", async (req, res) => {
  try {
    const { nickname, exchange, apiKey, apiSecret, isTestnet, autoTrade } = req.body;

    if (!nickname || !exchange || !apiKey || !apiSecret) {
      return res.status(400).json({ error: "nickname, exchange, apiKey, apiSecret are required" });
    }
    if (!["bybit", "binance"].includes(exchange)) {
      return res.status(400).json({ error: "exchange must be 'bybit' or 'binance'" });
    }

    // ── Verify API keys by connecting to the exchange ────────────────────
    let verifyResult;
    try {
      let client;
      if (exchange === "bybit") {
        client = new BybitClient({ apiKey, apiSecret, isTestnet: !!isTestnet });
      } else {
        client = new BinanceClient({ apiKey, apiSecret, isTestnet: !!isTestnet });
      }
      verifyResult = await client.getApiKeyInfo();
      console.log(`[ACCOUNTS] Verified ${exchange} API key for "${nickname}"`);
    } catch (err) {
      const detail = err.response ? JSON.stringify(err.response.data) : err.message;
      console.error(`[ACCOUNTS] API key verification failed:`, detail);
      return res.status(400).json({ error: "API key verification failed", detail });
    }

    const account = await ExchangeAccount.create({
      nickname,
      exchange,
      apiKey,       // pre-save hook encrypts this
      apiSecret,    // pre-save hook encrypts this
      isTestnet: !!isTestnet,
      isActive: true,
      autoTrade: !!autoTrade,

    });

    console.log(`[ACCOUNTS] Added account "${nickname}" (${exchange}) — ID: ${account._id}`);
    res.status(201).json({ success: true, account: account.toSafeJSON() });
  } catch (err) {
    console.error("[ACCOUNTS] Create failed:", err.message);
    res.status(500).json({ error: "Failed to create account", detail: err.message });
  }
});

// ── GET /api/accounts — List all accounts (masked keys) ────────────────────
router.get("/", async (req, res) => {
  try {
    const accounts = await ExchangeAccount.find().sort({ addedAt: -1 });
    res.json(accounts.map((a) => a.toSafeJSON()));
  } catch (err) {
    console.error("[ACCOUNTS] List failed:", err.message);
    res.status(500).json({ error: "Failed to list accounts" });
  }
});

// ── GET /api/accounts/summary — Aggregated balances across ALL accounts ────
router.get("/summary", async (req, res) => {
  try {
    const accounts = await ExchangeAccount.find({ isActive: true });
    const results = [];
    let totalEquity = 0;
    let totalAvailable = 0;
    let totalUnrealisedPnl = 0;
    let totalAccounts = accounts.length;
    let activeCount = 0;
    let errors = [];

    for (const acct of accounts) {
      try {
        const client = buildClient(acct);
        let balance;

        if (acct.exchange === "bybit") {
          const wallet = await client.getWalletBalance("UNIFIED");
          const acctList = wallet.list || [];
          const unified = acctList[0] || {};
          balance = {
            totalEquity: parseFloat(unified.totalEquity || 0),
            availableBalance: parseFloat(unified.totalAvailableBalance || 0),
            unrealisedPnl: parseFloat(unified.totalPerpUPL || 0),
          };
        } else {
          balance = await client.getWalletBalance();
        }

        // Update cached balance in DB
        acct.cachedBalance = {
          totalEquity: balance.totalEquity,
          availableBalance: balance.availableBalance,
          unrealisedPnl: balance.unrealisedPnl,
          updatedAt: new Date(),
        };
        await acct.save();

        totalEquity += balance.totalEquity;
        totalAvailable += balance.availableBalance;
        totalUnrealisedPnl += balance.unrealisedPnl;
        activeCount++;

        results.push({
          id: acct._id,
          nickname: acct.nickname,
          exchange: acct.exchange,
          isTestnet: acct.isTestnet,
          autoTrade: acct.autoTrade,

          balance,
          status: "connected",
        });
      } catch (err) {
        const detail = err.response ? JSON.stringify(err.response.data) : err.message;
        console.error(`[ACCOUNTS] Balance fetch failed for "${acct.nickname}":`, detail);
        errors.push({ id: acct._id, nickname: acct.nickname, error: detail });
        results.push({
          id: acct._id,
          nickname: acct.nickname,
          exchange: acct.exchange,
          isTestnet: acct.isTestnet,
          autoTrade: acct.autoTrade,

          balance: acct.cachedBalance || { totalEquity: 0, availableBalance: 0, unrealisedPnl: 0 },
          status: "error",
        });
      }
    }

    res.json({
      totalAccounts,
      activeConnected: activeCount,
      totalEquity: parseFloat(totalEquity.toFixed(2)),
      totalAvailable: parseFloat(totalAvailable.toFixed(2)),
      totalUnrealisedPnl: parseFloat(totalUnrealisedPnl.toFixed(2)),
      accounts: results,
      errors,
    });
  } catch (err) {
    console.error("[ACCOUNTS] Summary failed:", err.message);
    res.status(500).json({ error: "Summary failed" });
  }
});

// ── GET /api/accounts/:id — Single account details + live balance ──────────
router.get("/:id", async (req, res) => {
  try {
    const acct = await ExchangeAccount.findById(req.params.id);
    if (!acct) return res.status(404).json({ error: "Account not found" });

    let balance = null;
    let positions = [];
    let status = "disconnected";

    try {
      const client = buildClient(acct);

      if (acct.exchange === "bybit") {
        const wallet = await client.getWalletBalance("UNIFIED");
        const unified = (wallet.list || [])[0] || {};
        balance = {
          totalEquity: parseFloat(unified.totalEquity || 0),
          availableBalance: parseFloat(unified.totalAvailableBalance || 0),
          unrealisedPnl: parseFloat(unified.totalPerpUPL || 0),
          totalWalletBalance: parseFloat(unified.totalWalletBalance || 0),
          coins: (unified.coin || []).map((c) => ({
            coin: c.coin,
            equity: parseFloat(c.equity || 0),
            walletBalance: parseFloat(c.walletBalance || 0),
            unrealisedPnl: parseFloat(c.unrealisedPnl || 0),
            availableToWithdraw: parseFloat(c.availableToWithdraw || 0),
          })).filter((c) => c.equity > 0),
        };
      } else {
        balance = await client.getWalletBalance();
      }

      const rawPositions = await client.getOpenPositions();
      positions = rawPositions.map((p) => {
        if (acct.exchange === "bybit") {
          return {
            symbol: p.symbol,
            side: p.side,
            size: parseFloat(p.size),
            entryPrice: parseFloat(p.avgPrice),
            markPrice: parseFloat(p.markPrice),
            unrealisedPnl: parseFloat(p.unrealisedPnl),
            leverage: p.leverage,
          };
        } else {
          return {
            symbol: p.symbol,
            side: parseFloat(p.positionAmt) > 0 ? "Buy" : "Sell",
            size: Math.abs(parseFloat(p.positionAmt)),
            entryPrice: parseFloat(p.entryPrice),
            markPrice: parseFloat(p.markPrice),
            unrealisedPnl: parseFloat(p.unRealizedProfit),
            leverage: p.leverage,
          };
        }
      });

      status = "connected";

      // Cache
      if (balance) {
        acct.cachedBalance = {
          totalEquity: balance.totalEquity,
          availableBalance: balance.availableBalance,
          unrealisedPnl: balance.unrealisedPnl,
          updatedAt: new Date(),
        };
        await acct.save();
      }
    } catch (err) {
      const detail = err.response ? JSON.stringify(err.response.data) : err.message;
      console.error(`[ACCOUNTS] Detail fetch failed for "${acct.nickname}":`, detail);
      status = "error";
    }

    res.json({
      account: acct.toSafeJSON(),
      balance,
      positions,
      status,
    });
  } catch (err) {
    console.error("[ACCOUNTS] Get detail failed:", err.message);
    res.status(500).json({ error: "Failed to get account" });
  }
});

// ── PATCH /api/accounts/:id — Update account settings ──────────────────────
router.patch("/:id", async (req, res) => {
  try {
    const acct = await ExchangeAccount.findById(req.params.id);
    if (!acct) return res.status(404).json({ error: "Account not found" });

    const { nickname, isActive, autoTrade, apiKey, apiSecret, isTestnet } = req.body;
    if (nickname !== undefined) acct.nickname = nickname;
    if (isActive !== undefined) acct.isActive = isActive;
    if (autoTrade !== undefined) acct.autoTrade = autoTrade;
    if (isTestnet !== undefined) acct.isTestnet = isTestnet;

    if (apiKey) acct.apiKey = apiKey;         // pre-save encrypts
    if (apiSecret) acct.apiSecret = apiSecret; // pre-save encrypts

    await acct.save();
    console.log(`[ACCOUNTS] Updated account "${acct.nickname}" (${acct._id})`);
    res.json({ success: true, account: acct.toSafeJSON() });
  } catch (err) {
    console.error("[ACCOUNTS] Update failed:", err.message);
    res.status(500).json({ error: "Update failed" });
  }
});

// ── DELETE /api/accounts/:id — Remove an account ───────────────────────────
router.delete("/:id", async (req, res) => {
  try {
    const acct = await ExchangeAccount.findByIdAndDelete(req.params.id);
    if (!acct) return res.status(404).json({ error: "Account not found" });
    console.log(`[ACCOUNTS] Deleted account "${acct.nickname}" (${acct._id})`);
    res.json({ success: true, deleted: acct.nickname });
  } catch (err) {
    console.error("[ACCOUNTS] Delete failed:", err.message);
    res.status(500).json({ error: "Delete failed" });
  }
});

module.exports = router;
