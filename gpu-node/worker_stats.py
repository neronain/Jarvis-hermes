#!/usr/bin/env python3
"""Jarvis GPU node stats endpoint — feeds the HUD "machines" panel.

Wire-compatible with upstream jarvis_ai's ``worker_stats.py``: ``GET /stats``
returns CPU/RAM/GPU utilisation. Extended here with per-sidecar health so the
HUD can show whether the ears and the mouth are actually up, not just the host.

    GET /stats -> {"cpu": 12.5, "ram": 41.0, "gpu": [...], "sidecars": {...}}
"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any, Dict, List

import psutil
import requests
import uvicorn
from fastapi import FastAPI

PORT = int(os.environ.get("JARVIS_STATS_PORT", "8767"))
HOST = os.environ.get("JARVIS_STATS_HOST", "0.0.0.0")
STT_PORT = int(os.environ.get("JARVIS_STT_PORT", "8768"))
TTS_PORT = int(os.environ.get("JARVIS_TTS_PORT", "8769"))

app = FastAPI(title="Jarvis GPU node stats", version="1.0.0")


def _gpu() -> List[Dict[str, Any]]:
    """Read GPU telemetry via nvidia-smi. Returns [] when there is no GPU."""
    if not shutil.which("nvidia-smi"):
        return []
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
    except Exception:
        return []

    gpus: List[Dict[str, Any]] = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 5:
            continue
        name, util, used, total, temp = parts
        try:
            gpus.append(
                {
                    "name": name,
                    "util": float(util),
                    "mem_used_mb": float(used),
                    "mem_total_mb": float(total),
                    "mem_pct": round(float(used) / float(total) * 100, 1) if float(total) else 0.0,
                    "temp_c": float(temp),
                }
            )
        except ValueError:
            continue
    return gpus


def _sidecar(port: int) -> Dict[str, Any]:
    try:
        r = requests.get(f"http://127.0.0.1:{port}/health", timeout=2)
        return {"up": r.status_code == 200, **(r.json() if r.status_code == 200 else {})}
    except Exception:
        return {"up": False}


@app.get("/stats")
async def stats() -> dict:
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    return {
        "cpu": psutil.cpu_percent(interval=0.1),
        "ram": vm.percent,
        "ram_used_gb": round(vm.used / 1024**3, 1),
        "ram_total_gb": round(vm.total / 1024**3, 1),
        "disk_pct": disk.percent,
        "uptime_seconds": int(psutil.boot_time()),
        "gpu": _gpu(),
        "sidecars": {"stt": _sidecar(STT_PORT), "tts": _sidecar(TTS_PORT)},
    }


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
