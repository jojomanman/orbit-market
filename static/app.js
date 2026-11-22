// Globe and market panel. Orbits are propagated here, with the same Walker + J2 model as sim/orbit.py, from
// api/constellation; the server sends only market state (api/state) once per tick.
import * as THREE from "three";
import { OrbitControls } from "./vendor/OrbitControls.js";

const RE = 6378.137, MU = 398600.4418;
const RES = ["compute", "memory", "storage", "isl", "downlink"];
const RES_LABEL = { compute: "CPU", memory: "Mem", storage: "Store", isl: "ISL", downlink: "Down" };
const RES_UNIT = { compute: "$/unit-h", memory: "$/GB-h", storage: "$/TB-h", isl: "$/GB per hop", downlink: "$/GB" };

const $ = (id) => document.getElementById(id);
const host = $("globe");

// ---- scene ------------------------------------------------------------------------------------------------------
const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
host.appendChild(renderer.domElement);
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(35, 1, 0.01, 200);
camera.position.set(1.6, 1.4, 3.6);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.minDistance = 1.6;
controls.maxDistance = 8;
controls.autoRotate = true;
controls.autoRotateSpeed = 0.15;

const sunDir = new THREE.Vector3(1, 0, 0);
const earth = new THREE.Group();
scene.add(earth);

const sunUniform = { value: sunDir };
const tex = (f) => { const t = new THREE.TextureLoader().load(f); t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = renderer.capabilities.getMaxAnisotropy(); return t; };
// NASA Blue Marble (day) and Black Marble (night) via three.js examples; terminator from the sim's sun vector.
earth.add(new THREE.Mesh(
  new THREE.SphereGeometry(1, 128, 96),
  new THREE.ShaderMaterial({
    uniforms: { sun: sunUniform, day: { value: tex("tex/day.jpg") }, night: { value: tex("tex/night.jpg") } },
    vertexShader: `varying vec3 vN; varying vec2 vUv; void main(){ vUv = uv;
      vN = normalize((modelMatrix*vec4(position,0.)).xyz); gl_Position = projectionMatrix*viewMatrix*modelMatrix*vec4(position,1.); }`,
    fragmentShader: `uniform vec3 sun; uniform sampler2D day; uniform sampler2D night; varying vec3 vN; varying vec2 vUv;
      void main(){
        float l = dot(normalize(vN), sun);
        float d = smoothstep(-0.08, 0.12, l);
        vec3 dc = texture2D(day, vUv).rgb;
        dc *= 1. - 0.5*smoothstep(0.7, 0.9, dot(dc, vec3(0.3,0.59,0.11)));   // tame polar ice glare at the terminator
        dc = mix(vec3(dot(dc, vec3(0.3,0.59,0.11))), dc, 0.9) * (0.25 + 1.15*max(l, 0.));
        vec3 nc = texture2D(night, vUv).rgb * vec3(1.0,0.85,0.6) * 0.9 + vec3(0.006,0.009,0.014);
        gl_FragColor = vec4(mix(nc, dc, d), 1.); }`,
  })));

// Star field: procedural (no catalogue shipped). Brightness follows the steep magnitude count law, a third of the
// stars crowd a tilted band like the Milky Way; fixed in inertial space, so the Earth turns against it.
{
  const n = 7000, pos = new Float32Array(n * 3), col = new Float32Array(n * 3);
  const tilt = new THREE.Matrix4().makeRotationX(1.05);
  const v = new THREE.Vector3();
  for (let i = 0; i < n; i++) {
    const z = 2 * Math.random() - 1, ph = 2 * Math.PI * Math.random();
    v.set(Math.sqrt(1 - z * z) * Math.cos(ph), z, Math.sqrt(1 - z * z) * Math.sin(ph));
    if (i % 3 === 0) { v.y *= 0.12; v.normalize().applyMatrix4(tilt); }
    v.multiplyScalar(60); pos.set([v.x, v.y, v.z], i * 3);
    const b = 0.12 + 0.75 * Math.pow(Math.random(), 6), warm = Math.random();
    col.set([b * (0.85 + 0.15 * warm), b * 0.9, b * (1.0 - 0.15 * warm)], i * 3);
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  g.setAttribute("color", new THREE.BufferAttribute(col, 3));
  scene.add(new THREE.Points(g, new THREE.PointsMaterial({ size: 1.3, sizeAttenuation: false, vertexColors: true,
    transparent: true, depthWrite: false })));
}

function llToLocal(lat, lon, r) {
  const la = lat * Math.PI / 180, lo = lon * Math.PI / 180;
  return [r * Math.cos(la) * Math.cos(lo), r * Math.sin(la), -r * Math.cos(la) * Math.sin(lo)];
}

// ---- constellation ----------------------------------------------------------------------------------------------
let con = null, N = 0, a = null, inc = null, raan0 = null, u0 = null, nMot = null, raanDot = null;
let satGeom = null, satPos = null, satFlag = null, gsLocal = [];
const routeGeom = new THREE.BufferGeometry();
const MAX_SEG = 12000;
const routePos = new Float32Array(MAX_SEG * 6), routeCol = new Float32Array(MAX_SEG * 6);
routeGeom.setAttribute("position", new THREE.BufferAttribute(routePos, 3));
routeGeom.setAttribute("color", new THREE.BufferAttribute(routeCol, 3));
scene.add(new THREE.LineSegments(routeGeom, new THREE.LineBasicMaterial({
  vertexColors: true, transparent: true, opacity: 1, blending: THREE.AdditiveBlending, depthWrite: false })));

async function loadConstellation() {
  con = await (await fetch("api/constellation")).json();
  N = con.shells.reduce((s, sh) => s + sh.planes * sh.per_plane, 0);
  a = new Float64Array(N); inc = new Float64Array(N); raan0 = new Float64Array(N); u0 = new Float64Array(N);
  nMot = new Float64Array(N); raanDot = new Float64Array(N);
  for (const sh of con.shells) {
    for (let p = 0; p < sh.planes; p++) for (let s = 0; s < sh.per_plane; s++) {
      const i = sh.first + p * sh.per_plane + s;
      a[i] = (RE + sh.alt_km) / RE; inc[i] = sh.inc_deg * Math.PI / 180;
      raan0[i] = 2 * Math.PI * p / sh.planes;
      u0[i] = 2 * Math.PI * s / sh.per_plane + 2 * Math.PI * sh.phasing * p / (sh.planes * sh.per_plane);
      nMot[i] = sh.n; raanDot[i] = sh.raan_dot;
    }
  }
  satPos = new Float32Array(N * 3);
  satFlag = new Float32Array(N * 2);
  satGeom = new THREE.BufferGeometry();
  satGeom.setAttribute("position", new THREE.BufferAttribute(satPos, 3));
  satGeom.setAttribute("flag", new THREE.BufferAttribute(satFlag, 2));
  scene.add(new THREE.Points(satGeom, new THREE.ShaderMaterial({
    uniforms: { px: { value: renderer.getPixelRatio() } },
    transparent: true, depthWrite: false,
    vertexShader: `uniform float px; attribute vec2 flag; varying vec3 vC; varying float vA; void main(){
      float u = flag.x; float dark = flag.y;
      vec3 idle = vec3(0.42,0.50,0.58), busy = vec3(0.49,0.77,1.0), full = vec3(1.0,0.70,0.36);
      vC = u < 0.6 ? mix(idle, busy, u/0.6) : mix(busy, full, (u-0.6)/0.4);
      vA = dark > 0.5 ? 0.45 : 0.95;
      vec4 mv = modelViewMatrix*vec4(position,1.); gl_PointSize = px * (1.6 + 1.6*u) * (3.5 / -mv.z);
      gl_Position = projectionMatrix*mv; }`,
    fragmentShader: `varying vec3 vC; varying float vA; void main(){
      vec2 c = gl_PointCoord-0.5; if(dot(c,c)>0.25) discard; gl_FragColor = vec4(vC, vA); }`,
  })));
  const gsPos = new Float32Array(con.ground_stations.length * 3);
  con.ground_stations.forEach((g, i) => { gsLocal.push(llToLocal(g.lat, g.lon, 1.0)); gsPos.set(gsLocal[i], i * 3); });
  const gg = new THREE.BufferGeometry();
  gg.setAttribute("position", new THREE.BufferAttribute(gsPos, 3));
  earth.add(new THREE.Points(gg, new THREE.PointsMaterial({ color: 0xffb35c, size: 0.018 })));
}

function propagate(t) {
  const dt = t - con.epoch;
  for (let i = 0; i < N; i++) {
    const raan = raan0[i] + raanDot[i] * dt, u = u0[i] + nMot[i] * dt;
    const cu = Math.cos(u), su = Math.sin(u), cr = Math.cos(raan), sr = Math.sin(raan);
    const ci = Math.cos(inc[i]), si = Math.sin(inc[i]), r = a[i];
    const x = r * (cr * cu - sr * su * ci), y = r * (sr * cu + cr * su * ci), z = r * su * si;
    satPos[i * 3] = x; satPos[i * 3 + 1] = z; satPos[i * 3 + 2] = -y;   // ECI -> three (y up = north)
  }
  satGeom.attributes.position.needsUpdate = true;
}

// ---- state ------------------------------------------------------------------------------------------------------
let state = null, routes = [];
// Smooth clock: advance at the server's measured sim rate every frame and steer gently toward the server, so a slow
// or late tick never makes the satellites jump.
let simT = null, rate = 60, lastSrv = null;
const history = [];

async function poll() {
  try {
    const s = await (await fetch("api/state?mode=" + mode, { cache: "no-store" })).json();
    if (!s.modes) return;
    const now = performance.now();
    if (lastSrv && s.t > lastSrv.t) rate = 0.8 * rate + 0.2 * (s.t - lastSrv.t) / ((now - lastSrv.at) / 1000);
    if (!lastSrv || s.t !== lastSrv.t) lastSrv = { t: s.t, at: now };
    if (simT === null) simT = s.t;
    state = s;
    const raw = atob(s.sats);
    for (let i = 0; i < raw.length && i < N; i++) {
      const b = raw.charCodeAt(i);
      satFlag[i * 2] = (b & 127) / 127; satFlag[i * 2 + 1] = b >> 7;
    }
    if (satGeom) satGeom.attributes.flag.needsUpdate = true;
    routes = s.routes;
    if (!history.length || history[history.length - 1].t < s.history.t) history.push(s.history);
    while (history.length > 1440) history.shift();
    renderPanel(s);
  } catch (e) { /* next poll retries */ }
}

let lastFrame = performance.now();
function simTime() {
  const now = performance.now(), dt = Math.min(0.1, (now - lastFrame) / 1000);
  lastFrame = now;
  if (simT === null) return Date.now() / 1000;
  simT += dt * rate;
  const target = lastSrv.t + (now - lastSrv.at) / 1000 * rate;
  simT += (target - simT) * Math.min(1, dt * 0.5);
  return simT;
}

const tmp = new THREE.Vector3();
function nodePos(id, out) {
  if (id < N) return out.set(satPos[id * 3], satPos[id * 3 + 1], satPos[id * 3 + 2]);
  const g = gsLocal[id - N];
  return out.set(g[0], g[1], g[2]).applyMatrix4(earth.matrixWorld);
}

const fade = new Map();   // route id -> alpha, so routes fade in and out instead of popping
const ARC_DEG = 2;       // one sub-segment per 2 degrees of arc
const pa = new THREE.Vector3(), pb = new THREE.Vector3(), pc = new THREE.Vector3(), pd = new THREE.Vector3();
const ua = new THREE.Vector3(), ub = new THREE.Vector3();
// Point t along the great circle from pa to pb, radius eased between their radii and lifted by `lift` at the middle.
function arcPoint(t, lift, out) {
  const ang = ua.angleTo(ub), s = Math.sin(ang);
  out.copy(ua).multiplyScalar(s > 1e-6 ? Math.sin((1 - t) * ang) / s : 1 - t)
    .addScaledVector(ub, s > 1e-6 ? Math.sin(t * ang) / s : t);
  return out.multiplyScalar(pa.length() + (pb.length() - pa.length()) * t + lift * Math.sin(Math.PI * t));
}
function drawRoutes(dt) {
  if (highlight && highlight.path) routes = routes.filter((r) => r.id !== "hl").concat([{ id: "hl", path: highlight.path, load: 1 }]);
  const live = new Set(routes.map((r) => r.id));
  for (const r of routes) if (!fade.has(r.id)) fade.set(r.id, 0);
  let k = 0;
  const c = new THREE.Color();
  const line = (a, b, w) => {
    if (k >= MAX_SEG) return;
    routePos.set([a.x, a.y, a.z, b.x, b.y, b.z], k * 6);
    routeCol.set([c.r * w, c.g * w, c.b * w, c.r * w, c.g * w, c.b * w], k++ * 6);
  };
  for (const r of routes.concat([...fade.keys()].filter((id) => !live.has(id)).map((id) => ({ id, ghost: true })))) {
    let f = fade.get(r.id) + (r.ghost ? -dt : dt) * 1.5;
    if (f <= 0 && r.ghost) { fade.delete(r.id); continue; }
    f = Math.min(1, Math.max(0, f)); fade.set(r.id, f);
    if (r.ghost) { r.path = (ghostPaths.get(r.id) || {}).path; r.load = (ghostPaths.get(r.id) || {}).load; }
    else ghostPaths.set(r.id, r);
    if (!r.path) continue;
    c.setHex(0x5f8fb8).lerp(new THREE.Color(0xffb35c), Math.min(1, r.load || 0));
    const w = (r.id === "hl" ? 0.9 : 0.35) * f;
    // The relay hops zigzag through the mesh grid; draw the satellite part as one great-circle arc from the first to
    // the last satellite and the ground legs as straight up/down links.
    const sats = r.path.filter((id) => id < N);
    if (!sats.length) continue;
    if (r.path[0] >= N) line(nodePos(r.path[0], pc), nodePos(sats[0], pd), w);
    if (sats.length > 1) {
      nodePos(sats[0], pa); nodePos(sats[sats.length - 1], pb);
      ua.copy(pa).normalize(); ub.copy(pb).normalize();
      const ang = ua.angleTo(ub), steps = Math.max(1, Math.ceil(ang * 180 / Math.PI / ARC_DEG));
      arcPoint(0, 0, pc);
      for (let q = 1; q <= steps; q++) { arcPoint(q / steps, 0.08 * ang, pd); line(pc, pd, w); pc.copy(pd); }
    }
    const last = r.path[r.path.length - 1];
    if (last >= N) line(nodePos(sats[sats.length - 1], pc), nodePos(last, pd), w);
  }
  for (const id of ghostPaths.keys()) if (!fade.has(id)) ghostPaths.delete(id);
  routeGeom.setDrawRange(0, k * 2);
  routeGeom.attributes.position.needsUpdate = true;
  routeGeom.attributes.color.needsUpdate = true;
}
const ghostPaths = new Map();

// ---- panel ------------------------------------------------------------------------------------------------------
const fmt = (v) => v == null ? "" : v >= 10 ? v.toFixed(1) : v >= 1 ? v.toFixed(2) : v >= 0.1 ? v.toFixed(3) : v.toFixed(4);
const pct = (v) => v == null ? "" : Math.round(v * 100) + "%";
const usd = (v) => v == null ? "" : "$" + (v >= 100 ? Math.round(v).toLocaleString("en-US") : v.toFixed(2));
const NAMES = { compute: "Compute", memory: "Memory", storage: "Storage", isl: "Mesh links", downlink: "Downlink" };
let mode = new URLSearchParams(location.search).get("mode") || "rfq";

function seg(el, items, current, onPick) {
  el.innerHTML = items.map(([k, label]) => `<button data-k="${k}" class="${k === current ? "on" : ""}">${label}</button>`).join("");
  el.onclick = (e) => { const k = e.target.dataset.k; if (k) onPick(k); };
}

function renderPanel(s) {
  seg($("modes"), Object.entries(s.modes).map(([k, m]) => [k, m.label]), mode, (k) => {
    mode = k; history.length = 0;
    fetch("api/prices?mode=" + k).then((r) => r.json()).then((h) => history.push(...h.history));
    poll();
  });
  const rs = s.resources, bind = s.modes[mode].binding;
  $("binding").textContent = NAMES[bind];
  $("pressure").innerHTML = RES.map((r) => {
    const v = rs[r].hot, d = rs[r].demand;
    return `<span>${RES_LABEL[r]}</span><div class="bar"><i class="${v > 0.85 ? "hot" : ""}"
      style="width:${Math.min(100, v * 100)}%"></i></div><span class="num">${pct(v)}</span>`;
  }).join("");

  const head = `<tr><th>Region</th>${RES.map((r) => `<th title="${RES_UNIT[r]}">${RES_LABEL[r]}</th>`).join("")}</tr>`;
  const rows = s.regions.map((name, i) => `<tr><td>${name}</td>${RES.map((r) => {
    const p = s.prices[r][i];
    if (p == null) return `<td class="n" title="no ground station in region">none</td>`;
    const ratio = p / s.ref[r], col = ratio > 1.5 ? "var(--hot)" : ratio < 0.7 ? "var(--dim)" : "var(--text)";
    return `<td style="color:${col}">${fmt(p)}</td>`;
  }).join("")}</tr>`).join("");
  $("prices").innerHTML = head + rows;

  const ms = Object.entries(s.modes);
  const row = (label, f) => `<tr><td>${label}</td>${ms.map(([k, m]) => `<td class="${k === mode ? "" : "n"}">${f(m)}</td>`).join("")}</tr>`;
  $("compare").innerHTML = `<tr><th></th>${ms.map(([k, m]) => `<th>${m.label}</th>`).join("")}</tr>`
    + row("Welfare /h", (m) => usd(m.welfare_per_h)) + row("Served", (m) => pct(m.served))
    + row("Priced out", (m) => pct(m.priced_out)) + row("Missed", (m) => pct(m.missed))
    + row("p99 delivery", (m) => m.p99_delivery_s == null ? "" : m.p99_delivery_s >= 3600 ? (m.p99_delivery_s / 3600).toFixed(1) + " h" : Math.round(m.p99_delivery_s / 60) + " min")
    + row("Binding", (m) => RES_LABEL[m.binding]);

  renderScenario(s);
  $("clock").textContent = new Date(s.t * 1000).toISOString().slice(5, 16).replace("T", " ") + " UTC";
  $("counts").textContent = `${N.toLocaleString("en-US")} satellites, ${con ? con.ground_stations.length : ""} stations`;
  drawHistory(s);
}

// ---- scenarios --------------------------------------------------------------------------------------------------
let target = { lat: 14.6, lon: 121.0 };   // default: Manila (typhoon belt) until the globe is clicked
function renderScenario(s) {
  const sc = s.scenario, cur = sc.event ? "event" : sc.weather !== "off" ? "wx-" + sc.weather : "normal";
  seg($("scenarios"), [["normal", "Normal"], ["event", "Disaster"], ["wx-raw", "Weather raw"], ["wx-ai", "Weather AI"]],
    cur, (k) => {
      const body = k === "event" ? { event: { lat: target.lat, lon: target.lon, hours: 3, name: "disaster" }, weather: "off" }
        : k === "normal" ? { event: null, weather: "off" } : { event: null, weather: k.slice(3) };
      fetch("api/scenario", { method: "POST", body: JSON.stringify(body) }).then(poll);
    });
  const w = sc.weather_compare, wm = sc.weather_metrics;
  let html = "";
  if (cur.startsWith("wx") && w) {
    html = `<tr><th></th><th>Raw down</th><th>Onboard AI</th></tr>
      <tr><td>Downlink</td><td>${w.raw.gbps} Gbps</td><td>${w.ai.gbps} Gbps</td></tr>
      <tr><td>Cost /h</td><td>${usd(w.raw.usd_h)}</td><td>${usd(w.ai.usd_h)}</td></tr>
      <tr><td>Compute</td><td>0</td><td>${w.ai.units.toLocaleString("en-US")} units</td></tr>`
      + (wm ? `<tr><td>Delivered now</td><td colspan="2">${wm.down_gbps} Gbps, backlog ${Math.round(wm.backlog_gb).toLocaleString("en-US")} GB</td></tr>` : "");
  } else if (sc.event) {
    html = `<tr><td>Event</td><td>${sc.event.lat.toFixed(1)}, ${sc.event.lon.toFixed(1)}</td></tr>
      <tr><td>Ends</td><td>${new Date(sc.event.until * 1000).toISOString().slice(11, 16)} UTC</td></tr>`;
  }
  $("scenario").innerHTML = html;
}

// ---- image of a location: click the globe, get a quote per urgency, simulate one -------------------------------
const ray = new THREE.Raycaster(), mouse = new THREE.Vector2();
const marker = new THREE.Mesh(new THREE.RingGeometry(0.012, 0.018, 32),
  new THREE.MeshBasicMaterial({ color: 0xffffff, side: THREE.DoubleSide, transparent: true, opacity: 0.9 }));
earth.add(marker);
let down = null;
renderer.domElement.addEventListener("pointerdown", (e) => { down = [e.clientX, e.clientY]; });
renderer.domElement.addEventListener("pointerup", (e) => {
  if (!down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 4) return;
  const r = renderer.domElement.getBoundingClientRect();
  mouse.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
  ray.setFromCamera(mouse, camera);
  const hit = ray.intersectObject(earth.children[0])[0];
  if (!hit) return;
  const p = earth.worldToLocal(hit.point.clone()).normalize();
  setTarget(Math.asin(p.y) * 180 / Math.PI, Math.atan2(-p.z, p.x) * 180 / Math.PI);
});

function setTarget(lat, lon) {
  target = { lat, lon };
  const [x, y, z] = llToLocal(lat, lon, 1.002);
  marker.position.set(x, y, z); marker.lookAt(x * 2, y * 2, z * 2);
  $("target").textContent = `${lat.toFixed(1)}, ${lon.toFixed(1)}`;
  requote();
}

// ---- calculator -------------------------------------------------------------------------------------------------
let presets = [], preset = null;
const form = $("calc");
const WITHIN = { 60: "1 min", 900: "15 min", 3600: "1 h", 21600: "6 h", 86400: "24 h" };

function customerPreset(c) {
  const j = c.jobs[0], pj = j.per_job, dl = Math.min(86400, j.deadline_s);
  const dur = Math.min(Math.max(60, dl * 0.4), 3600);
  const cpu = Math.min(64, Math.max(1, Math.ceil(pj.cpu_core_h * 3600 / dur)));
  const lat = j.latency && j.latency.seconds <= 3600 ? { [j.latency.kind === "p50" ? "p50" : "max"]: j.latency.seconds * 1000 } : null;
  return { name: c.name, desc: `${c.domain}: ${j.type.replace("_", " ")}, ${j.jobs_per_day}/day, budget $${(c.budget_usd / 1e6).toFixed(1)}M/yr. ${c.budget_basis}`,
    job: { cpu, memory_gb: Math.min(256, Math.round(pj.mem_gb_h / (dur / 3600))), storage_gb: pj.storage_gb,
      duration_s: Math.round(dur), network: { gb: j.type === "relay_data" ? pj.isl_gb : pj.downlink_gb, ...(lat ? { latency_ms: lat } : {}) },
      deadline_s: dl, max_price_usd: j.value_per_job_usd } };
}

async function loadPresets() {
  presets = await (await fetch("presets.json")).json();
  const cs = (await (await fetch("api/customers")).json()).customers.filter((c) => c.jobs && c.jobs.length)
    .sort((a, b) => a.name.localeCompare(b.name)).map(customerPreset);
  const opt = (p, i) => `<option value="${i}">${p.name}</option>`;
  $("preset").innerHTML = `<optgroup label="Use cases">${presets.map(opt).join("")}</optgroup>`
    + `<optgroup label="Customers">${cs.map((p, i) => opt(p, presets.length + i)).join("")}</optgroup>`;
  presets = presets.concat(cs);
  $("preset").onchange = () => pick(presets[+$("preset").value]);
  pick(presets[0]);
}

function pick(p) {
  preset = p;
  $("desc").textContent = p.desc;
  const j = p.job || {};
  const v = { cpu: j.cpu, memory_gb: j.memory_gb, storage_gb: j.storage_gb, duration_s: j.duration_s,
    gb: j.network && j.network.gb, deadline_s: p.deadline_s || j.deadline_s,
    latency: j.network && j.network.latency_ms ? Object.values(j.network.latency_ms)[0] : "",
    max_price_usd: j.max_price_usd, price_cap_usd: "" };
  for (const [k, x] of Object.entries(v)) form.elements[k].value = x ?? "";
  form.elements.reserve.checked = false;
  form.style.display = p.image ? "none" : "";
  requote();
}

function body() {
  const f = form.elements, num = (k) => (f[k].value === "" ? undefined : +f[k].value);
  if (preset.image) return { image: target, deadline_s: num("deadline_s") };
  const b = { cpu: num("cpu") || 0, memory_gb: num("memory_gb") || 0, storage_gb: num("storage_gb") || 0,
    duration_s: num("duration_s") || 0, deadline_s: num("deadline_s"), max_price_usd: num("max_price_usd") };
  if (!preset.nolocation) b.location = target;
  if (preset.job && preset.job.rate) b.rate = preset.job.rate;
  if (num("gb")) {
    b.network = { gb: num("gb") };
    if (num("latency")) b.network.latency_ms = { [preset.job.network && preset.job.network.latency_ms && preset.job.network.latency_ms.p50 ? "p50" : "max"]: num("latency") };
    if (preset.relay) b.network.to = Math.abs(target.lat - 51.5) + Math.abs(target.lon) < 20 ? { lat: 40.7, lon: -74 } : { lat: 51.5, lon: -0.1 };
  }
  if (f.reserve.checked) { b.priority = "reserve"; b.price_cap_usd = num("price_cap_usd") ?? num("max_price_usd"); }
  return b;
}

let quoteTimer = null;
function requote() {
  clearTimeout(quoteTimer);
  quoteTimer = setTimeout(async () => {
    if (!preset) return;
    if (preset.image) {
      const q = await (await fetch(`api/quote?lat=${target.lat}&lon=${target.lon}`)).json();
      $("quote").innerHTML = `<tr><th>Within</th><th>Price</th><th>Pass in</th><th></th></tr>` + q.options.map((o) =>
        o.price_usd == null ? `<tr><td>${WITHIN[o.within_s]}</td><td class="n" colspan="3">no pass</td></tr>`
          : `<tr><td>${WITHIN[o.within_s]}</td><td>${usd(o.price_usd)}</td><td>${Math.round((o.pass_t - q.t) / 60)} min</td>
             <td><button data-w="${o.within_s}">Simulate</button></td></tr>`).join("");
      $("quote").onclick = (e) => { const w = e.target.dataset.w; if (w) submit({ image: target, deadline_s: +w }); };
      $("simulate").style.display = "none";
      return;
    }
    $("simulate").style.display = "";
    $("quote").onclick = null;
    const r = await (await fetch("api/quote?mode=" + mode, { method: "POST", body: JSON.stringify(body()) })).json();
    if (r.error || !r.feasible) { $("quote").innerHTML = `<tr><td class="n">${r.error || r.reason}</td></tr>`; return; }
    const b = r.breakdown, rows = [["Compute", b.compute], ["Memory", b.memory], ["Storage", b.storage],
      ["Route links", b.route], ["Downlink", b.downlink], ["Urgency premium", b.urgency_premium]];
    $("quote").innerHTML = rows.filter(([, v]) => v != null).map(([k, v]) => `<tr><td>${k}</td><td>${usd(v)}</td></tr>`).join("")
      + `<tr><td>Total now</td><td>${usd(r.total_usd)}</td></tr>`
      + (r.wait_price_usd < r.total_usd - 0.005 ? `<tr><td class="n">If it waits</td><td class="n">${usd(r.wait_price_usd)}</td></tr>` : "")
      + `<tr><td class="n">Satellite</td><td class="n">${r.sat}, ${r.region}</td></tr>`
      + (r.per_hour_usd != null ? `<tr><td class="n">Per hour</td><td class="n">${usd(r.per_hour_usd)}</td></tr>` : "")
      + (r.quotes && r.quotes.length ? `<tr><th>Quotes</th><th>${mode === "rfq" ? "paid = 2nd lowest" : ""}</th></tr>` + r.quotes.map((x, i) =>
        `<tr><td class="${i ? "n" : ""}">sat ${x.sat}, owner ${x.owner}</td><td class="${i ? "n" : ""}">${usd(x.usd)}</td></tr>`).join("") : "");
    highlight = { sat: r.sat, path: r.path };
  }, 250);
}
form.addEventListener("input", requote);
$("simulate").onclick = () => submit(body());
function submit(b) {
  fetch("api/jobs", { method: "POST", body: JSON.stringify(b) }).then((r) => r.json()).then(track);
}

let tracked = null;
function track(j) {
  if (j.error) { $("job").textContent = j.error; return; }
  tracked = j.id;
  const tick = () => fetch("api/jobs/" + tracked).then((r) => r.json()).then((s) => {
    const p = s.placed;
    const when = (t) => new Date(t * 1000).toISOString().slice(11, 16);
    $("job").textContent = s.status === "waiting" ? `quoted ${usd(s.quote_usd)}, waiting for the pass`
      : s.status === "running" ? `on satellite ${p.sat}, ${usd(p.price)}`
      : `${s.status === "done" ? "delivered" : "missed"} by ${when(s.deadline)} UTC, sat ${p ? p.sat : "-"}, ${usd(p && p.price)}`;
    highlight = p ? { sat: p.sat, path: p.path } : null;
    if (s.status === "waiting" || s.status === "running") setTimeout(tick, 2000);
  });
  tick();
}
let highlight = null;

const sparks = {};
function drawHistory(s) {
  const box = $("history");
  if (!box.childElementCount) {
    for (const r of RES) {
      const label = document.createElement("span"); label.textContent = RES_LABEL[r];
      const cv = document.createElement("canvas"); cv.width = 520; cv.height = 40;
      const v = document.createElement("span"); v.className = "num";
      box.append(label, cv, v); sparks[r] = { cv, v };
    }
  }
  for (const r of RES) {
    const { cv, v } = sparks[r], g = cv.getContext("2d");
    const ys = history.map((h) => Math.log(h[r] / s.ref[r]));
    g.clearRect(0, 0, cv.width, cv.height);
    g.strokeStyle = "#1a2129"; g.lineWidth = 1;
    g.beginPath(); g.moveTo(0, cv.height / 2); g.lineTo(cv.width, cv.height / 2); g.stroke();
    if (ys.length > 1) {
      const lim = Math.max(Math.log(3), ...ys.map(Math.abs));
      g.strokeStyle = "#7cc4ff"; g.lineWidth = 2; g.beginPath();
      ys.forEach((y, i) => {
        const X = (i / 1439) * cv.width, Y = cv.height / 2 - (y / lim) * (cv.height / 2 - 2);
        i ? g.lineTo(X, Y) : g.moveTo(X, Y);
      });
      g.stroke();
    }
    const last = history[history.length - 1];
    v.textContent = last ? (last[r] / s.ref[r]).toFixed(2) + "x" : "";
  }
}

// ---- loop -------------------------------------------------------------------------------------------------------
function resize() {
  const w = host.clientWidth, h = host.clientHeight;
  renderer.setSize(w, h); camera.aspect = w / h; camera.updateProjectionMatrix();
}
addEventListener("resize", resize);

function frame() {
  requestAnimationFrame(frame);
  if (con) {
    const t = simTime();
    if (state) {
      const gm = state.gmst + (t - state.t) * 7.2921159e-5;
      earth.rotation.y = gm;
      sunDir.set(state.sun[0], state.sun[2], -state.sun[1]);
    }
    earth.updateMatrixWorld();
    propagate(t);
    drawRoutes(1 / 60);
  }
  controls.update();
  renderer.render(scene, camera);
}

(async () => {
  resize();
  await loadConstellation();
  const h = await (await fetch("api/prices?mode=" + mode)).json();
  history.push(...h.history);
  await loadPresets();
  setTarget(target.lat, target.lon);
  await poll();
  setInterval(poll, 1000);
  frame();
})();
