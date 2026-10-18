"""
Amoriva coin backend — Flask on Render (IntaSend).

The ONLY place coins are ever added. The app can spend coins but can't add them
(the database rules make users/{uid}/coins decrease-only). This server credits coins
after verifying a real card/M-Pesa payment (IntaSend) or a rewarded ad (AdMob SSV).

IntaSend accepts international cards AND M-Pesa, and settles to a Kenyan account.

Environment variables (Render → Environment):
  FIREBASE_SERVICE_ACCOUNT   : full service-account JSON (one line)
  FIREBASE_DB_URL            : https://aviabustout-default-rtdb.firebaseio.com
  INTASEND_PUBLISHABLE_KEY   : ISPubKey_... (from IntaSend dashboard)
  INTASEND_WEBHOOK_CHALLENGE : the challenge word you set on the IntaSend webhook
  INTASEND_LIVE              : "false" for sandbox, "true" for live
  APP_URL                    : https://wazungukonect.vercel.app
  PRICE_AMOUNT               : 130        # price per coin pack
  PRICE_CURRENCY             : KES
  COINS_PER_PURCHASE         : 30
  COINS_PER_AD               : 10
  ADMOB_SSV_KEYS_URL         : https://www.gstatic.com/admob/reward/verifier-keys.json
"""

import os
import json
import time
import base64
import hashlib
from urllib.parse import parse_qsl

import requests
from flask import Flask, request, jsonify

import firebase_admin
from firebase_admin import credentials, auth as fb_auth, db as fb_db

# ---- Config -----------------------------------------------------------------
DB_URL            = os.environ["FIREBASE_DB_URL"]
APP_URL           = os.environ.get("APP_URL", "https://wazungukonect.vercel.app").rstrip("/")
PUB_KEY           = os.environ["INTASEND_PUBLISHABLE_KEY"]
WEBHOOK_CHALLENGE = os.environ.get("INTASEND_WEBHOOK_CHALLENGE", "")
LIVE              = os.environ.get("INTASEND_LIVE", "false").lower() == "true"
PRICE_AMOUNT      = float(os.environ.get("PRICE_AMOUNT", "130"))
PRICE_CURRENCY    = os.environ.get("PRICE_CURRENCY", "KES")
COINS_PER_PURCHASE = int(os.environ.get("COINS_PER_PURCHASE", "30"))
COINS_PER_AD       = int(os.environ.get("COINS_PER_AD", "10"))
ADMOB_KEYS_URL    = os.environ.get("ADMOB_SSV_KEYS_URL",
                                   "https://www.gstatic.com/admob/reward/verifier-keys.json")
ROOT              = "amoriva"

INTASEND_BASE = "https://payment.intasend.com" if LIVE else "https://sandbox.intasend.com"

cred = credentials.Certificate(json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT"]))
firebase_admin.initialize_app(cred, {"databaseURL": DB_URL})

app = Flask(__name__)


# ---- Helpers ----------------------------------------------------------------
def credit_coins(uid: str, amount: int, reason: str, ref_id: str) -> int:
    """Add coins atomically, idempotent per ref_id (a repeated webhook never double-credits)."""
    guard = fb_db.reference(f"{ROOT}/coinGrants/{ref_id}")
    if guard.get() is not None:
        return -1
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
    """App calls this with the user's Firebase ID token. Creates an IntaSend hosted
    checkout and returns its URL. No coins are added here — only on the webhook."""
    try:
        uid = verify_id_token(request)
    except Exception:
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    email = body.get("email") or f"{uid}@amoriva.user"   # IntaSend needs an email
    api_ref = f"buy_{uid}_{int(time.time())}"

    # Remember what this reference is worth, so the webhook credits the right amount.
    fb_db.reference(f"{ROOT}/pendingBuys/{api_ref}").set(
        {"uid": uid, "coins": COINS_PER_PURCHASE, "at": int(time.time() * 1000)}
    )

    payload = {
        "public_key": PUB_KEY,
        "amount": PRICE_AMOUNT,
        "currency": PRICE_CURRENCY,
        "email": email,
        "api_ref": api_ref,
        "comment": f"{COINS_PER_PURCHASE} Amoriva coins",
        "redirect_url": f"{APP_URL}/profile.html#wallet",
        "host": APP_URL,
    }
    try:
        r = requests.post(f"{INTASEND_BASE}/api/v1/checkout/", json=payload, timeout=30)
        r.raise_for_status()
        url = r.json().get("url")
        if not url:
            raise ValueError("no url in response")
        return jsonify({"url": url})
    except Exception as e:
        return jsonify({"error": "intasend_failed", "detail": str(e)}), 502


@app.post("/intasend/webhook")
def intasend_webhook():
    """IntaSend calls this when a payment's state changes. Credit coins only when the
    challenge matches and the state is COMPLETE."""
    data = request.get_json(silent=True) or {}

    # 1) Verify it's really from IntaSend using the challenge you configured.
    if not WEBHOOK_CHALLENGE or data.get("challenge") != WEBHOOK_CHALLENGE:
        return jsonify({"ok": False, "error": "bad_challenge"}), 403

    state = str(data.get("state", "")).upper()
    api_ref = data.get("api_ref")
    invoice_id = data.get("invoice_id") or data.get("id") or api_ref

    if state != "COMPLETE" or not api_ref:
        return jsonify({"ok": True})  # ignore non-success / incomplete events

    pending = fb_db.reference(f"{ROOT}/pendingBuys/{api_ref}").get()
    if not pending:
        return jsonify({"ok": False, "error": "unknown_reference"}), 404

    credit_coins(pending["uid"], int(pending["coins"]), "purchase", f"intasend_{invoice_id}")
    fb_db.reference(f"{ROOT}/pendingBuys/{api_ref}").delete()
    return jsonify({"ok": True})


@app.get("/admob/ssv")
def admob_ssv():
    """AdMob rewarded-ad server-side verification (Android app only).
    Set the ad's custom data to the user's uid; AdMob calls this after the ad is watched."""
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
        return False
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
