"""A tiny fake of GitHub's Contents API for the end-to-end sync checks. Local only.

- ``GET  /repos/<owner>/<repo>/contents/<path>``: the file (base64) and its ``sha``, or 404.
- ``PUT  /repos/<owner>/<repo>/contents/<path>``: write it. Like GitHub, a write must name the
  ``sha`` it read (409 when it is stale, 422 when a new file is written with one), and it needs a
  bearer token (401 without).
- Every accepted write is logged, decoded, to a JSON-lines file, so a check can read exactly what
  left a gateway. The checks only ever use made-up memories.

Usage: python fake_github.py PORT LOGFILE
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

FILES: dict[str, tuple[bytes, str]] = {}
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    log_path = "github.jsonl"

    def log_message(self, *_):
        pass

    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _key(self) -> str:
        path = unquote(urlsplit(self.path).path)
        return path if path.startswith("/repos/") and "/contents/" in path else ""

    def do_GET(self):
        key = self._key()
        with LOCK:
            found = FILES.get(key)
        if not key or found is None:
            return self._json(404, {"message": "Not Found"})
        raw, sha = found
        self._json(200, {"type": "file", "encoding": "base64", "size": len(raw), "sha": sha,
                         "content": base64.encodebytes(raw).decode()})

    def do_PUT(self):
        key = self._key()
        if not key:
            return self._json(404, {"message": "Not Found"})
        if not self.headers.get("Authorization", "").startswith("Bearer "):
            return self._json(401, {"message": "Bad credentials"})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        raw = base64.b64decode(body.get("content") or "")
        with LOCK:
            current = FILES.get(key)
            if current and body.get("sha") != current[1]:
                return self._json(409, {"message": f"{key} does not match {body.get('sha')}"})
            if not current and body.get("sha"):
                return self._json(422, {"message": "sha was supplied for a new file"})
            sha = hashlib.sha1(raw + str(len(FILES)).encode() + (current[1].encode() if current else b"")).hexdigest()
            FILES[key] = (raw, sha)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"path": key, "message": body.get("message"),
                                    "text": raw.decode("utf-8", "replace")}) + "\n")
        self._json(201 if not current else 200, {"content": {"sha": sha}})


def main() -> None:
    Handler.log_path = sys.argv[2] if len(sys.argv) > 2 else "github.jsonl"
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()


if __name__ == "__main__":
    main()
