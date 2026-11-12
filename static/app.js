// linewatch dashboard. Polls /api/overview and redraws.
// All device-supplied text goes through textContent (never innerHTML), so a
// compromised sensor can't inject script into the operator's browser.
"use strict";

const POLL_MS = 3000;
const LIMITS = { temp_c: [85, 95], vibration: [7.1, 11.2] };
const SVG = "http://www.w3.org/2000/svg";

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}

function fmt(v, digits = 1) {
  return v === null || v === undefined ? "-" : Number(v).toFixed(digits);
}

function level(field, v) {
  const lim = LIMITS[field];
  if (!lim || v === null || v === undefined) return "";
  return v >= lim[1] ? "crit" : v >= lim[0] ? "warn" : "";
}

function ago(seconds) {
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  return `${Math.round(seconds / 3600)}h ago`;
}

function sparkline(values, limit, cls) {
  const w = 240, h = 38, pad = 3;
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("class", "spark");
  if (values.length < 2) return svg;
  const lo = Math.min(...values, limit * 0.5);
  const hi = Math.max(...values, limit * 1.05);
  const y = (v) => h - pad - ((v - lo) / (hi - lo || 1)) * (h - 2 * pad);
  const x = (i) => (i / (values.length - 1)) * w;

  const lim = document.createElementNS(SVG, "line");
  lim.setAttribute("x1", 0); lim.setAttribute("x2", w);
  lim.setAttribute("y1", y(limit)); lim.setAttribute("y2", y(limit));
  lim.setAttribute("class", "lim");
  svg.appendChild(lim);

  const pl = document.createElementNS(SVG, "polyline");
  pl.setAttribute("points", values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" "));
  pl.setAttribute("class", cls);
  pl.setAttribute("vector-effect", "non-scaling-stroke");
  svg.appendChild(pl);
  return svg;
}

function kpi(label, value, cls) {
  const d = el("div", "kpi");
  d.appendChild(el("div", "label", label));
  d.appendChild(el("div", "value " + (cls || ""), value));
  return d;
}

function renderKpis(f) {
  const box = document.getElementById("kpis");
  box.replaceChildren(
    kpi("Online", `${f.online} / ${f.machines}`),
    kpi("Running", `${f.running}`),
    kpi("Availability (15m)", f.availability === null ? "-" : `${(f.availability * 100).toFixed(1)}%`),
    kpi("Throughput", `${Math.round(f.parts_per_hour).toLocaleString()} /hr`),
    kpi("Critical alerts", `${f.alerts_critical}`, f.alerts_critical ? "crit" : ""),
    kpi("Warnings", `${f.alerts_warning}`, f.alerts_warning ? "warn" : ""),
    kpi("Rejected readings (1h)", `${f.rejected_last_hour}`),
  );
}

function metric(k, v, cls) {
  const m = el("div", "metric");
  m.appendChild(el("div", "k", k));
  m.appendChild(el("div", "v " + (cls || ""), v));
  return m;
}

function card(m) {
  const sev = new Set(m.alerts.map((a) => a.severity));
  const c = el("div", `card ${m.status}` + (sev.has("critical") ? " has-crit" : "") + (sev.has("warning") ? " has-warn" : ""));

  const top = el("div", "card-top");
  top.appendChild(el("span", "name", m.name));
  top.appendChild(el("span", "id", m.id));
  c.appendChild(top);

  const st = el("div", "status");
  st.appendChild(el("i", `dot ${m.status}`));
  st.appendChild(el("span", null, `${m.status} · ${m.kind} · seen ${ago(m.last_seen_s)}`));
  c.appendChild(st);

  const mt = el("div", "metrics");
  mt.appendChild(metric("Temp °C", fmt(m.temp_c), level("temp_c", m.temp_c)));
  mt.appendChild(metric("Vib mm/s", fmt(m.vibration, 2), level("vibration", m.vibration)));
  mt.appendChild(metric("Parts/hr", fmt(m.parts_per_hour, 0)));
  c.appendChild(mt);

  const l1 = el("div", "spark-label");
  l1.appendChild(el("span", null, "Temperature"));
  l1.appendChild(el("span", null, `avail ${m.availability === null ? "-" : (m.availability * 100).toFixed(0) + "%"}`));
  c.appendChild(l1);
  c.appendChild(sparkline(m.spark.temp_c, LIMITS.temp_c[0], "t"));
  const l2 = el("div", "spark-label");
  l2.appendChild(el("span", null, "Vibration"));
  c.appendChild(l2);
  c.appendChild(sparkline(m.spark.vibration, LIMITS.vibration[0], "v"));

  if (m.alerts.length) {
    const chips = el("div", "chips");
    for (const a of m.alerts) chips.appendChild(el("span", `chip ${a.severity}`, a.rule));
    c.appendChild(chips);
  }
  return c;
}

function renderMachines(machines) {
  const byLine = new Map();
  for (const m of machines) {
    if (!byLine.has(m.line)) byLine.set(m.line, []);
    byLine.get(m.line).push(m);
  }
  const root = document.getElementById("lines");
  const parts = [];
  if (!machines.length) parts.push(el("div", "empty", "No machines have reported yet. Start the simulator."));
  for (const [line, ms] of byLine) {
    parts.push(el("h3", null, line));
    const g = el("div", "grid");
    for (const m of ms) g.appendChild(card(m));
    parts.push(g);
  }
  root.replaceChildren(...parts);
}

function renderAlerts(alerts, now) {
  const ol = document.getElementById("alerts");
  if (!alerts.length) {
    ol.replaceChildren(el("li", "empty", "No alerts."));
    return;
  }
  ol.replaceChildren(...alerts.map((a) => {
    const li = el("li", a.closed_ts ? "closed" : a.severity);
    const row = el("div", "row");
    row.appendChild(el("span", "rule", `${a.machine_id} · ${a.rule}`));
    row.appendChild(el("span", "when", a.closed_ts ? `cleared ${ago(now - a.closed_ts)}` : `open ${ago(now - a.opened_ts)}`));
    li.appendChild(row);
    li.appendChild(el("div", "msg", a.message));
    return li;
  }));
}

async function tick() {
  const conn = document.getElementById("conn");
  try {
    const r = await fetch("/api/overview", { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    renderKpis(data.fleet);
    renderMachines(data.machines);
    renderAlerts(data.alerts, data.generated_at);
    conn.textContent = `live · updated ${new Date(data.generated_at * 1000).toLocaleTimeString()}`;
    conn.classList.remove("bad");
  } catch (e) {
    conn.textContent = `connection lost (${e.message}) - retrying`;
    conn.classList.add("bad");
  }
}

tick();
setInterval(tick, POLL_MS);
