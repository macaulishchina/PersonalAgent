"""Private instance control endpoints. Same JSON protocol on both transports."""

import asyncio
import os
import socket
import struct

LIMIT = 4 * 1024 * 1024


async def connect(path, *, limit=LIMIT):
    if os.name == "nt":
        from .windows_pipe import connect

        return await connect(path, limit=limit)
    reader, writer = await asyncio.open_unix_connection(path, limit=limit)
    try:
        verify_peer(writer)
    except BaseException:
        writer.close()
        await writer.wait_closed()
        raise
    return reader, writer


def verify_peer(writer):
    peer = writer.get_extra_info("socket").getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    _, uid, _ = struct.unpack("3i", peer)
    if uid != os.getuid():
        raise PermissionError("Control endpoint belongs to another account")


async def serve(path, callback, *, limit=LIMIT):
    if os.name == "nt":
        from .windows_pipe import serve

        return await serve(path, callback, limit=limit)

    async def accepted(reader, writer):
        try:
            verify_peer(writer)
        except PermissionError:
            writer.close()
            await writer.wait_closed()
            return
        await callback(reader, writer)

    path.unlink(missing_ok=True)
    server = await asyncio.start_unix_server(accepted, path=path, limit=limit)
    os.chmod(path, 0o600)
    return server


def cleanup(path):
    if os.name != "nt":
        path.unlink(missing_ok=True)
