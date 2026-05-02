require('dotenv').config();
const { getClosedPnl } = require('./services/bybit');

(async () => {
  try {
    const data = await getClosedPnl("BTCUSDT", 5);
    console.log(data);
  } catch (err) {
    console.error(err);
  }
})();
