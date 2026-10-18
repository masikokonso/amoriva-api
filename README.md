# Amoriva coin backend

The only place coins are ever **added**. The app can spend coins but can't add them
(the database rules make `users/{uid}/coins` decrease-only). This service credits coins
after verifying a real **card payment (Stripe)** or a real **rewarded ad (AdMob SSV)**.

## Endpoints
- `POST /coins/checkout` — app calls this (with the user's Firebase ID token). Creates a
  Stripe Checkout session and returns `{ url }`. The app redirects the user there to pay
  by card. **No coins are added here.**
- `POST /stripe/webhook` — Stripe calls this when payment completes. Credits coins only on
  a paid session. Idempotent (a repeated webhook never double-credits).
- `GET /admob/ssv` — AdMob calls this after a rewarded ad is watched (Android app only).
  Signature is verified against Google's public keys before crediting.

## Why Stripe (and a Play Store note)
Stripe Checkout takes cards worldwide, so this works globally, not just Kenya.
**Important:** if you publish the B4A app on the Google Play Store, Google requires
**Google Play Billing** for digital goods (coins) bought *inside the app* — using Stripe
cards inside the Play app breaks Play policy. So: Stripe for the website / PWA, and
Google Play Billing inside the Android app (it can credit coins by calling a similar
verify-and-credit endpoint here).

## Deploy on Render
1. Push this folder to a GitHub repo.
2. Render → New → Web Service → pick the repo (it reads `render.yaml`).
3. Set environment variables (Render → Environment):
   - `FIREBASE_SERVICE_ACCOUNT` — Firebase Console → Project settings → **Service accounts**
     → *Generate new private key*. Paste the whole JSON as one value.
   - `STRIPE_SECRET_KEY` — Stripe Dashboard → Developers → API keys (`sk_test_...` to test).
   - `APP_URL` — `https://wazungukonect.vercel.app`
   - `COIN_PRICE_USD_CENTS` — `100` for $1 (change to your price).
4. Deploy. Health check: open the service URL → "Amoriva coin service OK".

## Create the Stripe webhook
Stripe Dashboard → Developers → **Webhooks** → Add endpoint:
- URL: `https://<your-service>.onrender.com/stripe/webhook`
- Event: `checkout.session.completed`
- Copy the signing secret (`whsec_...`) into the `STRIPE_WEBHOOK_SECRET` env var and redeploy.

## Wire the app to it
In `common.js`:
```js
const COINS = { ...,
  backendUrl: "https://<your-service>.onrender.com"
};
```
The "Buy" button then redirects to Stripe Checkout; after payment the webhook credits the
coins and the user returns to the wallet.

## AdMob rewarded ads (Android app only)
Rewarded ads come from the AdMob SDK inside the B4A app, not the website. In the app:
1. Show a rewarded ad.
2. Set the ad's **custom data (SSV)** to the signed-in user's `uid`.
3. AdMob console → set the **server-side verification URL** to
   `https://<your-service>.onrender.com/admob/ssv`.
Expose `window.AmorivaAds.showRewarded()` in the WebView so the web UI can trigger it.

## Data it writes (under /amoriva)
- `users/{uid}/coins`   — balance (incremented here, decremented by the app)
- `coinGrants/{refId}`  — one record per credit (prevents double-crediting)

These have no client rules, so the browser can't touch them — only the Admin SDK can.
