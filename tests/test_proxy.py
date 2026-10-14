"""HTTP/IPP streaming integration against a deterministic fake CUPS service."""
import hashlib
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from ipp import Attribute, attributes, decode_prefix, encode_prefix, read_prefix

try:
    from aiohttp import ClientSession, web
    from aiohttp.test_utils import TestServer
    from proxy import create_app
except ImportError:
    TestServer = None

ROOT = Path(__file__).resolve().parents[1]
QUEUE = "/printers/HP_LaserJet_Professional_P1102"


@unittest.skipIf(TestServer is None, "Install requirements-proxy.lock into .venv to test the proxy")
class ProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.received = []
        async def backend(request):
            prefix = await read_prefix(request.content)
            header, records = decode_prefix(prefix)
            digest = hashlib.sha256()
            async for chunk in request.content.iter_chunked(65536):
                digest.update(chunk)
            self.received.append((request.path, records, digest.hexdigest(), request.host))
            reply = encode_prefix(header[:2] + b"\0\0" + header[4:], [1, 2,
                Attribute(0x45, b"printer-uri-supported", f"ipp://internal:631{QUEUE}".encode()),
                Attribute(0x45, b"job-uri", b"ipp://internal:631/jobs/42"), 3])
            return web.Response(body=reply, content_type="application/ipp")
        app = web.Application(client_max_size=0)
        app.router.add_post("/{path:.*}", backend)
        self.backend = TestServer(app)
        await self.backend.start_server()
        self.addAsyncCleanup(self.backend.close)
        self.proxy = TestServer(create_app(ROOT / "printer.toml", str(self.backend.make_url(""))))
        await self.proxy.start_server()
        self.addAsyncCleanup(self.proxy.close)
        self.client = ClientSession()
        self.addAsyncCleanup(self.client.close)

    def message(self, operation, uri, attribute=b"printer-uri"):
        header = struct.pack(">BBHI", 2, 0, operation, 123)
        return encode_prefix(header, [1, Attribute(0x47, b"attributes-charset", b"utf-8"),
                                      Attribute(0x48, b"attributes-natural-language", b"en"),
                                      Attribute(0x45, attribute, uri.encode()), 3])

    async def test_chunked_print_document_is_preserved_and_reply_uses_public_uris(self):
        document = bytes(range(256)) * 16384 + b"ipp://internal:631/jobs/42"
        prefix = self.message(2, "ipp://client:8631/")
        async def upload():
            for i in range(0, len(prefix), 3):
                yield prefix[i:i + 3]
            for i in range(0, len(document), 32768):
                yield document[i:i + 32768]
        async with self.client.post(self.proxy.make_url("/"), data=upload(), chunked=True,
                                    headers={"Content-Type": "application/ipp"}) as response:
            self.assertEqual(response.status, 200, await response.text() if response.status != 200 else "")
            header, records = decode_prefix(await response.read())
        self.assertEqual(header[4:], struct.pack(">I", 123))
        path, request_records, digest, host = self.received[0]
        self.assertEqual(path, QUEUE)
        self.assertEqual(digest, hashlib.sha256(document).hexdigest())
        self.assertEqual(host, self.backend.make_url("/").authority)
        self.assertIn(f"ipp://localhost:631{QUEUE}".encode(), [r.value for _, r, _ in attributes(request_records)])
        host = self.proxy.make_url("/").authority
        values = [r.value for _, r, _ in attributes(records)]
        self.assertIn(f"ipp://{host}/".encode(), values)
        self.assertIn(f"ipp://{host}/jobs/42".encode(), values)

    async def test_job_status_and_cancellation_use_job_path(self):
        for operation in (9, 8):
            async with self.client.post(self.proxy.make_url("/jobs/42"),
                    data=self.message(operation, "ipp://client:8631/jobs/42", b"job-uri"),
                    headers={"Content-Type": "application/ipp"}) as response:
                self.assertEqual(response.status, 200)
                await response.read()
        self.assertEqual([r[0] for r in self.received], ["/jobs/42", "/jobs/42"])

    async def test_administration_is_not_forwarded(self):
        async with self.client.post(self.proxy.make_url("/"), data=self.message(0x4003, "ipp://client:8631/"),
                                    headers={"Content-Type": "application/ipp"}) as response:
            header, _ = decode_prefix(await response.read())
        self.assertEqual(struct.unpack_from(">H", header, 2)[0], 0x0501)
        self.assertEqual(self.received, [])

    async def test_external_hostname_is_not_forwarded_as_cups_host(self):
        async with self.client.post(self.proxy.make_url("/"), data=self.message(11, "ipp://home-server:8631/"),
                headers={"Content-Type": "application/ipp", "Host": "home-server:8631"}) as response:
            self.assertEqual(response.status, 200)
            _, records = decode_prefix(await response.read())
        self.assertEqual(self.received[0][3], self.backend.make_url("/").authority)
        values = [r.value for _, r, _ in attributes(records)]
        self.assertIn(b"ipp://home-server:8631/", values)


if __name__ == "__main__":
    unittest.main()
