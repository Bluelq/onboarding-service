# Deploying the onboarding service + wiring simplepresence.co.uk

Two separate hosts, on purpose:

| What | Host | Domain |
|---|---|---|
| Marketing site | **Vercel** | `simplepresence.co.uk` + `www` |
| **This onboarding service** (questionnaire / sign / pay) | **Render** | `get.simplepresence.co.uk` |

The onboarding service is **not** on Vercel because signed contracts live in SQLite —
Vercel's filesystem is ephemeral and would delete them. Render with a persistent disk
keeps them safe.

---

## Part A — Deploy the onboarding service to Render

1. **Put this folder on GitHub** (private repo recommended). From `onboarding_service/`:
   ```
   git init && git add . && git commit -m "Onboarding service"
   git branch -M main
   git remote add origin https://github.com/<you>/onboarding-service.git
   git push -u origin main
   ```
   (`git init` + first commit are already done for you — just add the remote and push.)

2. **Render → New + → Blueprint** → connect the repo. It reads `render.yaml`.
   - Plan is **Starter (~$7/mo)** — required for the persistent disk that retains
     signatures. (Free has no disk and wipes the DB on every redeploy — do not collect
     real signatures there.)

3. **Set the two secret env vars** in the Render dashboard (they're `sync: false`):
   - `ADMIN_PASSWORD` — your admin login for `/admin`
   - `ONBOARDING_API_KEY` — **must match** the `onboarding_api_key` saved in the CRM
     Settings (currently `simplepresence-meeting-key-7f3a` for local testing — pick a
     fresh strong value for production and update both sides).

4. Deploy. You get a URL like `https://onboarding-service.onrender.com`. Open
   `/admin` to confirm it's up. **This URL works immediately** — you can send clients
   links from it today, before the custom domain is ready.

5. **Point the CRM at it:** in the app's Settings (or `.settings.json`), set
   `onboarding_base_url = https://onboarding-service.onrender.com` and
   `onboarding_api_key = <the value from step 3>`. Now the Meeting tab mints public links.

---

## Part B — Custom domain `get.simplepresence.co.uk` → Render

1. Render → your service → **Settings → Custom Domains → Add** `get.simplepresence.co.uk`.
2. Render shows a **CNAME target** (e.g. `onboarding-service.onrender.com`).
3. At your **domain registrar's DNS**, add:
   ```
   Type: CNAME   Name: get   Value: <the onrender.com target Render shows>   TTL: auto
   ```
4. Propagation: usually minutes, up to a few hours. Render auto-issues HTTPS.

---

## Part C — `simplepresence.co.uk` (apex + www) → Vercel

1. Vercel → your marketing project → **Settings → Domains → Add** `simplepresence.co.uk`
   (and `www.simplepresence.co.uk`).
2. Vercel shows the records to add. Standard registrar-agnostic setup:
   ```
   Type: A      Name: @     Value: 76.76.21.21            (apex → Vercel)
   Type: CNAME  Name: www   Value: cname.vercel-dns.com   (www → Vercel)
   ```
   (Vercel may instead offer to use its nameservers — either works; the A + CNAME
   route lets you keep the `get` subdomain pointing at Render at the same registrar.)
3. Propagation: minutes to a few hours. Vercel auto-issues HTTPS.

---

## After everything is live
- Client link to send: `https://get.simplepresence.co.uk/onboard/<token>` (the Meeting
  tab generates these once `onboarding_base_url` points at the production URL).
- Keep `onboarding.db` backed up — it's your signed-contract record. (Render disk
  snapshots, or periodic download of the file.)
