"""Build a 2x2 album-art mosaic as a playlist cover (JPEG under Spotify's 256 KB limit)."""

from __future__ import annotations

import io

import httpx
from PIL import Image

SIZE = 640
MAX_BYTES = 190_000  # base64 adds ~33%, Spotify caps the upload at 256 KB


def _fetch(url: str, http: httpx.Client) -> Image.Image | None:
    try:
        r = http.get(url, timeout=15)
        r.raise_for_status()
        return Image.open(io.BytesIO(r.content)).convert("RGB")
    except Exception:
        return None


def make_cover(image_urls: list[str], http: httpx.Client) -> bytes | None:
    seen: list[str] = []
    for u in image_urls:
        if u and u not in seen:
            seen.append(u)
    images = [img for img in (_fetch(u, http) for u in seen[:8]) if img is not None]
    if not images:
        return None
    canvas = Image.new("RGB", (SIZE, SIZE))
    if len(images) < 4:
        canvas.paste(images[0].resize((SIZE, SIZE)))
    else:
        half = SIZE // 2
        for i, img in enumerate(images[:4]):
            canvas.paste(img.resize((half, half)), ((i % 2) * half, (i // 2) * half))
    for quality in (88, 80, 70, 60, 50, 40):
        buf = io.BytesIO()
        canvas.save(buf, "JPEG", quality=quality, optimize=True)
        if buf.tell() <= MAX_BYTES:
            return buf.getvalue()
    return None
