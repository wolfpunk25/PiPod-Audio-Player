#!/usr/bin/env python3
"""PiPod Audio Player: folder browser, VLC control, serial UI, and uploads."""

import argparse
import json
import logging
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse


AUDIO = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".aiff", ".wma"}
MAX_UPLOAD = 4 * 1024 ** 3
LOG = logging.getLogger("pipod")


def inside(root, relative):
    """Resolve a client path without permitting escape from the music directory."""
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if path != root and root not in path.parents:
        raise ValueError("path is outside music directory")
    return path


def entries(folder):
    return sorted(
        (p for p in folder.iterdir() if not p.name.startswith(".") and not p.is_symlink() and
         (p.is_dir() or (p.is_file() and p.suffix.lower() in AUDIO))),
        key=lambda p: (not p.is_dir(), p.name.casefold()),
    )


class VLC:
    def __init__(self, host="127.0.0.1", port=8888):
        self.host, self.port = host, port
        self.lock = threading.Lock()

    def call(self, command):
        if "\n" in command or "\r" in command:
            raise ValueError("invalid VLC command")
        with self.lock, socket.create_connection((self.host, self.port), timeout=2) as sock:
            sock.settimeout(2)
            self._read_prompt(sock)
            sock.sendall((command + "\n").encode())
            response = self._read_prompt(sock)
            sock.sendall(b"quit\n")
            return response.strip()

    @staticmethod
    def _read_prompt(sock):
        data = bytearray()
        while len(data) < 65536:
            part = sock.recv(4096)
            if not part:
                break
            data.extend(part)
            if data.endswith(b"> ") or data.endswith(b">\n"):
                break
        return data.decode("utf-8", "replace").removesuffix("> ")


class PiPod:
    def __init__(self, music_root, vlc):
        self.root = Path(music_root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.vlc = vlc
        self.folder = self.root
        self.position = 0
        self.screen = "browse"
        self.lock = threading.RLock()
        self.error = ""

    def browse(self):
        with self.lock:
            items = entries(self.folder)
            self.position = min(self.position, max(0, len(items) - 1))
            chosen = items[self.position] if items else None
            return {"type": "browse", "path": str(self.folder.relative_to(self.root)) or "/",
                    "name": chosen.name if chosen else "(empty)",
                    "directory": chosen.is_dir() if chosen else False,
                    "index": self.position + 1 if chosen else 0, "count": len(items),
                    "error": self.error}

    def action(self, name):
        with self.lock:
            self.error = ""
            try:
                items = entries(self.folder)
                selected = items[self.position] if items and self.position < len(items) else None
                if name == "up":
                    self.position = max(0, self.position - 1)
                    self.screen = "browse"
                elif name == "down":
                    self.position = min(max(0, len(items) - 1), self.position + 1)
                    self.screen = "browse"
                elif name == "back":
                    if self.folder != self.root:
                        child = self.folder
                        self.folder = self.folder.parent
                        parent_items = entries(self.folder)
                        self.position = parent_items.index(child) if child in parent_items else 0
                    self.screen = "browse"
                elif name == "select" and selected:
                    if selected.is_dir():
                        self.folder, self.position, self.screen = selected, 0, "browse"
                    else:
                        self.play_path(selected)
                elif name == "play_folder":
                    self.play_path(selected if selected and selected.is_dir() else self.folder)
                elif name == "now":
                    self.screen = "now" if self.screen == "browse" else "browse"
                elif name in {"play", "pause", "stop", "next", "prev", "volup", "voldown"}:
                    command = {"volup": "volup 1", "voldown": "voldown 1"}.get(name, name)
                    self.vlc.call(command)
                    if name in {"play", "pause", "next", "prev"}:
                        self.screen = "now"
                else:
                    return
            except (OSError, ValueError, TimeoutError) as exc:
                LOG.warning("action %s: %s", name, exc)
                self.error = str(exc)[:40]

    def play_path(self, path):
        tracks = ([path] if path.is_file() else sorted(
            (p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO),
            key=lambda p: str(p).casefold()))
        if not tracks:
            raise ValueError("No audio files in this folder")
        # VLC's RC input accepts file URIs. The generated playlist keeps paths with spaces safe.
        playlist = self.root / ".pipod-current.m3u8"
        playlist.write_text("#EXTM3U\n" + "\n".join(p.as_uri() for p in tracks) + "\n")
        self.vlc.call("clear")
        self.vlc.call("add " + playlist.as_uri())
        self.screen = "now"

    def now(self):
        try:
            status = self.vlc.call("status")
            playing = self.vlc.call("is_playing")
            elapsed = self.vlc.call("get_time")
            length = self.vlc.call("get_length")
            volume = self.vlc.call("volume")
            match = re.search(r"(?:new input|input):\s*(\S+)", status)
            uri = match.group(1) if match else ""
            title = Path(unquote(urlparse(uri).path)).stem if uri else "Nothing playing"
            state_match = re.search(r"state\s+([^\s)]+)", status)
            state = state_match.group(1) if state_match else ("playing" if playing.strip().startswith("1") else "stopped")
            def number(s):
                found = re.search(r"-?\d+", s)
                return int(found.group()) if found else 0
            return {"type": "now", "title": title, "state": state,
                    "elapsed": number(elapsed), "length": number(length),
                    "volume": number(volume), "error": self.error}
        except (OSError, ValueError, TimeoutError) as exc:
            return {"type": "now", "title": "VLC unavailable", "state": "offline",
                    "elapsed": 0, "length": 0, "volume": 0, "error": str(exc)[:40]}

    def display(self):
        return self.now() if self.screen == "now" else self.browse()


def serial_worker(jukebox, serial_path=None):
    from serial import Serial
    from serial.tools import list_ports
    while True:
        try:
            if serial_path:
                port = serial_path
            else:
                candidates = [p for p in list_ports.comports() if p.vid == 0x239A]
                data_ports = [p for p in candidates if "data" in (p.interface or "").lower()]
                port = (data_ports or sorted(candidates, key=lambda p: p.device, reverse=True))[0].device
            with Serial(port, baudrate=115200, timeout=0.2, write_timeout=2) as serial:
                LOG.info("MacroPad connected on %s", port)
                last = None
                refresh = 0
                while True:
                    line = serial.readline(128)
                    if line:
                        action = line.decode("ascii", "ignore").strip()
                        if action == "hello":
                            last = None
                        else:
                            LOG.info("MacroPad action: %s", action)
                            jukebox.action(action)
                    if time.monotonic() >= refresh or line:
                        state = jukebox.display()
                        encoded = (json.dumps(state, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
                        if encoded != last:
                            serial.write(encoded)
                            last = encoded
                        refresh = time.monotonic() + (1 if jukebox.screen == "now" else 2)
        except (IndexError, OSError, ValueError) as exc:
            LOG.info("waiting for MacroPad: %s", exc)
            time.sleep(3)


def make_handler(jukebox, token):
    class Handler(BaseHTTPRequestHandler):
        def _authorized(self):
            return secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token)

        def _json(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _require_auth(self):
            if self._authorized():
                return True
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "Enter the PiPod access token"})
            return False

        def do_GET(self):
            if self.path == "/":
                body = (Path(__file__).with_name("web.html")).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if not self._require_auth():
                return
            if self.path.startswith("/api/list?"):
                try:
                    rel = parse_qs(urlparse(self.path).query).get("path", [""])[0]
                    folder = inside(jukebox.root, rel)
                    if not folder.is_dir():
                        raise ValueError("folder does not exist")
                    self._json(200, {"path": rel, "items": [
                        {"name": p.name, "directory": p.is_dir()} for p in entries(folder)]})
                except (ValueError, OSError) as exc:
                    self._json(400, {"error": str(exc)})
            elif self.path == "/api/status":
                self._json(200, jukebox.now())
            else:
                self._json(404, {"error": "Not found"})

        def do_POST(self):
            if not self._require_auth():
                return
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            try:
                rel = query.get("path", [""])[0]
                folder = inside(jukebox.root, rel)
                if not folder.is_dir():
                    raise ValueError("folder does not exist")
                name = query.get("name", [""])[0]
                if not name or name in {".", ".."} or "/" in name or "\\" in name or name.startswith("."):
                    raise ValueError("invalid name")
                target = inside(folder, name)
                if parsed.path == "/api/mkdir":
                    target.mkdir(exist_ok=False)
                elif parsed.path == "/api/upload":
                    if target.suffix.lower() not in AUDIO:
                        raise ValueError("unsupported audio file")
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= MAX_UPLOAD:
                        raise ValueError("invalid upload size")
                    # Exclusive create prevents accidental replacement of an existing song.
                    created = False
                    try:
                        with target.open("xb") as output:
                            created = True
                            remaining = size
                            while remaining:
                                chunk = self.rfile.read(min(1024 * 1024, remaining))
                                if not chunk:
                                    raise ConnectionError("upload ended early")
                                output.write(chunk)
                                remaining -= len(chunk)
                    except (OSError, ConnectionError):
                        if created:
                            target.unlink(missing_ok=True)
                        raise
                else:
                    self._json(404, {"error": "Not found"})
                    return
                self._json(201, {"ok": True})
            except (ValueError, OSError, ConnectionError) as exc:
                self._json(400, {"error": str(exc)})

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--music", default=os.environ.get("PIPOD_MUSIC", "~/Music"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PIPOD_WEB_PORT", "8080")))
    parser.add_argument("--serial", default=os.environ.get("PIPOD_SERIAL"))
    parser.add_argument("--token", default=os.environ.get("PIPOD_TOKEN"))
    args = parser.parse_args()
    if not args.token or len(args.token) < 16:
        parser.error("Set PIPOD_TOKEN to a random token of at least 16 characters")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    player = VLC()
    jukebox = PiPod(args.music, player)
    threading.Thread(target=serial_worker, args=(jukebox, args.serial), daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(jukebox, args.token))
    LOG.info("web uploads available on port %d; music root %s", args.port, jukebox.root)
    server.serve_forever()


if __name__ == "__main__":
    main()
