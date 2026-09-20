#!/usr/bin/env python3
"""PiPod Audio Player: folder browser, VLC control, serial UI, and uploads."""

import argparse
import ipaddress
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


AUDIO = {".mp3", ".flac", ".m4a", ".m4b", ".aac", ".ogg", ".opus", ".wav", ".aiff", ".wma"}
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


def status_file_path(status):
    """Get VLC's local file path without treating '?' or '#' as URL separators."""
    match = re.search(r"(?:new input|input):[ \t]*(.+?)[ \t]*\)[ \t]*(?:\r?\n|$)", status)
    if not match or not match.group(1).startswith("file://"):
        return None
    return Path(unquote(match.group(1)[len("file://"):]))


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
        self.title_cache = {}
        self.media_cache = {}
        self.bookmark_file = self.root / ".pipod-positions.json"
        try:
            saved = json.loads(self.bookmark_file.read_text())
            self.bookmarks = ({key: value for key, value in saved.items()
                               if isinstance(key, str) and type(value) is int and value > 0}
                              if isinstance(saved, dict) else {})
        except (OSError, ValueError):
            self.bookmarks = {}

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
                elif name in {"next", "prev"}:
                    previous = self.current_track()
                    self.save_progress()
                    if not self.skip_chapter(name):
                        self.vlc.call(name)
                        self.resume_current(previous)
                    else:
                        self.save_progress()
                    self.screen = "now"
                elif name in {"play", "pause", "stop", "volup", "voldown"}:
                    if name in {"pause", "stop"}:
                        self.save_progress()
                    was_stopped = name == "play" and "state stopped" in self.vlc.call("status")
                    command = {"volup": "volup 1", "voldown": "voldown 1"}.get(name, name)
                    self.vlc.call(command)
                    if was_stopped:
                        self.resume_current()
                    if name in {"play", "pause"}:
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
        self.save_progress()
        # VLC's RC input accepts file URIs. The generated playlist keeps paths with spaces safe.
        playlist = self.root / ".pipod-current.m3u8"
        playlist.write_text("#EXTM3U\n" + "\n".join(p.as_uri() for p in tracks) + "\n")
        self.vlc.call("clear")
        self.vlc.call("add " + playlist.as_uri())
        self.resume_current()
        self.screen = "now"

    def track_title(self, path):
        fallback = path.stem
        try:
            path = inside(self.root, path.resolve().relative_to(self.root))
            stamp = path.stat().st_mtime_ns
            key = (path, stamp)
            if key not in self.title_cache:
                result = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format_tags=title",
                     "-of", "json", str(path)], capture_output=True, text=True, timeout=3,
                    check=False)
                tags = json.loads(result.stdout).get("format", {}).get("tags", {}) if result.returncode == 0 else {}
                title = next((str(value).strip() for name, value in tags.items()
                              if name.lower() == "title" and str(value).strip()), fallback)
                if len(self.title_cache) >= 64:
                    self.title_cache.clear()
                self.title_cache[key] = title
            return self.title_cache[key]
        except (OSError, ValueError, subprocess.TimeoutExpired, json.JSONDecodeError):
            return fallback

    def media_details(self, path):
        """Read duration and chapter starts once per file version."""
        try:
            path = path.resolve()
            key = (path, path.stat().st_mtime_ns)
            if key not in self.media_cache:
                result = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries",
                     "format=duration:chapter=start_time", "-of", "json", str(path)],
                    capture_output=True, text=True, timeout=5, check=False)
                data = json.loads(result.stdout) if result.returncode == 0 else {}
                details = {
                    "duration": float(data.get("format", {}).get("duration", 0)),
                    "chapters": [float(item["start_time"]) for item in data.get("chapters", [])],
                }
                if len(self.media_cache) >= 64:
                    self.media_cache.clear()
                self.media_cache[key] = details
            return self.media_cache[key]
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            return {"duration": 0, "chapters": []}

    def current_track(self):
        path = status_file_path(self.vlc.call("status"))
        if path is None:
            return None
        path = path.resolve()
        if path.suffix.lower() not in AUDIO or self.root not in path.parents or not path.is_file():
            return None
        return path

    def _write_bookmarks(self):
        temporary = self.bookmark_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.bookmarks, indent=2) + "\n")
        temporary.replace(self.bookmark_file)

    def save_progress(self):
        with self.lock:
            try:
                path = self.current_track()
                if path is None:
                    return
                duration = self.media_details(path)["duration"]
                if duration <= 600:
                    return
                seconds = int(self.vlc.call("get_time").strip())
                if seconds <= 0:
                    return
                key = str(path.relative_to(self.root))
                if seconds >= duration - 30:
                    if key in self.bookmarks:
                        del self.bookmarks[key]
                        self._write_bookmarks()
                elif self.bookmarks.get(key) != seconds:
                    self.bookmarks[key] = seconds
                    self._write_bookmarks()
            except (OSError, ValueError, TimeoutError) as exc:
                LOG.warning("saving playback position: %s", exc)

    def forget_bookmark(self, path):
        with self.lock:
            key = str(path.relative_to(self.root))
            if key in self.bookmarks:
                del self.bookmarks[key]
                self._write_bookmarks()

    def resume_current(self, previous=None):
        if not self.bookmarks:
            return
        for _ in range(6):
            path = self.current_track()
            if path is not None and path != previous:
                seconds = self.bookmarks.get(str(path.relative_to(self.root)), 0)
                if seconds and seconds < self.media_details(path)["duration"] - 30:
                    self.vlc.call("seek " + str(seconds))
                return
            time.sleep(0.1)

    def skip_chapter(self, direction):
        path = self.current_track()
        if path is None:
            return False
        chapters = self.media_details(path)["chapters"]
        if len(chapters) < 2:
            return False
        match = re.search(r"\d+", self.vlc.call("chapter"))
        if not match:
            return False
        index = int(match.group())
        if direction == "next" and index < len(chapters) - 1:
            self.vlc.call("chapter_n")
        elif direction == "prev" and index > 0:
            self.vlc.call("chapter_p")
        else:
            return False
        return True

    def now(self):
        try:
            status = self.vlc.call("status")
            playing = self.vlc.call("is_playing")
            elapsed = self.vlc.call("get_time")
            length = self.vlc.call("get_length")
            volume = self.vlc.call("volume")
            path = status_file_path(status)
            title = self.track_title(path) if path else "Nothing playing"
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


def serial_worker(jukebox, serial_path=None, web_url=""):
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
                        state["web_url"] = web_url
                        encoded = (json.dumps(state, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
                        if encoded != last:
                            serial.write(encoded)
                            last = encoded
                        refresh = time.monotonic() + (1 if jukebox.screen == "now" else 2)
        except (IndexError, OSError, ValueError) as exc:
            LOG.info("waiting for MacroPad: %s", exc)
            time.sleep(3)


def progress_worker(jukebox):
    while True:
        time.sleep(15)
        jukebox.save_progress()


def private_bind(address):
    try:
        ip = ipaddress.ip_address(address)
        return ip.is_loopback or ip in ipaddress.ip_network("100.64.0.0/10")
    except ValueError:
        return False


def make_handler(jukebox, token):
    class Handler(BaseHTTPRequestHandler):
        def _authorized(self):
            if not token:
                return True
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
                body = (Path(__file__).with_name("web.html")).read_bytes().replace(
                    b"__PIPOD_AUTH_REQUIRED__", b"true" if token else b"false")
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
                    if target.is_symlink():
                        raise ValueError("invalid folder")
                    target.mkdir(exist_ok=True)
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

        def do_DELETE(self):
            if not self._require_auth():
                return
            parsed = urlparse(self.path)
            if parsed.path != "/api/file":
                self._json(404, {"error": "Not found"})
                return
            try:
                rel = parse_qs(parsed.query).get("path", [""])[0]
                parts = Path(rel).parts
                if not parts or any(part in {".", ".."} or part.startswith(".") for part in parts):
                    raise ValueError("invalid file path")
                candidate = jukebox.root.joinpath(*parts)
                if any(path.is_symlink() for path in (candidate, *candidate.parents) if path != jukebox.root):
                    raise ValueError("invalid file path")
                target = inside(jukebox.root, rel)
                if target.suffix.lower() not in AUDIO or not target.is_file():
                    raise ValueError("audio file does not exist")
                target.unlink()
                jukebox.forget_bookmark(target)
                self._json(200, {"ok": True})
            except (ValueError, OSError) as exc:
                self._json(400, {"error": str(exc)})

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--music", default=os.environ.get("PIPOD_MUSIC", "~/Music"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PIPOD_WEB_PORT", "8080")))
    parser.add_argument("--bind", default=os.environ.get("PIPOD_BIND", "0.0.0.0"))
    parser.add_argument("--serial", default=os.environ.get("PIPOD_SERIAL"))
    parser.add_argument("--token", default=os.environ.get("PIPOD_TOKEN"))
    args = parser.parse_args()
    if args.token and len(args.token) < 16:
        parser.error("PIPOD_TOKEN must have at least 16 characters")
    if not args.token and not private_bind(args.bind):
        parser.error("Token-free mode requires PIPOD_BIND to be loopback or a Tailscale IP")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    player = VLC()
    jukebox = PiPod(args.music, player)
    web_url = f"http://{args.bind}:{args.port}/"
    threading.Thread(target=serial_worker, args=(jukebox, args.serial, web_url), daemon=True).start()
    threading.Thread(target=progress_worker, args=(jukebox,), daemon=True).start()
    server = ThreadingHTTPServer((args.bind, args.port), make_handler(jukebox, args.token))
    LOG.info("web uploads available on %s:%d; music root %s", args.bind, args.port, jukebox.root)
    server.serve_forever()


if __name__ == "__main__":
    main()
