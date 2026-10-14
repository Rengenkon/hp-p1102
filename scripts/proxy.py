"""Single-printer IPP facade: public / and /jobs/N, internal CUPS queue."""
import asyncio
import logging
import struct
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, web

from ipp import JOB_PATH, ipp_error, printer_attributes_request, read_prefix, rewrite_request, rewrite_response
from runtime import load_config

LOG = logging.getLogger("ipp-proxy")
SESSION = web.AppKey("session", ClientSession)
CONFIG_PATH = web.AppKey("config_path", str)
UPSTREAM = web.AppKey("upstream", str)
# Public print/job operations. CUPS administration is not routed through the facade.
OPERATIONS = set(range(0x0002, 0x0010))


def authority(host):
    if any(c in host for c in "/?#@\\\r\n"):
        raise ValueError("Invalid Host header")
    parsed = urlsplit("http://" + host)
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Invalid Host header")
    parsed.port  # validate port and IPv6 syntax
    return host


async def session_context(app):
    async with ClientSession(timeout=ClientTimeout(total=None, sock_connect=5, sock_read=60),
                             auto_decompress=False, trust_env=False) as session:
        app[SESSION] = session
        yield


async def health(request):
    # Probe CUPS through the public route too, so a live listener alone isn't healthy.
    prefix = printer_attributes_request("ipp://localhost:8631/")
    try:
        async with request.app[SESSION].post("http://127.0.0.1:8631/", data=prefix,
                                               headers={"Content-Type": "application/ipp"}) as reply:
            data = await reply.read()
            if reply.status != 200 or len(data) < 8 or struct.unpack_from(">H", data, 2)[0] >= 0x0400:
                raise ValueError("CUPS rejected Get-Printer-Attributes")
    except (ClientError, ValueError, TimeoutError) as error:
        raise web.HTTPServiceUnavailable(text=str(error)) from error
    return web.Response(text="OK\n")


async def forward(request):
    config = load_config(request.app[CONFIG_PATH])
    queue_path = "/printers/" + config["printer"]["name"]
    if request.path not in ("/", "/ipp/print", queue_path) and not JOB_PATH.fullmatch(request.path):
        raise web.HTTPNotFound()
    if request.content_type != "application/ipp" or request.headers.get("Content-Encoding", "identity") != "identity":
        raise web.HTTPUnsupportedMediaType(text="Expected uncompressed application/ipp")
    try:
        public = authority(request.host)
        async with asyncio.timeout(15):
            prefix = await read_prefix(request.content)
        operation = struct.unpack_from(">H", prefix, 2)[0]
        if operation not in OPERATIONS:
            return web.Response(body=ipp_error(prefix[:8], 0x0501, "Operation not supported by print service"),
                                content_type="application/ipp")
        rewritten, path = rewrite_request(prefix, queue_path)
        if JOB_PATH.fullmatch(request.path) and path == queue_path:
            path = request.path
    except (ValueError, UnicodeError, asyncio.IncompleteReadError, TimeoutError) as error:
        raise web.HTTPBadRequest(text=str(error)) from error

    async def document():
        yield rewritten
        async for chunk in request.content.iter_chunked(65536):
            yield chunk

    try:
        async with request.app[SESSION].post(
                request.app[UPSTREAM] + path, data=document(), chunked=True,
                headers={"Content-Type": "application/ipp"}, allow_redirects=False) as upstream:
            if upstream.status != 200 or upstream.content_type != "application/ipp":
                text = (await upstream.content.read(4096)).decode(errors="replace")
                LOG.error("CUPS HTTP %s for %s: %s", upstream.status, path, text)
                raise web.HTTPBadGateway(text=f"CUPS HTTP {upstream.status}: {text}")
            prefix = await read_prefix(upstream.content)
            reply = rewrite_response(prefix, public, queue_path)
            status = struct.unpack_from(">H", prefix, 2)[0]
            LOG.info("operation=0x%04x request=%s destination=%s ipp-status=0x%04x", operation,
                     struct.unpack_from(">I", prefix, 4)[0], path, status)
            response = web.StreamResponse(headers={"Content-Type": "application/ipp"})
            await response.prepare(request)
            await response.write(reply)
            async for chunk in upstream.content.iter_chunked(65536):
                await response.write(chunk)
            await response.write_eof()
            return response
    except (ClientError, ValueError, UnicodeError, asyncio.IncompleteReadError, TimeoutError) as error:
        LOG.error("CUPS exchange failed: %s", error)
        raise web.HTTPBadGateway(text="CUPS exchange failed: " + str(error)) from error


async def index(request):
    return web.Response(text="IPP print service: use ipp://" + authority(request.host) + "/\n")


def create_app(config_path="/etc/printer/printer.toml", upstream="http://127.0.0.1:631"):
    app = web.Application(client_max_size=0)
    app[CONFIG_PATH] = str(config_path)
    app[UPSTREAM] = upstream
    app.cleanup_ctx.append(session_context)
    app.router.add_get("/health", health)
    app.router.add_get("/", index)
    app.router.add_post("/{path:.*}", forward)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    web.run_app(create_app(), host="0.0.0.0", port=8631, access_log=LOG)
