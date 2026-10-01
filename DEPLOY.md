# Deploying PuckTrace

PuckTrace has two parts:

- **Backend** (FastAPI, headless Chromium, the game list in `data/games.db`). It must stay on: it refreshes the game list every day and some searches take up to a minute. It runs from the `Dockerfile` on any host with a persistent disk. `render.yaml` sets it up on Render.
- **Frontend** (`frontend/index.html`). Vercel serves it and forwards every `/api/...` call to the backend (`vercel.json`), so the browser only ever talks to the Vercel address.

## 1. Backend on Render

1. In Render, choose **New > Blueprint**, pick the `Hudl` repo, and apply `render.yaml`. This creates `pucktrace-api` on the Starter plan with a 5 GB disk at `/app/data`.
2. Set **APP_PASSWORD** to the team password. APP_SECRET is generated for you.
3. Wait for the first deploy, then open `https://<service>.onrender.com/api/healthz`. It should show `{"ok": true}`.
4. On the first start the game list is empty. The daily refresh starts by itself and takes about 30 minutes. Until it finishes, searches use the live sources only.
5. Upload the knowledge base from your Mac. It is built from Club Data, so it is not in the public repo:

   ```bash
   cd "/Users/ajinkya/Documents/My Files/HUDL/rtt-finder"
   B=https://<service>.onrender.com
   curl -c /tmp/pt.txt -X POST $B/api/login -H 'content-type: application/json' -d '{"password":"<APP_PASSWORD>"}'
   curl -b /tmp/pt.txt -X POST "$B/api/admin/upload?name=knowledge.json" --data-binary @data/knowledge.json -H 'content-type: application/json'
   curl -b /tmp/pt.txt -X POST "$B/api/admin/upload?name=aliases_learned.json" --data-binary @data/aliases_learned.json -H 'content-type: application/json'
   ```

Railway or Fly.io work the same way: build the `Dockerfile`, mount a volume at `/app/data`, set `APP_PASSWORD`, and set `PORT` if the host requires it.

## 2. Frontend on Vercel

1. In `vercel.json`, replace `BACKEND_HOST` with the backend's host, for example `pucktrace-api.onrender.com`, then commit.
2. In Vercel, choose **Add New > Project**, import the `Hudl` repo, and keep the settings from `vercel.json` (no build step, output folder `frontend`). Then deploy.
3. Open the Vercel address and sign in with the team password.

## Running locally

No password is set locally, so the app opens directly:

```bash
.venv/bin/uvicorn backend.app:app --port 8765
```

To try the sign-in screen locally, start it with `APP_PASSWORD=something`.
