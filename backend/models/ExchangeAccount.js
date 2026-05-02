const mongoose = require("mongoose");
const crypto = require("crypto");

// ── Encryption helpers (AES-256-GCM) ───────────────────────────────────────
// Uses a 32-byte key derived from ACCOUNT_ENC_KEY env (or a fallback).
// This keeps raw API secrets out of the DB.
const ENC_ALGO = "aes-256-gcm";

function getEncKey() {
  const raw = process.env.ACCOUNT_ENC_KEY || "nexustrade-default-key-change-me!";
  // Ensure exactly 32 bytes
  return crypto.createHash("sha256").update(raw).digest();
}

function encrypt(text) {
  const key = getEncKey();
  const iv = crypto.randomBytes(16);
  const cipher = crypto.createCipheriv(ENC_ALGO, key, iv);
  let enc = cipher.update(text, "utf8", "hex");
  enc += cipher.final("hex");
  const tag = cipher.getAuthTag().toString("hex");
  return iv.toString("hex") + ":" + tag + ":" + enc;
}

function decrypt(blob) {
  const key = getEncKey();
  const parts = blob.split(":");
  const iv = Buffer.from(parts[0], "hex");
  const tag = Buffer.from(parts[1], "hex");
  const enc = parts[2];
  const decipher = crypto.createDecipheriv(ENC_ALGO, key, iv);
  decipher.setAuthTag(tag);
  let dec = decipher.update(enc, "hex", "utf8");
  dec += decipher.final("utf8");
  return dec;
}

// ── Schema ─────────────────────────────────────────────────────────────────
const ExchangeAccountSchema = new mongoose.Schema({
  nickname:    { type: String, required: true },                       // e.g. "Main Bybit"
  exchange:    { type: String, enum: ["bybit", "binance"], required: true },
  apiKey:      { type: String, required: true },                       // stored encrypted
  apiSecret:   { type: String, required: true },                       // stored encrypted
  isTestnet:   { type: Boolean, default: false },
  isActive:    { type: Boolean, default: true },                       // toggle ON/OFF
  autoTrade:   { type: Boolean, default: false },                      // if true, signals execute here

  addedAt:     { type: Date, default: Date.now },

  // Cached balance snapshot (refreshed periodically)
  cachedBalance: {
    totalEquity:      { type: Number, default: 0 },
    availableBalance: { type: Number, default: 0 },
    unrealisedPnl:    { type: Number, default: 0 },
    updatedAt:        { type: Date, default: null },
  },
});

// ── Pre-save: encrypt keys ─────────────────────────────────────────────────
ExchangeAccountSchema.pre("save", function (next) {
  // Only encrypt if modified (avoid double-encrypting on updates)
  if (this.isModified("apiKey")) {
    this.apiKey = encrypt(this.apiKey);
  }
  if (this.isModified("apiSecret")) {
    this.apiSecret = encrypt(this.apiSecret);
  }
  next();
});

// ── Instance method: get decrypted keys ────────────────────────────────────
ExchangeAccountSchema.methods.getDecryptedKeys = function () {
  return {
    apiKey: decrypt(this.apiKey),
    apiSecret: decrypt(this.apiSecret),
  };
};

// ── toJSON: mask secrets ───────────────────────────────────────────────────
ExchangeAccountSchema.methods.toSafeJSON = function () {
  const obj = this.toObject();
  obj.apiKey = obj.apiKey ? "••••" + decrypt(obj.apiKey).slice(-4) : "";
  obj.apiSecret = "••••••••";
  return obj;
};

module.exports = mongoose.model("ExchangeAccount", ExchangeAccountSchema);
module.exports.encrypt = encrypt;
module.exports.decrypt = decrypt;
