"""
Amoriva coin backend — Flask on Render.

The ONLY place coins are ever added. The web app can spend coins but can't add them
(the database rules make users/{uid}/coins decrease-only). This server credits coins
after verifying a real card payment (Stripe) or a real rewarded ad (AdMob SSV).

Environment variables (Render → Environment):
  FIREBASE_SERVICE_ACCOUNT : full service-account JSON (one line)
  FIREBASE_DB_URL          : https://aviabustout-default-rtdb.firebaseio.com
  STRIPE_SECRET_KEY        : sk_live_... (or sk_test_... while testing)
  STRIPE_WEBHOOK_SECRET    : whsec_...  (from the Stripe webhook you create)
  APP_URL                  : https://wazungukonect.vercel.app
  COIN_PRICE_USD_CENTS     : 100   # $1.00 per pack
  COINS_PER_PURCHASE       : 30
  COINS_PER_AD             : 10
  ADMOB_SSV_KEYS_URL       : https://www.gstatic.com/admob/reward/verifier-keys.json
"""

import os
import json
import time
import base64
import hashlib
from urllib.parse import parse_qsl

import requests
import stripe
from flask import Flask, request, jsonify, redirect

import firebase_admin
from firebase_admin import credentials, auth as fb_auth, db as fb_db

# ---- Config -----------------------------------------------------------------
DB_URL              = os.environ["FIREBASE_DB_URL"]
APP_URL             = os.environ.get("APP_URL", "https://wazungukonect.vercel.app").rstrip("/")
COIN_PRICE_CENTS    = int(os.environ.get("COIN_PRICE_USD_CENTS", "100"))
COINS_PER_PURCHASE  = int(os.environ.get("COINS_PER_PURCHASE", "30"))
COINS_PER_AD        = int(os.environ.get("COINS_PER_AD", "10"))
ADMOB_KEYS_URL      = os.environ.get("ADMOB_SSV_KEYS_URL",
                                     "https://www.gstatic.com/admob/reward/verifier-keys.json")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
ROOT                = "amoriva"

stripe.api_key = os.environ["STRIPE_SECRET_KEY"]

cred = credentials.Certificate(json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT"]))
firebase_admin.initialize_app(cred, {"databaseURL": DB_URL})

app = Flask(__name__)


# ---- Helpers ----------------------------------------------------------------
def credit_coins(uid: str, amount: int, reason: str, ref_id: str) -> int:
    """Add coins atomically. Idempotent per ref_id so a webhook that fires twice
    never double-credits."""
    guard = fb_db.reference(f"{ROOT}/coinGrants/{ref_id}")
    if guard.get() is not None:
        return -1  # already processed
    guard.set({"uid": uid, "amount": amount, "reason": reason, "at": int(time.time() * 1000)})
    coins_ref = fb_db.reference(f"{ROOT}/users/{uid}/coins")
    return coins_ref.transaction(lambda cur: (cur or 0) + amount)


def verify_id_token(req) -> str:
    header = req.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise ValueError("missing token")
    return fb_auth.verify_id_token(header.split(" ", 1)[1])["uid"]


# ---- Routes -----------------------------------------------------------------
@app.get("/")
def health():
    return "Amoriva coin service OK"


@app.post("/coins/checkout")
def coins_checkout():
    """App calls this with the user's Firebase ID token. Creates a Stripe Checkout
    session and returns its URL. No coins are added here — only on the webhook."""
    try:
        uid = verify_id_token(request)
    except Exception:
        return jsonify({"error": "unauthorized"}), 401

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            line_items=[{
                "quantity": 1,
                "price_data": {
                    "currency": "usd",
                    "unit_amount": COIN_PRICE_CENTS,
                    "product_data": {"name": f"{COINS_PER_PURCHASE} Amoriva coins"},
                },
            }],
            metadata={"uid": uid, "coins": str(COINS_PER_PURCHASE)},
            success_url=f"{APP_URL}/profile.html#wallet?paid=1",
            cancel_url=f"{APP_URL}/profile.html#wallet",
        )
        return jsonify({"url": session.url})
    except Exception as e:
        return jsonify({"error": "stripe_failed", "detail": str(e)}), 502


@app.post("/stripe/webhook")
def stripe_webhook():
    """Stripe calls this when a payment completes. Credit coins only on success."""
    payload = request.get_data()
    sig = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception:
        return "bad signature", 400

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        if session.get("payment_status") == "paid":
            uid = (session.get("metadata") or {}).get("uid")
            coins = int((session.get("metadata") or {}).get("coins", COINS_PER_PURCHASE))
            if uid:
                credit_coins(uid, coins, "purchase", f"stripe_{session['id']}")
    return jsonify({"ok": True})


@app.get("/admob/ssv")
def admob_ssv():
    """AdMob rewarded-ad server-side verification (Android app only).
    Set the ad's custom data to the user's uid; AdMob calls this after the ad is watched.
    Docs: https://developers.google.com/admob/android/rewarded-ssv"""
    args = request.args.to_dict()
    if not _verify_admob_signature(request.query_string.decode()):
        return "invalid signature", 403
    uid = args.get("custom_data") or args.get("user_id")
    tx = args.get("transaction_id")
    if not uid or not tx:
        return "missing fields", 400
    credit_coins(uid, COINS_PER_AD, "ad_reward", f"ad_{tx}")
    return "", 200


# ---- AdMob signature verification -------------------------------------------
_keys_cache = {"at": 0, "keys": {}}

def _admob_keys():
    if time.time() - _keys_cache["at"] < 3600 and _keys_cache["keys"]:
        return _keys_cache["keys"]
    r = requests.get(ADMOB_KEYS_URL, timeout=20)
    r.raise_for_status()
    keys = {str(k["keyId"]): k["pem"] for k in r.json().get("keys", [])}
    _keys_cache.update(at=time.time(), keys=keys)
    return keys

def _verify_admob_signature(query_string: str) -> bool:
    try:
        from ecdsa import VerifyingKey
        from ecdsa.util import sigdecode_der
    except Exception:
        return False  # fail closed — never credit an unverified ad
    try:
        idx = query_string.rfind("&signature=")
        if idx == -1:
            return False
        signed_portion = query_string[:idx]
        params = dict(parse_qsl(query_string))
        signature = base64.urlsafe_b64decode(params["signature"] + "==")
        pem = _admob_keys().get(params["key_id"])
        if not pem:
            return False
        vk = VerifyingKey.from_pem(pem)
        return vk.verify(signature, signed_portion.encode(),
                         hashfunc=hashlib.sha256, sigdecode=sigdecode_der)
    except Exception:
        return False


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
