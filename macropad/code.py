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

browse_group = displayio.Group()
browse_lines = []
for y in (0, 11, 22, 33, 44, 55):
    line = label.Label(terminalio.FONT, text="", color=0xFFFFFF, x=0, y=y + 4)
    browse_group.append(line)
    browse_lines.append(line)

now_group = displayio.Group()
now_heading = label.Label(terminalio.FONT, text="NOW PLAYING", color=0xFFFFFF, x=0, y=4)
now_footer = label.Label(terminalio.FONT, text="", color=0xFFFFFF, x=0, y=59)
now_group.append(now_heading)
title_bitmap = displayio.Bitmap(128, 32, 2)
title_palette = displayio.Palette(2)
title_palette[0] = 0x000000
title_palette[1] = 0xFFFFFF
now_group.append(displayio.TileGrid(title_bitmap, pixel_shader=title_palette, x=0, y=22))
now_group.append(now_footer)
icon_bitmap = displayio.Bitmap(24, 21, 2)
icon_palette = displayio.Palette(2)
icon_palette[0] = 0x000000
icon_palette[1] = 0xFFFFFF
now_group.append(displayio.TileGrid(icon_bitmap, pixel_shader=icon_palette, x=104, y=0))

pad.display.root_group = browse_group
active_group = browse_group

serial = usb_cdc.data
buffer = bytearray()
state = None
encoder = pad.encoder
last_draw = 0
held_keys = set()
last_play_color = None
show_address = False
shown_title = None
last_icon_state = None


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


def draw_icon(playback_state):
    global last_icon_state
    if playback_state == last_icon_state:
        return
    last_icon_state = playback_state
    icon_bitmap.fill(0)
    for x in range(24):
        icon_bitmap[x, 0] = 1
        icon_bitmap[x, 20] = 1
    for y in range(21):
        icon_bitmap[0, y] = 1
        icon_bitmap[23, y] = 1
    if playback_state == "playing":
        for y in range(4, 17):
            for x in range(6, 7 + 2 * (6 - abs(y - 10))):
                icon_bitmap[x, y] = 1
    elif playback_state == "paused":
        for y in range(4, 17):
            for x in (7, 8, 9, 14, 15, 16):
                icon_bitmap[x, y] = 1
    elif playback_state == "stopped":
        for y in range(5, 16):
            for x in range(7, 17):
                icon_bitmap[x, y] = 1


def draw_title_glyph(character, left, top):
    """Draw terminalio's 6×8 glyph at 9×12, halfway between its 1× and 2× sizes."""
    glyph = terminalio.FONT.get_glyph(ord(character))
    if glyph is None:
        glyph = terminalio.FONT.get_glyph(ord("?"))
    if glyph is None:
        return
    font_width, font_height = terminalio.FONT.get_bounding_box()
    columns = glyph.bitmap.width // font_width
    source_x = (glyph.tile_index % columns) * font_width
    source_y = (glyph.tile_index // columns) * font_height
    for y in range(12):
        for x in range(9):
            if glyph.bitmap[source_x + x * font_width // 9,
                            source_y + y * font_height // 12]:
                title_bitmap[left + x, top + y] = 1


def title_lines(text):
    lines = ["", ""]
    row = 0
    for word in text.split():
        candidate = (lines[row] + " " + word) if lines[row] else word
        if len(candidate) <= 14:
            lines[row] = candidate
        elif row == 0 and lines[0]:
            row = 1
            if len(word) <= 14:
                lines[row] = word
            else:
                lines[row] = "..."
                break
        elif row == 1:
            while lines[1] and len(lines[1]) + 3 > 14:
                lines[1] = lines[1].rsplit(" ", 1)[0] if " " in lines[1] else ""
            lines[1] = (lines[1] + "...") if lines[1] else "..."
            break
        else:
            # An individual word wider than the display has no word boundary.
            lines[0] = word[:11] + "..."
            break
    return lines


def draw_title(text):
    title_bitmap.fill(0)
    for row, part in enumerate(title_lines(text)):
        for position, character in enumerate(part):
            draw_title_glyph(character, position * 9, row * 16)


def draw_now():
    global shown_title
    title = " ".join(str(state.get("title", "")).split())
    if title != shown_title:
        shown_title = title
        draw_title(title)
    footer = clip("%s/%s V:%s" % (
        clock(state.get("elapsed", 0)), clock(state.get("length", 0)),
        state.get("volume", 0)))
    if state.get("error"):
        footer = clip(state["error"])
    if now_footer.text != footer:
        now_footer.text = footer
    draw_icon(state.get("state", ""))


def draw():
    global active_group
    if not show_address and state and state.get("type") == "now":
        if active_group is not now_group:
            pad.display.root_group = now_group
            active_group = now_group
        draw_now()
        return
    if active_group is not browse_group:
        pad.display.root_group = browse_group
        active_group = browse_group
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
    if not show_address and state and state.get("error"):
        values[5] = clip(state["error"])
    for line, value in zip(browse_lines, values):
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
