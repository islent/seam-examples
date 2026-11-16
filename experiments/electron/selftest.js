// Headless self-test for the Electron host's data path.
//
// Electron itself needs a display and a ~200 MB download, so this exercises everything the
// shell actually depends on — the same three endpoints the shared renderer calls — using
// nothing but Node's standard library. If this passes and `npm start` shows a window, the
// only untested part is Chromium's window chrome.
//
//   node selftest.js --url http://127.0.0.1:8770

const http = require("node:http");
const { URL } = require("node:url");

const args = process.argv.slice(2);
const urlArg = args.indexOf("--url");
const BASE = urlArg >= 0 ? args[urlArg + 1] : "http://127.0.0.1:8770";

const fails = [];

function check(cond, label, detail) {
  console.log((cond ? "  ok   " : "  FAIL ") + label + (!cond && detail ? `  [${detail}]` : ""));
  if (!cond) fails.push(label);
}

function get(path) {
  return new Promise((resolve, reject) => {
    const u = new URL(path, BASE);
    http.get(u, (res) => {
      const chunks = [];
      res.on("data", (c) => chunks.push(c));
      res.on("end", () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks) }));
    }).on("error", reject);
  });
}

function post(path, body) {
  return new Promise((resolve, reject) => {
    const u = new URL(path, BASE);
    const payload = Buffer.from(JSON.stringify(body));
    const req = http.request(
      { protocol: u.protocol, hostname: u.hostname, port: u.port, path: u.pathname + u.search,
        method: "POST",
        headers: { "Content-Type": "application/json", "Content-Length": payload.length } },
      (res) => {
        const chunks = [];
        res.on("data", (c) => chunks.push(c));
        res.on("end", () => resolve({ status: res.statusCode, json: JSON.parse(Buffer.concat(chunks).toString()) }));
      },
    );
    req.on("error", reject);
    req.write(payload);
    req.end();
  });
}

// SSE: read for a bounded time and count `state` events. This is the transport the renderer
// uses in a browser, and it is the one that makes the surface feel live rather than polled.
function streamEvents(ms) {
  return new Promise((resolve) => {
    const u = new URL("/api/events", BASE);
    let events = 0;
    const req = http.get(u, (res) => {
      if (res.statusCode !== 200) { resolve({ events: 0, status: res.statusCode }); return; }
      let buf = "";
      res.on("data", (c) => {
        buf += c.toString("utf8");
        let i;
        while ((i = buf.indexOf("\n\n")) >= 0) {
          const block = buf.slice(0, i);
          buf = buf.slice(i + 2);
          if (block.startsWith("event: state")) events += 1;
        }
      });
    });
    setTimeout(() => { req.destroy(); resolve({ events, status: 200 }); }, ms);
  });
}

(async function main() {
  const man = await get("/api/manifest");
  const manifest = JSON.parse(man.body.toString());
  check(manifest.human && manifest.agent, "manifest carries both projections");
  check(Array.isArray(manifest.diff) && manifest.diff.length === 0,
        "projections agree", JSON.stringify(manifest.diff));
  check((manifest.channels || {}).events === "/api/events", "manifest advertises the event channel");

  const st = JSON.parse((await get("/api/state")).body.toString());
  check(typeof st.status === "string", "state carries a status", st.status);
  check(st.series && typeof st.series === "object", "state carries series");

  const grids = (st.controls || []).filter((c) => c.role === "observation" && c.shape === "grid");
  if (grids.length) {
    let ok = null;
    const why = [];
    for (const g of grids) {
      const fr = await get(`/api/frame?point=${encodeURIComponent(g.name)}&w=120&format=rgba`);
      if (fr.status === 200) { ok = { name: g.name, fr }; break; }
      why.push(`${g.name}: ${fr.body.toString().slice(0, 120)}`);
    }
    if (!ok) {
      check(false, "frame for any grid point", why.join(" | "));
    } else {
      const { name, fr } = ok;
      const w = Number(fr.headers["x-seam-width"] || 0);
      const h = Number(fr.headers["x-seam-height"] || 0);
      const meta = JSON.parse(fr.headers["x-seam-frame"] || "{}");
      check(fr.body.length === w * h * 3, `frame ${name} is raw RGB`, `${w}x${h} ${fr.body.length}B`);
      check(w <= 120, "frame respects the declared preview width", String(w));
      check(Object.keys(meta).length > 0, "frame carries normalisation metadata", JSON.stringify(meta).slice(0, 80));
      const distinct = new Set(fr.body.slice(0, 3000)).size;
      check(distinct > 3, "frame has structure, not a flat fill", String(distinct));
    }
  } else {
    console.log("  ..   no grid observation; frame channel untested on this manifest");
  }

  const rejected = await post("/api/intervene", { point: "__no_such_point__", value: 1 });
  check(rejected.json.outcome === "rejected", "unknown point is rejected, not ignored",
        JSON.stringify(rejected.json));

  const sse = await streamEvents(2500);
  check(sse.status === 200, "event stream opens", String(sse.status));
  check(sse.events >= 1, "event stream delivers at least one state frame", String(sse.events));

  console.log();
  if (fails.length) { console.log(`${fails.length} FAILURE(S)`); process.exit(1); }
  console.log("electron data path ok — `npm install && npm start` for the window");
})();
