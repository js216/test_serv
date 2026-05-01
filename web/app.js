// test_serv web UI -- vanilla JS, no build step.
// All status fetches are manual (refresh-now, run-inventory, or the
// implicit refresh after a plan submit). Submits plans via
// /submit-text and tracks artefacts at /outputs/<digest>.{txt,tar}.

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
  jobs: [],
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
    const [devices, leases, ops, jobs] = await Promise.all([
      jget("/devices"),
      jget("/leases"),
      jget("/ops"),
      jget("/jobs"),
    ]);
    state.devices = devices;
    state.leases = leases;
    state.ops = ops;
    state.jobs = jobs;
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
    e.textContent = `last refresh: ${ts}`;
    e.className = "poll-ok";
  } else {
    e.textContent = `last refresh: ${msg || "error"}`;
    e.className = "poll-err";
  }
}

// --- rendering -------------------------------------------------------

function renderAll() {
  renderDevices();
  renderLeases();
  renderJobs();
  renderInventory();
}

function fmtAge(secondsAgo) {
  if (secondsAgo < 0) return "—";
  if (secondsAgo < 60) return `${Math.floor(secondsAgo)}s ago`;
  const m = Math.floor(secondsAgo / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m ago`;
}

function fmtBytes(n) {
  if (n == null) return "—";
  if (n < 1024) return `${n}B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)}KiB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)}MiB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)}GiB`;
}

function renderJobs() {
  const tbody = $("#jobs-table tbody");
  tbody.innerHTML = "";
  if (!state.jobs.length) {
    $("#jobs-empty").classList.remove("hidden");
    return;
  }
  $("#jobs-empty").classList.add("hidden");
  const now = Date.now() / 1000;
  for (const j of state.jobs) {
    const ts = j.completed_at || j.picked_up_at || j.queued_at || 0;
    const age = ts ? fmtAge(now - ts) : "—";
    let statusEl;
    if (j.status === "queued") {
      statusEl = el("span", { class: "tag-warn" }, "queued");
    } else if (j.status === "running") {
      statusEl = el("span", { class: "tag-warn" },
        j.cancel_pending ? "running (cancel pending)" : "running");
    } else if (j.status === "done") {
      statusEl = el("span", { class: "tag-ok" }, "done");
    } else {
      statusEl = el("span", { class: "tag-unset" }, j.status || "?");
    }
    const actions = el("span", {});
    if (j.status === "queued" || j.status === "running") {
      actions.appendChild(el("button", {
        type: "button",
        onclick: () => cancelJob(j.digest),
      }, "cancel"));
    }
    if (j.status === "done") {
      actions.appendChild(el("a", {
        href: `/outputs/${j.digest}.tar`,
        download: `${j.digest}.tar`,
        class: "artefact-link",
      }, "download"));
      actions.appendChild(el("button", {
        type: "button",
        onclick: async () => {
          if (!confirm(`Delete artefact ${j.digest.slice(0, 12)}…?`)) return;
          await fetch(`/outputs/${j.digest}`, { method: "DELETE" });
          refresh();
        },
      }, "delete"));
    }
    const descText = j.meta && (j.meta.description || j.meta.Description);
    const idCell = el("td", { class: "mono", title: j.digest },
                      j.digest.slice(0, 12) + "…");
    if (descText) {
      idCell.appendChild(el("div", { class: "device-desc" }, descText));
    }
    tbody.appendChild(el("tr", {},
      idCell,
      el("td", {}, statusEl),
      el("td", { class: "mono" }, age),
      el("td", { class: "mono" }, fmtBytes(j.size_bytes)),
      el("td", {}, actions),
    ));
  }
}

async function cancelJob(digest) {
  if (!confirm(`Cancel job ${digest.slice(0, 12)}…?`)) return;
  try {
    const r = await fetch(`/jobs/${digest}`, { method: "DELETE" });
    if (!r.ok) {
      alert(`cancel failed: ${r.status}`);
      return;
    }
    const data = await r.json();
    // canceled_queued -> immediate; cancel_signaled -> poller will
    // notice on its next /cancels pull and abort the running session
    // at the next op boundary.
    console.log("cancel:", data);
  } catch (e) {
    alert(`cancel error: ${e.message}`);
  } finally {
    refresh();
  }
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

    const desc = d.spec && d.spec.description;
    const idCell = el("td", { class: "mono" }, d.id);
    if (desc) {
      idCell.appendChild(el("div", { class: "device-desc" }, desc));
    }
    tbody.appendChild(el("tr", {},
      idCell,
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
  if (out) out.innerHTML = "<div class='hint'>submitting...</div>";
  try {
    const headers = { "Content-Type": "text/plain" };
    for (const [k, v] of Object.entries(meta)) {
      if (v != null && v !== "") headers[`X-Test-${k}`] = String(v);
    }
    const r = await fetch("/submit-text", {
      method: "POST", headers, body: text,
    });
    const data = await r.json().catch(() => ({}));
    // Treat the server's idempotency states as "we already have or are
    // about to have an artefact for this digest" -- not as errors:
    //   stale_outputs  output is already on the server; fetch and show.
    //   duplicate      input is queued; wait for the artefact same as
    //                  if we'd just submitted it ourselves.
    let digest;
    if (r.status === 409 && data.status === "stale_outputs") {
      digest = data.digest;
      if (out) {
        out.innerHTML =
          `<div class='hint'>existing artefact ${digest.slice(0, 12)}…
              (server already has results; fetching)</div>`;
      }
    } else if (r.status === 409 && data.status === "duplicate") {
      digest = data.digest;
      if (out) {
        out.innerHTML =
          `<div class='hint'>plan already queued ${digest.slice(0, 12)}…
              waiting…</div>`;
      }
    } else if (!r.ok) {
      throw new Error(`submit failed: ${r.status} ${JSON.stringify(data)}`);
    } else {
      digest = data.digest;
      if (out) {
        out.innerHTML =
          `<div class='hint'>submitted ${digest.slice(0, 12)}… waiting…</div>`;
      }
    }
    const result = await waitForArtefact(digest);
    await renderArtefact(digest, result);
    refresh();
    return { digest, ...result };
  } catch (e) {
    if (out) out.innerHTML = `<div class='tag-err'>error: ${escapeHtml(e.message)}</div>`;
    throw e;
  } finally {
    if (btn) btn.disabled = false;
  }
}

// Poll for the .txt sentinel. Does NOT delete -- the artefact stays on
// the server so the user can browse files in the tarball afterwards.
// They can click the "delete" button in the result panel to clean up.
async function waitForArtefact(digest) {
  const deadline = Date.now() + SUBMIT_TIMEOUT_MS;
  while (Date.now() < deadline) {
    const r = await fetch(`/outputs/${digest}.txt`, { cache: "no-store" });
    if (r.ok) {
      const txt = await r.text();
      return { status: "done", txt };
    }
    if (r.status !== 404) {
      throw new Error(`artefact poll: ${r.status}`);
    }
    await sleep(SUBMIT_POLL_MS);
  }
  return { status: "timeout" };
}

async function renderArtefact(digest, result) {
  const out = $("#submit-result");
  if (!out) return;
  out.innerHTML = "";

  const header = el("div", { class: "artefact-header" },
    el("span", { class: "artefact-digest", title: digest },
      digest.slice(0, 12) + "…"),
    el("span", {},
      result.status === "done"
        ? el("span", { class: "tag-ok" }, "done")
        : el("span", { class: "tag-err" }, result.status)),
    el("a", {
      href: `/outputs/${digest}.tar`,
      download: `${digest}.tar`,
      class: "artefact-link",
    }, "download tar"),
    el("button", {
      type: "button",
      class: "artefact-delete",
      onclick: async () => {
        if (!confirm(`Delete artefact ${digest.slice(0, 12)}… ?`)) return;
        await fetch(`/outputs/${digest}`, { method: "DELETE" });
        out.innerHTML = "<div class='hint'>deleted.</div>";
      },
    }, "delete"),
  );
  out.appendChild(header);

  if (result.txt) {
    const sentinelDetails = el("details", { open: "" });
    sentinelDetails.appendChild(el("summary", {}, "sentinel"));
    sentinelDetails.appendChild(el("pre", { class: "artefact-text" }, result.txt));
    out.appendChild(sentinelDetails);
  }

  const filesDetails = el("details", { open: "" });
  filesDetails.appendChild(el("summary", {}, "files in tar"));
  const fileBox = el("div", { class: "artefact-files" });
  filesDetails.appendChild(fileBox);
  out.appendChild(filesDetails);

  let manifest = [];
  try {
    const r = await fetch(`/outputs/${digest}/manifest`, { cache: "no-store" });
    if (!r.ok) throw new Error(`manifest: ${r.status}`);
    manifest = await r.json();
  } catch (e) {
    fileBox.appendChild(el("div", { class: "tag-err" },
      `failed to read manifest: ${e.message}`));
    return;
  }

  if (!manifest.length) {
    fileBox.appendChild(el("div", { class: "hint" }, "(empty tar)"));
    return;
  }

  for (const f of manifest) {
    const row = el("div", { class: "artefact-file-row" },
      el("span", { class: "mono artefact-file-name" }, f.name),
      el("span", { class: "artefact-file-size" }, `${f.size}B`),
      el("a", {
        href: `/outputs/${digest}/file/${f.name}`,
        target: "_blank",
        rel: "noopener",
        class: "artefact-link",
      }, "view"),
      el("a", {
        href: `/outputs/${digest}/file/${f.name}`,
        download: f.name.replace(/[\\/]/g, "_"),
        class: "artefact-link",
      }, "download"),
    );
    fileBox.appendChild(row);
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function releaseLease(token) {
  if (!confirm(`Release lease ${token}?`)) return;
  await submitPlanText(`lease:release token="${token}"\n`,
                       { Description: `dashboard: release lease ${token.slice(0, 12)}…` });
}

// --- form wiring -----------------------------------------------------

$("#claim-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const devs = $("#claim-devices").value.trim();
  const dur = Number($("#claim-duration").value || 600);
  if (!devs) return;
  const lines = devs.split(",").map((s) => s.trim()).filter(Boolean)
    .map((d) => `lease:claim device=${d} duration_s=${dur}`);
  await submitPlanText(lines.join("\n") + "\n",
                       { Description: `dashboard: claim ${devs} for ${dur}s` });
});

$("#submit-plan-btn").addEventListener("click", async () => {
  const text = $("#plan-text").value;
  if (!text.trim()) return;
  const meta = {};
  const desc = $("#meta-description").value.trim();
  if (desc) meta.Description = desc;
  const rt = $("#meta-runtime").value;
  if (rt) meta.Runtime = rt;
  const ut = $("#meta-upload").value;
  if (ut) meta["Upload-Timeout"] = ut;
  await submitPlanText(text, meta, $("#submit-plan-btn"));
});

$("#refresh-now").addEventListener("click", refresh);

$("#prune-jobs").addEventListener("click", async () => {
  const stale = state.jobs.filter(
    j => j.status === "running" && !j.completed_at).length;
  if (!confirm(
    `Clear ${stale} 'running' job(s) with no artefact on the server?\n` +
    `(Queued and done jobs are NOT touched.)`)) {
    return;
  }
  try {
    const r = await fetch("/jobs", { method: "DELETE" });
    if (!r.ok) {
      alert(`prune failed: ${r.status}`);
      return;
    }
    const data = await r.json();
    console.log("prune:", data);
  } catch (e) {
    alert(`prune error: ${e.message}`);
  } finally {
    refresh();
  }
});

$("#wipe-jobs").addEventListener("click", async () => {
  const total = state.jobs.length;
  if (!confirm(
    `WIPE ${total} job(s)? This drops every queued plan, every running\n` +
    `record, and EVERY ARTEFACT on the server. Cannot be undone.\n\n` +
    `(Currently-running sessions are not force-cancelled; they will\n` +
    `still post their artefact when finished. Use per-job cancel for\n` +
    `that.)`)) {
    return;
  }
  try {
    const r = await fetch("/jobs/all", { method: "DELETE" });
    if (!r.ok) {
      alert(`wipe failed: ${r.status}`);
      return;
    }
    const data = await r.json();
    console.log("wipe:", data);
  } catch (e) {
    alert(`wipe error: ${e.message}`);
  } finally {
    refresh();
  }
});

$("#run-inventory").addEventListener("click", async () => {
  const btn = $("#run-inventory");
  // Embed a timestamp in a comment so the plan body's SHA256 differs
  // from previous runs -- otherwise queue_job's stale_outputs check
  // would short-circuit and we'd just re-fetch the previous artefact
  // instead of doing a fresh re-probe.
  const ts = new Date().toISOString();
  await submitPlanText(`# inventory ${ts}\ninventory verify=true\n`,
                       { Description: "dashboard: run inventory" }, btn);
});

// --- boot ------------------------------------------------------------

// One initial fetch so the page loads with current state. After that,
// refreshes only happen on user action (refresh-now button, run-inventory
// button, or as a side-effect of submitPlanText after a plan completes).
refresh();
