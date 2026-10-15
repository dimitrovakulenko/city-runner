"""Opaque analytics names and purpose-separated server credentials."""

import hashlib
import hmac
import re


def owner_key(account_id):
    return hashlib.sha256(account_id.encode()).hexdigest()


def role_name(owner):
    if re.fullmatch(r"[0-9a-f]{64}", owner or "") is None:
        raise ValueError("Invalid analytics owner.")
    return "cr_a_" + owner[:48]


def role_password(secret, owner):
    role_name(owner)
    return hmac.new(secret, f"analytics-role:{owner}".encode(), hashlib.sha256).hexdigest()


def cancel_role(database):
    return "cr_cancel_" + hashlib.sha256(database.encode()).hexdigest()[:24]


def cancel_password(secret, database):
    return hmac.new(secret, f"analytics-cancel:{database}".encode(), hashlib.sha256).hexdigest()
