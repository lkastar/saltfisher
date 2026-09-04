"""Image download, the 1x1 sentinel, the size cap, and vision detection.

No network, image fetches included: every download goes through
`httpx.MockTransport`. A test that pulls a real photo off the goofish CDN is a
flake that also tells Alibaba when the suite runs.

The test images are BUILT HERE with zlib + struct rather than committed as
binary fixtures, which is how the vision probe in
`research/llm-endpoint-probe.md` was made checkable: a real PNG whose
dimensions are known by construction, and a real 1x1 that is a valid image
rather than a truncated file. A committed .jpg would test that the CDN's
format is still the CDN's format, which is not the thing that breaks.
"""

import struct
import zlib

import httpx
import pytest

from app.llm.images import (
    MAX_BYTES,
    dimensions,
    fetch,
    supports_vision,
)

pytestmark = pytest.mark.asyncio

URL = "http://img.alicdn.com/bao/uploaded/one.jpg"


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))


def png(width: int, height: int) -> bytes:
    """A real, decodable RGB PNG of the given size."""
    raw = b"".join(b"\x00" + bytes([220, 30, 30] * width) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


def jpeg(width: int, height: int) -> bytes:
    """A JPEG header chain down to SOF0, which is all the parser reads.

    Byte-real as far as the marker walk goes: an APP0 segment it has to skip
    by length, then the frame header. No entropy-coded data, because nothing
    here decodes pixels.
    """
    app0 = b"JFIF\x00" + b"\x01\x01" + b"\x00" + b"\x00\x01\x00\x01" + b"\x00\x00"
    sof0 = b"\x08" + struct.pack(">HH", height, width) + b"\x01" + b"\x01\x11\x00"
    return (
        b"\xff\xd8"
        + b"\xff\xe0"
        + struct.pack(">H", len(app0) + 2)
        + app0
        + b"\xff\xc0"
        + struct.pack(">H", len(sof0) + 2)
        + sof0
        + b"\xff\xd9"
    )


def webp_vp8x(width: int, height: int) -> bytes:
    """A WEBP/VP8X header, which is what the goofish CDN actually serves.

    Byte-shaped after the real thing: measured on item 1079150111050's five
    photos, `RIFF....WEBPVP8X` with a 10-byte chunk carrying canvas width-1
    and height-1 as 24-bit little-endian.
    """
    chunk = b"\x20\x00\x00\x00" + (width - 1).to_bytes(3, "little")
    chunk += (height - 1).to_bytes(3, "little")
    body = b"WEBP" + b"VP8X" + struct.pack("<I", len(chunk)) + chunk
    return b"RIFF" + struct.pack("<I", len(body)) + body


def serving(*responses: httpx.Response) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    """A CDN that answers each request in turn, recording what it was asked."""
    seen: list[httpx.Request] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


def image_response(data: bytes, content_type: str = "image/jpeg") -> httpx.Response:
    return httpx.Response(200, content=data, headers={"content-type": content_type})


# --------------------------------------------------------------------------- #
# The measured trap: HTTP 200 + a 1x1 pixel means "no such file"
# --------------------------------------------------------------------------- #


async def test_a_1x1_pixel_at_http_200_is_treated_as_missing():
    """This CDN answers a missing file with 200 and a 1x1 pixel, so `onError`
    never fires in the browser and a status-code check never fires here.
    Sending it means paying a vision model to look at a blank dot.
    """
    client, _ = serving(image_response(png(1, 1), "image/png"))
    batch = await fetch(client, [URL])

    assert batch.images == ()
    assert "1×1" in batch.notes[0]


async def test_the_cdns_own_placeholder_body_is_rejected():
    """The measured sentinel, byte for byte at the header.

    Fetched 2026-09-05 with one character changed in a real photo path: the
    CDN answered a 49-byte `image/gif` whose first bytes are exactly these — a
    1x1 GIF. It came back as 404 that time and the status check would have
    caught it, but the frontend measured the same body at HTTP 200
    (`RemoteImage.tsx:14-17`), which is the case only the dimensions catch.
    """
    placeholder = b"GIF89a\x01\x00\x01\x00\x91\x00\x00\x00" + b"\xff" * 33
    assert dimensions(placeholder) == (1, 1)

    client, _ = serving(image_response(placeholder, "image/gif"))
    batch = await fetch(client, [URL])
    assert batch.images == ()
    assert "1×1" in batch.notes[0]


async def test_a_2x2_pixel_is_also_missing():
    """RemoteImage.tsx rejects anything <= 2px and the server agrees, so a
    panel cannot show a photo the model was told did not exist."""
    client, _ = serving(image_response(png(2, 2), "image/png"))
    assert (await fetch(client, [URL])).images == ()


async def test_a_real_photo_goes_through_with_its_own_media_type():
    client, _ = serving(image_response(png(64, 64), "image/png"))
    batch = await fetch(client, [URL])

    assert batch.notes == ()
    assert len(batch.images) == 1
    assert batch.images[0].media_type == "image/png"
    # Round-trips: what is sent is the bytes that arrived, not a re-encode.
    assert batch.images[0].b64.startswith("iVBORw0K")


async def test_dimensions_reads_every_container_the_cdn_can_answer_with():
    """WEBP is not optional here and is the reason this test exists.

    Measured 2026-09-05: every `...xy_item.jpg` photo of item
    1079150111050 came back `image/webp` (VP8X), on a request that sent no
    `Accept: image/webp`. A jpeg-only parser measures nothing, skips every
    photo and degrades 100% of real listings to text — while calling it an
    unrecognised format.
    """
    assert dimensions(webp_vp8x(1920, 864)) == (1920, 864)
    assert dimensions(png(64, 32)) == (64, 32)
    assert dimensions(jpeg(800, 600)) == (800, 600)
    assert dimensions(b"GIF89a" + struct.pack("<HH", 48, 24) + b"\x00" * 10) == (48, 24)


async def test_a_webp_photo_is_sent_with_the_cdns_own_label():
    client, _ = serving(image_response(webp_vp8x(800, 600), "image/webp"))
    batch = await fetch(client, [URL])

    assert batch.notes == ()
    assert batch.images[0].media_type == "image/webp"


async def test_a_1x1_webp_is_missing_too():
    """The sentinel check has to survive the format switch: it reads the
    parsed dimensions, not the container."""
    client, _ = serving(image_response(webp_vp8x(1, 1), "image/webp"))
    assert (await fetch(client, [URL])).images == ()


async def test_an_unmeasurable_body_is_skipped_rather_than_sent():
    """An HTML error page served as 200, or a container this cannot measure.
    Sending it blind is how the 1x1 sentinel gets through in another format.
    """
    client, _ = serving(image_response(b"<html>not found</html>", "text/html"))
    batch = await fetch(client, [URL])

    assert batch.images == ()
    assert "格式无法识别" in batch.notes[0]


# --------------------------------------------------------------------------- #
# Caps and failures, all of which have to be said out loud
# --------------------------------------------------------------------------- #


async def test_an_oversized_image_is_skipped_and_reported():
    client, _ = serving(image_response(b"\x89PNG\r\n\x1a\n" + b"\x00" * (MAX_BYTES + 1)))
    batch = await fetch(client, [URL])

    assert batch.images == ()
    assert "上限" in batch.notes[0]


async def test_a_failed_download_costs_the_photo_not_the_advice():
    client, _ = serving(httpx.Response(404))
    batch = await fetch(client, [URL])

    assert batch.images == ()
    assert "HTTP 404" in batch.notes[0]


async def test_a_transport_error_is_a_note_not_an_exception():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    batch = await fetch(client, [URL])

    assert batch.images == ()
    assert "下载失败" in batch.notes[0]
    # The URL must not travel into a user-facing note: httpx puts it in
    # str(exc) and a base_url can carry credentials.
    assert URL not in batch.notes[0]


async def test_at_most_three_images_are_downloaded():
    """The cap is 3 per design §6. In the real data 325 of 327 listings hold
    exactly one URL, so this ceiling is never reached today — the test pins
    the cap, not an expectation of galleries.
    """
    client, seen = serving(image_response(png(40, 40), "image/png"))
    batch = await fetch(client, [f"{URL}?{i}" for i in range(5)])

    assert len(seen) == 3
    assert len(batch.images) == 3


async def test_no_referer_is_sent():
    """Measured in `spec/backend/index.md`: this CDN serves fine without one,
    and sending it would leak our URLs to Alibaba (RemoteImage.tsx sets
    referrerPolicy="no-referrer" for the same reason)."""
    client, seen = serving(image_response(png(40, 40), "image/png"))
    await fetch(client, [URL])

    assert "referer" not in {name.lower() for name in seen[0].headers}


async def test_a_useless_content_type_falls_back_to_sniffing():
    """A gateway or a CDN that answers `application/octet-stream` must not
    make us label a PNG as one: the label is what the endpoint validates."""
    client, _ = serving(image_response(png(40, 40), "application/octet-stream"))
    batch = await fetch(client, [URL])

    assert batch.images[0].media_type == "image/png"


# --------------------------------------------------------------------------- #
# Vision detection
# --------------------------------------------------------------------------- #


async def test_vision_detection_matches_the_measured_pair():
    """Measured on the configured endpoint: the vision model accepts images,
    the plain one does not."""
    assert supports_vision("deepseek-v4-flash-vision-exp") is True
    assert supports_vision("deepseek-v4-flash") is False
    assert supports_vision("deepseek-v4-pro") is False
