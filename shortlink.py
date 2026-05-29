#!/usr/bin/env python3
"""
shortlink.py — Self-hosted URL shortener for xameco.net
Runs as a systemd service. Links are stored in links.json (same directory).

Usage:
    GET  /<slug>          -> 301 redirect to target URL (or default redirect if slug unknown)
    GET  /healthz         -> {"status": "ok", "links": N}
    POST /add             -> add/update a link  (requires X-API-Key header)
    DELETE /remove/<slug> -> delete a link      (requires X-API-Key header)
    GET  /list            -> list all links     (requires X-API-Key header)
"""

import json
import logging
import logging.handlers
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, request
from werkzeug.middleware.proxy_fix import ProxyFix

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR    = Path(__file__).parent
LINKS_FILE  = BASE_DIR / "links.json"
LOG_FILE    = BASE_DIR / "shortlink.log"
HOST        = "127.0.0.1"                # Replace with your IP 
PORT        = 5000
API_KEY          = os.environ.get("SHORTLINK_API_KEY", "change-me-in-env")
LOG_LEVEL        = os.environ.get("LOG_LEVEL", "INFO").upper()
MAX_LOG_MB       = 10                    # rotate at 10 MB
LOG_BACKUPS      = 5                     # keep 5 rotated files
# If set, unknown slugs redirect here instead of returning 404.
# Leave empty or unset to keep the 404 behaviour.
DEFAULT_REDIRECT = os.environ.get("SHORTLINK_DEFAULT", "").strip()

SLUG_RE     = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
def setup_logging() -> logging.Logger:
    logger = logging.getLogger("shortlink")
    logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    fmt = logging.Formatter(
        fmt="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    # --- Rotating file handler (always on) ---
    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE,
        maxBytes=MAX_LOG_MB * 1024 * 1024,
        backupCount=LOG_BACKUPS,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # --- Stdout handler (captured by journald when run as systemd service) ---
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger


log = setup_logging()

# ---------------------------------------------------------------------------
# Links store
# ---------------------------------------------------------------------------
def load_links() -> dict:
    if not LINKS_FILE.exists():
        log.warning("links.json not found — starting with empty store: %s", LINKS_FILE)
        return {}
    try:
        with LINKS_FILE.open(encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            log.error("links.json is not a JSON object — starting empty")
            return {}
        return data
    except json.JSONDecodeError as exc:
        log.error("links.json parse error: %s", exc)
        return {}


def save_links(links: dict) -> None:
    tmp = LINKS_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(links, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp.replace(LINKS_FILE)        # atomic rename
    log.debug("links.json saved (%d entries)", len(links))


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = True

# Trust exactly 1 proxy hop (Traefik). Increase x_for if you have
# multiple proxies stacked in front (e.g. CDN → Traefik → Flask).
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

def require_api_key():
    key = request.headers.get("X-API-Key", "")
    if key != API_KEY:
        log.warning(
            "Unauthorized API call  method=%s path=%s ip=%s",
            request.method, request.path, request.remote_addr,
        )
        abort(401, description="Invalid or missing X-API-Key header")


# ---------------------------------------------------------------------------
# Request logging (every hit, before and after)
# ---------------------------------------------------------------------------
@app.before_request
def log_request():
    request._start_ts = datetime.now(timezone.utc)
    log.debug(
        "→ %s %s  ip=%s  ua=%s",
        request.method,
        request.full_path.rstrip("?"),
        request.remote_addr,
        request.headers.get("User-Agent", "-"),
    )


@app.after_request
def log_response(response):
    duration_ms = (
        (datetime.now(timezone.utc) - request._start_ts).total_seconds() * 1000
        if hasattr(request, "_start_ts")
        else -1
    )
    log.info(
        "← %s %s  status=%d  %.1fms  ip=%s",
        request.method,
        request.path,
        response.status_code,
        duration_ms,
        request.remote_addr,
    )
    return response


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/healthz")
def healthz():
    links = load_links()
    return jsonify(status="ok", links=len(links), file=str(LINKS_FILE), default_redirect=DEFAULT_REDIRECT or None)


@app.route("/list")
def list_links():
    require_api_key()
    links = load_links()
    log.info("Link list requested  count=%d  ip=%s", len(links), request.remote_addr)
    return jsonify(links)


@app.route("/add", methods=["POST"])
def add_link():
    require_api_key()
    body = request.get_json(silent=True) or {}
    slug   = body.get("slug", "").strip()
    target = body.get("target", "").strip()

    if not slug or not target:
        log.warning("Add rejected — missing slug or target  ip=%s", request.remote_addr)
        abort(400, description="Both 'slug' and 'target' fields are required")

    if not SLUG_RE.match(slug):
        log.warning("Add rejected — invalid slug %r  ip=%s", slug, request.remote_addr)
        abort(400, description="Slug must be 1–64 chars: letters, digits, _ or -")

    if not target.startswith(("http://", "https://")):
        abort(400, description="Target must start with http:// or https://")

    links = load_links()
    action = "updated" if slug in links else "created"
    old = links.get(slug)
    links[slug] = target
    save_links(links)

    log.info(
        "Link %s  slug=%s  target=%s%s  ip=%s",
        action, slug, target,
        f"  (was: {old})" if old else "",
        request.remote_addr,
    )
    return jsonify(status=action, slug=slug, target=target), 201


@app.route("/remove/<slug>", methods=["DELETE"])
def remove_link(slug):
    require_api_key()
    links = load_links()
    if slug not in links:
        log.warning("Remove — slug not found: %s  ip=%s", slug, request.remote_addr)
        abort(404, description=f"Slug '{slug}' not found")

    old_target = links.pop(slug)
    save_links(links)
    log.info("Link removed  slug=%s  was=%s  ip=%s", slug, old_target, request.remote_addr)
    return jsonify(status="removed", slug=slug, was=old_target)

@app.route("/")
def root():
    if DEFAULT_REDIRECT:
        log.info("Root hit  ip=%s  -> default redirect: %s", request.remote_addr, DEFAULT_REDIRECT)
        return redirect(DEFAULT_REDIRECT, 302)
    log.warning("Root hit with no default redirect configured  ip=%s", request.remote_addr)
    abort(404)

@app.route("/<slug>")
def go(slug):
    if not SLUG_RE.match(slug):
        log.debug("Invalid slug format: %r  ip=%s", slug, request.remote_addr)
        abort(404)

    links = load_links()
    target = links.get(slug)

    if target:
        log.info(
            "Redirect  slug=%s  -> %s  ip=%s  ref=%s",
            slug, target, request.remote_addr,
            request.headers.get("Referer", "-"),
        )
        return redirect(target, 301)

    if DEFAULT_REDIRECT:
        log.warning(
            "Slug not found: %s  ip=%s  -> default redirect: %s",
            slug, request.remote_addr, DEFAULT_REDIRECT,
        )
        return redirect(DEFAULT_REDIRECT, 302)   # 302 so browsers don't cache it

    log.warning("Slug not found: %s  ip=%s", slug, request.remote_addr)
    abort(404)


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------
@app.errorhandler(400)
def bad_request(e):
    return jsonify(error=str(e.description)), 400

@app.errorhandler(401)
def unauthorized(e):
    return jsonify(error=str(e.description)), 401

@app.errorhandler(404)
def not_found(e):
    return jsonify(error="Not found"), 404

@app.errorhandler(500)
def server_error(e):
    log.exception("Unhandled exception")
    return jsonify(error="Internal server error"), 500


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    log.info(
        "Starting shortlink service  host=%s  port=%d  links_file=%s",
        HOST, PORT, LINKS_FILE,
    )
    links = load_links()
    log.info("Loaded %d link(s) from %s", len(links), LINKS_FILE)

    app.run(host=HOST, port=PORT, debug=False)
