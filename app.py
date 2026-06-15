"""
Onshape Side-Panel App
======================

This is a tiny website that Onshape loads *inside itself* as a right-side
panel. When you open it in a Part Studio, Onshape passes the current
document/workspace/element ids in the URL. The panel then calls Onshape's
REST API (using your API keys) to show live info about your model and lets
you do a few actions with buttons.

Run it:   .venv\\Scripts\\python.exe app.py
It serves on http://localhost:8000 — a tunnel gives it a public https URL
so Onshape can embed it.
"""

import os
import math
import asyncio
import httpx
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

BASE_URL = os.environ.get("ONSHAPE_BASE_URL", "https://cad.onshape.com/api/v10")
ACCESS_KEY = os.environ.get("ONSHAPE_ACCESS_KEY", "")
SECRET_KEY = os.environ.get("ONSHAPE_SECRET_KEY", "")

# Google Gemini — the free "brain" for the chat box.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
# Try these models in order. EACH has its own free daily quota, so when the
# best one is exhausted we fall through to the next before giving up — this is
# the main reason builds used to "completely fail".
GEMINI_MODELS = list(dict.fromkeys([
    GEMINI_MODEL, "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash"]))


def _gemini_url(model):
    return (f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent")

# Groq — free backup brain (much higher rate limits). When GROQ_API_KEY is set,
# it's used as a fallback whenever Gemini is rate-limited / out of quota, so the
# panel keeps working instead of failing. Vision (image->CAD) stays on Gemini.
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# --- Security -------------------------------------------------------------
# A secret token that must be present on every request. It is baked into the
# Action URL you register in Onshape (?token=...), so only you — loading the
# panel from inside your own Onshape — ever has it. Random people who find the
# host URL but don't know the token get rejected.
APP_TOKEN = os.environ.get("APP_TOKEN", "")

# Only let Onshape embed this page in an iframe (blocks clickjacking / other
# sites framing your panel).
SECURITY_HEADERS = {
    "Content-Security-Policy": "frame-ancestors https://*.onshape.com",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

# The standalone CAD page is its own top-level site (not framed by Onshape) and
# must load JSCAD/Three from a CDN and run AI-generated geometry code, so it
# uses light headers (no restrictive CSP).
SECURITY_HEADERS_CAD = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def token_ok(request) -> bool:
    """True if the request carries the right secret token."""
    if not APP_TOKEN:
        return True  # no token configured (local dev) -> allow
    supplied = request.query_params.get("token") or request.headers.get("x-app-token")
    return supplied == APP_TOKEN


async def onshape_get(path: str, params: dict | None = None):
    """Call the Onshape REST API and return parsed JSON."""
    async with httpx.AsyncClient(
        base_url=BASE_URL,
        auth=(ACCESS_KEY, SECRET_KEY),
        headers={"Accept": "application/json"},
        timeout=30.0,
    ) as c:
        r = await c.get(path, params=params or {})
        r.raise_for_status()
        return r.json()


async def onshape_post(path: str, body: dict):
    """POST to the Onshape REST API (used for write/create actions)."""
    async with httpx.AsyncClient(
        base_url=BASE_URL,
        auth=(ACCESS_KEY, SECRET_KEY),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=30.0,
    ) as c:
        r = await c.post(path, json=body)
        r.raise_for_status()
        return r.json()


async def onshape_delete(path: str):
    """DELETE on the Onshape REST API (used to remove features)."""
    async with httpx.AsyncClient(
        base_url=BASE_URL,
        auth=(ACCESS_KEY, SECRET_KEY),
        headers={"Accept": "application/json"},
        timeout=30.0,
    ) as c:
        r = await c.delete(path)
        r.raise_for_status()
        return r.json() if r.content else {}


# ---- The panel web page (HTML + a little JavaScript) ----------------------

PANEL_HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>My Onshape Panel</title>
<style>
  body { font-family: 'Segoe UI', system-ui, sans-serif; margin: 0; padding: 14px;
         background: #1e1e1e; color: #eee; font-size: 14px; }
  h1 { font-size: 16px; margin: 0 0 4px; }
  .sub { color: #888; font-size: 12px; margin-bottom: 14px; }
  button { background: #0b5fff; color: #fff; border: 0; border-radius: 6px;
           padding: 8px 12px; margin: 4px 4px 4px 0; cursor: pointer; font-size: 13px; }
  button:hover { background: #2a74ff; }
  #out { margin-top: 12px; white-space: pre-wrap; background: #111; border-radius: 6px;
         padding: 10px; min-height: 40px; font-family: Consolas, monospace; font-size: 12px; }
  .part { padding: 6px 8px; background: #2a2a2a; border-radius: 5px; margin: 4px 0; }
</style>
</head>
<body>
  <h1>🛠️ My Panel</h1>
  <div class="sub" id="ctx">reading document context…</div>

  <div id="fallback" style="display:none; background:#3a2e00; border:1px solid #6b5600;
       border-radius:6px; padding:8px; margin:6px 0 10px; font-size:12px;">
    Paste this model's Onshape link (copy it from your browser's address bar) so the
    panel can connect:
    <div style="display:flex; gap:6px; margin-top:6px;">
      <input id="pasteUrl" placeholder="https://cad.onshape.com/documents/…/w/…/e/…"
        style="flex:1; padding:6px; border-radius:5px; border:1px solid #444;
               background:#222; color:#eee; font-size:12px;">
      <button onclick="applyPastedUrl()">Connect</button>
    </div>
  </div>

  <button onclick="loadParts()">List parts</button>
  <button onclick="loadMass()">Mass &amp; volume</button>
  <button onclick="loadFeatures()">Feature tree</button>
  <button onclick="loadDetails()">See everything</button>

  <div id="out">Click a button above.</div>

  <hr style="border-color:#333; margin:16px 0;">
  <h1>💬 Ask the AI</h1>
  <div id="chat" style="background:#111; border-radius:6px; padding:8px; min-height:60px;
       max-height:220px; overflow-y:auto; margin-bottom:8px; font-size:13px;"></div>
  <div id="imgnote" style="display:none; font-size:12px; color:#ffd000; margin-bottom:6px;"></div>
  <div style="display:flex; gap:6px;">
    <input id="msg" placeholder="e.g. how big is this part?"
       style="flex:1; padding:8px; border-radius:6px; border:1px solid #444;
              background:#222; color:#eee; font-size:13px;"
       onkeydown="if(event.key==='Enter')sendChat()">
    <button onclick="document.getElementById('imgfile').click()" title="Build from a photo">📷</button>
    <button onclick="sendChat()">Send</button>
  </div>
  <input id="imgfile" type="file" accept="image/*" style="display:none"
     onchange="onImagePicked(this.files[0])">

<script>
  const TOKEN = "__APP_TOKEN__";
  let did, wid, eid;

  // A value is real (not an un-substituted placeholder like {$documentId} or
  // ${documentId} or its URL-encoded form, and not empty/undefined).
  function real(v){ return v && !/[{}$%]/.test(v) && v !== 'undefined' && v !== 'null'; }

  // Pull document/workspace/element ids out of any Onshape URL string.
  function idsFromUrl(str){
    const m = (str || '').match(
      /documents\\/([0-9a-f]{12,})\\/(?:w|v|m)\\/([0-9a-f]{12,})\\/e\\/([0-9a-f]{12,})/i);
    return m ? {did:m[1], wid:m[2], eid:m[3]} : null;
  }

  function readContext(){
    // 1) Query params from the Action URL placeholders (the official way).
    const q = new URLSearchParams(location.search);
    let d = q.get('documentId');
    let w = q.get('workspaceId') || q.get('workspaceOrVersionId') || q.get('workspaceOrVersion');
    let e = q.get('elementId');
    if (real(d) && real(e)) { did=d; wid=w; eid=e; return; }
    // 2) The Onshape document URL that embedded us (works with NO placeholders).
    const fromRef = idsFromUrl(document.referrer);
    if (fromRef) { did=fromRef.did; wid=fromRef.wid; eid=fromRef.eid; return; }
    // 3) Remembered from a previous manual paste.
    try {
      const s = JSON.parse(localStorage.getItem('onshapeCtx') || 'null');
      if (s && s.did) { did=s.did; wid=s.wid; eid=s.eid; }
    } catch(_){}
  }

  function applyPastedUrl(){
    const r = idsFromUrl(document.getElementById('pasteUrl').value.trim());
    if (!r){ alert('That is not an Onshape document URL. It should look like\\n' +
                   'cad.onshape.com/documents/.../w/.../e/...'); return; }
    did=r.did; wid=r.wid; eid=r.eid;
    try { localStorage.setItem('onshapeCtx', JSON.stringify({did,wid,eid})); } catch(_){}
    updateCtx();
  }

  function updateCtx(){
    const ok = real(did) && real(eid);
    document.getElementById('ctx').textContent = ok
      ? ('✓ connected · document ' + did.slice(0,8) + '… · element ' + eid.slice(0,8) + '…')
      : 'Not connected to a model yet — paste your Onshape link below. 👇';
    document.getElementById('fallback').style.display = ok ? 'none' : 'block';
  }

  readContext();
  updateCtx();

  const out = document.getElementById('out');
  function show(t){ out.textContent = t; }
  function busy(){ out.textContent = 'loading…'; }

  function connected(){
    if (real(did) && real(eid)) return true;
    document.getElementById('fallback').style.display = 'block';
    show('Not connected to a model yet — paste your Onshape link above first.');
    return false;
  }

  async function call(kind){
    if (!connected()) return;
    busy();
    const url = `/api/${kind}?documentId=${did}&workspaceId=${wid}&elementId=${eid}&token=${TOKEN}`;
    try {
      const r = await fetch(url);
      const data = await r.json();
      if (!r.ok) { show('Error: ' + (data.error || r.status)); return; }
      return data;
    } catch (e) { show('Error: ' + e); }
  }

  async function loadParts(){
    const d = await call('parts'); if(!d) return;
    out.innerHTML = d.length
      ? d.map(p => `<div class="part">🧩 <b>${p.name}</b> (${p.bodyType})</div>`).join('')
      : 'No parts found.';
  }
  async function loadMass(){
    const d = await call('mass'); if(!d) return;
    const v = d.volume ? (d.volume[0]*1e9).toFixed(1) + ' mm³' : 'n/a';
    const m = d.mass ? (d.mass[0]*1000).toFixed(2) + ' g' : 'n/a';
    show('Volume: ' + v + '\\nMass:   ' + m + '\\n(mass needs a material assigned)');
  }
  async function loadFeatures(){
    const d = await call('features'); if(!d) return;
    out.innerHTML = d.map(f => `<div class="part">${f.suppressed?'⊘':'▸'} ${f.name} <span style="color:#888">(${f.type})</span></div>`).join('');
  }
  async function loadDetails(){
    const d = await call('details'); if(!d) return;
    out.innerHTML = Array.isArray(d) ? d.map(s => `<div class="part">${s}</div>`).join('') : JSON.stringify(d);
  }

  // ---- AI chat ----
  const chat = document.getElementById('chat');
  let pendingImage = null;   // a data-URL when the user attaches a photo
  function bubble(who, text){
    const c = who==='you' ? '#0b5fff' : '#2a2a2a';
    chat.innerHTML += `<div style="margin:4px 0"><span style="background:${c};padding:4px 8px;border-radius:10px;display:inline-block">${text}</span></div>`;
    chat.scrollTop = chat.scrollHeight;
  }

  // Shrink a picked image to <=768px and remember it for the next send.
  function onImagePicked(file){
    if(!file) return;
    const img = new Image();
    img.onload = function(){
      const max = 768, s = Math.min(1, max/Math.max(img.width, img.height));
      const cv = document.createElement('canvas');
      cv.width = Math.round(img.width*s); cv.height = Math.round(img.height*s);
      cv.getContext('2d').drawImage(img, 0, 0, cv.width, cv.height);
      pendingImage = cv.toDataURL('image/jpeg', 0.85);
      const note = document.getElementById('imgnote');
      note.style.display = 'block';
      note.textContent = '📷 image attached — press Send (or type what to build) ';
    };
    img.src = URL.createObjectURL(file);
  }

  async function sendChat(){
    const input = document.getElementById('msg');
    const text = input.value.trim();
    if(!text && !pendingImage) return;          // nothing to send
    if(!real(did)||!real(eid)){ document.getElementById('fallback').style.display='block';
      bubble('ai','Paste your Onshape link at the top first so I can see/build the model.'); return; }
    const img = pendingImage; pendingImage = null;
    document.getElementById('imgnote').style.display = 'none';
    bubble('you', (img ? '📷 ' : '') + (text || 'build this from the image'));
    input.value=''; bubble('ai','…thinking');
    try{
      const r = await fetch('/api/chat', {
        method:'POST', headers:{'Content-Type':'application/json', 'x-app-token':TOKEN},
        body: JSON.stringify({message:text, image:img, documentId:did, workspaceId:wid, elementId:eid})
      });
      const data = await r.json();
      chat.lastChild.remove(); // remove the "thinking" bubble
      bubble('ai', r.ok ? data.answer : ('Error: ' + (data.error||r.status)));
    }catch(e){ chat.lastChild.remove(); bubble('ai','Error: '+e); }
  }
</script>
</body>
</html>
"""


# ---- Routes ---------------------------------------------------------------

async def panel(request):
    # Token can come from the URL path (/p/<token>) OR the ?token= query.
    # Path form is used inside Onshape so Onshape's own ?documentId=... params
    # don't collide with ours.
    token = request.path_params.get("token") or request.query_params.get("token", "")
    if APP_TOKEN and token != APP_TOKEN:
        return HTMLResponse("<h3>Not authorized.</h3>", status_code=403)
    html = PANEL_HTML.replace("__APP_TOKEN__", token)
    return HTMLResponse(html, headers=SECURITY_HEADERS)


async def api_parts(request):
    if not token_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    q = request.query_params
    try:
        data = await onshape_get(
            f"/parts/d/{q['documentId']}/w/{q['workspaceId']}/e/{q['elementId']}"
        )
        return JSONResponse([
            {"name": p.get("name"), "partId": p.get("partId"), "bodyType": p.get("bodyType")}
            for p in data
        ])
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_mass(request):
    if not token_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    q = request.query_params
    try:
        data = await onshape_get(
            f"/partstudios/d/{q['documentId']}/w/{q['workspaceId']}/e/{q['elementId']}/massproperties"
        )
        bodies = data.get("bodies", {})
        t = bodies.get("-all-", next(iter(bodies.values()), {}))
        return JSONResponse({"volume": t.get("volume"), "mass": t.get("mass")})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_features(request):
    if not token_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    q = request.query_params
    try:
        data = await onshape_get(
            f"/partstudios/d/{q['documentId']}/w/{q['workspaceId']}/e/{q['elementId']}/features"
        )
        return JSONResponse([
            {"name": f.get("name"), "type": f.get("featureType"), "suppressed": f.get("suppressed")}
            for f in data.get("features", [])
        ])
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


def _summarize_feature(f):
    """Turn one raw feature into a readable line, including sketch contents."""
    name = f.get("name", "?")
    ftype = f.get("featureType", "?")
    bits = []
    # Count what's drawn inside a sketch (lines, circles, arcs...).
    ents = f.get("entities", [])
    if ents:
        kinds = {}
        for e in ents:
            bt = (e.get("btType") or "").lower()
            if "curvesegment" in bt or "line" in bt:
                key = "lines"
            elif "circle" in bt:
                key = "circles"
            elif "arc" in bt:
                key = "arcs"
            elif "point" in bt:
                key = "points"
            else:
                key = "other"
            kinds[key] = kinds.get(key, 0) + 1
        bits.append("contains " + ", ".join(f"{v} {k}" for k, v in kinds.items()))
    # Pull a couple of parameter values (e.g. extrude depth).
    for p in f.get("parameters", []):
        pid = p.get("parameterId")
        val = p.get("expression") or p.get("value")
        if pid in ("depth", "distance", "angle") and val:
            bits.append(f"{pid}={val}")
    suffix = (" — " + "; ".join(bits)) if bits else ""
    sup = " (suppressed)" if f.get("suppressed") else ""
    return f"{name} [{ftype}]{sup}{suffix}"


async def api_details(request):
    """Detailed view of every feature, including what's inside each sketch."""
    if not token_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    q = request.query_params
    try:
        data = await onshape_get(
            f"/partstudios/d/{q['documentId']}/w/{q['workspaceId']}/e/{q['elementId']}/features"
        )
        return JSONResponse([_summarize_feature(f) for f in data.get("features", [])])
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


async def gather_context(did, wid, eid):
    """Collect a short summary of the current part studio for the AI."""
    facts = []
    try:
        parts = await onshape_get(f"/parts/d/{did}/w/{wid}/e/{eid}")
        facts.append("Parts: " + ", ".join(p.get("name", "?") for p in parts) if parts
                     else "Parts: none")
    except Exception:
        facts.append("Parts: (could not read)")
    try:
        mp = await onshape_get(
            f"/partstudios/d/{did}/w/{wid}/e/{eid}/massproperties")
        bodies = mp.get("bodies", {})
        t = bodies.get("-all-", next(iter(bodies.values()), {}))
        vol = t.get("volume")
        if vol:
            facts.append(f"Volume: {vol[0]*1e9:.1f} cubic millimetres")
    except Exception:
        pass
    try:
        ft = await onshape_get(f"/partstudios/d/{did}/w/{wid}/e/{eid}/features")
        feats = ft.get("features", [])
        if feats:
            facts.append("Feature tree (with sketch contents):")
            for f in feats:
                facts.append("  - " + _summarize_feature(f))
    except Exception:
        pass
    return "\n".join(facts)


# ---- WRITE actions (need API keys with the "write" scope) -----------------
# Every geometry tool below was verified against live Onshape (it actually
# produces a solid / changes mass) before being wired up.

PLANES = {"top": "Top", "front": "Front", "right": "Right"}


def _fs_path(did, wid, eid, suffix=""):
    return f"/partstudios/d/{did}/w/{wid}/e/{eid}{suffix}"


def _plane_id(name):
    return PLANES.get((name or "top").strip().lower(), "Top")


async def _all_features(did, wid, eid):
    return (await onshape_get(_fs_path(did, wid, eid, "/features"))).get("features", [])


async def _find_feature(did, wid, eid, name):
    """Find a feature by (case-insensitive) name; returns the raw dict or None."""
    name_l = (name or "").strip().lower()
    for f in await _all_features(did, wid, eid):
        if (f.get("name") or "").strip().lower() == name_l:
            return f
    return None


async def _update_feature(did, wid, eid, feature):
    """POST an edited feature back (used by edit/rename/suppress)."""
    fid = feature["featureId"]
    return await onshape_post(
        _fs_path(did, wid, eid, f"/features/featureid/{fid}"), {"feature": feature})


def _z_offset_params(cz_mm):
    """Extra extrude params so the solid's base sits at height cz (mm), letting
    the AI stack parts. Verified directions: cz>0 -> opposite=False shifts up;
    cz<0 -> opposite=True shifts down."""
    if not cz_mm:
        return []
    return [
        {"btType": "BTMParameterBoolean-144", "parameterId": "startOffset", "value": True},
        {"btType": "BTMParameterEnum-145", "parameterId": "startOffsetBound",
         "enumName": "StartOffsetType", "value": "BLIND"},
        {"btType": "BTMParameterQuantity-147", "parameterId": "startOffsetDistance",
         "expression": f"{abs(cz_mm)} mm"},
        {"btType": "BTMParameterBoolean-144", "parameterId": "startOffsetOppositeDirection",
         "value": cz_mm < 0},
    ]


async def _extrude_sketch(did, wid, eid, sketch_id, height_mm, name, cz_mm=0):
    extrude = {"feature": {
        "btType": "BTMFeature-134", "featureType": "extrude", "name": name,
        "parameters": [
            {"btType": "BTMParameterEnum-145", "parameterId": "bodyType",
             "enumName": "ExtendedToolBodyType", "value": "SOLID"},
            {"btType": "BTMParameterEnum-145", "parameterId": "endBound",
             "enumName": "BoundingType", "value": "BLIND"},
            {"btType": "BTMParameterQuantity-147", "parameterId": "depth",
             "expression": f"{height_mm} mm"},
            {"btType": "BTMParameterQueryList-148", "parameterId": "entities",
             "queries": [{"btType": "BTMIndividualQuery-138",
                          "queryString": f'query=qCreatedBy(makeId("{sketch_id}"), EntityType.FACE);'}]},
        ] + _z_offset_params(cz_mm)}}
    return await onshape_post(_fs_path(did, wid, eid, "/features"), extrude)


async def _cut_extrude(did, wid, eid, sketch_id, name, depth_mm=0):
    """Subtract a sketched profile from existing solids (a hole / pocket).
    depth_mm<=0 cuts all the way through; otherwise a blind pocket of that depth
    starting from the sketch (Top) plane. Verified: REMOVE op, mass drops."""
    bound = ([{"btType": "BTMParameterEnum-145", "parameterId": "endBound",
               "enumName": "BoundingType", "value": "THROUGH_ALL"}] if depth_mm <= 0 else
             [{"btType": "BTMParameterEnum-145", "parameterId": "endBound",
               "enumName": "BoundingType", "value": "BLIND"},
              {"btType": "BTMParameterQuantity-147", "parameterId": "depth",
               "expression": f"{depth_mm} mm"}])
    extrude = {"feature": {
        "btType": "BTMFeature-134", "featureType": "extrude", "name": name,
        "parameters": [
            {"btType": "BTMParameterEnum-145", "parameterId": "bodyType",
             "enumName": "ExtendedToolBodyType", "value": "SOLID"},
            {"btType": "BTMParameterEnum-145", "parameterId": "operationType",
             "enumName": "NewBodyOperationType", "value": "REMOVE"},
            {"btType": "BTMParameterQueryList-148", "parameterId": "entities",
             "queries": [{"btType": "BTMIndividualQuery-138",
                          "queryString": f'query=qCreatedBy(makeId("{sketch_id}"), EntityType.FACE);'}]},
        ] + bound}}
    return await onshape_post(_fs_path(did, wid, eid, "/features"), extrude)


def _line_seg(eid_, p1, p2, pts):
    """A straight sketch segment between two named points (metres)."""
    x0, y0 = pts[p1]; x1, y1 = pts[p2]
    length = math.hypot(x1 - x0, y1 - y0)
    return {"btType": "BTMSketchCurveSegment-155", "entityId": eid_,
            "startPointId": p1, "endPointId": p2, "startParam": 0.0, "endParam": length,
            "geometry": {"btType": "BTCurveGeometryLine-117",
                         "pntX": x0, "pntY": y0,
                         "dirX": (x1 - x0) / length, "dirY": (y1 - y0) / length}}


async def _sketch_on(did, wid, eid, plane_id, name, entities):
    sketch = {"feature": {
        "btType": "BTMSketch-151", "featureType": "newSketch", "name": name,
        "parameters": [{"btType": "BTMParameterQueryList-148", "parameterId": "sketchPlane",
            "queries": [{"btType": "BTMIndividualQuery-138",
                "queryString": f'query=qCreatedBy(makeId("{plane_id}"), EntityType.FACE);'}]}],
        "entities": entities}}
    sk = await onshape_post(_fs_path(did, wid, eid, "/features"), sketch)
    return sk["feature"]["featureId"]


async def _revolve_profile(did, wid, eid, plane_id, name, entities, axis_entity_id):
    """Sketch a closed profile that includes a straight axis edge, then revolve
    it 360 deg about that edge. Used for spheres and cones. (axis selected via
    sketchEntityQuery — verified against live Onshape.)"""
    sid = await _sketch_on(did, wid, eid, plane_id, name + " Sketch", entities)
    rev = {"feature": {"btType": "BTMFeature-134", "featureType": "revolve", "name": name,
        "parameters": [
            {"btType": "BTMParameterEnum-145", "parameterId": "bodyType",
             "enumName": "ExtendedToolBodyType", "value": "SOLID"},
            {"btType": "BTMParameterEnum-145", "parameterId": "operationType",
             "enumName": "NewBodyOperationType", "value": "NEW"},
            {"btType": "BTMParameterQueryList-148", "parameterId": "entities",
             "queries": [{"btType": "BTMIndividualQuery-138",
                "queryString": f'query=qCreatedBy(makeId("{sid}"), EntityType.FACE);'}]},
            {"btType": "BTMParameterQueryList-148", "parameterId": "axis",
             "queries": [{"btType": "BTMIndividualQuery-138",
                "queryString": f'query=sketchEntityQuery(makeId("{sid}"), EntityType.EDGE, "{axis_entity_id}");'}]},
            {"btType": "BTMParameterBoolean-144", "parameterId": "fullRevolve", "value": True}]}}
    return await onshape_post(_fs_path(did, wid, eid, "/features"), rev)


async def act_delete_last_feature(did, wid, eid):
    """Delete the most recent feature in the part studio."""
    feats = await _all_features(did, wid, eid)
    if not feats:
        return "There are no features to delete."
    last = feats[-1]
    await onshape_delete(_fs_path(did, wid, eid, f"/features/featureid/{last['featureId']}"))
    return f"Deleted the last feature: {last.get('name')}."


async def act_delete_feature(did, wid, eid, name=None):
    """Delete a feature by name (or the last one if no name given)."""
    if not name:
        return await act_delete_last_feature(did, wid, eid)
    f = await _find_feature(did, wid, eid, name)
    if not f:
        return f"No feature named '{name}'."
    await onshape_delete(_fs_path(did, wid, eid, f"/features/featureid/{f['featureId']}"))
    return f"Deleted feature '{f.get('name')}'."


async def act_build_box(did, wid, eid, width_mm, depth_mm, height_mm,
                        x_mm=0, y_mm=0, z_mm=0):
    """Create a box on the Top plane, centred at (x,y) and resting at height z
    (mm). x/y/z let the AI place and stack parts to assemble a bigger object."""
    X, Y = x_mm / 1000.0, y_mm / 1000.0
    w, d = width_mm / 2000.0, depth_mm / 2000.0   # half sizes, metres
    pts = {"pA": (X - w, Y - d), "pB": (X + w, Y - d),
           "pC": (X + w, Y + d), "pD": (X - w, Y + d)}
    ents = [_line_seg("lAB", "pA", "pB", pts), _line_seg("lBC", "pB", "pC", pts),
            _line_seg("lCD", "pC", "pD", pts), _line_seg("lDA", "pD", "pA", pts)]
    sid = await _sketch_on(did, wid, eid, "Top", "AI Box Sketch", ents)
    await _extrude_sketch(did, wid, eid, sid, height_mm, "AI Box Extrude", z_mm)
    return (f"Built a {width_mm}x{depth_mm}x{height_mm} mm box centred at "
            f"x={x_mm}, y={y_mm}, base at z={z_mm}.")


async def act_build_cylinder(did, wid, eid, diameter_mm, height_mm,
                             x_mm=0, y_mm=0, z_mm=0):
    """Create a cylinder on the Top plane, centred at (x,y), resting at height z."""
    X, Y = x_mm / 1000.0, y_mm / 1000.0
    r = diameter_mm / 2000.0
    circle = {"btType": "BTMSketchCurve-4", "entityId": "circle1",
        "geometry": {"btType": "BTCurveGeometryCircle-115", "radius": r,
            "xCenter": X, "yCenter": Y, "xDir": 1.0, "yDir": 0.0, "clockwise": False},
        "centerId": "circle1.center"}
    sid = await _sketch_on(did, wid, eid, "Top", "AI Cylinder Sketch", [circle])
    await _extrude_sketch(did, wid, eid, sid, height_mm, "AI Cylinder Extrude", z_mm)
    return (f"Built a {diameter_mm} mm wide x {height_mm} mm tall cylinder centred "
            f"at x={x_mm}, y={y_mm}, base at z={z_mm}.")


async def act_build_sphere(did, wid, eid, diameter_mm, x_mm=0, z_mm=0):
    """Create a sphere by revolving a half-disc. Centre lands at world
    (x, 0, z) mm — x is left/right, z is height (good for stacking, e.g. a
    snowman). Built on the Front plane."""
    r = diameter_mm / 2000.0
    X, Z = x_mm / 1000.0, z_mm / 1000.0
    arc = {"btType": "BTMSketchCurveSegment-155", "entityId": "arc",
           "startPointId": "pBot", "endPointId": "pTop",
           "startParam": -math.pi / 2, "endParam": math.pi / 2,
           "geometry": {"btType": "BTCurveGeometryCircle-115", "radius": r,
                        "xCenter": X, "yCenter": Z, "xDir": 1.0, "yDir": 0.0,
                        "clockwise": False}}
    axis = {"btType": "BTMSketchCurveSegment-155", "entityId": "axisLine",
            "startPointId": "pTop", "endPointId": "pBot",
            "startParam": 0.0, "endParam": 2 * r,
            "geometry": {"btType": "BTCurveGeometryLine-117",
                         "pntX": X, "pntY": Z + r, "dirX": 0.0, "dirY": -1.0}}
    await _revolve_profile(did, wid, eid, "Front", "AI Sphere", [arc, axis], "axisLine")
    return f"Built a {diameter_mm} mm sphere centred at x={x_mm}, height z={z_mm}."


async def act_build_cone(did, wid, eid, base_diameter_mm, height_mm, x_mm=0, z_mm=0):
    """Create a cone by revolving a right triangle. Base (centre x, height z)
    sits flat; apex points up. Built on the Front plane."""
    R = base_diameter_mm / 2000.0
    h = height_mm / 1000.0
    X, Z = x_mm / 1000.0, z_mm / 1000.0
    pts = {"pBase": (X, Z), "pRim": (X + R, Z), "pApex": (X, Z + h)}
    base = _line_seg("lBase", "pBase", "pRim", pts)
    slant = _line_seg("lSlant", "pRim", "pApex", pts)
    axis = _line_seg("axisLine", "pApex", "pBase", pts)
    await _revolve_profile(did, wid, eid, "Front", "AI Cone", [base, slant, axis], "axisLine")
    return (f"Built a {base_diameter_mm} mm wide x {height_mm} mm tall cone, "
            f"base at x={x_mm}, height z={z_mm}.")


async def act_build_torus(did, wid, eid, ring_diameter_mm, tube_diameter_mm,
                          x_mm=0, z_mm=0):
    """Build a torus (donut / ring / washer / tyre) by revolving a circle around
    an axis. ring_diameter = centreline diameter, tube_diameter = thickness."""
    R = ring_diameter_mm / 2000.0
    r = tube_diameter_mm / 2000.0
    X, Z = x_mm / 1000.0, z_mm / 1000.0
    circle = _circle_entity(X + R, Z, r, "tube")
    half = R + r + r
    axis = {"btType": "BTMSketchCurveSegment-155", "entityId": "axisLine",
            "startPointId": "pa", "endPointId": "pb",
            "startParam": 0.0, "endParam": 2 * half,
            "geometry": {"btType": "BTCurveGeometryLine-117",
                         "pntX": X, "pntY": Z - half, "dirX": 0.0, "dirY": 1.0}}
    await _revolve_profile(did, wid, eid, "Front", "AI Torus", [circle, axis], "axisLine")
    return (f"Built a torus (ring {ring_diameter_mm} mm across, tube "
            f"{tube_diameter_mm} mm thick) at x={x_mm}, height z={z_mm}.")


async def act_build_screw(did, wid, eid, diameter_mm, length_mm, pitch_mm=0,
                          x_mm=0, z_mm=0):
    """Build a threaded screw / bolt shaft: a cylinder with a real helical thread
    swept onto it. pitch_mm = distance between thread turns (auto if 0). Verified:
    helix (startAngle 0) + triangular profile straddling the surface + sweep ADD."""
    R = diameter_mm / 2000.0
    X, Z = x_mm / 1000.0, z_mm / 1000.0
    p_mm = pitch_mm or max(1.0, diameter_mm * 0.18)
    p = p_mm / 1000.0
    # 1) shaft
    shaft_circle = _circle_entity(X, 0.0, R, "shaft")
    ssid = await _sketch_on(did, wid, eid, "Top", "AI Screw Shaft Sketch", [shaft_circle])
    shaft_res = await _extrude_sketch(did, wid, eid, ssid, length_mm, "AI Screw Shaft", z_mm)
    shaft_id = shaft_res["feature"]["featureId"]
    # 2) helix on the shaft's cylindrical surface
    helix = {"feature": {"btType": "BTMFeature-134", "featureType": "helix",
        "name": "AI Screw Helix", "parameters": [
            {"btType": "BTMParameterEnum-145", "parameterId": "axisType",
             "enumName": "AxisType", "value": "SURFACE"},
            {"btType": "BTMParameterQueryList-148", "parameterId": "entities",
             "queries": [{"btType": "BTMIndividualQuery-138",
                "queryString": f'query=qGeometry(qCreatedBy(makeId("{shaft_id}"), EntityType.FACE), GeometryType.CYLINDER);'}]},
            {"btType": "BTMParameterEnum-145", "parameterId": "pathType",
             "enumName": "PathType", "value": "PITCH"},
            {"btType": "BTMParameterEnum-145", "parameterId": "endType",
             "enumName": "EndType", "value": "HEIGHT"},
            {"btType": "BTMParameterQuantity-147", "parameterId": "height",
             "expression": f"{length_mm * 0.92} mm"},
            {"btType": "BTMParameterQuantity-147", "parameterId": "helicalPitch",
             "expression": f"{p_mm} mm"},
            {"btType": "BTMParameterQuantity-147", "parameterId": "startRadius",
             "expression": f"{diameter_mm / 2} mm"},
            {"btType": "BTMParameterEnum-145", "parameterId": "startType",
             "enumName": "StartType", "value": "START_ANGLE"},
            {"btType": "BTMParameterQuantity-147", "parameterId": "startAngle",
             "expression": "0 deg"},
            {"btType": "BTMParameterEnum-145", "parameterId": "handedness",
             "enumName": "Direction", "value": "CW"}]}}
    helix_id = (await onshape_post(_fs_path(did, wid, eid, "/features"), helix))["feature"]["featureId"]
    # 3) triangular thread profile on the Front plane, straddling the surface at the start
    inner, outer, h = R - p * 0.13, R + p * 0.40, p * 0.5
    pts = {"q1": (X + inner, Z), "q2": (X + outer, Z + h / 2), "q3": (X + inner, Z + h)}
    seg = [_line_seg("t0", "q1", "q2", pts), _line_seg("t1", "q2", "q3", pts),
           _line_seg("t2", "q3", "q1", pts)]
    psid = await _sketch_on(did, wid, eid, "Front", "AI Screw Thread Profile", seg)
    # 4) sweep the profile along the helix and merge it onto the shaft
    sweep = {"feature": {"btType": "BTMFeature-134", "featureType": "sweep",
        "name": "AI Screw Thread", "parameters": [
            {"btType": "BTMParameterEnum-145", "parameterId": "bodyType",
             "enumName": "ExtendedToolBodyType", "value": "SOLID"},
            {"btType": "BTMParameterEnum-145", "parameterId": "operationType",
             "enumName": "NewBodyOperationType", "value": "ADD"},
            {"btType": "BTMParameterQueryList-148", "parameterId": "profiles",
             "queries": [{"btType": "BTMIndividualQuery-138",
                "queryString": f'query=qSketchRegion(makeId("{psid}"));'}]},
            {"btType": "BTMParameterQueryList-148", "parameterId": "path",
             "queries": [{"btType": "BTMIndividualQuery-138",
                "queryString": f'query=qCreatedBy(makeId("{helix_id}"));'}]}]}}
    await onshape_post(_fs_path(did, wid, eid, "/features"), sweep)
    return (f"Built a threaded screw: {diameter_mm} mm dia x {length_mm} mm long, "
            f"{p_mm:.1f} mm thread pitch, at x={x_mm}, base z={z_mm}.")


async def act_build_tube(did, wid, eid, outer_diameter_mm, inner_diameter_mm,
                         height_mm, x_mm=0, y_mm=0, z_mm=0):
    """Build a tube / pipe / ring-wall: a cylinder with a concentric hole."""
    await act_build_cylinder(did, wid, eid, outer_diameter_mm, height_mm, x_mm, y_mm, z_mm)
    await act_cut_hole(did, wid, eid, inner_diameter_mm, x_mm, y_mm, 0)
    return (f"Built a tube: {outer_diameter_mm} mm outside / {inner_diameter_mm} mm "
            f"bore, {height_mm} mm tall, at x={x_mm}, y={y_mm}, base z={z_mm}.")


async def act_build_wedge(did, wid, eid, width_mm, depth_mm, height_mm,
                          x_mm=0, y_mm=0, z_mm=0):
    """Build a wedge / ramp / triangular prism (right-angle at the low corner)."""
    X, Z = x_mm / 1000.0, z_mm / 1000.0
    W, H = width_mm / 1000.0, height_mm / 1000.0
    pts = {"pA": (X, Z), "pB": (X + W, Z), "pC": (X, Z + H)}
    ents = [_line_seg("w0", "pA", "pB", pts), _line_seg("w1", "pB", "pC", pts),
            _line_seg("w2", "pC", "pA", pts)]
    sid = await _sketch_on(did, wid, eid, "Front", "AI Wedge Sketch", ents)
    await _extrude_sketch(did, wid, eid, sid, depth_mm, "AI Wedge Body", y_mm)
    return (f"Built a {width_mm}x{depth_mm}x{height_mm} mm wedge/ramp at "
            f"x={x_mm}, y={y_mm}, base z={z_mm}.")


def _circle_entity(x_m, y_m, r_m, eid_="circle1"):
    return {"btType": "BTMSketchCurve-4", "entityId": eid_,
        "geometry": {"btType": "BTCurveGeometryCircle-115", "radius": r_m,
            "xCenter": x_m, "yCenter": y_m, "xDir": 1.0, "yDir": 0.0, "clockwise": False},
        "centerId": eid_ + ".center"}


async def act_build_gear(did, wid, eid, pitch_diameter_mm, thickness_mm, num_teeth,
                         bore_diameter_mm=0, x_mm=0, y_mm=0, z_mm=0):
    """Build a spur-style gear: a disc with `num_teeth` teeth cut around the rim
    (all gaps cut in one pass) and an optional centre bore. Verified geometry."""
    N = max(4, int(num_teeth))
    R = pitch_diameter_mm / 2000.0
    X, Y = x_mm / 1000.0, y_mm / 1000.0
    # blank disc
    bsid = await _sketch_on(did, wid, eid, "Top", "AI Gear Blank",
                            [_circle_entity(X, Y, R)])
    await _extrude_sketch(did, wid, eid, bsid, thickness_mm, "AI Gear Body", z_mm)
    # tooth gaps: N small radial rectangles around the rim, cut in one go
    pitch = math.pi * (pitch_diameter_mm / 1000.0) / N   # arc length per tooth (m)
    wt = pitch * 0.22                                     # half gap width (tangential)
    rr = (pitch_diameter_mm / 1000.0) * 0.06             # half tooth depth (radial)
    rc = R - rr * 0.4
    ents = []
    for k in range(N):
        a = 2 * math.pi * k / N
        ux, uy = math.cos(a), math.sin(a)        # radial
        tx, ty = -math.sin(a), math.cos(a)       # tangential
        cx, cy = X + rc * ux, Y + rc * uy

        def corner(sgn_t, sgn_r):
            return (cx + sgn_t * wt * tx + sgn_r * rr * ux,
                    cy + sgn_t * wt * ty + sgn_r * rr * uy)
        names = {f"a{k}": corner(-1, -1), f"b{k}": corner(1, -1),
                 f"c{k}": corner(1, 1), f"d{k}": corner(-1, 1)}
        loop = [(f"a{k}", f"b{k}"), (f"b{k}", f"c{k}"),
                (f"c{k}", f"d{k}"), (f"d{k}", f"a{k}")]
        for j, (p1, p2) in enumerate(loop):
            ents.append(_line_seg(f"g{k}_{j}", p1, p2, names))
    tsid = await _sketch_on(did, wid, eid, "Top", "AI Gear Teeth", ents)
    await _cut_extrude(did, wid, eid, tsid, "AI Gear Cut", 0)
    if bore_diameter_mm and bore_diameter_mm > 0:
        csid = await _sketch_on(did, wid, eid, "Top", "AI Gear Bore",
                                [_circle_entity(X, Y, bore_diameter_mm / 2000.0)])
        await _cut_extrude(did, wid, eid, csid, "AI Gear Bore Cut", 0)
    bore = f" with a {bore_diameter_mm} mm bore" if bore_diameter_mm else ""
    return (f"Built a {num_teeth}-tooth gear, {pitch_diameter_mm} mm across, "
            f"{thickness_mm} mm thick{bore}, at x={x_mm}, y={y_mm}.")


async def act_build_polygon(did, wid, eid, num_sides, diameter_mm, height_mm,
                            x_mm=0, y_mm=0, z_mm=0):
    """Build a regular-polygon prism (hexagon=nut/bolt-head, triangle, pentagon...).
    diameter_mm is across the corners. Placed at x/y, resting at height z."""
    n = max(3, int(num_sides))
    R = diameter_mm / 2000.0
    X, Y = x_mm / 1000.0, y_mm / 1000.0
    pts = {f"p{k}": (X + R * math.cos(2 * math.pi * k / n),
                     Y + R * math.sin(2 * math.pi * k / n)) for k in range(n)}
    ents = [_line_seg(f"e{k}", f"p{k}", f"p{(k + 1) % n}", pts) for k in range(n)]
    sid = await _sketch_on(did, wid, eid, "Top", "AI Polygon Sketch", ents)
    await _extrude_sketch(did, wid, eid, sid, height_mm, "AI Polygon Body", z_mm)
    return (f"Built a {n}-sided prism, {diameter_mm} mm across, {height_mm} mm tall, "
            f"at x={x_mm}, y={y_mm}, base z={z_mm}.")


async def act_combine(did, wid, eid, operation="union"):
    """Merge/intersect all separate solid bodies into one. operation = union
    (glue together), subtract, or intersect. Verified: union of overlapping
    boxes leaves one merged solid."""
    op = {"union": "UNION", "subtract": "SUBTRACTION",
          "intersect": "INTERSECTION"}.get((operation or "union").lower(), "UNION")
    feat = {"feature": {
        "btType": "BTMFeature-134", "featureType": "booleanBodies", "name": "AI Combine",
        "parameters": [
            {"btType": "BTMParameterEnum-145", "parameterId": "operationType",
             "enumName": "BooleanOperationType", "value": op},
            {"btType": "BTMParameterQueryList-148", "parameterId": "tools",
             "queries": [{"btType": "BTMIndividualQuery-138",
                          "queryString": "query=qAllModifiableSolidBodies();"}]}]}}
    await onshape_post(_fs_path(did, wid, eid, "/features"), feat)
    return f"Combined all parts into one solid ({op.lower()})."


async def act_cut_hole(did, wid, eid, diameter_mm, x_mm=0, y_mm=0, depth_mm=0):
    """Drill a round hole at (x,y). depth_mm=0 goes all the way through; a
    positive depth makes a blind pocket (good for hollowing a cup/mug)."""
    X, Y = x_mm / 1000.0, y_mm / 1000.0
    circle = {"btType": "BTMSketchCurve-4", "entityId": "circle1",
        "geometry": {"btType": "BTCurveGeometryCircle-115", "radius": diameter_mm / 2000.0,
            "xCenter": X, "yCenter": Y, "xDir": 1.0, "yDir": 0.0, "clockwise": False},
        "centerId": "circle1.center"}
    sid = await _sketch_on(did, wid, eid, "Top", "AI Hole Sketch", [circle])
    await _cut_extrude(did, wid, eid, sid, "AI Hole", depth_mm)
    how = "through" if depth_mm <= 0 else f"{depth_mm} mm deep"
    return f"Cut a {diameter_mm} mm hole ({how}) at x={x_mm}, y={y_mm}."


async def act_cut_rectangle(did, wid, eid, width_mm, length_mm, x_mm=0, y_mm=0, depth_mm=0):
    """Cut a rectangular hole/slot/pocket at (x,y). depth_mm=0 = all the way
    through; positive = blind pocket of that depth."""
    X, Y = x_mm / 1000.0, y_mm / 1000.0
    w, l = width_mm / 2000.0, length_mm / 2000.0
    pts = {"pA": (X - w, Y - l), "pB": (X + w, Y - l),
           "pC": (X + w, Y + l), "pD": (X - w, Y + l)}
    ents = [_line_seg("lAB", "pA", "pB", pts), _line_seg("lBC", "pB", "pC", pts),
            _line_seg("lCD", "pC", "pD", pts), _line_seg("lDA", "pD", "pA", pts)]
    sid = await _sketch_on(did, wid, eid, "Top", "AI Cut Sketch", ents)
    await _cut_extrude(did, wid, eid, sid, "AI Cut", depth_mm)
    how = "through" if depth_mm <= 0 else f"{depth_mm} mm deep"
    return f"Cut a {width_mm}x{length_mm} mm rectangular hole ({how}) at x={x_mm}, y={y_mm}."


async def act_fillet_all_edges(did, wid, eid, radius_mm):
    """Round (fillet) every straight edge in the part studio."""
    feat = {"feature": {
        "btType": "BTMFeature-134", "featureType": "fillet", "name": "AI Fillet",
        "parameters": [
            {"btType": "BTMParameterQuantity-147", "parameterId": "radius",
             "expression": f"{radius_mm} mm"},
            {"btType": "BTMParameterQueryList-148", "parameterId": "entities",
             "queries": [{"btType": "BTMIndividualQuery-138",
                "queryString": 'query=qGeometry(qEverything(EntityType.EDGE), GeometryType.LINE);'}]}]}}
    await onshape_post(_fs_path(did, wid, eid, "/features"), feat)
    return f"Rounded all edges with a {radius_mm} mm fillet."


async def act_chamfer_all_edges(did, wid, eid, width_mm):
    """Cut a flat 45 deg chamfer on every straight edge. (chamferType enum is
    EQUAL_OFFSETS — confirmed from Onshape's own featurespecs.)"""
    feat = {"feature": {
        "btType": "BTMFeature-134", "featureType": "chamfer", "name": "AI Chamfer",
        "parameters": [
            {"btType": "BTMParameterEnum-145", "parameterId": "chamferType",
             "enumName": "ChamferType", "value": "EQUAL_OFFSETS"},
            {"btType": "BTMParameterQuantity-147", "parameterId": "width",
             "expression": f"{width_mm} mm"},
            {"btType": "BTMParameterQueryList-148", "parameterId": "entities",
             "queries": [{"btType": "BTMIndividualQuery-138",
                "queryString": 'query=qGeometry(qEverything(EntityType.EDGE), GeometryType.LINE);'}]}]}}
    await onshape_post(_fs_path(did, wid, eid, "/features"), feat)
    return f"Cut a {width_mm} mm chamfer on all edges."


async def act_edit_dimension(did, wid, eid, feature_name, parameter_id, new_value):
    """Change a feature's dimension, e.g. an extrude 'depth' to '40 mm'."""
    f = await _find_feature(did, wid, eid, feature_name)
    if not f:
        return f"No feature named '{feature_name}'."
    hit = False
    for p in f.get("parameters", []):
        if p.get("parameterId") == parameter_id:
            p["expression"] = str(new_value)
            hit = True
    if not hit:
        ids = ", ".join(p.get("parameterId", "?") for p in f.get("parameters", []))
        return f"'{feature_name}' has no parameter '{parameter_id}'. It has: {ids}"
    await _update_feature(did, wid, eid, f)
    return f"Set {feature_name}.{parameter_id} = {new_value}."


async def act_rename_feature(did, wid, eid, old_name, new_name):
    """Rename a feature in the tree."""
    f = await _find_feature(did, wid, eid, old_name)
    if not f:
        return f"No feature named '{old_name}'."
    f["name"] = new_name
    await _update_feature(did, wid, eid, f)
    return f"Renamed '{old_name}' to '{new_name}'."


async def act_set_suppressed(did, wid, eid, feature_name, suppressed):
    """Turn a feature off (suppress) or back on."""
    f = await _find_feature(did, wid, eid, feature_name)
    if not f:
        return f"No feature named '{feature_name}'."
    f["suppressed"] = bool(suppressed)
    await _update_feature(did, wid, eid, f)
    state = "suppressed (off)" if suppressed else "un-suppressed (on)"
    return f"'{feature_name}' is now {state}."


async def act_add_raw_feature(did, wid, eid, feature):
    """Escape hatch: add ANY Onshape feature from a raw feature object.
    `feature` is the BTM* dict (what goes under the "feature" key). This is how
    the AI reaches tools we don't have a dedicated helper for (revolve, sweep,
    loft, patterns, chamfer, holes, ...)."""
    import json as _json
    if isinstance(feature, str):
        feature = _json.loads(feature)
    # Allow the AI to pass either the bare feature or a {"feature": ...} wrapper.
    if "feature" in feature and "btType" not in feature:
        feature = feature["feature"]
    res = await onshape_post(_fs_path(did, wid, eid, "/features"), {"feature": feature})
    nm = res.get("feature", {}).get("name", feature.get("name", "feature"))
    return f"Added feature '{nm}'."


# Tools the AI is allowed to run (these map to the write actions above).
_XYZ = {
    "x_mm": {"type": "number", "description": "left/right centre position, default 0"},
    "y_mm": {"type": "number", "description": "front/back centre position, default 0"},
    "z_mm": {"type": "number", "description": "height of the base above the floor, default 0"},
}

GEMINI_TOOLS = [{
    "functionDeclarations": [
        {
            "name": "build_box",
            "description": "Create a rectangular box (in mm). Use x_mm/y_mm/z_mm to "
                           "place it so you can assemble several parts into one object "
                           "(e.g. stack with z_mm, line up with x_mm).",
            "parameters": {"type": "object", "properties": {
                "width_mm": {"type": "number"}, "depth_mm": {"type": "number"},
                "height_mm": {"type": "number"}, **_XYZ},
                "required": ["width_mm", "depth_mm", "height_mm"]},
        },
        {
            "name": "build_cylinder",
            "description": "Create a cylinder (mm), placed at x_mm/y_mm and resting at "
                           "height z_mm. Good for legs, posts, wheels, etc.",
            "parameters": {"type": "object", "properties": {
                "diameter_mm": {"type": "number"}, "height_mm": {"type": "number"}, **_XYZ},
                "required": ["diameter_mm", "height_mm"]},
        },
        {
            "name": "build_sphere",
            "description": "Create a sphere (mm). x_mm is left/right, z_mm is the centre "
                           "height (stack spheres by raising z_mm, e.g. a snowman).",
            "parameters": {"type": "object", "properties": {
                "diameter_mm": {"type": "number"},
                "x_mm": {"type": "number"}, "z_mm": {"type": "number"}},
                "required": ["diameter_mm"]},
        },
        {
            "name": "build_cone",
            "description": "Create a cone (mm) with the base flat and the apex pointing up. "
                           "x_mm is left/right, z_mm is the base height.",
            "parameters": {"type": "object", "properties": {
                "base_diameter_mm": {"type": "number"}, "height_mm": {"type": "number"},
                "x_mm": {"type": "number"}, "z_mm": {"type": "number"}},
                "required": ["base_diameter_mm", "height_mm"]},
        },
        {
            "name": "build_tube",
            "description": "Build a tube / pipe / ring-wall: a cylinder with a concentric "
                           "hole through it. Place with x/y/z.",
            "parameters": {"type": "object", "properties": {
                "outer_diameter_mm": {"type": "number"}, "inner_diameter_mm": {"type": "number"},
                "height_mm": {"type": "number"}, **_XYZ},
                "required": ["outer_diameter_mm", "inner_diameter_mm", "height_mm"]},
        },
        {
            "name": "build_wedge",
            "description": "Build a wedge / ramp / triangular prism (right angle at the low "
                           "corner; slopes up over its width). Good for ramps, roofs, supports.",
            "parameters": {"type": "object", "properties": {
                "width_mm": {"type": "number"}, "depth_mm": {"type": "number"},
                "height_mm": {"type": "number"}, **_XYZ},
                "required": ["width_mm", "depth_mm", "height_mm"]},
        },
        {
            "name": "build_torus",
            "description": "Build a torus (donut / ring / washer / tyre). ring_diameter_mm "
                           "is the overall ring size, tube_diameter_mm the thickness. "
                           "x_mm = left/right, z_mm = height.",
            "parameters": {"type": "object", "properties": {
                "ring_diameter_mm": {"type": "number"}, "tube_diameter_mm": {"type": "number"},
                "x_mm": {"type": "number"}, "z_mm": {"type": "number"}},
                "required": ["ring_diameter_mm", "tube_diameter_mm"]},
        },
        {
            "name": "build_polygon",
            "description": "Build a regular-polygon prism: num_sides=6 is a hexagon "
                           "(nut / bolt head), 3=triangle, 5=pentagon, etc. diameter_mm "
                           "is across the corners. Place with x/y/z.",
            "parameters": {"type": "object", "properties": {
                "num_sides": {"type": "integer"}, "diameter_mm": {"type": "number"},
                "height_mm": {"type": "number"}, **_XYZ},
                "required": ["num_sides", "diameter_mm", "height_mm"]},
        },
        {
            "name": "combine",
            "description": "Merge all the separate solid parts into ONE solid. operation: "
                           "'union' (glue together, default), 'subtract', or 'intersect'.",
            "parameters": {"type": "object", "properties": {
                "operation": {"type": "string", "enum": ["union", "subtract", "intersect"]}}},
        },
        {
            "name": "build_screw",
            "description": "Build a threaded screw / bolt shaft: a cylinder with a REAL "
                           "helical thread on it. diameter_mm + length_mm; pitch_mm is the "
                           "thread spacing (omit for auto). x_mm left/right, z_mm base height. "
                           "For a full bolt, add a build_polygon hex head on top (z = length).",
            "parameters": {"type": "object", "properties": {
                "diameter_mm": {"type": "number"}, "length_mm": {"type": "number"},
                "pitch_mm": {"type": "number"},
                "x_mm": {"type": "number"}, "z_mm": {"type": "number"}},
                "required": ["diameter_mm", "length_mm"]},
        },
        {
            "name": "build_gear",
            "description": "Build a spur gear: a toothed disc with an optional centre bore "
                           "(hole for an axle). pitch_diameter_mm is the overall size, "
                           "num_teeth the number of teeth. Place with x/y/z.",
            "parameters": {"type": "object", "properties": {
                "pitch_diameter_mm": {"type": "number"}, "thickness_mm": {"type": "number"},
                "num_teeth": {"type": "integer"},
                "bore_diameter_mm": {"type": "number", "description": "centre axle hole, 0 = none"},
                **_XYZ},
                "required": ["pitch_diameter_mm", "thickness_mm", "num_teeth"]},
        },
        {
            "name": "cut_hole",
            "description": "Drill a round hole through the model at x_mm/y_mm. depth_mm=0 "
                           "goes all the way through; a positive depth_mm makes a blind "
                           "pocket (use this to hollow out a cup/mug/box).",
            "parameters": {"type": "object", "properties": {
                "diameter_mm": {"type": "number"},
                "x_mm": {"type": "number"}, "y_mm": {"type": "number"},
                "depth_mm": {"type": "number", "description": "0 = through everything"}},
                "required": ["diameter_mm"]},
        },
        {
            "name": "cut_rectangle",
            "description": "Cut a rectangular hole / slot / pocket at x_mm/y_mm. depth_mm=0 "
                           "cuts through; positive makes a blind pocket.",
            "parameters": {"type": "object", "properties": {
                "width_mm": {"type": "number"}, "length_mm": {"type": "number"},
                "x_mm": {"type": "number"}, "y_mm": {"type": "number"},
                "depth_mm": {"type": "number", "description": "0 = through everything"}},
                "required": ["width_mm", "length_mm"]},
        },
        {
            "name": "fillet_all_edges",
            "description": "Round every straight edge of the model with the given radius (mm).",
            "parameters": {"type": "object", "properties": {
                "radius_mm": {"type": "number"}}, "required": ["radius_mm"]},
        },
        {
            "name": "chamfer_all_edges",
            "description": "Cut a flat 45-degree chamfer on every straight edge (width in mm).",
            "parameters": {"type": "object", "properties": {
                "width_mm": {"type": "number"}}, "required": ["width_mm"]},
        },
        {
            "name": "edit_dimension",
            "description": "Change one parameter of an existing feature, e.g. make an "
                           "extrude taller. Use the feature's exact name, the parameter id "
                           "(e.g. 'depth', 'radius', 'distance', 'angle'), and a value with "
                           "units like '40 mm'.",
            "parameters": {"type": "object", "properties": {
                "feature_name": {"type": "string"},
                "parameter_id": {"type": "string"},
                "new_value": {"type": "string", "description": "e.g. '40 mm'"}},
                "required": ["feature_name", "parameter_id", "new_value"]},
        },
        {
            "name": "rename_feature",
            "description": "Rename a feature in the tree.",
            "parameters": {"type": "object", "properties": {
                "old_name": {"type": "string"}, "new_name": {"type": "string"}},
                "required": ["old_name", "new_name"]},
        },
        {
            "name": "set_suppressed",
            "description": "Turn a feature off (suppress) or back on.",
            "parameters": {"type": "object", "properties": {
                "feature_name": {"type": "string"},
                "suppressed": {"type": "boolean", "description": "true=off, false=on"}},
                "required": ["feature_name", "suppressed"]},
        },
        {
            "name": "delete_feature",
            "description": "Delete a feature by name, or the most recent one if no name given.",
            "parameters": {"type": "object", "properties": {
                "feature_name": {"type": "string"}}},
        },
        {
            "name": "add_feature",
            "description": "ADVANCED escape hatch: add ANY Onshape feature by giving its raw "
                           "feature JSON (a BTMFeature-134 / BTMSketch-151 object). Use this "
                           "for features without a dedicated tool: revolve, sweep, loft, "
                           "chamfer, hole, linearPattern, circularPattern, mirror, etc. The "
                           "JSON goes under Onshape's 'feature' key: include btType, "
                           "featureType, name, and parameters. Coordinates are in METRES.",
            "parameters": {"type": "object", "properties": {
                "feature_json": {"type": "string",
                    "description": "the feature object as a JSON string"}},
                "required": ["feature_json"]},
        },
    ]
}]

# Same tools, in OpenAI/Groq shape (used when falling back to Groq).
GROQ_TOOLS = [{"type": "function", "function": fd}
              for fd in GEMINI_TOOLS[0]["functionDeclarations"]]


async def _run_tool(name, args, did, wid, eid):
    """Execute one AI-requested action and return a short result string."""
    try:
        if name == "build_box":
            return await act_build_box(did, wid, eid,
                float(args["width_mm"]), float(args["depth_mm"]), float(args["height_mm"]),
                float(args.get("x_mm", 0)), float(args.get("y_mm", 0)), float(args.get("z_mm", 0)))
        if name == "build_cylinder":
            return await act_build_cylinder(did, wid, eid,
                float(args["diameter_mm"]), float(args["height_mm"]),
                float(args.get("x_mm", 0)), float(args.get("y_mm", 0)), float(args.get("z_mm", 0)))
        if name == "build_sphere":
            return await act_build_sphere(did, wid, eid,
                float(args["diameter_mm"]),
                float(args.get("x_mm", 0)), float(args.get("z_mm", 0)))
        if name == "build_cone":
            return await act_build_cone(did, wid, eid,
                float(args["base_diameter_mm"]), float(args["height_mm"]),
                float(args.get("x_mm", 0)), float(args.get("z_mm", 0)))
        if name == "build_tube":
            return await act_build_tube(did, wid, eid,
                float(args["outer_diameter_mm"]), float(args["inner_diameter_mm"]),
                float(args["height_mm"]), float(args.get("x_mm", 0)),
                float(args.get("y_mm", 0)), float(args.get("z_mm", 0)))
        if name == "build_wedge":
            return await act_build_wedge(did, wid, eid,
                float(args["width_mm"]), float(args["depth_mm"]), float(args["height_mm"]),
                float(args.get("x_mm", 0)), float(args.get("y_mm", 0)), float(args.get("z_mm", 0)))
        if name == "build_torus":
            return await act_build_torus(did, wid, eid,
                float(args["ring_diameter_mm"]), float(args["tube_diameter_mm"]),
                float(args.get("x_mm", 0)), float(args.get("z_mm", 0)))
        if name == "build_polygon":
            return await act_build_polygon(did, wid, eid,
                int(args["num_sides"]), float(args["diameter_mm"]), float(args["height_mm"]),
                float(args.get("x_mm", 0)), float(args.get("y_mm", 0)), float(args.get("z_mm", 0)))
        if name == "combine":
            return await act_combine(did, wid, eid, args.get("operation", "union"))
        if name == "build_screw":
            return await act_build_screw(did, wid, eid,
                float(args["diameter_mm"]), float(args["length_mm"]),
                float(args.get("pitch_mm", 0)), float(args.get("x_mm", 0)),
                float(args.get("z_mm", 0)))
        if name == "build_gear":
            return await act_build_gear(did, wid, eid,
                float(args["pitch_diameter_mm"]), float(args["thickness_mm"]),
                int(args["num_teeth"]), float(args.get("bore_diameter_mm", 0)),
                float(args.get("x_mm", 0)), float(args.get("y_mm", 0)), float(args.get("z_mm", 0)))
        if name == "cut_hole":
            return await act_cut_hole(did, wid, eid, float(args["diameter_mm"]),
                float(args.get("x_mm", 0)), float(args.get("y_mm", 0)),
                float(args.get("depth_mm", 0)))
        if name == "cut_rectangle":
            return await act_cut_rectangle(did, wid, eid,
                float(args["width_mm"]), float(args["length_mm"]),
                float(args.get("x_mm", 0)), float(args.get("y_mm", 0)),
                float(args.get("depth_mm", 0)))
        if name == "fillet_all_edges":
            return await act_fillet_all_edges(did, wid, eid, float(args["radius_mm"]))
        if name == "chamfer_all_edges":
            return await act_chamfer_all_edges(did, wid, eid, float(args["width_mm"]))
        if name == "edit_dimension":
            return await act_edit_dimension(did, wid, eid,
                args["feature_name"], args["parameter_id"], args["new_value"])
        if name == "rename_feature":
            return await act_rename_feature(did, wid, eid,
                args["old_name"], args["new_name"])
        if name == "set_suppressed":
            return await act_set_suppressed(did, wid, eid,
                args["feature_name"], bool(args["suppressed"]))
        if name == "delete_feature":
            return await act_delete_feature(did, wid, eid, args.get("feature_name"))
        if name == "delete_last_feature":   # kept for backwards-compat
            return await act_delete_last_feature(did, wid, eid)
        if name == "add_feature":
            return await act_add_raw_feature(did, wid, eid, args["feature_json"])
        return f"Unknown action: {name}"
    except Exception as e:
        return f"Action '{name}' failed: {e}"


async def api_chat(request):
    """Chat with the model. The AI can both answer AND run build/edit actions."""
    if not token_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    if not GEMINI_API_KEY and not GROQ_API_KEY:
        return JSONResponse({"error": "No AI key set (need GEMINI_API_KEY or GROQ_API_KEY)."},
                            status_code=400)
    body = await request.json()
    user_msg = body.get("message", "")
    image = body.get("image")  # optional data-URL: "data:image/png;base64,...."
    did, wid, eid = body.get("documentId"), body.get("workspaceId"), body.get("elementId")

    context = await gather_context(did, wid, eid) if did and eid else "(no model open)"
    system = (
        "You are an autonomous CAD modeller working INSIDE Onshape. You both answer "
        "questions and BUILD things by calling tools.\n\n"
        "BUILD TOOLS (all sizes mm): build_box(width,depth,height, x,y,z), "
        "build_cylinder(diameter,height, x,y,z), build_sphere(diameter, x,z), "
        "build_cone(base_diameter,height, x,z), build_torus(ring_diameter,tube_diameter, "
        "x,z) [donut/ring/washer/tyre], build_tube(outer,inner,height, x,y,z) [pipe], "
        "build_wedge(width,depth,height, x,y,z) [ramp/roof], build_polygon(num_sides,diameter,height, "
        "x,y,z) [hexagon=nut/bolt-head], build_gear(pitch_diameter,thickness,num_teeth, "
        "bore_diameter, x,y,z), build_screw(diameter,length,pitch, x,z) [REAL threaded "
        "screw/bolt shaft; add a hex build_polygon on top for a bolt], "
        "fillet_all_edges, chamfer_all_edges, combine(operation) "
        "[merge parts into one solid].\n"
        "CUT TOOLS (remove material — for holes & hollowing): "
        "cut_hole(diameter, x,y, depth), cut_rectangle(width,length, x,y, depth). "
        "depth=0 cuts all the way through; a positive depth is a blind pocket. To make "
        "a MUG/CUP/BOWL: build a solid cylinder, then cut_hole a smaller diameter with a "
        "blind depth a bit less than the height so a floor is left.\n"
        "EDIT TOOLS: edit_dimension, rename_feature, set_suppressed, delete_feature.\n"
        "ESCAPE HATCH: add_feature(raw Onshape feature JSON) for anything else "
        "(sweep, loft, holes, patterns, mirror). Sketch coords in raw JSON are METRES.\n\n"
        "POSITIONING IS KEY — this is how you assemble a real object from parts:\n"
        "- x = left/right, y = front/back, z = HEIGHT of the part's base above the floor.\n"
        "- A part with z=0 sits on the floor. To STACK something on top of a part that "
        "is H mm tall, give the next part z=H. To put legs UNDER a tabletop at height "
        "H, the tabletop gets z=H and the legs are H tall at z=0.\n"
        "- Spread parts out with x/y (e.g. four table legs at the four corners).\n"
        "- Each build makes a separate solid; that's fine for multi-part objects.\n"
        "PATTERNS / REPEATS: there is no single 'pattern' tool — to repeat a part or a "
        "hole, call the tool once per copy at positions you compute. For a ring of N "
        "(a bolt-hole circle, fan blades): x = cx + r*cos(2*pi*k/N), y = cy + r*sin(2*pi*k/N) "
        "for k=0..N-1. For a row/grid: step x (and y) by a fixed spacing. Use combine at "
        "the end if you want it as one solid.\n\n"
        "HOW TO WORK:\n"
        "1. PLAN the WHOLE model first. If the user is vague ('make a chair', 'a house', "
        "'a rocket'), pick sensible real sizes yourself — never interrogate the user.\n"
        "2. Build a COMPLETE model, not the bare minimum. Include every obvious part: a "
        "chair = seat + backrest + 4 legs; a house = 4 walls + roof + door hole; a car = "
        "body + 4 wheels; a table = top + 4 legs. Aim for 4-12 parts for a real object.\n"
        "3. CRITICAL — emit ALL the build tool calls TOGETHER in your FIRST reply (you can "
        "call many functions at once in one message). Work out every part's size and "
        "x/y/z position up front, then fire them all in a single batch. Do NOT build one "
        "part per message — that is slow and hits rate limits. Only use extra messages if "
        "you must edit a part you already built.\n"
        "4. Optionally finish with fillet_all_edges or chamfer_all_edges.\n"
        "5. After the build calls run, give ONE short friendly summary with key sizes. "
        "Keep prose short — a student is reading.\n\n"
        "IF AN IMAGE IS ATTACHED: study it, identify the object, and RE-CREATE it as a "
        "simple 3D model out of the primitive tools (boxes, cylinders, spheres, cones) "
        "positioned/stacked to match the picture. Pick reasonable sizes. Don't ask "
        "questions — just build your best version.\n\n"
        f"Live data about the current part studio:\n{context}"
    )
    # Pick the brain. Gemini is primary (smartest, and the only one with vision).
    # Groq is a free fallback with much higher rate limits, so when Gemini is out
    # of quota the panel keeps working instead of failing.
    if image:
        return await _run_gemini(system, user_msg, image, did, wid, eid)
    providers = (["gemini"] if GEMINI_API_KEY else []) + (["groq"] if GROQ_API_KEY else [])
    if not providers:
        return JSONResponse({"error": "No AI key set."}, status_code=400)
    result = JSONResponse({"error": "The AI could not complete that."}, status_code=400)
    for prov in providers:
        result = await (_run_gemini(system, user_msg, None, did, wid, eid)
                        if prov == "gemini" else _run_groq(system, user_msg, did, wid, eid))
        if result.status_code == 200:
            return result  # success — stop here
        # otherwise fall through to the next provider (e.g. Gemini quota -> Groq)
    return result


def _partial_success(actions_done, last_err):
    """If tools ran but we never got a tidy summary, report the built work
    instead of hiding it behind an error."""
    if not actions_done:
        return None
    summary = "Done! Here's what I built:\n- " + "\n- ".join(actions_done[-10:])
    if last_err:
        summary += (f"\n\n(The model is built — I just couldn't write a tidy "
                    f"summary because the AI was {last_err}.)")
    return JSONResponse({"answer": summary})


async def _run_gemini(system, user_msg, image, did, wid, eid):
    """Gemini agent loop (function-calling + optional image vision)."""
    first_parts = [{"text": system + "\n\nUser: " + (user_msg or "Build this.")}]
    if image:
        if image.startswith("data:") and "," in image:
            head, b64 = image.split(",", 1)
            mime = head[5:].split(";")[0] or "image/png"
        else:
            b64, mime = image, "image/png"
        first_parts.append({"inlineData": {"mimeType": mime, "data": b64}})
    contents = [{"role": "user", "parts": first_parts}]

    last_err = None
    actions_done = []
    backoff = 2.0
    model_idx = 0   # which GEMINI_MODELS entry we're currently using
    for _ in range(25):
        try:
            async with httpx.AsyncClient(timeout=40.0) as c:
                r = await c.post(_gemini_url(GEMINI_MODELS[model_idx]),
                                 params={"key": GEMINI_API_KEY},
                                 json={"contents": contents, "tools": GEMINI_TOOLS})
            if r.status_code in (429, 500, 503):
                last_err = f"Gemini busy ({r.status_code})"
                # This model is out of quota: switch to the next model if any.
                if model_idx + 1 < len(GEMINI_MODELS):
                    model_idx += 1
                    continue
                # All models exhausted. Bail fast (so Groq can take over) unless
                # we've already built something — then wait it out.
                if not actions_done:
                    return JSONResponse({"error": last_err}, status_code=400)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 20.0)
                continue
            r.raise_for_status()
            parts = r.json()["candidates"][0]["content"].get("parts", [])
            backoff = 2.0
        except Exception as e:
            last_err = str(e)
            if not actions_done:
                return JSONResponse({"error": f"Gemini error: {last_err}"}, status_code=400)
            await asyncio.sleep(1.0)
            continue

        calls = [p["functionCall"] for p in parts if "functionCall" in p]
        if calls:
            contents.append({"role": "model", "parts": parts})
            for call in calls:
                result = await _run_tool(call["name"], call.get("args", {}), did, wid, eid)
                actions_done.append(result)
                contents.append({"role": "user", "parts": [{
                    "functionResponse": {"name": call["name"],
                                         "response": {"result": result}}}]})
            continue
        text = " ".join(p["text"] for p in parts if "text" in p).strip()
        if text:
            return JSONResponse({"answer": text})

    return _partial_success(actions_done, last_err) or \
        JSONResponse({"error": last_err or "The AI could not complete that."}, status_code=400)


async def _run_groq(system, user_msg, did, wid, eid):
    """Groq agent loop (OpenAI-style function-calling). Used as a fallback."""
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user_msg or "Build this."}]
    last_err = None
    actions_done = []
    backoff = 2.0
    for _ in range(25):
        try:
            async with httpx.AsyncClient(timeout=40.0) as c:
                r = await c.post(GROQ_URL,
                                 headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                                 json={"model": GROQ_MODEL, "messages": messages,
                                       "tools": GROQ_TOOLS, "tool_choice": "auto"})
            if r.status_code in (429, 500, 503):
                last_err = f"Groq busy ({r.status_code})"
                if not actions_done:
                    return JSONResponse({"error": last_err}, status_code=400)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 20.0)
                continue
            r.raise_for_status()
            msg = r.json()["choices"][0]["message"]
            backoff = 2.0
        except Exception as e:
            last_err = str(e)
            if not actions_done:
                return JSONResponse({"error": f"Groq error: {last_err}"}, status_code=400)
            await asyncio.sleep(1.0)
            continue

        tool_calls = msg.get("tool_calls")
        if tool_calls:
            messages.append(msg)  # assistant turn carrying the tool_calls
            for tc in tool_calls:
                fn = tc.get("function", {})
                try:
                    import json as _json
                    args = _json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                result = await _run_tool(fn.get("name"), args, did, wid, eid)
                actions_done.append(result)
                messages.append({"role": "tool", "tool_call_id": tc.get("id"),
                                 "content": result})
            continue
        text = (msg.get("content") or "").strip()
        if text:
            return JSONResponse({"answer": text})

    return _partial_success(actions_done, last_err) or \
        JSONResponse({"error": last_err or "The AI could not complete that."}, status_code=400)


# ===========================================================================
#  Standalone browser CAD (no Onshape): the AI writes JSCAD code, the page
#  renders it in-browser and exports STL. One LLM call per model.
# ===========================================================================

JSCAD_SYSTEM = (
    "You write 3D models as JavaScript for the JSCAD library (@jscad/modeling). "
    "Output ONLY a function and NOTHING else — no markdown, no backticks, no prose:\n\n"
    "function buildModel(jscad) {\n"
    "  const { primitives, booleans, transforms, extrusions, hulls } = jscad\n"
    "  // ...build the model...\n"
    "  return result   // a geometry or an array of geometries\n"
    "}\n\n"
    "API (all sizes in millimetres; ROTATIONS ARE IN RADIANS — use Math.PI):\n"
    "Destructure what you need: const { primitives, booleans, transforms, extrusions, hulls, expansions } = jscad\n"
    "3D PRIMITIVES: primitives.cuboid({ size:[w,d,h] }); roundedCuboid({ size:[w,d,h], roundRadius:2 }); "
    "cylinder({ radius, height, segments:64 }); roundedCylinder({ radius, height, roundRadius:1, segments:64 }); "
    "cylinderElliptic({ height, startRadius:[r,r], endRadius:[0,0], segments:64 }) (=cone/taper); "
    "sphere({ radius, segments:48 }); ellipsoid({ radius:[a,b,c] }); "
    "torus({ innerRadius, outerRadius, innerSegments:32, outerSegments:64 }) (=ring/donut).\n"
    "2D SHAPES (for custom profiles): primitives.rectangle({ size:[w,h] }); circle({ radius }); "
    "ellipse({ radius:[a,b] }); polygon({ points:[[x,y],[x,y],...] }); star({ vertices:5, outerRadius, innerRadius }).\n"
    "TURN 2D INTO 3D: extrusions.extrudeLinear({ height }, shape2d) = push a flat shape straight up. "
    "extrusions.extrudeRotate({ segments:64 }, shape2d) = SPIN a profile around the Y axis -> vases, bottles, "
    "bowls, wine glasses, lathe/turned parts (keep the profile on the +X side of x=0).\n"
    "COMBINE: booleans.union(a, b, ...); booleans.subtract(a, hole, ...); booleans.intersect(a, b). "
    "Merge a LIST with spread: booleans.union(...arr).\n"
    "MOVE: transforms.translate([x,y,z], g); rotate([rx,ry,rz], g); rotateX/rotateY/rotateZ(rad, g); "
    "scale([sx,sy,sz], g); mirror({ normal:[0,0,1] }, g).\n"
    "SMOOTH / ORGANIC: hulls.hull(a, b, ...) wraps a smooth skin around shapes (rounded blobs, smooth bridges); "
    "hulls.hullChain(a, b, c) makes a smooth tube through a sequence of shapes (great for handles). "
    "expansions.expand({ delta:2, corners:'round' }, g) grows + rounds edges (negative delta shrinks/insets).\n\n"
    "EXACT API — getting names wrong crashes it:\n"
    "- Booleans take SEPARATE arguments, NOT an array. There is NO top-level union/subtract. "
    "Correct: booleans.union(a, b, c); booleans.subtract(body, hole); booleans.union(...myArray).\n"
    "- Every shape is primitives.X; every move/rotate is transforms.X; extrudes are extrusions.X; "
    "smooth ops are hulls.X / expansions.X.\n"
    "RECIPES: vase/bottle/glass = extrudeRotate a polygon profile. Smooth handle = hullChain of small spheres. "
    "Rounded box = roundedCuboid (or expand). Gear = cylinder + tooth cuboids in a for-loop + centre hole.\n\n"
    "RULES:\n"
    "- Build a COMPLETE, recognisable model with sensible real-world sizes. Never the bare minimum.\n"
    "- Make HOLES with booleans.subtract. Repeat parts with for-loops + translate/rotate.\n"
    "- Centre the model near the origin; the floor is z=0 so things sit on the ground.\n"
    "- For a GEAR: a cylinder with teeth (small cuboids/cylinders) arranged in a ring via a for-loop, "
    "plus a centre hole. For a MUG: subtract a smaller cylinder from a bigger one, add a torus handle.\n"
    "- Return ONE geometry (union everything) or an array. Output ONLY the function code."
)


def _strip_code(text):
    """Remove markdown fences / stray prose around the AI's code."""
    t = (text or "").strip()
    if "```" in t:
        # take the content of the first fenced block
        parts = t.split("```")
        if len(parts) >= 3:
            block = parts[1]
            if block.lstrip().lower().startswith(("javascript", "js")):
                block = block.split("\n", 1)[1] if "\n" in block else ""
            t = block.strip()
    return t


async def _gemini_text(system, user, models, key):
    if not key:
        return None
    contents = [{"role": "user", "parts": [{"text": system + "\n\n" + user}]}]
    for model in models:
        try:
            async with httpx.AsyncClient(timeout=60.0) as c:
                r = await c.post(_gemini_url(model), params={"key": key},
                                 json={"contents": contents})
            if r.status_code in (429, 500, 503):
                continue
            r.raise_for_status()
            parts = r.json()["candidates"][0]["content"].get("parts", [])
            t = " ".join(p["text"] for p in parts if "text" in p).strip()
            if t:
                return t
        except Exception:
            continue
    return None


async def _openai_style_text(system, user, url, key, model):
    """Works for Groq AND a local Ollama server (both OpenAI-compatible)."""
    try:
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        async with httpx.AsyncClient(timeout=120.0) as c:
            r = await c.post(url, headers=headers, json={"model": model, "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user}]})
        r.raise_for_status()
        return (r.json()["choices"][0]["message"].get("content") or "").strip()
    except Exception:
        return None


async def _llm_text(system, user, provider="auto", model=None, api_key=None, ollama_url=None):
    """Generate text from the chosen provider. provider: auto|gemini|groq|ollama."""
    p = (provider or "auto").lower()
    if p == "gemini":
        return await _gemini_text(system, user, [model] if model else GEMINI_MODELS,
                                  api_key or GEMINI_API_KEY)
    if p == "groq":
        return await _openai_style_text(system, user, GROQ_URL,
                                        api_key or GROQ_API_KEY, model or GROQ_MODEL)
    if p in ("ollama", "local"):
        base = (ollama_url or "http://localhost:11434").rstrip("/")
        return await _openai_style_text(system, user, base + "/v1/chat/completions",
                                        None, model or "qwen2.5:7b")
    # auto: Gemini models, then Groq
    t = await _gemini_text(system, user, GEMINI_MODELS, GEMINI_API_KEY)
    if t:
        return t
    if GROQ_API_KEY:
        return await _openai_style_text(system, user, GROQ_URL, GROQ_API_KEY, GROQ_MODEL)
    return None


async def api_generate(request):
    """Turn a prompt (and optional failing code + error) into JSCAD code."""
    if not token_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    body = await request.json()
    prompt = (body.get("prompt") or "").strip()
    prev = body.get("code")
    err = body.get("error")
    # AI provider selection (from the page's settings panel).
    provider = (body.get("provider") or "auto").lower()
    model = (body.get("model") or "").strip() or None
    api_key = (body.get("apiKey") or "").strip() or None
    ollama_url = (body.get("ollamaUrl") or "").strip() or None
    # Need *some* way to reach an AI: a configured env key, a key from the UI,
    # or a local Ollama server.
    if provider == "auto" and not GEMINI_API_KEY and not GROQ_API_KEY and not api_key:
        return JSONResponse({"error": "No AI key set. Open ⚙️ settings and pick a "
                                      "provider / paste a key (or run Ollama locally)."},
                            status_code=400)
    if prev and err:
        user = (f"Your JSCAD code for \"{prompt}\" threw this error when run:\n{err}\n\n"
                f"Here is the code:\n{prev}\n\nFix it and output ONLY the corrected "
                f"buildModel function.")
    else:
        user = f"Make this: {prompt}"
    code = await _llm_text(JSCAD_SYSTEM, user, provider, model, api_key, ollama_url)
    if not code:
        return JSONResponse({"error": "The AI is busy / unreachable — try again or "
                                      "switch provider in ⚙️ settings."}, status_code=400)
    return JSONResponse({"code": _strip_code(code)})


CAD_HTML = r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI CAD — build 3D models</title>
<style>
  html,body{margin:0;height:100%;font-family:'Segoe UI',system-ui,sans-serif;background:#15171c;color:#eee}
  #app{display:flex;flex-direction:column;height:100%}
  header{padding:10px 14px;background:#0b5fff;color:#fff}
  header h1{margin:0;font-size:17px}
  header .s{font-size:12px;opacity:.85}
  #bar{display:flex;gap:8px;padding:10px 14px;background:#1e2129}
  #prompt{flex:1;padding:10px;border-radius:8px;border:1px solid #333;background:#23262e;color:#eee;font-size:14px}
  button{background:#0b5fff;color:#fff;border:0;border-radius:8px;padding:10px 16px;cursor:pointer;font-size:14px}
  button:disabled{opacity:.5;cursor:default}
  button.alt{background:#2a2f3a}
  #main{flex:1;position:relative;min-height:0}
  #view{width:100%;height:100%;display:block}
  #status{position:absolute;top:10px;left:14px;font-size:13px;background:#000a;padding:6px 10px;border-radius:6px}
  #foot{display:flex;gap:8px;padding:8px 14px;background:#1e2129;align-items:center}
  #code{flex:1;height:70px;font-family:Consolas,monospace;font-size:12px;background:#0e1014;color:#9fe;border:1px solid #333;border-radius:8px;padding:8px;display:none}
  .chip{font-size:12px;color:#9bf;cursor:pointer}
</style>
</head>
<body>
<div id="app">
  <header><h1>🧊 AI CAD</h1><div class="s">Type what to build → the AI models it → download an STL. No Onshape needed.</div></header>
  <div id="bar">
    <input id="prompt" placeholder="e.g. a gear with 12 teeth and a hole, or a coffee mug" onkeydown="if(event.key==='Enter')window.go()">
    <button class="alt" onclick="window.toggleSettings()" title="AI settings">⚙️</button>
    <button id="goBtn" onclick="window.go()">Build</button>
  </div>
  <div id="settings" style="display:none;padding:8px 14px;background:#191c22;font-size:13px;align-items:center;gap:8px;flex-wrap:wrap;display:none">
    <span style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
    <label>AI:
      <select id="provider" onchange="window.providerChanged()" style="background:#23262e;color:#eee;border:1px solid #333;border-radius:6px;padding:5px">
        <option value="auto">Auto (Gemini→Groq)</option>
        <option value="gemini">Gemini</option>
        <option value="groq">Groq</option>
        <option value="ollama">Local (Ollama)</option>
      </select>
    </label>
    <input id="model" placeholder="model (optional)" style="width:150px;padding:5px;border-radius:6px;border:1px solid #333;background:#23262e;color:#eee">
    <input id="apikey" type="password" placeholder="API key (optional)" style="width:200px;padding:5px;border-radius:6px;border:1px solid #333;background:#23262e;color:#eee">
    <input id="ollamaurl" placeholder="http://localhost:11434" style="display:none;width:180px;padding:5px;border-radius:6px;border:1px solid #333;background:#23262e;color:#eee">
    <button class="alt" onclick="window.saveSettings()">save</button>
    <span id="setmsg" style="color:#7d7"></span>
    </span>
  </div>
  <div id="main">
    <canvas id="view"></canvas>
    <div id="status">Type something and press Build.</div>
  </div>
  <div id="foot">
    <span class="chip" onclick="window.toggleCode()">show / edit code</span>
    <textarea id="code" spellcheck="false"></textarea>
    <button class="alt" onclick="window.rerender()">Re-render</button>
    <button id="dlBtn" onclick="window.download()" disabled>Download STL</button>
  </div>
</div>
<script>
// Classic script: catches module/CDN load failures (which the module below
// can't) and shows them, so problems are visible instead of a blank page.
window.addEventListener('error', function(e){
  var s=document.getElementById('status');
  if(s) s.textContent='⚠️ load error: '+(e.message||e.filename||'a library failed to load');
});
window.addEventListener('unhandledrejection', function(e){
  var s=document.getElementById('status');
  if(s) s.textContent='⚠️ '+((e.reason&&e.reason.message)||e.reason||'something failed');
});
</script>
<script type="module">
import * as THREE from 'https://cdn.jsdelivr.net/npm/three@0.160.0/+esm'
import { STLLoader } from 'https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/loaders/STLLoader.js/+esm'
import { OrbitControls } from 'https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/controls/OrbitControls.js/+esm'
import * as jscadNS from 'https://cdn.jsdelivr.net/npm/@jscad/modeling@2/+esm'
import * as stlNS from 'https://cdn.jsdelivr.net/npm/@jscad/stl-serializer@2/+esm'

// Normalise the CDN module shapes (named vs default export) so a packaging
// quirk can't break us.
const jscad = (jscadNS && jscadNS.primitives) ? jscadNS : (jscadNS.default || jscadNS);
const serialize = stlNS.serialize
  || (stlNS.default && (stlNS.default.serialize || stlNS.default));

const TOKEN = "__APP_TOKEN__";
const statusEl = document.getElementById('status');
const setStatus = t => statusEl.textContent = t;

// --- AI settings (saved in this browser) ---
function loadSettings(){
  let s = {}; try{ s = JSON.parse(localStorage.getItem('aicad_settings')||'{}'); }catch(e){}
  document.getElementById('provider').value = s.provider || 'auto';
  document.getElementById('model').value = s.model || '';
  document.getElementById('apikey').value = s.apiKey || '';
  document.getElementById('ollamaurl').value = s.ollamaUrl || '';
  providerChanged();
}
function getSettings(){
  return { provider:document.getElementById('provider').value,
           model:document.getElementById('model').value.trim(),
           apiKey:document.getElementById('apikey').value.trim(),
           ollamaUrl:document.getElementById('ollamaurl').value.trim() };
}
window.saveSettings = function(){
  localStorage.setItem('aicad_settings', JSON.stringify(getSettings()));
  const m=document.getElementById('setmsg'); m.textContent='saved ✓'; setTimeout(()=>m.textContent='',1500);
};
window.toggleSettings = function(){ const s=document.getElementById('settings'); s.style.display = s.style.display==='none'?'flex':'none'; };
window.providerChanged = function(){
  const p=document.getElementById('provider').value;
  document.getElementById('apikey').style.display = (p==='gemini'||p==='groq')?'inline-block':'none';
  document.getElementById('ollamaurl').style.display = (p==='ollama')?'inline-block':'none';
  const placeholders={auto:'model (optional)',gemini:'gemini-2.5-flash',groq:'llama-3.3-70b-versatile',ollama:'qwen2.5:7b'};
  document.getElementById('model').placeholder = placeholders[p]||'model';
};
loadSettings();

// --- three.js scene ---
const canvas = document.getElementById('view');
const renderer = new THREE.WebGLRenderer({canvas, antialias:true});
const scene = new THREE.Scene(); scene.background = new THREE.Color(0x15171c);
const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 100000);
camera.position.set(120,90,120);
const controls = new OrbitControls(camera, canvas); controls.enableDamping = true;
scene.add(new THREE.HemisphereLight(0xffffff, 0x334455, 1.1));
const dl = new THREE.DirectionalLight(0xffffff, 1.0); dl.position.set(1,1.5,1); scene.add(dl);
const grid = new THREE.GridHelper(400, 20, 0x335, 0x223); scene.add(grid);
let mesh = null, currentBlob = null;
function resize(){ const w=canvas.clientWidth,h=canvas.clientHeight; if(canvas.width!==w||canvas.height!==h){renderer.setSize(w,h,false);camera.aspect=w/h;camera.updateProjectionMatrix();} }
function loop(){ resize(); controls.update(); renderer.render(scene,camera); requestAnimationFrame(loop); } loop();

function showGeometry(geometry){
  if(mesh){ scene.remove(mesh); mesh.geometry.dispose(); }
  geometry.computeVertexNormals(); geometry.center();
  const mat = new THREE.MeshStandardMaterial({color:0x4da3ff, metalness:0.1, roughness:0.6, flatShading:false});
  mesh = new THREE.Mesh(geometry, mat); scene.add(mesh);
  geometry.computeBoundingSphere(); const r = geometry.boundingSphere.radius || 50;
  camera.position.set(r*2, r*1.6, r*2); controls.target.set(0,0,0); controls.update();
  grid.scale.setScalar(Math.max(1, r/40));
}

function runCode(code){
  // Execute the AI's JSCAD function and turn it into an STL + a Three mesh.
  const fn = new Function('jscad', code + '\nreturn buildModel(jscad);');
  let geom = fn(jscad);
  const geoms = Array.isArray(geom) ? geom : [geom];
  const raw = serialize({binary:true}, ...geoms);
  currentBlob = new Blob(raw, {type:'model/stl'});
  return currentBlob.arrayBuffer().then(ab => {
    const g = new STLLoader().parse(ab);
    showGeometry(g);
    document.getElementById('dlBtn').disabled = false;
  });
}

async function generate(prompt, fixCode, fixErr){
  const s = getSettings();
  const r = await fetch('/api/generate', {method:'POST',
    headers:{'Content-Type':'application/json','x-app-token':TOKEN},
    body: JSON.stringify({prompt, code:fixCode, error:fixErr,
      provider:s.provider, model:s.model, apiKey:s.apiKey, ollamaUrl:s.ollamaUrl})});
  const d = await r.json();
  if(!r.ok) throw new Error(d.error||('HTTP '+r.status));
  return d.code;
}

window.go = async function(){
  const prompt = document.getElementById('prompt').value.trim();
  if(!prompt) return;
  document.getElementById('goBtn').disabled = true;
  document.getElementById('dlBtn').disabled = true;
  try{
    setStatus('🤔 designing “'+prompt+'” …');
    let code = await generate(prompt);
    document.getElementById('code').value = code;
    for(let attempt=0; attempt<3; attempt++){
      try{ setStatus('🛠️ rendering …'); await runCode(code); setStatus('✅ done — drag to rotate, scroll to zoom'); return; }
      catch(err){
        if(attempt===2){ setStatus('⚠️ could not render: '+err.message); return; }
        setStatus('↻ fixing a glitch …');
        code = await generate(prompt, code, String(err.message||err));
        document.getElementById('code').value = code;
      }
    }
  }catch(e){ setStatus('⚠️ '+e.message); }
  finally{ document.getElementById('goBtn').disabled = false; }
};

window.rerender = async function(){
  try{ setStatus('🛠️ rendering …'); await runCode(document.getElementById('code').value); setStatus('✅ done'); }
  catch(e){ setStatus('⚠️ '+e.message); }
};
window.toggleCode = function(){ const c=document.getElementById('code'); c.style.display = c.style.display==='none'?'block':'none'; };
window.download = function(){ if(!currentBlob) return; const a=document.createElement('a'); a.href=URL.createObjectURL(currentBlob); a.download='model.stl'; a.click(); };
</script>
</body>
</html>
"""


async def cad_page(request):
    token = request.path_params.get("token") or request.query_params.get("token", "")
    if APP_TOKEN and token != APP_TOKEN:
        return HTMLResponse("<h3>Not authorized.</h3>", status_code=403)
    return HTMLResponse(CAD_HTML.replace("__APP_TOKEN__", token), headers=SECURITY_HEADERS_CAD)


app = Starlette(routes=[
    Route("/", panel),
    Route("/p/{token}", panel),
    Route("/api/parts", api_parts),
    Route("/api/mass", api_mass),
    Route("/api/features", api_features),
    Route("/api/details", api_details),
    Route("/api/chat", api_chat, methods=["POST"]),
    # Standalone browser CAD (no Onshape):
    Route("/cad", cad_page),
    Route("/cad/{token}", cad_page),
    Route("/api/generate", api_generate, methods=["POST"]),
])


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
