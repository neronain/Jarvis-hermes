#!/usr/bin/env python3
"""Let the agent put a map on the HUD.

What the research turned up, because it decided the design:

  earth.google.com cannot be embedded. It answers with
  `x-frame-options: SAMEORIGIN`, so an iframe pointed at it renders nothing,
  and no amount of arranging around it changes that. The page the user linked
  (developers.google.com/maps/documentation/earth) is the end-user help for
  Earth — projects, KML, Street View — not an embedding API at all.

  The Maps Embed API is the supported way in. /maps/embed/v1/* sends no
  X-Frame-Options (verified against the live endpoint), is built for iframes,
  and `maptype=satellite` gives the same imagery people mean when they say
  Google Earth. It needs an API key.

  Photorealistic 3D — an actual tilted globe — is the Maps JavaScript API's
  maps3d library, not an iframe of Earth. That runs in a page we serve
  ourselves (hud/earth3d.html) and needs the same key.

So: POST /api/map, the server builds the URL and keeps the key, and the HUD is
told to show it over the transcript. The key never reaches the agent.

    python apply_maps.py /path/to/jarvis_ai

Idempotent; writes .orig backups on first run. Run apply_all.py instead of
this directly — the patchers share a .orig base.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

MARKER = "# --- jarvis-hermes: maps ---"

BLOCK = '''
''' + MARKER + '''
MAPS_KEY_ENV = (CFG.get("maps") or {}).get("api_key_env", "JARVIS_MAPS_API_KEY")
# Photorealistic 3D needs Geocoding API and Map Tiles API, and both need a
# billing account on the key's project — the Maps Embed API needs neither,
# which is why the flat map works on a key that the other two refuse. Off by
# default: "earth" then falls through to satellite at a close zoom, which is
# what people are asking to see. Set JARVIS_MAPS_3D=1 once billing is linked.
MAPS_3D = os.environ.get("JARVIS_MAPS_3D", "0") == "1"


def _maps_key() -> str:
    return os.environ.get(MAPS_KEY_ENV, "").strip()


# Where the person looking at the HUD is standing. The server cannot know this
# and neither can the agent — only the browser can, and only with permission.
# The HUD posts it here when it has it, and "here" resolves against it.
_HERE: dict = {"lat": None, "lng": None, "accuracy": None, "at": 0.0}
HERE_TTL = float(os.environ.get("JARVIS_HERE_TTL", "900"))
_HERE_WORDS = {"here", "me", "my location", "current",
               "ที่ผมอยู่", "ที่ฉันอยู่", "ตำแหน่งปัจจุบัน", "ที่นี่", "ผม", "ฉัน"}


def _resolve_here(value: str) -> str:
    """"here" -> "13.7461,100.5348", or raises with something worth saying."""
    if (value or "").strip().lower() not in _HERE_WORDS:
        return value
    if _HERE["lat"] is None:
        raise ValueError(
            "ยังไม่รู้ตำแหน่งของคุณ — เปิด HUD ผ่าน https (พอร์ต 8766) "
            "แล้วกดอนุญาตให้เข้าถึงตำแหน่ง เบราว์เซอร์ไม่ให้ขอตำแหน่งบน http ธรรมดา"
        )
    if time.time() - _HERE["at"] > HERE_TTL:
        raise ValueError("ตำแหน่งที่เก็บไว้เก่าเกินไป — รีเฟรช HUD แล้วลองใหม่")
    return f"{_HERE['lat']},{_HERE['lng']}"


def _map_url(body: dict) -> tuple[str, str]:
    """(url, title) for one map request, or raises ValueError with the reason.

    Modes map onto the Maps Embed API's own, plus "earth" for the 3D page we
    serve. Everything is URL-encoded here rather than in the caller: a place
    name in Thai is the normal case, not the exception.
    """
    from urllib.parse import quote_plus, urlencode

    key = _maps_key()
    if not key:
        raise ValueError(
            f"no Google Maps API key: set {MAPS_KEY_ENV} in ~/.hermes/.env. "
            "Create one at console.cloud.google.com with Maps Embed API "
            "enabled, restricted to this host. Embed needs no billing account."
        )

    mode = (body.get("mode") or "satellite").lower()
    q = _resolve_here((body.get("q") or "").strip())
    lat, lng = body.get("lat"), body.get("lng")
    title = body.get("title") or q or "แผนที่"

    if mode == "earth" and MAPS_3D:
        # Our own page, which loads the maps3d library. It asks /api/mapkey for
        # the key itself, so the key is not in the URL we broadcast.
        params = {"label": title}
        if lat is not None and lng is not None:
            params.update(lat=lat, lng=lng)
        elif q:
            params["q"] = q
        else:
            raise ValueError("earth mode needs lat/lng or q")
        return f"/hud/earth3d.html?{urlencode(params)}", title

    if mode == "directions":
        raw_origin = (body.get("origin") or "").strip()
        raw_dest = (body.get("destination") or q).strip()
        if not raw_origin or not raw_dest:
            raise ValueError("directions needs origin and destination")
        origin, dest = _resolve_here(raw_origin), _resolve_here(raw_dest)
        if not body.get("title"):
            # The label says "ที่ผมอยู่", not a pair of coordinates nobody reads.
            title = f"{raw_origin} → {raw_dest}"
        url = ("https://www.google.com/maps/embed/v1/directions"
               f"?key={key}&origin={quote_plus(origin)}&destination={quote_plus(dest)}"
               f"&mode={quote_plus(body.get('travel') or 'driving')}&language=th&region=TH")
        return url, title

    if mode == "streetview":
        # Street View takes coordinates or a pano id, never a place name:
        # "location=วัดอรุณ" comes back as "Invalid 'location' parameter". Turning
        # a name into coordinates is the Geocoding API, which needs a billing
        # account — so with an Embed-only key a name falls through to satellite
        # rather than showing an error the caller cannot act on.
        if lat is not None and lng is not None:
            return (f"https://www.google.com/maps/embed/v1/streetview?key={key}"
                    f"&location={lat},{lng}&language=th&region=TH"), title
        if body.get("pano"):
            return (f"https://www.google.com/maps/embed/v1/streetview?key={key}"
                    f"&pano={quote_plus(str(body['pano']))}&language=th&region=TH"), title
        if not q:
            raise ValueError("streetview needs lat/lng, a pano id, or q")
        mode = "earth"      # satellite, close in — and the label says so
        title = f"{title} · ดาวเทียม (สตรีทวิวต้องใช้พิกัด)"

    # place / satellite / map — the ordinary case, and where "earth" lands when
    # 3D is off. Satellite imagery at a close zoom is what people are asking
    # for when they say Earth; the tilted globe is the part that needs billing.
    if not q and (lat is None or lng is None):
        raise ValueError("a map needs q, or lat and lng")
    where = q if q else f"{lat},{lng}"
    maptype = "roadmap" if mode in ("roadmap", "map") else "satellite"
    zoom = int(body.get("zoom") or (18 if mode == "earth" else
                                    16 if maptype == "satellite" else 14))
    return (f"https://www.google.com/maps/embed/v1/place?key={key}"
            f"&q={quote_plus(where)}&maptype={maptype}&zoom={zoom}"
            "&language=th&region=TH"), title


async def _broadcast(payload: dict) -> int:
    sent = 0
    for client in list(WS_CLIENTS):
        try:
            await client.send_json(payload)
            sent += 1
        except Exception:
            WS_CLIENTS.discard(client)
    return sent


@app.get("/api/mapkey")
async def map_key() -> JSONResponse:
    """The browser key, for the 3D page. Behind the same token as everything
    else under /api/; a Maps key is meant to be public and restricted by
    referrer, but there is no reason to hand it to the open internet."""
    key = _maps_key()
    if not key:
        return JSONResponse({"error": f"{MAPS_KEY_ENV} is not set"}, status_code=503)
    return JSONResponse({"key": key})


@app.post("/api/here")
async def set_here(request: Request) -> JSONResponse:
    """The HUD telling the server where its viewer is.

    Kept in memory only, and only for HERE_TTL. It is one person's position in
    a house, not a location history, and it has no business outliving the
    process or reaching disk.
    """
    body = await request.json()
    try:
        lat, lng = float(body["lat"]), float(body["lng"])
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"error": "lat and lng required"}, status_code=400)
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return JSONResponse({"error": "out of range"}, status_code=400)
    _HERE.update(lat=round(lat, 6), lng=round(lng, 6),
                 accuracy=body.get("accuracy"), at=time.time())
    return JSONResponse({"ok": True, "accuracy_m": _HERE["accuracy"]})


@app.get("/api/here")
async def get_here() -> JSONResponse:
    fresh = _HERE["lat"] is not None and (time.time() - _HERE["at"]) <= HERE_TTL
    return JSONResponse({
        "known": fresh,
        "age_seconds": round(time.time() - _HERE["at"], 1) if _HERE["at"] else None,
        "accuracy_m": _HERE["accuracy"] if fresh else None,
    })


@app.post("/api/map")
async def show_map(request: Request) -> JSONResponse:
    """Put a map on every connected HUD.

    Body: {"q": "อนุสาวรีย์ชัยสมรภูมิ", "mode": "satellite"}
          {"lat": 13.7649, "lng": 100.5383, "mode": "earth", "title": "..."}
          {"origin": "here", "destination": "CYN Communication", "mode": "directions"}
          {"action": "dismiss"}

    The agent calls this with its terminal tool; it never sees the API key.
    """
    body = await request.json()
    if body.get("action") == "dismiss":
        return JSONResponse({"sent_to": await _broadcast({"type": "dismiss_panels"})})
    try:
        url, title = _map_url(body)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    sent = await _broadcast({
        "type": "summon_panel", "media": "map", "src": url, "title": title,
        "mode": (body.get("mode") or "satellite"),
    })
    return JSONResponse({"sent_to": sent, "title": title})


'''

# The agent runs on this host and calls /api/map with its terminal tool. Making
# it pass the HUD token would mean putting the token in the prompt, where it
# would end up in logs and transcripts; a loopback exemption is the smaller
# risk. It is safe by construction rather than by trust: /api/map never accepts
# a URL. It takes a place name and builds a Google Maps URL from our own
# template, so the worst a local process can do is show the wrong place.
AUTH_OLD = (
    '    if request.url.path.startswith("/api/") and not _request_authed(request):'
)
AUTH_NEW = (
    '    if (request.url.path == "/api/map"\n'
    '            and request.client and request.client.host in ("127.0.0.1", "::1")):\n'
    '        return await call_next(request)\n'
    '    if request.url.path.startswith("/api/") and not _request_authed(request):'
)

ANCHOR = '@app.post("/api/summon")'


def _patch(path: Path, pairs, marker: str) -> str:
    src = path.read_text(encoding="utf-8")
    if marker in src:
        return "already patched"
    for old, _ in pairs:
        if old not in src:
            return f"anchor not found: {old.strip()[:60]}"
    backup = path.with_suffix(path.suffix + ".orig")
    if not backup.exists():
        shutil.copy2(path, backup)
    for old, new in pairs:
        src = src.replace(old, new, 1)
    path.write_text(src, encoding="utf-8")
    return "patched"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    root = Path(argv[1]).expanduser().resolve()
    server = root / "server" / "server.py"
    if not server.exists():
        print(f"missing: {server}", file=sys.stderr)
        return 1
    print("server.py:", _patch(server, [
        (ANCHOR, BLOCK.lstrip("\n") + ANCHOR),
        (AUTH_OLD, AUTH_NEW),
    ], MARKER))

    # The 3D page ships with the HUD; copy it in if the Flight Deck is there.
    hud = root / "server" / "hud"
    here = Path(__file__).resolve().parents[1] / "hud" / "earth3d.html"
    if hud.exists() and here.exists():
        shutil.copy2(here, hud / "earth3d.html")
        print("hud: earth3d.html installed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
