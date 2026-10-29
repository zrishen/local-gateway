#!/usr/bin/env python3
"""Responses -> Chat Completions protocol bridge for compatible upstreams.

The bridge translates Codex Responses requests into Chat Completions requests
and converts streamed chunks back into Responses SSE events. It avoids sending
OpenAI account headers to a third-party upstream.
"""

import json
import time
import uuid

DROP_ITEM_TYPES = {"reasoning"}          # gemini 不返回思考内容，直接跳过


def _now():
    return int(time.time())


def _sse(event_type, payload):
    return ("event: %s\ndata: %s\n\n" % (event_type, json.dumps(payload, ensure_ascii=False))).encode("utf-8")


# ---------------------------------------------------------------- 请求方向

def _content_to_text(content):
    """Extract text for instructions and legacy text-only messages."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for c in content:
        if not isinstance(c, dict):
            continue
        if c.get("type") in ("input_text", "output_text", "text") and c.get("text"):
            parts.append(c["text"])
    return "\n".join(parts)


def _content_to_chat(content):
    """Preserve image URLs/data URLs and their order in multimodal messages."""
    if not isinstance(content, list) or not any(
            isinstance(c, dict) and c.get('type') in ('input_image', 'image_url')
            for c in content):
        return _content_to_text(content)
    parts = []
    for c in content:
        if not isinstance(c, dict):
            continue
        if c.get('type') in ('input_text', 'output_text', 'text') and c.get('text'):
            parts.append({'type': 'text', 'text': c['text']})
        elif c.get('type') in ('input_image', 'image_url'):
            source = c.get('image_url')
            image = dict(source) if isinstance(source, dict) else {'url': source}
            if image.get('url'):
                if c.get('detail') in ('auto', 'low', 'high'):
                    image['detail'] = c['detail']
                parts.append({'type': 'image_url', 'image_url': image})
    return parts


def build_chat_request(rbody, clean_schema=None):
    """Responses 请求体 -> chat/completions 请求体"""
    messages = []

    instr = rbody.get("instructions")
    if isinstance(instr, str) and instr.strip():
        messages.append({"role": "system", "content": instr})
    elif isinstance(instr, list):
        txt = _content_to_text(instr)
        if txt:
            messages.append({"role": "system", "content": txt})

    inputs = rbody.get("input") or []
    if isinstance(inputs, str):
        inputs = [inputs]
    for item in inputs:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "message" or (itype is None and item.get('role')):
            role = item.get("role") or "user"
            text = _content_to_chat(item.get("content"))
            if role == "developer":
                role = "system"
            messages.append({"role": role, "content": text})
        elif itype == "function_call":
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": item.get("call_id") or item.get("id") or "call_0",
                    "type": "function",
                    "function": {"name": item.get("name"), "arguments": item.get("arguments") or "{}"},
                }],
            })
        elif itype == "function_call_output":
            out = item.get("output")
            if not isinstance(out, str):
                out = json.dumps(out, ensure_ascii=False)
            messages.append({"role": "tool", "tool_call_id": item.get("call_id") or "", "content": out})
        elif itype in DROP_ITEM_TYPES:
            continue
        else:
            text = item.get("text") or _content_to_text(item.get("content"))
            if text:
                messages.append({"role": "user", "content": text})

    tools = []
    for t in rbody.get("tools") or []:
        if not isinstance(t, dict):
            continue
        if t.get("type") not in (None, "function"):
            continue
        name = t.get("name") or (t.get("function") or {}).get("name")
        if not name:
            continue
        params = t.get("parameters") or (t.get("function") or {}).get("parameters") or {"type": "object", "properties": {}}
        if clean_schema is not None:
            params = clean_schema(params, [0])
        function = {
            "name": name,
            "description": t.get("description") or (t.get("function") or {}).get("description") or "",
            "parameters": params,
        }
        strict = t.get('strict', (t.get('function') or {}).get('strict'))
        if strict is not None:
            function['strict'] = strict
        tools.append({'type': 'function', 'function': function})

    chat = {
        "model": rbody.get("model"),
        "messages": messages,
        "stream": True,
        "max_tokens": rbody.get("max_output_tokens") or rbody.get("max_tokens") or 32000,
    }
    if tools:
        chat["tools"] = tools
        tc = rbody.get("tool_choice")
        if isinstance(tc, str) and tc in ("auto", "none", "required"):
            chat["tool_choice"] = tc
        elif isinstance(tc, dict) and tc.get("type") == "function" and tc.get("name"):
            chat["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
    for k in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
        if rbody.get(k) is not None:
            chat[k] = rbody[k]
    effort = (rbody.get('reasoning') or {}).get('effort')
    if effort is not None:
        chat['reasoning_effort'] = effort
    if 'parallel_tool_calls' in rbody:
        chat['parallel_tool_calls'] = rbody['parallel_tool_calls']
    format_spec = (rbody.get('text') or {}).get('format') or {}
    if format_spec.get('type') == 'json_schema':
        chat['response_format'] = {'type': 'json_schema', 'json_schema': {
            k: v for k, v in format_spec.items() if k != 'type'}}
    elif format_spec.get('type') == 'json_object':
        chat['response_format'] = {'type': 'json_object'}
    return chat


# ---------------------------------------------------------------- 响应方向

class _StreamState:
    def __init__(self, model, instructions):
        self.resp_id = str(uuid.uuid4())
        self.created = _now()
        self.model = model
        self.instructions = instructions
        self.seq = 0
        self.output = []          # 已完成/进行中的 item
        self.text_item = None     # {id, text, index}
        self.tools = {}           # index -> {id, name, args, call_id, out_index}
        self.next_index = 0
        self.usage = None
        self.finish_reason = None

    def n(self):
        v = self.seq
        self.seq += 1
        return v

    def response_obj(self, status):
        return {
            "id": self.resp_id, "object": "response", "created_at": self.created,
            "status": status, "background": False,
            "completed_at": _now() if status == "completed" else None,
            "error": None, "incomplete_details": None, "instructions": self.instructions,
            "max_output_tokens": None, "max_tool_calls": None, "model": self.model,
            "output": self.output, "parallel_tool_calls": True,
            "previous_response_id": None, "prompt_cache_key": None, "prompt_cache_retention": None,
            "reasoning": {"effort": None, "summary": None}, "service_tier": "default",
            "store": False, "temperature": 1.0, "text": {"format": {"type": "text"}, "verbosity": None},
            "tool_choice": "auto", "tools": [], "top_p": 1.0, "truncation": "disabled",
            "usage": self.usage, "metadata": {},
        }


def stream_events(chat_chunks, model, instructions):
    """chat SSE 数据块（dict）迭代器 -> Responses SSE 字节流"""
    st = _StreamState(model, instructions)

    yield _sse("response.created", {"type": "response.created", "response": st.response_obj("in_progress"), "sequence_number": st.n()})
    yield _sse("response.in_progress", {"type": "response.in_progress", "response": st.response_obj("in_progress"), "sequence_number": st.n()})

    for chunk in chat_chunks:
        if not isinstance(chunk, dict):
            continue
        if chunk.get("usage"):
            u = chunk["usage"]
            st.usage = {
                "input_tokens": u.get("prompt_tokens", 0),
                "input_tokens_details": {"cached_tokens": (u.get('prompt_tokens_details') or {}).get('cached_tokens', 0)},
                "output_tokens": u.get("completion_tokens", 0),
                "output_tokens_details": {"reasoning_tokens": (u.get('completion_tokens_details') or {}).get('reasoning_tokens', 0)},
                "total_tokens": u.get("total_tokens", 0),
            }
        choices = chunk.get("choices") or []
        if not choices:
            continue
        ch = choices[0]
        delta = ch.get("delta") or {}
        if ch.get("finish_reason"):
            st.finish_reason = ch["finish_reason"]

        text = delta.get("content")
        if text:
            if st.text_item is None:
                item_id = str(uuid.uuid4())
                st.text_item = {"id": item_id, "text": "", "index": st.next_index}
                st.next_index += 1
                yield _sse("response.output_item.added", {
                    "type": "response.output_item.added",
                    "item": {"type": "message", "id": item_id, "status": "in_progress",
                             "content": [], "phase": "final_answer", "role": "assistant"},
                    "output_index": st.text_item["index"], "sequence_number": st.n()})
                yield _sse("response.content_part.added", {
                    "type": "response.content_part.added", "content_index": 0, "item_id": item_id,
                    "output_index": st.text_item["index"],
                    "part": {"type": "output_text", "annotations": [], "logprobs": [], "text": ""},
                    "sequence_number": st.n()})
            st.text_item["text"] += text
            yield _sse("response.output_text.delta", {
                "type": "response.output_text.delta", "content_index": 0, "delta": text,
                "item_id": st.text_item["id"], "logprobs": [], "output_index": st.text_item["index"],
                "sequence_number": st.n()})

        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index", 0)
            slot = st.tools.get(idx)
            fn = tc.get("function") or {}
            if slot is None:
                item_id = str(uuid.uuid4())
                slot = {"id": item_id, "name": fn.get("name") or tc.get("name") or "",
                        "args": "", "call_id": tc.get("id") or ("call_%d_%s" % (idx, uuid.uuid4().hex[:8])),
                        "index": st.next_index}
                st.next_index += 1
                st.tools[idx] = slot
                yield _sse("response.output_item.added", {
                    "type": "response.output_item.added",
                    "item": {"type": "function_call", "id": item_id, "status": "in_progress",
                             "arguments": "", "call_id": slot["call_id"], "name": slot["name"]},
                    "output_index": slot["index"], "sequence_number": st.n()})
            if fn.get("name") and not slot["name"]:
                slot["name"] = fn["name"]
            piece = fn.get("arguments")
            if piece:
                slot["args"] += piece
                yield _sse("response.function_call_arguments.delta", {
                    "type": "response.function_call_arguments.delta", "delta": piece,
                    "item_id": slot["id"], "output_index": slot["index"], "sequence_number": st.n()})

    # ---- 收尾 ----
    if st.finish_reason not in ('stop', 'tool_calls', 'function_call', 'length', 'content_filter'):
        response = st.response_obj('failed')
        response['error'] = {'code': 'upstream_stream_incomplete',
                             'message': 'Upstream stream ended without a completion marker'}
        yield _sse('response.failed', {'type': 'response.failed', 'response': response,
                                       'sequence_number': st.n()})
        return
    item_status = 'incomplete' if st.finish_reason in ('length', 'content_filter') else 'completed'
    if st.text_item is not None:
        item_id, text, idx = st.text_item["id"], st.text_item["text"], st.text_item["index"]
        yield _sse("response.output_text.done", {
            "type": "response.output_text.done", "content_index": 0, "item_id": item_id,
            "logprobs": [], "output_index": idx, "sequence_number": st.n(), "text": text})
        yield _sse("response.content_part.done", {
            "type": "response.content_part.done", "content_index": 0, "item_id": item_id,
            "output_index": idx,
            "part": {"type": "output_text", "annotations": [], "logprobs": [], "text": text},
            "sequence_number": st.n()})
        item = {"type": "message", "id": item_id, "status": item_status,
                "content": [{"type": "output_text", "annotations": [], "logprobs": [], "text": text}],
                "phase": "final_answer", "role": "assistant"}
        st.output.append(item)
        yield _sse("response.output_item.done", {
            "type": "response.output_item.done", "item": item, "output_index": idx, "sequence_number": st.n()})

    for idx in sorted(st.tools):
        slot = st.tools[idx]
        if item_status != 'completed':
            # Never present truncated arguments as an executable tool call.
            continue
        yield _sse("response.function_call_arguments.done", {
            "type": "response.function_call_arguments.done", "arguments": slot["args"],
            "item_id": slot["id"], "output_index": slot["index"], "sequence_number": st.n()})
        item = {"type": "function_call", "id": slot["id"], "status": "completed",
                "arguments": slot["args"], "call_id": slot["call_id"], "name": slot["name"]}
        st.output.append(item)
        yield _sse("response.output_item.done", {
            "type": "response.output_item.done", "item": item, "output_index": slot["index"],
            "sequence_number": st.n()})

    indices = {slot['id']: slot['index'] for slot in st.tools.values()}
    if st.text_item:
        indices[st.text_item['id']] = st.text_item['index']
    st.output.sort(key=lambda item: indices[item['id']])

    status, reason = 'completed', None
    if st.finish_reason in ('length', 'content_filter'):
        status = 'incomplete'
        reason = 'max_output_tokens' if st.finish_reason == 'length' else 'content_filter'
    elif st.finish_reason not in ('stop', 'tool_calls', 'function_call'):
        status, reason = 'failed', 'upstream_stream_incomplete'
    response = st.response_obj(status)
    if status == 'incomplete':
        response['incomplete_details'] = {'reason': reason}
    elif status == 'failed':
        response['error'] = {'code': reason, 'message': 'Upstream stream ended without a completion marker'}
    event = 'response.' + status
    yield _sse(event, {'type': event, 'response': response, 'sequence_number': st.n()})
