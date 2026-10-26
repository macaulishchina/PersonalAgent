"""Directory-relative operations. Handles pin directories; reparse points are rejected."""

import os
from pathlib import Path
import shutil
import sys


def component(name):
    if not isinstance(name, str) or name in {"", ".", ".."} or any(c in name for c in "/\\\x00"):
        raise ValueError("Expected one safe path component")
    if sys.platform == "win32":
        stem = name.split(".")[0].upper()
        if (
            name[-1:] in {" ", "."}
            or any(c in name for c in '<>:"|?*')
            or any(ord(c) < 32 for c in name)
            or stem
            in {
                "CON",
                "PRN",
                "AUX",
                "NUL",
                *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10)),
            }
        ):
            raise ValueError("Unsupported Windows file name")
    return name


class Directory:
    def __init__(self, path, *, parent=None, name=None):
        self.path = Path(path)
        self.fd, self.pins = None, []
        if sys.platform == "win32":
            self._windows_open()
        else:
            self.fd = os.open(
                name if parent else self.path,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                **({"dir_fd": parent.fd} if parent else {}),
            )

    def _windows_open(self):
        import win32con
        import win32file
        import pywintypes
        from .windows import file_error

        path = self.path.absolute()
        if str(path).startswith("\\\\"):
            raise ValueError("Private and managed directory operations require a local volume")
        try:
            for ancestor in [*reversed(path.parents), path]:
                handle = win32file.CreateFile(
                    str(ancestor),
                    win32con.GENERIC_READ,
                    win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
                    None,
                    win32con.OPEN_EXISTING,
                    win32con.FILE_FLAG_BACKUP_SEMANTICS | win32file.FILE_FLAG_OPEN_REPARSE_POINT,
                    None,
                )
                self.pins.append(handle)
                info = win32file.GetFileInformationByHandle(handle)
                if info[0] & win32con.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise ValueError("Reparse points are not managed directories")
            self.path = path
        except pywintypes.error as exc:
            self.close()
            raise file_error(exc, self.path) from exc
        except BaseException:
            self.close()
            raise

    def clone(self):
        if sys.platform == "win32":
            return Directory(self.path)
        assert self.fd is not None
        result = object.__new__(Directory)
        result.path, result.fd, result.pins = self.path, os.dup(self.fd), []
        return result

    def child(self, name):
        component(name)
        return Directory(self.path / name, parent=self, name=name)

    def stat(self, name=None):
        if name is not None:
            component(name)
        if sys.platform == "win32":
            info = os.stat(self.path / name if name else self.path, follow_symlinks=False)
            if getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Reparse points are not managed files")
            return info
        assert self.fd is not None
        return os.stat(name, dir_fd=self.fd, follow_symlinks=False) if name else os.fstat(self.fd)

    def mkdir(self, name, mode=0o700):
        component(name)
        if sys.platform == "win32":
            os.mkdir(self.path / name)
        else:
            os.mkdir(name, mode, dir_fd=self.fd)
        self.sync()

    def open_file(self, name, flags, mode=0o600, *, frozen=False, exclusive=False):
        component(name)
        if exclusive and sys.platform != "win32":
            raise ValueError("Exclusive managed writes currently require Windows")
        if frozen and (sys.platform != "win32" or flags != os.O_RDONLY):
            raise ValueError("Frozen source handles currently require Windows read-only access")
        if sys.platform != "win32":
            return os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode, dir_fd=self.fd)
        import msvcrt
        import win32con
        import win32file
        import pywintypes
        from .windows import security_attributes, file_error

        access = win32con.GENERIC_READ
        if flags & (os.O_WRONLY | os.O_RDWR):
            access |= win32con.GENERIC_WRITE
        creation = win32con.OPEN_ALWAYS if flags & os.O_CREAT else win32con.OPEN_EXISTING
        if flags & os.O_EXCL:
            creation = win32con.CREATE_NEW
        try:
            handle = win32file.CreateFile(
                str(self.path / name),
                access,
                win32con.FILE_SHARE_READ
                if frozen or exclusive
                else (win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE),
                security_attributes(),
                creation,
                win32file.FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
        except pywintypes.error as exc:
            raise file_error(exc, self.path / name) from exc
        try:
            info = win32file.GetFileInformationByHandle(handle)
            if info[0] & (win32con.FILE_ATTRIBUTE_REPARSE_POINT | win32con.FILE_ATTRIBUTE_DIRECTORY):
                raise ValueError("Expected a regular non-reparse file")
            # The CRT accepts access/append flags, not CreateFile creation flags.
            raw_handle = handle.Detach()
            try:
                fd = msvcrt.open_osfhandle(
                    raw_handle, (flags & (os.O_RDWR | os.O_WRONLY | os.O_APPEND)) | os.O_BINARY
                )
            except BaseException:
                win32file.CloseHandle(raw_handle)
                raise
            try:
                if flags & os.O_TRUNC:
                    os.ftruncate(fd, 0)
            except BaseException:
                os.close(fd)
                raise
            return fd
        finally:
            handle.Close()

    def names(self):
        return os.scandir(self.path if sys.platform == "win32" else self.fd)

    def link(self, source, destination, name):
        component(source)
        component(name)
        if sys.platform == "win32":
            self.stat(source)  # Reject a reparse entry before linking.
            os.link(self.path / source, destination.path / name, follow_symlinks=False)
        else:
            os.link(source, name, src_dir_fd=self.fd, dst_dir_fd=destination.fd, follow_symlinks=False)
        destination.sync()

    def unlink(self, name):
        component(name)
        if sys.platform == "win32":
            self.stat(name)
            os.unlink(self.path / name)
        else:
            os.unlink(name, dir_fd=self.fd)
        self.sync()

    def rmdir(self, name):
        component(name)
        self.stat(name)  # Reject reparse entries before removing an empty directory.
        if sys.platform == "win32":
            os.rmdir(self.path / name)
        else:
            os.rmdir(name, dir_fd=self.fd)
        self.sync()

    def sync(self):
        if sys.platform != "win32":
            assert self.fd is not None
            os.fsync(self.fd)

    def free_bytes(self):
        if sys.platform == "win32":
            return shutil.disk_usage(self.path).free
        assert self.fd is not None
        info = os.fstatvfs(self.fd)
        return info.f_bavail * info.f_frsize

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        for handle in reversed(self.pins):
            handle.Close()
        self.pins = []
