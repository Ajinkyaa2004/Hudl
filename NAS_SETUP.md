# Running the PuckTrace backend on the home NAS (Synology DS223)

The site stays at **https://pucktrace.actorix.in**. Vercel serves the page and forwards searches:

```
browser ─► pucktrace.actorix.in (Vercel)
              /api        ─► home NAS (Tailscale Funnel)       main
              /api-backup ─► Render (pucktrace-api-8u5i)       automatic backup
```

When the NAS doesn't answer, the page switches to Render for 5 minutes and then tries the NAS again,
so nobody sees an error when the NAS is off.

## What runs where

| | NAS | Render (backup) |
|---|---|---|
| Image | `ghcr.io/ajinkyaa2004/pucktrace:latest` (`Dockerfile.nas`, built for ARM64 by `.github/workflows/image.yml`) | `Dockerfile` |
| Searches at once | 8 (`MAX_SEARCHES`) | 3 |
| Headless browser | off (`LIGHT_MODE=1`, 2 GB RAM) | off |
| Game list | published list, every 3 h, kept on disk | published list, every 3 h |
| Knowledge base, learning | kept on disk | lost on restart |

## Part 1: the NAS owner (about 20 minutes)

1. Set up DSM 7.2 or newer and create a storage volume on the 4 TB disk.
2. In **Package Center**, install **Container Manager** and **Tailscale**.
3. Create a free account at tailscale.com and sign in to the Tailscale package on the NAS.
4. Install Tailscale on the Mac with the same account.
5. Turn on SSH under **Control Panel > Terminal & SNMP > Enable SSH service**.
6. Optional: set a power schedule under **Control Panel > Hardware & Power > Power Schedule**, for example on at 08:30 and off at 21:00 on weekdays.
7. Send over the NAS's Tailscale name and the DSM user name. **Do not send the password.**

## Part 2: install (done from the Mac over Tailscale)

1. Make the GitHub package `pucktrace` public: GitHub > Packages > pucktrace > Package settings > Change visibility > Public. The image holds only the public code.
2. On the NAS, create the folder `/volume2/docker/pucktrace/data` (volume 2; volume 1 holds personal files and is never used).
3. Copy the private data from the Mac: `data/knowledge.json` and `data/aliases_learned.json`, plus `data/games.db` to save the first download.
4. In Container Manager, choose **Project > Create**, name it `pucktrace`, set the path to `/volume2/docker/pucktrace`, and use `deploy/docker-compose.nas.yml`. Set `APP_PASSWORD` and `APP_SECRET` to **the same values as on Render**, so one sign-in works on both servers.
5. Publish it: DSM Task Scheduler, triggered task "PuckTrace Funnel" (user root, event Boot-up) running `/var/packages/Tailscale/target/bin/tailscale funnel --bg 8000`. The tailnet policy needs `"nodeAttrs": [{"target": ["autogroup:member"], "attr": ["funnel"]}]`. Address: https://home-nas.tailfff30a.ts.net
6. In `vercel.json`, point `/api/:path*` at `https://<nas-name>.<tailnet>.ts.net/api/:path*`, keep `/api-backup` on Render, then commit.
7. Test:
   - `https://pucktrace.actorix.in/api/healthz` shows `knowledge_base: true` and the game list is loaded;
   - 20 searches at once all finish without errors;
   - with the container stopped, searches still work through Render.

## Updating

Pushing to `main` builds a new image. On the NAS, in Container Manager, open **Project > pucktrace**, choose **Action > Build**, or run `docker compose pull && docker compose up -d`.
