"""The dashboard: a localhost HTTP server, and the human half of the board.

This is where invariants 4 and 5 are enforced. On a single-user laptop OS
identity cannot tell you from an agent, so the **interface** is the boundary:

* You act through the browser. Write endpoints require both a session cookie
  (``HttpOnly``, ``SameSite=Strict``) and a CSRF token that is injected into
  the page. Another local page cannot read our HTML, so it cannot forge the
  header; the cookie alone would have been CSRF-able.
* Agents act through the CLI, which has neither, so ``promote``, ``approve``,
  ``reject`` and ``answer`` are simply out of reach.

Both tokens are generated in memory at startup and never written to disk.

The server binds ``127.0.0.1`` only. It is a UI, not the board's integration
layer -- the files remain that, and this process is disposable.
"""

import json
import os
import secrets
import socket
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import dispatch, lease, model, render, store, transitions
from .actor import Actor

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_BODY = 1 << 20  # 1 MiB is plenty for a card form.


class Session:
    """In-memory tokens for one dashboard run."""

    def __init__(self):
        self.cookie = secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(32)


def _load_template():
    path = os.path.join(STATIC_DIR, "dashboard.html")
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


class Handler(BaseHTTPRequestHandler):
    server_version = "kanban"

    # -- plumbing -------------------------------------------------------

    def log_message(self, fmt, *args):
        """Quiet by default: this runs in a window the user may be watching."""
        if os.environ.get("KANBAN_HTTP_LOG"):
            super().log_message(fmt, *args)

    @property
    def board(self):
        return self.server.board

    @property
    def session(self):
        return self.server.session

    def _send(self, code, body, content_type="application/json", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code, payload, extra=None):
        self._send(code, json.dumps(payload, default=str), extra=extra)

    def _cookie_token(self):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "kanban_session":
                return value
        return None

    def _authorised(self):
        """Both factors must match: the cookie proves the session, the header
        proves the request came from our own page."""
        if not secrets.compare_digest(
            self._cookie_token() or "", self.session.cookie
        ):
            return False
        sent = self.headers.get("X-Kanban-CSRF") or ""
        return secrets.compare_digest(sent, self.session.csrf)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("request body too large")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    # -- routes ---------------------------------------------------------

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._serve_page()
        if path == "/api/board":
            return self._serve_board()
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        return self._json(404, {"error": "not found"})

    def _serve_page(self):
        try:
            html = _load_template()
        except OSError as exc:
            return self._send(500, "dashboard template missing: %s" % exc, "text/plain")
        html = html.replace("__CSRF_TOKEN__", self.session.csrf)
        cookie = (
            "kanban_session=%s; Path=/; HttpOnly; SameSite=Strict"
            % self.session.cookie
        )
        self._send(200, html, "text/html; charset=utf-8", {"Set-Cookie": cookie})

    def _serve_board(self):
        # Sweeping here means expired leases are reaped without a cron job,
        # and pumping means queued cards start as soon as a slot frees up.
        # Under the write lock: two tabs polling at once used to run two pumps
        # side by side, and both could launch an agent for the same card.
        with self.server.write_lock:
            try:
                lease.sweep(self.board)
            except Exception as exc:  # a sweep failure must not blank the board
                self.server.last_error = "sweep failed: %s" % exc

            config = self.board.read_config()
            if self.server.auto_dispatch:
                try:
                    dispatch.pump(self.board)
                except Exception as exc:
                    self.server.last_error = "dispatch failed: %s" % exc

        payload = render.board_payload(
            self.board, dispatch_available=dispatch.available(config)
        )
        payload["last_error"] = self.server.last_error
        self.server.last_error = None
        return self._json(200, payload)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if not self._authorised():
            return self._json(
                403,
                {
                    "error": "not authorised",
                    "detail": (
                        "Write actions require the dashboard's own session. "
                        "This is the review gate: agents use the CLI and "
                        "cannot reach here."
                    ),
                },
            )
        try:
            payload = self._read_json()
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})

        routes = {
            "/api/action": self._do_action,
            "/api/new": self._do_new,
            "/api/edit": self._do_edit,
        }
        if path not in routes:
            return self._json(404, {"error": "not found"})
        try:
            with self.server.write_lock:
                return routes[path](payload)
        except (
            store.BoardError,
            model.CardError,
            transitions.TransitionError,
            lease.ClaimRefused,
            dispatch.DispatchError,
        ) as exc:
            return self._json(400, {"error": str(exc)})
        except Exception as exc:  # never take the dashboard down
            return self._json(500, {"error": "%s: %s" % (type(exc).__name__, exc)})

    # -- actions --------------------------------------------------------

    def _do_action(self, payload):
        card_id = str(payload.get("card") or "")
        action = payload.get("action")
        text = (payload.get("text") or "").strip()
        you = Actor.as_human()

        if not card_id or not action:
            return self._json(400, {"error": "card and action are required"})

        if action == "execute":
            transitions.promote(self.board, card_id, you)
            return self._json(200, self._after_promote(card_id))

        if action == "start":
            return self._json(200, self._start(card_id))

        if action == "release":
            stopped = dispatch.stop_agent(self.board, card_id)
            lease.release(self.board, card_id, you, "released from dashboard")
            return self._json(200, {"ok": True, "message": "released %s%s" % (
                card_id, " and stopped its agent" if stopped else "")})

        if action == "to_inbox":
            stopped = dispatch.stop_agent(self.board, card_id)
            transitions.to_inbox(self.board, card_id, you)
            return self._json(200, {"ok": True, "message": "moved %s back to Inbox%s" % (
                card_id, ", agent stopped" if stopped else "")})

        if action == "approve":
            card, _ = transitions.approve(self.board, card_id, you)
            message = "approved %s" % card_id
            extra = {}
            if card["status"] == "ready":
                extra = self._start(card_id, quiet=True)
                message = "plan approved -- agent starting on %s" % card_id
            return self._json(200, dict({"ok": True, "message": message}, **extra))

        if action == "reject":
            if not text:
                return self._json(400, {"error": "rejection needs feedback"})
            transitions.reject(self.board, card_id, you, text)
            extra = self._start(card_id, quiet=True)
            return self._json(
                200,
                dict(
                    {"ok": True, "message": "sent %s back with feedback" % card_id},
                    **extra
                ),
            )

        if action == "answer":
            if not text:
                return self._json(400, {"error": "an answer is required"})
            transitions.answer(self.board, card_id, you, text)
            extra = self._start(card_id, quiet=True)
            return self._json(
                200,
                dict({"ok": True, "message": "answered %s" % card_id}, **extra),
            )

        return self._json(400, {"error": "unknown action %r" % action})

    def _after_promote(self, card_id):
        result = {"ok": True, "message": "moved %s to Execute" % card_id}
        result.update(self._start(card_id, quiet=True))
        return result

    def _start(self, card_id, quiet=False):
        """Try to start an agent, reporting rather than failing the action.

        A card that cannot start yet is not an error: it stays queued and the
        next board poll picks it up when a slot frees.
        """
        if not self.server.auto_dispatch:
            return {"dispatch": "disabled"}
        try:
            info = dispatch.dispatch(self.board, card_id)
            return {"dispatch": "started", "pid": info["pid"]}
        except dispatch.DispatchError as exc:
            if quiet:
                return {"dispatch": "queued", "dispatch_note": str(exc)}
            raise

    def _do_new(self, payload):
        title = (payload.get("title") or "").strip()
        if not title:
            return self._json(400, {"error": "a title is required"})
        board = self.board
        card = model.new_card(
            payload.get("id") or board.next_id(),
            title,
            goal=(payload.get("goal") or "").strip(),
            acceptance=_lines(payload.get("acceptance")),
            origin="human",
            tier="act" if payload.get("skip_planning") else "plan",
            read_only=bool(payload.get("read_only")),
            parent=(payload.get("parent") or "").strip() or None,
            depends_on=_lines(payload.get("depends_on")),
            scope_allow=_lines(payload.get("scope_allow")),
            scope_deny=_lines(payload.get("scope_deny")),
            skills=payload.get("skills") or [],
            max_turns=int(payload.get("max_turns") or 40),
        )
        model.require_valid(card)
        card_id = board.create_card_file(card, "")
        return self._json(
            200, {"ok": True, "id": card_id, "message": "created card %s" % card_id}
        )

    def _do_edit(self, payload):
        card_id = str(payload.get("card") or "")
        card, body = self.board.load(card_id)
        editable = ("inbox", "ready")
        if set(payload) <= {"card", "skills", "read_only"}:
            # Skills and read-only are read at the next dispatch, so a card waiting in Review
            # can still gain one before it is sent back.
            editable += ("needs_review",)
        if card.get("status") not in editable:
            return self._json(
                400,
                {
                    "error": "card %s is %s; edit it while it is in Inbox or "
                    "Execute" % (card_id, card.get("status"))
                },
            )
        spec = card.setdefault("spec", {})
        if "title" in payload:
            card["title"] = (payload["title"] or "").strip() or card["title"]
        if "goal" in payload:
            spec["goal"] = (payload["goal"] or "").strip()
        for key in ("acceptance", "scope_allow", "scope_deny", "depends_on"):
            if key in payload:
                target = card if key == "depends_on" else spec
                target[key] = _lines(payload[key])
        if "skills" in payload:
            spec["skills"] = payload["skills"] or []
        if "read_only" in payload:
            card["read_only"] = bool(payload["read_only"])
        if "tier" in payload and payload["tier"] in model.TIERS:
            card["tier"] = payload["tier"]
        model.require_valid(card)
        self.board.save(card_id, card, body)
        return self._json(200, {"ok": True, "message": "updated %s" % card_id})


def _lines(value):
    """Accept either a list or a newline-separated textarea value."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [line.strip() for line in str(value).splitlines() if line.strip()]


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, board, auto_dispatch=True):
        super().__init__(address, Handler)
        self.board = board
        self.session = Session()
        self.auto_dispatch = auto_dispatch
        self.last_error = None
        # Serialises everything that writes cards from this process.
        self.write_lock = threading.Lock()


def _pick_port(preferred):
    """Use the configured port, or the next free one above it."""
    for offset in range(0, 20):
        candidate = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", candidate))
            except OSError:
                continue
        return candidate
    raise store.BoardError(
        "no free port near %d -- set a different port in .kanban/config.json"
        % preferred
    )


def serve(board, port=None, open_browser=False, auto_dispatch=True):
    config = board.read_config()
    port = _pick_port(port or config.get("port") or 7777)
    server = DashboardServer(("127.0.0.1", port), board, auto_dispatch)
    url = "http://127.0.0.1:%d/" % port

    # flush=True: Python block-buffers stdout when it is redirected, so a log
    # file would stay empty until the buffer filled. The address is the one
    # thing a user needs immediately.
    print("kanban dashboard for %s" % board.root, flush=True)
    print("  %s" % url, flush=True)
    if not dispatch.available(config):
        print(
            "  note: Claude Code CLI not found, so agents cannot be started.\n"
            "        Set claude_bin in .kanban/config.json to fix that.",
            flush=True,
        )
    print("  (this window is the server -- close it to stop)", flush=True)

    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0
