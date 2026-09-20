"""MacroPad UI for PiPod Audio Player. Requires boot.py and Adafruit libraries."""

import json
import time
import usb_cdc
import displayio
import terminalio
from adafruit_display_text import label
from adafruit_macropad import MacroPad

pad = MacroPad()
pad.pixels.brightness = 0.12
pad.pixels.fill(0)
pad.pixels[0] = 0x00AA44  # play
pad.pixels[1] = 0xCCAA00  # pause
pad.pixels[2] = 0xAA2222  # stop
pad.pixels[3] = 0x6633AA  # now playing
pad.pixels[4] = 0x666666  # back
pad.pixels[5] = 0x008888  # play folder
pad.pixels[6] = 0xFFFFFF  # hold to show web address
pad.pixels[7] = 0x008888  # select
pad.pixels[8] = 0x884400  # louder
pad.pixels[9] = 0x2244AA  # previous
pad.pixels[10] = 0x2244AA # next
pad.pixels[11] = 0x884400 # quieter

group = displayio.Group()
lines = []
for y in (0, 11, 22, 33, 44, 55):
    line = label.Label(terminalio.FONT, text="", color=0xFFFFFF, x=0, y=y + 4)
    group.append(line)
    lines.append(line)
pad.display.root_group = group

serial = usb_cdc.data
buffer = bytearray()
state = None
encoder = pad.encoder
last_draw = 0
held_keys = set()
last_play_color = None
show_address = False


def send(command):
    if serial:
        serial.write((command + "\n").encode())


def clip(value, width=21):
    value = str(value)
    return value[:width]


def clock(seconds):
    seconds = max(0, int(seconds))
    return "%d:%02d" % (seconds // 60, seconds % 60)


def update_play_light():
    global last_play_color
    if state and state.get("state") == "playing":
        phase = (time.monotonic() % 1.8) / 1.8
        strength = 0.25 + 0.75 * (1 - abs(2 * phase - 1))
        color = (int(0xAA * strength) << 8) | int(0x44 * strength)
    else:
        color = 0x00AA44
    if color != last_play_color:
        pad.pixels[0] = color
        last_play_color = color


def draw():
    if show_address:
        url = state.get("web_url", "") if state else ""
        if url and "://" in url and ":" in url.split("://", 1)[1]:
            scheme, address = url.split("://", 1)
            host, port = address.rsplit(":", 1)
            values = ["WEB ADDRESS", scheme + "://", clip(host), clip(":" + port),
                      "", "Release 7: back"]
        else:
            values = ["WEB ADDRESS", "Waiting for Pi...", "", "", "", "Release 7: back"]
    elif state is None:
        values = ["PIPOD AUDIO", "Waiting for Pi...", "", "", "", ""]
    elif state.get("type") == "browse":
        values = ["BROWSE", clip(state.get("path", "/")),
                  ("> " if state.get("directory") else "♫ ") + clip(state.get("name", ""), 19),
                  "%s / %s" % (state.get("index", 0), state.get("count", 0)),
                  "Knob: browse/enter", "5: back  6: album"]
    else:
        values = ["NOW PLAYING", clip(state.get("title", "")),
                  clip(state.get("state", "")),
                  "%s / %s" % (clock(state.get("elapsed", 0)), clock(state.get("length", 0))),
                  "Vol: %s" % state.get("volume", 0), "4: folders 9/12:vol"]
    if not show_address and state and state.get("error"):
        values[5] = clip(state["error"])
    for line, value in zip(lines, values):
        line.text = clip(value)


send("hello")
draw()
while True:
    event = pad.keys.events.get()
    if event:
        key = event.key_number
        if event.pressed and key not in held_keys:
            held_keys.add(key)
            if key == 6:
                show_address = True
                draw()
            commands = {0: "play", 1: "pause", 2: "stop", 3: "now",
                        4: "back", 5: "play_folder", 7: "select",
                        8: "volup", 9: "prev", 10: "next", 11: "voldown"}
            if key in commands:
                send(commands[key])
        elif event.released:
            held_keys.discard(key)
            if key == 6:
                show_address = False
                draw()
    position = pad.encoder
    if position != encoder:
        direction = "down" if position > encoder else "up"
        for _ in range(min(abs(position - encoder), 8)):
            send(direction)
        encoder = position
    pad.encoder_switch_debounced.update()
    if pad.encoder_switch_debounced.pressed:
        send("select")
    if serial and serial.in_waiting:
        chunk = serial.read(min(serial.in_waiting, 128))
        for byte in chunk:
            if byte == 10:
                try:
                    state = json.loads(buffer.decode("utf-8"))
                    draw()
                except (ValueError, UnicodeError):
                    pass
                buffer = bytearray()
            elif len(buffer) < 512:
                buffer.append(byte)
            else:
                buffer = bytearray()
    update_play_light()
    time.sleep(0.01)
