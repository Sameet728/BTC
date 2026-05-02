/**
 * Binance Futures API Client — per-account instance
 *
 * Supports both testnet and mainnet Binance USDⓈ-M Futures.
 */

const axios = require("axios");
const crypto = require("crypto");

const TESTNET_BASE = "https://testnet.binancefuture.com";
const MAINNET_BASE = "https://fapi.binance.com";

class BinanceClient {
  constructor({ apiKey, apiSecret, isTestnet = false }) {
    this.apiKey = apiKey;
    this.apiSecret = apiSecret;
    this.base = isTestnet ? TESTNET_BASE : MAINNET_BASE;
  }

  /** HMAC-SHA256 signature */
  sign(queryString) {
    return crypto.createHmac("sha256", this.apiSecret).update(queryString).digest("hex");
  }

  headers() {
    return { "X-MBX-APIKEY": this.apiKey };
  }

  // ── Account Balance ────────────────────────────────────────────────────
  async getAccountInfo() {
    const ts = Date.now();
    const qs = `timestamp=${ts}`;
    const sig = this.sign(qs);

    const res = await axios.get(`${this.base}/fapi/v2/account?${qs}&signature=${sig}`, {
      headers: this.headers(),
    });

    return res.data;
  }

  // ── Wallet Balance ─────────────────────────────────────────────────────
  async getWalletBalance() {
    const info = await this.getAccountInfo();
    return {
      totalEquity: parseFloat(info.totalMarginBalance || 0),
      availableBalance: parseFloat(info.availableBalance || 0),
      unrealisedPnl: parseFloat(info.totalUnrealizedProfit || 0),
      totalWalletBalance: parseFloat(info.totalWalletBalance || 0),
    };
  }

  // ── Open Positions ─────────────────────────────────────────────────────
  async getOpenPositions(symbol) {
    const ts = Date.now();
    let qs = `timestamp=${ts}`;
    if (symbol) qs += `&symbol=${symbol}`;
    const sig = this.sign(qs);

    const res = await axios.get(`${this.base}/fapi/v2/positionRisk?${qs}&signature=${sig}`, {
      headers: this.headers(),
    });

    return res.data.filter((p) => parseFloat(p.positionAmt) !== 0);
  }

  // ── Closed PnL (Trade History) ─────────────────────────────────────────
  async getClosedPnl(symbol, limit = 50) {
    const ts = Date.now();
    let qs = `timestamp=${ts}&limit=${limit}`;
    if (symbol) qs += `&symbol=${symbol}`;
    const sig = this.sign(qs);

    const res = await axios.get(`${this.base}/fapi/v1/userTrades?${qs}&signature=${sig}`, {
      headers: this.headers(),
    });

    return res.data;
  }

  // ── Place Market Order ─────────────────────────────────────────────────
  async placeMarketOrder(symbol, side, quantity) {
    const ts = Date.now();
    const qs = `symbol=${symbol}&side=${side}&type=MARKET&quantity=${quantity}&timestamp=${ts}`;
    const sig = this.sign(qs);

    const res = await axios.post(`${this.base}/fapi/v1/order?${qs}&signature=${sig}`, null, {
      headers: this.headers(),
    });

    return { orderId: String(res.data.orderId) };
  }

  // ── Verify API connectivity ────────────────────────────────────────────
  async getApiKeyInfo() {
    const info = await this.getAccountInfo();
    return {
      canTrade: info.canTrade,
      totalAssets: info.assets ? info.assets.length : 0,
    };
  }
}

module.exports = BinanceClient;
