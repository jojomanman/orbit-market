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

  // Packets and busy-satellite glows share one point buffer, size and alpha per point.
  const pts = new THREE.BufferGeometry();
  const pPos = new Float32Array(MAX_PT * 3), pCol = new Float32Array(MAX_PT * 3), pAux = new Float32Array(MAX_PT * 2);
  pts.setAttribute("position", new THREE.BufferAttribute(pPos, 3));
  pts.setAttribute("color", new THREE.BufferAttribute(pCol, 3));
  pts.setAttribute("aux", new THREE.BufferAttribute(pAux, 2));
  const ptObj = new THREE.Points(pts, new THREE.ShaderMaterial({
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    vertexShader: `attribute vec3 color; attribute vec2 aux; varying vec3 vC; varying float vA;
      void main(){ vC = color; vA = aux.y; vec4 mv = modelViewMatrix*vec4(position,1.);
      gl_PointSize = aux.x * (3.5 / -mv.z); gl_Position = projectionMatrix*mv; }`,
    fragmentShader: `varying vec3 vC; varying float vA; void main(){
      float d = length(gl_PointCoord-0.5); if (d > 0.5) discard;
      gl_FragColor = vec4(vC, vA * (1.0 - smoothstep(0.15, 0.5, d))); }`,
  }));
  const group = new THREE.Group();
  group.add(lineObj, ptObj);
  group.visible = false;
  scene.add(group);

  // Pings (Jonas 15:25 "the pings on the map look bad"): one thin ring lying on the ground, a trade at the customer or a
  // delivery at the station. Instanced on the Earth so the globe hides the far side; additive, so colour x alpha fades.
  const PING_MAX = 30, PING_R = 0.05;
  const pingMesh = new THREE.InstancedMesh(new THREE.RingGeometry(0.93, 1, 64),
    new THREE.MeshBasicMaterial({ transparent: true, blending: THREE.AdditiveBlending, depthWrite: false }), PING_MAX);
  pingMesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
  pingMesh.setColorAt(0, COL[0]);
  pingMesh.frustumCulled = false;
  pingMesh.visible = false;
  earth.add(pingMesh);

  // Everything on the globe lives in sim time one tick behind the server (T = sim clock - tick), so each task, flow and
  // ping is scheduled at the instant the sim gives it (api/flows start/end) and nothing changes at the poll itself.
  // Polls add, patch and retire entries by job id; before, the whole set was rebuilt from each snapshot and every
  // link changed at the same moment (Jonas 16:24 "Why does the links update at the same time ... I want it smooth").
  const FADE = 15, PING_S = 30, PING_GAP = 45;   // sim seconds: 0.5 s, 1 s and 1.5 s of wall time at 1x
  let feed = null, lastT = null, tick = 60;
  const tasks = new Map(), flows = new Map();   // job id -> row + {gone}
  const pings = [];         // {p: surface point in Earth coordinates, c, born (sim)}
  const lastPing = new Map();  // ~4 degree cell -> start, so one busy city never piles rings up
  const Z = new THREE.Vector3(0, 0, 1);
  function ping(p, c, at) {
    const cell = `${Math.round(Math.asin(p.y) * 45)},${Math.round(Math.atan2(p.z, p.x) * 45)}`;
    if (pings.length >= PING_MAX || at - (lastPing.get(cell) ?? -1e9) < PING_GAP) return;
    lastPing.set(cell, at);
    pings.push({ p: p.multiplyScalar(1.002), c, born: at });
  }
  // Rows are [.., id, start, end]; an entry the server no longer lists ends at its last predicted end, clamped into
  // the tick it vanished in (server t is that tick's start), so removals spread like the sim's real finishes.
  function merge(map, rows, onEnd) {
    const seen = new Set();
    for (const r of rows) {
      seen.add(r[3]);
      const e = map.get(r[3]);
      if (e) { e.row = r; e.gone = false; } else map.set(r[3], { row: r, gone: false });
    }
    for (const [id, e] of map) {
      if (seen.has(id) || e.gone) continue;
      e.gone = true;
      e.row[5] = Math.min(Math.max(e.row[5], lastT), lastT + tick);
      if (onEnd) onEnd(e.row);
    }
  }

  const ground = (lat, lon) => { const l = llToLocal(lat, lon, 1.0); return () => new THREE.Vector3(...l).applyMatrix4(earth.matrixWorld); };

  async function poll() {
    if (!group.visible) return;
    try {
      const f = await (await fetch("api/flows", { cache: "no-store" })).json();
      if (!f.tasks || f.t === lastT) return;
      lastT = f.t;
      tick = f.tick || tick;
      const N = satCount();
      for (const [lat, lon, y, at] of f.new) ping(new THREE.Vector3(...llToLocal(lat, lon, 1)), COL[y], at);
      merge(tasks, f.tasks);
      merge(flows, f.flows, (r) => { const last = r[0][r[0].length - 1]; if (last >= N)
        ping(earth.worldToLocal(nodePos(last, new THREE.Vector3())).normalize(), COL[r[2]], r[5]); });
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
  const ringM = new THREE.Matrix4(), ringQ = new THREE.Quaternion(), ringN = new THREE.Vector3(), ringS = new THREE.Vector3();
  const ringC = new THREE.Color();
  const poly = [];   // reused polyline of the current route
  for (let i = 0; i < 512; i++) poly.push(new THREE.Vector3());
  const dv = new THREE.Vector3();
  // The station sees the satellite (elevation above 5 degrees). A job's upload station is fixed at match time; an
  // hour into the run the satellite is behind the Earth and the beam must not be drawn through it.
  const above = (g, s) => { dv.subVectors(s, g); return dv.dot(g) > 0.087 * dv.length() * g.length(); };
  // T: sim time the globe shows (one tick behind the server), rate: sim seconds per wall second at 1x. Fades, packet
  // speed and pings all run on T, so they freeze when paused and never depend on when a poll landed.
  const clamp01 = (x) => x < 0 ? 0 : x > 1 ? 1 : x;
  function frame(play, simT, rate) {
    if (!group.visible || !feed) return;
    const T = simT - tick, wall = T / rate;
    let k = 0, q = 0;
    const seg = (p, r, col, w) => {
      if (k >= MAX_SEG) return;
      lPos.set([p.x, p.y, p.z, r.x, r.y, r.z], k * 6);
      lCol.set([col.r * w, col.g * w, col.b * w, col.r * w, col.g * w, col.b * w], k++ * 6);
    };
    const dot = (p, col, size, alpha) => {
      if (q >= MAX_PT) return;
      pPos.set([p.x, p.y, p.z], q * 3); pCol.set([col.r, col.g, col.b], q * 3); pAux.set([size, alpha], q++ * 2);
    };
    // Visible from start (fade in over FADE) until end (+FADE fade out once the server dropped it); 0 = not yet.
    const alive = (map, e) => {
      const [, , , id, start, end] = e.row;
      if (T >= end + FADE && e.gone) { map.delete(id); return 0; }
      return Math.min(clamp01((T - start) / FADE), e.gone ? clamp01((end + FADE - T) / FADE) : 1);
    };
    const N = satCount();

    // Compute jobs: the input beam from the customer's station flashes and carries packets up; the satellite glows
    // by how many jobs it runs.
    const load = new Map();
    for (const e of tasks.values()) {
      const al = alive(tasks, e);
      if (!al) continue;
      const [s, g, y, , start, end] = e.row, prog = clamp01((T - start) / Math.max(end - start, 1));
      load.set(s, (load.get(s) || 0) + al);
      nodePos(s, b);
      const col = COL[y], ph = (s * 0.618) % 1;
      if (g >= 0 && above(nodePos(g, a), b)) {
        const flash = 0.5 + 0.5 * Math.sin((wall * 1.3 + ph) * Math.PI * 2);
        seg(a, b, col, al * (0.05 + 0.07 * flash * (1 - prog)));
        const u = (wall * 0.45 + ph) % 1;
        if (prog < 0.6) dot(c.copy(a).lerp(b, u), col, 7, 0.55 * al);
      }
    }
    for (const [s, n] of load) dot(nodePos(s, b), COL[1], 10 + 6 * Math.sqrt(n), Math.min(0.5, 0.12 + 0.08 * n));

    // Transfers: the route hop by hop through the satellites it really uses, each mesh hop an arc at altitude and
    // the ground legs straight while the station sees the satellite. One great circle from the first to the last
    // satellite was drawn before, which cut through the Earth (Jonas 15:45 "communicating through the earth").
    // A new route draws itself in from the satellite over FADE; packets run on sim time, so a poll never moves them.
    for (const e of flows.values()) {
      const al = alive(flows, e);
      if (!al) continue;
      const [path, gbps, y] = e.row;
      const sats = path.filter((id) => id < N);
      if (!sats.length) continue;
      let m = 0;
      nodePos(sats[0], a);
      if (path[0] >= N && above(nodePos(path[0], c), a)) poly[m++].copy(c);
      poly[m++].copy(a);
      for (let h = 1; h < sats.length && m < poly.length - 20; h++) {
        nodePos(sats[h], b);
        ua.copy(a).normalize(); ub.copy(b).normalize();
        const ang = ua.angleTo(ub), st = Math.min(16, Math.max(1, Math.ceil(ang * 180 / Math.PI / ARC_DEG))), sn = Math.sin(ang);
        for (let i = 1; i <= st; i++) {
          const t = i / st;
          poly[m++].copy(ua).multiplyScalar(sn > 1e-6 ? Math.sin((1 - t) * ang) / sn : 1 - t)
            .addScaledVector(ub, sn > 1e-6 ? Math.sin(t * ang) / sn : t)
            .multiplyScalar(a.length() + (b.length() - a.length()) * t);
        }
        a.copy(b);
      }
      if (path[path.length - 1] >= N && above(nodePos(path[path.length - 1], c), a)) poly[m++].copy(c);
      const col = COL[y], drawn = e.gone ? m : Math.max(2, Math.ceil(m * al));
      for (let i = 1; i < drawn; i++) seg(poly[i - 1], poly[i], col, 0.22 * al);
      const speed = Math.min(1.2, 0.04 * gbps + 0.05), n = 3;
      for (let j = 0; j < n; j++) {
        const u = ((wall * speed + j / n + (path[0] * 0.37) % 1) % 1) * (drawn - 1), i = Math.floor(u);
        dot(c.copy(poly[i]).lerp(poly[Math.min(i + 1, drawn - 1)], u - i), col, 9, 0.9 * al);
      }
    }

    // Pings: radius eases out to PING_R (~35 px across at the default zoom), alpha falls to 0.
    let m = 0;
    for (let i = pings.length - 1; i >= 0; i--) {
      const e = pings[i], age = (T - e.born) / PING_S;
      if (age >= 1) { pings.splice(i, 1); continue; }
      if (age < 0) continue;
      const r = PING_R * (0.15 + 0.85 * (1 - (1 - age) ** 3));
      ringM.compose(e.p, ringQ.setFromUnitVectors(Z, ringN.copy(e.p).normalize()), ringS.setScalar(r));
      pingMesh.setMatrixAt(m, ringM);
      pingMesh.setColorAt(m++, ringC.copy(e.c).multiplyScalar(0.55 * (1 - age) ** 2));
    }
    pingMesh.count = m;
    pingMesh.instanceMatrix.needsUpdate = pingMesh.instanceColor.needsUpdate = true;

    lines.setDrawRange(0, k * 2);
    lines.attributes.position.needsUpdate = lines.attributes.color.needsUpdate = true;
    pts.setDrawRange(0, q);
    pts.attributes.position.needsUpdate = pts.attributes.color.needsUpdate = pts.attributes.aux.needsUpdate = true;
  }

  function setOn(on) {
    group.visible = pingMesh.visible = on;
    if (on) poll();
  }
  $("llegend").innerHTML = TYPES.map(([n, c]) => `<span><i style="background:#${c.toString(16).padStart(6, "0")}"></i>${n}</span>`).join("");
  setInterval(poll, 1000);
  return { setOn, frame };
}
