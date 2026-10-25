"""Unified local keys: every client uses one sk-loc-* key against this gateway."""
import secrets
import time

from . import db


def mint(name):
    key = "sk-loc-" + secrets.token_urlsafe(24)
    kid = db.execute(
        "INSERT INTO keys (name,key,enabled,created_at) VALUES (?,?,1,?)",
        (name or "client", key, time.time()))
    return {"id": kid, "key": key}


def from_request(auth_header, api_key_header):
    token = None
    if auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
    token = token or api_key_header
    if not token:
        return None
    return db.one("SELECT * FROM keys WHERE key=? AND enabled=1", (token,))


def list_all():
    return db.query("SELECT * FROM keys ORDER BY id")


def reset_stats(kid):
    db.execute("UPDATE keys SET req_total=0, in_total=0, out_total=0, cost_usd=0, cost_cny=0 WHERE id=?", (kid,))


def rotate(kid):
    key = "sk-loc-" + secrets.token_urlsafe(24)
    db.execute("UPDATE keys SET key=?, last_used=0 WHERE id=?", (key, kid))
    return key


def set_enabled(kid, enabled):
    db.execute("UPDATE keys SET enabled=? WHERE id=?", (1 if enabled else 0, kid))


def delete(kid):
    db.execute("DELETE FROM keys WHERE id=?", (kid,))
