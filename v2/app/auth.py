"""Single-passphrase gate. Signed cookie; 30-day expiry."""
from __future__ import annotations
import os
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from fastapi import Request

PASSPHRASE = os.environ.get("IFT_FIN_PASS", "newminds123")
SECRET     = os.environ.get("IFT_FIN_SECRET", "ift-finance-default-secret-rotate-me")
COOKIE     = "ift_fin_auth"
MAX_AGE    = 60 * 60 * 24 * 30  # 30 days

_signer = URLSafeTimedSerializer(SECRET, salt="ift-fin")

def make_token() -> str:
    return _signer.dumps("ok")

def is_authed(request: Request) -> bool:
    tok = request.cookies.get(COOKIE)
    if not tok: return False
    try:
        _signer.loads(tok, max_age=MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False
