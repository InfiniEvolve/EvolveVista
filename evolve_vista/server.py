"""A server on this machine for the campaign page: the page, and the campaign as a stream of snapshots.

The page keeps one connection open (server-sent events). The server pushes a snapshot when the campaign
changed, and nothing otherwise; to notice, it compares the modification times of the files the snapshot reads
once a second. It listens on 127.0.0.1 only and answers only pages carrying the token from the printed address.

The snapshot is InfiniEvolve's own (infinievolve.snapshot) when InfiniEvolve is installed and the directory
is one of its campaigns, else this package's reader (campaign.py), which also reads the older infra-evolve
layout. A caller may pass its own reader.

A session dropped on the page's analysis area gets a TraceVista analysis, written by a coding agent on this
machine that the reader picks (those installed, and each --analyst), which follows the TraceVista skill in a
directory of its own, or by a model the page asks directly with the reader's key. This server extracts the
session's agent log, runs the agent and compiles the analysis (vista.py). Those analyses, under
<campaign>/vista/, are the only thing it writes; with analysts=None it starts no agents.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import secrets
import threading
import time
from typing import Callable, NamedTuple
from urllib.parse import parse_qs, urlsplit

from . import apistats, campaign, livelog, vista

STATIC = Path(__file__).resolve().parent / "static"
PAGE = "campaign.html"
DEFAULT_PORT = 8765
CHECK_SECONDS = 1.0       # how often a stream looks for a change
KEEPALIVE_SECONDS = 15    # a comment line, so that a dropped connection is noticed
HEADER = "X-TraceVista-Campaign"   # the page sends its token in this header


class Reader(NamedTuple):
    snapshot: Callable[[Path], dict]   # the campaign as one JSON object (format "infinievolve-campaign")
    stamp: Callable[[Path], str]       # changes whenever anything the snapshot reads changes


def reader_for(root: Path) -> Reader:
    """InfiniEvolve's snapshot for one of its campaigns when it is installed; this package's reader otherwise."""
    try:
        from infinievolve.snapshot import snapshot, stamp
    except ImportError:
        return Reader(campaign.snapshot, campaign.stamp)
    try:
        snapshot(root)
        return Reader(snapshot, stamp)
    except Exception:   # not an InfiniEvolve campaign, e.g. the older layout
        return Reader(campaign.snapshot, campaign.stamp)


class VistaServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, root, port: int = DEFAULT_PORT, static=STATIC, token: str | None = None, analysts=None,
                 reader: Reader | None = None):
        self.campaign = Path(root).resolve(strict=True)
        self.reader = reader or reader_for(self.campaign)
        self.reader.snapshot(self.campaign)   # refuse a directory that is not a campaign before serving anything
        self.static = Path(static).resolve()
        if not (self.static / PAGE).is_file():
            raise ValueError(f"the campaign page is missing from {self.static}: build it first (npm run build)")
        self.token = token or secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        self.vista_lock = threading.Lock()   # one extraction or compilation at a time
        self.analysts = analysts             # the agents a reader may start to analyse a session, or None
        self.cached: tuple[str, bytes] | None = None
        self.api_stats = apistats.ApiStats()   # the key is read here, once; it never leaves this process
        self.closing = threading.Event()
        super().__init__(("127.0.0.1", port), Handler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/#live={self.token}"

    def current(self) -> tuple[str, bytes]:
        """(stamp, snapshot JSON), rebuilt only when the campaign changed."""
        with self.lock:
            if self.cached is None or self.cached[0] != self.reader.stamp(self.campaign):
                data = self.reader.snapshot(self.campaign)
                self.cached = (data["stamp"], json.dumps(data, ensure_ascii=False).encode())
            return self.cached

    def server_close(self):
        self.closing.set()   # ends the open streams
        self.api_stats.pool.shutdown(wait=False, cancel_futures=True)
        super().server_close()


def start(root, port: int = DEFAULT_PORT, analysts=None, reader: Reader | None = None, token: str | None = None,
          static=STATIC) -> VistaServer:
    """Serve in a background thread; when the port is taken, on any free one."""
    try:
        server = VistaServer(root, port, static, token=token, analysts=analysts, reader=reader)
    except OSError:
        server = VistaServer(root, 0, static, token=token, analysts=analysts, reader=reader)
    threading.Thread(target=server.serve_forever, name="evolve-vista", daemon=True).start()
    return server


class Handler(BaseHTTPRequestHandler):
    def local(self) -> bool:
        expected = f"127.0.0.1:{self.server.server_port}"
        return self.headers.get("Host") == expected and self.headers.get("Origin") in (None, "http://" + expected)

    def respond(self, code, data=b"", content_type="application/json", headers=None):
        self.send_response(code)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def do_GET(self):
        if not self.local():
            self.respond(403, b'{"error":"Local origin required"}'); return
        url = urlsplit(self.path)
        if url.path == "/api/campaign/events":
            token = (parse_qs(url.query).get("token") or [""])[0]   # EventSource cannot send a header
            if not secrets.compare_digest(token, self.server.token):
                self.respond(403, b'{"error":"Open the address the watch command printed"}'); return
            self.stream(); return
        if url.path == "/api/campaign/agent-log":
            query = parse_qs(url.query)
            if not secrets.compare_digest((query.get("token") or [""])[0], self.server.token):
                self.respond(403, b'{"error":"Open the address the watch command printed"}'); return
            self.agent_log(query); return
        if url.path == "/api/campaign/agent-api-stats":
            query = parse_qs(url.query)
            if not secrets.compare_digest((query.get("token") or [""])[0], self.server.token):
                self.respond(403, b'{"error":"Open the address the watch command printed"}'); return
            self.agent_api_stats(query); return
        if url.path.startswith("/api/campaign/vista"):
            self.vista("GET", url); return
        if url.path == "/api/campaign":   # the snapshot once, for scripts; the page uses the stream
            self.snapshot(); return
        name = PAGE if url.path in ("/", "/index.html") else url.path.lstrip("/")
        path = (self.server.static / name).resolve()
        if not str(path).startswith(str(self.server.static) + "/") or not path.is_file() or path.suffix == ".json":
            self.respond(404, b"Not found", "text/plain"); return
        self.respond(200, path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream")

    def do_POST(self):
        if not self.local():
            self.respond(403, b'{"error":"Local origin required"}'); return
        url = urlsplit(self.path)
        if url.path.startswith("/api/campaign/vista"):
            self.vista("POST", url); return
        self.respond(404, b'{"error":"Not found"}')

    def vista(self, method: str, url) -> None:
        """The session-analysis routes: see vista.handle."""
        if not secrets.compare_digest(self.headers.get(HEADER, ""), self.server.token):
            self.respond(403, b'{"error":"Open the address the watch command printed"}'); return

        def read_body(limit: int):
            size = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(size) if 0 < size <= limit else None
        code, body = vista.handle(self.server, method, url.path[len("/api/campaign/vista"):], parse_qs(url.query), read_body)
        self.respond(code, body)

    def snapshot(self):
        if not secrets.compare_digest(self.headers.get(HEADER, ""), self.server.token):
            self.respond(403, b'{"error":"Open the address the watch command printed"}'); return
        try:
            current, body = self.server.current()
        except (OSError, ValueError):   # keep local paths out of the answer
            self.respond(409, b'{"error":"The campaign directory could not be read"}'); return
        tag = f'"{current}"'
        if self.headers.get("If-None-Match") == tag:
            self.respond(304, headers={"ETag": tag}); return
        self.respond(200, body, headers={"ETag": tag})

    def stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        sent, quiet = self.headers.get("Last-Event-ID"), time.monotonic()
        try:
            self.wfile.write(b"retry: 3000\n\n"); self.wfile.flush()
            while not self.server.closing.is_set():
                try:
                    current, body = self.server.current()
                except (OSError, ValueError):
                    current, body = None, None   # e.g. a file caught mid-write: try again next time
                if current and current != sent:
                    self.wfile.write(b"id: " + current.encode() + b"\nevent: snapshot\ndata: " + body + b"\n\n")
                    self.wfile.flush()
                    sent, quiet = current, time.monotonic()
                elif time.monotonic() - quiet > KEEPALIVE_SECONDS:
                    self.wfile.write(b": keepalive\n\n"); self.wfile.flush()
                    quiet = time.monotonic()
                self.server.closing.wait(CHECK_SECONDS)
        except OSError:
            pass   # the page went away

    def agent_log(self, query: dict):
        """A session's agent log as it grows: an event per batch of new entries, its id the offset reached."""
        task, session = (query.get("task") or [""])[0], (query.get("session") or [""])[0]
        try:
            log = livelog.log_for(self.server.campaign, task, session)
            livelog.redactor()   # refuse before streaming when the log could not be redacted
        except vista.VistaError as error:
            self.respond(error.status, json.dumps({"error": str(error)}).encode()); return
        given = self.headers.get("Last-Event-ID") or ""
        offset = int(given) if given.isdigit() else None
        private = (str(self.server.campaign),)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        first, quiet = True, time.monotonic()
        try:
            self.wfile.write(b"retry: 3000\n\n"); self.wfile.flush()
            while not self.server.closing.is_set():
                batch, moved = [], False
                if log.is_file() and log.stat().st_size != offset:
                    try:
                        batch, reached = livelog.read(log, offset, private)
                    except OSError:   # e.g. replaced mid-read: try again next time
                        reached = offset
                    moved, offset = reached != offset, reached
                if batch or moved or first:   # an empty batch still moves the id on, and the first says "connected"
                    data = json.dumps({"entries": batch}, ensure_ascii=False).encode()
                    self.wfile.write((f"id: {offset}\n" if offset is not None else "").encode() + b"event: entries\ndata: "
                                     + data + b"\n\n"); self.wfile.flush()
                    first, quiet = False, time.monotonic()
                elif time.monotonic() - quiet > KEEPALIVE_SECONDS:
                    self.wfile.write(b": keepalive\n\n"); self.wfile.flush()
                    quiet = time.monotonic()
                if not (moved and log.stat().st_size > offset):   # more to read at once, else wait
                    self.server.closing.wait(CHECK_SECONDS)
        except OSError:
            pass   # the page went away

    def agent_api_stats(self, query: dict):
        """How fast the session's model answered: every point found so far, then each new one as it is found."""
        task, session = (query.get("task") or [""])[0], (query.get("session") or [""])[0]
        try:
            calls = self.server.api_stats.session(self.server.campaign, task, session)
        except vista.VistaError as error:
            self.respond(error.status, json.dumps({"error": str(error)}).encode()); return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        sent, last, quiet = set(), None, time.monotonic()
        try:
            self.wfile.write(b"retry: 3000\n\n"); self.wfile.flush()
            while not self.server.closing.is_set():
                try:
                    calls.advance()
                except OSError:
                    pass   # e.g. the log replaced mid-read: try again next time
                view = calls.view()
                new = [p for p in view["points"] if p["id"] not in sent]
                summary = (view["state"], view["calls"], view["waiting"], view["missing"])
                if new or summary != last:   # each page gets every point once, and the counts when they change
                    sent.update(p["id"] for p in new)
                    data = json.dumps({**view, "points": new}, ensure_ascii=False).encode()
                    self.wfile.write(b"event: stats\ndata: " + data + b"\n\n"); self.wfile.flush()
                    last, quiet = summary, time.monotonic()
                elif time.monotonic() - quiet > KEEPALIVE_SECONDS:
                    self.wfile.write(b": keepalive\n\n"); self.wfile.flush()
                    quiet = time.monotonic()
                self.server.closing.wait(CHECK_SECONDS)
        except OSError:
            pass   # the page went away

    def log_message(self, *_):
        pass   # never log the token or the campaign's paths
