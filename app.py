from fastapi import FastAPI, HTTPException, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
import httpx
import asyncio
import json
import logging
import subprocess
import time

app = FastAPI(title="AI Model Manager", description="API for managing local AI models in VRAM")
ollama_url = "http://localhost:11434"
# Second Ollama server pinned to the R9700 (GPU 1) via HIP_VISIBLE_DEVICES=1
ollama_sub_url = "http://localhost:11435"


def _ollama_base(gpu=0):
    """Return the Ollama base URL for the selected GPU card (0=W7800, 1=R9700)."""
    return ollama_sub_url if gpu == 1 else ollama_url
# Loading large models into VRAM can take minutes, so use a generous timeout
client_timeout = httpx.Timeout(900.0)

logger = logging.getLogger("uvicorn.error")

# --- amd-smi terminal backend ---
AMD_SMI_CMD = ["/usr/bin/sudo", "/usr/bin/amd-smi"]
AMD_SMI_REFRESH_SECONDS = 1.0
AMD_SMI_ACTIVE_WINDOW_SECONDS = 6.0

_amd_smi_output = ""
_amd_smi_last_request = 0.0
# Structured GPU data from amd-smi (metric/process/static --json)
_amd_smi_gpu = {"gpus": [], "processes": [], "error": None, "updated_at": 0.0}
_amd_smi_static = None


def _run_amd_smi():
    try:
        proc = subprocess.run(
            AMD_SMI_CMD,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        return proc.stdout if proc.stdout else proc.stderr
    except Exception as e:
        return f"Error running amd-smi: {e}"


def _run_amd_smi_json(args):
    """Run `amd-smi <args>` and parse its --json stdout, or None on failure."""
    try:
        proc = subprocess.run(
            AMD_SMI_CMD + args,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        if proc.returncode != 0:
            return None
        return json.loads(proc.stdout)
    except Exception:
        return None


def _jval(node, *keys, default=None):
    """Dive into nested dicts and pull the 'value' field of a metric entry."""
    for k in keys:
        if not isinstance(node, dict):
            return default
        node = node.get(k)
    if isinstance(node, dict) and "value" in node:
        return node["value"]
    if isinstance(node, str):
        return node if node != "N/A" else default
    return default if node is None else node


def _amd_smi_static_info():
    """Return cached static amd-smi info (GPU names etc; fetched once)."""
    global _amd_smi_static
    if _amd_smi_static is None:
        _amd_smi_static = _run_amd_smi_json(["static", "--json"])
    return _amd_smi_static


def _collect_gpu_data():
    """Refresh _amd_smi_gpu from amd-smi metric/process/static --json output."""
    global _amd_smi_gpu
    metric = _run_amd_smi_json(["metric", "-m", "-u", "-p", "-c", "-t", "-f", "--json"])
    proc = _run_amd_smi_json(["process", "--json"])

    if metric is None:
        _amd_smi_gpu = {
            "gpus": [],
            "processes": [],
            "error": "amd-smi metric failed",
            "updated_at": time.time(),
        }
        return

    try:
        statics = {}
        static = _amd_smi_static_info()
        if static and "gpu_data" in static:
            for g in static["gpu_data"]:
                statics[g.get("gpu")] = g

        gpus = []
        for g in metric.get("gpu_data", []):
            gid = g.get("gpu")
            st = statics.get(gid, {})
            mem = g.get("mem_usage") or {}
            total = _jval(mem, "total_vram", default=0)
            used = _jval(mem, "used_vram", default=0)
            usage = g.get("usage") or {}
            power = g.get("power") or {}
            temp = g.get("temperature") or {}
            fan = g.get("fan") or {}
            clock = g.get("clock") or {}
            gfx0 = clock.get("gfx_0") or {}
            mem0 = clock.get("mem_0") or {}
            gpus.append({
                "gpu": gid,
                "name": _jval(st, "asic", "market_name", default=f"GPU {gid}"),
                "gfx_util": _jval(usage, "gfx_activity", default=0),
                "vram_used": used,
                "vram_total": total,
                "temp_edge": _jval(temp, "edge", default=None),
                "temp_hotspot": _jval(temp, "hotspot", default=None),
                "temp_mem": _jval(temp, "mem", default=None),
                "power": _jval(power, "socket_power", default=None),
                "power_management": _jval(power, "power_management", default=None),
                "throttle_status": _jval(power, "throttle_status", default=None),
                "gfx_voltage": _jval(power, "gfx_voltage", default=None),
                "soc_voltage": _jval(power, "soc_voltage", default=None),
                "gfx_clk": _jval(gfx0, "clk", default=None),
                "mem_clk": _jval(mem0, "clk", default=None),
                "fan_rpm": _jval(fan, "rpm", default=None),
                "fan_pct": _jval(fan, "usage", default=None),
            })

        processes = []
        if isinstance(proc, list):
            proc_gpus = proc
        elif isinstance(proc, dict):
            proc_gpus = proc.get("gpu_data", [])
        else:
            proc_gpus = []
        for g in proc_gpus:
                gid = g.get("gpu")
                plist = g.get("process_list") or []
                if not isinstance(plist, list):
                    plist = []
                for p in plist:
                    pi = p.get("process_info") if isinstance(p, dict) else None
                    # process_info may be the string "No running processes detected"
                    if not isinstance(pi, dict):
                        continue
                    name = _jval(pi, "name", default="?")
                    memu = pi.get("memory_usage") or {}
                    processes.append({
                        "gpu": gid,
                        "pid": _jval(pi, "pid", default=None),
                        "name": name.rsplit("/", 1)[-1] or name,
                        "gtt_mem": _jval(memu, "gtt_mem", default=None),
                        "vram_mem": _jval(memu, "vram_mem", default=None),
                        "mem_usage": _jval(pi, "mem_usage", default=None),
                        "cu_occupancy": _jval(pi, "cu_occupancy", default=None),
                    })

        _amd_smi_gpu = {
            "gpus": gpus,
            "processes": processes,
            "error": None,
            "updated_at": time.time(),
        }
    except Exception:
        logger.exception("Failed to parse amd-smi data")
        _amd_smi_gpu = {
            "gpus": [],
            "processes": [],
            "error": "Failed to parse amd-smi",
            "updated_at": time.time(),
        }


async def _amd_smi_worker():
    global _amd_smi_output
    while True:
        try:
            if time.time() - _amd_smi_last_request < AMD_SMI_ACTIVE_WINDOW_SECONDS:
                _amd_smi_output = await run_in_threadpool(_run_amd_smi)
                await run_in_threadpool(_collect_gpu_data)
        except Exception:
            logger.exception("amd-smi worker error")
        await asyncio.sleep(AMD_SMI_REFRESH_SECONDS)


# --- ollama ps helpers ---
OLLAMA_CMD = "/usr/local/bin/ollama"

# --- fan control backend ---
FAN_SH = "/home/tl0/tools/fan.sh"

# --- remote server fan control backend (fanapi) ---
FANAPI_URL = "http://192.168.1.84:22395"


def _get_fan_status():
    """Return current fan RPM and PWM from 'sensors' (or None if unavailable)."""
    try:
        proc = subprocess.run(
            ["/usr/bin/sensors"],
            capture_output=True, text=True, timeout=5.0,
        )
        rpm = None
        pwm = None
        for line in proc.stdout.split("\n"):
            low = line.lower()
            if "fan1" in low:
                parts = line.replace("(", " ").split()
                for i, p in enumerate(parts):
                    if p.replace(".", "").isdigit() and "RPM" in line:
                        rpm = int(round(float(p)))
                        break
            elif "pwm1" in low:
                for p in line.replace("(", " ").split():
                    if p.endswith("%"):
                        pwm = int(p.rstrip("%"))
                        break
        return {"rpm": rpm, "pwm": pwm}
    except Exception:
        return {"rpm": None, "pwm": None}


def _set_fan(percent):
    """Set GPU fan speed (0-100). Returns (ok, message)."""
    try:
        proc = subprocess.run(
            ["/usr/bin/sudo", FAN_SH, str(percent)],
            capture_output=True, text=True, timeout=30.0,
        )
        ok = "ok" in (proc.stdout + proc.stderr)
        msg = (proc.stdout or proc.stderr).strip() or f"fan.sh exited {proc.returncode}"
        return ok, msg
    except Exception as e:
        return False, f"Error running fan.sh: {e}"


def _reset_fan():
    """Reset GPU fan curve to defaults. Returns (ok, message)."""
    try:
        proc = subprocess.run(
            ["/usr/bin/sudo", FAN_SH, "--reset"],
            capture_output=True, text=True, timeout=30.0,
        )
        ok = "ok" in (proc.stdout + proc.stderr)
        msg = (proc.stdout or proc.stderr).strip() or f"fan.sh exited {proc.returncode}"
        return ok, msg
    except Exception as e:
        return False, f"Error running fan.sh: {e}"


def _vram_pids():
    """Return PIDs of processes using VRAM (from amd-smi process)."""
    try:
        proc = subprocess.run(
            ["/usr/bin/sudo", "/usr/bin/amd-smi", "process"],
            capture_output=True, text=True, timeout=10.0,
        )
        pids = []
        for line in proc.stdout.split("\n"):
            stripped = line.strip()
            if stripped.startswith("PID:"):
                val = stripped.split(":", 1)[1].strip()
                if val.isdigit():
                    pids.append(int(val))
        return pids
    except Exception:
        return []


def _kill_vram_processes():
    """Kill every process using VRAM (excluding ollama runtime), return killed PIDs."""
    killed = []
    for pid in _vram_pids():
        try:
            # Identify the process; never kill the ollama server
            out = subprocess.run(
                ["ps", "-p", str(pid), "-o", "comm="],
                capture_output=True, text=True, timeout=5.0,
            ).stdout.strip()
            if "ollama" in out:
                continue
            proc = subprocess.run(
                ["/usr/bin/sudo", "/usr/bin/kill", "-9", str(pid)],
                capture_output=True, text=True, timeout=10.0,
            )
            if proc.returncode == 0:
                killed.append(pid)
        except Exception:
            logger.exception("Failed to kill VRAM process %s", pid)
    return killed


def _parse_ps_table(output):
    """Parse the 'ollama ps' fixed-width table using header column offsets."""
    lines = output.strip().split("\n")
    if not lines:
        return []
    header = lines[0]
    offsets = {}
    for name in ("NAME", "ID", "SIZE", "PROCESSOR", "CONTEXT", "UNTIL"):
        offsets[name] = header.find(name)
    rows = []
    for line in lines[1:]:
        if not line.strip():
            continue

        def field(name, next_name=None):
            start = offsets.get(name)
            if start is None or start < 0 or start >= len(line):
                return ""
            if next_name:
                end = offsets.get(next_name)
                if end is not None and end > start:
                    return line[start:end].strip()
            return line[start:].strip()

        rows.append({
            "name": field("NAME", "ID"),
            "size": field("SIZE", "PROCESSOR"),
            "processor": field("PROCESSOR", "CONTEXT"),
            "context": field("CONTEXT", "UNTIL"),
        })
    return rows


def _get_current_model_from_ollama():
    """Return the name of the first currently running model (or '')."""
    try:
        proc = subprocess.run(
            [OLLAMA_CMD, "ps"],
            capture_output=True, text=True, timeout=5.0,
        )
        rows = _parse_ps_table(proc.stdout)
        return rows[0]["name"] if rows else ""
    except Exception:
        return ""


def _get_running_models_http(url):
    """Return running models from a specific ollama server via its /api/ps endpoint."""
    try:
        import httpx
        r = httpx.get(f"{url}/api/ps", timeout=5.0)
        if r.status_code != 200:
            return []
        out = []
        for m in r.json().get("models", []):
            size = m.get("size") or 0
            card = "R9700" if url == ollama_sub_url else "W7800"
            out.append({
                "name": m.get("name", ""),
                "size": fmt_gb(size),
                "processor": "100% GPU",
                "context": str(m.get("context_length") or ""),
                "card": card,
                "gpu_base": url,
            })
        return out
    except Exception:
        return []


def fmt_gb(size):
    """Format a byte count as a human-readable GB string."""
    try:
        return f"{size / (1024**3):.1f} GB"
    except Exception:
        return ""


def _get_running_models(url=ollama_url):
    """Return running models across both GPU cards (W7800 main + R9700 sub)."""
    rows = []
    for base in (ollama_url, ollama_sub_url):
        rows.extend(_get_running_models_http(base))
    return rows


def _get_running_models_on(url):
    """Return running models filtered to a single server URL."""
    return _get_running_models_http(url)


@app.on_event("startup")
async def startup():
    asyncio.create_task(_amd_smi_worker())

class ModelRequest(BaseModel):
    model: str
    gpu: int | None = None

class FanRequest(BaseModel):
    percent: int | None = None
    reset: bool = False

class ServerFanRequest(BaseModel):
    index: int | None = None
    percent: int | None = None
    mode: str | None = None

@app.get("/")
async def root():
    return {
        "message": "AI Model Manager API",
        "endpoints": [
            "/aimanager - Web UI",
            "/aimanager/models - List all models (ollama list)",
            "/aimanager/loaded - List currently loaded models (ollama ps)",
            "/aimanager/load - Load a model into VRAM (stays until unloaded)",
            "/aimanager/unload - Unload a model from VRAM",
        ],
    }

@app.get("/aimanager")
async def serve_ui():
    with open("index.html", "r") as f:
        html_content = f.read()
    return Response(content=html_content, media_type="text/html")

@app.get("/aimanager/terminal")
async def get_terminal():
    global _amd_smi_last_request
    _amd_smi_last_request = time.time()
    return Response(content=_amd_smi_output, media_type="text/plain")

@app.get("/aimanager/gpu")
async def get_gpu():
    """Return structured GPU metrics/processes parsed from amd-smi."""
    global _amd_smi_last_request
    _amd_smi_last_request = time.time()
    return _amd_smi_gpu

@app.get("/aimanager/current")
async def get_current():
    return {"model": _get_current_model_from_ollama()}

@app.get("/aimanager/fan")
async def get_fan():
    """Return current GPU fan status."""
    return _get_fan_status()

@app.post("/aimanager/fan")
async def set_fan(request: FanRequest):
    """Set GPU fan speed (percent 0-100) or reset the fan curve."""
    if request.reset:
        ok, msg = _reset_fan()
    elif request.percent is not None:
        if not 0 <= request.percent <= 100:
            raise HTTPException(status_code=400, detail="percent must be 0-100")
        ok, msg = _set_fan(request.percent)
    else:
        raise HTTPException(status_code=400, detail="Provide 'percent' or 'reset'")
    if not ok:
        raise HTTPException(status_code=500, detail=msg)
    return {"message": msg, "status": _get_fan_status()}

@app.get("/aimanager/serverfans")
async def get_server_fans():
    """List remote server fans (and temps) from the fanapi service."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            fans_r = await client.get(f"{FANAPI_URL}/api/fans")
            temps_r = await client.get(f"{FANAPI_URL}/api/temps")
        fans = fans_r.json().get("fans", []) if fans_r.status_code == 200 else []
        temps = temps_r.json().get("temps", []) if temps_r.status_code == 200 else []
        return {"fans": fans, "temps": temps}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error fetching server fans: {e}")

@app.post("/aimanager/serverfans")
async def set_server_fan(request: ServerFanRequest):
    """Control remote server fans. Without 'index', applies to ALL fans."""
    body = {}
    if request.percent is not None:
        if not 0 <= request.percent <= 100:
            raise HTTPException(status_code=400, detail="percent must be 0-100")
        body["percent"] = request.percent
    if request.mode is not None:
        if request.mode not in ("manual", "auto", "off"):
            raise HTTPException(status_code=400, detail="mode must be 'manual', 'auto' or 'off'")
        body["mode"] = request.mode
    if not body:
        raise HTTPException(status_code=400, detail="Provide 'percent' or 'mode'")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if request.index is None:
                fans_r = await client.get(f"{FANAPI_URL}/api/fans")
                fans = fans_r.json().get("fans", []) if fans_r.status_code == 200 else []
                if not fans:
                    raise HTTPException(status_code=502, detail="No fans found on fanapi")
                results = []
                for f in fans:
                    r = await client.put(f"{FANAPI_URL}/api/fans/{f['index']}", json=body)
                    results.append({"index": f["index"], "status": r.status_code})
                return {"results": results}
            r = await client.put(f"{FANAPI_URL}/api/fans/{request.index}", json=body)
            if r.status_code != 200:
                raise HTTPException(status_code=r.status_code, detail=f"fanapi: {r.text[:200]}")
            return r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error setting server fan: {e}")

@app.get("/aimanager/loaded")
async def get_loaded():
    """Return models currently loaded in VRAM (from 'ollama ps')."""
    return {"models": _get_running_models()}

@app.get("/aimanager/models")
async def list_models():
    try:
        async with httpx.AsyncClient(timeout=client_timeout) as client:
            response = await client.get(f"{ollama_url}/api/tags")
            if response.status_code != 200:
                raise HTTPException(status_code=response.status_code, detail="Failed to fetch models")
            models = [
                {
                    "name": model["name"],
                    "modified_at": model["modified_at"],
                    "size": model["size"],
                    "digest": model["digest"],
                }
                for model in response.json().get("models", [])
            ]
        return {"models": models}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to list models")
        raise HTTPException(status_code=500, detail=f"Error fetching models: {str(e)}")

@app.post("/aimanager/load")
async def load_model(request: ModelRequest):
    base = _ollama_base(request.gpu or 0)
    try:
        # keep_alive=-1 loads the model once and keeps it in VRAM
        async with httpx.AsyncClient(timeout=client_timeout) as client:
            response = await client.post(
                f"{base}/api/generate",
                json={
                    "model": request.model,
                    "prompt": "",
                    "stream": False,
                    "keep_alive": -1,
                },
            )
        if response.status_code == 200:
            info = next(
                (m for m in _get_running_models_on(base) if m["name"] == request.model),
                None,
            )
            card = "R9700" if (request.gpu == 1) else "W7800"
            return {
                "message": f"Successfully loaded: {request.model} onto {card}",
                "model": info,
            }
        else:
            raise HTTPException(status_code=response.status_code, detail="Failed to load model")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to load model")
        raise HTTPException(status_code=500, detail=f"Error loading model: {str(e)}")

@app.post("/aimanager/unload")
async def unload_model(request: ModelRequest):
    base = _ollama_base(request.gpu or 0)
    try:
        # keep_alive=0 unloads the model from VRAM
        async with httpx.AsyncClient(timeout=client_timeout) as client:
            response = await client.post(
                f"{base}/api/generate",
                json={
                    "model": request.model,
                    "prompt": "",
                    "stream": False,
                    "keep_alive": 0,
                },
            )
        if response.status_code == 200:
            card = "R9700" if (request.gpu == 1) else "W7800"
            return {"message": f"Successfully unloaded: {request.model} from {card}"}
        else:
            raise HTTPException(status_code=response.status_code, detail="Failed to unload model")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to unload model")
        raise HTTPException(status_code=500, detail=f"Error unloading model: {str(e)}")

@app.post("/aimanager/clear")
async def clear_vram():
    """Unload all ollama models AND kill every non-ollama process using VRAM."""
    unloaded = []
    async with httpx.AsyncClient(timeout=client_timeout) as client:
        for m in _get_running_models():
            base = m.get("gpu_base") or ollama_url
            try:
                response = await client.post(
                    f"{base}/api/generate",
                    json={
                        "model": m["name"],
                        "prompt": "",
                        "stream": False,
                        "keep_alive": 0,
                    },
                )
                if response.status_code == 200:
                    unloaded.append(m["name"])
            except Exception:
                logger.exception("Failed to unload %s during clear", m["name"])
    killed = _kill_vram_processes()
    msg = "Cleared VRAM"
    if unloaded:
        msg += f" (unloaded {len(unloaded)} model(s))"
    if killed:
        msg += f"; killed {len(killed)} process(es)"
    return {"message": msg, "unloaded": unloaded, "killed": killed}

if __name__ == "__main__":
    import uvicorn
    print("Starting AI Model Manager API on port 22345")
    print("Web UI is available at http://localhost:22345/aimanager")
    uvicorn.run(app, host="0.0.0.0", port=22345)
