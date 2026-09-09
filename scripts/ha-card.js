// Point one Home Assistant dashboard's iframe card at a URL, and read it back.
// Reading it back matters: lovelace/config/save reports success for a config
// the frontend will not render, so "OK" from the save is not verification.
const fs = require("fs");
const TOKEN = fs.readFileSync(process.env.HOME + "/.ha_token", "utf8").trim();
const HOST = process.env.HA_HOST || "172.16.106.12:8123";
const [dashboard, url] = process.argv.slice(2);
if (!dashboard || !url) { console.error("usage: ha-card.js <url_path> <iframe url>"); process.exit(2); }

const card = {
  type: "iframe", url,
  aspect_ratio: "150%", hide_background: true,
  allow_open_top_navigation: false, disable_sandbox: false,
  allow: "fullscreen; local-network; local-network-access",
};
const config = { title: "Hummer EV", views: [{
  title: "Telemetry", path: "telemetry", icon: "mdi:car-electric",
  type: "panel", cards: [card] }] };

const ws = new WebSocket(`ws://${HOST}/api/websocket`);
let id = 0, step = 0;
const timer = setTimeout(() => { console.error("timed out"); process.exit(1); }, 25000);
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.type === "auth_required") return ws.send(JSON.stringify({ type: "auth", access_token: TOKEN }));
  if (m.type === "auth_invalid") { console.error("auth_invalid"); process.exit(1); }
  if (m.type === "auth_ok") {
    step = 1;
    return ws.send(JSON.stringify({ id: ++id, type: "lovelace/config/save", url_path: dashboard, config }));
  }
  if (m.type !== "result") return;
  if (step === 1) {
    if (!m.success) { console.error("save failed:", JSON.stringify(m.error)); process.exit(1); }
    step = 2;
    return ws.send(JSON.stringify({ id: ++id, type: "lovelace/config", url_path: dashboard }));
  }
  clearTimeout(timer);
  if (!m.success) { console.error("read-back failed:", JSON.stringify(m.error)); process.exit(1); }
  const got = m.result.views[0].cards[0].url;
  if (got !== url) { console.error(`read-back mismatch: ${got}`); process.exit(1); }
  console.log(`card now points at ${got}`);
  ws.close(); process.exit(0);
};
ws.onerror = () => { console.error("websocket error"); process.exit(1); };
