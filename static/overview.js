// Live mode of the main page: every trade and transfer of the default market at once (Jonas 15:11: "just the live
// view of all the ongoing trades and data transfer ... there should be a lot going on at once"). Fed by api/flows,
// drawn in three buffers (beams + arcs as line segments, packets and effects as points) so a thousand flows stay at
// frame rate. Colour says what the customer bought.
import * as THREE from "three";

const TYPES = [["Imaging", 0x5fd3c6], ["Compute", 0xffb35c], ["Relay", 0xa78bfa], ["Downlink", 0x6ea8ff]];
const COL = TYPES.map(([, c]) => new THREE.Color(c));
const MAX_SEG = 16000, MAX_PT = 8000, RE_KM = 6378.137;
const ARC_DEG = 3;

export function createOverview({ scene, earth, nodePos, llToLocal, satCount, state, $ }) {
  const lines = new THREE.BufferGeometry();
  const lPos = new Float32Array(MAX_SEG * 6), lCol = new Float32Array(MAX_SEG * 6);
  lines.setAttribute("position", new THREE.BufferAttribute(lPos, 3));
  lines.setAttribute("color", new THREE.BufferAttribute(lCol, 3));
  const lineObj = new THREE.LineSegments(lines, new THREE.LineBasicMaterial({
    vertexColors: true, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false }));

  // Packets, busy-satellite glows, trade rings and delivery flashes share one point buffer: size and alpha per point,
  // ring = 1 draws an outline instead of a dot.
  const pts = new THREE.BufferGeometry();
  const pPos = new Float32Array(MAX_PT * 3), pCol = new Float32Array(MAX_PT * 3), pAux = new Float32Array(MAX_PT * 3);
  pts.setAttribute("position", new THREE.BufferAttribute(pPos, 3));
  pts.setAttribute("color", new THREE.BufferAttribute(pCol, 3));
  pts.setAttribute("aux", new THREE.BufferAttribute(pAux, 3));
  const ptObj = new THREE.Points(pts, new THREE.ShaderMaterial({
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    vertexShader: `attribute vec3 color; attribute vec3 aux; varying vec3 vC; varying float vA; varying float vR;
      void main(){ vC = color; vA = aux.y; vR = aux.z; vec4 mv = modelViewMatrix*vec4(position,1.);
      gl_PointSize = aux.x * (3.5 / -mv.z); gl_Position = projectionMatrix*mv; }`,
    fragmentShader: `varying vec3 vC; varying float vA; varying float vR; void main(){
      float d = length(gl_PointCoord-0.5); if (d > 0.5) discard;
      float a = vR > 0.5 ? smoothstep(0.32, 0.42, d) * (1.0 - smoothstep(0.45, 0.5, d)) : 1.0 - smoothstep(0.15, 0.5, d);
      gl_FragColor = vec4(vC, vA * a); }`,
  }));
  const group = new THREE.Group();
  group.add(lineObj, ptObj);
  group.visible = false;
  scene.add(group);

  let feed = null, lastT = null, clock = 0;
  const fx = [];            // {at: () => Vector3, c, born, life, size, ring}
  const ended = new Map();  // flow key -> last node, to flash at the station when a delivery finishes
  let prevFlows = new Map(), prevDone = new Set();

  const ground = (lat, lon) => { const l = llToLocal(lat, lon, 1.0); return () => new THREE.Vector3(...l).applyMatrix4(earth.matrixWorld); };
  const node = (id) => () => nodePos(id, new THREE.Vector3());

  async function poll() {
    if (!group.visible) return;
    try {
      const f = await (await fetch("api/flows", { cache: "no-store" })).json();
      if (!f.tasks || f.t === lastT) return;
      lastT = f.t;
      const N = satCount(), born = clock;
      // A tick's events are spread over the next ~2 s instead of popping together.
      for (const [lat, lon, y] of f.new) fx.push({ at: ground(lat, lon), c: COL[y], born: born + Math.random() * 2, life: 1.4, size: 46, ring: 1 });
      const flows = new Map(f.flows.map((x) => [x[0].join(","), x]));
      for (const [k, x] of prevFlows) if (!flows.has(k) && x[0][x[0].length - 1] >= N)
        fx.push({ at: node(x[0][x[0].length - 1]), c: COL[x[2]], born: born + Math.random() * 2, life: 0.9, size: 30, ring: 0 });
      const done = new Set(f.tasks.filter((t) => t[3] > 0.85).map((t) => t.join(",")));
      for (const k of prevDone) if (!done.has(k) && Math.random() < 0.5) {
        const [s, , y] = k.split(",").map(Number);
        fx.push({ at: node(s), c: COL[y], born: born + Math.random() * 2, life: 0.8, size: 18, ring: 0 });
      }
      prevFlows = flows; prevDone = done;
      while (fx.length > 600) fx.shift();
      feed = f;
      renderPane(f);
    } catch (e) { /* next poll retries */ }
  }

  const usd = (v) => "$" + (v >= 100 ? Math.round(v).toLocaleString("en-US") : v.toFixed(2));
  const NAMES = { compute: "Compute", memory: "Memory", storage: "Storage", isl: "Mesh links", downlink: "Downlink" };
  const p1 = new THREE.Vector3(), p2 = new THREE.Vector3();
  function routeKm(r) {
    const ids = (r.path && r.path.length ? r.path : [r.gs, r.sat]).filter((id) => id >= 0);
    let km = 0;
    if (r.loc) p1.copy(ground(r.loc[0], r.loc[1])());
    else nodePos(ids[0], p1);
    for (const id of ids) { nodePos(id, p2); km += p1.distanceTo(p2) * RE_KM; p1.copy(p2); }
    return km;
  }
  const seen = new Set();
  function renderPane(f) {
    const s = state();
    const bind = s && s.modes && s.modes.rfq ? NAMES[s.modes.rfq.binding] || s.modes.rfq.binding : "";
    $("lnums").innerHTML = [["Trades / min", f.per_min.trades.toFixed(0)], ["$ / min", usd(f.per_min.usd)],
      ["In flight", `${f.gbps.toFixed(0)}<small> Gbps</small>`], ["Bottleneck", bind]]
      .map(([k, v]) => `<div><span>${k}</span><b>${v}</b></div>`).join("");
    const body = $("lticker");
    const rows = f.ticker.filter((r) => !seen.has(r.id)).reverse();
    for (const r of rows) {
      seen.add(r.id);
      const tr = document.createElement("tr");
      tr.className = "new";
      tr.innerHTML = `<td><i style="background:#${COL[r.y].getHexString()}"></i>${r.c}<small>${r.k}</small></td>`
        + `<td>${usd(r.usd)}</td><td>${Math.round(routeKm(r)).toLocaleString("en-US")} km</td>`
        + `<td>${new Date(r.t * 1000).toISOString().slice(11, 19)}</td>`;
      body.prepend(tr);
    }
    while (body.children.length > 40) body.lastChild.remove();
  }

  // ---- per frame ------------------------------------------------------------------------------------------------
  const a = new THREE.Vector3(), b = new THREE.Vector3(), c = new THREE.Vector3(), ua = new THREE.Vector3(), ub = new THREE.Vector3();
  const poly = [];   // reused polyline of the current route
  for (let i = 0; i < 64; i++) poly.push(new THREE.Vector3());
  function frame(dt) {
    if (!group.visible || !feed) return;
    clock += dt;
    let k = 0, q = 0;
    const seg = (p, r, col, w) => {
      if (k >= MAX_SEG) return;
      lPos.set([p.x, p.y, p.z, r.x, r.y, r.z], k * 6);
      lCol.set([col.r * w, col.g * w, col.b * w, col.r * w, col.g * w, col.b * w], k++ * 6);
    };
    const dot = (p, col, size, alpha, ring = 0) => {
      if (q >= MAX_PT) return;
      pPos.set([p.x, p.y, p.z], q * 3); pCol.set([col.r, col.g, col.b], q * 3); pAux.set([size, alpha, ring], q++ * 3);
    };
    const N = satCount();

    // Compute jobs: the input beam from the customer's station flashes and carries packets up; the satellite glows
    // by how many jobs it runs.
    const load = new Map();
    for (const [s, g, y, prog] of feed.tasks) {
      load.set(s, (load.get(s) || 0) + 1);
      nodePos(s, b);
      const col = COL[y], ph = (s * 0.618) % 1;
      if (g >= 0) {
        nodePos(g, a);
        const flash = 0.5 + 0.5 * Math.sin((clock * 1.3 + ph) * Math.PI * 2);
        seg(a, b, col, 0.05 + 0.07 * flash * (1 - prog));
        const u = (clock * 0.45 + ph) % 1;
        if (prog < 0.6) dot(c.copy(a).lerp(b, u), col, 7, 0.55);
      }
    }
    for (const [s, n] of load) dot(nodePos(s, b), COL[1], 10 + 6 * Math.sqrt(n), Math.min(0.5, 0.12 + 0.08 * n));

    // Transfers: one arc per route (ground legs straight, the mesh part as a great circle), packets along it at a
    // speed set by the flow's data rate.
    for (const [path, gbps, y] of feed.flows) {
      const sats = path.filter((id) => id < N);
      if (!sats.length) continue;
      let m = 0;
      if (path[0] >= N) nodePos(path[0], poly[m++]);
      nodePos(sats[0], a); nodePos(sats[sats.length - 1], b);
      ua.copy(a).normalize(); ub.copy(b).normalize();
      const ang = ua.angleTo(ub), st = Math.min(40, Math.max(1, Math.ceil(ang * 180 / Math.PI / ARC_DEG))), sn = Math.sin(ang);
      for (let i = 0; i <= st; i++) {
        const t = i / st;
        poly[m++].copy(ua).multiplyScalar(sn > 1e-6 ? Math.sin((1 - t) * ang) / sn : 1 - t)
          .addScaledVector(ub, sn > 1e-6 ? Math.sin(t * ang) / sn : t)
          .multiplyScalar(a.length() + (b.length() - a.length()) * t + 0.03 * ang * Math.sin(Math.PI * t));
      }
      if (path[path.length - 1] >= N) nodePos(path[path.length - 1], poly[m++]);
      const col = COL[y];
      for (let i = 1; i < m; i++) seg(poly[i - 1], poly[i], col, 0.22);
      const speed = Math.min(1.2, 0.04 * gbps + 0.05), n = 3;
      for (let j = 0; j < n; j++) {
        const u = ((clock * speed + j / n + (path[0] * 0.37) % 1) % 1) * (m - 1), i = Math.floor(u);
        dot(c.copy(poly[i]).lerp(poly[Math.min(i + 1, m - 1)], u - i), col, 9, 0.9);
      }
    }

    // Trade rings at the customer, delivery flashes at the station.
    for (let i = fx.length - 1; i >= 0; i--) {
      const e = fx[i], age = (clock - e.born) / e.life;
      if (age >= 1) { fx.splice(i, 1); continue; }
      if (age < 0) continue;
      dot(e.at(), e.c, e.size * (e.ring ? 0.3 + age : 1 - 0.5 * age), (1 - age) * (e.ring ? 0.9 : 0.8), e.ring);
    }

    lines.setDrawRange(0, k * 2);
    lines.attributes.position.needsUpdate = lines.attributes.color.needsUpdate = true;
    pts.setDrawRange(0, q);
    pts.attributes.position.needsUpdate = pts.attributes.color.needsUpdate = pts.attributes.aux.needsUpdate = true;
  }

  function setOn(on) {
    group.visible = on;
    if (on) poll();
  }
  $("llegend").innerHTML = TYPES.map(([n, c]) => `<span><i style="background:#${c.toString(16).padStart(6, "0")}"></i>${n}</span>`).join("");
  setInterval(poll, 1000);
  return { setOn, frame };
}
