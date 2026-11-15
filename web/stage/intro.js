// The 15-second Demo intro: white -> black -> early-universe glow -> fly through stars and galaxies ->
// SPHEREx with Earth behind it -> turn around to its view -> fade into the live site.
// Everything is drawn live with three.js; nothing is a video, so it is light and stays sharp at any size.
const THREE_URL = "https://unpkg.com/three@0.160.0/build/three.module.js";
const EARTH_URL = "https://unpkg.com/three-globe@2.31.0/example/img/earth-blue-marble.jpg";
let THREE = null;
export const preload = () => (THREE ? Promise.resolve(THREE) : import(THREE_URL).then(m => (THREE = m)));

const ease = x => (x <= 0 ? 0 : x >= 1 ? 1 : x * x * (3 - 2 * x));
const seg = (t, a, b) => ease((t - a) / (b - a));
const lerp = (a, b, k) => a + (b - a) * k;

const CAPTIONS = [
  [1.2, 3.8, "13.8 billion years ago, everything started."],
  [4.2, 7.4, "Space stretched. Galaxies ignited. Quasars blazed."],
  [7.9, 10.4, "Today, SPHEREx maps every corner of the sky, every 6 months."],
  [11.4, 13.6, "This is what it sees."],
];

export async function play({ onSiteReveal, onDone }) {
  await preload();
  const T = THREE;
  const root = document.createElement("div"); root.id = "intro";
  root.innerHTML = `<div class="flash"></div><canvas></canvas><div class="grain"></div><div class="vig"></div>
    <p class="cap"></p><button class="skip" type="button">Skip</button>`;
  document.body.appendChild(root);
  const flash = root.querySelector(".flash"), cv = root.querySelector("canvas"), cap = root.querySelector(".cap");

  const renderer = new T.WebGLRenderer({ canvas: cv, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
  const size = () => { renderer.setSize(innerWidth, innerHeight, false); cam.aspect = innerWidth / innerHeight; cam.updateProjectionMatrix(); };
  const scene = new T.Scene();
  const cam = new T.PerspectiveCamera(60, innerWidth / innerHeight, 0.05, 6000);
  size(); addEventListener("resize", size);

  // ---- early-universe glow: a sphere around the camera with a mottled, cooling surface (like the CMB)
  const glowMat = new T.ShaderMaterial({
    side: T.BackSide, transparent: true, depthWrite: false,
    uniforms: { k: { value: 0 }, op: { value: 0 } },
    vertexShader: `varying vec3 p; void main(){ p = normalize(position); gl_Position = projectionMatrix*modelViewMatrix*vec4(position,1.); }`,
    fragmentShader: `varying vec3 p; uniform float k; uniform float op;
      float h(vec3 q){ return fract(sin(dot(q, vec3(12.9898,78.233,37.719)))*43758.5453); }
      float n(vec3 q){ vec3 i=floor(q), f=fract(q); f=f*f*(3.-2.*f);
        return mix(mix(mix(h(i),h(i+vec3(1,0,0)),f.x),mix(h(i+vec3(0,1,0)),h(i+vec3(1,1,0)),f.x),f.y),
                   mix(mix(h(i+vec3(0,0,1)),h(i+vec3(1,0,1)),f.x),mix(h(i+vec3(0,1,1)),h(i+vec3(1,1,1)),f.x),f.y),f.z); }
      void main(){ float v = .5*n(p*6.)+.3*n(p*13.)+.2*n(p*29.);
        vec3 hot = vec3(1.,.96,.88), warm = vec3(1.,.55,.2), cold = vec3(.35,.08,.05);
        vec3 c = mix(hot, mix(warm, cold, smoothstep(.4,1.,k)), smoothstep(0.,.45,k));
        c *= mix(1., .55 + .9*v, smoothstep(.15,.6,k));
        gl_FragColor = vec4(c, op); }`,
  });
  const glow = new T.Mesh(new T.SphereGeometry(400, 48, 32), glowMat); scene.add(glow);

  // ---- stars: a long tunnel of points; they fade in one by one and spread as space expands
  const N = 9000, pos = new Float32Array(N * 3), base = new Float32Array(N * 3), born = new Float32Array(N), col = new Float32Array(N * 3);
  for (let i = 0; i < N; i++) {
    const r = 6 + Math.pow(Math.random(), 0.7) * 160, a = Math.random() * Math.PI * 2;
    base[3 * i] = Math.cos(a) * r; base[3 * i + 1] = Math.sin(a) * r * 0.7; base[3 * i + 2] = -Math.random() * 2600;
    born[i] = 2.0 + Math.random() * 2.2;
    const w = Math.random(); col.set(w < .15 ? [1, .75, .55] : w < .3 ? [.7, .8, 1] : [1, .97, .92], 3 * i);
  }
  const sg = new T.BufferGeometry(); sg.setAttribute("position", new T.BufferAttribute(pos, 3)); sg.setAttribute("color", new T.BufferAttribute(col, 3));
  const alpha = new Float32Array(N); sg.setAttribute("a", new T.BufferAttribute(alpha, 1));
  const starMat = new T.ShaderMaterial({
    transparent: true, depthWrite: false, blending: T.AdditiveBlending, vertexColors: true,
    uniforms: { px: { value: renderer.getPixelRatio() } },
    vertexShader: `attribute float a; varying float va; varying vec3 vc; uniform float px;
      void main(){ va=a; vc=color; vec4 mv=modelViewMatrix*vec4(position,1.); gl_PointSize = px*clamp(260./-mv.z,1.,7.); gl_Position=projectionMatrix*mv; }`,
    fragmentShader: `varying float va; varying vec3 vc; void main(){ float d=length(gl_PointCoord-.5); gl_FragColor=vec4(vc, va*smoothstep(.5,0.,d)); }`,
  });
  scene.add(new T.Points(sg, starMat));

  // ---- streaks behind the stars while the camera is fast
  const trail = new Float32Array(N * 6), tg = new T.BufferGeometry(); tg.setAttribute("position", new T.BufferAttribute(trail, 3));
  const trailMat = new T.LineBasicMaterial({ color: 0xcfd8ff, transparent: true, opacity: 0, blending: T.AdditiveBlending, depthWrite: false });
  scene.add(new T.LineSegments(tg, trailMat));

  // ---- galaxies: sprites drawn once on a canvas (spirals and ellipticals)
  const galTex = kind => {
    const c = document.createElement("canvas"); c.width = c.height = 128; const g = c.getContext("2d");
    const grd = g.createRadialGradient(64, 64, 0, 64, 64, 64); grd.addColorStop(0, "rgba(255,240,215,1)"); grd.addColorStop(.18, "rgba(255,215,170,.55)"); grd.addColorStop(1, "rgba(0,0,0,0)");
    g.fillStyle = grd; g.fillRect(0, 0, 128, 128);
    if (kind) for (let i = 0; i < 1400; i++) {
      const arm = i % 2, t = Math.random() * 3.2, r = 6 + t * 17, th = t * 1.9 + arm * Math.PI + (Math.random() - .5) * .5;
      g.fillStyle = `rgba(${170 + Math.random() * 80},${190 + Math.random() * 60},255,${.25 * (1 - t / 3.4)})`; g.fillRect(64 + Math.cos(th) * r, 64 + Math.sin(th) * r, 1.5, 1.5);
    }
    const tx = new T.CanvasTexture(c); tx.colorSpace = T.SRGBColorSpace; return tx;
  };
  const texS = galTex(1), texE = galTex(0), gals = [];
  for (let i = 0; i < 260; i++) {
    const m = new T.SpriteMaterial({ map: Math.random() < .7 ? texS : texE, transparent: true, opacity: 0, blending: T.AdditiveBlending, depthWrite: false, rotation: Math.random() * 6.28 });
    const s = new T.Sprite(m), r = 25 + Math.random() * 170, a = Math.random() * 6.28, sc = 4 + Math.random() * 14;
    s.userData = { x: Math.cos(a) * r, y: Math.sin(a) * r * .7, z: -200 - Math.random() * 2300, sc, born: 3.0 + Math.random() * 2.0 };
    s.scale.set(sc, sc * (.45 + Math.random() * .55), 1); scene.add(s); gals.push(s);
  }

  // ---- quasars along the path: a blinding point with two jets, so they read differently from galaxies
  const coreTex = (() => { const c = document.createElement("canvas"); c.width = c.height = 64; const g = c.getContext("2d");
    const r = g.createRadialGradient(32, 32, 0, 32, 32, 32); r.addColorStop(0, "#fff"); r.addColorStop(.12, "rgba(200,220,255,.9)"); r.addColorStop(.4, "rgba(120,150,255,.25)"); r.addColorStop(1, "rgba(0,0,0,0)");
    g.fillStyle = r; g.fillRect(0, 0, 64, 64); const tx = new T.CanvasTexture(c); tx.colorSpace = T.SRGBColorSpace; return tx; })();
  const quasars = [];
  for (let i = 0; i < 7; i++) {
    const q = new T.Group(), a = Math.random() * 6.28, r = 6 + Math.random() * 14;
    const core = new T.Sprite(new T.SpriteMaterial({ map: coreTex, transparent: true, blending: T.AdditiveBlending, depthWrite: false, opacity: 0 }));
    core.scale.set(7, 7, 1); core.userData.op = 1; q.add(core);
    const jm = () => new T.MeshBasicMaterial({ color: 0x9fb8ff, transparent: true, opacity: 0, blending: T.AdditiveBlending, depthWrite: false, side: T.DoubleSide });
    for (const sgn of [1, -1]) { const j = new T.Mesh(new T.ConeGeometry(.35, 22, 12, 1, true), jm()); j.position.y = sgn * 11; j.rotation.z = sgn > 0 ? 0 : Math.PI; j.userData.op = .45; q.add(j); }
    q.rotation.set(Math.random() * 3, Math.random() * 3, Math.random() * 3);
    q.userData = { x: Math.cos(a) * r, y: Math.sin(a) * r * .7, z: -500 - i * 290 - Math.random() * 120, born: 3.2 + i * .25 };
    scene.add(q); quasars.push(q);
  }

  // ---- SPHEREx (a stand-in model: the three nested cone shields, telescope, bus and solar panel) and Earth
  const ship = new T.Group(), metal = new T.MeshStandardMaterial({ color: 0xd9dde3, metalness: .85, roughness: .32, side: T.DoubleSide });
  [[1.7, 1.15, 1.1, 0], [1.95, 1.35, 1.0, .18], [2.2, 1.55, .9, .36]].forEach(([rt, rb, h, y]) => {
    const m = new T.Mesh(new T.CylinderGeometry(rt, rb, h, 6, 1, true), metal); m.position.y = .55 + y; ship.add(m);
  });
  const scope = new T.Mesh(new T.CylinderGeometry(.55, .55, 1.0, 32, 1, true), new T.MeshStandardMaterial({ color: 0x2a2d33, metalness: .4, roughness: .6, side: T.DoubleSide }));
  scope.position.y = .6; ship.add(scope);
  const bus = new T.Mesh(new T.CylinderGeometry(.85, .85, .55, 6), new T.MeshStandardMaterial({ color: 0xb08a3a, metalness: .7, roughness: .45 }));
  bus.position.y = -.3; ship.add(bus);
  const panel = new T.Mesh(new T.BoxGeometry(2.4, .04, 1.1), new T.MeshStandardMaterial({ color: 0x14204a, metalness: .5, roughness: .35 }));
  panel.position.set(0, -.62, 0); ship.add(panel);
  ship.position.set(0, 0, -2650); ship.rotation.set(.35, .6, 0); ship.visible = false; scene.add(ship);

  const earthMat = new T.MeshStandardMaterial({ color: 0x2c5b9c, roughness: .9 });
  new T.TextureLoader().load(EARTH_URL, tx => { tx.colorSpace = T.SRGBColorSpace; earthMat.map = tx; earthMat.color.set(0xffffff); earthMat.needsUpdate = true; });
  const earth = new T.Mesh(new T.SphereGeometry(60, 96, 64), earthMat);
  earth.position.set(-35, -45, -2760); earth.rotation.y = 2.2; earth.visible = false; scene.add(earth);
  const atm = new T.Mesh(new T.SphereGeometry(62.5, 96, 64), new T.ShaderMaterial({
    transparent: true, side: T.BackSide, blending: T.AdditiveBlending, depthWrite: false,
    vertexShader: `varying vec3 n; void main(){ n=normalize(normalMatrix*normal); gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.); }`,
    fragmentShader: `varying vec3 n; void main(){ float i=pow(.72-dot(n,vec3(0,0,1.)),3.); gl_FragColor=vec4(.35,.6,1.,1.)*i; }`,
  }));
  atm.position.copy(earth.position); atm.visible = false; scene.add(atm);
  const sun = new T.DirectionalLight(0xfff4e6, 3.2); sun.position.set(30, 18, 20); scene.add(sun, sun.target);
  sun.target.position.copy(ship.position); scene.add(new T.AmbientLight(0x223044, .35));

  // ---- timeline
  const shipAt = ship.position.clone();
  let t0 = performance.now(), done = false, revealed = false, raf = 0, camZ = 0, lastT = 0;
  const finish = () => {
    if (done) return; done = true; cancelAnimationFrame(raf);
    if (!revealed) { revealed = true; onSiteReveal && onSiteReveal(); }
    root.classList.add("out"); setTimeout(() => { removeEventListener("resize", size); renderer.dispose(); root.remove(); onDone && onDone(); }, 900);
  };
  root.querySelector(".skip").onclick = finish;
  addEventListener("keydown", function k(e) { if (e.key === "Escape") { removeEventListener("keydown", k); finish(); } });

  const frame = () => {
    if (done) return;
    const now = performance.now();
    const t = (now - t0) / 1000, dt = Math.min(.05, t - lastT); lastT = t;
    // white over the site, then the canvas takes over
    flash.style.opacity = t < 1 ? seg(t, 0, .9) : 1 - seg(t, 1, 1.6);
    renderer.setClearColor(new T.Color(1, 1, 1).lerp(new T.Color(0, 0, 0), seg(t, 1.0, 2.0)), 1);
    glowMat.uniforms.k.value = seg(t, 1.0, 3.2); glowMat.uniforms.op.value = (t < 1.6 ? seg(t, 1.0, 1.4) : 1 - seg(t, 2.3, 3.6)) * .95;

    // camera speed: slow start, a long rush, a hard brake at the spacecraft
    const stop = shipAt.z + 9.5, prog = u => ease(ease((u - 2) / 5.8));     // slow start, long rush, hard brake at 7.8 s
    const prevZ = camZ; camZ = stop * prog(t);
    const v = dt > 0 ? Math.abs(camZ - prevZ) / dt : 0;
    const burst = .02 + .98 * Math.pow(seg(t, 1.9, 3.4), .6);    // stars and galaxies erupt out of the glow
    const expand = burst * (1 + seg(t, 3.4, 7.2) * 1.6);           // then space keeps stretching
    for (let i = 0; i < N; i++) {
      pos[3 * i] = base[3 * i] * expand; pos[3 * i + 1] = base[3 * i + 1] * expand; pos[3 * i + 2] = base[3 * i + 2];
      alpha[i] = seg(t, born[i], born[i] + .5) * (1 - .6 * seg(t, 6.6, 7.6) + .6 * seg(t, 10.8, 12.2));   // back to full once we look where SPHEREx looks
      const L = Math.min(60, v * .055);
      trail[6 * i] = pos[3 * i]; trail[6 * i + 1] = pos[3 * i + 1]; trail[6 * i + 2] = pos[3 * i + 2];
      trail[6 * i + 3] = pos[3 * i]; trail[6 * i + 4] = pos[3 * i + 1]; trail[6 * i + 5] = pos[3 * i + 2] - L;
    }
    sg.attributes.position.needsUpdate = sg.attributes.a.needsUpdate = tg.attributes.position.needsUpdate = true;
    trailMat.opacity = .18 * seg(v, 250, 900);
    for (const q of quasars) { const u = q.userData; q.position.set(u.x * expand, u.y * expand, u.z);
      const k = seg(t, u.born, u.born + .3) * (1 - seg(t, 6.9, 7.5)); q.children.forEach(c => (c.material.opacity = k * c.userData.op)); }
    for (const s of gals) { const u = s.userData; s.position.set(u.x * expand, u.y * expand, u.z); s.material.opacity = .85 * seg(t, u.born, u.born + .6) * (1 - seg(t, 6.6, 7.4)); }

    // spacecraft and Earth: arrive, drift, then the camera swings round to look where SPHEREx looks
    const near = t > 5.6; ship.visible = earth.visible = atm.visible = near;
    ship.rotation.y = .6 + t * .04; earth.rotation.y = 2.2 + t * .01;
    // after arriving: a slow drift toward SPHEREx, then the camera turns around to look where SPHEREx looks
    const drift = seg(t, 7.8, 10.5);
    cam.position.set(Math.sin(t * .45) * .2 * drift, .6 + .5 * drift, camZ + (t > 7.8 ? -1.5 * drift : 0));
    const turn = seg(t, 10.5, 12.6) * Math.PI;
    cam.lookAt(cam.position.x - Math.sin(turn) * 10, cam.position.y - .25 * (1 - seg(t, 10.5, 12)), cam.position.z - Math.cos(turn) * 10);
    cam.rotateZ(2 * Math.PI * seg(t, 2.2, 7.8));   // one slow full corkscrew through the galaxies, level again on arrival
    glow.position.copy(cam.position);

    // captions
    const c = CAPTIONS.find(([a, b]) => t >= a && t < b);
    if (c) { if (cap.textContent !== c[2]) cap.textContent = c[2]; cap.style.opacity = Math.min(seg(t, c[0], c[0] + .4), 1 - seg(t, c[1] - .4, c[1])); }
    else cap.style.opacity = 0;

    cv.style.opacity = 1 - seg(t, 13.6, 14.3);                    // fade to black ...
    if (t > 14.3 && !revealed) { revealed = true; onSiteReveal && onSiteReveal(); }
    if (t > 13) { root.style.background = "#000"; root.style.transition = "none"; root.style.opacity = 1 - seg(t, 14.3, 15); }   // ... then black fades away to whatever the site is
    renderer.render(scene, cam);
    if (t >= 15) return finish();
    raf = requestAnimationFrame(frame);
  };
  t0 = performance.now(); raf = requestAnimationFrame(frame);
}
