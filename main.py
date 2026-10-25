"""Local API Manager — aggregate every LLM provider behind one local port and one key.

OpenAI protocol:    http://127.0.0.1:8800/v1   (/v1/chat/completions, /v1/models)
Anthropic protocol: http://127.0.0.1:8800      (/v1/messages)
Console:            http://127.0.0.1:8800/console
"""
import json
import os
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from app import balance as balance_mod
from app import db, keys as keys_mod, presets, proxy, router

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"

app = FastAPI(title="Local API Manager", docs_url=None, redoc_url=None, openapi_url=None)

LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


@app.middleware("http")
async def local_guard(request: Request, call_next):
    host = (request.headers.get("host") or "").split(":")[0]
    if request.url.path.startswith("/api/") and host not in LOCAL_HOSTS:
        return JSONResponse({"error": "console api is local-only"}, status_code=403)
    origin = request.headers.get("origin")
    if (request.method in ("POST", "PATCH", "PUT", "DELETE")
            and request.url.path.startswith("/api/") and origin):
        allowed = (f"http://{request.headers.get('host')}", f"http://127.0.0.1:{request.url.port}",
                   f"http://localhost:{request.url.port}")
        if origin not in allowed:
            return JSONResponse({"error": "bad origin"}, status_code=403)
    return await call_next(request)


# ---------------- model endpoints ----------------

def _auth(request: Request):
    return keys_mod.from_request(request.headers.get("authorization"),
                                 request.headers.get("x-api-key"))


def _auth_error(proto):
    msg = {"type": "error",
           "error": {"type": "authentication_error", "message": "本地 Key 无效或缺失。"}}
    if proto == "openai":
        return JSONResponse({"error": {"message": "本地 Key 无效或缺失。", "type": "invalid_request_error",
                                       "code": "invalid_api_key"}}, status_code=401)
    return JSONResponse(msg, status_code=401)


@app.get("/v1/models")
async def v1_models(request: Request):
    key = _auth(request)
    if not key:
        return _auth_error("openai")
    data = [{k: v for k, v in m.items() if k != "upstream"} for m in router.list_local_models()]
    return {"object": "list", "data": data}


@app.get("/v1/models/{model}")
async def v1_model_detail(model: str, request: Request):
    key = _auth(request)
    if not key:
        return _auth_error("openai")
    for m in router.list_local_models():
        if m["id"] == model:
            return {k: v for k, v in m.items() if k != "upstream"}
    return JSONResponse({"error": {"message": f"model {model} not found",
                                   "type": "invalid_request_error"}}, status_code=404)


@app.post("/v1/chat/completions")
async def v1_chat(request: Request):
    key = _auth(request)
    if not key:
        return _auth_error("openai")
    try:
        body = await request.json()
    except Exception:
        return proxy.error_response("openai", 400, "请求体不是合法 JSON")
    return await proxy.proxy(proxy.CLIENT_OPENAI, body, key)


@app.post("/v1/messages")
async def v1_messages(request: Request):
    key = _auth(request)
    if not key:
        return _auth_error("anthropic")
    try:
        body = await request.json()
    except Exception:
        return proxy.error_response("anthropic", 400, "请求体不是合法 JSON")
    return await proxy.proxy(proxy.CLIENT_ANTHROPIC, body, key)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# ---------------- console api ----------------

def _provider_view(p):
    masked = db.mask_secret(p.get("api_key"))
    bal = ""
    try:
        b = json.loads(p.get("balance") or "null")
        if b:
            bal = b.get("text") or ("正常" if b.get("ok") else "失败")
    except Exception:
        pass
    return {"id": p["id"], "name": p["name"], "kind": p["kind"], "base_url": p["base_url"],
            "auth": p.get("auth"), "key_masked": masked, "has_key": bool(p.get("api_key")),
            "enabled": bool(p["enabled"]), "priority": p.get("priority", 100),
            "billing": p.get("billing"), "currency": p.get("currency"),
            "balance_type": p.get("balance_type"), "balance": bal,
            "models": p.get("models") or [], "models_error": p.get("models_error") or "",
            "models_at": p.get("models_at") or 0,
            "last_status": p.get("last_status") or "", "last_check_at": p.get("last_check_at") or 0,
            "refresh_token": bool(p.get("refresh_token"))}


def _overview():
    today = time.strftime("%Y-%m-%d")
    t = db.one("""SELECT COALESCE(SUM(req),0) req, COALESCE(SUM(in_tok),0) i,
                  COALESCE(SUM(out_tok),0) o, COALESCE(SUM(cost_usd),0) cu,
                  COALESCE(SUM(cost_cny),0) cc FROM daily WHERE day=?""", (today,)) or {}
    total = db.one("""SELECT COALESCE(SUM(req_total),0) req, COALESCE(SUM(in_total),0) i,
                      COALESCE(SUM(out_total),0) o, COALESCE(SUM(cost_usd),0) cu,
                      COALESCE(SUM(cost_cny),0) cc FROM keys""") or {}
    return {"today": t, "total": total,
            "providers_on": len([p for p in db.list_providers() if p["enabled"]]),
            "providers_all": len(db.list_providers()),
            "models": len(router.list_local_models()), "keys": len(keys_mod.list_all())}


@app.get("/api/state")
async def api_state():
    return {"overview": _overview(),
            "providers": [_provider_view(p) for p in db.list_providers()],
            "presets": presets.PRESETS,
            "keys": keys_mod.list_all(),
            "models": db.query("""SELECT m.*, (SELECT enabled FROM providers p
                                  WHERE p.id=m.provider_id) AS provider_on FROM models m
                                  ORDER BY m.local_name"""),
            "port": int(os.environ.get("APIPLATFORM_PORT", "8800"))}


@app.post("/api/providers/add")
async def api_provider_add(request: Request):
    import asyncio
    b = await request.json()
    preset = presets.BY_ID.get(b.get("preset_id", ""))
    if preset:
        p = dict(preset)
        p.update({k: (b[k] or "").strip() if isinstance(b[k], str) else b[k]
                  for k in ("api_key", "base_url", "name", "enabled")
                  if b.get(k) not in (None, "")})
        p.setdefault("enabled", 1)
        if b.get("id"):
            p["id"] = b["id"].strip()
    else:
        required = ("id", "name", "kind", "base_url")
        if not all(b.get(k) for k in required):
            return JSONResponse({"error": "缺少 id/name/kind/base_url"}, status_code=400)
        p = {"id": b["id"].strip(), "name": b["name"].strip(),
             "kind": b["kind"].strip(), "base_url": b["base_url"].strip(),
             "api_key": (b.get("api_key") or "").strip(), "auth": b.get("auth") or "bearer",
             "extra_headers": b.get("extra_headers") or {},
             "billing": b.get("billing") or "credit",
             "balance_type": b.get("balance_type") or "none",
             "currency": b.get("currency") or "USD",
             "chat_path": b.get("chat_path") or "", "priority": b.get("priority") or 100,
             "enabled": 1}
    db.upsert_provider(p)
    row = db.get_provider(p["id"])
    if b.get("discover", True) and row.get("api_key"):
        asyncio.get_event_loop().create_task(proxy.discover(row))
    return {"ok": True, "provider": _provider_view(db.get_provider(p["id"]))}


@app.post("/api/providers/codex/import")
async def api_codex_import():
    import pathlib
    auth_path = pathlib.Path.home() / ".codex" / "auth.json"
    try:
        d = json.loads(auth_path.read_text(encoding="utf-8"))
        tokens = d.get("tokens") or {}
        p = {"id": "codex", "name": "Codex 订阅", "kind": "codex",
             "base_url": "https://chatgpt.com/backend-api/codex",
             "api_key": tokens.get("access_token") or "",
             "refresh_token": tokens.get("refresh_token") or "",
             "account_id": d.get("tokens", {}).get("account_id") or "",
             "billing": "subscription", "balance_type": "codex", "enabled": 1, "priority": 30}
        db.upsert_provider(p)
        row = db.get_provider("codex")
        ids, err = await proxy.discover(row)
        return {"ok": True, "provider": _provider_view(db.get_provider("codex")),
                "models": ids, "error": err}
    except FileNotFoundError:
        return JSONResponse({"error": "未找到 ~/.codex/auth.json，请先登录 Codex CLI"},
                            status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=400)


@app.post("/api/providers/update")
async def api_provider_update(request: Request):
    b = await request.json()
    pid = b.get("id")
    row = db.get_provider(pid)
    if not row:
        return JSONResponse({"error": "no such provider"}, status_code=404)
    if "name" in b:
        db.execute("UPDATE providers SET name=? WHERE id=?", (b["name"].strip(), pid))
    if "base_url" in b and b["base_url"]:
        db.execute("UPDATE providers SET base_url=? WHERE id=?", (b["base_url"].strip(), pid))
    if "api_key" in b and b["api_key"]:
        db.execute("UPDATE providers SET api_key=? WHERE id=?", (b["api_key"].strip(), pid))
    if "auth" in b:
        db.execute("UPDATE providers SET auth=? WHERE id=?", (b["auth"], pid))
    if "priority" in b and b["priority"] is not None:
        db.execute("UPDATE providers SET priority=? WHERE id=?", (int(b["priority"]), pid))
    if "enabled" in b:
        db.execute("UPDATE providers SET enabled=? WHERE id=?", (1 if b["enabled"] else 0, pid))
    if "extra_headers" in b and isinstance(b["extra_headers"], dict):
        db.execute("UPDATE providers SET extra_headers=? WHERE id=?",
                   (json.dumps(b["extra_headers"], ensure_ascii=False), pid))
    if b.get("discover") and b.get("api_key"):
        asyncio.get_event_loop().create_task(proxy.discover(db.get_provider(pid)))
    return {"ok": True, "provider": _provider_view(db.get_provider(pid))}


@app.post("/api/providers/{pid}/delete")
async def api_provider_delete(pid: str):
    db.execute("DELETE FROM providers WHERE id=?", (pid,))
    db.execute("DELETE FROM models WHERE provider_id=?", (pid,))
    return {"ok": True}


@app.post("/api/providers/{pid}/discover")
async def api_provider_discover(pid: str):
    row = db.get_provider(pid)
    if not row:
        return JSONResponse({"error": "no such provider"}, status_code=404)
    ids, err = await proxy.discover(row)
    return {"ok": err == "", "models": ids, "error": err,
            "provider": _provider_view(db.get_provider(pid))}


@app.post("/api/providers/{pid}/test")
async def api_provider_test(pid: str):
    row = db.get_provider(pid)
    if not row:
        return JSONResponse({"error": "no such provider"}, status_code=404)
    res = await proxy.test_provider(row)
    bal = await balance_mod.fetch_balance(db.get_provider(pid))
    if bal.get("text"):
        balance_mod.save_balance(pid, bal)
    return {"test": res, "balance": bal,
            "provider": _provider_view(db.get_provider(pid))}


@app.post("/api/providers/{pid}/balance")
async def api_provider_balance(pid: str):
    row = db.get_provider(pid)
    if not row:
        return JSONResponse({"error": "no such provider"}, status_code=404)
    bal = await balance_mod.fetch_balance(row)
    balance_mod.save_balance(pid, bal)
    return {"balance": bal, "provider": _provider_view(db.get_provider(pid))}


@app.post("/api/models/add")
async def api_models_add(request: Request):
    b = await request.json()
    if not (b.get("local_name") and b.get("provider_id") and b.get("upstream_name")):
        return JSONResponse({"error": "缺少 local_name/provider_id/upstream_name"}, status_code=400)
    db.execute("""INSERT OR REPLACE INTO models (local_name,provider_id,upstream_name,enabled,source)
                  VALUES (?,?,?,1,'manual')""",
               (b["local_name"], b["provider_id"], b["upstream_name"]))
    return {"ok": True}


@app.post("/api/models/delete")
async def api_models_delete(request: Request):
    b = await request.json()
    db.execute("DELETE FROM models WHERE local_name=? AND provider_id=?",
               (b.get("local_name"), b.get("provider_id")))
    return {"ok": True}


@app.post("/api/models/toggle")
async def api_models_toggle(request: Request):
    b = await request.json()
    row = db.one("SELECT enabled FROM models WHERE local_name=? AND provider_id=?",
                 (b.get("local_name"), b.get("provider_id")))
    if row:
        db.execute("UPDATE models SET enabled=? WHERE local_name=? AND provider_id=?",
                   (0 if row["enabled"] else 1, b.get("local_name"), b.get("provider_id")))
    return {"ok": True}


@app.post("/api/keys/create")
async def api_keys_create(request: Request):
    b = await request.json()
    r = keys_mod.mint((b or {}).get("name") or "client")
    return {"ok": True, "id": r["id"], "key": r["key"]}


@app.post("/api/keys/{kid}/update")
async def api_keys_update(kid: int, request: Request):
    b = await request.json()
    if "name" in b:
        db.execute("UPDATE keys SET name=? WHERE id=?", (b["name"], kid))
    if "enabled" in b:
        keys_mod.set_enabled(kid, b["enabled"])
    return {"ok": True}


@app.post("/api/keys/{kid}/rotate")
async def api_keys_rotate(kid: int):
    key = keys_mod.rotate(kid)
    return {"ok": True, "key": key}


@app.post("/api/keys/{kid}/reset")
async def api_keys_reset(kid: int):
    keys_mod.reset_stats(kid)
    return {"ok": True}


@app.post("/api/keys/{kid}/delete")
async def api_keys_delete(kid: int):
    keys_mod.delete(kid)
    return {"ok": True}


@app.get("/api/logs")
async def api_logs(limit: int = 100, offset: int = 0):
    limit = max(1, min(limit, 1000))
    rows = db.query("SELECT * FROM logs ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))
    return {"logs": rows}


@app.get("/api/usage")
async def api_usage(days: int = 14):
    days = max(1, min(days, 90))
    since = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    by_day = db.query("""SELECT day, SUM(req) req, SUM(in_tok) i, SUM(out_tok) o,
                         SUM(cost_usd) cu, SUM(cost_cny) cc FROM daily WHERE day>=?
                         GROUP BY day ORDER BY day DESC""", (since,))
    by_provider = db.query("""SELECT provider_id, SUM(req) req, SUM(in_tok) i, SUM(out_tok) o,
                              SUM(cost_usd) cu, SUM(cost_cny) cc FROM daily WHERE day>=?
                              GROUP BY provider_id ORDER BY req DESC""", (since,))
    by_key = db.query("""SELECT COALESCE((SELECT name FROM keys WHERE id=daily.key_id),'—') AS key_name,
                         SUM(req) req, SUM(in_tok) i, SUM(out_tok) o,
                         SUM(cost_usd) cu, SUM(cost_cny) cc FROM daily WHERE day>=?
                         GROUP BY daily.key_id ORDER BY req DESC""", (since,))
    by_model = db.query("""SELECT model, SUM(req) req, SUM(in_tok) i, SUM(out_tok) o,
                           SUM(cost_usd) cu, SUM(cost_cny) cc FROM daily WHERE day>=?
                           GROUP BY model ORDER BY req DESC LIMIT 50""", (since,))
    return {"by_day": by_day, "by_provider": by_provider, "by_key": by_key, "by_model": by_model}


# ---------------- console static ----------------

@app.get("/console")
async def console():
    return FileResponse(STATIC / "index.html")


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/console/{path}")
async def console_assets(path: str):
    f = STATIC / path
    if f.exists() and f.parent == STATIC:
        return FileResponse(f)
    return Response(status_code=404)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("APIPLATFORM_PORT", "8800"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
