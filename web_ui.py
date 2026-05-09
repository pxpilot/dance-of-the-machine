"""
Web UI server — runs in a daemon thread alongside the audio loop.
Opens at http://localhost:7777
"""

import json
import time
import threading
from flask import Flask, jsonify, request, Response
import config as cfg

PORT = 7777

app = Flask(__name__)
app.logger.disabled = True
import logging
logging.getLogger("werkzeug").setLevel(logging.ERROR)

# ── Routes ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return HTML

@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify(cfg.get_all())

@app.route("/api/config", methods=["POST"])
def update_config():
    cfg.update(request.get_json(force=True))
    return jsonify({"ok": True})

@app.route("/api/stream")
def stream():
    def generate():
        while True:
            yield f"data: {json.dumps(cfg.get_telemetry())}\n\n"
            time.sleep(0.05)
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/api/test-motors", methods=["POST"])
def test_motors():
    devices, state_motor = cfg.get_devices()
    if not devices and not state_motor:
        return jsonify({"ok": False, "error": "No motors connected"})

    def _run():
        for m in devices:
            try: m.start_speed(0.5)
            except Exception: pass
        time.sleep(2.0)
        for m in devices:
            try: m.start_speed(0)
            except Exception: pass

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True})


# ── Start ──────────────────────────────────────────────────────────────────

def start(port: int = PORT):
    def _run():
        app.run(host="0.0.0.0", port=port, debug=False,
                use_reloader=False, threaded=True)
    threading.Thread(target=_run, daemon=True).start()
    print(f"Web UI → http://localhost:{port}")


# ── HTML ───────────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dance of the Machine</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#080808; --panel:#111; --border:#1e1e1e;
  --green:#00ff88; --yellow:#ffd000; --red:#ff3355;
  --cyan:#00cfff; --dim:#333; --muted:#555; --text:#ccc;
  --font:'SF Mono',ui-monospace,monospace;
}
body{background:var(--bg);color:var(--text);font-family:var(--font);
  min-height:100vh;padding:20px;max-width:640px;margin:0 auto;font-size:13px;}

/* ── header ── */
.header{display:flex;justify-content:space-between;align-items:flex-end;
  margin-bottom:20px;padding-bottom:14px;border-bottom:1px solid var(--border);}
.logo{font-size:11px;letter-spacing:4px;text-transform:uppercase;
  color:var(--cyan);text-shadow:0 0 12px var(--cyan);}
.bpm-block{text-align:right;}
.bpm-num{font-size:36px;line-height:1;color:var(--green);
  text-shadow:0 0 16px var(--green);letter-spacing:-1px;}
.bpm-unit{font-size:9px;letter-spacing:3px;color:var(--muted);text-transform:uppercase;}

/* ── LED strip ── */
.led-strip{display:flex;gap:6px;justify-content:center;margin-bottom:20px;padding:14px;
  background:var(--panel);border-radius:10px;border:1px solid var(--border);}
.led{width:14px;height:14px;border-radius:50%;background:var(--dim);
  transition:background .04s,box-shadow .04s;}
.led.on-chase{background:var(--cyan);box-shadow:0 0 10px var(--cyan),0 0 20px var(--cyan);}
.led.on-beat{background:#fff;box-shadow:0 0 14px #fff,0 0 30px var(--cyan);}

/* ── motor meters ── */
.motors-panel{background:var(--panel);border-radius:10px;border:1px solid var(--border);
  padding:18px 20px;margin-bottom:16px;}
.motor-row{display:grid;grid-template-columns:28px 1fr 46px;
  align-items:center;gap:12px;margin-bottom:14px;}
.motor-row:last-child{margin-bottom:0;}
.m-label{font-size:14px;color:var(--cyan);text-shadow:0 0 8px var(--cyan);font-weight:bold;}
.segs{display:flex;gap:2px;}
.seg{flex:1;height:18px;border-radius:2px;background:var(--dim);transition:background .05s,box-shadow .05s;}
.seg.g{background:var(--green);box-shadow:0 0 6px var(--green);}
.seg.y{background:var(--yellow);box-shadow:0 0 6px var(--yellow);}
.seg.r{background:var(--red);box-shadow:0 0 6px var(--red);}
.m-pct{font-size:12px;color:var(--muted);text-align:right;font-variant-numeric:tabular-nums;}

/* ── status bar ── */
.status-bar{display:flex;align-items:center;gap:12px;
  background:var(--panel);border-radius:10px;border:1px solid var(--border);
  padding:14px 20px;margin-bottom:16px;}
.dir-arrows{font-size:22px;transition:all .15s;color:var(--muted);}
.dir-arrows.active{color:var(--cyan);text-shadow:0 0 12px var(--cyan);}
.beat-lamp{width:24px;height:24px;border-radius:50%;border:2px solid var(--dim);
  background:var(--dim);transition:all .05s;flex-shrink:0;}
.beat-lamp.flash{background:var(--red);border-color:var(--red);
  box-shadow:0 0 16px var(--red),0 0 30px var(--red);}
.beat-label{font-size:9px;letter-spacing:2px;color:var(--muted);text-transform:uppercase;}
.spacer{flex:1;}
.test-btn{
  background:transparent;border:1px solid var(--green);color:var(--green);
  font-family:var(--font);font-size:12px;letter-spacing:2px;text-transform:uppercase;
  padding:8px 18px;border-radius:6px;cursor:pointer;transition:all .15s;
}
.test-btn:hover{background:var(--green);color:#000;box-shadow:0 0 16px var(--green);}
.test-btn:disabled{border-color:var(--dim);color:var(--dim);background:transparent;
  box-shadow:none;cursor:not-allowed;}
.test-btn.running{border-color:var(--yellow);color:var(--yellow);
  box-shadow:0 0 12px var(--yellow);animation:pulse-btn .4s infinite alternate;}
@keyframes pulse-btn{from{opacity:1}to{opacity:.5}}

/* ── params ── */
.params-wrap{background:var(--panel);border-radius:10px;border:1px solid var(--border);
  overflow:hidden;}
.section{border-bottom:1px solid var(--border);}
.section:last-child{border-bottom:none;}
.sec-head{display:flex;justify-content:space-between;align-items:center;
  padding:12px 18px;cursor:pointer;user-select:none;}
.sec-head:hover{background:#141414;}
.sec-title{font-size:9px;letter-spacing:3px;text-transform:uppercase;color:var(--muted);}
.sec-arrow{font-size:10px;color:var(--dim);transition:transform .2s;}
.sec-arrow.open{transform:rotate(180deg);}
.sec-body{padding:4px 18px 14px;display:none;}
.sec-body.open{display:block;}
.row{display:grid;grid-template-columns:175px 1fr 56px;align-items:center;
  gap:10px;margin-top:10px;}
.p-label{font-size:12px;color:#888;}
input[type=range]{width:100%;accent-color:var(--green);cursor:pointer;}
.p-val{font-size:12px;color:var(--green);text-align:right;
  font-variant-numeric:tabular-nums;text-shadow:0 0 6px var(--green);}

/* ── saved toast ── */
#saved{position:fixed;bottom:20px;right:20px;background:#111;border:1px solid var(--green);
  color:var(--green);font-size:11px;letter-spacing:2px;padding:8px 14px;border-radius:6px;
  opacity:0;transition:opacity .3s;text-shadow:0 0 8px var(--green);}
#saved.show{opacity:1;}
</style>
</head>
<body>

<div class="header">
  <div>
    <div class="logo">Dance of the Machine</div>
    <div style="font-size:10px;color:var(--muted);margin-top:3px;letter-spacing:1px;">LIVE CONTROL</div>
  </div>
  <div class="bpm-block">
    <div class="bpm-num" id="bpm">---</div>
    <div class="bpm-unit">BPM</div>
  </div>
</div>

<div class="led-strip" id="led-strip"></div>

<div class="motors-panel" id="motors-panel">
  <div style="color:var(--muted);font-size:12px;letter-spacing:1px;">
    WAITING FOR CONNECTION…
  </div>
</div>

<div class="status-bar">
  <div class="beat-lamp" id="beat-lamp"></div>
  <div class="beat-label">Beat</div>
  <div style="width:1px;height:20px;background:var(--border);margin:0 4px;"></div>
  <div class="dir-arrows" id="dir-sym">◀ ▶</div>
  <div class="spacer"></div>
  <button class="test-btn" id="test-btn" onclick="testMotors()">⚡ TEST MOTORS</button>
</div>

<div class="params-wrap" id="controls"></div>

<div id="saved">✓ SAVED</div>

<script>
// ── LED strip setup ──────────────────────────────────────────────────────
const LED_COUNT = 16;
const ledStrip = document.getElementById('led-strip');
for (let i = 0; i < LED_COUNT; i++) {
  const d = document.createElement('div');
  d.className = 'led';
  d.id = `led${i}`;
  ledStrip.appendChild(d);
}

let chasePos = 0;
let chaseBpm = 120;
let chaseTimer = null;

function startChase() {
  if (chaseTimer) clearInterval(chaseTimer);
  const interval = (60000 / chaseBpm) / 4;  // 4 steps per beat
  chaseTimer = setInterval(() => {
    document.querySelectorAll('.led').forEach(l => l.classList.remove('on-chase'));
    const led = document.getElementById(`led${chasePos % LED_COUNT}`);
    if (led) led.classList.add('on-chase');
    chasePos++;
  }, Math.max(60, interval));
}

function flashLeds() {
  document.querySelectorAll('.led').forEach(l => {
    l.classList.remove('on-chase');
    l.classList.add('on-beat');
  });
  setTimeout(() => {
    document.querySelectorAll('.led').forEach(l => l.classList.remove('on-beat'));
  }, 120);
}

startChase();

// ── Motor meters ─────────────────────────────────────────────────────────
const SEG_COUNT = 24;
let metersReady = false;

function buildMeters(levels) {
  const panel = document.getElementById('motors-panel');
  panel.innerHTML = Object.keys(levels).sort().map(l => `
    <div class="motor-row">
      <span class="m-label">${l}</span>
      <div class="segs" id="segs_${l}">
        ${Array.from({length:SEG_COUNT}, (_,i) => `<div class="seg" id="seg_${l}_${i}"></div>`).join('')}
      </div>
      <span class="m-pct" id="pct_${l}">0%</span>
    </div>`).join('');
  metersReady = true;
}

function updateMotor(letter, pwr) {
  const lit = Math.round(pwr * SEG_COUNT);
  for (let i = 0; i < SEG_COUNT; i++) {
    const seg = document.getElementById(`seg_${letter}_${i}`);
    if (!seg) continue;
    seg.className = 'seg';
    if (i < lit) {
      if (i < SEG_COUNT * 0.6)       seg.classList.add('g');
      else if (i < SEG_COUNT * 0.82) seg.classList.add('y');
      else                            seg.classList.add('r');
    }
  }
  const pct = document.getElementById(`pct_${letter}`);
  if (pct) pct.textContent = Math.round(pwr * 100) + '%';
}

// ── SSE stream ────────────────────────────────────────────────────────────
const es = new EventSource('/api/stream');
es.onmessage = e => {
  const d = JSON.parse(e.data);

  if (!metersReady && d.levels && Object.keys(d.levels).length)
    buildMeters(d.levels);

  if (metersReady) {
    Object.entries(d.levels || {}).forEach(([l, v]) => updateMotor(l, v));
  }

  // Direction
  const ds = document.getElementById('dir-sym');
  if (ds) {
    const fwd = d.direction === 1;
    ds.textContent = fwd ? '▶▶' : '◀◀';
    ds.className = 'dir-arrows active';
  }

  // Beat lamp
  if (d.is_beat) {
    const lamp = document.getElementById('beat-lamp');
    if (lamp) {
      lamp.classList.add('flash');
      setTimeout(() => lamp.classList.remove('flash'), 120);
    }
    flashLeds();
  }

  // BPM
  if (d.bpm && d.bpm > 20) {
    document.getElementById('bpm').textContent = Math.round(d.bpm);
    if (Math.abs(d.bpm - chaseBpm) > 5) {
      chaseBpm = d.bpm;
      startChase();
    }
  }
};

// ── Test button ───────────────────────────────────────────────────────────
function testMotors() {
  const btn = document.getElementById('test-btn');
  btn.disabled = true;
  btn.className = 'test-btn running';
  btn.textContent = '⚡ RUNNING…';

  fetch('/api/test-motors', {method:'POST'})
    .then(r => r.json())
    .then(r => {
      if (!r.ok) { btn.textContent = '✗ ' + (r.error || 'Error'); }
      else        { btn.textContent = '✓ DONE'; }
      setTimeout(() => {
        btn.disabled = false;
        btn.className = 'test-btn';
        btn.textContent = '⚡ TEST MOTORS';
      }, 2500);
    });
}

// ── Parameter sliders ─────────────────────────────────────────────────────
const PARAMS = [
  {s:"Motor",   key:"motor_max",              label:"Max Power",             min:0,      max:1,    step:0.05},
  {s:"Motor",   key:"smoothing",              label:"Attack Smoothing",      min:0,      max:0.99, step:0.01},
  {s:"Motor",   key:"decay",                  label:"Decay Speed",           min:0.01,   max:0.5,  step:0.01},
  {s:"Beat",    key:"beat_threshold",         label:"Beat Threshold",        min:1.0,    max:4.0,  step:0.1},
  {s:"Beat",    key:"beat_hold_frames",       label:"Beat Hold (frames)",    min:1,      max:20,   step:1},
  {s:"Beat",    key:"direction_flip_beats",   label:"Direction Flip (beats)",min:1,      max:16,   step:1},
  {s:"Beat",    key:"tempo_change_threshold", label:"Tempo Sensitivity",     min:0.05,   max:0.5,  step:0.01},
  {s:"Beat",    key:"state_angle",            label:"State Motor Angle",     min:10,     max:360,  step:5},
  {s:"Bass",    key:"bass_floor",             label:"Noise Floor",           min:0.001,  max:0.2,  step:0.001},
  {s:"Bass",    key:"bass_scale",             label:"Amplification",         min:0.5,    max:20,   step:0.5},
  {s:"Mid",     key:"mid_floor",              label:"Noise Floor",           min:0.001,  max:0.2,  step:0.001},
  {s:"Mid",     key:"mid_scale",              label:"Amplification",         min:0.5,    max:20,   step:0.5},
  {s:"Melody",  key:"melody_floor",           label:"Noise Floor",           min:0.0001, max:0.05, step:0.0005},
  {s:"Melody",  key:"melody_scale",           label:"Amplification",         min:0.5,    max:30,   step:0.5},
];

function fmt(v, step) {
  if (step >= 1)    return (+v).toFixed(0);
  if (step >= 0.01) return (+v).toFixed(2);
  return (+v).toFixed(4);
}

const ctrl = document.getElementById('controls');
let curSec = null, secBody = null;

PARAMS.forEach(p => {
  if (p.s !== curSec) {
    curSec = p.s;
    const sec = document.createElement('div');
    sec.className = 'section';
    const open = ['Motor','Beat'].includes(p.s);
    sec.innerHTML = `
      <div class="sec-head" onclick="toggleSec(this)">
        <span class="sec-title">${p.s}</span>
        <span class="sec-arrow ${open?'open':''}">▼</span>
      </div>
      <div class="sec-body ${open?'open':''}"></div>`;
    ctrl.appendChild(sec);
    secBody = sec.querySelector('.sec-body');
  }
  const row = document.createElement('div');
  row.className = 'row';
  row.innerHTML = `
    <span class="p-label">${p.label}</span>
    <input type="range" id="s_${p.key}" min="${p.min}" max="${p.max}" step="${p.step}">
    <span class="p-val" id="v_${p.key}">–</span>`;
  secBody.appendChild(row);

  const sl = row.querySelector('input');
  const vl = row.querySelector('.p-val');
  sl.addEventListener('input', () => { vl.textContent = fmt(sl.value, p.step); });
  sl.addEventListener('change', () => {
    fetch('/api/config', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({[p.key]: +sl.value})
    }).then(() => flashSaved());
  });
});

function toggleSec(head) {
  const body  = head.nextElementSibling;
  const arrow = head.querySelector('.sec-arrow');
  body.classList.toggle('open');
  arrow.classList.toggle('open');
}

fetch('/api/config').then(r=>r.json()).then(cfg => {
  PARAMS.forEach(p => {
    const sl = document.getElementById(`s_${p.key}`);
    const vl = document.getElementById(`v_${p.key}`);
    if (sl && cfg[p.key] != null) {
      sl.value = cfg[p.key];
      vl.textContent = fmt(cfg[p.key], p.step);
    }
  });
});

function flashSaved() {
  const el = document.getElementById('saved');
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 1400);
}
</script>
</body>
</html>"""
