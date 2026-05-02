const mongoose = require("mongoose");

const HeartbeatSchema = new mongoose.Schema({
  timestamp:   { type: Date, default: Date.now },
  symbol:      { type: String, default: "BTCUSDT" },
  interval:    { type: String, default: "1h" },
  lastSignal:  { type: String, default: null },
  atrPct:      { type: Number, default: null },
  rsi:         { type: Number, default: null },
  logs:        { type: [String], default: [] },
  lastTradeDetails: { type: mongoose.Schema.Types.Mixed, default: null },
});

// TTL index: auto-delete documents 5 minutes after creation
HeartbeatSchema.index({ timestamp: 1 }, { expireAfterSeconds: 300 });

module.exports = mongoose.model("Heartbeat", HeartbeatSchema);
