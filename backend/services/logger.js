/**
 * Logger Service — In-memory log capture for Render dashboard
 *
 * Intercepts console.log / console.error / console.warn and stores
 * the last MAX_LINES entries in a ring buffer with timestamps and levels.
 * Original console behaviour is preserved.
 */

const MAX_LINES = 200;
const logBuffer = [];

const _origLog   = console.log.bind(console);
const _origError = console.error.bind(console);
const _origWarn  = console.warn.bind(console);

function capture(level, args) {
  const line = {
    ts: new Date().toISOString(),
    level,
    msg: args.map(a => (typeof a === "object" ? JSON.stringify(a, null, 0) : String(a))).join(" "),
  };
  logBuffer.push(line);
  if (logBuffer.length > MAX_LINES) logBuffer.shift();
}

console.log   = (...args) => { capture("info",  args); _origLog(...args); };
console.error = (...args) => { capture("error", args); _origError(...args); };
console.warn  = (...args) => { capture("warn",  args); _origWarn(...args); };

/**
 * Return the last `n` log entries (default: all buffered).
 * Optional level filter: "info" | "error" | "warn"
 */
function getLogs(n = MAX_LINES, level = null) {
  let logs = level ? logBuffer.filter(l => l.level === level) : [...logBuffer];
  return logs.slice(-n);
}

/** Clear the buffer (manual reset from API if needed) */
function clearLogs() {
  logBuffer.length = 0;
}

module.exports = { getLogs, clearLogs };
