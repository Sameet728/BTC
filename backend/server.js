// Logger must be loaded FIRST so it captures all console output from boot
require("./services/logger");
require("dotenv").config({ path: "../.env" });
const express = require("express");
const mongoose = require("mongoose");
const path = require("path");
const tradeRoutes = require("./routes/trade");
const healthRoutes = require("./routes/health");
const accountRoutes = require("./routes/accounts");
const { startMonitor } = require("./services/monitor");

const app = express();
app.use(express.json());

app.use(express.static(path.join(__dirname, "public")));

app.use("/api/trade", tradeRoutes);
app.use("/api/health", healthRoutes);
app.use("/api/accounts", accountRoutes);
app.get("/health", (req, res) => res.json({ status: "ok", ts: new Date() }));

mongoose
  .connect(process.env.MONGO_URI)
  .then(() => {
    console.log("[DB] MongoDB connected");
    const PORT = process.env.PORT || 5000;
    app.listen(PORT, () => {
      console.log(`[SERVER] Listening on port ${PORT}`);
      startMonitor();
    });
  })
  .catch((err) => {
    console.error("[DB] Connection failed:", err.message);
    process.exit(1);
  });
