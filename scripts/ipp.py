"""Lossless IPP attribute framing; document bytes are never parsed or rewritten."""
import re
import struct
from dataclasses import dataclass
from urllib.parse import urlsplit

MAX_PREFIX = 1024 * 1024
URI_TAG = 0x45
REQUEST_URIS = {b"printer-uri", b"job-uri"}
RESPONSE_URIS = {b"printer-uri", b"printer-uri-supported", b"job-uri", b"job-printer-uri",
                 b"printer-more-info", b"job-more-info", b"notify-printer-uri"}
JOB_PATH = re.compile(r"/jobs/([1-9][0-9]*)\Z")


@dataclass
class Attribute:
    tag: int
    name: bytes
    value: bytes
    extended: bool = False

    def encode(self):
        if len(self.value) > 65535:
            raise ValueError("IPP value exceeds 65535 bytes")
        tag = b"\x7f" + struct.pack(">I", self.tag) if self.extended else bytes([self.tag])
        return tag + struct.pack(">H", len(self.name)) + self.name + struct.pack(">H", len(self.value)) + self.value


def decode_prefix(data):
    if len(data) < 9 or len(data) > MAX_PREFIX:
        raise ValueError("Invalid IPP attribute header size")
    records, offset = [], 8
    while offset < len(data):
        tag = data[offset]
        offset += 1
        if tag < 0x10:
            if tag == 0:
                raise ValueError("Invalid IPP delimiter")
            records.append(tag)
            if tag == 3:
                if offset != len(data):
                    raise ValueError("Document bytes passed to attribute decoder")
                return data[:8], records
            continue
        try:
            extended = tag == 0x7F
            if extended:
                tag = struct.unpack_from(">I", data, offset)[0]
                offset += 4
            length = struct.unpack_from(">H", data, offset)[0]
            offset += 2
            name = data[offset:offset + length]
            offset += length
            if len(name) != length:
                raise ValueError("Truncated IPP attribute name")
            length = struct.unpack_from(">H", data, offset)[0]
            offset += 2
            value = data[offset:offset + length]
            offset += length
            if len(value) != length:
                raise ValueError("Truncated IPP attribute value")
        except struct.error as error:
            raise ValueError("Truncated IPP attribute") from error
        records.append(Attribute(tag, name, value, extended))
    raise ValueError("Missing end-of-attributes")


async def read_prefix(stream):
    """Read exactly through end-of-attributes, leaving the document in stream."""
    data = bytearray(await stream.readexactly(8))

    async def read(size):
        if len(data) + size > MAX_PREFIX:
            raise ValueError("IPP attributes exceed 1 MiB")
        chunk = await stream.readexactly(size)
        data.extend(chunk)
        return chunk

    while True:
        tag = (await read(1))[0]
        if tag < 0x10:
            if tag == 3:
                return bytes(data)
            if tag == 0:
                raise ValueError("Invalid IPP delimiter")
            continue
        if tag == 0x7F:
            await read(4)
        await read(struct.unpack(">H", await read(2))[0])
        await read(struct.unpack(">H", await read(2))[0])


def attributes(records):
    """Yield effective names, handling multi-values and nested collections."""
    name, stack = b"", []
    for record in records:
        if isinstance(record, int):
            if stack:
                raise ValueError("Unclosed IPP collection")
            name = b""
            continue
        if record.name:
            name = record.name
        if record.tag == 0x4A:  # memberAttrName
            name = record.value
        yield name, record, len(stack)
        if record.tag == 0x34:  # beginCollection
            if len(stack) >= 32:
                raise ValueError("IPP collection nesting exceeds 32")
            stack.append(name)
            name = b""
        elif record.tag == 0x37:  # endCollection
            if not stack:
                raise ValueError("Unexpected endCollection")
            name = stack.pop()


def encode_prefix(header, records):
    return header + b"".join(bytes([r]) if isinstance(r, int) else r.encode() for r in records)


def rewrite_request(prefix, queue_path):
    header, records = decode_prefix(prefix)
    path = queue_path
    for name, attr, depth in attributes(records):
        if depth or attr.tag != URI_TAG or name not in REQUEST_URIS:
            continue
        uri = urlsplit(attr.value.decode("utf-8"))
        if uri.scheme not in ("ipp", "ipps") or not uri.netloc:
            raise ValueError("Invalid IPP destination URI")
        if name == b"job-uri":
            if not JOB_PATH.fullmatch(uri.path) or uri.query or uri.fragment:
                raise ValueError("Invalid job URI")
            path = uri.path
            attr.value = f"ipp://localhost:631{path}".encode()
        else:
            attr.value = f"ipp://localhost:631{queue_path}".encode()
    return encode_prefix(header, records), path


def rewrite_response(prefix, authority, queue_path):
    header, records = decode_prefix(prefix)
    for name, attr, depth in attributes(records):
        if depth or attr.tag != URI_TAG or name not in RESPONSE_URIS:
            continue
        uri = urlsplit(attr.value.decode("utf-8"))
        if uri.scheme not in ("ipp", "ipps", "http", "https"):
            continue
        if uri.path == queue_path:
            path = "/"
        elif JOB_PATH.fullmatch(uri.path):
            path = uri.path
        else:
            continue
        scheme = "http" if name in (b"printer-more-info", b"job-more-info") else "ipp"
        attr.value = f"{scheme}://{authority}{path}".encode()
    return encode_prefix(header, records)


def ipp_error(header, status, message):
    return (header[:2] + struct.pack(">H", status) + header[4:8] + b"\x01" +
            Attribute(0x47, b"attributes-charset", b"utf-8").encode() +
            Attribute(0x48, b"attributes-natural-language", b"en").encode() +
            Attribute(0x41, b"status-message", message.encode()).encode() + b"\x03")


def printer_attributes_request(uri, request_id=1):
    return (b"\x02\x00\x00\x0b" + struct.pack(">I", request_id) + b"\x01" +
            Attribute(0x47, b"attributes-charset", b"utf-8").encode() +
            Attribute(0x48, b"attributes-natural-language", b"en").encode() +
            Attribute(URI_TAG, b"printer-uri", uri.encode()).encode() + b"\x03")
