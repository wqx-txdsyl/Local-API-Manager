"""Non-stream response converters."""
import json


def _usage_o2a(u):
    u = u or {}
    return {"input_tokens": u.get("prompt_tokens", 0), "output_tokens": u.get("completion_tokens", 0)}


# -------- OpenAI chat response -> Anthropic messages response --------

def o2a_response(r, local_model=None):
    ch = (r.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    content = []
    text = msg.get("content")
    if isinstance(text, list):
        text = "\n".join(p.get("text", "") for p in text if isinstance(p, dict))
    if text:
        content.append({"type": "text", "text": text})
    if msg.get("reasoning_content"):
        content.insert(0, {"type": "thinking", "thinking": msg["reasoning_content"],
                           "signature": ""})
    for tc in msg.get("tool_calls") or []:
        fn = (tc or {}).get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:
            args = {"_raw": fn.get("arguments") or ""}
        content.append({"type": "tool_use", "id": tc.get("id", ""), "name": fn.get("name", ""),
                        "input": args})
    fr = ch.get("finish_reason")
    stop = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens",
            "content_filter": "refusal"}.get(fr, "end_turn")
    return {
        "id": "msg_" + (r.get("id") or "local").replace("chatcmpl-", ""),
        "type": "message", "role": "assistant",
        "model": local_model or r.get("model", ""),
        "content": content, "stop_reason": stop, "stop_sequence": None,
        "usage": _usage_o2a(r.get("usage")),
    }


# -------- Anthropic messages response -> OpenAI chat response --------

def a2o_response(r, local_model=None):
    text, reasoning, tool_calls = [], None, []
    for b in r.get("content") or []:
        t = b.get("type")
        if t == "text":
            text.append(b.get("text", ""))
        elif t == "thinking":
            reasoning = (reasoning or "") + b.get("thinking", "")
        elif t == "tool_use":
            tool_calls.append({"id": b.get("id", ""), "type": "function",
                               "function": {"name": b.get("name", ""),
                                            "arguments": json.dumps(b.get("input") or {},
                                                                    ensure_ascii=False)}})
    stop = {"end_turn": "stop", "stop_sequence": "stop", "tool_use": "tool_calls",
            "max_tokens": "length"}.get(r.get("stop_reason"), "stop")
    msg = {"role": "assistant", "content": "\n".join(text) if text else None}
    if reasoning:
        msg["reasoning_content"] = reasoning
    if tool_calls:
        msg["tool_calls"] = tool_calls
    u = r.get("usage") or {}
    return {
        "id": "chatcmpl-" + (r.get("id") or "local").replace("msg_", ""),
        "object": "chat.completion", "created": 0,
        "model": local_model or r.get("model", ""),
        "choices": [{"index": 0, "message": msg, "finish_reason": stop}],
        "usage": {"prompt_tokens": u.get("input_tokens", 0),
                  "completion_tokens": u.get("output_tokens", 0),
                  "total_tokens": u.get("input_tokens", 0) + u.get("output_tokens", 0)},
    }


# -------- OpenAI Responses response (Codex) -> OpenAI chat response --------

def r2o_response(r, local_model=None):
    text, tool_calls = [], []
    for item in r.get("output") or []:
        t = item.get("type")
        if t == "message":
            for part in item.get("content") or []:
                if part.get("type") in ("output_text", "text"):
                    text.append(part.get("text", ""))
        elif t == "function_call":
            tool_calls.append({"id": item.get("call_id") or item.get("id", ""), "type": "function",
                               "function": {"name": item.get("name", ""),
                                            "arguments": item.get("arguments") or "{}"}})
    status = r.get("status")
    reason = "stop"
    if tool_calls:
        reason = "tool_calls"
    elif status == "incomplete":
        reason = "length"
    u = (r.get("usage") or {})
    msg = {"role": "assistant", "content": "\n".join(text) if text else None}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-" + (r.get("id") or "local").replace("resp_", ""),
        "object": "chat.completion", "created": 0,
        "model": local_model or r.get("model", ""),
        "choices": [{"index": 0, "message": msg, "finish_reason": reason}],
        "usage": {"prompt_tokens": u.get("input_tokens", 0),
                  "completion_tokens": u.get("output_tokens", 0),
                  "total_tokens": u.get("input_tokens", 0) + u.get("output_tokens", 0)},
    }
