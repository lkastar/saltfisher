"""Listing photos, downloaded and base64'd for a vision model.

There was nothing to reuse: no server-side image fetch exists anywhere in
`app/`, and the browser collector deliberately BLOCKS image loads
(`collector/browser.py:109`). This module is written from scratch, and every
guard in it is a measured fact rather than defensive habit:

**The goofish CDN answers a missing file with HTTP 200 and a 1x1 pixel.** A
downloader that trusts the status code base64s a blank pixel, ships it to the
model and bills the user for reading nothing. The frontend already learned
this (`web/src/components/RemoteImage.tsx:14-17` checks `naturalWidth`), so
the check here is a port of that one and not a second opinion: the dimensions
are parsed out of the bytes and a side below `MIN_SIDE` counts as missing.

**325 of 327 listings hold exactly one image URL, and it is the cover** —
`collector/base.py:291` back-fills `image_urls = [cover_url]` for search rows,
which carry no gallery. `MAX_IMAGES` is 3 as the design asks, but on today's
data that means one thumbnail. That is the data, not a broken cap.

The httpx client is a parameter, like everywhere else in `llm/`: the caller
passes `app.state.notify_client`. `MtopClient`'s client carries the goofish
cookie jar and must never reach a third-party URL.
"""

import logging
import struct
from base64 import b64encode
from dataclasses import dataclass

import httpx

from app.llm.base import LlmImage, media_type

log = logging.getLogger(__name__)

# Design §6. One per listing is what the real data holds; see the docstring.
MAX_IMAGES = 3
# A listing photo off this CDN is tens of KB. The cap exists so one absurd
# response cannot inflate a request body (and its bill) by an order of
# magnitude — base64 adds a third on top of whatever arrives.
MAX_BYTES = 3 * 1024 * 1024
# Shorter than the chat timeout on purpose: this runs before the completion,
# so a stalled CDN would otherwise delay an answer the user can still have
# without pictures.
TIMEOUT_SECONDS = 15.0
# The port of RemoteImage.tsx's `naturalWidth <= 2`, i.e. a side of 3 is the
# smallest accepted. Kept at the frontend's threshold rather than the design's
# "any side <= 1" so both surfaces agree on what "missing" means: a real
# listing thumbnail is never 2px, and one surface calling a 2x2 response an
# image while the other calls it a hole is how a panel ends up showing a photo
# the model was told did not exist.
MIN_SIDE = 3

# Substrings that mean "this model reads images". A heuristic, and it has to
# be one: neither wire format exposes a capability lookup, `GET /models`
# returns ids and nothing else, and the only certain test is sending an image
# and having it rejected — after paying to upload it.
#
# Measured on the configured endpoint: `deepseek-v4-flash-vision-exp` accepts
# images, `deepseek-v4-flash` does not.
#
# ponytail: a substring list, not a capability table. The upgrade path is a
# `vision` flag on LlmScenarioConfig that the user ticks, which is a schema
# change and belongs with the config page. Being wrong is not silent either
# way: guessing "no vision" degrades to text and says so in the result, and
# guessing "vision" surfaces the endpoint's own rejection reason.
VISION_HINTS = ("vision", "-vl", "vl-", "gpt-4o", "omni", "multimodal", "claude-3", "gemini")


@dataclass(frozen=True, slots=True)
class ImageBatch:
    """What was downloaded, and what was not.

    `notes` are user-facing Chinese sentences the analyze route returns. A
    skipped photo has to be visible: "the model never mentioned the scratch"
    reads very differently once you know it never got the picture.
    """

    images: tuple[LlmImage, ...] = ()
    notes: tuple[str, ...] = ()


def supports_vision(model: str) -> bool:
    """Whether `model`'s name claims image input. See VISION_HINTS."""
    name = model.lower()
    return any(hint in name for hint in VISION_HINTS)


def dimensions(data: bytes) -> tuple[int, int] | None:
    """(width, height) read out of the container header.

    None means "not a container this function knows", which the caller treats
    as a skip: sending bytes we cannot even measure is how the 1x1 sentinel
    gets through in a format nobody expected.

    **WEBP is the format this CDN actually serves, and the URLs do not say
    so.** Measured 2026-09-05 against the five photos of item
    1079150111050: every `...xy_item.jpg` came back `Content-Type: image/webp`
    with a `RIFF/WEBP/VP8X` body, on a request that sent no
    `Accept: image/webp` at all. A parser that trusted the `.jpg` in the URL —
    or covered only jpeg — would measure nothing, skip every photo and degrade
    100% of real listings to text while reporting it as an unrecognised
    format. Which is exactly the shape of bug this whole module is guarding
    against, one level up.

    JPEG stays because the CDN can serve it (it does for older paths), GIF is
    two lines, and PNG is what the tests build with zlib + struct so their
    dimensions are known by construction.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
        return int(width), int(height)
    if data[:6] in (b"GIF87a", b"GIF89a"):
        width, height = struct.unpack("<HH", data[6:10])
        return int(width), int(height)
    if data.startswith(b"\xff\xd8"):
        return _jpeg_dimensions(data)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _webp_dimensions(data)
    return None


async def fetch(
    client: httpx.AsyncClient, urls: list[str], *, limit: int = MAX_IMAGES
) -> ImageBatch:
    """Download up to `limit` images. Never raises; a failure becomes a note.

    Never raises because a photo is an optional input: a CDN hiccup must cost
    the pictures, not the advice. Every drop is reported in `notes` so the
    user is told which inputs the answer was actually based on.
    """
    images: list[LlmImage] = []
    notes: list[str] = []
    for url in urls[:limit]:
        image, note = await _one(client, url)
        if image is not None:
            images.append(image)
        if note is not None:
            notes.append(note)
    log.info("llm images fetched", extra={"wanted": len(urls[:limit]), "sent": len(images)})
    return ImageBatch(images=tuple(images), notes=tuple(notes))


async def _one(client: httpx.AsyncClient, url: str) -> tuple[LlmImage | None, str | None]:
    """One download, classified. Returns (image, note); at most one is set."""
    try:
        # No referer, deliberately: `spec/backend/index.md` measured that this
        # CDN serves fine without one, and not sending it keeps our panel's
        # URLs out of Alibaba's logs — the same reason RemoteImage.tsx sets
        # referrerPolicy="no-referrer".
        response = await client.get(url, timeout=TIMEOUT_SECONDS)
    except httpx.HTTPError:
        # str(exc) stays out: httpx puts the URL in it, and these notes are
        # user-facing text (`llm/base.py` drops it for the same reason).
        return None, "有 1 张图片下载失败（网络或超时），本次分析没有用到它。"

    if response.status_code != 200:
        return None, f"有 1 张图片下载失败（HTTP {response.status_code}），本次分析没有用到它。"

    data = response.content
    if len(data) > MAX_BYTES:
        # ponytail: the body is already in memory by the time it is measured.
        # These URLs come from our own collector, so the ceiling is a cost cap
        # rather than a defence; switch to client.stream() the day a
        # user-supplied URL can reach here.
        return None, (
            f"有 1 张图片超过 {MAX_BYTES // (1024 * 1024)} MB 的单张上限，已跳过，"
            "本次分析没有用到它。"
        )

    size = dimensions(data)
    if size is None:
        return None, "有 1 张图片的格式无法识别，已跳过，本次分析没有用到它。"
    width, height = size
    if width < MIN_SIDE or height < MIN_SIDE:
        # The measured trap. HTTP 200 with a 1x1 body is this CDN's way of
        # saying "no such file"; treating it as a photo sends the model a
        # blank pixel and charges for the reading.
        return None, (
            f"有 1 张图片只有 {width}×{height} 像素——这个 CDN 用 HTTP 200 + 1×1 占位图"
            "表示文件不存在，已当作缺失跳过。"
        )

    b64 = b64encode(data).decode()
    return LlmImage(media_type=_mime(response, b64), b64=b64), None


def _mime(response: httpx.Response, b64: str) -> str:
    """The Content-Type the download already knew, or a sniff as fallback.

    Preferring the header is the whole reason `LlmImage` carries a media type:
    the downloader is the only place that ever sees it, and throwing it away
    to guess later from magic bytes replaces a fact with a guess.
    """
    header = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
    return header if header.startswith("image/") else media_type(b64)


def _webp_dimensions(data: bytes) -> tuple[int, int] | None:
    """Canvas size from the first RIFF chunk.

    All three variants, because the CDN picks one and does not tell us which:
    the photos measured on 2026-09-05 were `VP8X`, and a `VP8 ` or `VP8L`
    answer tomorrow must not silently turn every listing back into "format
    unrecognised". Each is a handful of bytes at a fixed offset.
    """
    fourcc, payload = data[12:16], data[20:]
    if fourcc == b"VP8X" and len(payload) >= 10:
        # Flags byte, 3 reserved bytes, then canvas width-1 and height-1 as
        # 24-bit little-endian.
        width = int.from_bytes(payload[4:7], "little") + 1
        height = int.from_bytes(payload[7:10], "little") + 1
        return width, height
    if fourcc == b"VP8 " and payload[3:6] == b"\x9d\x01\x2a":
        # Key-frame header: 3-byte frame tag, the sync code above, then two
        # 14-bit dimensions each packed into a little-endian u16.
        width, height = struct.unpack("<HH", payload[6:10])
        return width & 0x3FFF, height & 0x3FFF
    if fourcc == b"VP8L" and payload[:1] == b"\x2f":
        bits = int.from_bytes(payload[1:5], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """Walk the JPEG marker chain to the frame header.

    Written out rather than pulled from a library because the alternative is
    Pillow, and `quality-guidelines.md` weighs a new dependency against what
    it replaces — here, fifteen lines that only have to answer "how many
    pixels wide".
    """
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1  # Padding or a truncated segment: resync on the next 0xFF.
            continue
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1  # Fill byte; the real marker is the next one.
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD9:
            i += 2  # Standalone markers carry no length field.
            continue
        length = struct.unpack(">H", data[i + 2 : i + 4])[0]
        # SOF0..SOF15, minus the three that are not frame headers (DHT, JPG,
        # DAC). Every SOF spells the size the same way.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return int(width), int(height)
        if length < 2:
            return None  # Malformed: a zero-length segment would loop forever.
        i += 2 + length
    return None
