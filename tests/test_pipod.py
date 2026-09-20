import http.client
import json
import socket
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote
from unittest.mock import patch

from pi.pipod import PiPod, UPLOAD_CHUNK, entries, inside, make_handler, private_bind, status_file_path


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
        self.assertIn("clear", self.vlc.commands)
        self.assertTrue(any(command.startswith("add file://") for command in self.vlc.commands))
        self.assertEqual(self.jukebox.now()["title"], "Track")
        self.jukebox.action("back")
        self.assertEqual(self.jukebox.browse()["name"], "Album")

    def test_selecting_middle_song_keeps_album_for_previous_and_next(self):
        album = self.root / "Album"
        for name in ("01 First.mp3", "02 Middle.mp3", "03 Last.mp3"):
            (album / name).write_bytes(b"sample")

        class PlaylistVLC:
            def __init__(self):
                self.commands = []
                self.tracks = []
                self.index = 0

            def call(self, command):
                self.commands.append(command)
                if command == "clear":
                    self.tracks = []
                elif command.startswith("add "):
                    playlist = Path(command[4:].removeprefix("file://"))
                    self.tracks = [Path(unquote(uri.removeprefix("file://"))) for uri in
                                   playlist.read_text().splitlines()[1:]]
                    self.index = 0
                elif command == "playlist":
                    return ("| 1 - Playlist\n" + "\n".join(
                        "|  %s%d - %s" % ("*" if i == self.index else " ", i + 10, track.name)
                        for i, track in enumerate(self.tracks)))
                elif command.startswith("goto "):
                    self.index = int(command[5:]) - 10
                elif command == "next":
                    self.index = min(self.index + 1, len(self.tracks) - 1)
                elif command == "prev":
                    self.index = max(self.index - 1, 0)
                elif command == "status":
                    return "( new input: %s )\n( state playing )" % self.tracks[self.index].as_uri() if self.tracks else "( state stopped )"
                return "0" if command == "get_time" else ""

        vlc = PlaylistVLC()
        player = PiPod(self.root, vlc)
        player.media_details = lambda path: {"duration": 0, "chapters": []}
        player.play_path(album / "02 Middle.mp3")
        self.assertEqual([p.name for p in vlc.tracks],
                         ["01 First.mp3", "02 Middle.mp3", "03 Last.mp3", "Track.mp3"])
        self.assertEqual(player.current_track().name, "02 Middle.mp3")
        player.action("prev")
        self.assertEqual(player.current_track().name, "01 First.mp3")
        player.action("next")
        self.assertEqual(player.current_track().name, "02 Middle.mp3")
        player.action("next")
        self.assertEqual(player.current_track().name, "03 Last.mp3")

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

    def test_vlc_path_preserves_question_mark_in_audiobook_name(self):
        track = self.root / "How Are You? Its Alan (Partridge).m4a"
        track.write_bytes(b"sample")
        status = "( new input: file://" + str(track) + " )\r\n( state paused )"
        self.assertEqual(status_file_path(status), track)
        self.vlc.call = lambda command: status if command == "status" else "1"
        with patch("pi.pipod.subprocess.run") as probe:
            probe.return_value.returncode = 0
            probe.return_value.stdout = '{"format":{"tags":{"title":"How Are You? Its Alan"}}}'
            self.assertEqual(self.jukebox.now()["title"], "How Are You? Its Alan")

    def test_audiobook_position_survives_restart_and_chapter_keys(self):
        book = self.root / "Book.m4b"
        book.write_bytes(b"sample")

        class BookVLC:
            def __init__(self):
                self.commands = []
                self.current = None
                self.elapsed = 0
                self.chapter = 0

            def call(self, command):
                self.commands.append(command)
                if command == "status":
                    return ("( new input: " + self.current.as_uri() + " )\n( state playing )"
                            if self.current else "( state stopped )")
                if command.startswith("add "):
                    self.current = book
                    self.elapsed = 0
                elif command.startswith("seek "):
                    self.elapsed = int(command.split()[1])
                elif command == "chapter_n":
                    self.chapter += 1
                    self.elapsed = [0, 300, 700][self.chapter]
                elif command == "chapter_p":
                    self.chapter -= 1
                    self.elapsed = [0, 300, 700][self.chapter]
                elif command == "get_time":
                    return str(self.elapsed)
                elif command == "chapter":
                    return str(self.chapter)
                return ""

        vlc = BookVLC()
        player = PiPod(self.root, vlc)
        player.media_details = lambda path: {"duration": 1200, "chapters": [0, 300, 700]}
        player.bookmarks["Book.m4b"] = 125
        player._write_bookmarks()
        player.play_path(book)
        self.assertIn("seek 125", vlc.commands)
        vlc.elapsed = 225
        player.save_progress()
        self.assertEqual(PiPod(self.root, vlc).bookmarks["Book.m4b"], 225)
        player.action("next")
        self.assertIn("chapter_n", vlc.commands)
        self.assertEqual(vlc.chapter, 1)
        player.action("prev")
        self.assertIn("chapter_p", vlc.commands)
        self.assertEqual(vlc.chapter, 0)

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
            conn.request("POST", "/api/upload?path=New&name=Book.m4b", body=b"book", headers=headers)
            self.assertEqual(conn.getresponse().status, 201)
            self.assertEqual((self.root / "New" / "Song.mp3").read_bytes(), b"music")
            self.assertFalse(list((self.root / "New").glob(".pipod-upload-*")))
            conn.request("POST", "/api/upload?path=New&name=Song.mp3", body=b"replacement", headers=headers)
            self.assertEqual(conn.getresponse().status, 400)
            self.assertEqual((self.root / "New" / "Song.mp3").read_bytes(), b"music")
            conn.request("POST", "/api/upload?path=..%2F..&name=bad.mp3", body=b"music", headers=headers)
            self.assertEqual(conn.getresponse().status, 400)
            conn.request("GET", "/api/list?path=New", headers=headers)
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual([item["name"] for item in json.loads(response.read())["items"]],
                             ["Book.m4b", "Song.mp3"])
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

    def test_interrupted_upload_leaves_no_partial_audio_file(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.jukebox, "0123456789abcdef"))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with socket.create_connection(("127.0.0.1", server.server_port)) as conn:
                conn.sendall(
                    b"POST /api/upload?path=Album&name=Interrupted.m4b HTTP/1.1\r\n"
                    b"Host: 127.0.0.1\r\n"
                    b"Authorization: Bearer 0123456789abcdef\r\n"
                    b"Content-Length: 20\r\n\r\npartial")
                conn.shutdown(socket.SHUT_WR)
                self.assertIn(b"400", conn.recv(1024))
            self.assertFalse((self.root / "Album" / "Interrupted.m4b").exists())
            self.assertFalse(list((self.root / "Album").glob(".pipod-upload-*")))
        finally:
            server.shutdown()
            server.server_close()

    def test_large_upload_resumes_after_interrupted_chunk(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.jukebox, "0123456789abcdef"))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            data = b"a" * UPLOAD_CHUNK + b"b" * UPLOAD_CHUNK + b"end"
            query = "path=Album&name=Large.m4b&id=" + "a" * 32 + "&total=" + str(len(data))
            headers = {"Authorization": "Bearer 0123456789abcdef"}
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
            conn.request("PUT", "/api/upload/chunk?" + query + "&offset=0",
                         body=data[:UPLOAD_CHUNK], headers=headers)
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["offset"], UPLOAD_CHUNK)
            self.assertFalse((self.root / "Album" / "Large.m4b").exists())
            conn.request("GET", "/api/upload/status?" + query, headers=headers)
            self.assertEqual(json.loads(conn.getresponse().read())["offset"], UPLOAD_CHUNK)
            with socket.create_connection(("127.0.0.1", server.server_port)) as broken:
                broken.sendall((
                    "PUT /api/upload/chunk?" + query + "&offset=" + str(UPLOAD_CHUNK) + " HTTP/1.1\r\n"
                    "Host: 127.0.0.1\r\n"
                    "Authorization: Bearer 0123456789abcdef\r\n"
                    "Content-Length: " + str(UPLOAD_CHUNK) + "\r\n\r\n").encode() + b"partial")
                broken.shutdown(socket.SHUT_WR)
                self.assertIn(b"400", broken.recv(1024))
            conn.request("GET", "/api/upload/status?" + query, headers=headers)
            self.assertEqual(json.loads(conn.getresponse().read())["offset"], UPLOAD_CHUNK)
            conn.request("PUT", "/api/upload/chunk?" + query + "&offset=" + str(UPLOAD_CHUNK),
                         body=data[UPLOAD_CHUNK:2 * UPLOAD_CHUNK], headers=headers)
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            conn.request("PUT", "/api/upload/chunk?" + query + "&offset=" + str(2 * UPLOAD_CHUNK),
                         body=b"end", headers=headers)
            response = conn.getresponse()
            self.assertEqual(response.status, 201)
            response.read()
            self.assertEqual((self.root / "Album" / "Large.m4b").read_bytes(), data)
            conn.request("GET", "/api/upload/status?" + query, headers=headers)
            self.assertTrue(json.loads(conn.getresponse().read())["complete"])
            conn.request("GET", "/api/upload/status?" + query.replace("a" * 32, "b" * 32), headers=headers)
            response = conn.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
            self.assertFalse(list((self.root / "Album").glob(".pipod-upload-*")))
            conn.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_folder_delete_removes_contents_without_escaping_music(self):
        album = self.root / "Album"
        disc = album / "Disc 1"
        disc.mkdir()
        (disc / "Chapter.m4b").write_bytes(b"book")
        commands = []
        def playing_vlc(command):
            commands.append(command)
            return ("( new input: %s )\n( state playing )" % (disc / "Chapter.m4b").as_uri()
                    if command == "status" else "0" if command == "get_time" else "")
        self.vlc.call = playing_vlc
        self.jukebox.folder = disc.resolve()
        self.jukebox.bookmarks = {"Album/Disc 1/Chapter.m4b": 80, "Other.m4b": 90}
        self.jukebox._write_bookmarks()
        with tempfile.TemporaryDirectory() as outside_dir:
            outside = Path(outside_dir) / "outside.mp3"
            outside.write_bytes(b"keep")
            (disc / "link.mp3").symlink_to(outside)
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.jukebox, "0123456789abcdef"))
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
                headers = {"Authorization": "Bearer 0123456789abcdef"}
                for path in ("", "..%2FAlbum", "Album%2FDisc%201%2Flink.mp3"):
                    conn.request("DELETE", "/api/folder?path=" + path, headers=headers)
                    self.assertEqual(conn.getresponse().status, 400)
                conn.request("DELETE", "/api/folder?path=Album")
                self.assertEqual(conn.getresponse().status, 401)
                self.assertTrue(album.exists())
                conn.request("DELETE", "/api/folder?path=Album", headers=headers)
                self.assertEqual(conn.getresponse().status, 200)
                self.assertFalse(album.exists())
                self.assertEqual(outside.read_bytes(), b"keep")
                self.assertIn("stop", commands)
                self.assertIn("clear", commands)
                self.assertEqual(self.jukebox.folder, self.jukebox.root)
                self.assertEqual(self.jukebox.bookmarks, {"Other.m4b": 90})
                conn.close()
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
