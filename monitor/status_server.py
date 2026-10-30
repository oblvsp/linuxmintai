#!/usr/bin/env python3
"""Панель состояния linuxmintai: bastion + GPU + память на 127.0.0.1:11436.

    python3 status_server.py [--port 11436] [--bastion http://127.0.0.1:11434]

Эндпоинты:
    /            HTML-дашборд (обновляется раз в секунду)
    /api/status  JSON для дашборда и внешних потребителей
    /api/text    4 строки текста для desklet/Conky
"""
import argparse
import json
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASTION = "http://127.0.0.1:11434/broker/status"
PORT = 11436
CACHE_TTL = 0.7

PHASE_RU = {
    "idle": "простой",
    "generating": "генерация",
    "loading": "загрузка модели",
    "queued": "очередь",
    "offline": "bastion недоступен",
}

_cache = {"at": 0.0, "payload": None}
_lock = threading.Lock()


def _short(name):
    return name.split(":")[0]


def read_mem():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, _, rest = line.partition(":")
            info[key] = int(rest.split()[0])
    total = info["MemTotal"] // 1024
    available = info.get("MemAvailable", info["MemFree"]) // 1024
    swap_total = info["SwapTotal"] // 1024
    swap_used = (info["SwapTotal"] - info["SwapFree"]) // 1024
    return {
        "total_mb": total,
        "used_mb": total - available,
        "swap_total_mb": swap_total,
        "swap_used_mb": swap_used,
    }


def read_load1():
    try:
        with open("/proc/loadavg") as f:
            return float(f.read().split()[0])
    except OSError:
        return None


def gpu_direct():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=1,
        ).stdout.strip().splitlines()[0]
        util, used, total, temp = (x.strip() for x in out.split(","))
        return {
            "util_pct": int(util), "vram_used_mb": int(used),
            "vram_total_mb": int(total), "temp_c": int(temp),
        }
    except Exception:
        return None


def fetch_bastion():
    with urllib.request.urlopen(BASTION, timeout=1.5) as r:
        return json.load(r)


def build_payload():
    mem = read_mem()
    try:
        s = fetch_bastion()
    except Exception:
        return {
            "ts": round(time.time(), 3),
            "phase": "offline",
            "model": None,
            "queue_depth": None,
            "loaded": [],
            "gpu": gpu_direct(),
            "mem": mem,
            "load1": read_load1(),
            "requests": None,
            "swaps": None,
            "stall": "",
        }

    loaded = [m.get("name", "?") for m in (s.get("loaded_models") or [])]
    inflight = list((s.get("inflight_models") or {}).keys())
    queued = list((s.get("queue_by_model") or {}).keys())
    loaded_short = {_short(n) for n in loaded}
    pending = [n for n in inflight if _short(n) not in loaded_short]

    if pending:
        phase = "loading"
    elif inflight:
        phase = "generating"
    elif s.get("queue_depth"):
        phase = "queued"
    else:
        phase = "idle"

    if phase == "queued":
        model = queued[0] if queued else None
    else:
        model = (pending or inflight or loaded or [None])[0]
    g = s.get("gpu") or {}
    return {
        "ts": round(time.time(), 3),
        "phase": phase,
        "model": model,
        "queue_depth": s.get("queue_depth"),
        "loaded": [
            {"name": m.get("name", "?"),
             "vram_gb": m.get("declared_vram_gb")
             or round((m.get("size_vram") or 0) / 1e9, 1)}
            for m in (s.get("loaded_models") or [])
        ],
        "gpu": {
            "util_pct": g.get("compute_utilization_pct"),
            "vram_used_mb": g.get("vram_used_mb"),
            "vram_total_mb": g.get("vram_total_mb"),
            "temp_c": g.get("temperature_c"),
            "power_w": g.get("power_draw_watts"),
            "fan_pct": g.get("fan_speed_pct"),
        },
        "mem": mem,
        "load1": read_load1(),
        "requests": s.get("total_requests_served"),
        "swaps": s.get("total_model_swaps"),
        "stall": s.get("stall_reason") or "",
    }


def current():
    now = time.time()
    with _lock:
        if _cache["payload"] is None or now - _cache["at"] > CACHE_TTL:
            _cache["payload"] = build_payload()
            _cache["at"] = now
        return _cache["payload"]


def as_text(p):
    phase = PHASE_RU.get(p["phase"], p["phase"])
    head = phase if not p.get("model") else f"{phase} · {_short(p['model'])}"
    g = p.get("gpu") or {}
    if g.get("vram_used_mb") is not None:
        line2 = (f"VRAM {g['vram_used_mb'] / 1024:.1f}/{g['vram_total_mb'] / 1024:.1f} GB"
                 f" · GPU {g.get('util_pct', '—')}% · {g.get('temp_c', '—')}°C")
    else:
        line2 = "GPU: нет данных"
    m = p["mem"]
    line3 = (f"RAM {m['used_mb'] / 1024:.1f}/{m['total_mb'] / 1024:.1f} GB"
             f" · swap {m['swap_used_mb'] / 1024:.1f}/{m['swap_total_mb'] / 1024:.1f} GB")
    if p["phase"] == "offline":
        line4 = "проверьте: systemctl --user status bastion"
    else:
        line4 = (f"очередь {p['queue_depth']} · запросов {p['requests']}"
                 f" · переключений {p['swaps']}")
    return "\n".join([head, line2, line3, line4])


PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>linuxmintai · состояние</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 :root { color-scheme: dark; }
 * { box-sizing: border-box; }
 body { margin:0; background:#0f1115; color:#e6e8eb;
        font:14px/1.45 "Segoe UI", system-ui, sans-serif; }
 .wrap { max-width:660px; margin:0 auto; padding:28px 20px 40px; }
 h1 { font-size:15px; font-weight:600; letter-spacing:.04em; text-transform:uppercase;
      color:#8b95a5; margin:0 0 16px; }
 .card { background:#171a21; border:1px solid #232833; border-radius:14px;
         padding:18px 20px; margin-bottom:12px; }
 .phase { display:flex; align-items:center; gap:9px; font-size:16px; font-weight:600; }
 .dot { width:9px; height:9px; border-radius:50%; background:#6b7686;
        box-shadow:0 0 0 4px rgba(255,255,255,.04); }
 .model { color:#9aa4b2; font-size:13px; margin-top:4px; }
 .row { display:flex; justify-content:space-between; align-items:baseline;
        margin:14px 0 6px; font-variant-numeric:tabular-nums; }
 .row span:last-child { color:#9aa4b2; font-size:12.5px; }
 .bar { height:8px; background:#232833; border-radius:6px; overflow:hidden; }
 .fill { height:100%; width:0; border-radius:6px; transition:width .35s ease; }
 .grid { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; }
 .metric { background:#12151b; border-radius:10px; padding:10px 12px; }
 .metric b { display:block; font-size:18px; font-weight:600; font-variant-numeric:tabular-nums;
             white-space:nowrap; }
 .metric i { font-style:normal; color:#7c8698; font-size:11.5px; text-transform:uppercase;
             letter-spacing:.05em; }
 .foot { display:flex; justify-content:space-between; color:#6b7686; font-size:12px; }
 canvas { width:100%; height:46px; display:block; margin-top:12px; }
</style></head>
<body><div class="wrap">
 <h1>linuxmintai · состояние</h1>
 <div class="card">
   <div class="phase"><div class="dot" id="dot"></div><span id="phase">…</span></div>
   <div class="model" id="model">подключение к bastion…</div>
   <div class="row"><span>VRAM</span><span id="vram-t">—</span></div>
   <div class="bar"><div class="fill" id="vram-b"></div></div>
   <div class="row"><span>RAM</span><span id="ram-t">—</span></div>
   <div class="bar"><div class="fill" id="ram-b"></div></div>
   <canvas id="spark" width="620" height="46"></canvas>
 </div>
 <div class="card"><div class="grid">
   <div class="metric"><i>GPU</i><b id="gpu">—</b></div>
   <div class="metric"><i>температура</i><b id="temp">—</b></div>
   <div class="metric"><i>очередь</i><b id="queue">—</b></div>
 </div></div>
 <div class="card"><div class="grid">
   <div class="metric"><i>запросов</i><b id="req">—</b></div>
   <div class="metric"><i>переключений</i><b id="swaps">—</b></div>
   <div class="metric"><i>swap</i><b id="swap">—</b></div>
 </div></div>
 <div class="foot"><span id="stall"></span><span id="age"></span></div>
</div>
<script>
const COLORS = {
  idle:       ["#6b7686", "простой"],
  generating: ["#4cc38a", "генерация"],
  loading:    ["#e0b341", "загрузка модели"],
  queued:     ["#e0b341", "ожидание в очереди"],
  offline:    ["#e5484d", "bastion недоступен"]
};
const history = [];
const $ = id => document.getElementById(id);
function bar(el, ratio) {
  el.style.width = Math.max(0, Math.min(100, ratio * 100)) + "%";
  el.style.background = ratio > .92 ? "#e5484d" : ratio > .8 ? "#e0b341" : "#4cc38a";
}
function spark() {
  const c = $("spark"), ctx = c.getContext("2d");
  const w = c.width, h = c.height;
  ctx.clearRect(0, 0, w, h);
  ctx.strokeStyle = "#232833"; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(0, h - 1); ctx.lineTo(w, h - 1); ctx.stroke();
  if (history.length < 2) return;
  ctx.beginPath();
  history.forEach((v, i) => {
    const x = i * w / Math.max(1, history.length - 1);
    const y = h - 2 - (v / 100) * (h - 6);
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  });
  ctx.strokeStyle = "#4cc38a"; ctx.lineWidth = 2; ctx.stroke();
}
function render(s) {
  const [color, label] = COLORS[s.phase] || COLORS.idle;
  $("dot").style.background = color;
  $("phase").textContent = label;
  $("model").textContent = s.model ? s.model : (s.phase === "offline"
      ? "проверьте: systemctl --user status bastion" : "модель не загружена");
  const g = s.gpu || {};
  if (g.vram_used_mb != null) {
    bar($("vram-b"), g.vram_used_mb / g.vram_total_mb);
    $("vram-t").textContent = (g.vram_used_mb / 1024).toFixed(1) + " / "
      + (g.vram_total_mb / 1024).toFixed(1) + " GB";
    $("gpu").textContent = (g.util_pct ?? "—") + "%";
    $("temp").textContent = (g.temp_c ?? "—") + "°C";
  }
  const m = s.mem;
  bar($("ram-b"), m.used_mb / m.total_mb);
  $("ram-t").textContent = (m.used_mb / 1024).toFixed(1) + " / "
    + (m.total_mb / 1024).toFixed(1) + " GB";
  $("swap").textContent = (m.swap_used_mb / 1024).toFixed(1) + " GB";
  $("queue").textContent = s.queue_depth ?? "—";
  $("req").textContent = s.requests ?? "—";
  $("swaps").textContent = s.swaps ?? "—";
  $("stall").textContent = s.stall ? "stall: " + s.stall : "";
  $("age").textContent = "обновлено " + new Date(s.ts * 1000).toLocaleTimeString("ru-RU");
  if (g.util_pct != null) { history.push(g.util_pct); if (history.length > 90) history.shift(); }
  spark();
}
async function tick() {
  try {
    const r = await fetch("/api/status", {cache: "no-store"});
    render(await r.json());
  } catch (e) {
    render({phase: "offline", mem: {used_mb: 0, total_mb: 1, swap_used_mb: 0}, gpu: {}, queue_depth: "—", ts: Date.now() / 1000});
  }
  setTimeout(tick, 1000);
}
tick();
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif self.path == "/api/status":
            self._send(200, json.dumps(current(), ensure_ascii=False), "application/json; charset=utf-8")
        elif self.path == "/api/text":
            self._send(200, as_text(current()), "text/plain; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def log_message(self, *args):
        pass


def main():
    global BASTION
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--bastion", default=BASTION)
    args = ap.parse_args()
    BASTION = args.bastion
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"linuxmintai status: http://127.0.0.1:{args.port}/", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
