const axios = require("axios");
const crypto = require("crypto");

// ── Bybit V5 API ────────────────────────────────────────────────────────────
// Supports demo (testnet) and mainnet via BYBIT_TESTNET env flag
// NOTE: Bybit replaced the old testnet with "Demo Trading" at api-demo.bybit.com
const DEMO_BASE = "https://api-demo.bybit.com";
const MAINNET_BASE = "https://api.bybit.com";
const BASE = process.env.BYBIT_TESTNET === "true" ? DEMO_BASE : MAINNET_BASE;
const RECV_WINDOW = "20000";

/**
 * Generate HMAC-SHA256 signature for Bybit V5 API
 * Sign string = timestamp + apiKey + recvWindow + (queryString | rawBody)
 */
function sign(timestamp, payload) {
  const secret = process.env.BYBIT_SECRET;
  const apiKey = process.env.BYBIT_API_KEY;
  const preSign = timestamp + apiKey + RECV_WINDOW + payload;
  return crypto.createHmac("sha256", secret).update(preSign).digest("hex");
}

function buildHeaders(timestamp, signature) {
  return {
    "X-BAPI-API-KEY": process.env.BYBIT_API_KEY,
    "X-BAPI-TIMESTAMP": timestamp,
    "X-BAPI-SIGN": signature,
    "X-BAPI-RECV-WINDOW": RECV_WINDOW,
    "Content-Type": "application/json",
  };
}

// ── Place Market Order ──────────────────────────────────────────────────────
async function placeMarketOrder(symbol, side, quantity) {
  const timestamp = String(Date.now());
  const body = {
    category: "linear",
    symbol: symbol,
    side: side === "BUY" ? "Buy" : "Sell",
    orderType: "Market",
    qty: String(quantity),
    timeInForce: "GTC",
  };
  const rawBody = JSON.stringify(body);
  const signature = sign(timestamp, rawBody);

  const response = await axios.post(
    `${BASE}/v5/order/create`,
    body,
    { headers: buildHeaders(timestamp, signature) }
  );

  if (response.data.retCode !== 0) {
    throw new Error(`Bybit order failed: ${response.data.retMsg}`);
  }

  console.log(`[BYBIT] Market order placed: ${response.data.result.orderId}`);
  return {
    orderId: response.data.result.orderId,
    orderLinkId: response.data.result.orderLinkId,
  };
}

// ── Get Price ───────────────────────────────────────────────────────────────
async function getPrice(symbol) {
  const response = await axios.get(`${BASE}/v5/market/tickers`, {
    params: { category: "linear", symbol },
  });

  if (response.data.retCode !== 0) {
    throw new Error(`Bybit price fetch failed: ${response.data.retMsg}`);
  }

  const ticker = response.data.result.list[0];
  return parseFloat(ticker.lastPrice);
}

// ── Set Position TP/SL via Trading Stop ─────────────────────────────────────
// Uses /v5/position/trading-stop so SL and TP appear under the "TP/SL" tab
// on Bybit, NOT as conditional orders.
async function setTradingStop(symbol, positionSide, sl, tp) {
  const timestamp = String(Date.now());
  // positionSide: the side of the OPEN position ("Buy" for long, "Sell" for short)
  const body = {
    category: "linear",
    symbol: symbol,
    positionIdx: 0, // 0 = one-way mode (default)
    stopLoss: String(Number(sl).toFixed(1)),
    takeProfit: String(Number(tp).toFixed(1)),
    tpslMode: "Full",            // Apply to entire position
    tpOrderType: "Market",       // Close at market when TP triggers
    slOrderType: "Market",       // Close at market when SL triggers
  };
  const rawBody = JSON.stringify(body);
  const signature = sign(timestamp, rawBody);

  const response = await axios.post(
    `${BASE}/v5/position/trading-stop`,
    body,
    { headers: buildHeaders(timestamp, signature) }
  );

  if (response.data.retCode !== 0) {
    throw new Error(`Bybit trading-stop failed: ${response.data.retMsg}`);
  }

  console.log(`[BYBIT] Position TP/SL set — SL: ${sl}, TP: ${tp}`);
  return { success: true, sl, tp };
}

// ── Get Closed PnL (Real Trade History) ───────────────────────────────────────
async function getClosedPnl(symbol, limit = 50) {
  const timestamp = String(Date.now());
  let queryString = `category=linear&limit=${limit}`;
  if (symbol) queryString += `&symbol=${symbol}`;
  
  const signature = sign(timestamp, queryString);
  
  const response = await axios.get(`${BASE}/v5/position/closed-pnl?${queryString}`, {
    headers: buildHeaders(timestamp, signature)
  });

  if (response.data.retCode !== 0) {
    throw new Error(`Bybit fetch closed PnL failed: ${response.data.retMsg}`);
  }

  return response.data.result.list;
}

// ── Get Open Positions (Real Data) ──────────────────────────────────────────
async function getOpenPositions(symbol) {
  const timestamp = String(Date.now());
  let queryString = `category=linear&settleCoin=USDT`;
  if (symbol) queryString += `&symbol=${symbol}`;
  
  const signature = sign(timestamp, queryString);
  
  const response = await axios.get(`${BASE}/v5/position/list?${queryString}`, {
    headers: buildHeaders(timestamp, signature)
  });

  if (response.data.retCode !== 0) {
    throw new Error(`Bybit fetch open positions failed: ${response.data.retMsg}`);
  }

  // Bybit returns all symbols, but size is '0' if no position is open
  return response.data.result.list.filter(p => parseFloat(p.size) > 0);
}

module.exports = { placeMarketOrder, getPrice, setTradingStop, getClosedPnl, getOpenPositions };
