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
log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)

# ── HTML ───────────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dance of the Machine</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0d0d0d;--surface:#161616;--surface2:#1f1f1f;
  --text:#e0e0e0;--muted:#666;--accent:#00e87a;--beat:#ff4455;
  --font:'SF Mono',ui-monospace,monospace;
}
body{background:var(--bg);color:var(--text);font-family:var(--font);
  padding:28px 24px;max-width:580px;margin:0 auto;font-size:13px;}

h1{font-size:15px;letter-spacing:3px;text-transform:uppercase;color:var(--text);margin-bottom:3px;}
.sub{color:var(--muted);font-size:11px;letter-spacing:1px;margin-bottom:28px;}

/* ── meters ── */
.meters{background:var(--surface);border-radius:10px;padding:18px 20px;margin-bottom:24px;}
.meter-row{display:grid;grid-template-columns:24px 1fr 44px;align-items:center;
  gap:10px;margin-bottom:10px;}
.meter-row:last-of-type{margin-bottom:0;}
.m-label{font-size:12px;color:var(--muted);}
.bar-bg{height:6px;background:var(--surface2);border-radius:3px;overflow:hidden;}
.bar{height:100%;background:var(--accent);border-radius:3px;width:0;
  transition:width 50ms linear;}
.m-pct{font-size:11px;color:var(--muted);text-align:right;}
.status-row{display:flex;align-items:center;gap:16px;margin-top:14px;
  padding-top:12px;border-top:1px solid var(--surface2);font-size:12px;color:var(--muted);}
#dir-sym{font-size:18px;color:var(--accent);}
.beat-dot{width:9px;height:9px;background:var(--surface2);border-radius:50%;}
.beat-dot.on{background:var(--beat);}
#bpm-val{margin-left:auto;font-size:11px;}

/* ── sections ── */
.section{margin-bottom:22px;}
.sec-title{font-size:10px;letter-spacing:2px;text-transform:uppercase;color:#444;
  padding-bottom:8px;border-bottom:1px solid #1c1c1c;margin-bottom:14px;}
.row{display:grid;grid-template-columns:180px 1fr 56px;align-items:center;
  gap:10px;margin-bottom:11px;}
.p-label{font-size:12px;color:#aaa;}
input[type=range]{width:100%;accent-color:var(--accent);cursor:pointer;height:4px;}
.p-val{font-size:12px;color:var(--accent);text-align:right;
  font-variant-numeric:tabular-nums;letter-spacing:0.5px;}

/* ── saved flash ── */
#saved{position:fixed;top:20px;right:20px;font-size:11px;color:var(--accent);
  opacity:0;transition:opacity .4s;letter-spacing:1px;}
#saved.show{opacity:1;}
</style>
</head>
<body>
<h1>Dance of the Machine</h1>
<p class="sub">live control panel</p>
<div id="saved">✓ saved</div>

<div class="meters" id="meters"><div style="color:#444;font-size:12px;">Waiting for connection…</div></div>
<div id="controls"></div>

<script>
const PARAMS = [
  {s:"Motor",       key:"motor_max",              label:"Max Power",            min:0,      max:1,    step:0.05},
  {s:"Motor",       key:"smoothing",              label:"Attack Smoothing",     min:0,      max:0.99, step:0.01},
  {s:"Motor",       key:"decay",                  label:"Decay Speed",          min:0.01,   max:0.5,  step:0.01},
  {s:"Beat",        key:"beat_threshold",         label:"Beat Threshold",       min:1.0,    max:4.0,  step:0.1},
  {s:"Beat",        key:"beat_hold_frames",       label:"Beat Hold (frames)",   min:1,      max:20,   step:1},
  {s:"Beat",        key:"direction_flip_beats",   label:"Direction Flip (beats)",min:1,     max:16,   step:1},
  {s:"Beat",        key:"tempo_change_threshold", label:"Tempo Sensitivity",    min:0.05,   max:0.5,  step:0.01},
  {s:"Beat",        key:"state_angle",            label:"State Motor Angle",    min:10,     max:360,  step:5},
  {s:"Bass",        key:"bass_floor",             label:"Noise Floor",          min:0.001,  max:0.2,  step:0.001},
  {s:"Bass",        key:"bass_scale",             label:"Amplification",        min:0.5,    max:20,   step:0.5},
  {s:"Mid",         key:"mid_floor",              label:"Noise Floor",          min:0.001,  max:0.2,  step:0.001},
  {s:"Mid",         key:"mid_scale",              label:"Amplification",        min:0.5,    max:20,   step:0.5},
  {s:"Melody",      key:"melody_floor",           label:"Noise Floor",          min:0.0001, max:0.05, step:0.0005},
  {s:"Melody",      key:"melody_scale",           label:"Amplification",        min:0.5,    max:30,   step:0.5},
];

function fmt(v, step) {
  if (step >= 1)    return (+v).toFixed(0);
  if (step >= 0.01) return (+v).toFixed(2);
  return (+v).toFixed(4);
}

// Build controls
const ctrl = document.getElementById('controls');
let curSection = null, secEl = null;
PARAMS.forEach(p => {
  if (p.s !== curSection) {
    curSection = p.s;
    secEl = document.createElement('div');
    secEl.className = 'section';
    secEl.innerHTML = `<div class="sec-title">${p.s}</div>`;
    ctrl.appendChild(secEl);
  }
  const row = document.createElement('div');
  row.className = 'row';
  row.innerHTML = `
    <span class="p-label">${p.label}</span>
    <input type="range" id="s_${p.key}" min="${p.min}" max="${p.max}" step="${p.step}">
    <span class="p-val" id="v_${p.key}">–</span>`;
  secEl.appendChild(row);

  const sl = row.querySelector('input');
  const vl = row.querySelector('.p-val');
  sl.addEventListener('input', () => { vl.textContent = fmt(sl.value, p.step); });
  sl.addEventListener('change', () => {
    fetch('/api/config', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({[p.key]: +sl.value})
    }).then(() => flashSaved());
  });
});

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

// Meters
let metersReady = false;
function buildMeters(levels) {
  const el = document.getElementById('meters');
  el.innerHTML = Object.keys(levels).sort().map(l => `
    <div class="meter-row">
      <span class="m-label">${l}</span>
      <div class="bar-bg"><div class="bar" id="bar_${l}"></div></div>
      <span class="m-pct" id="pct_${l}">0%</span>
    </div>`).join('') + `
    <div class="status-row">
      <span>Direction</span><span id="dir-sym">▶</span>
      <span>Beat</span><div class="beat-dot" id="beat-dot"></div>
      <span id="bpm-val"></span>
    </div>`;
  metersReady = true;
}

const es = new EventSource('/api/stream');
es.onmessage = e => {
  const d = JSON.parse(e.data);
  if (!metersReady && d.levels && Object.keys(d.levels).length)
    buildMeters(d.levels);
  if (!metersReady) return;

  Object.entries(d.levels||{}).forEach(([l,v]) => {
    const b = document.getElementById(`bar_${l}`);
    const p = document.getElementById(`pct_${l}`);
    if (b) b.style.width = (v*100).toFixed(1)+'%';
    if (p) p.textContent = Math.round(v*100)+'%';
  });
  const ds = document.getElementById('dir-sym');
  if (ds) ds.textContent = d.direction===1 ? '▶' : '◀';
  const bd = document.getElementById('beat-dot');
  if (bd && d.is_beat) { bd.classList.add('on'); setTimeout(()=>bd.classList.remove('on'),120); }
  const bv = document.getElementById('bpm-val');
  if (bv && d.bpm) bv.textContent = Math.round(d.bpm)+' BPM';
};

function flashSaved() {
  const el = document.getElementById('saved');
  el.classList.add('show');
  setTimeout(()=>el.classList.remove('show'), 1200);
}
</script>
</body>
</html>"""


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
            data = json.dumps(cfg.get_telemetry())
            yield f"data: {data}\n\n"
            time.sleep(0.05)   # 20 fps

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Start ──────────────────────────────────────────────────────────────────

def start(port: int = PORT):
    def _run():
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)

    threading.Thread(target=_run, daemon=True).start()
    print(f"Web UI → http://localhost:{port}")
