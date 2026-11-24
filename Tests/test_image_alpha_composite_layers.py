from __future__ import annotations

import pytest

from PIL import Image, ImageDraw

from .helper import assert_image_equal

CANVAS_SIZE = (9, 8)


def _pattern_image(size: tuple[int, int], seed: int) -> Image.Image:
    width, height = size
    pixels = [
        (
            (37 * x + 17 * y + seed) % 256,
            (91 * x + 13 * y + 2 * seed) % 256,
            (13 * x + 53 * y + 3 * seed) % 256,
            40 + 25 * ((x + 2 * y + seed) % 8),
        )
        for y in range(height)
        for x in range(width)
    ]
    image = Image.new("RGBA", size)
    image.putdata(pixels)
    return image


def _tile_edges(start: int, stop: int) -> list[int]:
    """Edges for small, variably sized, non-overlapping tiles."""
    edges = [start]
    current = start
    for step in (1, 2, 2, 1):
        current = min(stop, current + step)
        edges.append(current)
    edges.append(stop)
    return sorted(set(edges))


def _submit_nonoverlapping_batches(
    canvas: Image.Image,
    layer: Image.Image,
    dest: tuple[int, int],
    source_box: tuple[int, int, int, int],
    *,
    crop_tiles: bool = False,
) -> None:
    left, top, right, bottom = source_box
    x_edges = _tile_edges(left, right)
    y_edges = _tile_edges(top, bottom)

    for y0, y1 in zip(y_edges, y_edges[1:]):
        for x0, x1 in zip(x_edges, x_edges[1:]):
            tile_dest = (dest[0] + x0 - left, dest[1] + y0 - top)
            if crop_tiles:
                tile = layer.crop((x0, y0, x1, y1))
                canvas.alpha_composite(tile, tile_dest)
            else:
                canvas.alpha_composite(layer, tile_dest, (x0, y0, x1, y1))


def _visible_intersection(
    canvas_size: tuple[int, int],
    dest: tuple[int, int],
    source_box: tuple[int, int, int, int],
) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]] | None:
    left, top, right, bottom = source_box
    dest_x, dest_y = dest
    overlay_width = right - left
    overlay_height = bottom - top

    dest_box = (
        max(0, dest_x),
        max(0, dest_y),
        min(canvas_size[0], dest_x + overlay_width),
        min(canvas_size[1], dest_y + overlay_height),
    )
    if dest_box[0] >= dest_box[2] or dest_box[1] >= dest_box[3]:
        return None

    source_patch_box = (
        left + dest_box[0] - dest_x,
        top + dest_box[1] - dest_y,
        left + dest_box[2] - dest_x,
        top + dest_box[3] - dest_y,
    )
    return dest_box, source_patch_box


@pytest.mark.parametrize(
    "layer_size, source_box, dest",
    [
        ((7, 6), (1, 1, 6, 5), (2, 2)),
        ((7, 6), (1, 1, 6, 5), (-2, -1)),
        ((7, 6), (1, 1, 6, 5), (6, 5)),
        ((8, 5), (2, 0, 7, 4), (-3, 5)),
        ((5, 7), (0, 2, 4, 6), (7, -2)),
        ((12, 9), (3, 1, 10, 8), (1, 0)),
        ((7, 6), (1, 1, 6, 5), (-10, -10)),
        ((4, 3), (0, 0, 4, 3), (12, 10)),
    ],
)
def test_nonoverlapping_batches_match_whole_submission(
    layer_size: tuple[int, int],
    source_box: tuple[int, int, int, int],
    dest: tuple[int, int],
) -> None:
    original = _pattern_image(CANVAS_SIZE, seed=11)
    layer = _pattern_image(layer_size, seed=29)

    whole = original.copy()
    whole.alpha_composite(layer, dest, source_box)

    batched = original.copy()
    _submit_nonoverlapping_batches(batched, layer, dest, source_box)

    cropped_batches = original.copy()
    _submit_nonoverlapping_batches(
        cropped_batches, layer, dest, source_box, crop_tiles=True
    )

    assert_image_equal(batched, whole)
    assert_image_equal(cropped_batches, whole)
    assert batched.size == CANVAS_SIZE
    assert batched.mode == "RGBA"

    intersection = _visible_intersection(CANVAS_SIZE, dest, source_box)
    if intersection is None:
        assert_image_equal(batched, original)
        return

    dest_box, source_patch_box = intersection
    expected_visible = Image.alpha_composite(
        original.crop(dest_box), layer.crop(source_patch_box)
    )
    assert_image_equal(batched.crop(dest_box), expected_visible)

    original_pixels = original.load()
    batched_pixels = batched.load()
    x0, y0, x1, y1 = dest_box
    for y in range(CANVAS_SIZE[1]):
        for x in range(CANVAS_SIZE[0]):
            if not (x0 <= x < x1 and y0 <= y < y1):
                assert batched_pixels[x, y] == original_pixels[x, y]


def test_source_origin_two_tuple_matches_source_rectangle() -> None:
    layer = _pattern_image((7, 6), seed=3)
    source_origin = (2, 1)
    dest = (4, 3)

    from_origin = _pattern_image(CANVAS_SIZE, seed=17)
    from_origin.alpha_composite(layer, dest, source_origin)

    from_rectangle = _pattern_image(CANVAS_SIZE, seed=17)
    source_box = (*source_origin, layer.width, layer.height)
    from_rectangle.alpha_composite(layer, dest, source_box)

    assert_image_equal(from_origin, from_rectangle)


def test_empty_source_rectangle_is_a_noop() -> None:
    original = _pattern_image(CANVAS_SIZE, seed=5)
    layer = _pattern_image((7, 6), seed=7)
    canvas = original.copy()

    canvas.alpha_composite(layer, (2, 1), (4, 3, 4, 3))

    assert_image_equal(canvas, original)


def test_translucent_layers_accumulate_actual_color_and_alpha() -> None:
    size = (5, 5)
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))

    red = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(red).rectangle((0, 0, 3, 3), fill=(255, 0, 0, 128))

    green = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(green).rectangle((1, 1, 4, 4), fill=(0, 255, 0, 128))

    blue = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(blue).rectangle((2, 0, 4, 2), fill=(0, 0, 255, 128))

    canvas.alpha_composite(red)
    assert canvas.getpixel((0, 0)) == (255, 0, 0, 128)

    canvas.alpha_composite(green)
    assert canvas.getpixel((1, 1)) == (85, 170, 0, 192)
    assert canvas.getpixel((3, 3)) == (85, 170, 0, 192)

    canvas.alpha_composite(blue)

    # One-layer contributions remain pure source over the transparent base.
    assert canvas.getpixel((0, 0)) == (255, 0, 0, 128)
    assert canvas.getpixel((4, 4)) == (0, 255, 0, 128)
    assert canvas.getpixel((4, 0)) == (0, 0, 255, 128)

    # Two- and three-layer overlaps have the color and coverage produced by
    # compositing each translucent layer in submission order.
    assert canvas.getpixel((1, 1)) == (85, 170, 0, 192)
    assert canvas.getpixel((3, 3)) == (85, 170, 0, 192)
    assert canvas.getpixel((2, 2)) == (36, 73, 146, 224)
    assert canvas.getpixel((3, 2)) == (36, 73, 146, 224)

    # The base pixel outside every layer is untouched.
    assert canvas.getpixel((0, 4)) == (0, 0, 0, 0)
    assert canvas.size == size
    assert canvas.mode == "RGBA"


def test_cropping_preserves_visible_result_and_input_layer() -> None:
    original = _pattern_image(CANVAS_SIZE, seed=41)
    layer = _pattern_image((7, 6), seed=53)
    layer_bytes = layer.tobytes()
    source_box = (1, 1, 6, 5)
    dest = (6, 5)

    with_source_box = original.copy()
    with_source_box.alpha_composite(layer, dest, source_box)

    cropped_layer = layer.crop(source_box)
    with_physical_crop = original.copy()
    with_physical_crop.alpha_composite(cropped_layer, dest)

    assert_image_equal(with_source_box, with_physical_crop)

    intersection = _visible_intersection(CANVAS_SIZE, dest, source_box)
    assert intersection is not None
    dest_box, _ = intersection
    assert_image_equal(
        with_source_box.crop(dest_box), with_physical_crop.crop(dest_box)
    )

    # Cropping/compositing reads the input layer but does not rewrite it.
    assert layer.tobytes() == layer_bytes
    assert layer.size == (7, 6)
    assert layer.mode == "RGBA"


@pytest.mark.parametrize(
    "dest, source, message",
    [
        pytest.param(
            "invalid destination",
            (0, 0),
            "Destination must be a list or tuple",
            id="dest-string",
        ),
        pytest.param(
            0,
            (0, 0),
            "Destination must be a list or tuple",
            id="dest-integer",
        ),
        pytest.param(
            (0,),
            (0, 0),
            "Destination must be a sequence of length 2",
            id="dest-too-short",
        ),
        pytest.param(
            (0, 0, 0),
            (0, 0),
            "Destination must be a sequence of length 2",
            id="dest-too-long",
        ),
        pytest.param(
            (0, 0),
            "invalid source",
            "Source must be a list or tuple",
            id="source-string",
        ),
        pytest.param(
            (0, 0),
            0,
            "Source must be a list or tuple",
            id="source-integer",
        ),
        pytest.param(
            (0, 0),
            (0,),
            "Source must be a sequence of length 2 or 4",
            id="source-too-short",
        ),
        pytest.param(
            (0, 0),
            (0, 0, 1),
            "Source must be a sequence of length 2 or 4",
            id="source-three-items",
        ),
        pytest.param(
            (0, 0),
            (0, 0, 1, 2, 3),
            "Source must be a sequence of length 2 or 4",
            id="source-too-long",
        ),
        pytest.param(
            (0, 0),
            (0, -1),
            "Source must be non-negative",
            id="source-negative-origin",
        ),
        pytest.param(
            (0, 0),
            (-1, 0, 2, 2),
            "Source must be non-negative",
            id="source-negative-rectangle",
        ),
        pytest.param(
            (0, 0),
            (2, 2, 1, 3),
            "less than",
            id="source-inverted-horizontal",
        ),
        pytest.param(
            (0, 0),
            (0, 2, 2, 1),
            "less than",
            id="source-inverted-vertical",
        ),
    ],
)
def test_invalid_parameters_leave_existing_picture_unchanged(
    dest: object, source: object, message: str
) -> None:
    canvas = _pattern_image(CANVAS_SIZE, seed=67)
    canvas.alpha_composite(Image.new("RGBA", (4, 3), (255, 128, 0, 160)), (1, 1))
    expected_bytes = canvas.tobytes()

    layer = _pattern_image((7, 6), seed=71)
    with pytest.raises(ValueError, match=message):
        canvas.alpha_composite(
            layer,
            dest,  # type: ignore[arg-type]
            source,  # type: ignore[arg-type]
        )

    assert canvas.tobytes() == expected_bytes
    assert canvas.size == CANVAS_SIZE
    assert canvas.mode == "RGBA"


def test_invalid_layer_mode_leaves_existing_picture_unchanged() -> None:
    canvas = _pattern_image(CANVAS_SIZE, seed=73)
    expected_bytes = canvas.tobytes()
    layer = Image.new("RGB", (5, 4), (255, 0, 0))

    with pytest.raises(ValueError, match="images do not match"):
        canvas.alpha_composite(layer)

    assert canvas.tobytes() == expected_bytes
    assert canvas.size == CANVAS_SIZE
    assert canvas.mode == "RGBA"


def test_functional_alpha_composite_rejects_mismatched_images() -> None:
    background = _pattern_image(CANVAS_SIZE, seed=79)
    foreground = _pattern_image((4, 4), seed=83)
    background_bytes = background.tobytes()
    foreground_bytes = foreground.tobytes()

    with pytest.raises(ValueError, match="images do not match"):
        Image.alpha_composite(background, foreground)

    assert background.tobytes() == background_bytes
    assert foreground.tobytes() == foreground_bytes
