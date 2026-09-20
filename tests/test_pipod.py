import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from pi.pipod import PiPod, entries, inside, make_handler, private_bind


class FakeVLC:
    def __init__(self):
        self.commands = []

    def call(self, command):
        self.commands.append(command)
        return {"status": "( state playing )\n( new input: file:///Music/Track.mp3 )",
                "is_playing": "1", "get_time": "25", "get_length": "100",
                "volume": "volume: 256"}.get(command, "")


class PiPodTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "Album").mkdir()
        (self.root / "Album" / "Track.mp3").write_bytes(b"sample")
        (self.root / "ignored.txt").write_text("not audio")
        self.vlc = FakeVLC()
        self.jukebox = PiPod(self.root, self.vlc)

    def tearDown(self):
        self.tmp.cleanup()

    def test_navigation_and_playback(self):
        self.assertEqual([p.name for p in entries(self.root)], ["Album"])
        self.jukebox.action("select")
        self.assertEqual(self.jukebox.browse()["name"], "Track.mp3")
        self.jukebox.action("select")
        self.assertEqual(self.jukebox.screen, "now")
        self.assertEqual(self.vlc.commands[0], "clear")
        self.assertTrue(self.vlc.commands[1].startswith("add file://"))
        self.assertEqual(self.jukebox.now()["title"], "Track")
        self.jukebox.action("back")
        self.assertEqual(self.jukebox.browse()["name"], "Album")

    def test_path_cannot_escape_music(self):
        with self.assertRaises(ValueError):
            inside(self.root, "../elsewhere")
        outside = self.root.parent / "outside.mp3"
        (self.root / "link.mp3").symlink_to(outside)
        self.assertEqual([p.name for p in entries(self.root)], ["Album"])

    def test_now_playing_toggle(self):
        self.assertEqual(self.jukebox.display()["type"], "browse")
        self.jukebox.action("now")
        self.assertEqual(self.jukebox.display()["type"], "now")
        self.jukebox.action("now")
        self.assertEqual(self.jukebox.display()["type"], "browse")

    def test_now_playing_uses_metadata_and_filename_fallback(self):
        track = self.root / "Album" / "15 Does Your Mother Know.mp3"
        track.write_bytes(b"sample")
        self.vlc.call = lambda command: (
            "( state playing )\r\n( new input: file://" + str(track) + " )\r\n"
            if command == "status" else "1")
        with patch("pi.pipod.subprocess.run") as probe:
            probe.return_value.returncode = 0
            probe.return_value.stdout = '{"format":{"tags":{"title":"Does Your Mother Know"}}}'
            self.assertEqual(self.jukebox.now()["title"], "Does Your Mother Know")
            self.assertEqual(self.jukebox.now()["title"], "Does Your Mother Know")
            self.assertEqual(probe.call_count, 1)
        self.jukebox.title_cache.clear()
        with patch("pi.pipod.subprocess.run") as probe:
            probe.return_value.returncode = 0
            probe.return_value.stdout = '{"format":{}}'
            self.assertEqual(self.jukebox.now()["title"], "15 Does Your Mother Know")

    def test_token_free_mode_is_limited_to_private_bind(self):
        self.assertTrue(private_bind("127.0.0.1"))
        self.assertTrue(private_bind("100.101.102.103"))
        self.assertFalse(private_bind("0.0.0.0"))
        self.assertFalse(private_bind("192.168.68.54"))
        self.assertFalse(private_bind("8.8.8.8"))

    def test_token_free_browser(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.jukebox, None))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
            conn.request("GET", "/")
            page = conn.getresponse().read().decode()
            self.assertIn('data-auth="false"', page)
            conn.request("GET", "/api/list?path=")
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["items"][0]["name"], "Album")
            conn.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_authenticated_upload_and_directory_creation(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.jukebox, "0123456789abcdef"))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
            conn.request("GET", "/api/list?path=")
            self.assertEqual(conn.getresponse().status, 401)
            headers = {"Authorization": "Bearer 0123456789abcdef"}
            conn.request("POST", "/api/mkdir?path=&name=New", headers=headers)
            self.assertEqual(conn.getresponse().status, 201)
            conn.request("POST", "/api/mkdir?path=&name=New", headers=headers)
            self.assertEqual(conn.getresponse().status, 201)
            conn.request("POST", "/api/upload?path=New&name=Song.mp3", body=b"music", headers=headers)
            self.assertEqual(conn.getresponse().status, 201)
            self.assertEqual((self.root / "New" / "Song.mp3").read_bytes(), b"music")
            conn.request("POST", "/api/upload?path=New&name=Song.mp3", body=b"replacement", headers=headers)
            self.assertEqual(conn.getresponse().status, 400)
            self.assertEqual((self.root / "New" / "Song.mp3").read_bytes(), b"music")
            conn.request("POST", "/api/upload?path=..%2F..&name=bad.mp3", body=b"music", headers=headers)
            self.assertEqual(conn.getresponse().status, 400)
            conn.request("GET", "/api/list?path=New", headers=headers)
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["items"][0]["name"], "Song.mp3")
            conn.request("DELETE", "/api/file?path=New%2FSong.mp3")
            self.assertEqual(conn.getresponse().status, 401)
            conn.request("DELETE", "/api/file?path=New%2FSong.mp3", headers=headers)
            self.assertEqual(conn.getresponse().status, 200)
            self.assertFalse((self.root / "New" / "Song.mp3").exists())
            conn.request("DELETE", "/api/file?path=..%2Fignored.txt", headers=headers)
            self.assertEqual(conn.getresponse().status, 400)
            self.assertTrue((self.root / "ignored.txt").exists())
            conn.close()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
