"""A tiny fake OpenAI-compatible model server for the end-to-end checks. Local only.

- Logs every request to a JSON-lines file: path, model, number of messages and the full text.
  The checks only ever use made-up memories, so logging the text is fine here.
- Answers with a fixed sentence, or, when the newest user message holds lines like
  ``[[call whos-listening_remember {"text": "Likes spicy food", "tier": "shared"}]]``, asks for those tool
  calls in order, one per step, and answers after the last result comes back. That lets a check drive the
  memory tools without a real model.

Usage: python fake_llm.py PORT LOGFILE
"""

from __future__ import annotations

import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CALL_RE = re.compile(r"\[\[call ([\w-]+) (\{.*?\})\]\]", re.DOTALL)
ANSWER = "Hello from the fake model."
MODELS = ("private-model", "shared-model")


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return ""


def plan_reply(messages: list) -> dict:
    """What the fake model says next: the next scripted tool call, or plain text.

    The newest user message may script several ``[[call ...]]`` lines; they run in order, one
    per step, counting the tool results that came back since that message."""
    last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    if last_user < 0:
        return {"text": ANSWER}
    calls = CALL_RE.findall(_text(messages[last_user].get("content")))
    done = sum(1 for m in messages[last_user + 1:] if m.get("role") == "tool")
    if done < len(calls):
        name, args = calls[done]
        return {"tool": name, "args": args}
    if done:
        return {"text": "Done. " + _text(messages[-1].get("content"))[:300]}
    return {"text": ANSWER}


CHECK_RE = re.compile(r"<<memory>>\n(.*?)\n<</memory>>.*?<<user-messages>>\n(.*?)\n<</user-messages>>", re.DOTALL)


def side_reply(messages: list) -> str:
    """A request without tools: a title, or whos-listening's Auto-review check. The fake check
    says yes only if the memory's text appears in the user's own words (scripted calls removed)."""
    text = " ".join(_text(m.get("content")) for m in messages)
    found = CHECK_RE.search(text)
    if "You check one memory" in text and found:
        memory, users = found.group(1).strip().lower(), CALL_RE.sub("", found.group(2)).lower()
        if memory and memory in users:
            if "Target tier: shared" in text:
                may_share = any(phrase in users for phrase in
                                ("any model may see", "can be shared", "may be shared with any model"))
                return json.dumps({"save": True, "share": may_share, "reason": "You said this yourself."})
            return '{"save": true, "reason": "You said this yourself."}'
        return '{"save": false, "reason": "You never said this."}'
    return ANSWER


class Handler(BaseHTTPRequestHandler):
    log_path = "requests.jsonl"

    def log_message(self, *_):
        pass

    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._json(200, {"object": "list", "data": [{"id": m, "object": "model"} for m in MODELS]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        messages = body.get("messages") or []
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"path": self.path, "model": body.get("model"), "n_messages": len(messages),
                                "has_tools": bool(body.get("tools")),
                                "text": json.dumps(body, ensure_ascii=False, default=str)}) + "\n")
        reply = plan_reply(messages) if body.get("tools") else {"text": side_reply(messages)}
        model = body.get("model") or "unknown"
        stamp = int(time.time())
        if "tool" in reply:
            call = {"index": 0, "id": f"call_{stamp}", "type": "function",
                    "function": {"name": reply["tool"], "arguments": reply["args"]}}
            message, finish = {"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls"
        else:
            message, finish = {"role": "assistant", "content": reply["text"]}, "stop"
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for delta, fin in ((message, None), ({}, finish)):
                chunk = {"id": "x", "object": "chat.completion.chunk", "created": stamp, "model": model,
                         "choices": [{"index": 0, "delta": delta, "finish_reason": fin}]}
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return
        self._json(200, {"id": "x", "object": "chat.completion", "created": stamp, "model": model,
                         "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                         "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})


def main() -> None:
    Handler.log_path = sys.argv[2] if len(sys.argv) > 2 else "requests.jsonl"
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()


if __name__ == "__main__":
    main()
