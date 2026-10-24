"""POSIX console implementation."""

import asyncio
import os
import sys


async def terminal_input(*, secret):
    """Cancellable input: browser completion must not leave a blocked input thread."""
    import termios

    loop = asyncio.get_running_loop()
    future = loop.create_future()
    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd) if secret and os.isatty(fd) else None
    if previous:
        hidden = list(previous)
        hidden[3] &= ~termios.ECHO
        termios.tcsetattr(fd, termios.TCSANOW, hidden)

    def ready():
        if not future.done():
            value = sys.stdin.readline()
            if not value:
                future.set_exception(EOFError("Model login input closed"))
            else:
                future.set_result(value.rstrip("\n"))

    try:
        loop.add_reader(fd, ready)
        return await future
    finally:
        loop.remove_reader(fd)
        if previous:
            termios.tcsetattr(fd, termios.TCSANOW, previous)
            print(flush=True)
