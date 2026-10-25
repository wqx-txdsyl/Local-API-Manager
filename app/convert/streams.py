"""Streaming transcoders.

Every upstream SSE protocol is parsed into a canonical event stream:
  {"t":"start","id","model"}
  {"t":"text","delta"}
  {"t":"reasoning","delta"}
  {"t":"tool_start","index","id","name"}
  {"t":"tool_args","index","delta"}
  {"t":"finish","reason":"stop|tools|max_tokens|error","usage":{"in","out"}|None}
  {"t":"end"}
Client serializers then render canonical events as OpenAI chunks or
Anthropic events. usage=None means the upstream never reported usage;
the proxy estimates from accumulated output length.
"""
import json
import secrets

FINISH_MAP_OAI = {"stop": "stop", "tool_calls": "tools", "length": "max_tokens"}


class OpenAIStreamParser:
    kind = "openai"

    def __init__(self):
        self.usage = None
        self.out_chars = 0
        self._finish = None
        self._tools_started = set()
        self.done = False

    def feed(self, payload):
        if self.done or not isinstance(payload, dict):
            return []
        events = []
        if payload.get("usage"):
            u = payload["usage"]
            self.usage = {"in": u.get("prompt_tokens", 0), "out": u.get("completion_tokens", 0)}
        for choice in payload.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                self.out_chars += len(delta["content"])
                events.append({"t": "text", "delta": delta["content"]})
            if delta.get("reasoning_content"):
                events.append({"t": "reasoning", "delta": delta["reasoning_content"]})
            for tc in delta.get("tool_calls") or []:
                i = tc.get("index", 0)
                fn = tc.get("function") or {}
                if (tc.get("id") or fn.get("name")) and i not in self._tools_started:
                    self._tools_started.add(i)
                    events.append({"t": "tool_start", "index": i,
                                   "id": tc.get("id") or f"call_{i}", "name": fn.get("name", "")})
                if fn.get("arguments"):
                    events.append({"t": "tool_args", "index": i, "delta": fn["arguments"]})
            if choice.get("finish_reason"):
                self._finish = FINISH_MAP_OAI.get(choice["finish_reason"], "stop")
        if self._finish and self.usage is not None:
            events.append({"t": "finish", "reason": self._finish, "usage": self.usage})
            events.append({"t": "end"})
            self.done = True
        return events

    def flush(self):
        if self.done:
            return []
        self.done = True
        return [{"t": "finish", "reason": self._finish or "stop", "usage": self.usage},
                {"t": "end"}]


class AnthropicStreamParser:
    kind = "anthropic"

    def __init__(self):
        self.usage = {"in": 0, "out": 0}
        self.out_chars = 0
        self.done = False
        self._finish = None

    def feed(self, ev):
        if self.done or not isinstance(ev, dict):
            return []
        t = ev.get("type")
        out = []
        if t == "message_start":
            msg = ev.get("message") or {}
            self.usage["in"] = (msg.get("usage") or {}).get("input_tokens", 0)
            out.append({"t": "start", "id": msg.get("id", ""), "model": msg.get("model", "")})
        elif t == "content_block_start":
            b = ev.get("content_block") or {}
            if b.get("type") == "tool_use":
                out.append({"t": "tool_start", "index": ev.get("index", 0),
                            "id": b.get("id", ""), "name": b.get("name", "")})
        elif t == "content_block_delta":
            d = ev.get("delta") or {}
            if d.get("type") == "text_delta" and d.get("text"):
                self.out_chars += len(d["text"])
                out.append({"t": "text", "delta": d["text"]})
            elif d.get("type") == "thinking_delta" and d.get("thinking"):
                out.append({"t": "reasoning", "delta": d["thinking"]})
            elif d.get("type") == "input_json_delta" and d.get("partial_json"):
                out.append({"t": "tool_args", "index": ev.get("index", 0),
                            "delta": d["partial_json"]})
        elif t == "message_delta":
            d = ev.get("delta") or {}
            stop = {"end_turn": "stop", "tool_use": "tools", "max_tokens": "max_tokens",
                    "stop_sequence": "stop"}.get(d.get("stop_reason"), "stop")
            u = ev.get("usage") or {}
            if u.get("output_tokens") is not None:
                self.usage["out"] = u["output_tokens"]
            self._finish = stop
        elif t == "message_stop":
            out.append({"t": "finish", "reason": self._finish or "stop", "usage": self.usage})
            out.append({"t": "end"})
            self.done = True
        return out

    def flush(self):
        return self._final()

    def _final(self):
        if self.done:
            return []
        self.done = True
        return [{"t": "finish", "reason": self._finish or "stop", "usage": self.usage},
                {"t": "end"}]


class ResponsesStreamParser:
    """OpenAI Responses API SSE (Codex subscription backend)."""
    kind = "responses"

    def __init__(self):
        self.usage = None
        self.out_chars = 0
        self.done = False
        self._tool_idx = -1
        self._started_calls = set()

    def feed(self, ev):
        if self.done or not isinstance(ev, dict):
            return []
        t = ev.get("type")
        out = []
        if t == "response.created":
            r = ev.get("response") or {}
            out.append({"t": "start", "id": r.get("id", ""), "model": r.get("model", "")})
        elif t == "response.output_item.added":
            item = ev.get("item") or {}
            if item.get("type") == "function_call":
                self._tool_idx += 1
                cid = item.get("call_id") or item.get("id") or f"call_{self._tool_idx}"
                self._started_calls.add(cid)
                out.append({"t": "tool_start", "index": self._tool_idx, "id": cid,
                            "name": item.get("name", "")})
        elif t == "response.output_text.delta":
            d = ev.get("delta") or ""
            self.out_chars += len(d)
            out.append({"t": "text", "delta": d})
        elif t in ("response.reasoning_text.delta", "response.reasoning_summary_text.delta"):
            if ev.get("delta"):
                out.append({"t": "reasoning", "delta": ev["delta"]})
        elif t == "response.function_call_arguments.delta":
            out.append({"t": "tool_args", "index": max(self._tool_idx, 0),
                        "delta": ev.get("delta") or ""})
        elif t == "response.output_item.done":
            item = ev.get("item") or {}
            if item.get("type") == "function_call":
                cid = item.get("call_id") or item.get("id") or ""
                if cid not in self._started_calls:
                    self._tool_idx += 1
                    self._started_calls.add(cid)
                    out.append({"t": "tool_start", "index": self._tool_idx, "id": cid,
                                "name": item.get("name", "")})
                    out.append({"t": "tool_args", "index": self._tool_idx,
                                "delta": item.get("arguments") or "{}"})
        elif t == "response.completed":
            r = ev.get("response") or {}
            u = r.get("usage") or {}
            self.usage = {"in": u.get("input_tokens", 0), "out": u.get("output_tokens", 0)}
            reason = "tools" if self._tool_idx >= 0 else "stop"
            out.append({"t": "finish", "reason": reason, "usage": self.usage})
            out.append({"t": "end"})
            self.done = True
        elif t == "response.incomplete":
            u = (ev.get("response") or {}).get("usage") or {}
            self.usage = {"in": u.get("input_tokens", 0), "out": u.get("output_tokens", 0)}
            out.append({"t": "finish", "reason": "max_tokens", "usage": self.usage})
            out.append({"t": "end"})
            self.done = True
        elif t in ("response.failed", "error"):
            msg = ""
            if t == "error":
                msg = (ev.get("error") or {}).get("message", "")
            out.append({"t": "finish", "reason": "error", "usage": self.usage, "error": msg})
            out.append({"t": "end"})
            self.done = True
        return out

    def flush(self):
        if self.done:
            return []
        self.done = True
        return [{"t": "finish", "reason": "stop", "usage": self.usage}, {"t": "end"}]


# ---------------- client serializers ----------------

class OpenAIChunkWriter:
    def __init__(self, local_model):
        self.id = "chatcmpl-local-" + secrets.token_hex(8)
        self.model = local_model
        self._role_sent = False
        self.usage = None
        self.out_chars = 0

    def _chunk(self, delta, finish=None):
        return {"id": self.id, "object": "chat.completion.chunk", "created": 0,
                "model": self.model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    def feed(self, ev):
        out = []
        t = ev["t"]
        if t in ("text", "reasoning", "tool_start", "tool_args") and not self._role_sent:
            self._role_sent = True
            out.append(self._chunk({"role": "assistant", "content": ""}))
        if t == "text":
            self.out_chars += len(ev["delta"])
            out.append(self._chunk({"content": ev["delta"]}))
        elif t == "reasoning":
            out.append(self._chunk({"reasoning_content": ev["delta"]}))
        elif t == "tool_start":
            out.append(self._chunk({"tool_calls": [{
                "index": ev["index"], "id": ev["id"], "type": "function",
                "function": {"name": ev["name"], "arguments": ""}}]}))
        elif t == "tool_args":
            out.append(self._chunk({"tool_calls": [{
                "index": ev["index"],
                "function": {"arguments": ev["delta"]}}]}))
        elif t == "finish":
            self.usage = ev.get("usage")
            reason = {"tools": "tool_calls", "max_tokens": "length"}.get(ev["reason"], "stop")
            out.append(self._chunk({}, finish=reason))
            u = self.usage or {}
            if u.get("in") or u.get("out"):
                out.append({"id": self.id, "object": "chat.completion.chunk", "created": 0,
                            "model": self.model, "choices": [],
                            "usage": {"prompt_tokens": u.get("in", 0),
                                      "completion_tokens": u.get("out", 0),
                                      "total_tokens": u.get("in", 0) + u.get("out", 0)}})
        return out


class AnthropicEventWriter:
    def __init__(self, local_model):
        self.model = local_model
        self._started = False
        self._block = None   # ("text"|"thinking"|"tool", index)
        self._next_block = -1
        self._tool_block = {}
        self.usage = None
        self.out_chars = 0

    def _open(self, kind, extra=None):
        self._next_block += 1
        self._block = (kind, self._next_block)
        return [{"type": "content_block_start", "index": self._next_block,
                 "content_block": extra or {"type": kind}}]

    def _close(self):
        if self._block is not None:
            i = self._block[1]
            self._block = None
            return [{"type": "content_block_stop", "index": i}]
        return []

    def feed(self, ev):
        out = []
        t = ev["t"]
        if t == "start":
            self._started = True
            out.append({"type": "message_start", "message": {
                "id": "msg_local-" + secrets.token_hex(8), "type": "message",
                "role": "assistant", "model": self.model, "content": [],
                "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0}}})
            return out
        if not self._started:
            self._started = True
            out.append({"type": "message_start", "message": {
                "id": "msg_local-" + secrets.token_hex(8), "type": "message",
                "role": "assistant", "model": self.model, "content": [],
                "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0}}})
        if t == "text":
            if not (self._block and self._block[0] == "text"):
                out += self._close()
                out += self._open("text")
            self.out_chars += len(ev["delta"])
            out.append({"type": "content_block_delta", "index": self._block[1],
                        "delta": {"type": "text_delta", "text": ev["delta"]}})
        elif t == "reasoning":
            if not (self._block and self._block[0] == "thinking"):
                out += self._close()
                out += self._open("thinking", {"type": "thinking", "thinking": ""})
            out.append({"type": "content_block_delta", "index": self._block[1],
                        "delta": {"type": "thinking_delta", "thinking": ev["delta"]}})
        elif t == "tool_start":
            out += self._close()
            out += self._open("tool_use", {"type": "tool_use", "id": ev["id"],
                                           "name": ev["name"], "input": {}})
            self._tool_block[ev["index"]] = self._block[1]
        elif t == "tool_args":
            bi = self._tool_block.get(ev["index"], self._block[1] if self._block else 0)
            out.append({"type": "content_block_delta", "index": bi,
                        "delta": {"type": "input_json_delta", "partial_json": ev["delta"]}})
        elif t == "finish":
            self.usage = ev.get("usage")
            out += self._close()
            stop = {"stop": "end_turn", "tools": "tool_use",
                    "max_tokens": "max_tokens", "error": "end_turn"}.get(ev["reason"], "end_turn")
            u = self.usage or {}
            out.append({"type": "message_delta", "index": 0,
                        "delta": {"stop_reason": stop, "stop_sequence": None},
                        "usage": {"input_tokens": u.get("in", 0),
                                  "output_tokens": u.get("out", 0)}})
        elif t == "end":
            out.append({"type": "message_stop"})
        return out


def sse(obj) -> bytes:
    return b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n\n"


def anthropic_sse(obj) -> bytes:
    return sse(obj)
