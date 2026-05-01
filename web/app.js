// test_serv web UI -- vanilla JS, no build step.
// Polls /devices, /leases, /ops every REFRESH_MS. Submits plans via
// /submit-text and tracks artefacts at /outputs/<digest>.{txt,tar}.

const REFRESH_MS = 5000;
const SUBMIT_POLL_MS = 1000;
const SUBMIT_TIMEOUT_MS = 60_000;

const $ = (sel) => document.querySelector(sel);
const el = (tag, props = {}, ...children) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") e.className = v;
    else if (k === "html") e.innerHTML = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const c of children) {
    if (c == null) continue;
    e.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return e;
};

// Stable rendering of how-long-from-now.
function fmtRemaining(seconds) {
  if (seconds <= 0) return "expired";
  const s = Math.floor(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const r = s % 60;
  if (m < 60) return `${m}m ${String(r).padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return `${h}h ${String(rm).padStart(2, "0")}m`;
}

// Render the verify column.
function verifyCell(v) {
  if (!v) return el("span", { class: "tag-unset" }, "—");
  if (v.ok === true)
    return el("span", { class: "tag-ok" },
              v.verified ? "OK (id verified)" : "OK (open ok)");
  if (v.ok === false)
    return el("span", { class: "tag-err", title: v.err || "" }, "FAIL");
  return el("span", { class: "tag-warn", title: v.err || "" },
            v.err || "in use");
}

// Locator string from a spec dict (mirrors poller's _describe_spec).
const LOC_KEYS = ["serial_port", "resource", "ft4222_desc", "ft2232h_desc",
                  "ip", "usb_serial", "device"];
function describeLoc(spec) {
  for (const k of LOC_KEYS) {
    if (spec && spec[k]) return String(spec[k]);
  }
  return "";
}

// --- state -----------------------------------------------------------

const state = {
  devices: [],
  leases: [],
  ops: {},
  lastFetch: 0,
};

// --- fetchers --------------------------------------------------------

async function jget(path) {
  const r = await fetch(path, { cache: "no-store" });
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}

async function refresh() {
  try {
    const [devices, leases, ops] = await Promise.all([
      jget("/devices"),
      jget("/leases"),
      jget("/ops"),
    ]);
    state.devices = devices;
    state.leases = leases;
    state.ops = ops;
    state.lastFetch = Date.now();
    renderAll();
    setPollStatus("ok");
  } catch (e) {
    setPollStatus("err", e.message);
  }
}

function setPollStatus(kind, msg) {
  const e = $("#poll-status");
  if (kind === "ok") {
    const ts = new Date(state.lastFetch).toLocaleTimeString();
    e.textContent = `poll: ${ts}`;
    e.className = "poll-ok";
  } else {
    e.textContent = `poll: ${msg || "error"}`;
    e.className = "poll-err";
  }
}

// --- rendering -------------------------------------------------------

function renderAll() {
  renderDevices();
  renderLeases();
  renderInventory();
}

function renderDevices() {
  const tbody = $("#devices-table tbody");
  tbody.innerHTML = "";
  if (!state.devices.length) {
    $("#devices-empty").classList.remove("hidden");
    return;
  }
  $("#devices-empty").classList.add("hidden");

  // Build a quick {key -> token} map from leases for the lease column.
  const leaseFor = {};
  for (const l of state.leases) {
    for (const k of l.devices || []) leaseFor[k] = l.token;
  }

  for (const d of state.devices) {
    const v = d.verify || null;
    const lat = v && v.latency_ms != null
      ? `${Number(v.latency_ms).toFixed(1)}ms` : "—";
    const handle = d.status || "—";
    const leaseTok = leaseFor[d.id];
    const leaseEl = leaseTok
      ? el("span", { class: "tag-warn", title: leaseTok }, "leased")
      : el("span", { class: "tag-unset" }, "—");

    tbody.appendChild(el("tr", {},
      el("td", { class: "mono" }, d.id),
      el("td", {}, d.plugin),
      el("td", { class: "mono" }, describeLoc(d.spec)),
      el("td", {}, verifyCell(v)),
      el("td", {}, lat),
      el("td", {}, handle),
      el("td", {}, leaseEl),
    ));
  }
}

function renderLeases() {
  const tbody = $("#leases-table tbody");
  tbody.innerHTML = "";
  if (!state.leases.length) {
    $("#leases-empty").classList.remove("hidden");
    return;
  }
  $("#leases-empty").classList.add("hidden");

  const now = Date.now() / 1000;
  for (const l of state.leases) {
    const remaining = (l.expires_at_walltime != null)
      ? l.expires_at_walltime - now
      : l.expires_in_s;

    tbody.appendChild(el("tr", {},
      el("td", { class: "mono", title: l.token }, l.token),
      el("td", { class: "mono" }, (l.devices || []).join(", ")),
      el("td", { class: "remaining mono",
                 "data-walltime": String(l.expires_at_walltime || 0) },
        fmtRemaining(remaining)),
      el("td", {},
        el("button", {
          type: "button",
          onclick: () => releaseLease(l.token),
        }, "release")),
    ));
  }
}

// 1Hz local countdown so leases tick every second without a backend
// roundtrip; values get re-synced on each refresh().
setInterval(() => {
  const now = Date.now() / 1000;
  for (const e of document.querySelectorAll(".remaining")) {
    const w = Number(e.dataset.walltime);
    if (!w) continue;
    e.textContent = fmtRemaining(w - now);
  }
}, 1000);

function renderInventory() {
  const c = $("#inventory");
  c.innerHTML = "";
  const names = Object.keys(state.ops).sort();
  if (!names.length) {
    c.appendChild(el("div", { class: "empty" },
      "no inventory (poller offline?)"));
    return;
  }
  for (const name of names) {
    const plugin = state.ops[name] || {};
    const ops = plugin.ops || {};
    const det = el("details", {});
    const opCount = Object.keys(ops).length;
    det.appendChild(el("summary", {},
      `${name} (${opCount} op${opCount === 1 ? "" : "s"})`));
    if (plugin.doc) det.appendChild(el("div", { class: "plugin-doc" }, plugin.doc));
    for (const opName of Object.keys(ops).sort()) {
      const op = ops[opName] || {};
      const argsStr = Object.entries(op.args || {})
        .map(([k, t]) => `${k}=<${t}>`).join(" ");
      const optStr = Object.entries(op.optional_args || {})
        .map(([k, t]) => `[${k}=<${t}>]`).join(" ");
      const sig = el("div", {},
        el("span", { class: "op-name" }, `${name}:${opName}`),
        " ",
        el("span", { class: "op-args" }, [argsStr, optStr].filter(Boolean).join(" "))
      );
      const doc = op.doc
        ? el("div", { class: "op-doc" }, op.doc)
        : null;
      det.appendChild(el("div", { class: "op-block" }, sig, doc));
    }
    c.appendChild(det);
  }
}

// --- actions ---------------------------------------------------------

async function submitPlanText(text, meta = {}, btn) {
  if (btn) btn.disabled = true;
  const out = $("#submit-result");
  if (out) {
    out.textContent = "submitting...\n";
  }
  try {
    const headers = { "Content-Type": "text/plain" };
    for (const [k, v] of Object.entries(meta)) {
      if (v != null && v !== "") headers[`X-Test-${k}`] = String(v);
    }
    const r = await fetch("/submit-text", {
      method: "POST", headers, body: text,
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      throw new Error(`submit failed: ${r.status} ${JSON.stringify(data)}`);
    }
    const digest = data.digest;
    if (out) {
      out.textContent = `submitted: ${digest}\nwaiting...\n`;
    }
    const result = await waitForArtefact(digest);
    if (out) {
      out.textContent =
        `submitted: ${digest}\nstatus: ${result.status}\n\n` +
        `--- sentinel ---\n${result.txt || "(none)"}\n` +
        (result.tarSummary ? `\n--- tar ---\n${result.tarSummary}\n` : "");
    }
    refresh();
    return { digest, ...result };
  } catch (e) {
    if (out) out.textContent = `error: ${e.message}\n`;
    throw e;
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function waitForArtefact(digest) {
  const deadline = Date.now() + SUBMIT_TIMEOUT_MS;
  while (Date.now() < deadline) {
    const r = await fetch(`/outputs/${digest}.txt`, { cache: "no-store" });
    if (r.ok) {
      const txt = await r.text();
      let tarSummary = "";
      try {
        const tr = await fetch(`/outputs/${digest}.tar`, { cache: "no-store" });
        if (tr.ok) {
          const sz = (await tr.blob()).size;
          tarSummary = `${sz} bytes`;
        }
      } catch (_) {}
      // Best-effort cleanup -- the agent's only fetch.
      fetch(`/outputs/${digest}`, { method: "DELETE" }).catch(() => {});
      return { status: "done", txt, tarSummary };
    }
    if (r.status !== 404) {
      throw new Error(`artefact poll: ${r.status}`);
    }
    await sleep(SUBMIT_POLL_MS);
  }
  return { status: "timeout" };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function releaseLease(token) {
  if (!confirm(`Release lease ${token}?`)) return;
  await submitPlanText(`lease:release token="${token}"\n`);
}

// --- form wiring -----------------------------------------------------

$("#claim-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const devs = $("#claim-devices").value.trim();
  const dur = Number($("#claim-duration").value || 600);
  if (!devs) return;
  const lines = devs.split(",").map((s) => s.trim()).filter(Boolean)
    .map((d) => `lease:claim device=${d} duration_s=${dur}`);
  await submitPlanText(lines.join("\n") + "\n");
});

$("#submit-plan-btn").addEventListener("click", async () => {
  const text = $("#plan-text").value;
  if (!text.trim()) return;
  const meta = {};
  const rt = $("#meta-runtime").value;
  if (rt) meta.Runtime = rt;
  const ut = $("#meta-upload").value;
  if (ut) meta["Upload-Timeout"] = ut;
  await submitPlanText(text, meta, $("#submit-plan-btn"));
});

$("#refresh-now").addEventListener("click", refresh);

// --- boot ------------------------------------------------------------

refresh();
setInterval(refresh, REFRESH_MS);
