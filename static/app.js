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
const camera = new THREE.PerspectiveCamera(35, 1, 0.01, 100);
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
        dc = mix(vec3(dot(dc, vec3(0.3,0.59,0.11))), dc, 0.8) * (0.25 + 0.6*max(l, 0.));
        vec3 nc = texture2D(night, vUv).rgb * vec3(1.0,0.85,0.6) * 0.9 + vec3(0.006,0.009,0.014);
        gl_FragColor = vec4(mix(nc, dc, d), 1.); }`,
  })));

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
    const s = await (await fetch("api/state", { cache: "no-store" })).json();
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
const ARC = 6;           // sub-segments per hop
const pa = new THREE.Vector3(), pb = new THREE.Vector3(), pc = new THREE.Vector3(), pd = new THREE.Vector3();
function drawRoutes(dt) {
  const live = new Set(routes.map((r) => r.id));
  for (const r of routes) if (!fade.has(r.id)) fade.set(r.id, 0);
  let k = 0;
  const c = new THREE.Color();
  for (const r of routes.concat([...fade.keys()].filter((id) => !live.has(id)).map((id) => ({ id, ghost: true })))) {
    let f = fade.get(r.id) + (r.ghost ? -dt : dt) * 1.5;
    if (f <= 0 && r.ghost) { fade.delete(r.id); continue; }
    f = Math.min(1, Math.max(0, f)); fade.set(r.id, f);
    if (r.ghost) { r.path = (ghostPaths.get(r.id) || {}).path; r.load = (ghostPaths.get(r.id) || {}).load; }
    else ghostPaths.set(r.id, r);
    if (!r.path) continue;
    c.setHex(0x5f8fb8).lerp(new THREE.Color(0xffb35c), Math.min(1, r.load || 0));
    const w = 0.55 * f;
    for (let j = 0; j + 1 < r.path.length && k < MAX_SEG; j++) {
      nodePos(r.path[j], pa); nodePos(r.path[j + 1], pb);
      const ground = r.path[j + 1] >= N;
      const steps = ground ? 1 : ARC;
      const ra = pa.length(), rb = pb.length();
      for (let q = 0; q < steps && k < MAX_SEG; q++, k++) {
        for (const [t, out] of [[q / steps, pc], [(q + 1) / steps, pd]]) {
          if (ground) out.copy(pa).lerp(pb, t);
          else out.copy(pa).normalize().lerp(pb.clone().normalize(), t).normalize().multiplyScalar(ra + (rb - ra) * t);
        }
        routePos.set([pc.x, pc.y, pc.z, pd.x, pd.y, pd.z], k * 6);
        routeCol.set([c.r * w, c.g * w, c.b * w, c.r * w, c.g * w, c.b * w], k * 6);
      }
    }
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

function renderPanel(s) {
  $("binding").textContent = RES_LABEL[s.binding] === "Down" ? "Downlink" : { compute: "Compute", memory: "Memory",
    storage: "Storage", isl: "Mesh links" }[s.binding];
  $("pressure").innerHTML = RES.map((r) => {
    const v = s.global_pressure[r];
    return `<span>${RES_LABEL[r]}</span><div class="bar"><i class="${v > 0.85 ? "hot" : ""}"
      style="width:${Math.min(100, v * 100)}%"></i></div><span class="num">${pct(v)}</span>`;
  }).join("");

  const head = `<tr><th>Region</th>${RES.map((r) => `<th title="${RES_UNIT[r]}">${RES_LABEL[r]}</th>`).join("")}</tr>`;
  const rows = s.regions.map((name, i) => `<tr><td>${name}</td>${RES.map((r) => {
    const p = s.prices[r][i], ratio = p / s.ref[r];
    const none = r === "downlink" && s.pressure.downlink[i] == null;
    const col = ratio > 1.5 ? "var(--hot)" : ratio < 0.7 ? "var(--dim)" : "var(--text)";
    return none ? `<td class="n"></td>` : `<td style="color:${col}">${fmt(p)}</td>`;
  }).join("")}</tr>`).join("");
  $("prices").innerHTML = head + rows;

  const m = s.metrics.market, b = s.metrics.baseline;
  const row = (label, f, x, y) => `<tr><td>${label}</td><td>${f(x)}</td><td>${f(y)}</td></tr>`;
  const usd = (v) => "$" + Math.round(v).toLocaleString("en-US");
  $("compare").innerHTML = `<tr><th></th><th>Market</th><th>Fixed + FIFO</th></tr>`
    + row("Value served /h", usd, m.value_per_h, b.value_per_h)
    + row("On time", pct, m.on_time, b.on_time)
    + row("Compute used", pct, m.compute_util, b.compute_util)
    + row("Downlink used", pct, m.downlink_util, b.downlink_util)
    + row("Waiting", (v) => v, m.pending, b.pending);

  $("clock").textContent = new Date(s.t * 1000).toISOString().slice(5, 16).replace("T", " ") + " UTC";
  $("counts").textContent = `${N.toLocaleString("en-US")} satellites, ${con ? con.ground_stations.length : ""} stations`;
  drawHistory(s);
}

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
  const h = await (await fetch("api/prices")).json();
  history.push(...h.history);
  await poll();
  setInterval(poll, 1000);
  frame();
})();
