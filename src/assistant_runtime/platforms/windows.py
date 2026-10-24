"""Windows ACL and console mechanisms. Imported only on Windows."""

import asyncio
import sys


def security_attributes(*, directory=False):
    import ntsecuritycon
    import win32api
    import win32con
    import win32security
    import pywintypes

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    acl = win32security.ACL()
    flags = (win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE) if directory else 0
    for sid in (user, win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)):
        acl.AddAccessAllowedAceEx(win32security.ACL_REVISION, flags, ntsecuritycon.FILE_ALL_ACCESS, sid)
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorDacl(1, acl, 0)
    descriptor.SetSecurityDescriptorControl(win32security.SE_DACL_PROTECTED, win32security.SE_DACL_PROTECTED)
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = descriptor
    attributes.bInheritHandle = False
    return attributes


def file_error(exc, path=None):
    """Expose normal Python filesystem errors across the pywin32 boundary."""
    if sys.platform != "win32":
        raise RuntimeError("Windows file errors are not available on this platform")
    import ctypes

    error = ctypes.WinError(exc.winerror)
    if path is not None:
        error.filename = str(path)
    return error


def verify_process_owner(pid):
    """ACLs authorize clients; verify both peers as the instance account too."""
    import win32api
    import win32con
    import win32security

    process = win32api.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    tokens = []
    try:
        for handle in (process, win32api.GetCurrentProcess()):
            tokens.append(win32security.OpenProcessToken(handle, win32con.TOKEN_QUERY))
        users = [win32security.GetTokenInformation(token, win32security.TokenUser)[0] for token in tokens]
        if users[0] != users[1]:
            raise PermissionError("Control endpoint belongs to another account")
    finally:
        for token in tokens:
            token.Close()
        process.Close()


def private(path, *, directory=False):
    import win32security

    acl = security_attributes(directory=directory).SECURITY_DESCRIPTOR.GetSecurityDescriptorDacl()
    win32security.SetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        acl,
        None,
    )


async def terminal_input(*, secret):
    if sys.platform != "win32":
        raise RuntimeError("Windows terminal input is not available on this platform")
    import msvcrt

    if not sys.stdin.isatty():
        raise RuntimeError("Account input requires an interactive Windows console")
    value: list[str] = []
    try:
        while True:
            if not msvcrt.kbhit():
                await asyncio.sleep(0.05)
                continue
            char = msvcrt.getwch()
            if char in {"\r", "\n"}:
                return "".join(value)
            if char == "\x03":
                raise asyncio.CancelledError
            if char == "\x1a":
                raise EOFError("Model login input closed")
            if char in {"\x00", "\xe0"}:
                msvcrt.getwch()
            elif char == "\b":
                if value:
                    value.pop()
                    if not secret:
                        print("\b \b", end="", flush=True)
            elif char.isprintable():
                value.append(char)
                if not secret:
                    print(char, end="", flush=True)
    finally:
        print(flush=True)
