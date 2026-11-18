"""Magic-byte detection for the six allowed types (docs/spec.md section 6.2).

Checked on the first min(size, 4096) bytes. No libmagic, and no image decoding on the server.
"""

from __future__ import annotations

HEAD_BYTES = 4096

MP4_BRANDS = {
    b"isom",
    b"iso2",
    b"iso4",
    b"iso5",
    b"iso6",
    b"mp41",
    b"mp42",
    b"avc1",
    b"dash",
    b"M4V ",
}
EBML_MAGIC = b"\x1a\x45\xdf\xa3"
DOCTYPE_ID = b"\x42\x82"


def _ftyp_brands(head: bytes) -> list[bytes] | None:
    if len(head) < 16 or head[4:8] != b"ftyp":
        return None
    size = int.from_bytes(head[0:4], "big")
    end = min(size if size >= 16 else len(head), len(head))
    brands = [head[8:12]]
    brands.extend(head[offset : offset + 4] for offset in range(16, end - 3, 4))
    return brands


def _read_vint(data: bytes, offset: int) -> tuple[int, int] | None:
    """Read an EBML variable-length integer; returns (value, length) or None. Bounded to 8."""

    if offset >= len(data):
        return None
    first = data[offset]
    for length in range(1, 9):
        if first & (0x80 >> (length - 1)):
            if offset + length > len(data):
                return None
            value = first & ((0x80 >> (length - 1)) - 1)
            for byte in data[offset + 1 : offset + length]:
                value = (value << 8) | byte
            return value, length
    return None


def _webm_doctype(head: bytes) -> bytes | None:
    if not head.startswith(EBML_MAGIC):
        return None
    header = _read_vint(head, 4)
    if header is None:
        return None
    header_size, size_length = header
    position = 4 + size_length
    end = min(position + header_size, len(head))
    while position + 2 < end:
        if head[position : position + 2] == DOCTYPE_ID:
            length = _read_vint(head, position + 2)
            if length is None:
                return None
            value_length, vint_length = length
            start = position + 2 + vint_length
            return head[start : start + value_length]
        position += 1
    return None


def _quicktime(head: bytes) -> bool:
    brands = _ftyp_brands(head)
    if brands is not None:
        return brands[0] == b"qt  "
    if len(head) < 8:
        return False
    first_atom = head[4:8]
    if first_atom == b"moov":
        return True
    if first_atom == b"wide":
        # `wide` must be immediately followed by mdat or moov; a bare free/mdat start is refused.
        size = int.from_bytes(head[0:4], "big") or 8
        return head[size + 4 : size + 8] in (b"mdat", b"moov")
    return False


def detect(head: bytes) -> str | None:
    """Return the detected content type, or None when the bytes match no allowed type."""

    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(head) >= 12 and head[0:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    doctype = _webm_doctype(head)
    if doctype is not None:
        return "video/webm" if doctype == b"webm" else None
    brands = _ftyp_brands(head)
    if brands is not None and brands[0] != b"qt  ":
        return "video/mp4" if any(brand in MP4_BRANDS for brand in brands) else None
    if _quicktime(head):
        return "video/quicktime"
    return None
