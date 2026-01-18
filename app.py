import os
import time
from typing import Dict, Tuple, Optional

from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv

import base58
from solana.rpc.api import Client
from solana.rpc.types import TxOpts
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import transfer, TransferParams
from solders.transaction import Transaction

load_dotenv()

# ====== ENV ======
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.devnet.solana.com")
REWARD_SOL_DEFAULT = float(os.getenv("REWARD_SOL_DEFAULT", "0.01"))
SECRET_B58 = os.getenv("REWARD_WALLET_SECRET_BASE58", "")

if not SECRET_B58:
    raise RuntimeError("Missing REWARD_WALLET_SECRET_BASE58 in env")

LAMPORTS_PER_SOL = 1_000_000_000

# ====== Solana client + signer ======
client = Client(SOLANA_RPC_URL)
secret_bytes = base58.b58decode(SECRET_B58)
payer = Keypair.from_bytes(secret_bytes)  # expects 64 bytes
payer_pubkey = payer.pubkey()

# ====== Flask app ======
app = Flask(__name__)

# Allow all CORS (no cookies)
CORS(app, resources={r"/*": {"origins": "*"}})

# ====== Idempotency (in-memory; replace with DB for prod) ======
PAID: Dict[Tuple[str, str], Tuple[str, float]] = {}
PAID_TTL_SEC = 60 * 60 * 24 * 7  # 7 days


def cleanup_paid():
    now = time.time()
    expired = [k for k, (_, ts) in PAID.items() if now - ts > PAID_TTL_SEC]
    for k in expired:
        PAID.pop(k, None)


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "rpc": SOLANA_RPC_URL,
        "from_wallet": str(payer_pubkey),
        "default_reward_sol": REWARD_SOL_DEFAULT,
    })


@app.post("/reward/send")
def reward_send():
    """
    Public endpoint:
      POST /reward/send
      JSON body:
        {
          "receiver_wallet_address": "<base58 pubkey>",
          "amount_sol": 0.01,                # optional
          "idempotency_key": "song_123"      # optional but strongly recommended
        }
    """
    cleanup_paid()

    body = request.get_json(silent=True) or {}
    receiver = body.get("receiver_wallet_address")
    amount_sol = body.get("amount_sol", REWARD_SOL_DEFAULT)
    idem_key = body.get("idempotency_key")  # recommended

    if not receiver:
        return jsonify({"ok": False, "error": "Missing receiver_wallet_address"}), 400

    # Validate amount
    try:
        amount_sol = float(amount_sol)
        if amount_sol <= 0 or amount_sol > 0.5:
            return jsonify({"ok": False, "error": "amount_sol out of allowed range"}), 400
    except Exception:
        return jsonify({"ok": False, "error": "Invalid amount_sol"}), 400

    # Validate receiver pubkey
    try:
        to_pubkey = Pubkey.from_string(receiver)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid receiver wallet address"}), 400

    # Idempotency: if key is provided, ensure one payout per (receiver, key)
    if idem_key:
        k = (receiver, str(idem_key))
        if k in PAID:
            sig, _ = PAID[k]
            return jsonify({
                "ok": True,
                "signature": sig,
                "already_paid": True,
                "from_wallet": str(payer_pubkey),
                "to_wallet": receiver,
                "amount_sol": amount_sol
            })

    lamports = int(amount_sol * LAMPORTS_PER_SOL)

    try:
        latest = client.get_latest_blockhash()
        blockhash = latest.value.blockhash

        ix = transfer(
            TransferParams(
                from_pubkey=payer_pubkey,
                to_pubkey=to_pubkey,
                lamports=lamports,
            )
        )

        tx = Transaction.new_signed_with_payer(
            [ix],
            payer_pubkey,
            [payer],
            blockhash,
        )

        resp = client.send_transaction(
            tx,
            opts=TxOpts(skip_preflight=False, preflight_commitment="confirmed"),
        )

        signature = resp.value
    except Exception as e:
        return jsonify({"ok": False, "error": f"Transfer failed: {str(e)}"}), 500

    if idem_key:
        PAID[(receiver, str(idem_key))] = (signature, time.time())

    return jsonify({
        "ok": True,
        "signature": signature,
        "from_wallet": str(payer_pubkey),
        "to_wallet": receiver,
        "amount_sol": amount_sol,
        "already_paid": False,
    })
