"""Fixtures shared by the HOCON test modules: ``pyhocon_tripwire`` fails any call into
pyhocon's include machinery, on the shared module and on the backend's private copy of
it (a different class); ``http_server`` records the network requests an include makes.
"""

import http.server
import threading

import pytest

from hyera.backends._hocon import _hocon_parser


@pytest.fixture
def pyhocon_tripwire(monkeypatch):
    pytest.importorskip("pyhocon")
    import pyhocon.config_parser as cp

    calls = []

    def _tripwire(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("pyhocon's own include machinery must never run")

    # Both the shared module and HOCONBackend's own private copy (see the
    # module docstring) -- `_hocon_parser()` also builds/caches the latter,
    # which must exist before it can be patched.
    for target in (cp, _hocon_parser()):
        monkeypatch.setattr(target.ConfigFactory, "parse_file", _tripwire)
        monkeypatch.setattr(target.ConfigFactory, "parse_URL", _tripwire)
        monkeypatch.setattr(target.ConfigParser, "resolve_package_path", _tripwire)
    return calls


@pytest.fixture
def http_server():
    hits = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"probe = hit\n")

        def log_message(self, *args, **kwargs):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


BS = chr(92)
