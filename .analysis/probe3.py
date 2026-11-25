"""Third-round probes: malformed-count fixtures, info aliasing, delay fractions."""
from __future__ import annotations

import struct
import warnings
from io import BytesIO
from pathlib import Path

from PIL import Image, PngImagePlugin

REPO = Path("/srv/fb2/feng0732/0130-pillow-2-f431261a")
APNG = REPO / "Tests/images/apng"


def parse(path):
    data = Path(path).read_bytes()
    pos, out = 8, []
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        cid = data[pos + 4:pos + 8].decode("ascii")
        body = data[pos + 8:pos + 8 + length]
        extra = ""
        if cid == "acTL":
            extra = f" num_frames={struct.unpack('>I', body[:4])[0]} num_plays={struct.unpack('>I', body[4:8])[0]}"
        if cid == "fcTL":
            extra = (f" seq={struct.unpack('>I', body[:4])[0]}"
                     f" delay={struct.unpack('>H', body[20:22])[0]}/"
                     f"{struct.unpack('>H', body[22:24])[0]}")
        out.append(cid + extra)
        pos += 12 + length
    return out


for name in ("syntax_num_frames_low.png", "syntax_num_frames_high.png",
             "syntax_num_frames_zero.png", "syntax_num_frames_zero_default.png",
             "syntax_num_frames_invalid.png", "single_frame.png",
             "single_frame_default.png", "delay_round.png",
             "delay_short_max.png", "delay_zero_denom.png"):
    print(name)
    print("  ", parse(APNG / name))

banner = lambda t: print(f"\n===== {t} =====")

# low count: extra animation data exists beyond public n_frames
banner("num_frames_low: hidden extra fcTL unreachable")
with Image.open(APNG / "syntax_num_frames_low.png") as im:
    print("public n_frames =", im.n_frames)
    try:
        im.seek(2)
    except EOFError as e:
        print("seek(2) blocked:", e)

banner("num_frames_zero with default: static fallback still renders")
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    with Image.open(APNG / "syntax_num_frames_zero_default.png") as im:
        print("n_frames =", im.n_frames, "is_animated =", im.is_animated,
              "default_image flag =", im.info.get("default_image"))
        im.load()
        print("loaded pixel(0,0) =", im.getpixel((0, 0)))
    print("warning:", [str(x.message) for x in w])

# info dict aliasing across seeks
banner("im.info object identity across seeks")
with Image.open(APNG / "delay.png") as im:
    ref = im.info
    im.seek(1)
    d1 = ref["duration"]
    im.seek(2)
    d2 = ref["duration"]
    print("same dict object:", ref is im.info, "| values seen via old ref:", d1, "->", d2)

# 16-bit denominator limits: very long fractional delay
banner("delay fraction limits (Fraction limit_denominator 65535)")
for d in (33.333333333, 100.0 / 7, 0.001, 65535000, 65535000 + 1):
    a = Image.new("L", (2, 2))
    c = Image.new("L", (2, 2), 1)
    bb = BytesIO()
    try:
        a.save(bb, "PNG", save_all=True, append_images=[c], duration=d)
        bb.seek(0)
        with Image.open(bb) as im:
            im.seek(1)
            print(f"  requested {d!r:>18} -> {im.info['duration']!r}")
    except ValueError as e:
        print(f"  requested {d!r:>18} -> ValueError: {e}")

# loop default and the meaning of total timeline
banner("loop defaults on save")
a = Image.new("L", (2, 2))
c = Image.new("L", (2, 2), 1)
bb = BytesIO()
a.save(bb, "PNG", save_all=True, append_images=[c], duration=[100, 200])
bb.seek(0)
with Image.open(bb) as im:
    print("default loop =", im.info.get("loop"), "(0 = infinite)")
    durations = []
    for f in __import__("PIL.ImageSequence", fromlist=["Iterator"]).Iterator(im):
        durations.append(f.info.get("duration"))
    print("per-frame durations:", durations, "-> one-pass total =", sum(durations), "ms")
