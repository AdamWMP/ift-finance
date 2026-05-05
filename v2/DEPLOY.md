# Deploying IFT Finance to finance.imageft.ie

End-to-end walk-through. After this, the dashboard is at `https://finance.imageft.ie`, the 10-min sync runs in the cloud (not your Mac), and a daily digest email lands in your inbox at 09:00 Mon–Fri.

Estimated total time: **~45 minutes** the first time.
Cost: **~$7/month** on Render's Starter plan (web + 1GB persistent disk).

## Pre-requisites

- A GitHub account
- A Render.com account (free to sign up; billing kicks in only when the service runs)
- Access to the `imageft.ie` DNS zone (Cloudflare, GoDaddy, wherever the domain lives)

## 1. Push the code to GitHub (~10 min)

```bash
cd "$HOME/Library/Mobile Documents/com~apple~CloudDocs/Image Fitness Training /Finance"
git init
git add .gitignore v2/
echo "*.db
.op_*.csv
.s26_*.json
sync.log
__pycache__/
*.pyc
.DS_Store
.venvs/
v2/app/ift_finance.db
v2/tabs/" > .gitignore
git add .gitignore v2/ && git commit -m "Initial commit of IFT Finance v2"
```

Then on github.com:
1. Create a new **private** repo named `ift-finance`
2. Don't initialise with README
3. Follow the "push existing repo" instructions GitHub shows you, e.g.:
   ```bash
   git remote add origin git@github.com:<your-username>/ift-finance.git
   git branch -M main
   git push -u origin main
   ```

## 2. Connect Render to the repo (~10 min)

1. Sign in at https://render.com
2. Top right → **New +** → **Blueprint**
3. Connect your GitHub account, pick `ift-finance` repo
4. Render reads `v2/render.yaml` and shows three services to provision:
   - `ift-finance` (web)
   - `ift-finance-sync` (cron, every 10 min)
   - `ift-finance-digest` (cron, weekdays 09:00)
5. Click **Apply**

## 3. Set the secret env vars (~5 min)

Render will prompt for the secrets that aren't auto-generated. Paste these values:

| Variable | Value |
|---|---|
| `IFT_FIN_PASS` | `newminds123` (or any new passphrase you want) |
| `OP_APP_ID` | `2_98540_7LPYP2Ces` |
| `OP_API_KEY` | `1l3In6M39GMuhtX` |
| `IFT_SALES_BOARD_URL` | the Apps Script web-app URL ending in `/exec` |
| `IFT_SALES_BOARD_TOKEN` | `newminds123` (same as the SECRET in your Apps Script) |
| `IFT_SMTP_PASS` | the Gmail app password from `~/.zshrc` |

Click **Save**. Render will start the first build (~5 min).

## 4. First deploy + initial data load

Once the web service is **Live** (green dot in Render):

1. Open the Render dashboard → `ift-finance-sync` cron → **Trigger run** (manual, just for the first time so the dashboard has data)
2. Wait ~3 minutes for it to finish (you can watch the logs)
3. Open the Render-issued URL (looks like `https://ift-finance.onrender.com`) — you should see the login page
4. Sign in with `IFT_FIN_PASS` → board view loads with all your data

## 5. Custom domain (~10 min)

In Render, on the `ift-finance` web service:

1. **Settings → Custom Domain** → add `finance.imageft.ie`
2. Render gives you a CNAME target like `ift-finance.onrender.com`
3. In your DNS zone for `imageft.ie`, add:
   ```
   Type:  CNAME
   Name:  finance
   Value: ift-finance.onrender.com
   TTL:   Auto
   ```
4. Wait 1–5 minutes for DNS to propagate
5. Render auto-provisions a Let's Encrypt TLS cert
6. Visit https://finance.imageft.ie — done

## 6. Send a test digest

To make sure the email pipe works:
- Render → `ift-finance-digest` cron → **Trigger run**
- Within 10 seconds you should receive an email at adam@imageft.ie titled `IFT Finance · ...`

If nothing arrives within a minute, check the cron's logs in Render — most likely `IFT_SMTP_PASS` is wrong.

## What's running where after this

| Where | What |
|---|---|
| Render web service | The dashboard (always on, behind passphrase) |
| Render sync cron | Every 10 min: refresh contacts, invoices, tags, sales board, push L18 |
| Render digest cron | Mon–Fri 09:00 UTC: build summary + email Adam |
| Your Mac | Nothing — local cron / local server can be stopped |
| ONtraport | Source of truth for contacts + invoices |
| Sales Board (Apps Script) | Source of truth for non-PT/Pilates revenue + L18 receives the live-money-in figure |

## Stopping the local cron

Once Render is live, kill the local sync cron so you don't double-run:
```bash
crontab -l | grep -v 'ift-finance' | crontab -
```

## Updating the dashboard later

```bash
cd "$HOME/Library/Mobile Documents/com~apple~CloudDocs/Image Fitness Training /Finance"
# make code changes
git add v2/ && git commit -m "what changed"
git push
```
Render auto-redeploys on every push to `main`.

## Troubleshooting

- **502/503 from Render** during first deploy — Docker build still running, wait 5 min.
- **"Disk full" later** — bump the volume size in `render.yaml` (`sizeGB`).
- **Sync doesn't appear to run** — Render → cron → logs. The schedule is in UTC; "every 10 minutes" should always fire.
- **Email digest is empty** — `IFT_SMTP_PASS` invalid, or Google revoked the app password. Regenerate at https://myaccount.google.com/apppasswords.
- **Dashboard shows old data** — click ↻ Refresh or trigger the sync cron manually.

## Rolling back

If a deploy breaks something:
1. Render → web service → **Deploys** → pick the last working build → **Redeploy**
2. Or revert the bad commit on GitHub: `git revert HEAD && git push`
