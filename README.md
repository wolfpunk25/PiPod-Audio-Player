# PiPod Audio Player

An Adafruit MacroPad RP2040 controls VLC on a Raspberry Pi over USB. The Pi serves a small web page for uploading audio files and creating album folders. Music is discovered directly from the folders, so no playlist configuration is needed.

This is an adaptation of [Carlos Olmos's MacroPad Jukebox](https://github.com/carlosolmos/macropadjukebox) ([MIT license](https://github.com/carlosolmos/macropadjukebox/blob/main/LICENSE)).

## Controls

| Control | Action |
| --- | --- |
| Encoder turn | Move through the current folder |
| Encoder press or key 8 | Open folder or play the selected song |
| Key 5 | Go to parent folder |
| Key 6 | Play every audio file in the selected folder, or the current folder |
| Key 4 | Toggle folder browser and now playing screen |
| Hold key 7 | Show the PiPod web address; release to return |
| Keys 1, 2, 3 | Play, pause, stop |
| Keys 9, 12 | Volume up, down |
| Keys 10, 11 | Previous, next chapter when the file has chapters; otherwise previous, next track |

The 3×4 key layout follows the numbered physical keys:

| 1 Play | 2 Pause | 3 Stop |
| --- | --- | --- |
| 4 Screen | 5 Back | 6 Play Folder |
| 7 Hold: Web Address | 8 Select | 9 Volume Up |
| 10 Previous | 11 Next | 12 Volume Down |

The keys light up by action: Play green, Pause yellow, Stop red, screen toggle purple, web address white, Previous and Next blue, Volume Down and Up orange, Select and Play Folder teal, and Back grey.
The Play key gently pulses while audio is playing and stays steady when paused or stopped.

The now playing screen displays the embedded track title when present, falling back to the filename, plus playback state, elapsed and total time, and VLC volume. Folder navigation includes the path, selection, and item count. Audio formats recognized: MP3, FLAC, M4A, M4B, AAC, OGG, Opus, WAV, AIFF, WMA. VLC must have a decoder for the particular file.

For files longer than ten minutes, PiPod saves playback position every 15 seconds and when you pause, stop, or switch tracks. Selecting a saved file resumes from that point. The positions are stored in `~/Music/.pipod-positions.json` and survive a Pi restart. Keys 10 and 11 move between embedded chapters when the current file has them; otherwise they move between playlist tracks. At the first or last chapter, they move to the previous or next playlist track. PiPod clears a saved position near the end of a file so a completed book starts at the beginning.

## Pi setup

1. Connect the MacroPad directly to the Pi with a data-capable USB cable. Connect the Pi's audio output to a speaker or headphones.
2. Install VLC, FFprobe, and Python serial support if they are absent: `sudo apt-get update && sudo apt-get install -y vlc ffmpeg python3-serial`.
3. Copy this repository to `~/pipod-audio-player` on the Pi. Create `~/Music` if needed. Files uploaded from the web page go there.
4. Run `sh ~/pipod-audio-player/pi/install.sh`. It creates a private `pipod.env` file with an access token and enables the player services. The existing token is retained if upgrading from the earlier MacroPad Jukebox installation.

   Example environment file:

   ```sh
   PIPOD_MUSIC=/home/your-user/Music
   PIPOD_WEB_PORT=8080
   PIPOD_TOKEN=replace-with-a-long-random-token
   ```

   Generate a new token with `python3 -c 'import secrets; print(secrets.token_urlsafe(24))'` if you want to replace it.

5. To manage the two user services manually:

   ```sh
   systemctl --user status pipod-vlc.service pipod.service
   ```

6. Copy `macropad/boot.py` and `macropad/code.py` to the CIRCUITPY drive, then power cycle the MacroPad. Its `lib` directory needs `adafruit_macropad.mpy`, `adafruit_display_text`, and their dependencies from the matching CircuitPython library bundle.
7. Open `http://<pi-address>:8080/` on the Mac (currently [192.168.68.54](http://192.168.68.54:8080/)). Enter the token when prompted, then create album folders and upload audio files. To display the token on the Pi, run `sed -n 's/^PIPOD_TOKEN=//p' ~/pipod-audio-player/pipod.env`.

Check logs in `~/pipod-audio-player/pipod.log` and `~/pipod-audio-player/vlc.log`. The web server is intended for a trusted local network: its token is sent over HTTP, so do not forward port 8080 to the internet.

## Private access away from home without a PiPod token

Install [Tailscale](https://tailscale.com/docs/install/linux) on the Pi and on each phone, tablet, or computer that should reach PiPod. Sign all devices into the same tailnet. The Pi receives a private `100.x.y.z` Tailscale address; this address works when the devices are away from home as long as Tailscale is connected. Do not use Tailscale Funnel or forward port 8080 on the router, because those expose the uploader to the public internet.

After the Pi has joined the tailnet, run `sh ~/pipod-audio-player/pi/enable-tailscale.sh`. It sets the bind address and removes the PiPod token from the active configuration, keeping a private backup of the previous configuration. The resulting environment file contains:

```sh
PIPOD_MUSIC=/home/your-user/Music
PIPOD_WEB_PORT=8080
PIPOD_BIND=100.x.y.z
PIPOD_TOKEN=
```

Open `http://100.x.y.z:8080/` from a device connected to the tailnet. The uploader does not prompt for a PiPod token in this mode. PiPod refuses to start without a token if bound to the LAN or all network interfaces.

The web page can upload individual songs or a complete album folder. On a computer, choose **Upload album folder** to keep its folder structure, including subfolders. On a phone or tablet, create an album folder in the web page, open it, select multiple songs, and choose **Upload files**. Each listed audio file has a **Delete** button with a confirmation prompt; deletion is permanent. Existing files are never overwritten by an upload.

The original sequencer firmware is saved locally under `backups/macropad-before-jukebox-2026-09-20/`. That directory is excluded from Git. Restore its `code.py` and `boot.py` to CIRCUITPY to return to the sequencer.
