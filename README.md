# LazyMirror 🪞
### On-demand offline web archiver for Windows 10/11

Browse any website normally — LazyMirror caches every page and resource
you visit. When the site updates later, just click the new pages and they
get added. No full re-scraping needed.

---

## Setup (do once, in order)

### 1. `SETUP.bat`
Installs Python packages and generates the CA certificate.
> Run as normal user (not required to be Administrator).

### 2. `install_cert.bat`
Installs the certificate so your browser trusts the proxy for HTTPS.
> Run as **Administrator** (right-click → Run as administrator).

### 3. `configure_proxy.bat` → press **[1]**
Routes Chrome/Edge through the proxy automatically.

> **Firefox users**: Also go to Firefox Settings → Network Settings →
> Manual Proxy → HTTP: `127.0.0.1` Port: `8080`

### 4. `START.bat`
Launches the proxy + dashboard. Dashboard opens in your browser.

### 5. Browse normally
Every page you visit gets saved to `offline_cache/`.
The dashboard at **http://127.0.0.1:7779** shows everything cached.

---

## Troubleshooting

**Run `DIAGNOSE.bat` first** — it prints everything needed to find the problem.

### "No internet" in browser after enabling proxy
The proxy isn't running yet, or failed to start.
1. Run `START.bat` first, then browse
2. Check the console window for any errors
3. Check `logs/proxy.log`

### `certs/` folder is empty after SETUP
The cert is generated when mitmdump first runs.
- Run `START.bat` once — certs are created in `certs/`
- Then run `install_cert.bat` again

### Certificate errors on HTTPS sites
- Run `install_cert.bat` as Administrator
- Restart Chrome/Edge completely after installing

### Firefox shows "Secure Connection Failed"
Firefox has its own cert store:
Settings → Privacy → Certificates → View Certificates → Authorities → Import
Select `certs\mitmproxy-ca-cert.cer` and trust it for websites.

### mitmdump not found
After running SETUP.bat, close and reopen the command prompt so
the updated PATH takes effect. Or just use `START.bat` which finds
it automatically.

---

## Files

```
lazy-mirror/
├── START.bat            ← Launch LazyMirror
├── SETUP.bat            ← First-time install
├── install_cert.bat     ← Install HTTPS certificate (run as Admin)
├── configure_proxy.bat  ← Set/unset Windows system proxy
├── DIAGNOSE.bat         ← Troubleshooting info
├── lazymirror.py        ← Main launcher (finds mitmdump, starts everything)
├── src/
│   ├── proxy_addon.py   ← mitmproxy caching logic
│   └── dashboard.py     ← Web dashboard (Flask)
├── offline_cache/       ← All cached content (one folder per domain)
│   └── _meta.json       ← Index of all cached URLs
├── certs/               ← Generated CA certificate
│   └── mitmproxy-ca-cert.cer
└── logs/
    ├── proxy.log
    └── dashboard.log
```

---

## How It Works

```
Your Browser
    ↓  (proxy: 127.0.0.1:8080)
LazyMirror Proxy (mitmdump + proxy_addon.py)
    ↓  intercepts every request/response
    ├── Saves response to offline_cache/<domain>/path/file
    └── Passes response through to browser unchanged

Next visit to same URL:
    → Served from offline_cache/ instantly (offline mode)
    → Or fetched fresh and re-cached (online mode)
```

**Online mode** (default): Pages load from internet, get saved as they go.  
**Offline mode**: All cached pages load from disk. Toggle in the dashboard.

---

## Privacy

- Everything runs 100% locally on your machine
- No data is sent anywhere
- The CA certificate is generated locally and unique to your install
- Cache files are plain files — inspect or delete anytime
