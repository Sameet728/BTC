const Trade = require("../models/Trade");
const { getPrice, placeMarketOrder, ACTIVE_EXCHANGE } = require("./exchange");
const { sendTelegramAlert } = require("./telegram");

const POLL_INTERVAL_MS = 5000;

async function checkOpenTrades() {
  let openTrades;
  try {
    openTrades = await Trade.find({ status: "OPEN" });
  } catch (err) {
    console.error("[MONITOR] DB error:", err.message);
    return;
  }

  for (const trade of openTrades) {
    try {
      const price = await getPrice(trade.symbol);
      let hitReason = null;
      if (trade.side === "BUY") {
        if (price <= trade.sl) hitReason = "SL";
        else if (price >= trade.tp) hitReason = "TP";
      } else {
        if (price >= trade.sl) hitReason = "SL";
        else if (price <= trade.tp) hitReason = "TP";
      }
      if (hitReason) {
        console.log(`[MONITOR] ${trade.symbol} hit ${hitReason} at ${price}`);
        const closeSide = trade.side === "BUY" ? "SELL" : "BUY";

        // Only place a manual close order if Bybit doesn't have native bracket orders
        const hasNativeBrackets = trade.bybit_sl_order_id || trade.bybit_tp_order_id;

        if (!hasNativeBrackets) {
          try {
            await placeMarketOrder(trade.symbol, closeSide, trade.position_size);
          } catch (err) {
            console.error(`[MONITOR] Close order failed: ${err.message}`);
          }
        } else {
          console.log(`[MONITOR] Native Bybit bracket orders exist. Skipping manual close to prevent double execution.`);
        }

        // Calculate PnL
        const pnl = trade.side === "BUY"
          ? (price - trade.entry) * trade.position_size
          : (trade.entry - price) * trade.position_size;

        await Trade.findByIdAndUpdate(trade._id, {
          status: "CLOSED", close_reason: hitReason,
          close_price: price, closedAt: new Date(),
          pnl: parseFloat(pnl.toFixed(2)),
        });
        console.log(`[MONITOR] Trade ${trade._id} CLOSED (${hitReason}) PnL: ${pnl.toFixed(2)}`);

        const icon = hitReason === "TP" ? "✅" : "❌";
        const pnlStr = pnl >= 0 ? `+$${pnl.toFixed(2)}` : `-$${Math.abs(pnl).toFixed(2)}`;
        const tgMessage = `${icon} <b>${hitReason} HIT: ${trade.symbol}</b>\n\n<b>Close Price:</b> ${price}\n<b>Original Side:</b> ${trade.side}\n<b>Position Size:</b> ${trade.position_size}\n<b>PnL:</b> ${pnlStr}\n<b>Exchange:</b> BYBIT`;
        sendTelegramAlert(tgMessage);
      }
    } catch (err) {
      console.error(`[MONITOR] Error on trade ${trade._id}: ${err.message}`);
    }
  }
}

function startMonitor() {
  console.log(`[MONITOR] Started — polling every ${POLL_INTERVAL_MS}ms | exchange: BYBIT`);
  setInterval(checkOpenTrades, POLL_INTERVAL_MS);
}

module.exports = { startMonitor };
