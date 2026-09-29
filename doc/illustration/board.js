// Procedural 3D model of the ModRetro Chromatic mainboard (top side).
// Layout follows a photo of the real board: coordinates are in photo pixels (1100 x 1970, origin top-left).
import * as THREE from 'three';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';

export const IMG_W = 1100, IMG_H = 1970;
export const PCB_W = 6.0, PCB_H = PCB_W * IMG_H / IMG_W, PCB_T = 0.12;
const K = PCB_W / IMG_W;                              // world units per photo pixel
export const PX = (x, y) => new THREE.Vector3((x - IMG_W / 2) * K, PCB_T / 2, (y - IMG_H / 2) * K);

function rng(seed) { return () => { seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 4294967296; }; }
function roundRect(g, x, y, w, h, r) {
  g.beginPath(); g.moveTo(x + r, y); g.arcTo(x + w, y, x + w, y + h, r); g.arcTo(x + w, y + h, x, y + h, r);
  g.arcTo(x, y + h, x, y, r); g.arcTo(x, y, x + w, y, r); g.closePath();
}
function tex(c, srgb = true) { const t = new THREE.CanvasTexture(c); if (srgb) t.colorSpace = THREE.SRGBColorSpace; t.anisotropy = 16; return t; }
function canvas(w, h) { const c = document.createElement('canvas'); c.width = w; c.height = h; return c; }

// ------------------------------------------------------------------------------------------ layout
// Board outline (photo px): left notch, large bottom-right radius.
const OUTLINE = [[14, 40], [1086, 40], [1086, 1810], ['arc', 930, 1810, 156], [14, 1966], [14, 730], [100, 730], [100, 468], [14, 468]];
const HOLES = [[300, 1512, 24], [800, 1506, 28], [650, 176, 14], [1022, 60, 16], [70, 1850, 20], [1070, 1465, 18]];
const U4 = [500, 415, 712, 638];
const DPAD = [[240, 1180], [122, 1282], [358, 1282], [240, 1398]];
const RINGPADS = [[958, 1222], [770, 1308], [445, 1612], [680, 1612]];
const BLOBS = [
  { c: DPAD, r: 92, extra: [[240, 1290, 110]] },
  { c: [[958, 1222], [770, 1308]], r: 110, extra: [[864, 1265, 90], [850, 1180, 70], [880, 1350, 70]] },
  { c: [[445, 1612], [680, 1612]], r: 105, extra: [[562, 1612, 80], [562, 1520, 70], [562, 1705, 70]] },
];
const TPS = [[125, 66], [125, 130], [438, 292], [478, 318], [872, 332], [888, 440], [855, 500], [1000, 205], [1000, 245],
  [654, 707], [708, 832], [940, 1030], [75, 1215], [118, 1236], [512, 1432], [548, 1450], [606, 1452], [628, 1358],
  [120, 1575], [66, 1555], [766, 1570], [180, 1528], [440, 1735], [688, 1043], [214, 1094], [212, 1124], [500, 1135]];
const HEADER = Array.from({ length: 6 }, (_, i) => [1034, 214 + i * 38]);

// Components: [kind, x0, y0, x1, y1, h, opts]
const PARTS = [
  ['esp32', 580, 0, 768, 265, 0.22, { label: 'ESP32-MINI-1', lh: 1.9, ref: 'U18' }],
  ['sop', 510, 282, 592, 390, 0.09, { pins: 'lr', n: 4, text: ['apmemory', 'APS6404L'], label: 'APMemory PSRAM', lh: 1.0, ref: 'U23' }],
  ['qfn', 140, 1610, 235, 1690, 0.08, { text: ['TI', 'DAC3100'], label: 'TLV320DAC3100 codec', lh: 2.3, ref: 'U1' }],
  ['usbc', 492, 1818, 640, 1972, 0.3, { ref: 'J8' }],
  ['jack', 205, 1752, 335, 1990, 0.46, { label: 'Audio jack', lh: 0.8, ref: 'J1' }],
  ['sop', 336, 700, 448, 758, 0.1, { pins: 'tb', n: 14, text: ['LVC245'], ref: 'U10' }],
  ['sop', 604, 744, 696, 826, 0.1, { pins: 'lr', n: 10, text: ['42K', 'AR2S'], ref: 'U11' }],
  ['sop', 884, 790, 978, 930, 0.1, { pins: 'lr', n: 14, text: ['LVC', '245'], ref: 'U9' }],
  ['sop', 906, 156, 970, 220, 0.11, { pins: 'tb', n: 4, text: ['25Q'], ref: 'U6' }],
  ['qfn', 900, 310, 972, 382, 0.08, { text: ['USB'], ref: 'U5' }],
  ['qfn', 250, 716, 298, 764, 0.07, { ref: 'U26' }],
  ['qfn', 548, 1218, 606, 1272, 0.07, { ref: 'U14' }],
  ['qfn', 560, 1034, 616, 1086, 0.07, { ref: 'U7' }],
  ['qfn', 918, 698, 976, 746, 0.07, { ref: 'U9' }],
  ['switch', 258, 26, 358, 92, 0.22, { ref: 'SW3' }],
  ['xtal', 980, 395, 1022, 425, 0.09, { ref: 'Y1' }], ['xtal', 335, 405, 375, 440, 0.09, { ref: 'Y4' }],
  ['xtal', 624, 1052, 660, 1080, 0.08, { ref: 'Y3' }],
  ['ind', 268, 440, 325, 495, 0.14, { ref: 'L3' }], ['ind', 205, 455, 250, 495, 0.13, { ref: 'L11' }],
  ['ind', 85, 930, 135, 970, 0.12, { ref: 'L9' }], ['ind', 270, 875, 320, 915, 0.12, { ref: 'L14' }],
  ['ind', 478, 1290, 516, 1340, 0.1, { ref: 'L10' }],
  ['led', 826, 44, 866, 70, 0.07, { ref: 'D2' }],
  ['sot', 842, 140, 874, 172, 0.06, { ref: 'Q1' }], ['sot', 150, 520, 184, 552, 0.06, { ref: 'U2' }],
  ['sot', 362, 284, 396, 306, 0.06, { ref: 'D13' }], ['sot', 424, 344, 456, 372, 0.06, { ref: 'Q2' }],
  ['sot', 56, 1350, 90, 1382, 0.06, { ref: 'Q4' }], ['sot', 460, 1480, 494, 1512, 0.06, { ref: 'Q5' }],
];
const BIGCAPS = [[435, 120, 485, 160], [435, 165, 485, 205], [505, 200, 540, 240], [505, 150, 540, 190], [220, 360, 262, 405],
  [770, 575, 812, 635], [915, 515, 955, 560], [572, 945, 622, 1000], [270, 1580, 325, 1620], [285, 1665, 330, 1715],
  [30, 1730, 80, 1780], [125, 1740, 170, 1790], [150, 650, 200, 690], [620, 1860, 660, 1900], [800, 820, 830, 860]];
// Zones filled with 0402/0603 passives (photo px rects).
const ZONES = [[30, 90, 175, 310], [110, 330, 200, 440], [330, 110, 420, 260], [770, 100, 900, 250], [790, 290, 890, 420],
  [720, 440, 880, 560], [860, 560, 1000, 690], [60, 540, 150, 700], [190, 560, 300, 690], [200, 770, 320, 850],
  [470, 660, 560, 700], [700, 960, 900, 1010], [960, 700, 1060, 900], [20, 870, 250, 1000], [300, 960, 560, 1020],
  [430, 1100, 700, 1200], [420, 1300, 640, 1420], [20, 1540, 140, 1720], [170, 1530, 320, 1600], [390, 1700, 520, 1780],
  [680, 1830, 820, 1900], [1000, 1010, 1070, 1100], [20, 1380, 80, 1520]];

// ------------------------------------------------------------------------------------------ helpers
function outlinePath(g, s = 1) {
  g.beginPath();
  OUTLINE.forEach((p, i) => {
    if (p[0] === 'arc') { const [, cx, cy, r] = p; g.arc(cx * s, cy * s, r * s, 0, Math.PI / 2); return; }
    i ? g.lineTo(p[0] * s, p[1] * s) : g.moveTo(p[0] * s, p[1] * s);
  });
  g.closePath();
}
function route(g, s, a, b) {  // 45-degree router
  const dx = b[0] - a[0], dy = b[1] - a[1];
  g.beginPath(); g.moveTo(a[0] * s, a[1] * s);
  if (Math.abs(dy) > Math.abs(dx)) g.lineTo(a[0] * s, (b[1] - Math.sign(dy) * Math.abs(dx)) * s);
  else g.lineTo((b[0] - Math.sign(dx) * Math.abs(dy)) * s, a[1] * s);
  g.lineTo(b[0] * s, b[1] * s); g.stroke();
}
function inRect(x, y, r, m = 0) { return x > r[0] - m && x < r[2] + m && y > r[1] - m && y < r[3] + m; }

// ------------------------------------------------------------------------------------------ build
export function buildBoard(env = {}) {
  const group = new THREE.Group();
  const R = rng(1234);
  const S = 2;                                           // canvas px per photo px
  const CW = IMG_W * S, CH = IMG_H * S;
  const col = canvas(CW, CH), rough = canvas(CW, CH), metal = canvas(CW, CH), bump = canvas(CW, CH);
  const gc = col.getContext('2d'), gr = rough.getContext('2d'), gm = metal.getContext('2d'), gb = bump.getContext('2d');

  // base: glossy black solder mask
  gc.fillStyle = '#0d100e'; gc.fillRect(0, 0, CW, CH);
  gr.fillStyle = '#4a4a4a'; gr.fillRect(0, 0, CW, CH);     // mask roughness ~0.3
  gm.fillStyle = '#000'; gm.fillRect(0, 0, CW, CH);
  gb.fillStyle = '#000'; gb.fillRect(0, 0, CW, CH);
  // subtle copper pour texture under mask
  for (let i = 0; i < 9000; i++) {
    const x = R() * CW, y = R() * CH; gc.fillStyle = `rgba(40,52,44,${0.05 + R() * 0.06})`; gc.fillRect(x, y, 2 + R() * 3, 2 + R() * 3);
  }

  // traces (copper under mask: slightly lighter + raised)
  const trace = (a, b, w = 3) => {
    gc.strokeStyle = '#1c231e'; gc.lineWidth = w * S; gc.lineCap = gc.lineJoin = 'round'; route(gc, S, a, b);
    gb.strokeStyle = '#fff'; gb.lineWidth = w * S; gb.lineCap = gb.lineJoin = 'round'; route(gb, S, a, b);
  };
  const via = (x, y) => {
    gc.fillStyle = '#8d7a45'; gc.beginPath(); gc.arc(x * S, y * S, 5 * S, 0, 7); gc.fill();
    gc.fillStyle = '#050505'; gc.beginPath(); gc.arc(x * S, y * S, 2.2 * S, 0, 7); gc.fill();
    gm.fillStyle = '#fff'; gm.beginPath(); gm.arc(x * S, y * S, 5 * S, 0, 7); gm.fill();
    gr.fillStyle = '#555'; gr.beginPath(); gr.arc(x * S, y * S, 5 * S, 0, 7); gr.fill();
  };
  const bundle = (a, b, n, sp = 8, w = 3) => {
    const dx = b[0] - a[0], dy = b[1] - a[1], l = Math.hypot(dx, dy), px = -dy / l, py = dx / l;
    for (let i = 0; i < n; i++) {
      const o = (i - (n - 1) / 2) * sp;
      const A = [a[0] + px * o, a[1] + py * o], B = [b[0] + px * o, b[1] + py * o];
      trace(A, B, w); if (R() < 0.5) via(B[0], B[1]);
    }
  };
  const cx4 = (U4[0] + U4[2]) / 2, cy4 = (U4[1] + U4[3]) / 2;
  bundle([cx4 - 30, U4[1]], [550, 395], 8, 8);            // -> PSRAM
  bundle([cx4 + 40, U4[1]], [700, 262], 6, 9);            // -> ESP32
  bundle([U4[0], cy4 - 40], [330, 560], 10, 8);           // -> left (LCD / U10)
  bundle([U4[0], cy4 + 50], [400, 700], 8, 8);            // -> U10
  bundle([cx4, U4[3]], [650, 744], 8, 8);                 // -> U11
  bundle([U4[2], cy4 + 40], [884, 820], 10, 8);           // -> U9
  bundle([U4[2], cy4 - 40], [900, 382], 6, 9);            // -> U5
  bundle([cx4 - 60, U4[3]], [240, 1180], 3, 10);          // -> D-pad
  bundle([cx4 + 60, U4[3]], [770, 1210], 3, 10);          // -> A/B
  bundle([cx4, U4[3]], [560, 1500], 4, 9, 2.5);           // -> start/select
  bundle([cx4 - 90, U4[3]], [190, 1610], 5, 8, 2.5);      // -> codec
  bundle([600, 1090], [566, 1818], 4, 8, 3.5);            // -> USB-C
  bundle([190, 1690], [260, 1752], 3, 12, 3);             // codec -> jack
  bundle([668, 470], [668, 265], 4, 10);                  // -> ESP32
  for (let i = 0; i < 120; i++) {                         // local routing between passives
    const z = ZONES[Math.floor(R() * ZONES.length)];
    const a = [z[0] + R() * (z[2] - z[0]), z[1] + R() * (z[3] - z[1])];
    const b = [a[0] + (R() - 0.5) * 220, a[1] + (R() - 0.5) * 220];
    trace(a, b, 2 + R() * 2); if (R() < 0.6) via(b[0], b[1]);
  }
  // power/ground stitching vias
  for (let i = 0; i < 160; i++) {
    const x = 30 + R() * 1040, y = 60 + R() * 1880;
    if (BLOBS.some(bl => bl.c.some(([cx, cy]) => Math.hypot(x - cx, y - cy) < bl.r + 15))) continue;
    if (inRect(x, y, U4, 10)) continue;
    via(x, y);
  }

  // BGA landing pads under the FPGA (visible once the chip is lifted)
  gc.fillStyle = '#0a0c0b'; gc.fillRect(U4[0] * S, U4[1] * S, (U4[2] - U4[0]) * S, (U4[3] - U4[1]) * S);
  for (let i = 0; i < 16; i++) for (let j = 0; j < 16; j++) {
    const x = U4[0] + 14 + i * ((U4[2] - U4[0] - 28) / 15), y = U4[1] + 14 + j * ((U4[3] - U4[1] - 28) / 15);
    gc.fillStyle = '#c9a458'; gc.beginPath(); gc.arc(x * S, y * S, 4.2 * S, 0, 7); gc.fill();
    gm.fillStyle = '#fff'; gm.beginPath(); gm.arc(x * S, y * S, 4.2 * S, 0, 7); gm.fill();
    gr.fillStyle = '#404040'; gr.beginPath(); gr.arc(x * S, y * S, 4.2 * S, 0, 7); gr.fill();
  }

  // button areas: silkscreen blob outline, then exposed gold pads
  for (const bl of BLOBS) {
    const circles = [...bl.c.map(([x, y]) => [x, y, bl.r]), ...bl.extra];
    gc.fillStyle = '#e6e6de';
    for (const [x, y, r] of circles) { gc.beginPath(); gc.arc(x * S, y * S, (r + 3) * S, 0, 7); gc.fill(); }
    gc.fillStyle = '#060807';
    for (const [x, y, r] of circles) { gc.beginPath(); gc.arc(x * S, y * S, r * S, 0, 7); gc.fill(); }
  }
  const gold = (draw) => { for (const [g, c] of [[gc, '#d9b263'], [gm, '#fff'], [gr, '#3a3a3a']]) { g.fillStyle = c; g.strokeStyle = c; draw(g); } };
  for (const [x, y] of DPAD) gold(g => {
    g.beginPath(); g.arc(x * S, y * S, 56 * S, 0, 7); g.fill();
    if (g === gc) { g.fillStyle = '#3a2f1a'; g.fillRect((x - 3) * S, (y - 56) * S, 6 * S, 112 * S); g.fillRect((x - 56) * S, (y - 3) * S, 56 * S, 6 * S); }
  });
  for (const [x, y] of RINGPADS) gold(g => {
    g.lineWidth = 7 * S;
    for (const r of [48, 34, 20]) { g.beginPath(); g.arc(x * S, y * S, r * S, 0.25, Math.PI * 2 - 0.25); g.stroke(); }
    g.beginPath(); g.arc(x * S, y * S, 7 * S, 0, 7); g.fill();
    g.fillRect(x * S, (y - 4) * S, 50 * S, 8 * S);
  });
  // test points and header
  for (const [x, y] of TPS) gold(g => { g.beginPath(); g.arc(x * S, y * S, 9 * S, 0, 7); g.fill(); });
  for (const [x, y] of HEADER) gold(g => { g.lineWidth = 6 * S; g.beginPath(); g.arc(x * S, y * S, 13 * S, 0, 7); g.stroke(); });
  for (const [x, y, r] of HOLES) gold(g => { g.lineWidth = 7 * S; g.beginPath(); g.arc(x * S, y * S, (r + 5) * S, 0, 7); g.stroke(); });
  // plated edge ring
  gold(g => { g.lineWidth = 7 * S; g.save(); outlinePath(g, S); g.clip(); outlinePath(g, S); g.lineWidth = 14 * S; g.stroke(); g.restore(); });
  gc.save(); outlinePath(gc, S); gc.clip(); gc.strokeStyle = '#0d100e'; gc.lineWidth = 6 * S; outlinePath(gc, S); gc.stroke(); gc.restore();

  // silkscreen: part outlines + reference designators + board name
  gc.strokeStyle = gc.fillStyle = '#e6e6de'; gc.lineWidth = 2.2 * S; gc.font = `500 ${17 * S}px Ubuntu`;
  for (const [kind, x0, y0, x1, y1, , o] of PARTS) {
    if (!['usbc', 'jack', 'esp32'].includes(kind)) gc.strokeRect((x0 - 6) * S, (y0 - 6) * S, (x1 - x0 + 12) * S, (y1 - y0 + 12) * S);
    if (o.ref) gc.fillText(o.ref, (x1 + 8) * S, (y0 + 14) * S);
  }
  gc.strokeRect((U4[0] - 8) * S, (U4[1] - 8) * S, (U4[2] - U4[0] + 16) * S, (U4[3] - U4[1] + 16) * S);
  gc.fillText('U4', (U4[2] - 30) * S, (U4[3] + 26) * S);
  ['B4', 'B2', 'B3', 'B1'].forEach((t, i) => gc.fillText(t, (DPAD[i][0] + (i === 1 ? 60 : i === 2 ? -84 : 18)) * S, (DPAD[i][1] + (i === 0 ? 76 : i === 3 ? -62 : 6)) * S));
  ['B5', 'B6', 'B7', 'B8'].forEach((t, i) => gc.fillText(t, (RINGPADS[i][0] - 20) * S, (RINGPADS[i][1] + 76) * S));
  const refs = ['R97', 'R96', 'C125', 'R95', 'R93', 'R94', 'R99', 'R98', 'R100'];
  refs.forEach((t, i) => gc.fillText(t, 60 * S, (110 + i * 22) * S));
  gc.save(); gc.translate(1040 * S, 1440 * S); gc.rotate(Math.PI / 2);
  gc.font = `700 ${58 * S}px Ubuntu`; gc.fillText('CHROMATIC', 0, 0);
  gc.font = `500 ${30 * S}px Ubuntu`; gc.fillText('MODRETRO', 0, -54 * S); gc.restore();

  // ---- board mesh (extruded outline with drilled holes)
  const shape = new THREE.Shape();
  const sp = ([x, y]) => [(x - IMG_W / 2) * K, (IMG_H / 2 - y) * K];
  OUTLINE.forEach((p, i) => {
    if (p[0] === 'arc') { const [, cx, cy, r] = p; const [ax, ay] = sp([cx, cy]); shape.absarc(ax, ay, r * K, 0, -Math.PI / 2, true); return; }
    const [x, y] = sp(p); i ? shape.lineTo(x, y) : shape.moveTo(x, y);
  });
  for (const [x, y, r] of HOLES) { const h = new THREE.Path(); const [hx, hy] = sp([x, y]); h.absarc(hx, hy, r * K, 0, Math.PI * 2, true); shape.holes.push(h); }
  const geo = new THREE.ExtrudeGeometry(shape, { depth: PCB_T, bevelEnabled: false, curveSegments: 32 });
  const W = PCB_W, H = PCB_H;
  const mk = (c, srgb) => { const t = tex(c, srgb); t.repeat.set(1 / W, 1 / H); t.offset.set(0.5, 0.5); return t; };
  const bumpT = mk(bump, false);
  const top = new THREE.MeshPhysicalMaterial({
    map: mk(col, true), roughnessMap: mk(rough, false), metalnessMap: mk(metal, false), bumpMap: bumpT, bumpScale: 0.6,
    roughness: 1, metalness: 1, clearcoat: 0.4, clearcoatRoughness: 0.35, envMapIntensity: 0.8,
  });
  const edge = new THREE.MeshStandardMaterial({ color: 0x2b2a20, roughness: 0.7 });
  const board = new THREE.Mesh(geo, [top, edge]);
  board.rotation.x = -Math.PI / 2; board.position.y = -PCB_T / 2;
  board.castShadow = board.receiveShadow = true;
  group.add(board);

  // ------------------------------------------------------------------------------------------ parts
  const M = {
    black: new THREE.MeshStandardMaterial({ color: 0x151518, roughness: 0.55, metalness: 0.1 }),
    tin: new THREE.MeshStandardMaterial({ color: 0xc9ccd2, roughness: 0.3, metalness: 1 }),
    steel: new THREE.MeshStandardMaterial({ color: 0x8c9199, roughness: 0.42, metalness: 1, envMapIntensity: 0.7 }),
    cap: new THREE.MeshStandardMaterial({ color: 0xa88963, roughness: 0.55 }),
    res: new THREE.MeshStandardMaterial({ color: 0x141414, roughness: 0.5 }),
    ind: new THREE.MeshStandardMaterial({ color: 0x3a3c40, roughness: 0.6, metalness: 0.2 }),
    sub: new THREE.MeshStandardMaterial({ color: 0x14171b, roughness: 0.4 }),
    white: new THREE.MeshStandardMaterial({ color: 0x9a9a92, roughness: 0.6 }),
  };
  const pins = [], pads = [];                               // matrices for instanced pins / QFN pads
  const comps = [];
  const topLabel = (w, d, lines, bg = '#17171a', fg = '#7c818c') => {
    const c = canvas(256, Math.max(64, Math.round(256 * d / w))), g = c.getContext('2d');
    g.fillStyle = bg; g.fillRect(0, 0, c.width, c.height);
    g.fillStyle = fg; g.textAlign = 'center';
    const fs = Math.min(64, c.height / (lines.length + 1.2));
    g.font = `500 ${fs}px Ubuntu Mono`;
    lines.forEach((l, i) => g.fillText(l, c.width / 2, c.height / 2 + (i - (lines.length - 1) / 2) * fs * 1.1 + fs * 0.35));
    g.fillStyle = '#2a2b30'; g.beginPath(); g.arc(22, 22, 10, 0, 7); g.fill();
    const m = new THREE.Mesh(new THREE.PlaneGeometry(w * 0.96, d * 0.96), new THREE.MeshStandardMaterial({ map: tex(c), roughness: 0.55 }));
    m.rotation.x = -Math.PI / 2; return m;
  };
  const box = (w, h, d, mat, r = 0) => new THREE.Mesh(r ? new RoundedBoxGeometry(w, h, d, 2, r) : new THREE.BoxGeometry(w, h, d), mat);
  const u4c = PX(cx4, cy4);

  for (const [kind, x0, y0, x1, y1, h, o] of PARTS) {
    const c = PX((x0 + x1) / 2, (y0 + y1) / 2), w = (x1 - x0) * K, d = (y1 - y0) * K;
    const g = new THREE.Group(); g.position.copy(c);
    const add = (m, y) => { m.position.y = y; m.castShadow = true; m.receiveShadow = true; g.add(m); return m; };
    if (kind === 'sop' || kind === 'qfn') {
      const inset = kind === 'sop' ? 0.028 : 0;
      const bw = o.pins === 'lr' ? w - 2 * inset : w, bd = o.pins === 'tb' ? d - 2 * inset : d;
      add(box(bw, h, bd, M.black, 0.008), h / 2);
      const lab = topLabel(bw, bd, o.text || []); add(lab, h + 0.001);
      if (kind === 'sop') {
        for (let i = 0; i < o.n; i++) for (const s of [-1, 1]) {
          const t = (i + 0.5) / o.n - 0.5, m4 = new THREE.Matrix4();
          if (o.pins === 'lr') m4.compose(new THREE.Vector3(c.x + s * (w / 2 - inset / 2), c.y + 0.012, c.z + t * bd * 0.9), new THREE.Quaternion(), new THREE.Vector3(inset + 0.01, 0.018, bd * 0.9 / o.n * 0.45));
          else m4.compose(new THREE.Vector3(c.x + t * bw * 0.9, c.y + 0.012, c.z + s * (d / 2 - inset / 2)), new THREE.Quaternion(), new THREE.Vector3(bw * 0.9 / o.n * 0.45, 0.018, inset + 0.01));
          pins.push([m4, g]);
        }
      } else {
        const n = 8;
        for (let i = 0; i < n; i++) for (const s of [-1, 1]) {
          const t = (i + 0.5) / n - 0.5;
          pads.push([new THREE.Matrix4().compose(new THREE.Vector3(c.x + s * w / 2, c.y + 0.006, c.z + t * d * 0.85), new THREE.Quaternion(), new THREE.Vector3(0.012, 0.012, d * 0.85 / n * 0.5)), g]);
          pads.push([new THREE.Matrix4().compose(new THREE.Vector3(c.x + t * w * 0.85, c.y + 0.006, c.z + s * d / 2), new THREE.Quaternion(), new THREE.Vector3(w * 0.85 / n * 0.5, 0.012, 0.012)), g]);
        }
      }
    } else if (kind === 'esp32') {
      add(box(w, 0.05, d, M.sub), 0.025);
      // antenna keep-out with printed antenna
      const ca = canvas(512, 190), ga = ca.getContext('2d'); ga.fillStyle = '#14171b'; ga.fillRect(0, 0, 512, 190);
      ga.strokeStyle = '#6c5a2e'; ga.lineWidth = 10; ga.beginPath(); ga.moveTo(40, 150);
      for (let i = 0; i < 8; i++) { ga.lineTo(40 + i * 56, 40); ga.lineTo(68 + i * 56, 40); ga.lineTo(68 + i * 56, 150); ga.lineTo(96 + i * 56, 150); } ga.stroke();
      const ant = new THREE.Mesh(new THREE.PlaneGeometry(w * 0.98, (70 * K) * 0.98), new THREE.MeshStandardMaterial({ map: tex(ca), roughness: 0.45 }));
      ant.rotation.x = -Math.PI / 2; ant.position.set(0, 0.051, -d / 2 + 35 * K); g.add(ant);
      const cw = (765 - 592) * K, cd = (252 - 72) * K;
      const can = add(box(cw, h - 0.05, cd, M.steel, 0.01), 0.05 + (h - 0.05) / 2); can.position.z = (72 + 252) / 2 * K - (y0 + y1) / 2 * K;
      const ct = canvas(512, 530), gt = ct.getContext('2d');
      gt.fillStyle = '#b9bdc5'; gt.fillRect(0, 0, 512, 530);
      for (let i = 0; i < 530; i += 3) { gt.fillStyle = `rgba(255,255,255,${0.04 + (i % 7) * 0.006})`; gt.fillRect(0, i, 512, 1); }
      gt.fillStyle = '#5d626b'; gt.font = '700 58px Ubuntu'; gt.fillText('ESPRESSIF', 110, 90);
      gt.font = '500 46px Ubuntu Mono'; gt.fillText('ESP32-MINI-1', 60, 160); gt.font = '400 30px Ubuntu Mono';
      ['CE', 'FCC ID: 2AC7Z-', 'ESP32MINI1', 'MGN4'].forEach((l, i) => gt.fillText(l, 60, 240 + i * 42));
      for (let i = 0; i < 12; i++) for (let j = 0; j < 12; j++) if ((i * 7 + j * 13 + i * j) % 3) { gt.fillRect(330 + i * 12, 230 + j * 12, 11, 11); }
      const lab = new THREE.Mesh(new THREE.PlaneGeometry(cw * 0.97, cd * 0.97), new THREE.MeshStandardMaterial({ map: tex(ct), color: 0x9a9ea6, roughness: 0.5, metalness: 0.6, envMapIntensity: 0.6 }));
      lab.rotation.x = -Math.PI / 2; lab.position.set(0, h + 0.001, can.position.z); g.add(lab);
    } else if (kind === 'usbc') {
      const s = add(box(w, h, d * 0.92, M.steel, 0.06), h / 2); s.position.z = d * 0.04;
      const mouth = new THREE.Mesh(new RoundedBoxGeometry(w * 0.8, h * 0.45, 0.02, 2, 0.04), M.black);
      mouth.position.set(0, h / 2, d / 2 + 0.002); g.add(mouth);
    } else if (kind === 'jack') {
      add(box(w, h, d * 0.86, M.black, 0.03), h / 2);
      const ring = new THREE.Mesh(new THREE.CylinderGeometry(h * 0.36, h * 0.36, 0.08, 32), M.steel);
      ring.rotation.x = Math.PI / 2; ring.position.set(0, h / 2, d * 0.45); g.add(ring);
      const hole = new THREE.Mesh(new THREE.CylinderGeometry(h * 0.2, h * 0.2, 0.09, 24), new THREE.MeshBasicMaterial({ color: 0x000000 }));
      hole.rotation.x = Math.PI / 2; hole.position.copy(ring.position); hole.position.z += 0.005; g.add(hole);
      for (const s of [-1, 1]) { const t = add(box(0.05, 0.02, 0.12, M.tin), 0.01); t.position.set(s * (w / 2 + 0.02), 0.01, -d * 0.2); }
    } else if (kind === 'switch') {
      add(box(w, h * 0.7, d, M.steel, 0.01), h * 0.35);
      const lever = add(box(w * 0.22, h * 0.5, d * 0.5, M.black, 0.01), h * 0.7 + h * 0.2); lever.position.x = -w * 0.18;
    } else if (kind === 'xtal') {
      add(box(w, h, d, M.steel, 0.01), h / 2);
    } else if (kind === 'ind') {
      add(box(w, h, d, M.ind, 0.02), h / 2);
      for (const s of [-1, 1]) { const t = add(box(w * 0.22, h * 0.5, d * 1.02, M.tin), h * 0.25); t.position.x = s * w * 0.4; }
    } else if (kind === 'led') {
      add(box(w, h, d, M.white, 0.005), h / 2);
    } else if (kind === 'sot') {
      add(box(w * 0.7, h, d * 0.55, M.black, 0.004), h / 2);
      for (const [px, pz] of [[-0.3, -0.42], [0.3, -0.42], [0, 0.42]]) { const t = add(box(w * 0.14, 0.02, d * 0.2, M.tin), 0.01); t.position.set(px * w, 0.01, pz * d); }
    }
    group.add(g);
    comps.push({ g, dist: Math.hypot(c.x - u4c.x, c.z - u4c.z), label: o.label, lh: o.lh || 1, h, kind: 'part' });
  }

  const inst = (geo, mat, arr) => { const m = new THREE.InstancedMesh(geo, mat, arr.length); arr.forEach((a, i) => m.setMatrixAt(i, a)); m.castShadow = m.receiveShadow = true; return m; };
  const unit = new THREE.BoxGeometry(1, 1, 1);

  // pins / QFN pads: instanced per part, in the part's local frame (so they travel with it)
  const byPart = new Map();
  for (const [m4, g] of [...pins, ...pads]) {
    const local = new THREE.Matrix4().makeTranslation(-g.position.x, -g.position.y, -g.position.z).multiply(m4);
    if (!byPart.has(g)) byPart.set(g, []); byPart.get(g).push(local);
  }
  for (const [g, arr] of byPart) g.add(inst(unit, M.tin, arr));

  // passives (instanced), one group per board area so they can be placed in waves
  const passiveSets = [];
  const newSet = (cx, cy) => { const s = { cx, cy, cap: [], res: [], term: [] }; passiveSets.push(s); return s; };
  const addPassive = (set, x, y, l, wdt, h, type, rot) => {
    const c = PX(x, y), q = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 1, 0), rot);
    set[type].push(new THREE.Matrix4().compose(new THREE.Vector3(c.x, c.y + h / 2, c.z), q, new THREE.Vector3(l * 0.62, h, wdt)));
    for (const s of [-1, 1]) {
      const off = new THREE.Vector3(s * l * 0.4, 0, 0).applyQuaternion(q);
      set.term.push(new THREE.Matrix4().compose(new THREE.Vector3(c.x + off.x, c.y + h / 2, c.z + off.z), q, new THREE.Vector3(l * 0.2, h * 1.02, wdt * 1.02)));
    }
  };
  {
    const big = newSet(550, 900);
    for (const [x0, y0, x1, y1] of BIGCAPS) {
      const w = (x1 - x0) * K, d = (y1 - y0) * K, rot = w > d ? 0 : Math.PI / 2;
      addPassive(big, (x0 + x1) / 2, (y0 + y1) / 2, Math.max(w, d), Math.min(w, d), 0.11, 'cap', rot);
    }
  }
  const occupied = (x, y) => PARTS.some(p => inRect(x, y, [p[1], p[2], p[3], p[4]], 14)) || BIGCAPS.some(r => inRect(x, y, r, 10)) ||
    inRect(x, y, U4, 12) || BLOBS.some(bl => bl.c.some(([cx, cy]) => Math.hypot(x - cx, y - cy) < bl.r + 8)) ||
    TPS.some(([tx, ty]) => Math.hypot(x - tx, y - ty) < 16) || HOLES.some(([hx, hy, r]) => Math.hypot(x - hx, y - hy) < r + 14);
  const placed = [];
  for (const z of ZONES) {
    const set = newSet((z[0] + z[2]) / 2, (z[1] + z[3]) / 2);
    const n = Math.round((z[2] - z[0]) * (z[3] - z[1]) / 1300);
    for (let i = 0, tries = 0; i < n && tries < n * 8; tries++) {
      const x = z[0] + R() * (z[2] - z[0]), y = z[1] + R() * (z[3] - z[1]);
      if (occupied(x, y) || placed.some(([px, py]) => Math.abs(x - px) < 22 && Math.abs(y - py) < 16)) continue;
      placed.push([x, y]); i++;
      const bg = R() < 0.3, l = (bg ? 20 : 14) * K, wdt = (bg ? 10 : 7) * K;
      addPassive(set, x, y, l, wdt, bg ? 0.05 : 0.035, R() < 0.55 ? 'cap' : 'res', R() < 0.5 ? 0 : Math.PI / 2);
    }
  }
  for (const set of passiveSets) {
    const g = new THREE.Group(); group.add(g);
    for (const [arr, mat] of [[set.cap, M.cap], [set.res, M.res], [set.term, M.tin]]) if (arr.length) g.add(inst(unit, mat, arr));
    const c = PX(set.cx, set.cy);
    comps.push({ g, dist: Math.hypot(c.x - u4c.x, c.z - u4c.z), h: 0.05, kind: 'passive' });
  }

  // buttons: silicone membrane domes over the contact pads, then the plastic caps
  const rubber = new THREE.MeshPhysicalMaterial({ color: 0x2c3036, roughness: 0.75, sheen: 0.3, transparent: true, opacity: 0.93 });
  const darkPlastic = new THREE.MeshPhysicalMaterial({ color: 0x1b1c20, roughness: 0.45, clearcoat: 0.3 });
  const pinkPlastic = new THREE.MeshPhysicalMaterial({ color: 0xc48c8a, roughness: 0.55, clearcoat: 0.2 });
  const button = (x, y, build) => {
    const g = new THREE.Group(); g.position.copy(PX(x, y)); build(g); group.add(g);
    comps.push({ g, dist: Math.hypot(g.position.x - u4c.x, g.position.z - u4c.z), h: 0.2, kind: 'button' });
  };
  const dome = (g, x, z, r) => {
    const m = new THREE.Mesh(new THREE.SphereGeometry(r, 32, 12, 0, Math.PI * 2, 0, Math.PI * 0.32), rubber);
    m.scale.y = 0.45; m.position.set(x, -r * Math.cos(Math.PI * 0.32) * 0.45 + 0.002, z); m.castShadow = true; g.add(m);
    const skirt = new THREE.Mesh(new THREE.CylinderGeometry(r * 1.22, r * 1.28, 0.02, 32), rubber);
    skirt.position.set(x, 0.01, z); skirt.receiveShadow = true; g.add(skirt);
  };
  // D-pad: 4 domes + cross
  button(240, 1290, g => {
    for (const [x, y] of DPAD) dome(g, (x - 240) * K, (y - 1290) * K, 50 * K);
    const s = new THREE.Shape(), a = 42 * K, b = 150 * K;
    s.moveTo(-a, b); s.lineTo(a, b); s.lineTo(a, a); s.lineTo(b, a); s.lineTo(b, -a); s.lineTo(a, -a); s.lineTo(a, -b);
    s.lineTo(-a, -b); s.lineTo(-a, -a); s.lineTo(-b, -a); s.lineTo(-b, a); s.lineTo(-a, a); s.lineTo(-a, b);
    const cross = new THREE.Mesh(new THREE.ExtrudeGeometry(s, { depth: 0.1, bevelEnabled: true, bevelSize: 0.025, bevelThickness: 0.025, bevelSegments: 3 }), darkPlastic);
    cross.rotation.x = -Math.PI / 2; cross.position.y = 0.1; cross.castShadow = true; g.add(cross);
  });
  // A / B
  for (const [x, y] of RINGPADS.slice(0, 2)) button(x, y, g => {
    dome(g, 0, 0, 52 * K);
    const cap = new THREE.Mesh(new THREE.CylinderGeometry(46 * K, 48 * K, 0.13, 40), pinkPlastic);
    cap.position.y = 0.14; cap.castShadow = true; g.add(cap);
  });
  // Start / Select rubbers
  for (const [x, y] of RINGPADS.slice(2)) button(x, y, g => {
    dome(g, 0, 0, 50 * K);
    const pill = new THREE.Mesh(new THREE.CapsuleGeometry(16 * K, 70 * K, 6, 16), darkPlastic);
    pill.rotation.set(Math.PI / 2, 0, Math.PI / 2 + 0.4); pill.position.y = 0.1; pill.castShadow = true; g.add(pill);
  });

  return { group, comps, usbPos: PX(566, 1935), bare: board, bareMats: [top, edge], u4pos: PX(cx4, cy4), U4_SIZE: (U4[2] - U4[0]) * K };
}
