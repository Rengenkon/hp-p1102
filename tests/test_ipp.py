"""Protocol regressions: URI lengths, repeated values, collections and document framing."""
import asyncio
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from ipp import Attribute, attributes, decode_prefix, encode_prefix, read_prefix, rewrite_request, rewrite_response

QUEUE = "/printers/HP_LaserJet_Professional_P1102"
HEADER = b"\x02\x00\x00\x02\x00\x00\x00\x7b"


class IPPTests(unittest.TestCase):
    def test_repeated_uris_and_nested_collections_are_lossless(self):
        records = [1, Attribute(0x45, b"printer-uri", b"ipp://server:8631/"), 2,
                   Attribute(0x34, b"media-col", b""),
                   Attribute(0x4A, b"", b"printer-uri"),
                   Attribute(0x45, b"", b"ipp://unrelated/nested"),
                   Attribute(0x37, b"", b""),
                   Attribute(0x45, b"document-uri", b"http://documents/a.pdf"), 3]
        original = encode_prefix(HEADER, records)
        rewritten, path = rewrite_request(original, QUEUE)
        self.assertEqual(path, QUEUE)
        header, parsed = decode_prefix(rewritten)
        self.assertEqual(header, HEADER)
        values = [r.value for r in parsed if isinstance(r, Attribute) and r.tag == 0x45]
        self.assertEqual(values, [f"ipp://localhost:631{QUEUE}".encode(), b"ipp://unrelated/nested", b"http://documents/a.pdf"])

    def test_repeated_response_values_and_job_links(self):
        prefix = encode_prefix(HEADER, [1, 4,
            Attribute(0x45, b"printer-uri-supported", f"ipp://internal:631{QUEUE}".encode()),
            Attribute(0x45, b"", f"ipps://internal:631{QUEUE}".encode()),
            Attribute(0x45, b"job-uri", b"ipp://internal:631/jobs/42"),
            Attribute(0x45, b"job-more-info", b"http://internal:631/jobs/42"),
            Attribute(0x45, b"unrelated-uri", b"http://documents/jobs/42"), 3])
        result = rewrite_response(prefix, "[::1]:8631", QUEUE)
        _, records = decode_prefix(result)
        values = [r.value for r in records if isinstance(r, Attribute)]
        self.assertEqual(values, [b"ipp://[::1]:8631/", b"ipp://[::1]:8631/", b"ipp://[::1]:8631/jobs/42",
                                  b"http://[::1]:8631/jobs/42", b"http://documents/jobs/42"])

    def test_job_uri_preserves_identifier(self):
        prefix = encode_prefix(HEADER, [1, Attribute(0x45, b"job-uri", b"ipp://server:8631/jobs/42"), 3])
        result, path = rewrite_request(prefix, QUEUE)
        self.assertEqual(path, "/jobs/42")
        self.assertIn(b"ipp://localhost:631/jobs/42", result)

    def test_truncated_attributes_are_rejected(self):
        with self.assertRaises(ValueError):
            decode_prefix(HEADER + b"\x01\x45\x00\xffbroken")

    def test_extended_unknown_value_tag_survives_rewrite(self):
        prefix = encode_prefix(HEADER, [1, Attribute(0x45, b"printer-uri", b"ipp://server:8631/"),
                                      Attribute(0x12345678, b"vendor-extension", b"\0\xff\x03opaque", extended=True), 3])
        result, _ = rewrite_request(prefix, QUEUE)
        header, records = decode_prefix(result)
        self.assertEqual(records[2], Attribute(0x12345678, b"vendor-extension", b"\0\xff\x03opaque", extended=True))
        self.assertEqual(encode_prefix(header, records), result)

    def test_stream_framing_does_not_consume_or_rewrite_document(self):
        async def check():
            prefix = encode_prefix(HEADER, [1, Attribute(0x45, b"printer-uri", b"ipp://server:8631/"),
                                          Attribute(0x12345678, b"vendor-extension", b"\x03opaque", extended=True), 3])
            document = bytes(range(256)) * 10 + prefix
            stream = asyncio.StreamReader()
            stream.feed_data(prefix + document)
            stream.feed_eof()
            self.assertEqual(await read_prefix(stream), prefix)
            self.assertEqual(await stream.read(), document)
        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
