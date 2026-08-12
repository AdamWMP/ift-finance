"""Passphrase gate with two access levels. Signed cookie; 30-day expiry.

  full     — the original passphrase: everything, including Dashboard View.
  accounts — IFT_FIN_ACCOUNTS_PASS (Shauna, 12 Aug 2026): debt collection,
             admin work, activity and transactions, but NO Dashboard View
             (revenue, targets, the book).
"""
from __future__ import annotations
import os
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from fastapi import Request

PASSPHRASE = os.environ.get("IFT_FIN_PASS", "newminds123")
ACCOUNTS_PASSPHRASE = os.environ.get("IFT_FIN_ACCOUNTS_PASS", "imageaccounts26")
SECRET     = os.environ.get("IFT_FIN_SECRET", "ift-finance-default-secret-rotate-me")
COOKIE     = "ift_fin_auth"
MAX_AGE    = 60 * 60 * 24 * 30  # 30 days

_signer = URLSafeTimedSerializer(SECRET, salt="ift-fin")

def make_token(role: str = "full") -> str:
    return _signer.dumps(role if role in ("full", "accounts") else "full")

def _payload(request: Request):
    tok = request.cookies.get(COOKIE)
    if not tok: return None
    try:
        return _signer.loads(tok, max_age=MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None

def is_authed(request: Request) -> bool:
    return _payload(request) is not None

def role_of(request: Request) -> str:
    """'accounts' only for accounts-issued cookies. Legacy tokens carried
    the literal "ok" — those stay full-access so nobody is locked out."""
    p = _payload(request)
    return "accounts" if p == "accounts" else "full"

def can_see_dashboard(request: Request) -> bool:
    return role_of(request) != "accounts"
