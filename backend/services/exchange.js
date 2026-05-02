/**
 * Exchange Service — Bybit only
 *
 * All trade execution routes through Bybit v5 API.
 */

const bybit = require("./bybit");

const ACTIVE_EXCHANGE = "bybit";

function getExchange() {
  return bybit;
}

/**
 * Place market order on Bybit.
 * Returns { results: [ { exchange, orderId } ] }
 */
async function placeMarketOrder(symbol, side, quantity) {
  try {
    const order = await bybit.placeMarketOrder(symbol, side, quantity);
    const result = { exchange: "bybit", orderId: String(order.orderId), success: true };
    console.log(`[EXCHANGE] BYBIT market order: ${order.orderId}`);
    return { results: [result], primaryOrderId: result.orderId, primaryExchange: "bybit" };
  } catch (err) {
    const detail = err.response ? JSON.stringify(err.response.data) : err.message;
    console.error(`[EXCHANGE] BYBIT market order FAILED:`, detail);
    throw new Error(`bybit: ${detail}`);
  }
}

/**
 * Set position-level TP/SL via Trading Stop.
 * This places SL and TP under the "TP/SL" tab on Bybit, NOT as conditional orders.
 * Must be called AFTER the market order fills (position exists).
 */
async function setPositionTPSL(symbol, side, sl, tp) {
  const result = { sl: null, tp: null, success: false };
  try {
    await bybit.setTradingStop(symbol, side, sl, tp);
    result.sl = sl;
    result.tp = tp;
    result.success = true;
    console.log(`[EXCHANGE] BYBIT TP/SL set — SL: ${sl}, TP: ${tp}`);
  } catch (err) {
    const detail = err.response ? JSON.stringify(err.response.data) : err.message;
    console.error(`[EXCHANGE] BYBIT TP/SL FAILED:`, detail);
    result.error = detail;
  }
  return result;
}

/**
 * Get price from Bybit.
 */
async function getPrice(symbol) {
  return await bybit.getPrice(symbol);
}

/**
 * Get closed PnL from Bybit.
 */
async function getClosedPnl(symbol, limit) {
  return await bybit.getClosedPnl(symbol, limit);
}

/**
 * Get open positions from Bybit.
 */
async function getOpenPositions(symbol) {
  return await bybit.getOpenPositions(symbol);
}

module.exports = {
  placeMarketOrder,
  setPositionTPSL,
  getPrice,
  getExchange,
  getClosedPnl,
  getOpenPositions,
  ACTIVE_EXCHANGE,
};

