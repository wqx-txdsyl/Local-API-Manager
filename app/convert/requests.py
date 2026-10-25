"""Request-body converters between OpenAI Chat, Anthropic Messages and
OpenAI Responses (Codex subscription) shapes."""
import base64
import json
import re

DATA_URL = re.compile(r"^data:([^;]+);base64,(.*)$", re.S)


def _img_to_anthropic(url):
    m = DATA_URL.match(url or "")
    if m:
        try:
            base64.b64decode(m.group(2)[:64])  # sanity check
            return {"type": "image", "source": {"type": "base64",
                                                "media_type": m.group(1), "data": m.group(2)}}
        except Exception:
            pass
    return {"type": "text", "text": f"[image] {url}"}


def _img_to_dataurl(source):
    if isinstance(source, dict) and source.get("type") == "base64":
        return f"data:{source.get('media_type', 'image/png')};base64,{source.get('data', '')}"
    if isinstance(source, dict) and source.get("type") == "url":
        return source.get("url")
    return None


# ---------------- OpenAI -> Anthropic ----------------

def o2a_request(body):
    out = {}
    msgs = body.get("messages") or []
    system_parts = []
    a_msgs = []
    pending_tools = []  # consecutive openai tool msgs merged into one anthropic user turn

    def flush_tools():
        if pending_tools:
            a_msgs.append({"role": "user", "content": pending_tools.copy()})
            pending_tools.clear()

    for m in msgs:
        role = m.get("role")
        content = m.get("content")
        if role == "system" or role == "developer":
            if isinstance(content, str):
                system_parts.append(content)
            elif isinstance(content, list):
                system_parts += [p.get("text", "") for p in content if isinstance(p, dict)]
            continue
        if role == "tool":
            txt = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            pending_tools.append({"type": "tool_result", "tool_use_id": m.get("tool_call_id", ""),
                                  "content": txt})
            continue
        flush_tools()
        if role == "user":
            if isinstance(content, str):
                a_msgs.append({"role": "user", "content": [{"type": "text", "text": content}]})
            elif isinstance(content, list):
                blocks = []
                for p in content:
                    if not isinstance(p, dict):
                        continue
                    if p.get("type") == "text":
                        blocks.append({"type": "text", "text": p.get("text", "")})
                    elif p.get("type") == "image_url":
                        blocks.append(_img_to_anthropic((p.get("image_url") or {}).get("url", "")))
                a_msgs.append({"role": "user", "content": blocks or [{"type": "text", "text": ""}]})
        elif role == "assistant":
            blocks = []
            if isinstance(content, str) and content:
                blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                for p in content:
                    if isinstance(p, dict) and p.get("type") == "text":
                        blocks.append({"type": "text", "text": p.get("text", "")})
            for tc in m.get("tool_calls") or []:
                fn = (tc or {}).get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {"_raw": fn.get("arguments") or ""}
                blocks.append({"type": "tool_use", "id": tc.get("id", ""), "name": fn.get("name", ""),
                               "input": args})
            a_msgs.append({"role": "assistant", "content": blocks})
        else:
            a_msgs.append({"role": "user", "content": [{"type": "text", "text": str(content)}]})
    flush_tools()
    if system_parts:
        out["system"] = "\n\n".join([s for s in system_parts if s])
    out["messages"] = a_msgs or [{"role": "user", "content": [{"type": "text", "text": ""}]}]

    tools = body.get("tools")
    if tools:
        at = []
        for t in tools:
            fn = t.get("function") if t.get("type") == "function" else t
            if isinstance(fn, dict) and fn.get("name"):
                at.append({"name": fn.get("name"), "description": fn.get("description") or "",
                           "input_schema": fn.get("parameters") or {"type": "object", "properties": {}}})
        if at:
            out["tools"] = at
    tc = body.get("tool_choice")
    if tc == "required":
        out["tool_choice"] = {"type": "any"}
    elif tc == "auto" or tc is None:
        pass
    elif tc == "none":
        out.pop("tools", None)
    elif isinstance(tc, dict):
        fn = tc.get("function") or {}
        if fn.get("name"):
            out["tool_choice"] = {"type": "tool", "name": fn["name"]}

    mt = body.get("max_tokens") or body.get("max_completion_tokens") or 8192
    out["max_tokens"] = mt
    for k in ("temperature", "top_p", "stop_sequences", "metadata", "user_id"):
        if body.get(k) is not None:
            out[k] = body[k]
    if body.get("stop"):
        out["stop_sequences"] = body["stop"] if isinstance(body["stop"], list) else [body["stop"]]
    if body.get("stream") is not None:
        out["stream"] = body["stream"]
    return out


# ---------------- Anthropic -> OpenAI ----------------

def _system_text(system):
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n\n".join(b.get("text", "") for b in system if isinstance(b, dict))
    return ""


def a2o_request(body):
    out = {}
    msgs = []
    if body.get("system"):
        msgs.append({"role": "system", "content": _system_text(body["system"])})

    for m in body.get("messages") or []:
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, str):
            msgs.append({"role": role, "content": content})
            continue
        if role == "assistant":
            text, reasoning, tool_calls = [], None, []
            for b in content or []:
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
            msg = {"role": "assistant", "content": "\n".join(text) if text else None}
            if reasoning:
                msg["reasoning_content"] = reasoning
            if tool_calls:
                msg["tool_calls"] = tool_calls
            msgs.append(msg)
        else:  # user
            tool_msgs, texts, images = [], [], []
            for b in content or []:
                t = b.get("type")
                if t == "text":
                    texts.append(b.get("text", ""))
                elif t == "tool_result":
                    c = b.get("content")
                    if isinstance(c, list):
                        c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
                    tool_msgs.append({"role": "tool", "tool_call_id": b.get("tool_use_id", ""),
                                      "content": c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)})
                elif t == "image":
                    url = _img_to_dataurl(b.get("source"))
                    if url:
                        images.append({"type": "image_url", "image_url": {"url": url}})
            msgs.extend(tool_msgs)
            if texts or images:
                c = texts if not images else (
                    [{"type": "text", "text": t} for t in texts] + images)
                msgs.append({"role": "user", "content": "\n".join(texts) if not images else c})

    out["messages"] = msgs
    if body.get("tools"):
        out["tools"] = [{"type": "function", "function": {
            "name": t.get("name"), "description": t.get("description") or "",
            "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}}
            for t in body["tools"] if isinstance(t, dict) and t.get("name")]
    tc = body.get("tool_choice")
    if isinstance(tc, dict):
        if tc.get("type") == "any":
            out["tool_choice"] = "required"
        elif tc.get("type") == "tool":
            out["tool_choice"] = {"type": "function", "function": {"name": tc.get("name")}}
        elif tc.get("type") == "auto":
            out["tool_choice"] = "auto"
    elif tc is not None:
        out["tool_choice"] = tc

    out["max_tokens"] = body.get("max_tokens") or 8192
    for k in ("temperature", "top_p", "stream", "n", "user"):
        if body.get(k) is not None:
            out[k] = body[k]
    if body.get("stop_sequences"):
        out["stop"] = body["stop_sequences"]
    if body.get("thinking") and isinstance(body["thinking"], dict):
        budget = body["thinking"].get("budget_tokens")
        if budget:
            out["max_tokens"] = max(out["max_tokens"], budget + 4096)
    return out


# ---------------- OpenAI -> Responses (Codex) ----------------

def o2r_request(body):
    instructions, items = [], []
    for m in body.get("messages") or []:
        role = m.get("role")
        content = m.get("content")
        if role in ("system", "developer"):
            if isinstance(content, str):
                instructions.append(content)
            elif isinstance(content, list):
                instructions += [p.get("text", "") for p in content if isinstance(p, dict)]
            continue
        if role == "tool":
            txt = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            items.append({"type": "function_call_output", "call_id": m.get("tool_call_id", ""),
                          "output": txt})
            continue
        if role == "assistant":
            text_parts = []
            if isinstance(content, str) and content:
                text_parts.append(content)
            elif isinstance(content, list):
                for p in content:
                    if isinstance(p, dict) and p.get("type") == "text":
                        text_parts.append(p.get("text", ""))
            if text_parts:
                items.append({"type": "message", "role": "assistant",
                              "content": [{"type": "output_text", "text": "\n".join(text_parts),
                                           "annotations": []}]})
            for tc in m.get("tool_calls") or []:
                fn = (tc or {}).get("function") or {}
                items.append({"type": "function_call", "call_id": tc.get("id", tc.get("call_id", "")),
                              "name": fn.get("name", ""), "arguments": fn.get("arguments") or "{}"})
        else:  # user
            if isinstance(content, str):
                items.append({"type": "message", "role": "user",
                              "content": [{"type": "input_text", "text": content}]})
            elif isinstance(content, list):
                parts = []
                for p in content:
                    if not isinstance(p, dict):
                        continue
                    if p.get("type") == "text":
                        parts.append({"type": "input_text", "text": p.get("text", "")})
                    elif p.get("type") == "image_url":
                        url = (p.get("image_url") or {}).get("url", "")
                        if url.startswith("data:"):
                            parts.append({"type": "input_image", "image_url": url})
                items.append({"type": "message", "role": "user", "content": parts})
    out = {"input": items}
    if instructions:
        out["instructions"] = "\n\n".join(instructions)
    if body.get("tools"):
        out["tools"] = [{"type": "function", "name": (t.get("function") or t).get("name"),
                         "description": (t.get("function") or t).get("description") or "",
                         "parameters": (t.get("function") or t).get("parameters")
                         or {"type": "object", "properties": {}},
                         "strict": False}
                        for t in body["tools"] if (t.get("function") or t).get("name")]
    tc = body.get("tool_choice")
    if tc == "required":
        out["tool_choice"] = "required"
    elif isinstance(tc, dict) and (tc.get("function") or {}).get("name"):
        out["tool_choice"] = {"type": "function", "name": tc["function"]["name"]}
    mt = body.get("max_tokens") or body.get("max_completion_tokens")
    if mt:
        out["max_output_tokens"] = mt
    for k in ("temperature", "top_p", "stream"):
        if body.get(k) is not None:
            out[k] = body[k]
    if body.get("reasoning_effort"):
        out["reasoning"] = {"effort": body["reasoning_effort"], "summary": "auto"}
    if body.get("metadata") and isinstance(body["metadata"], dict):
        for k in ("user_id",):
            if body["metadata"].get(k):
                out["metadata"] = {"user_id": str(body["metadata"][k])[:127]}
    out["store"] = False
    return out
