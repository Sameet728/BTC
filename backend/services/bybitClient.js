/**
 * Bybit API Client — per-account instance
 *
 * Unlike the singleton in services/bybit.js (which reads keys from env),
 * this creates a client for ANY account given its API key + secret.
 */

const axios = require("axios");
const crypto = require("crypto");

const DEMO_BASE = "https://api-demo.bybit.com";
const MAINNET_BASE = "https://api.bybit.com";
const RECV_WINDOW = "20000";

class BybitClient {
  constructor({ apiKey, apiSecret, isTestnet = false }) {
    this.apiKey = apiKey;
    this.apiSecret = apiSecret;
    this.base = isTestnet ? DEMO_BASE : MAINNET_BASE;
  }

  /** HMAC-SHA256 signature */
  sign(timestamp, payload) {
    const preSign = timestamp + this.apiKey + RECV_WINDOW + payload;
    return crypto.createHmac("sha256", this.apiSecret).update(preSign).digest("hex");
  }

  headers(timestamp, signature) {
    return {
      "X-BAPI-API-KEY": this.apiKey,
      "X-BAPI-TIMESTAMP": timestamp,
      "X-BAPI-SIGN": signature,
      "X-BAPI-RECV-WINDOW": RECV_WINDOW,
      "Content-Type": "application/json",
    };
  }

  // ── Wallet Balance ─────────────────────────────────────────────────────
  async getWalletBalance(accountType = "UNIFIED") {
    const timestamp = String(Date.now());
    const qs = `accountType=${accountType}`;
    const signature = this.sign(timestamp, qs);

    const res = await axios.get(`${this.base}/v5/account/wallet-balance?${qs}`, {
      headers: this.headers(timestamp, signature),
    });

    if (res.data.retCode !== 0) throw new Error(res.data.retMsg);
    return res.data.result;
  }

  // ── Open Positions ─────────────────────────────────────────────────────
  async getOpenPositions(symbol) {
    const timestamp = String(Date.now());
    let qs = `category=linear&settleCoin=USDT`;
    if (symbol) qs += `&symbol=${symbol}`;
    const signature = this.sign(timestamp, qs);

    const res = await axios.get(`${this.base}/v5/position/list?${qs}`, {
      headers: this.headers(timestamp, signature),
    });

    if (res.data.retCode !== 0) throw new Error(res.data.retMsg);
    return res.data.result.list.filter((p) => parseFloat(p.size) > 0);
  }

  // ── Closed PnL ─────────────────────────────────────────────────────────
  async getClosedPnl(symbol, limit = 50) {
    const timestamp = String(Date.now());
    let qs = `category=linear&limit=${limit}`;
    if (symbol) qs += `&symbol=${symbol}`;
    const signature = this.sign(timestamp, qs);

    const res = await axios.get(`${this.base}/v5/position/closed-pnl?${qs}`, {
      headers: this.headers(timestamp, signature),
    });

    if (res.data.retCode !== 0) throw new Error(res.data.retMsg);
    return res.data.result.list;
  }

  // ── Place Market Order ─────────────────────────────────────────────────
  async placeMarketOrder(symbol, side, quantity) {
    const timestamp = String(Date.now());
    const body = {
      category: "linear",
      symbol,
      side: side === "BUY" ? "Buy" : "Sell",
      orderType: "Market",
      qty: String(quantity),
      timeInForce: "GTC",
    };
    const rawBody = JSON.stringify(body);
    const signature = this.sign(timestamp, rawBody);

    const res = await axios.post(`${this.base}/v5/order/create`, body, {
      headers: this.headers(timestamp, signature),
    });

    if (res.data.retCode !== 0) throw new Error(`Bybit order failed: ${res.data.retMsg}`);
    return { orderId: res.data.result.orderId };
  }

  // ── API Key Info (verify connectivity) ─────────────────────────────────
  async getApiKeyInfo() {
    const timestamp = String(Date.now());
    const qs = "";
    const signature = this.sign(timestamp, qs);

    const res = await axios.get(`${this.base}/v5/user/query-api`, {
      headers: this.headers(timestamp, signature),
    });

    if (res.data.retCode !== 0) throw new Error(res.data.retMsg);
    return res.data.result;
  }

  // ── Set Position TP/SL (Trading Stop) ──────────────────────────────────
  async setTradingStop(symbol, sl, tp) {
    const timestamp = String(Date.now());
    const body = {
      category: "linear",
      symbol,
      positionIdx: 0,
      stopLoss: String(Number(sl).toFixed(1)),
      takeProfit: String(Number(tp).toFixed(1)),
      tpslMode: "Full",
      tpOrderType: "Market",
      slOrderType: "Market",
    };
    const rawBody = JSON.stringify(body);
    const signature = this.sign(timestamp, rawBody);

    const res = await axios.post(`${this.base}/v5/position/trading-stop`, body, {
      headers: this.headers(timestamp, signature),
    });

    if (res.data.retCode !== 0) throw new Error(`Bybit trading-stop failed: ${res.data.retMsg}`);
    return { success: true, sl, tp };
  }
  // ── Set Leverage ─────────────────────────────────────────────────────────
  async setLeverage(symbol, leverage) {
    const timestamp = String(Date.now());
    const levStr = String(leverage);
    const body = {
      category: "linear",
      symbol,
      buyLeverage: levStr,
      sellLeverage: levStr,
    };
    const rawBody = JSON.stringify(body);
    const signature = this.sign(timestamp, rawBody);

    try {
      const res = await axios.post(`${this.base}/v5/position/set-leverage`, body, {
        headers: this.headers(timestamp, signature),
      });

      // 110043: Set leverage not modified (already at this leverage)
      if (res.data.retCode !== 0 && res.data.retCode !== 110043) {
        throw new Error(`Bybit set-leverage failed: ${res.data.retMsg}`);
      }
      return { success: true };
    } catch (err) {
      if (err.response && err.response.data && err.response.data.retCode === 110043) {
        return { success: true }; // Already set
      }
      throw err;
    }
  }
}

module.exports = BybitClient;
