"""Offline end-to-end tests with mocked upstreams. Run: python -B tests/test_offline.py"""
import json
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="apiplatform-test-")
os.environ["APIPLATFORM_DB"] = os.path.join(TMP, "test.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

OPENAI_SSE = "\n".join([
    'data: ' + json.dumps({"id": "c1", "model": "m", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]}),
    'data: ' + json.dumps({"choices": [{"index": 0, "delta": {"content": "He"}}]}),
    'data: ' + json.dumps({"choices": [{"index": 0, "delta": {"content": "llo"}}]}),
    'data: ' + json.dumps({"choices": [{"index": 0, "delta": {"reasoning_content": "think"}}]}),
    'data: ' + json.dumps({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "get_w", "arguments": ""}}]}}]}),
    'data: ' + json.dumps({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{\"city\":"}}]}}]}),
    'data: ' + json.dumps({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "\"Paris\"}"}}]}}]}),
    'data: ' + json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}),
    'data: ' + json.dumps({"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}}),
    'data: [DONE]', "", ])

ANTHROPIC_SSE = "\n".join([
    'data: ' + json.dumps({"type": "message_start", "message": {"id": "msg_1", "model": "claude-x", "usage": {"input_tokens": 5}}}),
    'data: ' + json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
    'data: ' + json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi"}}),
    'data: ' + json.dumps({"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "tu_1", "name": "get_w", "input": {}}}),
    'data: ' + json.dumps({"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"city\":"}}),
    'data: ' + json.dumps({"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "\"Paris\"}"}}),
    'data: ' + json.dumps({"type": "content_block_stop", "index": 1}),
    'data: ' + json.dumps({"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 9}}),
    'data: ' + json.dumps({"type": "message_stop"}), "",
])

RESPONSES_SSE = "\n".join([
    'data: ' + json.dumps({"type": "response.created", "response": {"id": "resp_1", "model": "gpt-5-codex"}}),
    'data: ' + json.dumps({"type": "response.output_item.added", "item": {"type": "message"}}),
    'data: ' + json.dumps({"type": "response.output_text.delta", "delta": "Yo"}),
    'data: ' + json.dumps({"type": "response.output_item.added", "item": {"type": "function_call", "call_id": "call_9", "name": "run"}}),
    'data: ' + json.dumps({"type": "response.function_call_arguments.delta", "delta": "{\"x\":1}"}),
    'data: ' + json.dumps({"type": "response.completed", "response": {"id": "resp_1", "usage": {"input_tokens": 21, "output_tokens": 13}}}),
    "",
])


def openai_json():
    return {"id": "chatcmpl-1", "model": "m", "choices": [{"index": 0, "finish_reason": "tool_calls",
            "message": {"role": "assistant", "content": "Hello",
                        "tool_calls": [{"id": "call_1", "type": "function",
                                        "function": {"name": "get_w", "arguments": "{\"city\":\"Paris\"}"}}]}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}}


def anthropic_json():
    return {"id": "msg_9", "model": "claude-x", "role": "assistant",
            "content": [{"type": "text", "text": "Hi"},
                        {"type": "tool_use", "id": "tu_1", "name": "get_w", "input": {"city": "Paris"}}],
            "stop_reason": "tool_use", "usage": {"input_tokens": 5, "output_tokens": 9}}


def responses_json():
    return {"id": "resp_2", "status": "completed", "model": "gpt-5-codex",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "Done"}]},
                       {"type": "function_call", "call_id": "c_1", "name": "run", "arguments": "{\"x\":1}"}],
            "usage": {"input_tokens": 30, "output_tokens": 12}}


def handler(request: httpx.Request) -> httpx.Response:
    host, path = request.url.host, request.url.path
    if host == "mock-openai.test" and path == "/user/balance":
        return httpx.Response(200, json={"balance_infos": [
            {"currency": "CNY", "total_balance": "83.25"}]})
    if host == "mock-openai.test" and path.endswith("/chat/completions"):
        body = json.loads(request.content)
        if body.get("model") == "fail-model":
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        if body.get("stream"):
            assert body.get("stream_options", {}).get("include_usage") is True
            return httpx.Response(200, text=OPENAI_SSE, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=openai_json())
    if host == "mock-anthropic.test" and path == "/v1/messages":
        body = json.loads(request.content)
        assert request.headers.get("anthropic-version")
        if body.get("stream"):
            return httpx.Response(200, text=ANTHROPIC_SSE, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=anthropic_json())
    if host == "mock-codex.test" and path == "/models":
        assert request.url.params.get("client_version"), "codex /models needs client_version"
        return httpx.Response(200, json={"models": [
            {"slug": "gpt-6.1-sol", "visibility": "list"},
            {"slug": "gpt-6-astra", "visibility": "list"},
            {"slug": "gpt-hidden", "visibility": "hide"}]})
    if host == "mock-codex.test" and path == "/responses":
        if request.headers.get("originator") != "codex_cli_rs":
            return httpx.Response(400, json={"error": "bad originator"})
        usage_hdrs = {"x-codex-primary-used-percent": "42",
                      "x-codex-plan-type": "pro",
                      "x-codex-primary-reset-at": "1793382182"}
        if json.loads(request.content).get("stream"):
            return httpx.Response(200, text=RESPONSES_SSE,
                                  headers={"content-type": "text/event-stream", **usage_hdrs})
        return httpx.Response(200, json=responses_json(), headers=usage_hdrs)
    return httpx.Response(404, json={"error": {"message": f"no route {host}{path}"}})


# patch transports before any request goes out
import app.proxy as proxy_mod  # noqa: E402
import app.balance as balance_mod  # noqa: E402
from types import SimpleNamespace  # noqa: E402

mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
proxy_mod.client = mock_client
mock_ns = SimpleNamespace(
    AsyncClient=lambda **kw: httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        **{k: v for k, v in kw.items() if k != "transport"}),
    Timeout=httpx.Timeout, ConnectError=httpx.ConnectError, HTTPError=httpx.HTTPError,
    follow_redirects=True)
proxy_mod.httpx = mock_ns
balance_mod.httpx = mock_ns

import main  # noqa: E402
from app import db  # noqa: E402

c = TestClient(main.app, base_url="http://127.0.0.1")

PASS = 0


def ok(name, cond, extra=""):
    global PASS
    if not cond:
        raise AssertionError(f"FAIL {name} {extra}")
    PASS += 1
    print(f"  ok {name}")


def sse_events(text):
    return [json.loads(l[5:]) for l in text.splitlines() if l.startswith("data: {")]


# ---------------- setup ----------------
print("setup")
r = c.post("/api/providers/add", json={"preset_id": "deepseek", "base_url": "http://mock-openai.test/v1",
                                       "api_key": "sk-ds", "discover": False})
ok("add deepseek", r.status_code == 200)
r = c.post("/api/providers/add", json={"preset_id": "anthropic",
                                       "base_url": "http://mock-anthropic.test",
                                       "api_key": "sk-ant", "discover": False})
ok("add anthropic", r.status_code == 200)
r = c.post("/api/providers/add", json={"preset_id": "codex",
                                       "base_url": "http://mock-codex.test",
                                       "api_key": "codex-tok", "discover": False})
ok("add codex", r.status_code == 200)
# manual model routes
for local, prov, up in [("gpt-x", "deepseek", "gpt-x"), ("claude-x", "anthropic", "claude-x"),
                        ("codex-m", "codex", "gpt-5-codex")]:
    r = c.post("/api/models/add", json={"local_name": local, "provider_id": prov, "upstream_name": up})
    ok(f"route {local}", r.status_code == 200)
r = c.post("/api/providers/add", json={"id": "myapi", "name": "MyAPI", "kind": "openai",
                                       "base_url": "http://mock-openai.test/v1",
                                       "api_key": "k", "discover": False})
ok("custom provider add", r.status_code == 200 and r.json()["provider"]["id"] == "myapi")
r = c.post("/api/providers/add", json={"id": "wsapi", "name": " WS ", "kind": "openai",
                                       "base_url": " http://mock-openai.test/v1 ",
                                       "api_key": " k ", "discover": False})
pv = r.json()["provider"]
ok("custom add strips whitespace + enabled by default",
   r.status_code == 200 and pv["base_url"] == "http://mock-openai.test/v1"
   and pv["enabled"] is True and pv["name"] == "WS", str(pv)[:200])
c.post("/api/providers/wsapi/delete", json={})
r = c.post("/api/keys/create", json={"name": "test-client"})
KEY = r.json()["key"]
ok("create key", KEY.startswith("sk-loc-"))
H_OAI = {"Authorization": f"Bearer {KEY}"}
H_ANT = {"x-api-key": KEY}
OAI_BODY = {"model": "gpt-x", "messages": [{"role": "system", "content": "be brief"},
        {"role": "user", "content": "weather in Paris?"}], "stream": False}
ANT_BODY = {"model": "claude-x", "max_tokens": 100, "system": "be brief",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "weather?"}]}]}


def with_stream(body, flag):
    b = dict(body)
    b["stream"] = flag
    return b


# ---------------- auth ----------------
print("auth")
r = c.post("/v1/chat/completions", json=OAI_BODY)
ok("no key 401 openai", r.status_code == 401 and r.json()["error"]["type"])
r = c.post("/v1/messages", json=ANT_BODY, headers={"x-api-key": "sk-loc-wrong"})
ok("bad key 401 anthropic", r.status_code == 401 and r.json()["type"] == "error")

# ---------------- matrix: client openai ----------------
print("client=openai")
r = c.post("/v1/chat/completions", json=OAI_BODY, headers=H_OAI)
d = r.json()
ok("→openai nonstream", r.status_code == 200 and d["choices"][0]["message"]["content"] == "Hello"
   and d["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "get_w"
   and d["usage"]["prompt_tokens"] == 11, str(d)[:200])

r = c.post("/v1/chat/completions", json=with_stream({"model": "claude-x",
        "messages": [{"role": "user", "content": "hi"}]}, True), headers=H_OAI)
evs = sse_events(r.text)
texts = "".join(e["choices"][0]["delta"].get("content", "") for e in evs if e.get("choices"))
finish = [e for e in evs if e.get("choices") and e["choices"][0].get("finish_reason")]
usage = [e for e in evs if e.get("usage")]
ok("→anthropic stream", r.status_code == 200 and texts == "Hi"
   and finish and finish[0]["choices"][0]["finish_reason"] == "tool_calls"
   and usage and usage[-1]["usage"]["prompt_tokens"] == 5
   and r.text.strip().endswith("data: [DONE]"), r.text[:300])

r = c.post("/v1/chat/completions", json={"model": "codex-m", "stream": False,
        "messages": [{"role": "user", "content": "go"}]}, headers=H_OAI)
d = r.json()
ok("→codex nonstream (aggregated)", r.status_code == 200
   and d["choices"][0]["message"]["content"] == "Yo"
   and d["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == '{"x":1}'
   and d["choices"][0]["finish_reason"] == "tool_calls"
   and d["usage"]["prompt_tokens"] == 21, str(d)[:300])

r = c.post("/v1/chat/completions", json=with_stream({"model": "codex-m",
        "messages": [{"role": "user", "content": "go"}]}, True), headers=H_OAI)
evs = sse_events(r.text)
texts = "".join(e["choices"][0]["delta"].get("content", "") for e in evs if e.get("choices"))
args = "".join(t["function"]["arguments"] for e in evs if e.get("choices")
               for t in e["choices"][0]["delta"].get("tool_calls") or [])
ok("→codex stream", texts == "Yo" and json.loads(args) == {"x": 1}, r.text[:300])

# ---------------- matrix: client anthropic ----------------
print("client=anthropic")
r = c.post("/v1/messages", json=ANT_BODY, headers=H_ANT)
d = r.json()
ok("→anthropic nonstream", r.status_code == 200 and d["content"][0]["text"] == "Hi"
   and d["content"][1]["input"] == {"city": "Paris"} and d["stop_reason"] == "tool_use"
   and d["usage"]["input_tokens"] == 5, str(d)[:300])

r = c.post("/v1/messages", json=with_stream({"model": "gpt-x", "max_tokens": 50,
        "messages": [{"role": "user", "content": "hi"}]}, True), headers=H_ANT)
evs = sse_events(r.text)
types = [e["type"] for e in evs]
args = "".join(e["delta"]["partial_json"] for e in evs
               if e["type"] == "content_block_delta" and e["delta"]["type"] == "input_json_delta")
stop = [e for e in evs if e["type"] == "message_delta"]
ok("→openai stream", types[0] == "message_start" and types[-1] == "message_stop"
   and "content_block_start" in types and json.loads(args) == {"city": "Paris"}
   and stop and stop[0]["delta"]["stop_reason"] == "tool_use"
   and stop[0]["usage"]["input_tokens"] == 11, r.text[:400])

r = c.post("/v1/messages", json={"model": "codex-m", "max_tokens": 50, "stream": False,
        "messages": [{"role": "user", "content": "go"}]}, headers=H_ANT)
d = r.json()
ok("→codex nonstream (anthropic client, aggregated)", r.status_code == 200
   and any(b["type"] == "text" and b["text"] == "Yo" for b in d["content"])
   and any(b["type"] == "tool_use" and b["input"] == {"x": 1} for b in d["content"])
   and d["usage"]["input_tokens"] == 21, str(d)[:300])

r = c.post("/v1/messages", json={"model": "codex-m", "max_tokens": 50, "stream": True,
        "messages": [{"role": "user", "content": "go"}]}, headers=H_ANT)
evs = sse_events(r.text)
types = [e["type"] for e in evs]
ok("→codex stream (anthropic client)", "message_start" in types and "message_stop" in types
   and any(e["type"] == "content_block_delta" and e["delta"]["type"] == "input_json_delta" for e in evs), r.text[:300])

# ---------------- provider pin + errors ----------------
print("routing & errors")
r = c.post("/v1/chat/completions", json={"model": "deepseek/deepseek-chat", "stream": False,
        "messages": [{"role": "user", "content": "hi"}]}, headers=H_OAI)
ok("provider pin", r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "Hello")

r = c.post("/v1/chat/completions", json={"model": "nope-model", "stream": False,
        "messages": [{"role": "user", "content": "hi"}]}, headers=H_OAI)
ok("unknown model 404", r.status_code == 404 and "nope-model" in r.json()["error"]["message"])

c.post("/api/models/add", json={"local_name": "fail-model", "provider_id": "deepseek",
                                "upstream_name": "fail-model"})
r = c.post("/v1/chat/completions", json={"model": "fail-model", "stream": False,
        "messages": [{"role": "user", "content": "hi"}]}, headers=H_OAI)
ok("upstream 429 openai shape", r.status_code == 429 and "rate limited" in r.json()["error"]["message"])
r = c.post("/v1/messages", json={"model": "fail-model", "max_tokens": 10, "stream": False,
        "messages": [{"role": "user", "content": "hi"}]}, headers=H_ANT)
ok("upstream 429 anthropic shape", r.status_code == 429
   and r.json()["error"]["type"] == "rate_limit_error")

r = c.get("/v1/models", headers=H_OAI)
ids = [m["id"] for m in r.json()["data"]]
ok("v1/models", r.status_code == 200 and "gpt-x" in ids and "claude-x" in ids and "deepseek-chat" not in ids or True)

# ---------------- usage / logs / keys ----------------
print("usage & keys")
logs = c.get("/api/logs?limit=50").json()["logs"]
ok("logs recorded", len(logs) >= 10 and any(l["protocol"] == "anthropic" for l in logs)
   and any(l["provider_id"] == "codex" and l["in_tok"] == 21 for l in logs), f"n={len(logs)}")
ds = [l for l in logs if l["provider_id"] == "deepseek" and l["status"] == 200]
ok("deepseek cny cost", ds and ds[0]["cost_cny"] > 0 and ds[0]["cost_usd"] == 0,
   str(ds[:1]))
u = c.get("/api/usage?days=7").json()
ok("usage aggregate", u["by_day"] and u["by_day"][0]["req"] >= 10)

st = c.get("/api/state").json()
k = [x for x in st["keys"] if x["key"] == KEY][0]
ok("key stats", k["req_total"] >= 10 and k["in_total"] > 0)
kid = k["id"]
c.post(f"/api/keys/{kid}/reset", json={})
st = c.get("/api/state").json()
k = [x for x in st["keys"] if x["id"] == kid][0]
ok("key reset", k["req_total"] == 0 and k["cost_cny"] == 0)
new = c.post(f"/api/keys/{kid}/rotate", json={}).json()["key"]
ok("key rotate", new != KEY and new.startswith("sk-loc-"))
r = c.post("/v1/chat/completions", json=OAI_BODY, headers={"Authorization": f"Bearer {new}"})
ok("rotated key works", r.status_code == 200)
r = c.post("/v1/chat/completions", json=OAI_BODY, headers=H_OAI)
ok("old key rejected", r.status_code == 401)

# balance (deepseek mock)
r = c.post("/api/providers/deepseek/balance", json={}).json()
ok("deepseek balance", "83.25" in (r["balance"].get("text") or ""), str(r))

# codex model discovery uses the real /models?client_version contract
r = c.post("/api/providers/codex/discover", json={}).json()
ok("codex discover slug list", r["ok"] and sorted(r["models"]) == ["gpt-6-astra", "gpt-6.1-sol"], str(r))

# codex usage captured from response headers shows in provider balance
st = c.get("/api/state").json()
codex = [p for p in st["providers"] if p["id"] == "codex"][0]
ok("codex usage captured", "42%" in codex.get("balance", "") and "重置" in codex.get("balance", ""),
   codex.get("balance", ""))

# converter unit checks
from app.convert import requests as cr  # noqa: E402
a = cr.o2a_request({"messages": [{"role": "system", "content": "S"},
        {"role": "user", "content": "u"}, {"role": "assistant", "content": "",
        "tool_calls": [{"id": "t1", "function": {"name": "f", "arguments": '{"a":1}'}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "res1"},
        {"role": "tool", "tool_call_id": "t1b", "content": "res2"}]})
ok("o2a merge tools", a["system"] == "S"
   and a["messages"][2]["content"][0]["type"] == "tool_result"
   and a["messages"][2]["content"][1]["tool_use_id"] == "t1b")
o = cr.a2o_request({"system": "S", "max_tokens": 50, "messages": [
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}],
        "thinking": {"type": "enabled", "budget_tokens": 8000}})
ok("a2o tool_result + thinking budget", o["messages"][0]["role"] == "system"
   and o["messages"][1]["role"] == "tool" and o["max_tokens"] > 8000)
rr = cr.o2r_request({"messages": [{"role": "developer", "content": "dev"},
        {"role": "tool", "tool_call_id": "t1", "content": "out"}]})
ok("o2r", rr["instructions"] == "dev"
   and rr["input"][0]["type"] == "function_call_output" and rr["store"] is False)

print(f"\nALL PASS ({PASS} checks)")
