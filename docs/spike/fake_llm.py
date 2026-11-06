"""Tiny fake OpenAI-compatible server. Logs every request (path, model, whether the
private marker is present) to requests.jsonl and replies with a fixed answer."""
import json, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = sys.argv[2] if len(sys.argv) > 2 else "requests.jsonl"
MARK = "SPIKE-PRIVATE-MARKER"

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        self._send(200, {"object": "list", "data": [{"id": m, "object": "model"} for m in ("cfg-model", "chat-model", "other-model")]})
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        text = json.dumps(body.get("messages", []))
        is_check = "spike-check" in text
        with open(LOG, "a") as f:
            f.write(json.dumps({"path": self.path, "model": body.get("model"), "stream": body.get("stream"),
                                "has_private_marker": MARK in text, "is_plugin_llm_check": is_check,
                                "n_messages": len(body.get("messages", []))}) + "\n")
        answer = "yes" if is_check else "Hello from the fake model."
        model = body.get("model") or "unknown"
        if body.get("stream"):
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            for chunk in ({"role": "assistant", "content": answer}, {}):
                d = {"id": "x", "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
                     "choices": [{"index": 0, "delta": chunk, "finish_reason": None if chunk else "stop"}]}
                self.wfile.write(f"data: {json.dumps(d)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n"); return
        self._send(200, {"id": "x", "object": "chat.completion", "created": int(time.time()), "model": model,
                         "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
