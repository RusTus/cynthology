"""Entry point: one process serving the same app on HTTP (5000) and HTTPS (5001).

    python3 -m cynthology.serve

One process (rather than two) so that logins, caches and package state are shared.
"""
import asyncio
import os

import uvicorn

from . import settings, tls
from .main import app


def configs() -> list[uvicorn.Config]:
    s = settings.get_all()
    host = os.environ.get("CYN_BIND", "0.0.0.0")
    out = [uvicorn.Config(app, host=host, port=int(os.environ.get("CYN_HTTP_PORT", s["http_port"])),
                          proxy_headers=False, log_level="info")]
    if tls.ensure_cert():
        out.append(uvicorn.Config(app, host=host, port=int(os.environ.get("CYN_HTTPS_PORT", s["https_port"])),
                                  ssl_certfile=tls.CERT, ssl_keyfile=tls.KEY, proxy_headers=False,
                                  log_level="info"))
    return out


async def main() -> None:
    servers = [uvicorn.Server(c) for c in configs()]
    # only the first server installs signal handlers; the rest stop with it
    for s in servers[1:]:
        s.install_signal_handlers = lambda: None
    first = asyncio.ensure_future(servers[0].serve())
    rest = [asyncio.ensure_future(s.serve()) for s in servers[1:]]
    await first
    for s in servers[1:]:
        s.should_exit = True
    await asyncio.gather(*rest, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
