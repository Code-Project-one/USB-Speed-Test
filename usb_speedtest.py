#!/usr/bin/env python3
"""
USB Speed Test  —  Cross-platform, no pip install required.
Usage:  python3 usb_speedtest.py
        python usb_speedtest.py       (Windows)
"""

import os, json, time, threading, platform, subprocess
import webbrowser, queue
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

# macOS: F_NOCACHE (fcntl flag 48) tells the kernel not to cache file I/O,
# so read speeds reflect actual drive throughput, not RAM.
_fcntl = None
_F_NOCACHE = None
if platform.system() == "Darwin":
    try:
        import fcntl as _fcntl
        _F_NOCACHE = 48
    except ImportError:
        pass

PORT  = 7789
HOST  = "127.0.0.1"
SYS   = platform.system()          # 'Darwin' | 'Windows' | 'Linux'

# ── Global state ──────────────────────────────────────────────────────────────
_state = dict(
    running=False, phase=None, progress=0,
    current_speed=0.0,
    write_speeds=[], read_speeds=[],
    write_avg=0.0, write_peak=0.0,
    read_avg=0.0,  read_peak=0.0,
    error=None, complete=False,
)
_state_lock = threading.Lock()
_stop        = threading.Event()
_sse_queues: "list[queue.Queue]" = []
_sse_lock    = threading.Lock()

# ── SSE broadcast ─────────────────────────────────────────────────────────────
def _push(event: str, data: dict):
    msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    with _sse_lock:
        dead = []
        for q in _sse_queues:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _sse_queues.remove(q)

# ── Drive detection ───────────────────────────────────────────────────────────
def _get_drives():
    if SYS == "Darwin":
        return _drives_mac()
    if SYS == "Windows":
        return _drives_windows()
    return _drives_linux()

def _drives_mac():
    drives = []
    try:
        r = subprocess.run(
            ["diskutil", "list", "external", "physical"],
            capture_output=True, text=True, timeout=15,
        )
        disk_ids = []
        for line in r.stdout.splitlines():
            line = line.strip()
            if line.startswith("/dev/disk"):
                disk_ids.append(line.split()[0])

        for disk in disk_ids:
            r2 = subprocess.run(
                ["diskutil", "list", disk],
                capture_output=True, text=True, timeout=10,
            )
            # diskutil list output has IDENTIFIER as the last token on numbered lines:
            #   1:   Windows_NTFS MJ-Test   1.0 TB   disk22s1
            # Identifiers appear WITHOUT /dev/ prefix in this output.
            parts = []
            for line in r2.stdout.splitlines():
                cols = line.strip().split()
                if cols and cols[0].rstrip(":").isdigit():
                    ident = cols[-1]           # e.g. "disk22s1"
                    if "s" in ident:           # partitions have sN suffix
                        parts.append("/dev/" + ident)
            if not parts:
                parts = [disk]                 # fall back to whole disk

            for part in parts:
                r3 = subprocess.run(
                    ["diskutil", "info", part],
                    capture_output=True, text=True, timeout=10,
                )
                mount = name = size = None
                for line in r3.stdout.splitlines():
                    k, _, v = line.partition(":")
                    v = v.strip()
                    if "Mount Point" in k:
                        mount = v
                    elif "Volume Name" in k and v and v not in ("None", ""):
                        name = v
                    elif "Volume Total Space" in k or "Disk Size" in k:
                        if not size:
                            size = v.split("(")[0].strip()
                if mount and mount != "/" and os.path.ismount(mount):
                    drives.append(dict(
                        path=mount,
                        name=name or os.path.basename(mount),
                        device=part,
                        size=size or "?",
                        writable=os.access(mount, os.W_OK),
                    ))
    except Exception as e:
        print(f"[drive detect] {e}")
    return drives

def _drives_windows():
    drives = []
    # Try PowerShell first (works on all modern Windows)
    try:
        ps = (
            "Get-WmiObject Win32_LogicalDisk | "
            "Where-Object {$_.DriveType -eq 2} | "
            "Select-Object DeviceID,VolumeName,Size,FreeSpace | "
            "ConvertTo-Json -Compress"
        )
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=20,
        )
        data = json.loads(r.stdout.strip() or "[]")
        if isinstance(data, dict):
            data = [data]
        for d in data:
            cap  = d.get("DeviceID", "")
            path = cap + "\\" if cap else ""
            sz   = d.get("Size") or 0
            drives.append(dict(
                path=path,
                name=d.get("VolumeName") or cap,
                device=cap,
                size=f"{int(sz)/1e9:.1f} GB" if sz else "?",
                writable=bool(path and os.access(path, os.W_OK)),
            ))
    except Exception:
        pass

    if not drives:
        # wmic fallback
        try:
            r = subprocess.run(
                ["wmic", "logicaldisk", "where", "DriveType=2",
                 "get", "Caption,VolumeName,Size", "/format:csv"],
                capture_output=True, text=True, timeout=20,
            )
            for line in r.stdout.splitlines():
                cols = [c.strip() for c in line.split(",")]
                if len(cols) < 4 or not cols[1]:
                    continue
                _, cap, sz, vol = cols[0], cols[1], cols[2], cols[3]
                path = cap + "\\"
                drives.append(dict(
                    path=path, name=vol or cap, device=cap,
                    size=f"{int(sz)/1e9:.1f} GB" if sz.isdigit() else "?",
                    writable=os.access(path, os.W_OK),
                ))
        except Exception:
            pass
    return drives

def _drives_linux():
    drives = []
    try:
        r = subprocess.run(
            ["lsblk", "-J", "-o", "NAME,MOUNTPOINT,SIZE,HOTPLUG,VENDOR"],
            capture_output=True, text=True, timeout=15,
        )
        data = json.loads(r.stdout)
        def walk(devices):
            for d in devices:
                if d.get("hotplug") and d.get("mountpoint"):
                    mp = d["mountpoint"]
                    if os.path.ismount(mp):
                        drives.append(dict(
                            path=mp, name=(d.get("vendor") or d["name"]).strip(),
                            device="/dev/" + d["name"],
                            size=d.get("size", "?"),
                            writable=os.access(mp, os.W_OK),
                        ))
                if d.get("children"):
                    walk(d["children"])
        walk(data.get("blockdevices", []))
    except Exception as e:
        print(f"[drive detect] {e}")
    return drives

# ── Speed test (runs in worker thread) ───────────────────────────────────────
def _run_test(drive_path: str, size_mb: int):
    CHUNK = 1024 * 1024        # 1 MB
    chunks = max(size_mb, 1)
    test_file = os.path.join(drive_path, ".usb_speedtest_tmp")

    with _state_lock:
        _state.update(running=True, complete=False, error=None, phase="write",
                      progress=0, current_speed=0.0,
                      write_speeds=[], read_speeds=[])

    _push("status", {"phase": "write", "progress": 0, "speed": 0})

    try:
        # ── Check free space ────────────────────────────────────────────────
        try:
            free = os.statvfs(drive_path).f_bavail * os.statvfs(drive_path).f_frsize \
                   if SYS != "Windows" else \
                   int(subprocess.check_output(
                       ["powershell", "-NoProfile", "-Command",
                        f"(Get-PSDrive '{drive_path[0]}').Free"],
                       text=True).strip())
            needed = chunks * CHUNK
            if free < needed + 10 * 1024 * 1024:
                raise RuntimeError(
                    f"Not enough free space. Need {needed//1048576} MB, "
                    f"have {free//1048576} MB."
                )
        except RuntimeError:
            raise
        except Exception:
            pass   # skip space check if it fails

        # ── WRITE ───────────────────────────────────────────────────────────
        payload = os.urandom(CHUNK)    # random data defeats compression
        w_speeds = []

        with open(test_file, "wb") as f:
            if _fcntl and _F_NOCACHE:
                try:
                    _fcntl.fcntl(f.fileno(), _F_NOCACHE, 1)
                except Exception:
                    pass
            for i in range(chunks):
                if _stop.is_set():
                    return
                t0 = time.perf_counter()
                f.write(payload)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
                dt = time.perf_counter() - t0
                spd = (CHUNK / dt) / 1_048_576
                w_speeds.append(round(spd, 2))
                prog = int((i + 1) / chunks * 100)
                with _state_lock:
                    _state["current_speed"] = spd
                    _state["progress"]       = prog
                    _state["write_speeds"]   = w_speeds.copy()
                _push("progress", {
                    "phase": "write", "progress": prog,
                    "speed": round(spd, 1), "speeds": w_speeds.copy(),
                })

        if _stop.is_set():
            return

        w_avg  = sum(w_speeds) / len(w_speeds)
        w_peak = max(w_speeds)
        with _state_lock:
            _state.update(write_avg=w_avg, write_peak=w_peak,
                          phase="read", progress=0, current_speed=0.0)
        _push("status", {"phase": "read", "progress": 0, "speed": 0})

        # ── READ ────────────────────────────────────────────────────────────
        r_speeds = []

        with open(test_file, "rb") as f:
            if _fcntl and _F_NOCACHE:
                try:
                    _fcntl.fcntl(f.fileno(), _F_NOCACHE, 1)
                except Exception:
                    pass
            for i in range(chunks):
                if _stop.is_set():
                    return
                t0   = time.perf_counter()
                data = f.read(CHUNK)
                dt   = time.perf_counter() - t0
                if not data:
                    break
                spd = (len(data) / dt) / 1_048_576
                r_speeds.append(round(spd, 2))
                prog = int((i + 1) / chunks * 100)
                with _state_lock:
                    _state["current_speed"] = spd
                    _state["progress"]       = prog
                    _state["read_speeds"]    = r_speeds.copy()
                _push("progress", {
                    "phase": "read", "progress": prog,
                    "speed": round(spd, 1), "speeds": r_speeds.copy(),
                })

        r_avg  = sum(r_speeds) / len(r_speeds) if r_speeds else 0
        r_peak = max(r_speeds)                  if r_speeds else 0

        with _state_lock:
            _state.update(read_avg=r_avg, read_peak=r_peak,
                          complete=True, running=False)
        _push("complete", {
            "write_avg":  round(w_avg,  1), "write_peak": round(w_peak, 1),
            "read_avg":   round(r_avg,  1), "read_peak":  round(r_peak, 1),
            "write_speeds": w_speeds, "read_speeds": r_speeds,
        })

    except Exception as exc:
        with _state_lock:
            _state.update(error=str(exc), running=False)
        _push("error", {"message": str(exc)})

    finally:
        try:
            if os.path.exists(test_file):
                os.remove(test_file)
        except Exception:
            pass
        with _state_lock:
            _state["running"] = False

# ── Drive identification (used by drag-and-drop) ──────────────────────────────
def _identify_drive(dtype: str, name: str, size: int):
    """Match a dropped file/folder name against detected USB drives."""
    drives = _get_drives()

    if dtype == "dir" and name:
        # 1. Exact volume-name match (e.g. drag the volume icon from Finder)
        for d in drives:
            vol = os.path.basename(d["path"].rstrip("/\\"))
            if vol.lower() == name.lower():
                return d
        # 2. Drive label match
        for d in drives:
            if d["name"].lower() == name.lower():
                return d

    if dtype == "file" and name:
        # 3. File exists on one of the drives (with optional size check)
        for d in drives:
            candidate = os.path.join(d["path"], name)
            try:
                if os.path.exists(candidate):
                    if not size or os.path.getsize(candidate) == size:
                        return d
            except Exception:
                pass

    # 4. Fuzzy fallback: name substring anywhere in path or label
    if name:
        nl = name.lower()
        for d in drives:
            if nl in d["path"].lower() or nl in d["name"].lower():
                return d

    return None

# ── Embedded HTML ─────────────────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>USB Speed Test</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#0d1117;--surface:#161b22;--border:#21262d;--border2:#30363d;
  --text:#e6edf3;--muted:#8b949e;--blue:#58a6ff;--green:#3fb950;--red:#f85149;
}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;min-height:100vh;display:flex;flex-direction:column;align-items:center}

/* ── Header ── */
.hdr{width:100%;padding:18px 32px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--border)}
.logo{display:flex;align-items:center;gap:10px;font-size:18px;font-weight:700}
.logo svg{width:28px;height:28px}
.badge{background:var(--surface);border:1px solid var(--border2);border-radius:20px;padding:4px 14px;font-size:12px;color:var(--muted)}

/* ── Controls ── */
.ctrl{width:100%;max-width:860px;display:flex;align-items:center;gap:10px;padding:20px 32px}
select,button{height:40px;border-radius:8px;border:1px solid var(--border2);background:var(--surface);color:var(--text);padding:0 14px;font-size:14px;cursor:pointer;outline:none}
select{flex:1;min-width:0}
.sel-sz{width:110px;flex:none}
.btn-go{background:#238636;border-color:#2ea043;font-weight:600;padding:0 28px;transition:background .15s}
.btn-go:hover{background:#2ea043}
.btn-go:disabled{background:var(--border);border-color:var(--border2);color:var(--muted);cursor:not-allowed}
.btn-stop{background:#b91c1c;border-color:var(--red);font-weight:600;padding:0 28px}
.btn-stop:hover{background:var(--red)}
.btn-ref{width:40px;padding:0;font-size:18px;background:transparent;border-color:var(--border2)}
.btn-ref:hover{background:var(--border)}

/* ── Main ── */
.main{width:100%;max-width:860px;padding:0 32px;display:flex;flex-direction:column;align-items:center}

/* phase */
.phase{font-size:12px;font-weight:700;letter-spacing:3px;text-transform:uppercase;color:var(--muted);margin:18px 0 8px;min-height:18px;transition:color .3s}
.phase.w{color:var(--blue)}.phase.r{color:var(--green)}.phase.ok{color:var(--green)}

/* gauge */
.gauge-wrap{position:relative;width:260px;height:260px}
.gauge-wrap svg{width:100%;height:100%;overflow:visible}
.g-track{fill:none;stroke:var(--border);stroke-width:18;stroke-linecap:round}
.g-fill{fill:none;stroke:var(--blue);stroke-width:18;stroke-linecap:round;transition:stroke-dasharray .18s linear,stroke .3s}
.g-glow{fill:none;stroke:var(--blue);stroke-width:26;stroke-linecap:round;opacity:.12;transition:stroke-dasharray .18s linear,stroke .3s}
.gauge-mid{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);text-align:center;pointer-events:none}
.spd-val{font-size:56px;font-weight:200;line-height:1;font-variant-numeric:tabular-nums;letter-spacing:-2px}
.spd-unit{font-size:13px;color:var(--muted);margin-top:2px}
.spd-label{font-size:11px;color:var(--muted);margin-top:6px;letter-spacing:1px;text-transform:uppercase;min-height:14px}

/* progress */
.prog-wrap{width:100%;max-width:380px;height:3px;background:var(--border);border-radius:3px;margin:14px auto 0;overflow:hidden}
.prog-fill{height:100%;width:0%;background:var(--blue);border-radius:3px;transition:width .25s ease,background .3s}

/* status msg */
.status{font-size:13px;color:var(--muted);text-align:center;min-height:22px;margin:10px 0}
.status.err{color:var(--red)}

/* charts */
.charts{display:grid;grid-template-columns:1fr 1fr;gap:16px;width:100%;margin:18px 0 0}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px}
.card-title{font-size:11px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);margin-bottom:12px}
.chart-box{position:relative;height:130px}

/* results */
.results{width:100%;background:var(--surface);border:1px solid var(--border);border-radius:12px;display:grid;grid-template-columns:repeat(4,1fr);gap:0;margin:16px 0 40px;overflow:hidden}
.res-item{text-align:center;padding:18px 10px;border-right:1px solid var(--border)}
.res-item:last-child{border-right:none}
.res-lbl{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:8px}
.res-val{font-size:32px;font-weight:200;font-variant-numeric:tabular-nums;line-height:1}
.res-unit{font-size:11px;color:var(--muted);margin-top:3px}
.c-blue{color:var(--blue)}.c-green{color:var(--green)}

/* rating chip */
.rating{display:inline-block;margin-top:8px;padding:2px 10px;border-radius:20px;font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase}
.rat-fast{background:#1a3a1a;color:var(--green)}
.rat-good{background:#1a2a3a;color:var(--blue)}
.rat-slow{background:#3a1a1a;color:var(--red)}

@media(max-width:640px){
  .charts{grid-template-columns:1fr}
  .results{grid-template-columns:repeat(2,1fr)}
  .res-item:nth-child(2){border-right:none}
  .res-item:nth-child(3){border-top:1px solid var(--border)}
}

/* ── Drag-and-drop overlay ── */
.drop-overlay{
  position:fixed;inset:0;z-index:999;
  background:rgba(13,17,23,.88);backdrop-filter:blur(4px);
  display:none;flex-direction:column;align-items:center;justify-content:center;gap:14px;
  pointer-events:none;
}
.drop-overlay.active{display:flex}
.drop-ring{
  position:fixed;inset:14px;border:2px dashed var(--blue);border-radius:18px;
  pointer-events:none;animation:ringpulse 1.1s ease-in-out infinite;
}
.drop-icon{font-size:60px;animation:bob .7s ease-in-out infinite alternate;line-height:1}
.drop-title{font-size:20px;font-weight:700;color:var(--blue);letter-spacing:.5px}
.drop-sub{font-size:13px;color:var(--muted)}
@keyframes bob{from{transform:translateY(-9px)}to{transform:translateY(9px)}}
@keyframes ringpulse{0%,100%{opacity:.35}50%{opacity:.85}}

/* flash highlight on the select when drive is auto-selected */
@keyframes flashsel{0%{box-shadow:0 0 0 3px rgba(88,166,255,.6)}100%{box-shadow:none}}
.flash{animation:flashsel .9s ease-out}
</style>
</head>
<body>

<div class="hdr">
  <div class="logo">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
      <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/>
    </svg>
    USB Speed Test
  </div>
  <span class="badge" id="osBadge">detecting...</span>
</div>

<div class="ctrl">
  <select id="driveSelect"><option value="">— Loading drives —</option></select>
  <select class="sel-sz" id="sizeSelect">
    <option value="64">64 MB</option>
    <option value="256" selected>256 MB</option>
    <option value="512">512 MB</option>
    <option value="1024">1 GB</option>
  </select>
  <button class="btn-ref" id="btnRef" title="Refresh drives">&#8635;</button>
  <button class="btn-go" id="btnStart">Start Test</button>
</div>

<div class="main">
  <div class="phase" id="phaseLabel">Select a USB drive above to begin</div>

  <div class="gauge-wrap">
    <svg id="gaugeSvg" viewBox="0 0 260 260">
      <path class="g-glow" id="gGlow"/>
      <path class="g-track" id="gTrack"/>
      <path class="g-fill"  id="gFill"/>
    </svg>
    <div class="gauge-mid">
      <div class="spd-val" id="spdVal">0</div>
      <div class="spd-unit">MB/s</div>
      <div class="spd-label" id="spdLabel"></div>
    </div>
  </div>

  <div class="prog-wrap"><div class="prog-fill" id="progFill"></div></div>

  <div class="status" id="statusMsg"></div>

  <div class="charts">
    <div class="card">
      <div class="card-title">&#9998; Write Speed (MB/s)</div>
      <div class="chart-box"><canvas id="cWrite"></canvas></div>
    </div>
    <div class="card">
      <div class="card-title">&#9654; Read Speed (MB/s)</div>
      <div class="chart-box"><canvas id="cRead"></canvas></div>
    </div>
  </div>

  <div class="results">
    <div class="res-item">
      <div class="res-lbl">Write Avg</div>
      <div class="res-val c-blue" id="rWA">—</div>
      <div class="res-unit">MB/s</div>
    </div>
    <div class="res-item">
      <div class="res-lbl">Write Peak</div>
      <div class="res-val c-blue" id="rWP">—</div>
      <div class="res-unit">MB/s</div>
    </div>
    <div class="res-item">
      <div class="res-lbl">Read Avg</div>
      <div class="res-val c-green" id="rRA">—</div>
      <div class="res-unit">MB/s</div>
    </div>
    <div class="res-item">
      <div class="res-lbl">Read Peak</div>
      <div class="res-val c-green" id="rRP">—</div>
      <div class="res-unit">MB/s</div>
    </div>
  </div>
</div>

<script>
// ── Gauge math ────────────────────────────────────────────────────────────────
const CX=130,CY=130,R=100,START_ANG=225,SWEEP=270;
const ARC_LEN = 2*Math.PI*R*(SWEEP/360);

function p2xy(ang){
  const r=(ang-90)*Math.PI/180;
  return{x:CX+R*Math.cos(r),y:CY+R*Math.sin(r)};
}
function arcD(startAng,sweepAng){
  const e=startAng+sweepAng,p1=p2xy(startAng),p2=p2xy(e),la=sweepAng>180?1:0;
  return `M${p1.x.toFixed(2)} ${p1.y.toFixed(2)} A${R} ${R} 0 ${la} 1 ${p2.x.toFixed(2)} ${p2.y.toFixed(2)}`;
}
const trackD=arcD(START_ANG,SWEEP);
document.getElementById('gTrack').setAttribute('d',trackD);
document.getElementById('gFill').setAttribute('d',trackD);
document.getElementById('gGlow').setAttribute('d',trackD);
document.getElementById('gFill').setAttribute('stroke-dasharray',`0 ${ARC_LEN}`);
document.getElementById('gGlow').setAttribute('stroke-dasharray',`0 ${ARC_LEN}`);

let dynMax=100;
function setGauge(spd,phase){
  dynMax=Math.max(dynMax,spd*1.25,100);
  const ratio=Math.min(spd/dynMax,1),filled=(ratio*ARC_LEN).toFixed(2);
  const rest=(ARC_LEN*(1-ratio)).toFixed(2);
  const da=`${filled} ${rest}`;
  const col=phase==='read'?'var(--green)':'var(--blue)';
  document.getElementById('gFill').setAttribute('stroke-dasharray',da);
  document.getElementById('gFill').style.stroke=col;
  document.getElementById('gGlow').setAttribute('stroke-dasharray',da);
  document.getElementById('gGlow').style.stroke=col;
  document.getElementById('spdVal').textContent=spd<10?spd.toFixed(1):Math.round(spd);
  document.getElementById('progFill').style.background=col;
}

// ── Charts ────────────────────────────────────────────────────────────────────
const chartOpts={
  responsive:true,maintainAspectRatio:false,animation:false,
  plugins:{legend:{display:false},tooltip:{enabled:false}},
  scales:{
    x:{display:false},
    y:{beginAtZero:true,grid:{color:'rgba(255,255,255,.05)'},
       ticks:{color:'#8b949e',font:{size:10},maxTicksLimit:5}}
  },
  elements:{point:{radius:0},line:{tension:.35,borderWidth:2}}
};
function mkChart(id,color){
  return new Chart(document.getElementById(id).getContext('2d'),{
    type:'line',
    data:{labels:[],datasets:[{data:[],borderColor:color,
      backgroundColor:color.replace(')',',0.08)').replace('var','rgba').replace('--blue','88,166,255').replace('--green','63,185,80'),
      fill:true}]},
    options:JSON.parse(JSON.stringify(chartOpts))
  });
}
// For backgroundColor we need actual values
const wChart=new Chart(document.getElementById('cWrite').getContext('2d'),{
  type:'line',
  data:{labels:[],datasets:[{data:[],borderColor:'#58a6ff',backgroundColor:'rgba(88,166,255,.08)',fill:true}]},
  options:JSON.parse(JSON.stringify(chartOpts))
});
const rChart=new Chart(document.getElementById('cRead').getContext('2d'),{
  type:'line',
  data:{labels:[],datasets:[{data:[],borderColor:'#3fb950',backgroundColor:'rgba(63,185,80,.08)',fill:true}]},
  options:JSON.parse(JSON.stringify(chartOpts))
});
function pushChart(chart,speeds){
  chart.data.labels=speeds.map((_,i)=>i+1);
  chart.data.datasets[0].data=speeds;
  chart.update('none');
}

// ── Drive list ────────────────────────────────────────────────────────────────
const driveEl=document.getElementById('driveSelect');
async function loadDrives(){
  driveEl.innerHTML='<option value="">Loading...</option>';
  try{
    const d=await(await fetch('/api/drives')).json();
    driveEl.innerHTML='';
    if(!d.length){driveEl.innerHTML='<option value="">No USB drives found — insert one and refresh</option>';return;}
    d.forEach(dr=>{
      const o=document.createElement('option');
      o.value=dr.path;
      o.textContent=`${dr.name}  (${dr.size})  —  ${dr.path}${dr.writable?'':' [read-only]'}`;
      o.disabled=!dr.writable;
      driveEl.appendChild(o);
    });
  }catch{driveEl.innerHTML='<option value="">Error detecting drives</option>';}
}

// ── Rating helper ─────────────────────────────────────────────────────────────
function rate(mbps){
  if(mbps>=100)return['FAST','rat-fast'];
  if(mbps>=30) return['GOOD','rat-good'];
  return['SLOW','rat-slow'];
}

// ── Phase label ───────────────────────────────────────────────────────────────
const phEl=document.getElementById('phaseLabel');
function setPhase(ph,txt){
  phEl.className='phase '+(ph||'');
  phEl.textContent=txt||'';
}

// ── Result fields ─────────────────────────────────────────────────────────────
function setRes(id,val){document.getElementById(id).textContent=val!=null?Number(val).toFixed(1):'—';}

// ── Test control ──────────────────────────────────────────────────────────────
const btnStart=document.getElementById('btnStart');
const btnRef  =document.getElementById('btnRef');
const progFill=document.getElementById('progFill');
const statusEl=document.getElementById('statusMsg');
const spdLabel=document.getElementById('spdLabel');
let running=false,es=null;

function setRunning(r){
  running=r;
  btnStart.textContent=r?'Stop Test':'Start Test';
  btnStart.className  =r?'btn-stop':'btn-go';
  driveEl.disabled=r;
  document.getElementById('sizeSelect').disabled=r;
  btnRef.disabled=r;
}

btnStart.addEventListener('click',async()=>{
  if(running){
    await fetch('/api/stop',{method:'POST'});
    setRunning(false);
    setPhase('','Stopped by user');
    if(es){es.close();es=null;}
    return;
  }
  const path=driveEl.value;
  if(!path){statusEl.textContent='Please select a USB drive first.';return;}
  const sizeMb=parseInt(document.getElementById('sizeSelect').value);

  // Reset
  pushChart(wChart,[]);pushChart(rChart,[]);
  ['rWA','rWP','rRA','rRP'].forEach(id=>setRes(id,null));
  dynMax=100;
  setGauge(0,'write');
  progFill.style.width='0%';
  statusEl.textContent='';statusEl.className='status';
  spdLabel.textContent='';
  setPhase('w','● WRITE TEST');

  if(es)es.close();
  es=new EventSource('/api/stream');

  es.addEventListener('progress',e=>{
    const d=JSON.parse(e.data);
    setPhase(d.phase==='read'?'r':'w',
             d.phase==='read'?'● READ TEST':'● WRITE TEST');
    setGauge(d.speed,d.phase);
    progFill.style.width=d.progress+'%';
    spdLabel.textContent=d.progress+'%';
    if(d.phase==='write')pushChart(wChart,d.speeds);
    else                  pushChart(rChart,d.speeds);
  });
  es.addEventListener('status',e=>{
    const d=JSON.parse(e.data);
    setPhase(d.phase==='read'?'r':'w',
             d.phase==='read'?'● READ TEST':'● WRITE TEST');
    setGauge(0,d.phase);
    spdLabel.textContent='';
  });
  es.addEventListener('complete',e=>{
    const d=JSON.parse(e.data);
    setGauge(0,'read');
    progFill.style.width='100%';
    spdLabel.textContent='';
    phEl.className='phase ok';phEl.textContent='✓ TEST COMPLETE';
    setRes('rWA',d.write_avg);setRes('rWP',d.write_peak);
    setRes('rRA',d.read_avg); setRes('rRP',d.read_peak);
    pushChart(wChart,d.write_speeds);
    pushChart(rChart,d.read_speeds);
    const [rTxt,rCls]=rate(Math.min(d.write_avg,d.read_avg));
    statusEl.innerHTML=`Test complete &nbsp;<span class="rating ${rCls}">${rTxt}</span>`;
    setRunning(false);es.close();es=null;
  });
  es.addEventListener('error',e=>{
    try{
      const d=JSON.parse(e.data);
      statusEl.textContent='Error: '+d.message;
      statusEl.className='status err';
    }catch{}
    setPhase('','Error — see message above');
    setRunning(false);if(es){es.close();es=null;}
  });

  const r=await fetch('/api/start',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({path,size_mb:sizeMb})
  });
  if(!r.ok){
    const t=await r.text();
    statusEl.textContent='Could not start: '+t;
    if(es){es.close();es=null;}
    return;
  }
  setRunning(true);
});

btnRef.addEventListener('click',loadDrives);

// ── Init ──────────────────────────────────────────────────────────────────────
const ua=navigator.userAgent;
document.getElementById('osBadge').textContent=
  ua.includes('Win')?'Windows':ua.includes('Mac')?'macOS':'Linux';

loadDrives();

// ── Drag & Drop to identify USB drive ────────────────────────────────────────
const overlay = document.getElementById('dropOverlay');
let dragCount = 0;

document.addEventListener('dragenter', e => {
  if (running) return;
  e.preventDefault();
  if (++dragCount === 1) overlay.classList.add('active');
});
document.addEventListener('dragleave', () => {
  if (--dragCount <= 0) { dragCount = 0; overlay.classList.remove('active'); }
});
document.addEventListener('dragover', e => {
  e.preventDefault();
  e.dataTransfer.dropEffect = 'link';
});
document.addEventListener('drop', async e => {
  e.preventDefault();
  dragCount = 0;
  overlay.classList.remove('active');
  if (running) return;

  const items = [...(e.dataTransfer.items || [])];
  let payload = null;

  for (const item of items) {
    if (item.kind !== 'file') continue;
    // Try to get the filesystem entry (works in Chromium, Firefox 50+)
    const entry = item.webkitGetAsEntry ? item.webkitGetAsEntry() : null;
    if (entry && entry.isDirectory) {
      payload = { type: 'dir', name: entry.name };
      break;
    }
    const file = item.getAsFile ? item.getAsFile() : null;
    if (file) {
      payload = { type: 'file', name: file.name, size: file.size };
      break;
    }
  }

  if (!payload) {
    statusEl.textContent = 'Nothing recognized — try dropping a file from inside the USB.';
    statusEl.className = 'status err';
    return;
  }

  statusEl.textContent = 'Detecting drive…';
  statusEl.className = 'status';

  try {
    const r = await fetch('/api/identify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const res = await r.json();

    if (res.path) {
      // Make sure the drive list is up to date, then select it
      await loadDrives();
      driveEl.value = res.path;
      driveEl.classList.remove('flash');
      void driveEl.offsetWidth;       // force reflow so animation re-triggers
      driveEl.classList.add('flash');
      statusEl.textContent = '✓ Selected: ' + (res.name || res.path);
      statusEl.className = 'status';
    } else {
      statusEl.textContent = 'Drive not found. Try dropping a file from inside the USB drive.';
      statusEl.className = 'status err';
    }
  } catch (err) {
    statusEl.textContent = 'Identify error: ' + err.message;
    statusEl.className = 'status err';
  }
});
</script>

<div class="drop-overlay" id="dropOverlay">
  <div class="drop-ring"></div>
  <div class="drop-icon">&#128190;</div>
  <div class="drop-title">Drop USB drive or any file from it</div>
  <div class="drop-sub">We’ll detect which drive it belongs to</div>
</div>

</body>
</html>
"""

# ── HTTP request handler ───────────────────────────────────────────────────────
class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass   # silence access log

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML.encode("utf-8"))
            return

        if path == "/api/drives":
            drives = _get_drives()
            body = json.dumps(drives).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self._cors()
            self.end_headers()
            q: queue.Queue = queue.Queue(maxsize=200)
            with _sse_lock:
                _sse_queues.append(q)
            try:
                while True:
                    try:
                        msg = q.get(timeout=25)
                        self.wfile.write(msg.encode())
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
            except Exception:
                pass
            finally:
                with _sse_lock:
                    if q in _sse_queues:
                        _sse_queues.remove(q)
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)
        data   = json.loads(body) if body else {}

        if path == "/api/start":
            with _state_lock:
                if _state["running"]:
                    self.send_response(409)
                    self.end_headers()
                    self.wfile.write(b"Already running")
                    return
            drive_path = data.get("path", "").strip()
            size_mb    = max(1, int(data.get("size_mb", 256)))
            if not drive_path or not os.path.isdir(drive_path):
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Invalid drive path")
                return
            _stop.clear()
            threading.Thread(
                target=_run_test, args=(drive_path, size_mb), daemon=True
            ).start()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            return

        if path == "/api/stop":
            _stop.set()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            return

        if path == "/api/identify":
            dtype = data.get("type", "")
            name  = data.get("name", "").strip()
            size  = int(data.get("size", 0))
            found = _identify_drive(dtype, name, size)
            body  = json.dumps(found if found else {"path": None}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.end_headers()

# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    server = HTTPServer((HOST, PORT), _Handler)
    url    = f"http://localhost:{PORT}"
    print(f"\n  USB Speed Test  —  {url}\n")
    print(f"  Platform : {SYS} ({platform.machine()})")
    print(f"  Press Ctrl+C to quit.\n")

    def _open():
        time.sleep(0.9)
        webbrowser.open(url)

    threading.Thread(target=_open, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")

if __name__ == "__main__":
    main()
