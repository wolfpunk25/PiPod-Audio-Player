"""Run on the Pi to verify live upload, VLC playback, and status parsing."""

import io
import json
import sys
import time
import urllib.request
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pi"))
from pipod import PiPod, VLC


home = Path.home()
env = dict(line.split("=", 1) for line in
           (home / "pipod-audio-player" / "pipod.env").read_text().splitlines() if "=" in line)
root = Path(env["PIPOD_MUSIC"])
name = "pipod-smoke-test.wav"
target = root / name
audio = io.BytesIO()
with wave.open(audio, "wb") as output:
    output.setnchannels(1)
    output.setsampwidth(2)
    output.setframerate(8000)
    output.writeframes(b"\0\0" * 16000)
request = urllib.request.Request(
    "http://" + env.get("PIPOD_BIND", "127.0.0.1") + ":8080/api/upload?path=&name=" + name,
    data=audio.getvalue(), method="POST",
    headers=({"Authorization": "Bearer " + env["PIPOD_TOKEN"]}
             if env.get("PIPOD_TOKEN") else {}))
vlc = VLC()
try:
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 201
    assert target.exists()
    jukebox = PiPod(root, vlc)
    jukebox.play_path(target)
    time.sleep(0.5)
    status = jukebox.now()
    print(json.dumps(status))
    assert "pipod-smoke-test" in status["title"]
finally:
    vlc.call("stop")
    target.unlink(missing_ok=True)
