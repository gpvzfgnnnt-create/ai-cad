# Putting the panel online permanently (free) + into Onshape

The panel currently runs on your laptop with a temporary address. These steps
move it to **free permanent hosting (Render.com)** so the address never changes,
then pin it into **Onshape**.

Your secret values (keep these safe — don't post them anywhere):

| Name | Value |
|------|-------|
| `ONSHAPE_ACCESS_KEY` | (your Onshape access key) |
| `ONSHAPE_SECRET_KEY` | (the long Onshape secret) |
| `GEMINI_API_KEY` | (your AIza… key) |
| `APP_TOKEN` | (any random string you pick) |

---

## Part A — Put the code on GitHub (free)

1. Make a free account at https://github.com
2. Click **New repository** → name it `onshape-panel` → **Private** → Create.
3. GitHub shows commands under "…or push an existing repository". From this
   folder run them (Claude can do this part once you're logged in), e.g.:
   ```
   git remote add origin https://github.com/YOURNAME/onshape-panel.git
   git branch -M main
   git push -u origin main
   ```

## Part B — Host it on Render (free, no card)

1. Make a free account at https://render.com (sign in with GitHub — easiest).
2. **New +** → **Web Service** → connect your `onshape-panel` repo.
3. Render auto-detects `render.yaml`. Confirm:
   - Build: `pip install -r requirements.txt`
   - Start: `uvicorn app:app --host 0.0.0.0 --port $PORT`
4. Under **Environment**, add the 4 secret values from the table above.
5. **Create Web Service**. After a minute you get a permanent URL like:
   `https://onshape-panel-xxxx.onrender.com`

## Part C — Pin it into Onshape

1. Go to https://dev-portal.onshape.com → **OAuth applications** →
   **Create new OAuth application**.
   - Name: `My Panel`
   - Redirect URL / OAuth URL: your Render URL
   - Permissions: **Read documents** + **Read profile**
2. Open the app → **Extensions** → **Add extension**:
   - Location: **Element right panel**
   - **Action URL** (note `{$...}` = brace-then-dollar; the workspace placeholder is
     `{$workspaceOrVersionId}` — there is NO `{$workspaceId}`):
     ```
     https://YOUR-APP.onrender.com/p/YOUR_APP_TOKEN?documentId={$documentId}&workspaceId={$workspaceOrVersionId}&elementId={$elementId}
     ```
3. Open a Part Studio in Onshape → click your panel's icon on the right edge. 🎉

**If the panel ever says "not connected":** copy the Onshape link from your
browser's address bar and paste it into the box the panel shows — it pulls the
ids out itself and remembers them, so it works even if the placeholders fail.

---

### Notes
- Render's **free** plan sleeps after 15 min idle; the first click after a nap
  takes ~30 seconds to wake, then it's fast.
- Secrets live only in Render's dashboard, never in the code.
- If a key ever leaks: regenerate it (Onshape dev portal / Google AI Studio) and
  update it in Render.
