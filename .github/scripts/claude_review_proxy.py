"""Job-local Anthropic transport. The real provider key never enters the sandbox."""

import hmac
import http.client
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


MAX_REQUEST_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_REQUESTS = 64
MAX_OUTPUT_TOKENS = 65536
LIFETIME_SECONDS = 15 * 60


class ReviewServer(ThreadingHTTPServer):
    """Host-enforced exposure limits, independent of the CLI's dollar budget."""

    def __init__(self, provider_key, proxy_token, model):
        super().__init__(("127.0.0.1", 0), Handler)
        self.provider_key = provider_key
        self.proxy_token = proxy_token
        self.model = model
        self.deadline = time.monotonic() + LIFETIME_SECONDS
        self.remaining_requests = MAX_REQUESTS
        self.remaining_bytes = MAX_TOTAL_BYTES
        self.budget_lock = threading.Lock()

    def reserve(self, length):
        # Charge attempts, including count_tokens and upstream failures. Never
        # refund: concurrent requests and retries must not multiply the ceiling.
        with self.budget_lock:
            if time.monotonic() >= self.deadline:
                return 403
            if self.remaining_requests <= 0 or length > self.remaining_bytes:
                return 429
            self.remaining_requests -= 1
            self.remaining_bytes -= length
        return None


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *_args):
        pass  # Do not log model data, headers or upstream error bodies.

    def do_POST(self):
        token = self.headers.get("x-api-key", "")
        if not hmac.compare_digest(token, self.server.proxy_token):
            self.send_error(403)
            return
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or parsed.path not in (
            "/v1/messages",
            "/v1/messages/count_tokens",
        ):
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400)
            return
        if not 0 < length <= MAX_REQUEST_BYTES or self.headers.get("Transfer-Encoding"):
            self.send_error(413)
            return
        if status := self.server.reserve(length):
            self.send_error(status, "Review proxy limit reached")
            return
        body = self.rfile.read(length)
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError, RecursionError):
            self.send_error(400, "Invalid review request")
            return
        if not isinstance(payload, dict) or payload.get("model") != self.server.model:
            self.send_error(403, "Review model not allowed")
            return
        if parsed.path == "/v1/messages":
            max_tokens = payload.get("max_tokens")
            if type(max_tokens) is not int or not 0 < max_tokens <= MAX_OUTPUT_TOKENS:
                self.send_error(400, "Review output limit exceeded")
                return
        # Reading a slow body must not authorize a call beyond the deadline.
        if time.monotonic() >= self.server.deadline:
            self.send_error(403, "Review proxy expired")
            return
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.server.provider_key,
            "anthropic-version": self.headers.get("anthropic-version", "2023-06-01"),
        }
        if beta := self.headers.get("anthropic-beta"):
            headers["anthropic-beta"] = beta
        # Fixed HTTPS origin, normal certificate validation, no redirects and no
        # environment proxy settings. Only the ephemeral local token is supplied
        # to Claude; it is useful solely for this loopback listener's lifetime.
        upstream = http.client.HTTPSConnection("api.anthropic.com", timeout=120)
        started = False
        try:
            upstream.request("POST", self.path, body=body, headers=headers)
            response = upstream.getresponse()
            self.send_response(response.status)
            # Never reflect upstream header bytes through send_header: it does
            # not reject CR/LF. The model API needs only JSON and SSE, so emit
            # a constant media type and discard all upstream parameters.
            media_type = response.getheader("Content-Type", "").split(";", 1)[0]
            if media_type.strip().lower() == "text/event-stream":
                self.send_header("Content-Type", "text/event-stream")
            else:
                self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            started = True
            while chunk := response.read1(65536):
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, http.client.HTTPException):
            if not started:
                self.send_error(502, "Model transport failed")
        finally:
            upstream.close()
            self.close_connection = True


if __name__ == "__main__":
    server = ReviewServer(os.environ["ANTHROPIC_API_KEY"], sys.argv[2], sys.argv[3])
    # Stop even if the sandbox keeps a stream or socket alive. Handler threads
    # are daemon threads; exiting the host process closes their connections.
    expiry = threading.Timer(LIFETIME_SECONDS, server.shutdown)
    expiry.daemon = True
    expiry.start()
    Path(sys.argv[1]).write_text(str(server.server_port))
    try:
        server.serve_forever()
    finally:
        expiry.cancel()
        server.server_close()
