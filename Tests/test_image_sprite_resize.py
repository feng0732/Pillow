"""
Tests for using an image atlas to produce sprite previews at several sizes.

The sprite region is selected with a crop box, optionally shrunk in a first
(reduce) pass before the final resample, and the alpha channel is weighted so
that fully transparent pixels cannot tint the visible sprite.
"""

from __future__ import annotations

import math

import pytest

from PIL import Image, ImageChops, ImageStat

from .helper import assert_image_equal


def _as_pixel(value: float | tuple[int, ...]) -> tuple[int, ...]:
    assert isinstance(value, tuple)
    return value


def _as_int(value: float | tuple[int, ...]) -> int:
    assert isinstance(value, int)
    return value


def _max_extrema(im: Image.Image) -> int:
    extrema = im.getextrema()
    if isinstance(extrema[0], tuple):
        return max(band[1] for band in extrema)
    return int(extrema[1])


ALL_RESAMPLING = (
    Image.Resampling.NEAREST,
    Image.Resampling.BOX,
    Image.Resampling.BILINEAR,
    Image.Resampling.HAMMING,
    Image.Resampling.BICUBIC,
    Image.Resampling.LANCZOS,
    Image.Resampling.MKS2013,
    Image.Resampling.MKS2021,
)
# Only NEAREST copies source pixels; BOX has a half-pixel support and so is
# still a point-sampling filter for the purpose of which source pixels it reads.
POINT_RESAMPLING = (Image.Resampling.NEAREST, Image.Resampling.BOX)
ANTIALIAS_RESAMPLING = tuple(
    resample for resample in ALL_RESAMPLING if resample != Image.Resampling.NEAREST
)
SMOOTH_RESAMPLING = tuple(
    resample for resample in ALL_RESAMPLING if resample not in POINT_RESAMPLING
)

ATLAS_SIZE = (320, 240)
SPRITE_SIZE = (64, 48)
SPRITE_OFFSET = (64, 48)
GUARD = 8
SPRITE_BOX = (
    SPRITE_OFFSET[0] - GUARD,
    SPRITE_OFFSET[1] - GUARD,
    SPRITE_OFFSET[0] + SPRITE_SIZE[0] + GUARD,
    SPRITE_OFFSET[1] + SPRITE_SIZE[1] + GUARD,
)

PREVIEW_SIZES = ((20, 16), (40, 32), (80, 64), (160, 128))
DOWNSCALE_SIZES = ((20, 16), (40, 32), (10, 8))

MARKER_OFFSET = (24, 16)
MARKER_SIZE = (16, 16)
DETAIL_OFFSET = (8, 16)
DETAIL_SIZE = (8, 8)

BLANK = {"RGBA": (255, 0, 255, 0), "LA": (255, 0)}
OTHER_BLANK = {
    "RGBA": ((0, 255, 0, 0), (0, 0, 0, 0), (17, 99, 200, 0)),
    "LA": ((0, 0), (17, 0), (200, 0)),
}
BASE_COLOUR = {"RGBA": (200, 30, 40), "LA": (200,)}
MARKER_COLOUR = {"RGBA": (0, 128, 255), "LA": (128,)}
DETAIL_COLOUR = {"RGBA": (120, 40, 200), "LA": (80,)}

NEIGHBOUR_OFFSET = (232, 176)
NEIGHBOUR_SIZE = (48, 40)
NEIGHBOUR_COLOUR = {"RGBA": (30, 90, 200, 200), "LA": (90, 200)}
NEIGHBOUR_BOX = (216, 164, 296, 228)

PREMULTIPLIED_MODE = {"RGBA": "RGBa", "LA": "La"}


def _colour(mode: str, rgb: tuple[int, ...], alpha: int) -> tuple[int, ...]:
    return (*rgb, alpha)


def _sprite_atlas(
    mode: str,
    *,
    padding: tuple[int, ...] | None = None,
    sprite_alpha: int = 255,
    marker_alpha: int = 128,
    detail_alpha: int = 64,
) -> Image.Image:
    """An atlas whose transparent padding surrounds a sprite with details."""
    if padding is None:
        padding = BLANK[mode]
    atlas = Image.new(mode, ATLAS_SIZE, padding)
    sprite = Image.new(
        mode, SPRITE_SIZE, _colour(mode, BASE_COLOUR[mode], sprite_alpha)
    )
    sprite.paste(
        Image.new(
            mode,
            MARKER_SIZE,
            _colour(mode, MARKER_COLOUR[mode], marker_alpha),
        ),
        MARKER_OFFSET,
    )
    sprite.paste(
        Image.new(
            mode,
            DETAIL_SIZE,
            _colour(mode, DETAIL_COLOUR[mode], detail_alpha),
        ),
        DETAIL_OFFSET,
    )
    atlas.paste(sprite, SPRITE_OFFSET)
    return atlas


def _uniform_atlas(
    mode: str, *, sprite_alpha: int, padding: tuple[int, ...] | None = None
) -> Image.Image:
    if padding is None:
        padding = BLANK[mode]
    atlas = Image.new(mode, ATLAS_SIZE, padding)
    atlas.paste(
        Image.new(mode, SPRITE_SIZE, _colour(mode, BASE_COLOUR[mode], sprite_alpha)),
        SPRITE_OFFSET,
    )
    return atlas


def _blob_atlas(mode: str, *, padding: tuple[int, ...] | None = None) -> Image.Image:
    """A single uniform semi-transparent blob, used to track its weight."""
    if padding is None:
        padding = BLANK[mode]
    blob_colour = _colour(mode, (0, 128, 255) if mode == "RGBA" else (128,), 96)
    atlas = Image.new(mode, ATLAS_SIZE, padding)
    atlas.paste(
        Image.new(mode, (24, 24), blob_colour),
        (SPRITE_OFFSET[0] + 20, SPRITE_OFFSET[1] + 12),
    )
    return atlas


def _atlas_with_neighbour(mode: str, with_neighbour: bool) -> Image.Image:
    atlas = _sprite_atlas(mode)
    if with_neighbour:
        atlas.paste(
            Image.new(mode, NEIGHBOUR_SIZE, NEIGHBOUR_COLOUR[mode]),
            NEIGHBOUR_OFFSET,
        )
    return atlas


def _resample(
    atlas: Image.Image,
    size: tuple[int, int],
    resample: Image.Resampling,
    gap: float | None,
) -> Image.Image:
    return atlas.resize(size, resample, box=SPRITE_BOX, reducing_gap=gap)


def _edge_probe_colour(mode: str) -> tuple[int, ...]:
    return _colour(mode, (0, 255, 0) if mode == "RGBA" else (0,), 255)


def _support_radius(size: tuple[int, int], resample: Image.Resampling) -> int:
    """Source pixels beyond the box that the filter may still read."""
    support = Image._filters_support.get(resample, 0.5) - 0.5
    scale = max(
        (SPRITE_BOX[2] - SPRITE_BOX[0]) / size[0],
        (SPRITE_BOX[3] - SPRITE_BOX[1]) / size[1],
    )
    return max(0, math.ceil(support * max(1.0, scale)))


def _safe_reduce_box(
    size: tuple[int, int], resample: Image.Resampling
) -> tuple[int, int, int, int]:
    """The box the first reduce pass must cover for the same filter support."""
    support = Image._filters_support[resample] - 0.5
    scale_x = (SPRITE_BOX[2] - SPRITE_BOX[0]) / size[0]
    scale_y = (SPRITE_BOX[3] - SPRITE_BOX[1]) / size[1]
    return (
        max(0, int(SPRITE_BOX[0] - support * scale_x)),
        max(0, int(SPRITE_BOX[1] - support * scale_y)),
        min(ATLAS_SIZE[0], math.ceil(SPRITE_BOX[2] + support * scale_x)),
        min(ATLAS_SIZE[1], math.ceil(SPRITE_BOX[3] + support * scale_y)),
    )


def _mean_abs_difference(a: Image.Image, b: Image.Image) -> float:
    assert a.mode == b.mode
    assert a.size == b.size
    return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean)


def _alpha_band(im: Image.Image) -> Image.Image:
    return im.getchannel("A")


def _alpha_moments(im: Image.Image) -> tuple[float, float, float]:
    alpha = _alpha_band(im)
    pixels = alpha.load()
    assert pixels is not None
    total = 0.0
    moment_x = 0.0
    moment_y = 0.0
    for y in range(alpha.height):
        for x in range(alpha.width):
            value = _as_int(pixels[x, y])
            total += value
            moment_x += x * value
            moment_y += y * value
    if total == 0:
        return 0.0, 0.0, 0.0
    return total, moment_x / total, moment_y / total


def _assert_visible_equal(a: Image.Image, b: Image.Image, tolerance: int = 0) -> None:
    assert a.mode == b.mode
    assert a.size == b.size
    alpha = _alpha_band(a)
    alpha_difference = ImageChops.difference(alpha, _alpha_band(b))
    if tolerance:
        assert _max_extrema(alpha_difference) <= tolerance
    else:
        assert alpha_difference.getbbox() is None
    visible = ImageChops.lighter(alpha, _alpha_band(b)).point(
        lambda value: 255 if value else 0
    )
    for band_a, band_b in zip(a.split(), b.split()):
        difference = ImageChops.multiply(ImageChops.difference(band_a, band_b), visible)
        assert _max_extrema(difference) <= tolerance


def _assert_transparent_pixels_have_no_colour(im: Image.Image) -> None:
    alpha = _alpha_band(im)
    invisible = alpha.point(lambda value: 255 if value == 0 else 0)
    for band in im.split()[:-1]:
        assert ImageChops.multiply(band, invisible).getbbox() is None


class TestSpriteRegionSelection:
    @pytest.mark.parametrize("mode", ("RGBA", "LA"))
    @pytest.mark.parametrize("resample", ALL_RESAMPLING)
    @pytest.mark.parametrize(
        "size, gap",
        (((20, 16), None), ((40, 32), 2.0), ((160, 128), None)),
    )
    def test_box_selects_only_the_requested_region(
        self,
        mode: str,
        resample: Image.Resampling,
        size: tuple[int, int],
        gap: float | None,
    ) -> None:
        alone = _resample(_atlas_with_neighbour(mode, False), size, resample, gap)
        with_neighbour = _resample(
            _atlas_with_neighbour(mode, True), size, resample, gap
        )
        assert_image_equal(alone, with_neighbour)

    @pytest.mark.parametrize("mode", ("RGBA", "LA"))
    def test_different_crop_position_gives_different_pixels(self, mode: str) -> None:
        atlas = _atlas_with_neighbour(mode, True)
        sprite = atlas.resize((40, 32), Image.Resampling.BICUBIC, box=SPRITE_BOX)
        neighbour = atlas.resize((40, 32), Image.Resampling.BICUBIC, box=NEIGHBOUR_BOX)
        assert sprite.size == neighbour.size
        assert sprite.tobytes() != neighbour.tobytes()

    @pytest.mark.parametrize("mode", ("RGBA", "LA"))
    @pytest.mark.parametrize("resample", POINT_RESAMPLING)
    @pytest.mark.parametrize("size", PREVIEW_SIZES)
    @pytest.mark.parametrize("gap", (None, 2.0, 8.0))
    def test_point_filters_match_crop_then_resize(
        self,
        mode: str,
        resample: Image.Resampling,
        size: tuple[int, int],
        gap: float | None,
    ) -> None:
        atlas = _sprite_atlas(mode)
        from_box = _resample(atlas, size, resample, gap)
        from_crop = atlas.crop(SPRITE_BOX).resize(size, resample, reducing_gap=gap)
        # BOX averaging is not bit-exact on a box boundary in the reference
        # implementation, but nothing visible may move.
        _assert_visible_equal(from_box, from_crop, tolerance=1)


class TestBoxNeighbourhood:
    @pytest.mark.parametrize("mode", ("RGBA", "LA"))
    @pytest.mark.parametrize("resample", ALL_RESAMPLING)
    @pytest.mark.parametrize("size", DOWNSCALE_SIZES)
    @pytest.mark.parametrize("gap", (None, 2.0))
    def test_pixels_beyond_the_support_never_contribute(
        self,
        mode: str,
        resample: Image.Resampling,
        size: tuple[int, int],
        gap: float | None,
    ) -> None:
        radius = _support_radius(size, resample)
        baseline = _resample(_sprite_atlas(mode), size, resample, gap)
        middle_y = (SPRITE_BOX[1] + SPRITE_BOX[3]) // 2
        for distance in range(radius, radius + 3):
            touched = _sprite_atlas(mode)
            touched.putpixel(
                (SPRITE_BOX[2] + distance, middle_y), _edge_probe_colour(mode)
            )
            assert_image_equal(baseline, _resample(touched, size, resample, gap))

    @pytest.mark.parametrize("mode", ("RGBA", "LA"))
    @pytest.mark.parametrize("resample", SMOOTH_RESAMPLING)
    @pytest.mark.parametrize("size", DOWNSCALE_SIZES)
    def test_antialias_filter_reads_pixels_just_outside_the_box(
        self, mode: str, resample: Image.Resampling, size: tuple[int, int]
    ) -> None:
        # On a fair resample, an adjacent source pixel is part of the filter
        # support and must be able to change the result.
        assert _support_radius(size, resample) > 0
        baseline = _resample(_sprite_atlas(mode), size, resample, None)
        touched = _sprite_atlas(mode)
        middle_y = (SPRITE_BOX[1] + SPRITE_BOX[3]) // 2
        touched.putpixel((SPRITE_BOX[2], middle_y), _edge_probe_colour(mode))
        assert baseline.tobytes() != _resample(touched, size, resample, None).tobytes()

    @pytest.mark.parametrize("mode", ("RGBA", "LA"))
    @pytest.mark.parametrize(
        "resample",
        (
            Image.Resampling.BILINEAR,
            Image.Resampling.HAMMING,
            Image.Resampling.BICUBIC,
            Image.Resampling.LANCZOS,
        ),
    )
    @pytest.mark.parametrize(
        "size, gap",
        (((20, 16), 1.0), ((20, 16), 2.0), ((40, 32), 2.0), ((40, 32), 1.0)),
    )
    def test_two_stage_path_still_reads_the_support_outside_the_box(
        self,
        mode: str,
        resample: Image.Resampling,
        size: tuple[int, int],
        gap: float,
    ) -> None:
        # The reduce pass must be given an expanded box; otherwise the pixel
        # just beyond the crop box would silently stop contributing when the
        # requested size triggers a first reduce step.
        baseline = _resample(_sprite_atlas(mode), size, resample, gap)
        touched = _sprite_atlas(mode)
        middle_y = (SPRITE_BOX[1] + SPRITE_BOX[3]) // 2
        touched.putpixel((SPRITE_BOX[2], middle_y), _edge_probe_colour(mode))
        gapped = _resample(touched, size, resample, gap)
        assert baseline.tobytes() != gapped.tobytes()

        # The same pixel must matter identically on the fair path.
        fair_baseline = _resample(_sprite_atlas(mode), size, resample, None)
        fair_touched = _resample(touched, size, resample, None)
        assert fair_baseline.tobytes() != fair_touched.tobytes()

    @pytest.mark.parametrize("mode", ("RGBA", "LA"))
    @pytest.mark.parametrize("resample", POINT_RESAMPLING)
    @pytest.mark.parametrize("size", DOWNSCALE_SIZES)
    def test_point_filters_ignore_pixels_outside_the_box(
        self, mode: str, resample: Image.Resampling, size: tuple[int, int]
    ) -> None:
        assert _support_radius(size, resample) == 0
        baseline = _resample(_sprite_atlas(mode), size, resample, None)
        middle_y = (SPRITE_BOX[1] + SPRITE_BOX[3]) // 2
        for distance in range(3):
            touched = _sprite_atlas(mode)
            touched.putpixel(
                (SPRITE_BOX[2] + distance, middle_y), _edge_probe_colour(mode)
            )
            assert_image_equal(baseline, _resample(touched, size, resample, None))


class TestTransparentPadding:
    @pytest.mark.parametrize("mode", ("RGBA", "LA"))
    @pytest.mark.parametrize("resample", ALL_RESAMPLING)
    @pytest.mark.parametrize("size", ((40, 32), (80, 64)))
    @pytest.mark.parametrize("gap", (None, 2.0, 8.0))
    def test_hidden_colour_does_not_reach_visible_pixels(
        self,
        mode: str,
        resample: Image.Resampling,
        size: tuple[int, int],
        gap: float | None,
    ) -> None:
        reference = _resample(_sprite_atlas(mode), size, resample, gap)
        for padding in OTHER_BLANK[mode]:
            other = _resample(_sprite_atlas(mode, padding=padding), size, resample, gap)
            if resample == Image.Resampling.NEAREST:
                # Nearest neighbour copies whole pixels, so only the pixels
                # that end up visible are guaranteed to be identical.
                _assert_visible_equal(reference, other)
            else:
                assert_image_equal(reference, other)

    @pytest.mark.parametrize("mode", ("RGBA", "LA"))
    @pytest.mark.parametrize("resample", ANTIALIAS_RESAMPLING)
    @pytest.mark.parametrize("size", ((20, 16), (40, 32), (80, 64)))
    @pytest.mark.parametrize("gap", (None, 2.0))
    def test_fully_transparent_pixels_carry_no_colour(
        self,
        mode: str,
        resample: Image.Resampling,
        size: tuple[int, int],
        gap: float | None,
    ) -> None:
        _assert_transparent_pixels_have_no_colour(
            _resample(_sprite_atlas(mode), size, resample, gap)
        )


class TestAlphaWeighting:
    @pytest.mark.parametrize("mode", ("RGBA", "LA"))
    @pytest.mark.parametrize("resample", ANTIALIAS_RESAMPLING)
    @pytest.mark.parametrize("size", PREVIEW_SIZES)
    @pytest.mark.parametrize("gap", (None, 2.0, 8.0))
    def test_matches_premultiplied_reference(
        self,
        mode: str,
        resample: Image.Resampling,
        size: tuple[int, int],
        gap: float | None,
    ) -> None:
        atlas = _sprite_atlas(mode)
        expected = (
            atlas.convert(PREMULTIPLIED_MODE[mode])
            .resize(size, resample, box=SPRITE_BOX, reducing_gap=gap)
            .convert(mode)
        )
        assert_image_equal(_resample(atlas, size, resample, gap), expected)

    @pytest.mark.parametrize("mode", ("RGBA", "LA"))
    @pytest.mark.parametrize("resample", ALL_RESAMPLING)
    @pytest.mark.parametrize("size", ((20, 16), (40, 32)))
    @pytest.mark.parametrize("gap", (None, 2.0))
    def test_alpha_channel_is_resampled_independently(
        self,
        mode: str,
        resample: Image.Resampling,
        size: tuple[int, int],
        gap: float | None,
    ) -> None:
        atlas = _sprite_atlas(mode)
        resized = _resample(atlas, size, resample, gap)
        alpha_only = atlas.getchannel("A").resize(
            size, resample, box=SPRITE_BOX, reducing_gap=gap
        )
        assert_image_equal(_alpha_band(resized), alpha_only)

    @pytest.mark.parametrize("mode", ("RGBA", "LA"))
    @pytest.mark.parametrize("factor", (2, (2, 3), (3, 2)))
    def test_reduce_matches_premultiplied_reduce(
        self, mode: str, factor: int | tuple[int, int]
    ) -> None:
        atlas = _sprite_atlas(mode)
        expected = (
            atlas.convert(PREMULTIPLIED_MODE[mode])
            .reduce(factor, box=SPRITE_BOX)
            .convert(mode)
        )
        assert_image_equal(atlas.reduce(factor, box=SPRITE_BOX), expected)


class TestTwoStagePipeline:
    @pytest.mark.parametrize("mode", ("RGBA", "LA"))
    @pytest.mark.parametrize(
        "resample",
        (
            Image.Resampling.BOX,
            Image.Resampling.BICUBIC,
            Image.Resampling.LANCZOS,
        ),
    )
    @pytest.mark.parametrize(
        "size, gap",
        (
            ((20, 16), 1.0),
            ((20, 16), 2.0),
            ((10, 8), 2.0),
            ((10, 8), 3.0),
            ((160, 128), 8.0),
        ),
    )
    def test_reduce_then_resize_is_the_only_pipeline(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mode: str,
        resample: Image.Resampling,
        size: tuple[int, int],
        gap: float,
    ) -> None:
        calls: list[
            tuple[
                str,
                tuple[int, int],
                tuple[int, int, int, int] | None,
                Image.Image,
            ]
        ] = []
        original_reduce = Image.Image.reduce

        def spy(
            self: Image.Image,
            factor: int | tuple[int, int],
            box: tuple[int, int, int, int] | None = None,
        ) -> Image.Image:
            reduced = original_reduce(self, factor, box)
            factor_pair = factor if isinstance(factor, tuple) else (factor, factor)
            calls.append((self.mode, factor_pair, box, reduced.copy()))
            return reduced

        monkeypatch.setattr(Image.Image, "reduce", spy)

        atlas = _sprite_atlas(mode)
        got = _resample(atlas, size, resample, gap)

        expected_factor = (
            int((SPRITE_BOX[2] - SPRITE_BOX[0]) / size[0] / gap) or 1,
            int((SPRITE_BOX[3] - SPRITE_BOX[1]) / size[1] / gap) or 1,
        )
        if expected_factor == (1, 1):
            assert calls == []
            reference = (
                atlas.convert(PREMULTIPLIED_MODE[mode])
                .resize(size, resample, box=SPRITE_BOX)
                .convert(mode)
            )
            assert_image_equal(got, reference)
            return

        assert len(calls) == 1
        reduced_mode, factor, reduce_box, reduced = calls[0]
        assert reduced_mode == PREMULTIPLIED_MODE[mode]
        assert factor == expected_factor
        # The first pass must be given a box that is at least as large as the
        # requested box, so it does not clip the pixels the final filter reads.
        expected_reduce_box = _safe_reduce_box(size, resample)
        assert reduce_box == expected_reduce_box
        assert reduce_box[0] <= SPRITE_BOX[0]
        assert reduce_box[1] <= SPRITE_BOX[1]
        assert reduce_box[2] >= SPRITE_BOX[2]
        assert reduce_box[3] >= SPRITE_BOX[3]

        map_x = (SPRITE_BOX[0] - reduce_box[0]) / factor[0]
        map_y = (SPRITE_BOX[1] - reduce_box[1]) / factor[1]
        mapped_box = (
            map_x,
            map_y,
            (SPRITE_BOX[2] - reduce_box[0]) / factor[0],
            (SPRITE_BOX[3] - reduce_box[1]) / factor[1],
        )
        assert_image_equal(
            got,
            reduced.resize(size, resample, box=mapped_box).convert(mode),
        )

    @pytest.mark.parametrize("mode", ("RGBA", "LA"))
    @pytest.mark.parametrize("resample", ALL_RESAMPLING)
    @pytest.mark.parametrize("size", PREVIEW_SIZES)
    def test_more_gap_converges_to_fair_resampling(
        self, mode: str, resample: Image.Resampling, size: tuple[int, int]
    ) -> None:
        atlas = _sprite_atlas(mode)
        fair = _resample(atlas, size, resample, None)
        differences = [
            _mean_abs_difference(fair, _resample(atlas, size, resample, gap))
            for gap in (1.0, 2.0, 3.0)
        ]
        assert differences[0] + 1e-9 >= differences[1]
        assert differences[1] + 1e-9 >= differences[2]
        assert_image_equal(fair, _resample(atlas, size, resample, 8.0))

    @pytest.mark.parametrize("mode", ("RGBA", "LA"))
    @pytest.mark.parametrize("size", PREVIEW_SIZES)
    def test_nearest_ignores_the_reducing_gap(
        self, mode: str, size: tuple[int, int]
    ) -> None:
        # NEAREST never takes the first (reduce) step, so the gap cannot
        # change which source pixel is copied.
        atlas = _sprite_atlas(mode)
        fair = _resample(atlas, size, Image.Resampling.NEAREST, None)
        for gap in (1.0, 2.0, 8.0):
            assert_image_equal(
                fair, _resample(atlas, size, Image.Resampling.NEAREST, gap)
            )


class TestSemiTransparentDetail:
    @pytest.mark.parametrize("mode", ("RGBA", "LA"))
    @pytest.mark.parametrize("resample", ANTIALIAS_RESAMPLING)
    @pytest.mark.parametrize("size", PREVIEW_SIZES)
    @pytest.mark.parametrize("gap", (None, 2.0, 8.0))
    def test_interior_colour_and_alpha_are_unchanged(
        self,
        mode: str,
        resample: Image.Resampling,
        size: tuple[int, int],
        gap: float | None,
    ) -> None:
        alpha = 96
        atlas = _uniform_atlas(mode, sprite_alpha=alpha)
        resized = _resample(atlas, size, resample, gap)
        scale_x = (SPRITE_BOX[2] - SPRITE_BOX[0]) / size[0]
        scale_y = (SPRITE_BOX[3] - SPRITE_BOX[1]) / size[1]
        margin = _support_radius(size, resample) + 2
        pixels = resized.load()
        assert pixels is not None
        target = BASE_COLOUR[mode]
        for y in range(size[1]):
            source_y = SPRITE_BOX[1] + (y + 0.5) * scale_y
            if not (
                SPRITE_OFFSET[1] + margin
                < source_y
                < SPRITE_OFFSET[1] + SPRITE_SIZE[1] - margin
            ):
                continue
            for x in range(size[0]):
                source_x = SPRITE_BOX[0] + (x + 0.5) * scale_x
                if not (
                    SPRITE_OFFSET[0] + margin
                    < source_x
                    < SPRITE_OFFSET[0] + SPRITE_SIZE[0] - margin
                ):
                    continue
                pixel = _as_pixel(pixels[x, y])
                assert abs(pixel[-1] - alpha) <= 2
                for index, component in enumerate(target):
                    assert abs(pixel[index] - component) <= 2

    @pytest.mark.parametrize("mode", ("RGBA", "LA"))
    @pytest.mark.parametrize("resample", ANTIALIAS_RESAMPLING)
    @pytest.mark.parametrize("size", PREVIEW_SIZES + ((10, 8),))
    @pytest.mark.parametrize("gap", (None, 1.0, 2.0, 3.0, 8.0))
    def test_edge_is_not_darkened_towards_the_hidden_colour(
        self,
        mode: str,
        resample: Image.Resampling,
        size: tuple[int, int],
        gap: float | None,
    ) -> None:
        # Fully opaque sprite on a fully transparent, coloured background:
        # premultiplied resampling must keep visible edge colours close to the
        # sprite colour instead of fading towards black or the hidden colour.
        resized = _resample(_uniform_atlas(mode, sprite_alpha=255), size, resample, gap)
        for raw_pixel in resized.get_flattened_data():
            pixel = _as_pixel(raw_pixel)
            if pixel[-1] < 32:
                continue
            for index, component in enumerate(BASE_COLOUR[mode]):
                assert pixel[index] >= component - 16

    @pytest.mark.parametrize("mode", ("RGBA", "LA"))
    @pytest.mark.parametrize(
        "resample",
        (
            Image.Resampling.BOX,
            Image.Resampling.BILINEAR,
            Image.Resampling.BICUBIC,
            Image.Resampling.LANCZOS,
            Image.Resampling.MKS2021,
        ),
    )
    @pytest.mark.parametrize("size", PREVIEW_SIZES + ((32, 22),))
    def test_blob_weight_and_position_are_stable(
        self, mode: str, resample: Image.Resampling, size: tuple[int, int]
    ) -> None:
        blob_centre_x = SPRITE_OFFSET[0] + 20 + 24 / 2
        blob_centre_y = SPRITE_OFFSET[1] + 12 + 24 / 2
        expected_x = (blob_centre_x - SPRITE_BOX[0]) / (
            SPRITE_BOX[2] - SPRITE_BOX[0]
        ) * size[0] - 0.5
        expected_y = (blob_centre_y - SPRITE_BOX[1]) / (
            SPRITE_BOX[3] - SPRITE_BOX[1]
        ) * size[1] - 0.5

        atlas = _blob_atlas(mode)
        fair_mass, fair_x, fair_y = _alpha_moments(
            _resample(atlas, size, resample, None)
        )
        assert abs(fair_x - expected_x) <= 0.25
        assert abs(fair_y - expected_y) <= 0.25

        for gap in (2.0, 3.0, 8.0):
            mass, centre_x, centre_y = _alpha_moments(
                _resample(atlas, size, resample, gap)
            )
            assert abs(mass / fair_mass - 1) <= 0.03
            assert abs(centre_x - fair_x) <= 0.2
            assert abs(centre_y - fair_y) <= 0.2

    @pytest.mark.parametrize("mode", ("RGBA", "LA"))
    @pytest.mark.parametrize(
        "resample",
        (
            Image.Resampling.BILINEAR,
            Image.Resampling.BICUBIC,
            Image.Resampling.LANCZOS,
        ),
    )
    @pytest.mark.parametrize("size", PREVIEW_SIZES + ((10, 8),))
    def test_resampling_a_larger_preview_down_keeps_weight_and_position(
        self, mode: str, resample: Image.Resampling, size: tuple[int, int]
    ) -> None:
        # This is the geometry a two-step "reduce first" pipeline approximates:
        # resampling twice must not shift or thin out the semi-transparent
        # detail relative to a single fair resample.
        atlas = _blob_atlas(mode)
        direct = _alpha_moments(_resample(atlas, size, resample, None))
        larger = (size[0] * 2, size[1] * 2)
        two_pass = _alpha_moments(
            _resample(atlas, larger, resample, None).resize(size, resample)
        )
        assert abs(two_pass[0] / direct[0] - 1) <= 0.05
        assert abs(two_pass[1] - direct[1]) <= 0.1
        assert abs(two_pass[2] - direct[2]) <= 0.1

    @pytest.mark.parametrize("mode", ("RGBA", "LA"))
    @pytest.mark.parametrize("size", PREVIEW_SIZES)
    def test_crop_box_shift_moves_the_detail_by_the_expected_amount(
        self, mode: str, size: tuple[int, int]
    ) -> None:
        atlas = _blob_atlas(mode)
        reference = _alpha_moments(
            _resample(atlas, size, Image.Resampling.BICUBIC, None)
        )
        for shift in (1, 2, 3):
            shifted_box = (
                SPRITE_BOX[0] + shift,
                SPRITE_BOX[1] + shift,
                SPRITE_BOX[2] + shift,
                SPRITE_BOX[3] + shift,
            )
            shifted = _alpha_moments(
                atlas.resize(size, Image.Resampling.BICUBIC, box=shifted_box)
            )
            expected_x = size[0] * shift / (SPRITE_BOX[2] - SPRITE_BOX[0])
            expected_y = size[1] * shift / (SPRITE_BOX[3] - SPRITE_BOX[1])
            assert abs(-(shifted[1] - reference[1]) - expected_x) <= 0.3
            assert abs(-(shifted[2] - reference[2]) - expected_y) <= 0.3


class TestSpriteModesAndSources:
    @pytest.mark.parametrize("mode", ("RGBA", "LA"))
    @pytest.mark.parametrize("resample", ALL_RESAMPLING)
    @pytest.mark.parametrize("gap", (None, 2.0))
    def test_resampling_keeps_the_mode(
        self, mode: str, resample: Image.Resampling, gap: float | None
    ) -> None:
        atlas = _sprite_atlas(mode)
        resized = _resample(atlas, (40, 32), resample, gap)
        assert resized.mode == mode
        assert len(resized.getbands()) == (4 if mode == "RGBA" else 2)
        assert resized.size == (40, 32)

        reduced = atlas.reduce(2, box=SPRITE_BOX)
        assert reduced.mode == mode

    @pytest.mark.parametrize("mode", ("RGBA", "LA"))
    @pytest.mark.parametrize("resample", ALL_RESAMPLING)
    def test_resizing_does_not_modify_the_atlas(
        self, mode: str, resample: Image.Resampling
    ) -> None:
        atlas = _sprite_atlas(mode)
        before = atlas.tobytes()
        size = atlas.size
        for gap in (None, 2.0):
            _resample(atlas, (40, 32), resample, gap)
        atlas.reduce(2, box=SPRITE_BOX)
        assert atlas.size == size
        assert atlas.tobytes() == before

    @pytest.mark.parametrize("mode", ("RGBA", "LA"))
    def test_repeated_resizes_are_deterministic(self, mode: str) -> None:
        atlas = _sprite_atlas(mode)
        first = _resample(atlas, (32, 22), Image.Resampling.BICUBIC, 2.0)
        second = _resample(atlas, (32, 22), Image.Resampling.BICUBIC, 2.0)
        assert_image_equal(first, second)


class TestSpriteThumbnail:
    @pytest.mark.parametrize("mode", ("RGBA", "LA"))
    @pytest.mark.parametrize(
        "request_size, expected_size",
        (
            ((32, 32), (32, 26)),
            ((16, 16), (16, 13)),
            ((10, 100), (10, 8)),
            ((128, 90), (80, 64)),
            ((40, 20), (25, 20)),
            ((7, 9), (7, 6)),
            ((96, 60), (75, 60)),
            ((1000, 1000), (80, 64)),
        ),
    )
    def test_thumbnail_size_and_content(
        self,
        mode: str,
        request_size: tuple[int, int],
        expected_size: tuple[int, int],
    ) -> None:
        atlas = _sprite_atlas(mode)
        atlas_before = atlas.tobytes()
        sprite = atlas.crop(SPRITE_BOX)
        sprite_before = sprite.tobytes()

        thumbnail = sprite.copy()
        thumbnail.thumbnail(request_size)

        assert thumbnail.size == expected_size
        assert thumbnail.width <= request_size[0]
        assert thumbnail.height <= request_size[1]
        assert thumbnail.mode == mode
        if thumbnail.size == sprite.size:
            assert_image_equal(thumbnail, sprite)
        else:
            assert_image_equal(
                thumbnail,
                sprite.resize(
                    thumbnail.size,
                    Image.Resampling.BICUBIC,
                    reducing_gap=2.0,
                ),
            )

        # thumbnail() modifies its own image only.
        assert atlas.tobytes() == atlas_before
        assert sprite.tobytes() == sprite_before

    @pytest.mark.parametrize("mode", ("RGBA", "LA"))
    @pytest.mark.parametrize("resample", ALL_RESAMPLING)
    def test_thumbnail_matches_resize_for_every_filter(
        self, mode: str, resample: Image.Resampling
    ) -> None:
        sprite = _sprite_atlas(mode).crop(SPRITE_BOX)
        thumbnail = sprite.copy()
        thumbnail.thumbnail((32, 32), resample=resample)
        expected = sprite.resize(thumbnail.size, resample, reducing_gap=2.0)
        assert_image_equal(thumbnail, expected)
