"""Second-round probes: explicit overrides, malformed counts, chunk layout."""
from __future__ import annotations

import struct
import warnings
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageSequence, PngImagePlugin

REPO = Path("/srv/fb2/feng0732/0130-pillow-2-f431261a")
APNG = REPO / "Tests/images/apng"


def chunks(data: bytes):
    pos = 8
    out = []
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        cid = data[pos + 4:pos + 8]
        out.append((cid.decode("ascii"), pos, length))
        pos += 12 + length
    return out


def banner(t):
    print(f"\n===== {t} =====")


# A. explicit default_image=False overrides inherited info flag
banner("explicit default_image=False override")
with Image.open(APNG / "single_frame_default.png") as im:
    all_frames = [f.copy() for f in ImageSequence.Iterator(im)]
print("copied frame0 info has default_image =", all_frames[0].info.get("default_image"))
buf = BytesIO()
all_frames[0].save(buf, "PNG", save_all=True, default_image=False,
                   append_images=all_frames[1:])
raw = buf.getvalue()
print("chunk order:", [c[0] for c in chunks(raw)])
buf.seek(0)
with Image.open(buf) as im2:
    print("n_frames =", im2.n_frames, "| default_image =", im2.info.get("default_image"))
    for i in range(im2.n_frames):
        im2.seek(i)
        print(f"  frame {i}: duration={im2.info.get('duration')} pixel(0,0)={im2.getpixel((0, 0))}")

# B. chunk layout when default image exists vs not
banner("chunk layout: default_image=True")
red = Image.new("RGBA", (4, 4), (255, 0, 0, 255))
green = Image.new("RGBA", (4, 4), (0, 255, 0, 255))
b1 = BytesIO()
red.save(b1, "PNG", save_all=True, default_image=True, append_images=[green],
         duration=[100])
print([c[0] for c in chunks(b1.getvalue())])
banner("chunk layout: no default image")
b2 = BytesIO()
red.save(b2, "PNG", save_all=True, append_images=[green], duration=[100, 200])
print([c[0] for c in chunks(b2.getvalue())])

# C. acTL declared count vs actual frames
banner("acTL count mismatch on full iteration")
for name in ("syntax_num_frames_low.png", "syntax_num_frames_high.png"):
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        with Image.open(APNG / name) as im:
            print(name, "declared n_frames =", im.n_frames)
            try:
                seen = []
                for f in ImageSequence.Iterator(im):
                    seen.append(f.tell())
                print("  iterated frames:", seen)
            except (EOFError, OSError, SyntaxError) as e:
                print("  iteration failed:", type(e).__name__, e)
        if w:
            print("  warnings:", [str(x.message) for x in w])

# D. acTL after IDAT -> treated as static PNG
banner("acTL after IDAT (layout dependency)")
with Image.open(APNG / "chunk_actl_after_idat.png") as im:
    print("n_frames =", im.n_frames, "| is_animated =", im.is_animated,
          "| mimetype =", im.get_format_mimetype())
with Image.open(APNG / "chunk_no_actl.png") as im:
    print("no_actl: n_frames =", im.n_frames, "| is_animated =", im.is_animated)

# E. duration list length mismatch behavior
banner("duration list too short / too long")
a = Image.new("L", (2, 2))
c = Image.new("L", (2, 2), 1)
try:
    bb = BytesIO()
    a.save(bb, "PNG", save_all=True, append_images=[c], duration=[100])  # need 2
    bb.seek(0)
    with Image.open(bb) as im:
        im.seek(1)
        print("short list: frame0..1 durations ->", end=" ")
        im.seek(0)
        print(im.info.get("duration"), end=" / ")
        im.seek(1)
        print(im.info.get("duration"))
except Exception as e:
    print("short list raised:", type(e).__name__, e)
try:
    bb = BytesIO()
    a.save(bb, "PNG", save_all=True, append_images=[c], duration=[100, 200, 300])
    print("long list: saved without error")
except Exception as e:
    print("long list raised:", type(e).__name__, e)

# F. bbox delta optimization: partially changed frame gets sub-rect fcTL
banner("sub-region delta frame fcTL geometry")
base = Image.new("RGBA", (10, 10), (255, 0, 0, 255))
changed = base.copy()
for x in range(2, 6):
    changed.putpixel((x, 3), (0, 255, 0, 255))
bb = BytesIO()
base.save(bb, "PNG", save_all=True, append_images=[changed], duration=[100, 100])
bb.seek(0)
with Image.open(bb) as im:
    print("n_frames:", im.n_frames)
    im.seek(1)
    print("frame1 bbox as read:", im.info.get("bbox"))
    # composited output must still equal full changed frame
    print("pixel equal to source:",
          all(im.getpixel((x, 3)) == changed.getpixel((x, 3)) for x in range(10)))

# G. different_durations fixture: default image present?
banner("different_durations.png layout")
with Image.open(APNG / "different_durations.png") as im:
    print("n_frames =", im.n_frames, "| default_image =", im.info.get("default_image"),
          "| open duration =", im.info.get("duration"))

# H. single-frame save_all collapses to static PNG (acTL dropped)
banner("single animation frame collapses to static PNG")
solo = Image.new("L", (2, 2), 5)
bb = BytesIO()
solo.save(bb, "PNG", save_all=True, duration=100)
print("chunks:", [c[0] for c in chunks(bb.getvalue())])
bb.seek(0)
with Image.open(bb) as im:
    print("n_frames =", im.n_frames, "| mimetype =", im.get_format_mimetype(),
          "| duration in info =", im.info.get("duration"))

# but default_image=True keeps acTL even with one anim frame
bb = BytesIO()
solo.save(bb, "PNG", save_all=True, default_image=True, append_images=[solo.copy()],
          duration=[100])
print("with default image chunks:", [c[0] for c in chunks(bb.getvalue())])
bb.seek(0)
with Image.open(bb) as im:
    print("n_frames =", im.n_frames, "| mimetype =", im.get_format_mimetype())
