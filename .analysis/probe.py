"""Empirical probes for APNG default-image / frame-index / duration semantics."""
from __future__ import annotations

import warnings
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageSequence, PngImagePlugin

REPO = Path("/srv/fb2/feng0732/0130-pillow-2-f431261a")
APNG = REPO / "Tests/images/apng"


def banner(title: str) -> None:
    print(f"\n===== {title} =====")


def dump(path: str | BytesIO, label: str) -> None:
    banner(label)
    with Image.open(path) as im:
        print("n_frames =", im.n_frames, "| is_animated =", im.is_animated,
              "| mimetype =", im.get_format_mimetype())
        print("open-time info keys:", sorted(k for k in im.info
              if k in ("default_image", "duration", "loop", "disposal", "blend")))
        print("open-time info:", {k: im.info.get(k) for k in
              ("default_image", "duration", "loop", "disposal", "blend")})
        for i in range(im.n_frames):
            im.seek(i)
            print(f"  seek({i}): tell={im.tell()} duration={im.info.get('duration')!r}",
                  f"disposal={im.info.get('disposal')!r} blend={im.info.get('blend')!r}",
                  f"bbox={im.info.get('bbox')!r} default_image={im.info.get('default_image')!r}")
        total = 0.0
        im.seek(0)
        for f in ImageSequence.Iterator(im):
            total += f.info.get("duration", 0) or 0
        print("sum of info['duration'] over all public frames (ms):", total)


# 1. Two fixtures: no default image vs. with default image
dump(APNG / "single_frame.png", "single_frame.png (acTL num_frames=1, no default)")
dump(APNG / "single_frame_default.png", "single_frame_default.png (default + 1 anim frame)")
dump(APNG / "delay.png", "delay.png (durations fixture)")

# 2. Arbitrary seek: backwards seek rewinds and replays; O(n) re-decode
banner("arbitrary seek behavior")
with Image.open(APNG / "single_frame_default.png") as im:
    im.seek(1)
    px1 = im.getpixel((0, 0))
    im.seek(1)  # same frame no-op
    print("seek(1) after seek(1) no-op, tell =", im.tell())
    im.seek(0)
    print("after seek(0): pixel(0,0) =", im.getpixel((0, 0)), "(red default image)")
    im.seek(1)
    print("after seek(1) again: pixel(0,0) =", im.getpixel((0, 0)), "(green anim frame)",
          "matches earlier:", im.getpixel((0, 0)) == px1)
    im.seek(0)
    try:
        im._seek(2)  # gap: __frame=0, cannot jump straight to 2
    except ValueError as e:
        print("direct _seek(2) after seek(0) raises ValueError:", e)
    im.seek(1)
    try:
        im._seek(2)  # sequential but past EOF
    except (ValueError, OSError, EOFError) as e:
        print("direct _seek(2) at last frame raises:", type(e).__name__, e)
    try:
        im.seek(im.n_frames)
    except EOFError as e:
        print("seek(n_frames) raises EOFError:", e)

# 3. Metadata-only open does not validate frames; n_frames comes from acTL
banner("metadata read without loading")
with Image.open(APNG / "syntax_num_frames_low.png") as im:
    print("num_frames_low: n_frames =", im.n_frames, "(declared in acTL; actual fcTL count differs)")
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    with Image.open(APNG / "syntax_num_frames_high.png") as im:
        print("num_frames_high: n_frames =", im.n_frames,
              "| is_animated =", im.is_animated)
    print("warnings:", [str(x.message) for x in w])

# 4. Save round trip: default image preservation
banner("round trip with default_image=True")
with Image.open(APNG / "single_frame_default.png") as im:
    frames = [f.copy() for f in ImageSequence.Iterator(im)]
buf = BytesIO()
frames[0].save(buf, "PNG", save_all=True, default_image=True, append_images=frames[1:])
buf.seek(0)
dump(buf, "re-saved single_frame_default (default_image=True)")

# 5. Save WITHOUT default_image flag -> index shift: old frame 0 becomes anim frame 0
banner("round trip dropping default_image (index shift)")
buf2 = BytesIO()
with Image.open(APNG / "single_frame_default.png") as im:
    all_frames = [f.copy() for f in ImageSequence.Iterator(im)]
all_frames[0].save(buf2, "PNG", save_all=True, append_images=all_frames[1:])
buf2.seek(0)
dump(buf2, "re-saved without default_image flag")

# 6. duration list indexing excludes the default image
banner("duration list indexing vs public frame index")
red = Image.new("RGBA", (4, 4), (255, 0, 0, 255))
g1 = Image.new("RGBA", (4, 4), (0, 255, 0, 255))
g2 = Image.new("RGBA", (4, 4), (0, 0, 255, 255))
buf3 = BytesIO()
red.save(buf3, "PNG", save_all=True, default_image=True,
         append_images=[g1, g2], duration=[100, 200])
buf3.seek(0)
dump(buf3, "3 public frames (default + 2 anim), duration list len 2 = [100,200]")

# 7. Identical-frame merging shifts indices and accumulates duration
banner("identical consecutive frame merge")
base = Image.new("RGBA", (4, 4), (255, 0, 0, 255))
other = Image.new("RGBA", (4, 4), (0, 255, 0, 255))
buf4 = BytesIO()
base.save(buf4, "PNG", save_all=True,
          append_images=[base.copy(), other], duration=[500, 100, 150])
buf4.seek(0)
with Image.open(buf4) as im:
    print("n_frames after merge:", im.n_frames)
    for i in range(im.n_frames):
        im.seek(i)
        print(f"  frame {i}: duration={im.info.get('duration')}")

# 8. Fractional duration round trip (Fraction limit_denominator)
banner("fractional / awkward duration round trip")
for d in (0.5, 1000 / 3, 33.3, 0.1, 7.0):
    b = BytesIO()
    a = Image.new("L", (2, 2))
    c = Image.new("L", (2, 2), 1)
    a.save(b, "PNG", save_all=True, append_images=[c], duration=d)
    b.seek(0)
    with Image.open(b) as im:
        im.seek(1)
        print(f"  requested={d!r:>10} -> frame1 duration read back={im.info['duration']!r}")

# 9. zero duration handling: Pillow keeps 0; spec says treat as 100ms
banner("zero delay numerator fixture")
dump(APNG / "delay_zero_numer.png", "delay_zero_numer.png")

# 10. loop round trip
banner("loop/num_plays")
with Image.open(APNG / "num_plays.png") as im:
    print("num_plays.png loop =", im.info.get("loop"))
with Image.open(APNG / "num_plays_1.png") as im:
    print("num_plays_1.png loop =", im.info.get("loop"))

# 11. disposal/blend are NOT carried automatically on plain re-save
banner("disposal/blend round trip without explicit params")
with Image.open(APNG / "dispose_op_background.png") as im:
    print("source frame infos:")
    for i in range(im.n_frames):
        im.seek(i)
        print(f"  frame {i}: disposal={im.info.get('disposal')} blend={im.info.get('blend')}")
    saved = [f.copy() for f in ImageSequence.Iterator(im)]
b5 = BytesIO()
saved[0].save(b5, "PNG", save_all=True, append_images=saved[1:], duration=[100] * len(saved))
b5.seek(0)
with Image.open(b5) as im:
    print("re-saved (defaults) frame infos:")
    for i in range(im.n_frames):
        im.seek(i)
        print(f"  frame {i}: disposal={im.info.get('disposal')} blend={im.info.get('blend')}")

# 12. text property side effect: seeks to final frame
banner("im.text side effect on frame position")
with Image.open(APNG / "delay.png") as im:
    print("tell before text:", im.tell())
    _ = im.text
    print("tell after im.text:", im.tell(), "(moved to last frame internally, then restored)")
    print("tell now:", im.tell())
