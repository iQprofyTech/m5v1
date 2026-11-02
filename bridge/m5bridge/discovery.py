"""LAN discovery: the Cardputer broadcasts `M5BRIDGE?` over UDP and the bridge answers with its HTTP port.

The reply carries no secrets; the device still has to authenticate with the token.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

PROBE = b"M5BRIDGE?"


class DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, http_port: int, name: str):
        self.reply = f"M5BRIDGE {http_port} {name}".encode("utf-8")[:200]
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if data.strip() == PROBE and self.transport is not None:
            log.debug("discovery probe from %s", addr[0])
            self.transport.sendto(self.reply, addr)


async def start_discovery(port: int, http_port: int, name: str) -> asyncio.DatagramTransport:
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: DiscoveryProtocol(http_port, name), local_addr=("0.0.0.0", port)
    )
    return transport
