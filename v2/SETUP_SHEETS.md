# Sales Board → IFT Finance setup (Apps Script web app, ~5 min one time)

We can't use a service account because your Workspace org blocks service-account key creation. Apps Script is the cleaner alternative: the script runs as adam@imageft.ie, exposes the Sheet over a tokenised HTTPS endpoint, and the Python sync calls it.

No GCP project, no JSON keys, no org-policy fight.

## Steps (signed in as adam@imageft.ie)

### 1. Open the Apps Script editor for the Sales Board
1. Open the IFT Sales Board sheet.
2. Menu → **Extensions → Apps Script**. A new tab opens.

### 2. Paste the script
1. Delete any starter code in `Code.gs`.
2. Open `v2/sales_board.gs` from this repo, copy its full contents, paste into `Code.gs`.
3. **Change the `SECRET` constant** at the top to a random string. Anything works — long is good. Keep it secret. e.g.:
   ```js
   const SECRET = 'xkj38hd7-ift-finance-prod-2026';
   ```
4. Save (⌘S). Give the project a name when prompted (e.g. "IFT Finance reader").

### 3. Deploy as a web app
1. Top-right **Deploy → New deployment**.
2. Click the gear icon → **Web app**.
3. Description: `IFT Finance reader`.
4. **Execute as:** `Me (adam@imageft.ie)` — the script reads the sheet using your identity.
5. **Who has access:** `Anyone` — token in the URL is what gates access, not Google login.
6. Click **Deploy**.
7. Authorise when prompted (Google will warn that the script isn't verified — click **Advanced → Go to … (unsafe)** → **Allow**. Safe; it's your own script).
8. Copy the **Web app URL** (looks like `https://script.google.com/macros/s/AKfy.../exec`).

### 4. Save the URL and token to your environment
Add these two lines to `~/.zshrc`:

```bash
export IFT_SALES_BOARD_URL='<paste the web app URL>'
export IFT_SALES_BOARD_TOKEN='<the same SECRET you set in step 2>'
```

Then reload:
```bash
source ~/.zshrc
```

### 5. Test
```bash
cd "$HOME/Library/Mobile Documents/com~apple~CloudDocs/Image Fitness Training /Finance/v2"
~/.venvs/ift-finance/bin/python3 -m app.sheets
```
Expected: `sales board: ingested N S26 rows`. Done.

## Switching terms

When you start tracking A26, just keep working in the sheet — the script reads whatever tab name you ask for (`?period=A26`). No code changes.

## If a step doesn't work

- **`unauthorised`** → the token in your env doesn't match the SECRET in the script. Re-check both.
- **`tab_not_found`** → the tab inside the Sales Board needs to be named exactly `S26`.
- **HTML response instead of JSON** → the deployment authorisation is incomplete. Re-open the deployment URL in a browser; Google will prompt to authorise. Once you've clicked through, try the Python test again.
- **Permission to deploy denied** → your Workspace admin (you) may need to enable Apps Script web-app deployment in the Admin console under Apps → Google Workspace → Apps Script.
