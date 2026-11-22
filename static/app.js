// Globe and market panel. Orbits are propagated here, with the same Walker + J2 model as sim/orbit.py, from
// api/constellation; the server sends only market state (api/state) once per tick.
import * as THREE from "three";
import { OrbitControls } from "./vendor/OrbitControls.js";

const RE = 6378.137, MU = 398600.4418;
const RES = ["compute", "memory", "storage", "isl", "downlink"];
const RES_LABEL = { compute: "CPU", memory: "Mem", storage: "Store", isl: "ISL", downlink: "Down" };
const RES_UNIT = { compute: "$/unit-h", memory: "$/GB-h", storage: "$/TB-h", isl: "$/GB per hop", downlink: "$/GB" };
const KIND_COLOR = { bulk: 0x7cc4ff, eo: 0x9be7c4, batch: 0x7cc4ff, relay: 0xffb35c, edge: 0xd6a8ff, api: 0xffffff };

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
earth.add(new THREE.Mesh(
  new THREE.SphereGeometry(0.995, 96, 64),
  new THREE.ShaderMaterial({
    uniforms: { sun: sunUniform },
    vertexShader: `varying vec3 vN; void main(){ vN = normalize((modelMatrix*vec4(position,0.)).xyz);
      gl_Position = projectionMatrix*viewMatrix*modelMatrix*vec4(position,1.); }`,
    fragmentShader: `uniform vec3 sun; varying vec3 vN; void main(){
      float d = smoothstep(-0.15, 0.25, dot(vN, sun));
      gl_FragColor = vec4(mix(vec3(0.025,0.032,0.045), vec3(0.06,0.085,0.12), d), 1.); }`,
  })));

scene.add(new THREE.Mesh(
  new THREE.SphereGeometry(1.06, 64, 48),
  new THREE.ShaderMaterial({
    side: THREE.BackSide, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    vertexShader: `varying vec3 vN; varying vec3 vV; void main(){ vN = normalize(normalMatrix*normal);
      vec4 mv = modelViewMatrix*vec4(position,1.); vV = normalize(-mv.xyz); gl_Position = projectionMatrix*mv; }`,
    fragmentShader: `varying vec3 vN; varying vec3 vV; void main(){
      float f = pow(1. - abs(dot(vN, vV)), 3.); gl_FragColor = vec4(0.30,0.55,0.85, f*0.35); }`,
  })));

function llToLocal(lat, lon, r) {
  const la = lat * Math.PI / 180, lo = lon * Math.PI / 180;
  return [r * Math.cos(la) * Math.cos(lo), r * Math.sin(la), -r * Math.cos(la) * Math.sin(lo)];
}

fetch("land.json").then((r) => r.json()).then((pts) => {
  const pos = new Float32Array(pts.length * 3);
  pts.forEach(([la, lo], i) => pos.set(llToLocal(la, lo, 1.0), i * 3));
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  earth.add(new THREE.Points(g, new THREE.ShaderMaterial({
    uniforms: { sun: sunUniform, px: { value: renderer.getPixelRatio() } },
    transparent: true,
    vertexShader: `uniform float px; varying float vD; void main(){
      vec3 n = normalize((modelMatrix*vec4(position,0.)).xyz); vD = n.x; vec4 mv = modelViewMatrix*vec4(position,1.);
      gl_PointSize = px * 2.2 * (3.5 / -mv.z); gl_Position = projectionMatrix*mv; }`,
    fragmentShader: `uniform vec3 sun; varying float vD; void main(){
      vec2 c = gl_PointCoord-0.5; if(dot(c,c)>0.25) discard; gl_FragColor = vec4(0.36,0.47,0.58,0.75); }`,
  })));
});

// ---- constellation ----------------------------------------------------------------------------------------------
let con = null, N = 0, a = null, inc = null, raan0 = null, u0 = null, nMot = null, raanDot = null;
let satGeom = null, satPos = null, satFlag = null, gsLocal = [];
const routeGeom = new THREE.BufferGeometry();
const MAX_SEG = 6000;
const routePos = new Float32Array(MAX_SEG * 6), routeCol = new Float32Array(MAX_SEG * 6);
routeGeom.setAttribute("position", new THREE.BufferAttribute(routePos, 3));
routeGeom.setAttribute("color", new THREE.BufferAttribute(routeCol, 3));
scene.add(new THREE.LineSegments(routeGeom, new THREE.LineBasicMaterial({
  vertexColors: true, transparent: true, opacity: 0.85, blending: THREE.AdditiveBlending, depthWrite: false })));

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
let state = null, stateAt = 0, routes = [];
const history = [];

async function poll() {
  try {
    const s = await (await fetch("api/state", { cache: "no-store" })).json();
    state = s; stateAt = performance.now();
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

function simTime() {
  return state ? state.t + (performance.now() - stateAt) / 1000 * state.speed : Date.now() / 1000;
}

const tmp = new THREE.Vector3();
function nodePos(id, out) {
  if (id < N) return out.set(satPos[id * 3], satPos[id * 3 + 1], satPos[id * 3 + 2]);
  const g = gsLocal[id - N];
  return out.set(g[0], g[1], g[2]).applyMatrix4(earth.matrixWorld);
}

function drawRoutes() {
  let k = 0;
  const c = new THREE.Color();
  const prev = new THREE.Vector3();
  for (const r of routes) {
    c.setHex(KIND_COLOR[r.kind] || 0x7cc4ff);
    const w = Math.min(1, 0.35 + r.gbps / 5);
    for (let j = 0; j + 1 < r.path.length && k < MAX_SEG; j++, k++) {
      nodePos(r.path[j], prev); nodePos(r.path[j + 1], tmp);
      routePos.set([prev.x, prev.y, prev.z, tmp.x, tmp.y, tmp.z], k * 6);
      routeCol.set([c.r * w, c.g * w, c.b * w, c.r * w, c.g * w, c.b * w], k * 6);
    }
  }
  routeGeom.setDrawRange(0, k * 2);
  routeGeom.attributes.position.needsUpdate = true;
  routeGeom.attributes.color.needsUpdate = true;
}

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
    drawRoutes();
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
