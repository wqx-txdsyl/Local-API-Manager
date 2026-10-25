"""Request pipeline: auth -> route -> convert -> forward -> transcode -> record."""
import json
import time

import httpx
from fastapi.responses import JSONResponse, StreamingResponse

from . import balance as balance_mod
from . import db, presets, pricing, router
from .convert import requests as creq
from .convert import responses as cresp
from .convert import streams as cstreams

CLIENT_OPENAI = "openai"
CLIENT_ANTHROPIC = "anthropic"

client = httpx.AsyncClient(timeout=httpx.Timeout(connect=15, read=600, write=120, pool=30),
                           follow_redirects=True, trust_env=False)

ANTHROPIC_TYPE = {400: "invalid_request_error", 401: "authentication_error",
                  403: "permission_error", 404: "not_found_error", 413: "request_too_large",
                  429: "rate_limit_error"}


def anthropic_error_type(status):
    return ANTHROPIC_TYPE.get(status, "api_error")


def error_response(client_proto, status, message, err_type=None):
    if client_proto == CLIENT_ANTHROPIC:
        t = err_type or ANTHROPIC_TYPE.get(status, "api_error")
        return JSONResponse({"type": "error", "error": {"type": t, "message": message}},
                            status_code=status)
    return JSONResponse({"error": {"message": message, "type": err_type or "proxy_error",
                                   "code": status}}, status_code=status)


def _capture_codex_usage(provider, headers):
    """ChatGPT codex responses carry x-codex-* usage headers; persist for console."""
    pct = headers.get("x-codex-primary-used-percent")
    if pct is None:
        return
    plan = headers.get("x-codex-plan-type") or ""
    reset = headers.get("x-codex-primary-reset-at") or ""
    reset_dt = ""
    if reset.isdigit():
        reset_dt = time.strftime("%m-%d %H:%M", time.localtime(int(reset)))
    text = f"周配额 {float(pct):.0f}%"
    if reset_dt:
        text += f" · 重置 {reset_dt}"
    if plan:
        text = f"[{plan}] " + text
    balance_mod.save_balance(provider["id"], {"ok": True, "text": text, "at": time.time()})


def upstream_headers(p, accept_sse=False):
    h = {"Content-Type": "application/json",
         "Accept": "text/event-stream" if accept_sse else "application/json"}
    key = p.get("api_key") or ""
    if p["kind"] == "codex":
        h.update(balance_mod.codex_headers(p))
    elif p.get("auth") == "x-api-key" and key:
        h["x-api-key"] = key
    elif p.get("auth") == "none":
        pass
    elif key:
        h["Authorization"] = f"Bearer {key}"
    if p["kind"] == "anthropic":
        h["anthropic-version"] = "2023-06-01"
    h.update(p.get("extra_headers") or {})
    return h


async def refresh_codex(p):
    if not p.get("refresh_token"):
        return None
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post("https://auth.openai.com/oauth/token", json={
                "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
                "grant_type": "refresh_token",
                "refresh_token": p["refresh_token"]})
            d = r.json()
            if r.status_code < 400 and d.get("access_token"):
                db.execute(
                    "UPDATE providers SET api_key=?, refresh_token=COALESCE(?,refresh_token) WHERE id=?",
                    (d["access_token"], d.get("refresh_token"), p["id"]))
                p = db.get_provider(p["id"])
                return p
    except Exception:
        pass
    return None


def _cost(provider, model, in_tok, out_tok):
    cin, cout, cur = pricing.estimate(provider["id"], model, in_tok, out_tok,
                                      provider.get("billing") or "credit")
    cost = cin + cout
    return (cost, 0.0) if cur == "USD" else (0.0, cost)


def _upstream_error_body(resp):
    try:
        d = resp.json()
        e = d.get("error")
        if isinstance(e, dict):
            return str(e.get("message") or e)
        if isinstance(e, str):
            return e
        if d.get("type") == "error":
            return str((d.get("error") or {}).get("message") or d)
        return json.dumps(d, ensure_ascii=False)[:400]
    except Exception:
        return resp.text[:400] if hasattr(resp, "text") else ""


def _build_upstream(client_proto, body, provider, upstream_model):
    """Returns (url, upstream_body, parser_class)."""
    kind = provider["kind"]
    base = provider["base_url"].rstrip("/")
    if kind == "openai":
        ub = body if client_proto == CLIENT_OPENAI else creq.a2o_request(body)
        ub = dict(ub)
        ub["model"] = upstream_model
        if ub.get("stream") and "stream_options" not in ub:
            ub["stream_options"] = {"include_usage": True}
        url = base + (provider.get("chat_path") or "/chat/completions")
        return url, ub, cstreams.OpenAIStreamParser
    if kind == "anthropic":
        ub = body if client_proto == CLIENT_ANTHROPIC else creq.o2a_request(body)
        ub = dict(ub)
        ub["model"] = upstream_model
        return base + "/v1/messages", ub, cstreams.AnthropicStreamParser
    # codex / Responses
    oai = body if client_proto == CLIENT_OPENAI else creq.a2o_request(body)
    ub = creq.o2r_request(oai)
    ub["model"] = upstream_model
    ub["stream"] = True  # ChatGPT codex backend only accepts streamed requests
    return base + "/responses", ub, cstreams.ResponsesStreamParser


def _final_response(client_proto, provider, local_model, upstream_json):
    kind = provider["kind"]
    if kind == "openai":
        r = dict(upstream_json)
        if client_proto == CLIENT_OPENAI:
            r["model"] = local_model
            return r
        return cresp.o2a_response(r, local_model)
    if kind == "anthropic":
        if client_proto == CLIENT_ANTHROPIC:
            r = dict(upstream_json)
            r["model"] = local_model
            return r
        return cresp.a2o_response(upstream_json, local_model)
    oai = cresp.r2o_response(upstream_json, local_model)
    if client_proto == CLIENT_OPENAI:
        return oai
    return cresp.o2a_response(oai, local_model)


async def _aggregate_stream(resp, parser_cls, local_model):
    """Consume an upstream SSE stream into one OpenAI chat-shaped dict.
    Used when the upstream only streams (codex) but the client asked for JSON."""
    parser = parser_cls()
    text, tools = [], {}
    usage, finish = None, "stop"

    def consume(ev):
        nonlocal usage, finish
        t = ev["t"]
        if t == "text":
            text.append(ev["delta"])
        elif t == "tool_start":
            tools[ev["index"]] = {"id": ev["id"], "name": ev["name"], "args": ""}
        elif t == "tool_args":
            if ev["index"] in tools:
                tools[ev["index"]]["args"] += ev["delta"]
        elif t == "finish":
            usage = ev.get("usage")
            finish = ev["reason"]

    try:
        async for line in resp.aiter_lines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            for ev in parser.feed(obj):
                consume(ev)
        for ev in parser.flush():
            consume(ev)
    finally:
        await resp.aclose()

    msg = {"role": "assistant", "content": "".join(text) if text else None}
    if tools:
        msg["tool_calls"] = [{"id": t["id"], "type": "function",
                              "function": {"name": t["name"], "arguments": t["args"] or "{}"}}
                             for _, t in sorted(tools.items())]
    reason = "tool_calls" if tools else {"max_tokens": "length"}.get(finish, "stop")
    u = usage or {}
    return {
        "id": "chatcmpl-aggr-" + str(int(time.time())), "object": "chat.completion",
        "created": 0, "model": local_model,
        "choices": [{"index": 0, "message": msg, "finish_reason": reason}],
        "usage": {"prompt_tokens": u.get("in", 0), "completion_tokens": u.get("out", 0),
                  "total_tokens": u.get("in", 0) + u.get("out", 0)},
    }


def _pick_usage(usage, out_chars):
    if usage and (usage.get("in") or usage.get("out")):
        return usage.get("in", 0), usage.get("out", 0)
    return 0, (out_chars // 4 + 1)


async def proxy(client_proto, body, key_row):
    local_model = body.get("model") or ""
    t0 = time.time()
    try:
        provider, upstream = router.resolve(local_model)
    except KeyError as e:
        db.record_usage(key_row, "-", local_model, local_model, "none", 404,
                        int((time.time() - t0) * 1000), 0, 0, 0, 0, 0, str(e)[:200], client_proto)
        return error_response(client_proto, 404, str(e), "not_found_error")

    stream = bool(body.get("stream"))
    route_disp = f"{provider['id']}/{upstream}"
    url, ubody, parser_cls = _build_upstream(client_proto, body, provider, upstream)

    async def _send(p):
        req = client.build_request("POST", url, json=ubody,
                                   headers=upstream_headers(p, accept_sse=stream))
        return await client.send(req, stream=stream)

    try:
        resp = await _send(provider)
        if resp.status_code == 401 and provider["kind"] == "codex":
            await resp.aclose()
            p2 = await refresh_codex(provider)
            if p2:
                provider = p2
                resp = await _send(provider)
        if provider["kind"] == "codex":
            _capture_codex_usage(provider, resp.headers)
    except httpx.ConnectError as e:
        msg = f"连接不上 {provider['id']}: {e}"
        db.record_usage(key_row, provider["id"], local_model, route_disp, "connect", 502,
                        int((time.time() - t0) * 1000), 0, 0, 0, 0, stream, msg[:200], client_proto)
        return error_response(client_proto, 502, msg)
    except httpx.HTTPError as e:
        msg = f"上游请求失败: {e}"
        db.record_usage(key_row, provider["id"], local_model, route_disp, "network", 502,
                        int((time.time() - t0) * 1000), 0, 0, 0, 0, stream, msg[:200], client_proto)
        return error_response(client_proto, 502, msg)

    def _fail(status, message):
        db.record_usage(key_row, provider["id"], local_model, route_disp, "upstream", status,
                        int((time.time() - t0) * 1000), 0, 0, 0, 0, stream, message[:200],
                        client_proto)
        return error_response(client_proto, status if status >= 400 else 502, message)

    if stream:
        if resp.status_code >= 400:
            await resp.aread()
            msg = _upstream_error_body(resp)
            await resp.aclose()
            return _fail(resp.status_code, f"[{provider['id']}] {msg}")

        parser = parser_cls()
        writer = (cstreams.OpenAIChunkWriter(local_model) if client_proto == CLIENT_OPENAI
                  else cstreams.AnthropicEventWriter(local_model))

        async def gen():
            status = 200
            err = ""
            try:
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line or line.startswith(":") or line.startswith("event:"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        obj = json.loads(payload)
                    except Exception:
                        continue
                    for ev in parser.feed(obj):
                        if ev["t"] == "finish" and ev.get("error"):
                            err = ev["error"]
                        for chunk in writer.feed(ev):
                            yield cstreams.sse(chunk)
                for ev in parser.flush():
                    for chunk in writer.feed(ev):
                        yield cstreams.sse(chunk)
                if client_proto == CLIENT_OPENAI:
                    yield b"data: [DONE]\n\n"
            except Exception as e:
                status = 502
                err = str(e)[:200]
            finally:
                await resp.aclose()
                in_tok, out_tok = _pick_usage(parser.usage, parser.out_chars + writer.out_chars)
                cu, cc = _cost(provider, upstream, in_tok, out_tok)
                db.record_usage(key_row, provider["id"], local_model, route_disp, "upstream",
                                status, int((time.time() - t0) * 1000), in_tok, out_tok,
                                cu, cc, True, err, client_proto)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # non-stream
    try:
        if resp.status_code >= 400:
            return _fail(resp.status_code, f"[{provider['id']}] {_upstream_error_body(resp)}")
        if provider["kind"] == "codex":
            # upstream forced to stream; aggregate into a JSON response
            data = await _aggregate_stream(resp, parser_cls, local_model)
            if client_proto == CLIENT_OPENAI:
                out = data
            else:
                out = cresp.o2a_response(data, local_model)
            u = data.get("usage") or {}
            cu, cc = _cost(provider, upstream, u.get("prompt_tokens", 0),
                           u.get("completion_tokens", 0))
            db.record_usage(key_row, provider["id"], local_model, route_disp, "upstream", 200,
                            int((time.time() - t0) * 1000), u.get("prompt_tokens", 0),
                            u.get("completion_tokens", 0), cu, cc, False, "", client_proto)
            return JSONResponse(out)
        data = resp.json()
    except Exception as e:
        return _fail(502, f"[{provider['id']}] {e}")
    u = data.get("usage") or {}
    if provider["kind"] == "openai":
        in_tok, out_tok = u.get("prompt_tokens", 0), u.get("completion_tokens", 0)
    elif provider["kind"] == "anthropic":
        in_tok, out_tok = u.get("input_tokens", 0), u.get("output_tokens", 0)
    else:
        in_tok, out_tok = u.get("input_tokens", 0), u.get("output_tokens", 0)
    cu, cc = _cost(provider, upstream, in_tok, out_tok)
    db.record_usage(key_row, provider["id"], local_model, route_disp, "upstream", 200,
                    int((time.time() - t0) * 1000), in_tok, out_tok, cu, cc, False, "",
                    client_proto)
    out = _final_response(client_proto, provider, local_model, data)
    return JSONResponse(out)


async def passthrough(provider, path, body):
    """Raw passthrough for OpenAI-style aux endpoints (embeddings, completions)."""
    url = provider["base_url"].rstrip("/") + path
    try:
        r = await client.post(url, json=body, headers=upstream_headers(provider))
        return JSONResponse(r.json(), status_code=r.status_code)
    except Exception as e:
        return error_response(CLIENT_OPENAI, 502, f"[{provider['id']}] {e}")


async def discover(provider):
    """Fetch the provider's model list; store cache + route rows."""
    base = provider["base_url"].rstrip("/")

    async def _fallback(reason):
        fb = (balance_mod.CODEX_FALLBACK_MODELS if provider["kind"] == "codex"
              else presets.BY_ID.get(provider["id"], {}).get("fallback_models"))
        if fb:
            db.execute("UPDATE providers SET models=?, models_at=?, models_error=? WHERE id=?",
                       (json.dumps(fb), time.time(), reason, provider["id"]))
            router.sync_discovered(provider, fb)
            return fb, ""
        db.execute("UPDATE providers SET models_error=? WHERE id=?",
                   (reason[:200], provider["id"]))
        return None, reason

    try:
        async with httpx.AsyncClient(timeout=25, trust_env=False) as c:
            if provider["kind"] == "anthropic":
                r = await c.get(base + "/v1/models", headers=upstream_headers(provider))
            elif provider["kind"] == "codex":
                ver = balance_mod.codex_client_version()
                r = await c.get(base + "/models", params={"client_version": ver},
                                headers=upstream_headers(provider))
            else:
                r = await c.get(base + "/models", headers=upstream_headers(provider))
            if r.status_code == 401 and provider["kind"] == "codex":
                p2 = await refresh_codex(provider)
                if p2:
                    provider = p2
                    r = await c.get(base + "/models",
                                    params={"client_version": balance_mod.codex_client_version()},
                                    headers=upstream_headers(provider))
            if r.status_code >= 400:
                return await _fallback(f"HTTP {r.status_code}: {r.text[:120]}")
            d = r.json()
            data = d.get("data") if isinstance(d, dict) else None
            if provider["kind"] == "codex" and isinstance(d, dict) and d.get("models"):
                ids = sorted({m.get("slug") for m in d["models"]
                              if isinstance(m, dict) and m.get("slug")
                              and m.get("visibility", "list") == "list"})
            else:
                ids = sorted({m.get("id") for m in (data or [])
                              if isinstance(m, dict) and m.get("id")})
            if not ids:
                hint = json.dumps(d, ensure_ascii=False)[:120]
                rejected = isinstance(d, dict) and (
                    d.get("success") is False
                    or (isinstance(d.get("code"), int) and d.get("code") != 200)
                    or d.get("error"))
                why = f"上游拒绝: {hint}" if rejected else f"模型列表为空: {hint}"
                return await _fallback(why)
            db.execute("UPDATE providers SET models=?, models_at=?, models_error='' WHERE id=?",
                       (json.dumps(ids, ensure_ascii=False), time.time(), provider["id"]))
            router.sync_discovered(provider, ids)
            return ids, ""
    except Exception as e:
        return await _fallback(str(e))


async def test_provider(provider):
    t0 = time.time()
    ids, err = await discover(provider)
    ok = err == ""
    db.execute("UPDATE providers SET last_status=?, last_check_at=? WHERE id=?",
               ("ok" if ok else f"err: {err}"[:200], time.time(), provider["id"]))
    return {"ok": ok, "ms": int((time.time() - t0) * 1000),
            "models": len(ids) if ids else 0, "error": err}
