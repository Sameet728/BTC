const mongoose = require("mongoose");

const TradeSchema = new mongoose.Schema({
  symbol: { type: String, required: true },
  side: { type: String, enum: ["BUY", "SELL"], required: true },
  entry: { type: Number, required: true },
  sl: { type: Number, required: true },
  tp: { type: Number, required: true },
  position_size: { type: Number, required: true },

  // Exchange — always Bybit
  exchange: { type: String, default: "bybit" },

  // Bybit order IDs
  bybit_order_id: { type: String, default: null },
  bybit_sl_order_id: { type: String, default: null },
  bybit_tp_order_id: { type: String, default: null },

  status: { type: String, enum: ["OPEN", "CLOSED", "FAILED"], default: "OPEN" },
  close_reason: { type: String, default: null },
  close_price: { type: Number, default: null },
  pnl: { type: Number, default: null },
  rsi: { type: Number, default: null },
  atr: { type: Number, default: null },
  createdAt: { type: Date, default: Date.now },
  closedAt: { type: Date, default: null },
});

module.exports = mongoose.model("Trade", TradeSchema);
