# 🧊 AI CAD

Type what you want in plain English — **"a gear with 12 teeth and a hole"**, **"a coffee mug"**, **"a phone stand"** — and an AI builds a real 3D model you can spin around and download as an **STL** for 3D printing. Runs in your browser, no CAD software needed.

There are **two ways to use it**:

| Mode | What it does | Needs |
|------|--------------|-------|
| **Standalone CAD** (`/cad`) | AI writes [JSCAD](https://jscad.app) code, renders it live with three.js, exports STL. One AI call per model. | Just a browser + a free AI key |
| **Onshape panel** (`/`) | An AI side-panel *inside* Onshape that builds real, editable Onshape features (box, cylinder, sphere, cone, torus, gear, polygon, holes, fillet, chamfer, combine…). | An Onshape account + API keys |

> Built by a Grade-8 student with the help of Claude. The standalone mode was *inspired by* the idea behind [CADAM](https://github.com/Adam-CAD/CADAM) (text → OpenSCAD), but the code here is original and uses JSCAD.

## Quick start (standalone CAD, on your own computer)

1. Install Python 3.12+ and the deps:
   ```
   pip install -r requirements.txt
   ```
2. Get a **free** API key from one of:
   - [Google AI Studio](https://aistudio.google.com/apikey) → `GEMINI_API_KEY`  (free, easiest)
   - [Groq](https://console.groq.com) → `GROQ_API_KEY`  (free, higher limits)
   - or run a local model with [Ollama](https://ollama.com) — no key needed
3. Set the key and run:
   ```
   # Windows (PowerShell)
   $env:GEMINI_API_KEY="your_key"; python app.py
   # macOS/Linux
   GEMINI_API_KEY=your_key python app.py
   ```
4. Open **http://localhost:8000/cad** in your browser, type something, hit **Build**.

The ⚙️ settings on the page let you switch between Gemini / Groq / local Ollama and paste a key right in the UI.

## How it works

```
your prompt ──▶ /api/generate ──▶ LLM writes JSCAD code ──▶ browser runs it
                                                              ├─ three.js preview
                                                              └─ STL download
```

- **Backend:** Python + [Starlette](https://www.starlette.io/) (`app.py`). Talks to Gemini, Groq, or a local Ollama server. One model = one AI call. If the rendered code errors, it feeds the error back to the AI to self-fix (up to 3×).
- **Frontend:** [@jscad/modeling](https://github.com/jscad/OpenJSCAD.org) for geometry, [@jscad/stl-serializer](https://www.npmjs.com/package/@jscad/stl-serializer) for export, [three.js](https://threejs.org) for the live 3D view — all loaded from a CDN, no build step.
- **AI providers:** `auto` (Gemini → Groq fallback), or pick one. Each Gemini model has its own free daily quota, so it cascades `flash → flash-lite → 2.0-flash` before falling back to Groq.

## Onshape mode (optional)

`app.py` also serves an Onshape "Element right panel" extension that builds real Onshape geometry via the REST API. See [`DEPLOY.md`](DEPLOY.md) for the Render + Onshape setup. (This mode needs Onshape API keys and is being phased out in favour of the standalone tool.)

## Security note

`APP_TOKEN` (optional) gates the API so only people with the token can use your AI key. Never commit real keys — they belong in environment variables. `run_local.bat` and `keys.env` are git-ignored for exactly this reason.

## License

[GPL-3.0](LICENSE) — free to use and modify; share-alike.
