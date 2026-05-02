const axios = require("axios");

const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN;
const TELEGRAM_CHAT_IDS = process.env.TELEGRAM_CHAT_ID ? process.env.TELEGRAM_CHAT_ID.split(',') : [];

async function sendTelegramAlert(message) {
  if (!TELEGRAM_BOT_TOKEN || TELEGRAM_CHAT_IDS.length === 0) return;
  const url = `https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage`;
  for (const chatId of TELEGRAM_CHAT_IDS) {
    try {
      await axios.post(url, { chat_id: chatId.trim(), text: message, parse_mode: "HTML" });
    } catch (err) {
      console.error(`[TELEGRAM] Failed to send alert to ${chatId}:`, err.message);
    }
  }
}

module.exports = { sendTelegramAlert };
