#!/usr/bin/env python3
"""Хаб linuxmintai: фаза bastion, стек, ресурсы, модели и бэкапы на 127.0.0.1:11436.

    python3 status_server.py [--port 11436] [--bastion http://127.0.0.1:11434]

Эндпоинты:
    /            авто: вёрстка по ориентации окна, переключается на лету
    /v1          вертикальная вёрстка под портретный экран 1024×1280
    /v2          широкий пульт под основной экран 1920×1080
    /api/status  JSON для дашборда и внешних потребителей (живые метрики)
    /api/hub     JSON сводки хаба (стек, модели, бэкапы, сбои)
    /api/text    4 строки текста для desklet/Conky
"""
import argparse
import json
import os
import subprocess
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

BASTION = "http://127.0.0.1:11434/broker/status"
PORT = 11436
CACHE_TTL = 0.7

HOSTAGENT = "http://172.17.0.1:8767"
WEBUI = "http://127.0.0.1:3000"
SEARXNG = "http://127.0.0.1:8888"
OLLAMA_TAGS = "http://127.0.0.1:11434/api/tags"
BACKUP_RUNS = "/media/vsp/0FD14653488FB355/locallmstack-backup/runs"
TIMER_UNITS = ("locallmstack-backup.timer", "locallmstack-backup-full.timer",
               "locallmstack-models.timer")
HUB_TTL = 6.0

PHASE_RU = {
    "idle": "простой",
    "generating": "генерация",
    "loading": "загрузка модели",
    "queued": "очередь",
    "offline": "bastion недоступен",
}

_cache = {"at": 0.0, "payload": None}
_lock = threading.Lock()
_hub_cache = {"at": 0.0, "payload": None}
_hub_lock = threading.Lock()


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


def _fetch_json(url, timeout=1.5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def _fetch_status(url, timeout=1.5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status


def _safe(fn, *args):
    try:
        return fn(*args), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def docker_states():
    try:
        out = subprocess.run(
            ["docker", "inspect", "open-webui", "searxng", "--format",
             "{{.Name}}|{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}"
             "{{else}}-{{end}}|{{.Config.Image}}|{{.State.StartedAt}}"],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except Exception:
        return {}
    states = {}
    for line in out.splitlines():
        name, status, health, image, started = (line.split("|") + [""] * 5)[:5]
        states[name.lstrip("/")] = {
            "status": status, "health": health, "image": image, "started": started,
        }
    return states


def timer_states():
    try:
        out = subprocess.run(
            ["systemctl", "--user", "list-timers", "--all", "--output=json"],
            capture_output=True, text=True, timeout=3,
        ).stdout
        rows = json.loads(out) if out.strip() else []
    except Exception:
        return []
    by_unit = {r.get("unit"): r for r in rows if isinstance(r, dict)}
    result = []
    for unit in TIMER_UNITS:
        r = by_unit.get(unit)
        if not r:
            result.append({"unit": unit, "present": False})
            continue
        # systemd отдаёт next/last в микросекундах epoch
        next_s = (r.get("next") or 0) / 1e6 or None
        last_s = (r.get("last") or 0) / 1e6 or None
        result.append({"unit": unit, "present": True, "next": next_s, "last": last_s})
    return result


def backup_summary():
    base = BACKUP_RUNS
    try:
        runs = sorted(
            (d for d in os.listdir(base)
             if os.path.isdir(os.path.join(base, d)) and not d.startswith(".")),
            reverse=True,
        )
    except OSError as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "location": base}
    if not runs:
        return {"ok": True, "runs": 0, "location": base}

    path = os.path.join(base, runs[0])
    meta = {}
    try:
        with open(os.path.join(path, "manifest.txt")) as f:
            for line in f:
                if line[:1].isspace() or "=" not in line:
                    continue
                key, _, value = line.strip().partition("=")
                meta[key] = value
    except OSError:
        pass

    size = files = 0
    try:
        for entry in os.scandir(path):
            if entry.is_file():
                size += entry.stat().st_size
                files += 1
    except OSError:
        pass

    try:
        counts = json.loads(meta.get("counts", "{}"))
    except ValueError:
        counts = {}
    return {
        "ok": True,
        "runs": len(runs),
        "latest": runs[0],
        "date": meta.get("date"),
        "size_bytes": size,
        "files": files,
        "sums": os.path.exists(os.path.join(path, "SHA256SUMS")),
        "counts": counts,
        "image": meta.get("image", ""),
        "ollama": meta.get("ollama", ""),
        "location": base,
    }


def build_hub():
    state = current()

    with ThreadPoolExecutor(max_workers=4) as ex:
        f_host = ex.submit(_safe, _fetch_json, HOSTAGENT + "/metrics", 1.5)
        f_web = ex.submit(_safe, _fetch_json, WEBUI + "/health", 1.5)
        f_search = ex.submit(_safe, _fetch_status, SEARXNG + "/", 1.5)
        f_tags = ex.submit(_safe, _fetch_json, OLLAMA_TAGS, 2.0)
        host, host_err = f_host.result()
        web, web_err = f_web.result()
        search, search_err = f_search.result()
        tags, tags_err = f_tags.result()

    docker = docker_states()
    loaded = {_short(m["name"]) for m in state.get("loaded") or []}

    def svc(name, ok, detail, url, container=None):
        st = docker.get(container) if container else None
        if st and st.get("health") not in (None, "-"):
            detail = f"{detail} · {st['health']}"
        return {"id": name, "ok": bool(ok), "detail": detail, "url": url,
                "started": (st or {}).get("started")}

    services = [
        svc("Open WebUI", web is not None and web.get("status") is True,
            "HTTP 200" if web_err is None else web_err, WEBUI, "open-webui"),
        svc("SearXNG", search == 200,
            "HTTP 200" if search == 200 else (search_err or f"HTTP {search}"),
            SEARXNG, "searxng"),
        svc("Bastion / Ollama", state["phase"] != "offline",
            PHASE_RU.get(state["phase"], state["phase"]) + " · :11434",
            "http://127.0.0.1:11434"),
        svc("Hostagent", host is not None,
            "метрики хоста" if host_err is None else host_err, HOSTAGENT),
    ]

    model_list = []
    for m in (tags or {}).get("models", []):
        name = m.get("name", "?")
        model_list.append({
            "name": name,
            "size_gb": round((m.get("size") or 0) / 1e9, 1),
            "loaded": _short(name) in loaded,
        })

    if host:
        mem = host.get("memory") or {}
        disks = host.get("disks") or []
        gpu = host.get("gpu") or {}
        resources = {
            "ok": True,
            "load": host.get("load"),
            "uptime_s": host.get("uptime_seconds"),
            "mem_total": mem.get("total"), "mem_used": mem.get("used"),
            "mem_pct": mem.get("percent"),
            "swap_total": mem.get("swap_total"),
            "swap_used": (mem.get("swap_total") or 0) - (mem.get("swap_free") or 0),
            "disks": [{"target": d.get("target"), "pct": d.get("percent"),
                       "used": d.get("used"), "total": d.get("total")} for d in disks],
            "gpu_pct": gpu.get("util_percent"), "gpu_temp": gpu.get("temperature_c"),
            "gpu_used": gpu.get("memory_used"), "gpu_total": gpu.get("memory_total"),
        }
    else:
        mem = read_mem()
        g = gpu_direct() or {}
        resources = {
            "ok": False, "error": host_err, "load": [read_load1()],
            "mem_total": mem["total_mb"] * 1024 * 1024,
            "mem_used": mem["used_mb"] * 1024 * 1024,
            "mem_pct": round(mem["used_mb"] * 100 / max(1, mem["total_mb"]), 1),
            "swap_total": mem["swap_total_mb"] * 1024 * 1024,
            "swap_used": mem["swap_used_mb"] * 1024 * 1024,
            "disks": [],
            "gpu_pct": g.get("util_pct"), "gpu_temp": g.get("temp_c"),
            "gpu_used": (g.get("vram_used_mb") or 0) * 1024 * 1024,
            "gpu_total": (g.get("vram_total_mb") or 0) * 1024 * 1024,
        }

    return {
        "ts": round(time.time(), 3),
        "phase": state["phase"],
        "services": services,
        "models": model_list,
        "models_error": tags_err,
        "backups": backup_summary(),
        "timers": timer_states(),
        "resources": resources,
        "failed": {
            "system": (host or {}).get("failed_units") or [],
            "user": (host or {}).get("user_failed_units") or [],
        },
        "links": [
            {"title": "Open WebUI", "url": WEBUI},
            {"title": "SearXNG", "url": SEARXNG},
            {"title": "Этот хаб", "url": f"http://127.0.0.1:{PORT}/"},
        ],
    }


def current_hub():
    now = time.time()
    with _hub_lock:
        if _hub_cache["payload"] is None or now - _hub_cache["at"] > HUB_TTL:
            _hub_cache["payload"] = build_hub()
            _hub_cache["at"] = now
        return _hub_cache["payload"]


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
<title>linuxmintai · хаб</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 :root { color-scheme: dark; }
 * { box-sizing: border-box; }
 body { margin:0; background:#0f1115; color:#e6e8eb;
        font:14px/1.45 "Segoe UI", system-ui, sans-serif; }
 .wrap { margin:0 auto; }
 header { display:flex; align-items:center; gap:12px; padding:18px 0 14px; }
 h1 { font-size:14px; font-weight:600; letter-spacing:.05em; text-transform:uppercase;
      color:#8b95a5; margin:0; }
 h2 { font-size:12px; font-weight:600; letter-spacing:.06em; text-transform:uppercase;
      color:#7c8698; margin:0 0 12px; }
 .switch { margin-left:auto; display:flex; gap:3px; background:#171a21;
           border:1px solid #232833; border-radius:9px; padding:3px; }
 .switch a { color:#8b95a5; text-decoration:none; font-size:12px; padding:4px 10px;
             border-radius:6px; white-space:nowrap; }
 .switch a.on { background:#232833; color:#e6e8eb; }
 .card { background:#171a21; border:1px solid #232833; border-radius:14px;
         padding:16px 18px; }
 .phase { display:flex; align-items:center; gap:9px; font-size:20px; font-weight:600; }
 .dot, .sdot { width:9px; height:9px; border-radius:50%; background:#6b7686;
        box-shadow:0 0 0 4px rgba(255,255,255,.04); flex:none; }
 .sdot.ok { background:#4cc38a; }
 .sdot.bad { background:#e5484d; }
 .model { color:#9aa4b2; font-size:13.5px; margin-top:4px; }
 .row { display:flex; justify-content:space-between; align-items:baseline;
        margin:14px 0 6px; font-variant-numeric:tabular-nums; }
 .row span:last-child { color:#9aa4b2; font-size:12.5px; }
 .bar { height:8px; background:#232833; border-radius:6px; overflow:hidden; }
 .fill { height:100%; width:0; border-radius:6px; transition:width .35s ease; }
 .metrics { display:grid; gap:8px; margin-top:14px; }
 .grid { display:grid; grid-template-columns:repeat(3,1fr); gap:8px; }
 .metric { background:#12151b; border-radius:10px; padding:9px 11px; }
 .metric b { display:block; font-size:20px; font-weight:600; font-variant-numeric:tabular-nums;
             white-space:nowrap; }
 .metric i { font-style:normal; color:#7c8698; font-size:11px; text-transform:uppercase;
             letter-spacing:.05em; }
 .foot { display:flex; justify-content:space-between; color:#6b7686; font-size:12px;
         padding:10px 2px 0; }
 canvas { width:100%; height:56px; display:block; margin-top:12px; }
 .svc, .mrow, .trow { display:flex; align-items:center; gap:9px;
        padding:7px 0; border-bottom:1px solid #1e222b; }
 .svc:last-child, .mrow:last-child, .trow:last-child { border-bottom:0; }
 .sname { font-weight:600; }
 .sdet { color:#8b95a5; font-size:12.5px; margin-left:auto; text-align:right; }
 .svc a, .links a { color:#7cb3ff; text-decoration:none; }
 .svc a { margin-left:10px; }
 .mmeta { margin-left:auto; color:#8b95a5; font-size:12.5px; }
 .badge { display:inline-block; padding:1px 8px; border-radius:20px; font-size:11.5px;
          background:#1d3a2c; color:#6fd7a1; margin-left:8px; }
 .kv { display:grid; grid-template-columns:auto 1fr; gap:4px 12px; font-size:13px; }
 .kv i { font-style:normal; color:#7c8698; }
 .muted { color:#7c8698; font-size:12.5px; }
 .timer { margin-top:10px; padding-top:10px; border-top:1px solid #1e222b; }
 .trow { font-size:12.5px; padding:4px 0; border-bottom:0; }
 .trow span:first-child { color:#b6bfcc; }
 .trow span:last-child { margin-left:auto; color:#8b95a5; }
 .links { display:flex; gap:10px; flex-wrap:wrap; }
 .links a { background:#12151b; border:1px solid #232833; border-radius:10px;
            padding:9px 12px; }
 .warn { color:#e0b341; font-size:12.5px; }
 .bad { color:#e5484d; }

 /* v1 — вертикальная вёрстка под портретный экран 1024×1280 */
 .v1 .wrap { max-width:1024px; padding:0 18px 24px; }
 .v1 .board { display:grid; grid-template-columns:1fr; gap:10px; }
 .v1 .phase { font-size:23px; }
 .v1 .model { font-size:14px; }
 .v1 .metrics { grid-template-columns:repeat(3,1fr); }
 .v1 .metric b { font-size:23px; }
 .v1 canvas { height:64px; }
 .v1 .svc, .v1 .mrow, .v1 .trow { padding:8px 0; font-size:14px; }

 /* v2 — широкий пульт под основной экран 1920×1080 */
 .v2 .wrap { max-width:1880px; padding:0 26px 20px; }
 .v2 .board { display:grid; grid-template-columns:repeat(12,1fr); gap:12px;
              align-items:start; }
 .v2 .hero { grid-column:span 7; }
 .v2 .c-svc { grid-column:span 5; }
 .v2 .c-models, .v2 .c-backups, .v2 .c-res { grid-column:span 4; }
 .v2 .c-failed { grid-column:span 8; }
 .v2 .c-links { grid-column:span 4; }
 .v2 .metrics { grid-template-columns:repeat(6,1fr); }
 .v2 .phase { font-size:22px; }
 .v2 .metric b { font-size:22px; }
 @media (max-width:1200px) {
   .v2 .board { grid-template-columns:1fr 1fr; }
   .v2 .hero, .v2 .c-svc, .v2 .c-failed { grid-column:span 2; }
   .v2 .c-models, .v2 .c-backups, .v2 .c-res, .v2 .c-links { grid-column:span 1; }
 }
 @media (max-width:760px) {
   .v2 .board { grid-template-columns:1fr; }
   .v2 .hero, .v2 .c-svc, .v2 .c-models, .v2 .c-backups, .v2 .c-res,
   .v2 .c-failed, .v2 .c-links { grid-column:span 1; }
 }
</style></head>
<body class="__CLS__"><script>
(function () {
  if (document.body.className !== "auto") return;
  const pick = () => innerHeight > innerWidth ? "v1" : "v2";
  document.body.className = pick();
  addEventListener("resize", () => { document.body.className = pick(); });
})();
</script><div class="wrap">
 <header><h1>linuxmintai · хаб</h1>
   <nav class="switch">
     <a href="/" class="__ONA__">авто</a><a href="/v1" class="__ON1__">вертикальная</a><a href="/v2" class="__ON2__">широкая</a>
   </nav>
 </header>
 <div class="board">
  <section class="card hero">
   <div class="phase"><div class="dot" id="dot"></div><span id="phase">…</span></div>
   <div class="model" id="model">подключение к bastion…</div>
   <div class="row"><span>VRAM</span><span id="vram-t">—</span></div>
   <div class="bar"><div class="fill" id="vram-b"></div></div>
   <div class="row"><span>RAM</span><span id="ram-t">—</span></div>
   <div class="bar"><div class="fill" id="ram-b"></div></div>
   <canvas id="spark"></canvas>
   <div class="metrics">
     <div class="metric"><i>GPU</i><b id="gpu">—</b></div>
     <div class="metric"><i>температура</i><b id="temp">—</b></div>
     <div class="metric"><i>очередь</i><b id="queue">—</b></div>
     <div class="metric"><i>запросов</i><b id="req">—</b></div>
     <div class="metric"><i>переключений</i><b id="swaps">—</b></div>
     <div class="metric"><i>swap</i><b id="swap">—</b></div>
   </div>
  </section>
  <section class="card c-svc"><h2>Стек</h2><div id="services" class="muted">…</div></section>
  <section class="card c-models"><h2>Модели</h2><div id="models" class="muted">…</div></section>
  <section class="card c-backups"><h2>Бэкапы</h2><div id="backups" class="muted">…</div></section>
  <section class="card c-res"><h2>Ресурсы</h2><div id="resources" class="muted">…</div></section>
  <section class="card c-failed"><h2>Внимание</h2><div id="failed" class="muted">…</div></section>
  <section class="card c-links"><h2>Ссылки</h2><div id="links" class="links">…</div></section>
 </div>
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
const esc = s => String(s).replace(/[&<>"']/g, c => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
}[c]));
function fmtSize(bytes) {
  if (bytes == null) return "—";
  const gb = bytes / 1e9;
  return gb >= 1 ? gb.toFixed(2) + " GB" : Math.round(bytes / 1e6) + " MB";
}
function fmtDate(sec) {
  if (!sec) return "—";
  return new Date(sec * 1000).toLocaleString("ru-RU",
    {day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit"});
}
function fmtRel(sec) {
  if (!sec) return "";
  const d = sec - Date.now() / 1000, a = Math.abs(d);
  let v, u;
  if (a < 3600) { v = Math.round(a / 60); u = "мин"; }
  else if (a < 86400) { v = (a / 3600).toFixed(1); u = "ч"; }
  else { v = (a / 86400).toFixed(1); u = "д"; }
  return d >= 0 ? `через ${v} ${u}` : `${v} ${u} назад`;
}
function fmtUptime(sec) {
  if (sec == null) return "—";
  const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600);
  return d ? `${d} д ${h} ч` : `${h} ч`;
}
function bar(el, ratio, warn, bad) {
  el.style.width = Math.max(0, Math.min(100, ratio * 100)) + "%";
  el.style.background = ratio > .92 ? "#e5484d" : ratio > .8 ? "#e0b341" : "#4cc38a";
}
function spark() {
  const c = $("spark"), ctx = c.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth || 600, h = c.clientHeight || 56;
  if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) {
    c.width = Math.round(w * dpr); c.height = Math.round(h * dpr);
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
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
function renderHub(d) {
  $("services").innerHTML = d.services.map(s =>
    `<div class="svc"><span class="sdot ${s.ok ? "ok" : "bad"}"></span>` +
    `<span class="sname">${esc(s.id)}</span>` +
    `<span class="sdet">${esc(s.detail || "")}</span>` +
    `<a href="${esc(s.url)}" target="_blank" rel="noreferrer">↗</a></div>`).join("");

  $("models").innerHTML = d.models.length ? d.models.map(m =>
    `<div class="mrow"><span>${esc(m.name)}</span>` +
    `<span class="mmeta">${m.size_gb} GB` +
    (m.loaded ? `<span class="badge">загружена</span>` : "") + `</span></div>`).join("")
    : `<span class="bad">модели недоступны${d.models_error ? ": " + esc(d.models_error) : ""}</span>`;

  const b = d.backups || {};
  if (!b.ok) {
    $("backups").innerHTML = `<span class="bad">хранилище недоступно: ${esc(b.error || "")}</span>`;
  } else if (!b.runs) {
    $("backups").innerHTML = "снимков пока нет";
  } else {
    const c = b.counts || {};
    const counts = ["user", "chat", "model", "tool", "function"]
      .filter(k => c[k] != null).map(k => `${k} ${c[k]}`).join(" · ");
    let html = `<div class="kv">` +
      `<i>снимок</i><span>${esc(b.latest)}` +
      `<span class="badge" style="background:#1e2a3a;color:#7cb3ff">${b.runs} всего</span></span>` +
      `<i>дата</i><span>${esc(b.date || "—")}</span>` +
      `<i>размер</i><span>${fmtSize(b.size_bytes)} · ${b.files} файлов</span>` +
      `<i>контроль</i><span class="${b.sums ? "" : "warn"}">` +
      (b.sums ? "SHA256SUMS на месте" : "SHA256SUMS отсутствует") + `</span>` +
      `<i>состав</i><span>${esc(counts || "—")}</span></div>`;
    const timers = (d.timers || []).filter(t => t.present);
    if (timers.length) {
      html += `<div class="timer">` + timers.map(t =>
        `<div class="trow"><span>${esc(t.unit.replace("locallmstack-", "").replace(".timer", ""))}</span>` +
        `<span>${t.next ? "след. " + fmtDate(t.next) + " (" + fmtRel(t.next) + ")" :
          "след. —"}</span></div>`).join("") + `</div>`;
    } else {
      html += `<div class="timer muted">таймеры недоступны</div>`;
    }
    $("backups").innerHTML = html;
  }

  const r = d.resources || {};
  const load = (r.load || [])[0];
  const disk = (r.disks || []).find(x => x.target === "/") || (r.disks || [])[0];
  $("resources").innerHTML = `<div class="grid">` +
    `<div class="metric"><i>load 1m</i><b>${load != null ? load.toFixed(2) : "—"}</b></div>` +
    `<div class="metric"><i>RAM</i><b>${r.mem_pct != null ? r.mem_pct + "%" : "—"}</b></div>` +
    `<div class="metric"><i>диск /</i><b>${disk ? disk.pct + "%" : "—"}</b></div>` +
    `<div class="metric"><i>GPU</i><b>${r.gpu_pct ?? "—"}${r.gpu_pct != null ? "%" : ""}</b></div>` +
    `<div class="metric"><i>темп.</i><b>${r.gpu_temp ?? "—"}${r.gpu_temp != null ? "°C" : ""}</b></div>` +
    `<div class="metric"><i>uptime</i><b>${fmtUptime(r.uptime_s)}</b></div>` +
    `</div>` + (r.ok ? "" : `<div class="muted" style="margin-top:8px">` +
      `hostagent недоступен: ${esc(r.error || "")}</div>`);

  const failed = [...(d.failed?.system || []), ...(d.failed?.user || [])];
  $("failed").innerHTML = failed.length ? failed.map(f => {
    const parts = f.trim().split(/\\s+/);
    const unit = parts[0] || f;
    const state = parts[2] || "failed";
    const benign = unit.startsWith("casper-md5check") ? " (безвреден на Mint)" : "";
    return `<div class="warn">${esc(unit)} — ${esc(state)}${benign}</div>`;
  }).join("") : "сбоев нет";

  $("links").innerHTML = (d.links || []).map(l =>
    `<a href="${esc(l.url)}" target="_blank" rel="noreferrer">${esc(l.title)} ↗</a>`).join("");
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
async function tickHub() {
  try {
    const r = await fetch("/api/hub", {cache: "no-store"});
    renderHub(await r.json());
  } catch (e) {}
  setTimeout(tickHub, 6000);
}
tick();
tickHub();
</script>
</body></html>
"""


def page(variant):
    return (PAGE
            .replace("__CLS__", variant)
            .replace("__ON1__", "on" if variant == "v1" else "")
            .replace("__ON2__", "on" if variant == "v2" else "")
            .replace("__ONA__", "on" if variant == "auto" else ""))


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
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, page("auto"), "text/html; charset=utf-8")
        elif path in ("/v1", "/v2"):
            self._send(200, page(path[1:]), "text/html; charset=utf-8")
        elif self.path == "/api/status":
            self._send(200, json.dumps(current(), ensure_ascii=False), "application/json; charset=utf-8")
        elif self.path == "/api/hub":
            self._send(200, json.dumps(current_hub(), ensure_ascii=False), "application/json; charset=utf-8")
        elif self.path == "/api/text":
            self._send(200, as_text(current()), "text/plain; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def log_message(self, *args):
        pass


def main():
    global BASTION, BACKUP_RUNS
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--bastion", default=BASTION)
    ap.add_argument("--backup-dir", default=BACKUP_RUNS)
    args = ap.parse_args()
    BASTION = args.bastion
    BACKUP_RUNS = args.backup_dir
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"linuxmintai status: http://127.0.0.1:{args.port}/", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
