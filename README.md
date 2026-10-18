# Amoriva coin backend (IntaSend)

The only place coins are ever **added**. The app can spend coins but can't add them
(the database rules make `users/{uid}/coins` decrease-only). This service credits coins
after verifying a real payment (IntaSend — international cards **and** M-Pesa) or a real
rewarded ad (AdMob SSV).

## Endpoints
- `POST /coins/checkout` — app calls this (with the user's Firebase ID token). Creates an
  IntaSend hosted checkout and returns `{ url }`. The app redirects the user there to pay.
  **No coins are added here.**
- `POST /intasend/webhook` — IntaSend calls this when a payment completes. Credits coins
  only when the challenge matches and the state is `COMPLETE`. Idempotent (no double-credit).
- `GET /admob/ssv` — AdMob calls this after a rewarded ad is watched (Android app only).

## Step 1 — IntaSend account
1. Sign up at intasend.com and verify (ID / KRA PIN — quick for Kenyan businesses).
2. Stay in **Sandbox** while testing (toggle in the dashboard).
3. Dashboard → **API Keys / Settings**: copy your **Publishable key** (`ISPubKey_...`).

## Step 2 — Put this folder on GitHub
Unzip and push `app.py`, `requirements.txt`, `render.yaml`, `README.md` to a new repo.

## Step 3 — Firebase admin key
Firebase Console → `aviabustout` → gear → Project settings → **Service accounts** →
*Generate new private key*. Open the downloaded JSON, copy the whole thing (one value).

## Step 4 — Deploy on Render
Render → New → Web Service → pick the repo (it reads `render.yaml`). Set env vars:
- `FIREBASE_SERVICE_ACCOUNT` = the JSON from Step 3
- `INTASEND_PUBLISHABLE_KEY` = your `ISPubKey_...`
- `INTASEND_LIVE` = `false` (sandbox) for now
- `APP_URL` = `https://wazungukonect.vercel.app`
- `PRICE_AMOUNT` = `130` , `PRICE_CURRENCY` = `KES` (set your real price)
- `INTASEND_WEBHOOK_CHALLENGE` = leave blank until Step 5, then fill it
Deploy. Open the service URL → it should say "Amoriva coin service OK". Copy that URL.

## Step 5 — Create the IntaSend webhook
IntaSend dashboard → **Webhooks**:
- URL: `https://<your-service>.onrender.com/intasend/webhook`
- **Challenge:** type any secret word (e.g. a random string). Save it.
- Put that same word in Render → `INTASEND_WEBHOOK_CHALLENGE` → redeploy.
The challenge is how the backend confirms a webhook really came from IntaSend.

## Step 6 — Point the app at the backend
In `common.js`:
```js
const COINS = { ...,
  priceLabel: "KES 130",
  backendUrl: "https://<your-service>.onrender.com"
};
```
Push the website so Vercel redeploys.

## Step 7 — Test
Profile → Buy coins → you're sent to IntaSend's checkout. In sandbox, use IntaSend's test
card/M-Pesa numbers from their docs. After paying, your coin balance goes up by 30 within
a few seconds. When it all works, switch `INTASEND_LIVE` to `true` and use your live
publishable key + a live webhook.

## AdMob rewarded ads (Android app only)
Rewarded ads come from the AdMob SDK inside the B4A app, not the website. In the app:
1. Show a rewarded ad; set its **custom data (SSV)** to the user's `uid`.
2. AdMob console → set the **server-side verification URL** to
   `https://<your-service>.onrender.com/admob/ssv`.
3. Expose `window.AmorivaAds.showRewarded()` in the WebView so the web UI can trigger it.

## Data it writes (under /amoriva)
- `users/{uid}/coins`       — balance (incremented here, decremented by the app)
- `coinGrants/{refId}`      — one record per credit (prevents double-crediting)
- `pendingBuys/{api_ref}`   — in-flight purchases awaiting the IntaSend webhook

These have no client rules, so the browser can't touch them — only the Admin SDK can.
