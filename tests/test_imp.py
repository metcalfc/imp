"""Tests for imp.

The interesting claims imp makes are security claims: the sprite's capability
is not a credential, the path allowlist holds before the real token is
attached, and the framed tunnel is 8-bit clean. Those are what is tested here.

Stdlib only, matching the tool itself. Run: python3 -m unittest discover tests
"""

import base64
import importlib.util
import io
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_imp():
    """`imp` has no .py extension, so import it by path.

    The stdlib had a module called `imp` until 3.12; register ours under a
    distinct name so nothing resolves to the wrong one.
    """
    spec = importlib.util.spec_from_loader(
        "imp_tool",
        importlib.machinery.SourceFileLoader("imp_tool", os.path.join(ROOT, "imp")),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["imp_tool"] = mod
    spec.loader.exec_module(mod)
    return mod


imp = load_imp()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# --------------------------------------------------------------------------
# request-target normalization
# --------------------------------------------------------------------------

class TestNormalizePath(unittest.TestCase):
    def test_plain_path_survives(self):
        self.assertEqual(imp.normalize_path("/v1/messages"), "/v1/messages")

    def test_query_and_fragment_are_dropped(self):
        self.assertEqual(imp.normalize_path("/v1/models?limit=5"), "/v1/models")
        self.assertEqual(imp.normalize_path("/v1/models#x"), "/v1/models")

    def test_traversal_is_refused(self):
        # The whole point: this would walk past a prefix check.
        self.assertIsNone(imp.normalize_path("/v1/messages/../../v1/organizations"))
        self.assertIsNone(imp.normalize_path("/v1/./messages"))
        self.assertIsNone(imp.normalize_path("/v1//messages"))

    def test_relative_target_is_refused(self):
        self.assertIsNone(imp.normalize_path("v1/messages"))

    def test_absolute_url_target_is_refused(self):
        self.assertIsNone(imp.normalize_path("https://evil.example/v1/messages"))

    def test_backslash_is_refused(self):
        self.assertIsNone(imp.normalize_path("/v1\\messages"))

    def test_trailing_slash_is_tolerated(self):
        self.assertEqual(imp.normalize_path("/v1/models/"), "/v1/models")

    def test_percent_encoded_traversal_is_refused(self):
        # %2e%2e survives posixpath.normpath untouched, so a check that does
        # not decode first sees nothing wrong while the far end sees `..`.
        self.assertIsNone(
            imp.normalize_path("/v1/models/%2e%2e/%2e%2e/v1/organizations"))
        self.assertIsNone(imp.normalize_path("/v1/models/%2E%2E/x"))
        self.assertIsNone(imp.normalize_path("/v1/models/%2e%2e%2fx/y"))

    def test_double_encoded_traversal_is_refused(self):
        self.assertIsNone(
            imp.normalize_path("/v1/models/%252e%252e/v1/organizations"))

    def test_encoded_separator_is_refused(self):
        self.assertIsNone(imp.normalize_path("/v1/models%2f..%2fv1/api_keys"))

    def test_control_bytes_are_refused(self):
        self.assertIsNone(imp.normalize_path("/v1/mess%00ages"))
        self.assertIsNone(imp.normalize_path("/v1/messages%0d%0aX-Evil:%20y"))

    def test_benign_encoding_round_trips(self):
        self.assertEqual(imp.normalize_path("/v1/models/a%20b"), "/v1/models/a%20b")


# --------------------------------------------------------------------------
# the allowlist
# --------------------------------------------------------------------------

class TestPathAllowed(unittest.TestCase):
    def setUp(self):
        self.default = imp.DEFAULT_ALLOWED

    def test_inference_paths_are_allowed(self):
        for p in ("/v1/messages", "/v1/messages/count_tokens", "/v1/models",
                  "/v1/models/claude-opus-4-20250514"):
            self.assertTrue(imp.path_allowed(p, self.default), p)

    def test_account_paths_are_refused(self):
        for p in ("/v1/organizations/me", "/v1/api_keys", "/v1/messages/batches",
                  "/v1/organizations/me/api_keys"):
            self.assertFalse(imp.path_allowed(p, self.default), p)

    def test_extra_allow_glob_widens_it(self):
        widened = self.default + ("/v1/messages/batches*",)
        self.assertTrue(imp.path_allowed("/v1/messages/batches", widened))
        self.assertTrue(imp.path_allowed("/v1/messages/batches_x", widened))
        self.assertFalse(imp.path_allowed("/v1/api_keys", widened))

    def test_star_does_not_cross_a_path_separator(self):
        # /v1/models/* means one model id, not everything underneath it.
        self.assertTrue(imp.path_allowed("/v1/models/claude-opus-4", self.default))
        self.assertFalse(imp.path_allowed("/v1/models/a/b", self.default))
        self.assertFalse(imp.path_allowed("/v1/models/anything/at/all", self.default))

    def test_double_star_spans_segments_when_asked_for(self):
        widened = self.default + ("/v1/messages/batches/**",)
        self.assertTrue(imp.path_allowed("/v1/messages/batches/a/b", widened))
        self.assertFalse(imp.path_allowed("/v1/api_keys", widened))

    def test_empty_patterns_means_allow_any_path(self):
        self.assertTrue(imp.path_allowed("/v1/organizations/me", ()))

    def test_matching_is_case_sensitive(self):
        # fnmatchcase, not fnmatch -- otherwise macOS's case-insensitive
        # instincts would let /V1/MESSAGES through.
        self.assertFalse(imp.path_allowed("/V1/Messages", self.default))


# --------------------------------------------------------------------------
# credential extraction
# --------------------------------------------------------------------------

class TestExtractToken(unittest.TestCase):
    def test_bare_token(self):
        self.assertEqual(imp._extract_token("  sk-ant-oat01-abc  "), "sk-ant-oat01-abc")

    def test_nested_access_token(self):
        blob = json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat01-xyz",
                                             "refreshToken": "nope"}})
        self.assertEqual(imp._extract_token(blob), "sk-ant-oat01-xyz")

    def test_access_token_inside_a_list(self):
        blob = json.dumps({"accounts": [{"accessToken": "tok-1"}]})
        self.assertEqual(imp._extract_token(blob), "tok-1")

    def test_json_without_an_access_token(self):
        self.assertIsNone(imp._extract_token(json.dumps({"refreshToken": "r"})))

    def test_empty(self):
        self.assertIsNone(imp._extract_token("   "))


# --------------------------------------------------------------------------
# the injecting proxy -- against a stand-in upstream
# --------------------------------------------------------------------------

class CaseInsensitiveHeaders(dict):
    """urllib title-cases header names on the way out, so a plain dict lookup
    for "anthropic-beta" would miss "Anthropic-Beta"."""

    def __init__(self, message):
        super().__init__((k.lower(), v) for k, v in message.items())
        self.original_names = [k for k, _ in message.items()]

    def get(self, key, default=None):
        return super().get(key.lower(), default)

    def __getitem__(self, key):
        return super().__getitem__(key.lower())

    def __contains__(self, key):
        return super().__contains__(key.lower())


class FakeUpstream(object):
    """Stands in for api.anthropic.com and records what actually arrived."""

    def __init__(self):
        self.requests = []          # (method, path, headers dict, body bytes)
        self.status = 200
        self.body = b'{"ok":true}'
        self.sse = None             # list of chunks to stream instead
        self.fail_until_token = None  # 401 unless Authorization matches this
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _handle(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                outer.requests.append(
                    (self.command, self.path, CaseInsensitiveHeaders(self.headers),
                     body))

                if (outer.fail_until_token is not None
                        and self.headers.get("Authorization")
                        != "Bearer " + outer.fail_until_token):
                    payload = b'{"type":"error"}'
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return

                if outer.sse is not None:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    for c in outer.sse:
                        self.wfile.write(b"%x\r\n" % len(c) + c + b"\r\n")
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                    return

                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(outer.body)))
                self.end_headers()
                self.wfile.write(outer.body)

            do_GET = do_POST = do_PUT = do_DELETE = _handle

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.port

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakeCredential(object):
    def __init__(self, token="sk-ant-oat01-REAL", rotate_to=None):
        self.token = token
        self.source = "test"
        self.rotate_to = rotate_to
        self.refreshes = 0

    def refresh(self):
        self.refreshes += 1
        if self.rotate_to is None:
            return False
        self.token, self.rotate_to = self.rotate_to, None
        return True


CAPABILITY = "test-capability-string"


class ProxyTestCase(unittest.TestCase):
    """Boots a real imp proxy in front of a fake upstream."""

    def setUp(self):
        self.upstream = FakeUpstream()
        self._real_upstream = imp.UPSTREAM
        imp.UPSTREAM = self.upstream.url
        self.cred = FakeCredential()
        self.allowed = imp.DEFAULT_ALLOWED
        self.server = None

    def start(self, allowed=None, verbose=False):
        handler = imp.make_proxy_handler(
            self.cred, CAPABILITY, verbose,
            self.allowed if allowed is None else allowed)
        self.server = imp.ProxyServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self.port

    def tearDown(self):
        imp.UPSTREAM = self._real_upstream
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        self.upstream.close()

    def request(self, path="/v1/messages", method="POST", auth=None,
                body=b'{"model":"x"}', headers=None):
        """Raw HTTP so we control the exact bytes on the wire."""
        if auth is None:
            auth = "Bearer " + CAPABILITY
        hdrs = {"Host": "api.anthropic.com", "Content-Type": "application/json"}
        if auth is not False:
            hdrs["Authorization"] = auth
        # Default to the true length, but let a test declare a lying one --
        # that is exactly the input the bounds are there to survive.
        hdrs["Content-Length"] = str(len(body))
        hdrs.update(headers or {})

        raw = ("%s %s HTTP/1.1\r\n" % (method, path)).encode()
        raw += b"".join(("%s: %s\r\n" % (k, v)).encode() for k, v in hdrs.items())
        raw += b"\r\n" + body

        conn = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        conn.sendall(raw)
        chunks = []
        conn.settimeout(10)
        try:
            while True:
                b = conn.recv(65536)
                if not b:
                    break
                chunks.append(b)
                # Enough to read the status line and headers; stop once the
                # terminating chunk arrives so we do not block on keep-alive.
                blob = b"".join(chunks)
                if b"\r\n\r\n" in blob and (blob.endswith(b"0\r\n\r\n")
                                            or b"Content-Length" in blob.split(b"\r\n\r\n")[0]):
                    break
        except socket.timeout:
            pass
        conn.close()
        blob = b"".join(chunks)
        head, _, rest = blob.partition(b"\r\n\r\n")
        status = int(head.split(b"\r\n")[0].split()[1])
        return status, head, rest


class TestCapabilityEnforcement(ProxyTestCase):
    def test_correct_capability_reaches_upstream(self):
        self.start()
        status, _, _ = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(len(self.upstream.requests), 1)

    def test_wrong_capability_is_refused_and_never_reaches_upstream(self):
        self.start()
        status, _, _ = self.request(auth="Bearer not-the-capability")
        self.assertEqual(status, 403)
        self.assertEqual(self.upstream.requests, [],
                         "upstream must not be called on a bad capability")

    def test_missing_authorization_is_refused(self):
        self.start()
        status, _, _ = self.request(auth=False)
        self.assertEqual(status, 403)
        self.assertEqual(self.upstream.requests, [])

    def test_capability_accepted_via_x_api_key(self):
        self.start()
        status, _, _ = self.request(auth=False,
                                    headers={"x-api-key": CAPABILITY})
        self.assertEqual(status, 200)

    def test_bare_capability_without_bearer_prefix(self):
        self.start()
        status, _, _ = self.request(auth=CAPABILITY)
        self.assertEqual(status, 200)


class TestTokenInjection(ProxyTestCase):
    def test_real_token_replaces_the_capability(self):
        self.start()
        self.request()
        _, _, hdrs, _ = self.upstream.requests[0]
        self.assertEqual(hdrs.get("Authorization"), "Bearer " + self.cred.token)

    def test_capability_never_reaches_upstream(self):
        self.start()
        self.request(headers={"x-api-key": CAPABILITY})
        _, _, hdrs, body = self.upstream.requests[0]
        blob = (json.dumps(dict(hdrs)) + body.decode()).lower()
        self.assertNotIn(CAPABILITY.lower(), blob)
        self.assertNotIn("x-api-key", hdrs)

    def test_required_betas_are_added(self):
        self.start()
        self.request(headers={"anthropic-beta": "claude-code-20250219"})
        _, _, hdrs, _ = self.upstream.requests[0]
        betas = hdrs.get("anthropic-beta", "").split(",")
        # Without oauth-2025-04-20 the Max entitlement is not honoured.
        self.assertIn("oauth-2025-04-20", betas)
        self.assertIn("claude-code-20250219", betas)

    def test_betas_are_not_duplicated(self):
        self.start()
        self.request(headers={
            "anthropic-beta": "oauth-2025-04-20,claude-code-20250219"})
        _, _, hdrs, _ = self.upstream.requests[0]
        betas = hdrs.get("anthropic-beta", "").split(",")
        self.assertEqual(len(betas), len(set(betas)))
        self.assertEqual(sorted(betas),
                         sorted(["oauth-2025-04-20", "claude-code-20250219"]))

    def test_betas_are_added_when_the_sprite_sends_none(self):
        self.start()
        self.request()
        _, _, hdrs, _ = self.upstream.requests[0]
        betas = hdrs.get("anthropic-beta", "").split(",")
        for b in imp.REQUIRED_BETAS:
            self.assertIn(b, betas)

    def test_body_is_forwarded_verbatim(self):
        self.start()
        payload = json.dumps({"model": "x", "big": "y" * 50000}).encode()
        self.request(body=payload)
        _, _, _, body = self.upstream.requests[0]
        self.assertEqual(body, payload)


class TestAllowlistOverTheWire(ProxyTestCase):
    def test_disallowed_path_is_refused_before_the_token_is_attached(self):
        self.start()
        status, _, _ = self.request(path="/v1/organizations/me")
        self.assertEqual(status, 403)
        self.assertEqual(self.upstream.requests, [])

    def test_traversal_is_a_400(self):
        self.start()
        status, _, _ = self.request(path="/v1/messages/../../v1/organizations")
        self.assertEqual(status, 400)
        self.assertEqual(self.upstream.requests, [])

    def test_allowlist_is_checked_before_the_capability(self):
        # A bad path must not be a capability oracle.
        self.start()
        status, _, _ = self.request(path="/v1/api_keys", auth="Bearer wrong")
        self.assertEqual(status, 403)
        self.assertEqual(self.upstream.requests, [])

    def test_widened_allowlist_lets_the_extra_path_through(self):
        self.start(allowed=imp.DEFAULT_ALLOWED + ("/v1/messages/batches*",))
        status, _, _ = self.request(path="/v1/messages/batches")
        self.assertEqual(status, 200)

    def test_allow_any_path_lets_everything_through(self):
        self.start(allowed=())
        status, _, _ = self.request(path="/v1/organizations/me")
        self.assertEqual(status, 200)

    def test_query_string_is_preserved_upstream(self):
        self.start()
        self.request(path="/v1/models?limit=5", method="GET", body=b"")
        _, path, _, _ = self.upstream.requests[0]
        self.assertEqual(path, "/v1/models?limit=5")

    def test_encoded_traversal_never_reaches_upstream(self):
        self.start()
        status, _, _ = self.request(
            path="/v1/models/%2e%2e/%2e%2e/v1/organizations")
        self.assertEqual(status, 400)
        self.assertEqual(self.upstream.requests, [])

    def test_upstream_gets_the_target_we_authorized(self):
        """Authorizing one spelling and forwarding another is the bug this
        whole path exists to prevent."""
        self.start()
        self.request(path="/v1/models/", method="GET", body=b"")
        _, path, _, _ = self.upstream.requests[0]
        self.assertEqual(path, "/v1/models")

    def test_star_does_not_grant_the_subtree_over_the_wire(self):
        self.start()
        status, _, _ = self.request(path="/v1/models/a/b", method="GET", body=b"")
        self.assertEqual(status, 403)
        self.assertEqual(self.upstream.requests, [])


class TestCredentialRefresh(ProxyTestCase):
    def test_401_reloads_the_credential_and_retries(self):
        self.cred = FakeCredential(token="stale", rotate_to="fresh")
        self.upstream.fail_until_token = "fresh"
        self.start()
        status, _, _ = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(self.cred.refreshes, 1)
        self.assertEqual(len(self.upstream.requests), 2)
        self.assertEqual(self.upstream.requests[0][2]["Authorization"], "Bearer stale")
        self.assertEqual(self.upstream.requests[1][2]["Authorization"], "Bearer fresh")

    def test_401_without_a_new_credential_is_passed_through(self):
        self.cred = FakeCredential(token="stale")   # refresh() returns False
        self.upstream.fail_until_token = "fresh"
        self.start()
        status, _, _ = self.request()
        self.assertEqual(status, 401)
        self.assertEqual(len(self.upstream.requests), 1, "must not retry blindly")


class TestResourceBounds(ProxyTestCase):
    """A hostile sprite declares the numbers here; none of them may turn into
    an unbounded allocation on this machine."""

    def test_oversized_content_length_is_refused(self):
        self.start()
        # Declared, not sent -- the point is that we never allocate for it.
        status, _, _ = self.request(
            body=b"x", headers={"Content-Length": str(imp.MAX_BODY + 1)})
        self.assertEqual(status, 413)
        self.assertEqual(self.upstream.requests, [])

    def test_negative_content_length_is_refused(self):
        # int("-1") parses fine and rfile.read(-1) reads to EOF.
        self.start()
        status, _, _ = self.request(body=b"", headers={"Content-Length": "-1"})
        self.assertEqual(status, 400)
        self.assertEqual(self.upstream.requests, [])

    def test_malformed_content_length_is_refused(self):
        self.start()
        status, _, _ = self.request(body=b"", headers={"Content-Length": "abc"})
        self.assertEqual(status, 400)
        self.assertEqual(self.upstream.requests, [])

    def test_body_is_not_read_before_the_capability_check(self):
        """A bad capability must be refused without allocating for the body."""
        self.start()
        status, _, _ = self.request(
            auth="Bearer wrong",
            body=b"x", headers={"Content-Length": str(imp.MAX_BODY + 1)})
        # 403 (capability), not 413 (size) -- proving auth ran first.
        self.assertEqual(status, 403)
        self.assertEqual(self.upstream.requests, [])

    def test_a_large_but_legal_body_still_goes_through(self):
        self.start()
        payload = b'{"m":"' + b"z" * (2 * 1024 * 1024) + b'"}'
        status, _, _ = self.request(body=payload)
        self.assertEqual(status, 200)
        self.assertEqual(self.upstream.requests[0][3], payload)


class TestNoRedirect(ProxyTestCase):
    def test_a_redirect_is_passed_back_and_not_followed(self):
        """urllib's default handler copies Authorization across origins."""
        followed = []

        class Redirector(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(n)
                if self.path.startswith("/v1/messages"):
                    self.send_response(302)
                    self.send_header("Location", "/elsewhere")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    followed.append(dict(self.headers))
                    self.send_response(200)
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"{}")

        srv = HTTPServer(("127.0.0.1", 0), Redirector)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        imp.UPSTREAM = "http://127.0.0.1:%d" % srv.server_address[1]
        try:
            self.start()
            status, _, _ = self.request()
            self.assertEqual(status, 302)
            self.assertEqual(followed, [],
                             "the redirect must not be followed with the token")
        finally:
            srv.shutdown()
            srv.server_close()


class TestStreaming(ProxyTestCase):
    def test_sse_chunks_arrive(self):
        self.upstream.sse = [b"event: message_start\n\n",
                             b"event: content_block_delta\n\n",
                             b"event: message_stop\n\n"]
        self.start()
        status, _, body = self.request()
        self.assertEqual(status, 200)
        self.assertIn(b"message_start", body)
        self.assertIn(b"message_stop", body)


# --------------------------------------------------------------------------
# the tunnel -- the relay is what actually runs on the sprite
# --------------------------------------------------------------------------

class RelayHarness(object):
    """Runs RELAY_SRC exactly as imp ships it and speaks its frame protocol."""

    def __init__(self, port, idle=30.0):
        self.port = port
        b64 = base64.b64encode(imp.RELAY_SRC.encode()).decode()
        self.proc = subprocess.Popen(
            [sys.executable, "-u", "-c",
             "exec(__import__('base64').b64decode('%s'))" % b64,
             str(port), str(idle)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=0)

    def read_frame(self, timeout=10):
        h = self._readexact(imp.HDR.size, timeout)
        if h is None:
            return None
        sid, t, ln = imp.HDR.unpack(h)
        payload = self._readexact(ln, timeout) if ln else b""
        return sid, t, payload

    def _readexact(self, n, timeout):
        buf = b""
        while len(buf) < n:
            c = self.proc.stdout.read(n - len(buf))
            if not c:
                return None
            buf += c
        return buf

    def send(self, sid, t, payload=b""):
        self.proc.stdin.write(imp.HDR.pack(sid, t, len(payload)))
        if payload:
            self.proc.stdin.write(payload)
        self.proc.stdin.flush()

    def close(self):
        try:
            self.proc.kill()
            self.proc.wait(timeout=5)
        except Exception:
            pass
        for pipe in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                pipe.close()
            except Exception:
                pass


class TestRelay(unittest.TestCase):
    def setUp(self):
        self.port = free_port()
        self.relay = RelayHarness(self.port)
        # The relay pings once it is listening -- that frame is the only
        # honest signal that it is up.
        frame = self.relay.read_frame()
        self.assertIsNotNone(frame, "relay never signalled ready")
        self.assertEqual(frame[1], imp.T_PING)

    def tearDown(self):
        self.relay.close()

    def test_connection_produces_an_open_frame(self):
        conn = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        sid, t, _ = self.relay.read_frame()
        self.assertEqual(t, imp.T_OPEN)
        self.assertGreater(sid, 0)
        conn.close()

    def test_bytes_from_the_sprite_arrive_framed(self):
        conn = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        sid, t, _ = self.relay.read_frame()
        conn.sendall(b"GET /v1/models HTTP/1.1\r\n\r\n")
        sid2, t2, payload = self.relay.read_frame()
        self.assertEqual(t2, imp.T_DATA)
        self.assertEqual(sid2, sid)
        self.assertIn(b"/v1/models", payload)
        conn.close()

    def test_data_frames_reach_the_sprite_side_socket(self):
        conn = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        sid, _, _ = self.relay.read_frame()
        self.relay.send(sid, imp.T_DATA, b"HTTP/1.1 200 OK\r\n\r\nhi")
        conn.settimeout(5)
        self.assertIn(b"200 OK", conn.recv(65536))
        conn.close()

    def test_eight_bit_clean_round_trip(self):
        """The design rests on the ssh stdio channel carrying arbitrary bytes.
        256KB of every byte value, both directions, must survive intact."""
        blob = bytes(range(256)) * 1024
        self.assertEqual(len(blob), 262144)

        conn = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        sid, _, _ = self.relay.read_frame()

        # sprite -> us
        threading.Thread(target=conn.sendall, args=(blob,), daemon=True).start()
        got = b""
        while len(got) < len(blob):
            frame = self.relay.read_frame()
            self.assertIsNotNone(frame, "stream truncated at %d bytes" % len(got))
            _, t, payload = frame
            if t == imp.T_DATA:
                got += payload
        self.assertEqual(got, blob, "sprite -> host was not byte-identical")

        # us -> sprite
        def push():
            for i in range(0, len(blob), 65536):
                self.relay.send(sid, imp.T_DATA, blob[i:i + 65536])
        threading.Thread(target=push, daemon=True).start()
        back = b""
        conn.settimeout(10)
        while len(back) < len(blob):
            c = conn.recv(65536)
            if not c:
                break
            back += c
        self.assertEqual(back, blob, "host -> sprite was not byte-identical")
        conn.close()

    def test_close_frame_drops_the_connection(self):
        conn = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        sid, _, _ = self.relay.read_frame()
        self.relay.send(sid, imp.T_CLOSE)
        conn.settimeout(5)
        self.assertEqual(conn.recv(65536), b"", "relay should have closed it")
        conn.close()


class TestMuxFrameBounds(unittest.TestCase):
    """The frame length is a uint32 the sprite chooses."""

    def test_limit_is_far_above_what_the_relay_sends(self):
        # The relay reads in 64KB chunks, so the cap only ever bites on a
        # length nobody legitimate would declare.
        self.assertGreater(imp.MAX_FRAME, 65536)

    def test_oversized_frame_closes_the_link_without_allocating(self):
        class FakeProc(object):
            """stdout declares a 4GB frame and then stops talking."""

            def __init__(self):
                self.stdin = io.BytesIO()
                self.stdout = io.BytesIO(imp.HDR.pack(1, imp.T_DATA, 0xFFFFFFFF))
                self.reads = 0

            def poll(self):
                return None

        proc = FakeProc()
        mux = imp.Mux(proc, 1, False)
        mux.run()          # must return, not try to read 4GB
        self.assertFalse(mux.alive)
        self.assertEqual(mux.socks, {})


class TestMuxStreamBounds(unittest.TestCase):
    """Each OPEN frame costs a socket and a thread on this machine."""

    class FakeProc(object):
        def __init__(self):
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO(b"")

        def poll(self):
            return None

    def frames_sent(self, proc):
        raw, out = proc.stdin.getvalue(), []
        i = 0
        while i + imp.HDR.size <= len(raw):
            sid, t, ln = imp.HDR.unpack(raw[i:i + imp.HDR.size])
            out.append((sid, t))
            i += imp.HDR.size + ln
        return out

    def wait_for_frame(self, proc, want, timeout=5):
        """send() enqueues; a writer thread drains. Give it a moment."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if want in self.frames_sent(proc):
                return True
            time.sleep(0.02)
        return False

    def test_open_beyond_the_cap_is_refused(self):
        proc = self.FakeProc()
        mux = imp.Mux(proc, 1, False)
        # Port 1 is not listening, so a stream that got past the cap would
        # fail to connect -- but the cap should refuse it before that.
        mux.socks = {i: None for i in range(imp.MAX_STREAMS)}
        mux._open(9999)
        self.assertNotIn(9999, mux.socks)
        self.assertTrue(self.wait_for_frame(proc, (9999, imp.T_CLOSE)),
                        "expected a T_CLOSE for the refused stream")

    def test_duplicate_sid_closes_the_socket_it_replaces(self):
        proc = self.FakeProc()
        mux = imp.Mux(proc, 1, False)
        a, b = socket.socketpair()
        try:
            mux.socks = {7: imp.Stream(a)}
            mux._open(7)          # connect to port 1 fails, but `a` must close
            deadline = time.time() + 5
            while time.time() < deadline and a.fileno() != -1:
                time.sleep(0.02)
            with self.assertRaises(OSError):
                a.send(b"x")
        finally:
            for sk in (a, b):
                try:
                    sk.close()
                except OSError:
                    pass


class TestStreamBuffering(unittest.TestCase):
    def test_feed_never_blocks_and_refuses_past_the_cap(self):
        a, b = socket.socketpair()
        st = imp.Stream(a)
        try:
            self.assertTrue(st.feed(b"small"))
            # One chunk larger than the whole allowance: the peer cannot be
            # keeping up, and feed must say so rather than wait.
            self.assertFalse(st.feed(b"x" * (imp.MAX_STREAM_BUFFER + 1)))
        finally:
            st.close()
            b.close()

    def test_feed_on_a_closed_stream_is_refused(self):
        a, b = socket.socketpair()
        st = imp.Stream(a)
        st.close()
        try:
            self.assertFalse(st.feed(b"x"))
        finally:
            b.close()


class TestHeadOfLineBlocking(unittest.TestCase):
    """The reason this matters: several `claude` sessions on one sprite.

    One that stops draining must not stall the others -- and because the
    frame reader is also what refreshes the relay's idle timestamp, stalling
    it would trip the watchdog into killing the whole session.
    """

    def setUp(self):
        self.port = free_port()
        self.relay = RelayHarness(self.port)
        self.assertEqual(self.relay.read_frame()[1], imp.T_PING)

    def tearDown(self):
        self.relay.close()

    def open_stream(self, rcvbuf=None):
        conn = socket.socket()
        if rcvbuf is not None:
            # A small receive buffer makes the relay's send buffer fill fast
            # and stay full. Without this the test proves nothing: loopback
            # buffers auto-tune into the megabytes, so a blocking relay
            # absorbs everything a reasonable test would send and looks fine.
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        conn.connect(("127.0.0.1", self.port))
        sid, t, _ = self.relay.read_frame()
        self.assertEqual(t, imp.T_OPEN)
        return sid, conn

    def flood(self, sid, mb=1):
        """Push at a stream from a thread -- if the relay ever stops reading,
        these writes block rather than failing the test outright."""
        def go():
            try:
                for _ in range(mb * 16):
                    self.relay.send(sid, imp.T_DATA, b"z" * 65536)
            except OSError:
                pass
        t = threading.Thread(target=go, daemon=True)
        t.start()
        return t

    def test_a_stalled_consumer_does_not_block_another_stream(self):
        stalled_sid, stalled = self.open_stream(rcvbuf=4096)
        live_sid, live = self.open_stream()
        try:
            self.flood(stalled_sid, mb=1)     # nothing ever recv()s on it
            time.sleep(1.0)                   # let it wedge if it is going to
            try:
                self.relay.send(live_sid, imp.T_DATA, b"STILL-FLOWING")
            except OSError as e:
                # A relay wedged on one socket stops reading frames; its idle
                # timestamp then never advances and the watchdog kills it.
                self.fail("relay stopped reading frames while one consumer "
                          "was stalled (%r)" % (e,))

            live.settimeout(8)
            got = b""
            while b"STILL-FLOWING" not in got:
                more = live.recv(65536)
                if not more:
                    self.fail("the relay closed the healthy stream -- a "
                              "stalled consumer took the whole session down")
                got += more
        finally:
            for c in (stalled, live):
                try:
                    c.close()
                except OSError:
                    pass

    def test_the_relay_keeps_accepting_while_a_consumer_is_stalled(self):
        """The frame loop is also what refreshes the idle timestamp, so a
        relay wedged on one socket eventually kills itself."""
        stalled_sid, stalled = self.open_stream(rcvbuf=4096)
        try:
            self.flood(stalled_sid, mb=1)
            time.sleep(1.0)
            _, conn = self.open_stream()      # still responsive
            conn.close()
        finally:
            try:
                stalled.close()
            except OSError:
                pass


class TestRelayWatchdog(unittest.TestCase):
    def test_relay_exits_after_the_idle_timeout(self):
        """Orphaned relay must free the port on its own -- there is no teardown
        that has to succeed."""
        port = free_port()
        relay = RelayHarness(port, idle=1.0)
        try:
            self.assertIsNotNone(relay.read_frame())
            deadline = time.time() + 20
            while time.time() < deadline and relay.proc.poll() is None:
                time.sleep(0.25)
            self.assertIsNotNone(relay.proc.poll(),
                                 "relay did not exit after going idle")
            # Port is free again.
            s = socket.socket()
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
            s.close()
        finally:
            relay.close()


# --------------------------------------------------------------------------
# settings.json handling on the sprite
# --------------------------------------------------------------------------

class TestSettingsScript(unittest.TestCase):
    """SETTINGS_SCRIPT runs on the sprite; drive it the way imp does."""

    def run_script(self, home, base_url, capability):
        env = dict(os.environ, HOME=home)
        return subprocess.run(
            [sys.executable, "-u", "-c", imp.SETTINGS_SCRIPT],
            input="%s\n%s\n" % (base_url, capability),
            capture_output=True, text=True, env=env)

    def listener(self):
        """Stand in for another session's live relay; returns its port."""
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(8)
        self.addCleanup(srv.close)
        return srv.getsockname()[1]

    def read(self):
        with open(self.path) as f:
            return f.read()

    def read_json(self):
        with open(self.path) as f:
            return json.load(f)

    def setUp(self):
        import tempfile
        self.home = tempfile.mkdtemp()
        self.path = os.path.join(self.home, ".claude", "settings.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.home, ignore_errors=True)

    def test_writes_pointer_into_a_fresh_home(self):
        r = self.run_script(self.home, "http://127.0.0.1:8080", "CAP")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = self.read_json()
        self.assertEqual(data["env"]["ANTHROPIC_BASE_URL"], "http://127.0.0.1:8080")
        self.assertEqual(data["env"]["ANTHROPIC_AUTH_TOKEN"], "CAP")

    def test_settings_file_is_not_world_readable(self):
        self.run_script(self.home, "http://127.0.0.1:8080", "CAP")
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_teardown_removes_the_env_block_entirely(self):
        self.run_script(self.home, "http://127.0.0.1:8080", "CAP")
        r = self.run_script(self.home, "", "CAP")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = self.read_json()
        self.assertNotIn("env", data, "no env block should be left behind")

    def test_unrelated_settings_survive_both_directions(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            json.dump({"theme": "dark", "env": {"FOO": "bar"}}, f)
        self.run_script(self.home, "http://127.0.0.1:8080", "CAP")
        self.run_script(self.home, "", "CAP")
        data = self.read_json()
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data["env"], {"FOO": "bar"})

    def test_refuses_to_touch_invalid_json(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            f.write("{not json")
        r = self.run_script(self.home, "http://127.0.0.1:8080", "CAP")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.read(), "{not json")

    def test_refuses_to_displace_a_live_session(self):
        live = self.listener()
        self.run_script(self.home, "http://127.0.0.1:%d" % live, "CAP-A")
        r = self.run_script(self.home, "http://127.0.0.1:9999", "CAP-B")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("BUSY", r.stderr)
        # A's session is untouched.
        env = self.read_json()["env"]
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "CAP-A")

    def test_takes_over_a_dead_sessions_pointer(self):
        """A killed imp leaves an inert pointer. Blocking every future run on
        it would be worse than the problem it prevents."""
        dead = free_port()          # nothing is listening there
        self.run_script(self.home, "http://127.0.0.1:%d" % dead, "CAP-A")
        r = self.run_script(self.home, "http://127.0.0.1:9999", "CAP-B")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("took over", r.stdout)
        self.assertEqual(self.read_json()["env"]["ANTHROPIC_AUTH_TOKEN"], "CAP-B")

    def test_unparseable_pointer_is_not_treated_as_live(self):
        self.run_script(self.home, "http://127.0.0.1:8080", "CAP-A")
        data = self.read_json()
        data["env"]["ANTHROPIC_BASE_URL"] = "not-a-url"
        with open(self.path, "w") as f:
            json.dump(data, f)
        r = self.run_script(self.home, "http://127.0.0.1:9999", "CAP-B")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_reinstalling_our_own_capability_is_not_busy(self):
        self.run_script(self.home, "http://127.0.0.1:8080", "CAP-A")
        r = self.run_script(self.home, "http://127.0.0.1:8080", "CAP-A")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_teardown_leaves_another_sessions_capability_alone(self):
        """The half that actually damaged the other session: teardown used to
        strip ANTHROPIC_* unconditionally."""
        self.run_script(self.home, "http://127.0.0.1:8080", "CAP-A")
        self.run_script(self.home, "http://127.0.0.1:8081", "CAP-B")
        r = self.run_script(self.home, "", "CAP-A")     # A exits last
        self.assertEqual(r.returncode, 0, r.stderr)
        env = self.read_json()["env"]
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "CAP-B")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:8081")

    def test_teardown_removes_our_own(self):
        self.run_script(self.home, "http://127.0.0.1:8080", "CAP-A")
        self.run_script(self.home, "", "CAP-A")
        self.assertNotIn("env", self.read_json())

    def test_no_fixed_temp_file_is_left_behind(self):
        self.run_script(self.home, "http://127.0.0.1:8080", "CAP")
        leftovers = [f for f in os.listdir(os.path.dirname(self.path))
                     if f != "settings.json"]
        self.assertEqual(leftovers, [], "temp file should be replaced away")

    def test_warns_about_a_stored_oauth_token(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            json.dump({"env": {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-old"}}, f)
        r = self.run_script(self.home, "http://127.0.0.1:8080", "CAP")
        self.assertIn("WARNING", r.stdout)


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------

class TestRemotePython(unittest.TestCase):
    def test_source_is_shipped_as_base64_not_a_file(self):
        cmd = imp.remote_python("print('hi')", ("1", "2"))
        self.assertIn("b64decode", cmd)
        self.assertNotIn("print('hi')", cmd, "source must not appear in argv")
        self.assertTrue(cmd.endswith("'1' '2'"))

    def test_it_actually_runs(self):
        cmd = imp.remote_python("import sys; print(sys.argv[1])", ("hello",))
        # Strip the leading `python3 -u -c ` and run the rest ourselves.
        r = subprocess.run(["/bin/sh", "-c",
                            cmd.replace("python3", sys.executable, 1)],
                           capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "hello", r.stderr)


class TestSshArgv(unittest.TestCase):
    def test_uses_sprite_proxy_as_the_transport(self):
        argv = imp.ssh_argv("my-sprite", "true")
        self.assertIn("ProxyCommand=sprite proxy --ssh -s my-sprite", argv)

    def test_omits_s_when_the_sprite_is_ambient(self):
        argv = imp.ssh_argv("", "true")
        self.assertIn("ProxyCommand=sprite proxy --ssh", argv)
        self.assertNotIn("-s ", " ".join(argv).replace("-s my-sprite", ""))

    def test_does_not_reuse_a_control_socket(self):
        # A shared ControlMaster would outlive revocation.
        argv = imp.ssh_argv("s", "true")
        self.assertIn("ControlMaster=no", argv)
        self.assertIn("ControlPath=none", argv)


class TestHopHeaders(unittest.TestCase):
    def test_hop_by_hop_headers_are_not_forwarded(self):
        for h in ("connection", "transfer-encoding", "content-length", "host"):
            self.assertIn(h, imp.HOP)


if __name__ == "__main__":
    unittest.main()
