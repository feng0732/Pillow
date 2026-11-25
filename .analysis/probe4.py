"""Fourth-round probes: save-time default flag resolution and timeline math."""
from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageSequence, PngImagePlugin

banner = lambda t: print(f"\n===== {t} =====")

# 1. round trip delay.png using naive per-frame duration list INCLUDING default slot
banner("naive round trip: duration list aligned to PUBLIC index (off-by-one)")
with Image.open("Tests/images/apng/delay.png") as im:
    frames = [f.copy() for f in ImageSequence.Iterator(im)]
    public_durations = [f.info.get("duration", 0) for f in frames]
    print("public-index durations (default slot is 0):", public_durations)

# correct approach (mirrors test_apng_save_duration_loop): skip default slot
correct = [d for i, d in enumerate(public_durations) if i != 0]
b_ok = BytesIO()
frames[0].save(b_ok, "PNG", save_all=True, default_image=True,
               append_images=frames[1:], duration=correct)
b_ok.seek(0)
with Image.open(b_ok) as im:
    got = []
    for f in ImageSequence.Iterator(im):
        got.append(f.info.get("duration"))
    print("correct list -> read back:", got)

# naive approach: feed all 5 values while only 4 animation frames exist
b_bad = BytesIO()
try:
    frames[0].save(b_bad, "PNG", save_all=True, default_image=True,
                   append_images=frames[1:], duration=public_durations)
    b_bad.seek(0)
    with Image.open(b_bad) as im:
        got = [f.info.get("duration") for f in ImageSequence.Iterator(im)]
        print("naive list accepted -> read back:", got)
except Exception as e:
    print("naive list raised:", type(e).__name__, e)

# 2. even worse: public index 2's duration applied to animation index 1
banner("index correspondence table for delay.png style file")
with Image.open("Tests/images/apng/delay.png") as im:
    for i in range(im.n_frames):
        im.seek(i)
        anim_idx = None if im.info.get("default_image") and i == 0 else i - 1
        print(f"  public frame {i}: fcTL animation index = {anim_idx}, "
              f"duration = {im.info.get('duration')} ms")

# 3. no-default file: public index == animation index, no shift
banner("no-default file correspondence")
with Image.open("Tests/images/apng/different_durations.png") as im:
    for i in range(im.n_frames):
        im.seek(i)
        print(f"  public frame {i}: duration = {im.info.get('duration')} ms, "
              f"default_image = {im.info.get('default_image')}")

# 4. total duration of delay.png from file (default contributes nothing)
banner("timeline totals")
with Image.open("Tests/images/apng/delay.png") as im:
    anim_total = 0.0
    for i in range(im.n_frames):
        im.seek(i)
        anim_total += im.info.get("duration") or 0
    print("delay.png: 5 public frames, sum =", anim_total,
          "ms (default image contributes 0; timeline = 3000 ms)")

# 5. verify the merged-frame accumulated duration math precisely
banner("merge accumulation exactness")
base = Image.new("RGBA", (4, 4), (1, 2, 3, 255))
b = BytesIO()
base.save(b, "PNG", save_all=True,
          append_images=[base.copy(), base.copy(), Image.new("RGBA", (4, 4), (9, 9, 9, 255))],
          duration=[100, 100, 100, 250])
b.seek(0)
with Image.open(b) as im:
    print("n_frames:", im.n_frames)
    for i in range(im.n_frames):
        im.seek(i)
        print(f"  frame {i}: duration = {im.info['duration']}")
    print("total one-pass duration preserved:",
          sum((lambda: (im.seek(i), im.info["duration"])[1])() for i in range(im.n_frames)))

# 6. what happens to the default image pixel content on round trip (it is a
#    separate still, not regenerated from compositing)
banner("default image pixels preserved independently")
with Image.open("Tests/images/apng/single_frame_default.png") as im:
    frames = [f.copy() for f in ImageSequence.Iterator(im)]
b = BytesIO()
frames[0].save(b, "PNG", save_all=True, default_image=True, append_images=frames[1:])
b.seek(0)
with Image.open(b) as im:
    print("frame0 (default) pixel:", im.getpixel((0, 0)), "expected (255,0,0,255)")
    im.seek(1)
    print("frame1 (anim) pixel:  ", im.getpixel((0, 0)), "expected (0,255,0,255)")
