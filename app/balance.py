"""Balance / subscription quota fetchers, best-effort per provider type."""
import glob
import json
import os
import re
import subprocess
import time

import httpx

from . import db

CODEX_FALLBACK_MODELS = ["gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna",
                         "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]
_codex_version_cache = None


def codex_client_version():
    """Codex backend /models requires client_version; prefer the locally
    installed codex CLI's version, fall back to a known-good constant."""
    global _codex_version_cache
    if _codex_version_cache:
        return _codex_version_cache
    root = os.path.expandvars(r"%LOCALAPPDATA%\OpenAI\Codex\bin")
    for exe in sorted(glob.glob(root + r"\*\codex.exe"), reverse=True):
        try:
            out = subprocess.run([exe, "--version"], capture_output=True,
                                 text=True, timeout=10).stdout
            m = re.search(r"(\d+\.\d+\.\d+)", out)
            if m:
                _codex_version_cache = m.group(1)
                return _codex_version_cache
        except Exception:
            continue
    return "0.164.0"


def codex_headers(p):
    h = {"Authorization": f"Bearer {p['api_key']}",
         "OpenAI-Beta": "responses=experimental",
         "originator": "codex_cli_rs",
         "User-Agent": f"codex_cli_rs/{codex_client_version()}"}
    if p.get("account_id"):
        h["chatgpt-account-id"] = p["account_id"]
    h.update(p.get("extra_headers") or {})
    return h


async def fetch_balance(p):
    """Returns dict {text, ok, at}. text None means unsupported."""
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            bt = p.get("balance_type")
            if bt == "deepseek":
                base = p["base_url"].rstrip("/").removesuffix("/v1")
                r = await c.get(f"{base}/user/balance",
                                headers={"Authorization": f"Bearer {p['api_key']}"})
                d = r.json()
                infos = d.get("balance_infos") or []
                parts = [f"{i.get('total_balance', '?')} {i.get('currency', '')}".strip()
                         for i in infos]
                return {"ok": r.status_code < 400, "text": " · ".join(parts) or None}
            if bt == "openrouter":
                r = await c.get(f"{p['base_url'].rstrip('/')}/credits",
                                headers={"Authorization": f"Bearer {p['api_key']}"})
                d = (r.json().get("data") or {})
                total = float(d.get("total_credits") or 0)
                used = float(d.get("total_usage") or 0)
                return {"ok": r.status_code < 400, "text": f"${total - used:.2f} left"}
            if bt == "siliconflow":
                r = await c.get(f"{p['base_url'].rstrip('/')}/user/info",
                                headers={"Authorization": f"Bearer {p['api_key']}"})
                d = (r.json().get("data") or {})
                bal = d.get("balance")
                if bal is not None:
                    return {"ok": r.status_code < 400, "text": f"¥{float(bal):.2f}"}
                return {"ok": False, "text": None}
            if bt == "codex":
                # /usage endpoints are websocket-gated; use the usage captured
                # from real request response headers (x-codex-*) instead
                row = db.one("SELECT balance FROM providers WHERE id=?", (p["id"],))
                try:
                    saved = json.loads((row or {}).get("balance") or "null")
                except Exception:
                    saved = None
                if saved and saved.get("text"):
                    return saved
                return {"ok": True, "text": "发一次请求后可看周配额"}
            return {"ok": True, "text": None}
    except Exception:
        return {"ok": False, "text": None}


def save_balance(pid, res):
    db.execute("UPDATE providers SET balance=?, balance_at=? WHERE id=?",
               (json.dumps(res, ensure_ascii=False), time.time(), pid))
