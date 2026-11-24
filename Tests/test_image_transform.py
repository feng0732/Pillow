from __future__ import annotations

import math

import pytest

from PIL import Image, ImageTransform

from .helper import assert_image_equal, assert_image_similar, hopper

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Callable


class TestImageTransform:
    def test_sanity(self) -> None:
        im = hopper()

        for transform in (
            ImageTransform.AffineTransform((1, 0, 0, 0, 1, 0)),
            ImageTransform.PerspectiveTransform((1, 0, 0, 0, 1, 0, 0, 0)),
            ImageTransform.ExtentTransform((0, 0, *im.size)),
            ImageTransform.QuadTransform(
                (0, 0, 0, im.height, im.width, im.height, im.width, 0)
            ),
            ImageTransform.MeshTransform(
                [
                    (
                        (0, 0, *im.size),
                        (0, 0, 0, im.height, im.width, im.height, im.width, 0),
                    )
                ]
            ),
        ):
            assert_image_equal(im, im.transform(im.size, transform))

    def test_info(self) -> None:
        comment = b"File written by Adobe Photoshop\xa8 4.0"

        with Image.open("Tests/images/hopper.gif") as im:
            assert im.info["comment"] == comment

            transform = ImageTransform.ExtentTransform((0, 0, 0, 0))
            new_im = im.transform((100, 100), transform)
        assert new_im.info["comment"] == comment

    @pytest.mark.parametrize("mode", ("P", "PA"))
    def test_palette(self, mode: str) -> None:
        im = hopper(mode)
        transformed = im.transform(im.size, Image.Transform.AFFINE, [1, 0, 0, 0, 1, 0])
        assert im.palette is not None
        assert transformed.palette is not None
        assert im.palette.palette == transformed.palette.palette

    def test_extent(self) -> None:
        im = hopper("RGB")
        w, h = im.size
        transformed = im.transform(
            im.size,
            Image.Transform.EXTENT,
            (0, 0, w // 2, h // 2),  # ul -> lr
            Image.Resampling.BILINEAR,
        )

        scaled = im.resize((w * 2, h * 2), Image.Resampling.BILINEAR).crop((0, 0, w, h))

        # undone -- precision?
        assert_image_similar(transformed, scaled, 23)

    def test_quad(self) -> None:
        # one simple quad transform, equivalent to scale & crop upper left quad
        im = hopper("RGB")
        w, h = im.size
        transformed = im.transform(
            im.size,
            Image.Transform.QUAD,
            (0, 0, 0, h // 2, w // 2, h // 2, w // 2, 0),  # ul -> ccw around quad
            Image.Resampling.BILINEAR,
        )

        scaled = im.transform(
            (w, h),
            Image.Transform.AFFINE,
            (0.5, 0, 0, 0, 0.5, 0),
            Image.Resampling.BILINEAR,
        )

        assert_image_equal(transformed, scaled)

    @pytest.mark.parametrize(
        "mode, expected_pixel",
        (
            ("RGB", (255, 0, 0)),
            ("RGBA", (255, 0, 0, 255)),
            ("LA", (76, 255)),
        ),
    )
    def test_fill(self, mode: str, expected_pixel: tuple[int, ...]) -> None:
        im = hopper(mode)
        w, h = im.size
        transformed = im.transform(
            im.size,
            Image.Transform.EXTENT,
            (0, 0, w * 2, h * 2),
            Image.Resampling.BILINEAR,
            fillcolor="red",
        )
        assert transformed.getpixel((w - 1, h - 1)) == expected_pixel

    def test_mesh(self) -> None:
        # this should be a checkerboard of halfsized hoppers in ul, lr
        im = hopper("RGBA")
        w, h = im.size
        transformed = im.transform(
            im.size,
            Image.Transform.MESH,
            (
                (
                    (0, 0, w // 2, h // 2),  # box
                    (0, 0, 0, h, w, h, w, 0),  # ul -> ccw around quad
                ),
                (
                    (w // 2, h // 2, w, h),  # box
                    (0, 0, 0, h, w, h, w, 0),  # ul -> ccw around quad
                ),
            ),
            Image.Resampling.BILINEAR,
        )

        scaled = im.transform(
            (w // 2, h // 2),
            Image.Transform.AFFINE,
            (2, 0, 0, 0, 2, 0),
            Image.Resampling.BILINEAR,
        )

        checker = Image.new("RGBA", im.size)
        checker.paste(scaled, (0, 0))
        checker.paste(scaled, (w // 2, h // 2))

        assert_image_equal(transformed, checker)

        # now, check to see that the extra area is (0, 0, 0, 0)
        blank = Image.new("RGBA", (w // 2, h // 2), (0, 0, 0, 0))

        assert_image_equal(blank, transformed.crop((w // 2, 0, w, h // 2)))
        assert_image_equal(blank, transformed.crop((0, h // 2, w // 2, h)))

    @pytest.mark.parametrize(
        "resample",
        (
            Image.Resampling.NEAREST,
            Image.Resampling.BILINEAR,
            Image.Resampling.BICUBIC,
        ),
    )
    def test_mesh_adjacent_grid(self, resample: Image.Resampling) -> None:
        # The destination boxes tile the canvas.  Their identity quads use
        # the documented NW, SW, SE, NE order and the corner coordinate
        # convention, so every output pixel samples the same source pixel.
        size = (9, 7)
        source = Image.new("RGB", size)
        source.putdata(
            [
                ((17 * x) % 256, (29 * y) % 256, (11 * (x + y)) % 256)
                for y in range(size[1])
                for x in range(size[0])
            ]
        )

        xs = (0, 4, size[0])
        ys = (0, 3, size[1])
        mesh = [
            (
                (x0, y0, x1, y1),
                (x0, y0, x0, y1, x1, y1, x1, y0),
            )
            for y0, y1 in zip(ys, ys[1:])
            for x0, x1 in zip(xs, xs[1:])
        ]

        transformed = source.transform(size, Image.Transform.MESH, mesh, resample)
        assert transformed.size == size
        assert_image_equal(transformed, source)

        # Pixels on both sides of each shared edge retain their own source
        # coordinates instead of being shifted or exchanged by the split.
        for y in range(size[1]):
            assert transformed.getpixel((3, y)) == source.getpixel((3, y))
            assert transformed.getpixel((4, y)) == source.getpixel((4, y))
        for x in range(size[0]):
            assert transformed.getpixel((x, 2)) == source.getpixel((x, 2))
            assert transformed.getpixel((x, 3)) == source.getpixel((x, 3))

        reordered = source.transform(size, Image.Transform.MESH, mesh[::-1], resample)
        assert reordered.size == size
        assert_image_equal(reordered, source)

        via_transform = source.transform(
            size, ImageTransform.MeshTransform(mesh), resample=resample
        )
        assert_image_equal(via_transform, source)

    @pytest.mark.parametrize(
        "resample",
        (
            Image.Resampling.NEAREST,
            Image.Resampling.BILINEAR,
            Image.Resampling.BICUBIC,
        ),
    )
    def test_mesh_quad_samples_into_destination_box(
        self, resample: Image.Resampling
    ) -> None:
        source = Image.new("L", (4, 2))
        source.putdata([10, 20, 30, 40, 50, 60, 70, 80])

        # The box is the rectangle to write in output coordinates; the quad
        # is the four source positions to sample, in NW, SW, SE, NE order.
        transformed = source.transform(
            (4, 2),
            Image.Transform.MESH,
            [((2, 0, 4, 2), (0, 0, 0, 2, 2, 2, 2, 0))],
            resample,
        )

        expected = Image.new("L", (4, 2))
        expected.putdata([0, 0, 10, 20, 0, 0, 50, 60])
        assert_image_equal(transformed, expected)

    @pytest.mark.parametrize(
        "matrix",
        (
            (2, 0, 0, 0, 2, 0),
            (1, 1, 0, 0, 1, 0),
        ),
    )
    @pytest.mark.parametrize(
        "resample",
        (
            Image.Resampling.NEAREST,
            Image.Resampling.BILINEAR,
            Image.Resampling.BICUBIC,
        ),
    )
    def test_mesh_grid_source_positions(
        self,
        matrix: tuple[int, int, int, int, int, int],
        resample: Image.Resampling,
    ) -> None:
        size = (8, 6)
        source = Image.new("RGB", (16, 12))
        source.putdata(
            [
                ((17 * x) % 256, (29 * y) % 256, (11 * (x + y)) % 256)
                for y in range(source.height)
                for x in range(source.width)
            ]
        )

        a, b, c, d, e, f = matrix

        def map_corner(x: int, y: int) -> tuple[int, int]:
            return a * x + b * y + c, d * x + e * y + f

        xs = (0, 3, size[0])
        ys = (0, 2, size[1])
        mesh = []
        for y0, y1 in zip(ys, ys[1:]):
            for x0, x1 in zip(xs, xs[1:]):
                nw = map_corner(x0, y0)
                sw = map_corner(x0, y1)
                se = map_corner(x1, y1)
                ne = map_corner(x1, y0)
                mesh.append(
                    (
                        (x0, y0, x1, y1),
                        (*nw, *sw, *se, *ne),
                    )
                )

        transformed = source.transform(size, Image.Transform.MESH, mesh, resample)
        expected = source.transform(size, Image.Transform.AFFINE, matrix, resample)
        assert transformed.size == size
        assert_image_equal(transformed, expected)

        reordered = source.transform(
            size, Image.Transform.MESH, [mesh[3], mesh[0], mesh[2], mesh[1]], resample
        )
        assert_image_equal(reordered, expected)

        if resample == Image.Resampling.NEAREST and matrix == (2, 0, 0, 0, 2, 0):
            for y in range(size[1]):
                for x in range(size[0]):
                    assert transformed.getpixel((x, y)) == source.getpixel(
                        (2 * x + 1, 2 * y + 1)
                    )

    @pytest.mark.parametrize("mode", ("RGB", "RGBA", "LA"))
    @pytest.mark.parametrize("fillcolor", (None, "red"))
    @pytest.mark.parametrize(
        "resample",
        (
            Image.Resampling.NEAREST,
            Image.Resampling.BILINEAR,
            Image.Resampling.BICUBIC,
        ),
    )
    def test_mesh_uncovered_grid_area(
        self,
        mode: str,
        fillcolor: str | None,
        resample: Image.Resampling,
    ) -> None:
        size = (8, 6)
        rgb = Image.new("RGB", size)
        rgb.putdata(
            [
                ((31 * x) % 256, (43 * y) % 256, (17 * (x + y)) % 256)
                for y in range(size[1])
                for x in range(size[0])
            ]
        )
        source = rgb.convert(mode)

        # Omit the lower-left block.  The other three share both seams and
        # leave one explicitly uncovered output rectangle.
        boxes = ((0, 0, 4, 3), (4, 0, 8, 3), (4, 3, 8, 6))
        mesh = [
            (box, (box[0], box[1], box[0], box[3], box[2], box[3], box[2], box[1]))
            for box in boxes
        ]

        transformed = source.transform(
            size,
            Image.Transform.MESH,
            mesh,
            resample,
            fillcolor=fillcolor,
        )

        fill = Image.new(mode, (1, 1), fillcolor).getpixel((0, 0))
        assert isinstance(fill, tuple)
        expected = Image.new(mode, size, fillcolor)
        for box in boxes:
            expected.paste(source.crop(box), box[:2])

        assert transformed.size == size
        assert_image_equal(transformed, expected)

        for y in range(3, 6):
            for x in range(4):
                assert transformed.getpixel((x, y)) == fill
        if mode in {"RGBA", "LA"} and fillcolor is None:
            assert fill[-1] == 0

    @pytest.mark.parametrize(
        "resample",
        (
            Image.Resampling.NEAREST,
            Image.Resampling.BILINEAR,
            Image.Resampling.BICUBIC,
        ),
    )
    @pytest.mark.parametrize(
        "fillcolor,expected_fill",
        (
            (None, (0, 0, 0, 0)),
            ("red", (255, 0, 0, 255)),
        ),
    )
    def test_mesh_quad_outside_source_fill(
        self,
        resample: Image.Resampling,
        fillcolor: str | None,
        expected_fill: tuple[int, int, int, int],
    ) -> None:
        source = Image.new("RGBA", (4, 3))
        source.putdata(
            [
                ((40 * x) % 256, (60 * y) % 256, 128, 255)
                for y in range(source.height)
                for x in range(source.width)
            ]
        )

        size = (8, 5)
        box = (2, 1, 6, 4)
        # The left half of this quad is before x=0 in the source; its right
        # half maps destination columns 4 and 5 to source columns 0 and 1.
        quad = (-2, 0, -2, 3, 2, 3, 2, 0)
        transformed = source.transform(
            size,
            Image.Transform.MESH,
            [(box, quad)],
            resample,
            fillcolor=fillcolor,
        )

        expected = Image.new("RGBA", size, fillcolor)
        expected.paste(source.crop((0, 0, 2, 3)), (4, 1))

        assert transformed.size == size
        assert_image_equal(transformed, expected)
        for x in (2, 3):
            for y in range(1, 4):
                assert transformed.getpixel((x, y)) == expected_fill

    @pytest.mark.parametrize(
        "resample",
        (Image.Resampling.BILINEAR, Image.Resampling.BICUBIC),
    )
    def test_mesh_alpha_premult(self, resample: Image.Resampling) -> None:
        def op(im: Image.Image, size: tuple[int, int]) -> Image.Image:
            w, h = im.size
            return im.transform(
                size,
                Image.Transform.MESH,
                [((0, 0, *size), (0, 0, 0, h, w, h, w, 0))],
                resample,
            )

        self._test_alpha_premult(op)

    @pytest.mark.parametrize("mode", ("RGBA", "LA"))
    def test_mesh_nearest_alpha(self, mode: str) -> None:
        def op(im: Image.Image, size: tuple[int, int]) -> Image.Image:
            w, h = im.size
            return im.transform(
                size,
                Image.Transform.MESH,
                [((0, 0, *size), (0, 0, 0, h, w, h, w, 0))],
                Image.Resampling.NEAREST,
            )

        self._test_nearest(op, mode)

    def _test_alpha_premult(
        self, op: Callable[[Image.Image, tuple[int, int]], Image.Image]
    ) -> None:
        # create image with half white, half black,
        # with the black half transparent.
        # do op,
        # there should be no darkness in the white section.
        im = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
        im2 = Image.new("RGBA", (5, 10), (255, 255, 255, 255))
        im.paste(im2, (0, 0))

        im = op(im, (40, 10))
        im_background = Image.new("RGB", (40, 10), (255, 255, 255))
        im_background.paste(im, (0, 0), im)

        hist = im_background.histogram()
        assert 40 * 10 == hist[-1]

    def test_alpha_premult_resize(self) -> None:
        def op(im: Image.Image, sz: tuple[int, int]) -> Image.Image:
            return im.resize(sz, Image.Resampling.BILINEAR)

        self._test_alpha_premult(op)

    def test_alpha_premult_transform(self) -> None:
        def op(im: Image.Image, sz: tuple[int, int]) -> Image.Image:
            w, h = im.size
            return im.transform(
                sz, Image.Transform.EXTENT, (0, 0, w, h), Image.Resampling.BILINEAR
            )

        self._test_alpha_premult(op)

    def _test_nearest(
        self, op: Callable[[Image.Image, tuple[int, int]], Image.Image], mode: str
    ) -> None:
        # create white image with half transparent,
        # do op,
        # the image should remain white with half transparent
        transparent, opaque = {
            "RGBA": ((255, 255, 255, 0), (255, 255, 255, 255)),
            "LA": ((255, 0), (255, 255)),
        }[mode]
        im = Image.new(mode, (10, 10), transparent)
        im2 = Image.new(mode, (5, 10), opaque)
        im.paste(im2, (0, 0))

        im = op(im, (40, 10))

        colors = im.getcolors()
        assert colors is not None
        assert sorted(colors) == sorted(
            (
                (20 * 10, opaque),
                (20 * 10, transparent),
            )
        )

    @pytest.mark.parametrize("mode", ("RGBA", "LA"))
    def test_nearest_resize(self, mode: str) -> None:
        def op(im: Image.Image, sz: tuple[int, int]) -> Image.Image:
            return im.resize(sz, Image.Resampling.NEAREST)

        self._test_nearest(op, mode)

    @pytest.mark.parametrize("mode", ("RGBA", "LA"))
    def test_nearest_transform(self, mode: str) -> None:
        def op(im: Image.Image, sz: tuple[int, int]) -> Image.Image:
            w, h = im.size
            return im.transform(
                sz, Image.Transform.EXTENT, (0, 0, w, h), Image.Resampling.NEAREST
            )

        self._test_nearest(op, mode)

    def test_blank_fill(self) -> None:
        # attempting to hit
        # https://github.com/python-pillow/Pillow/issues/254 reported
        #
        # issue is that transforms with transparent overflow area
        # contained junk from previous images, especially on systems with
        # constrained memory. So, attempt to fill up memory with a
        # pattern, free it, and then run the mesh test again. Using a 1Mp
        # image with 4 bands, for 4 megs of data allocated, x 64. OMM (64
        # bit 12.04 VM with 512 megs available, this fails with Pillow <
        # a0eaf06cc5f62a6fb6de556989ac1014ff3348ea
        #
        # Running by default, but I'd totally understand not doing it in
        # the future

        pattern: list[Image.Image] | None = [
            Image.new("RGBA", (1024, 1024), (a, a, a, a)) for a in range(1, 65)
        ]

        # Yeah. Watch some JIT optimize this out.
        pattern = None  # noqa: F841

        self.test_mesh()

    def test_missing_method_data(self) -> None:
        with hopper() as im:
            with pytest.raises(ValueError):
                im.transform((100, 100), None)  # type: ignore[arg-type]

    def test_invalid_data(self) -> None:
        with hopper() as im:
            # Test __transformer data
            for data in ([math.nan], [math.inf]):
                with pytest.raises(ValueError, match="invalid data"):
                    im.transform((100, 100), Image.Transform.EXTENT, data)

            # Test __transformer box
            for mesh_data in ([[(0, 0, 0, math.nan), []]], [[(0, 0, 0, math.inf), []]]):
                with pytest.raises(ValueError, match="invalid data"):
                    im.transform((100, 100), Image.Transform.MESH, mesh_data)

    @pytest.mark.parametrize("resample", (Image.Resampling.BOX, "unknown"))
    def test_unknown_resampling_filter(self, resample: Image.Resampling | str) -> None:
        with hopper() as im:
            w, h = im.size
            with pytest.raises(ValueError):
                im.transform((100, 100), Image.Transform.EXTENT, (0, 0, w, h), resample)  # type: ignore[arg-type]


class TestImageTransformAffine:
    transform = Image.Transform.AFFINE

    def _test_image(self) -> Image.Image:
        im = hopper("RGB")
        return im.crop((10, 20, im.width - 10, im.height - 20))

    @pytest.mark.parametrize(
        "deg, transpose",
        (
            (0, None),
            (90, Image.Transpose.ROTATE_90),
            (180, Image.Transpose.ROTATE_180),
            (270, Image.Transpose.ROTATE_270),
        ),
    )
    def test_rotate(self, deg: int, transpose: Image.Transpose | None) -> None:
        im = self._test_image()

        angle = -math.radians(deg)
        matrix = [
            round(math.cos(angle), 15),
            round(math.sin(angle), 15),
            0.0,
            round(-math.sin(angle), 15),
            round(math.cos(angle), 15),
            0.0,
            0,
            0,
        ]
        matrix[2] = (1 - matrix[0] - matrix[1]) * im.width / 2
        matrix[5] = (1 - matrix[3] - matrix[4]) * im.height / 2

        if transpose is not None:
            transposed = im.transpose(transpose)
        else:
            transposed = im

        for resample in [
            Image.Resampling.NEAREST,
            Image.Resampling.BILINEAR,
            Image.Resampling.BICUBIC,
        ]:
            transformed = im.transform(
                transposed.size, self.transform, matrix, resample
            )
            assert_image_equal(transposed, transformed)

    @pytest.mark.parametrize(
        "scale, epsilon_scale",
        (
            (1.1, 6.9),
            (1.5, 5.5),
            (2.0, 5.5),
            (2.3, 3.7),
            (2.5, 3.7),
        ),
    )
    @pytest.mark.parametrize(
        "resample,epsilon",
        (
            (Image.Resampling.NEAREST, 0),
            (Image.Resampling.BILINEAR, 2),
            (Image.Resampling.BICUBIC, 1),
        ),
    )
    def test_resize(
        self,
        scale: float,
        epsilon_scale: float,
        resample: Image.Resampling,
        epsilon: int,
    ) -> None:
        im = self._test_image()

        size_up = int(round(im.width * scale)), int(round(im.height * scale))
        matrix_up = [1 / scale, 0, 0, 0, 1 / scale, 0, 0, 0]
        matrix_down = [scale, 0, 0, 0, scale, 0, 0, 0]

        transformed = im.transform(size_up, self.transform, matrix_up, resample)
        transformed = transformed.transform(
            im.size, self.transform, matrix_down, resample
        )
        assert_image_similar(transformed, im, epsilon * epsilon_scale)

    @pytest.mark.parametrize(
        "x, y, epsilon_scale",
        (
            (0.1, 0, 3.7),
            (0.6, 0, 9.1),
            (50, 50, 0),
        ),
    )
    @pytest.mark.parametrize(
        "resample, epsilon",
        (
            (Image.Resampling.NEAREST, 0),
            (Image.Resampling.BILINEAR, 1.5),
            (Image.Resampling.BICUBIC, 1),
        ),
    )
    def test_translate(
        self,
        x: float,
        y: float,
        epsilon_scale: float,
        resample: Image.Resampling,
        epsilon: float,
    ) -> None:
        im = self._test_image()

        size_up = int(round(im.width + x)), int(round(im.height + y))
        matrix_up = [1, 0, -x, 0, 1, -y, 0, 0]
        matrix_down = [1, 0, x, 0, 1, y, 0, 0]

        transformed = im.transform(size_up, self.transform, matrix_up, resample)
        transformed = transformed.transform(
            im.size, self.transform, matrix_down, resample
        )
        assert_image_similar(transformed, im, epsilon * epsilon_scale)


class TestImageTransformPerspective(TestImageTransformAffine):
    # Repeat all tests for AFFINE transformations with PERSPECTIVE
    transform = Image.Transform.PERSPECTIVE
