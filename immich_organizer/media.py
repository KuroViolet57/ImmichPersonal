"""Small picture helpers used by Search+ (shrinking images, frames of animated GIF/WebP/PNG)."""

from __future__ import annotations

ANIMATED_EXT = (".gif", ".webp", ".png", ".apng")


def shrink_image(data: bytes, max_side: int) -> bytes:
    """Scale an image down so its longest side is ``max_side`` (fewer tokens, faster)."""
    if not max_side:
        return data
    try:
        import io

        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            if max(im.size) <= max_side:
                return data
            im = im.convert("RGB")
            im.thumbnail((max_side, max_side))
            out = io.BytesIO()
            im.save(out, "JPEG", quality=88)
            return out.getvalue()
    except Exception:  # noqa: BLE001 - Pillow missing or odd file: send it as is
        return data


def animation_frames(path: str, n: int, positions: list[float] | None = None) -> list[bytes]:
    """Up to n frames spread over an animated GIF/WebP/PNG, as JPEGs (one frame = not animated).

    ``positions`` (fractions 0-1 of the animation's length) picks the frames instead of spreading ``n`` evenly.
    """
    try:
        import io

        from PIL import Image
        with Image.open(path) as im:
            total = int(getattr(im, "n_frames", 1) or 1)
            if total < 2:
                return []
            out = []
            spots = positions if positions else [(k + 0.5) / n for k in range(n)]
            for i in sorted({int(total * p) for p in spots}):
                im.seek(min(i, total - 1))
                frame = im.convert("RGBA")
                bg = Image.new("RGBA", frame.size, (255, 255, 255, 255))
                bg.alpha_composite(frame)
                rgb = bg.convert("RGB")
                rgb.thumbnail((1024, 1024))
                buf = io.BytesIO()
                rgb.save(buf, "JPEG", quality=90)
                out.append(buf.getvalue())
            return out
    except Exception:  # noqa: BLE001 - odd file: fall back to the preview
        return []
