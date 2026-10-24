"""Platform-aware assertions: Windows ACLs, never POSIX permission-bit substitutes."""

import os
from pathlib import Path
import unittest


def symlink_or_skip(link, target, *, target_is_directory=False):
    try:
        Path(link).symlink_to(target, target_is_directory=target_is_directory)
    except OSError as exc:
        if os.name == "nt" and exc.winerror == 1314:
            raise unittest.SkipTest("Windows symlink privilege is not available for this account") from exc
        raise


def private_mode(path):
    path = Path(path)
    if os.name != "nt":
        return path.stat().st_mode & 0o777
    import win32security

    descriptor = win32security.GetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
    )
    acl = descriptor.GetSecurityDescriptorDacl()
    if acl is None:
        raise AssertionError("Private file has a null ACL")
    broad = {"S-1-1-0", "S-1-5-11", "S-1-5-32-545"}
    for index in range(acl.GetAceCount()):
        ace = acl.GetAce(index)
        if ace[0][0] == win32security.ACCESS_ALLOWED_ACE_TYPE:
            if win32security.ConvertSidToStringSid(ace[-1]) in broad and ace[1]:
                raise AssertionError("Private file grants general users access")
    return 0o700 if path.is_dir() else 0o600


def process_alive(pid):
    if os.name == "nt":
        import pywintypes
        import win32api
        import win32process

        try:
            handle = win32api.OpenProcess(0x1000, False, pid)
        except pywintypes.error as exc:
            if exc.winerror == 87:
                return False
            raise
        try:
            return win32process.GetExitCodeProcess(handle) == 259
        finally:
            handle.Close()
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        return state not in {"Z", "X"}
    except FileNotFoundError:
        return False
