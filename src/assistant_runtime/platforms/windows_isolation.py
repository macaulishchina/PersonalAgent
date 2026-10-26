"""Windows LPAC runner for trusted, fixed cmd check scripts in managed workspaces.

No arbitrary shell API. This backend owns a fresh profile/job for each operation;
model-controlled arguments never form the command line or the trusted script.
"""

import ctypes as C
from ctypes import wintypes as W
import os
from pathlib import Path
import threading
import time
import uuid

K = C.WinDLL("kernel32", use_last_error=True)
A = C.WinDLL("advapi32", use_last_error=True)
U = C.WinDLL("userenv", use_last_error=True)
P = C.c_void_p
SZ = C.c_size_t


class SI(C.Structure):
    _fields_ = [
        ("cb", W.DWORD),
        ("reserved", W.LPWSTR),
        ("desktop", W.LPWSTR),
        ("title", W.LPWSTR),
        ("x", W.DWORD),
        ("y", W.DWORD),
        ("xs", W.DWORD),
        ("ys", W.DWORD),
        ("xc", W.DWORD),
        ("yc", W.DWORD),
        ("fill", W.DWORD),
        ("flags", W.DWORD),
        ("show", W.WORD),
        ("cb2", W.WORD),
        ("reserved2", P),
        ("stdin", W.HANDLE),
        ("stdout", W.HANDLE),
        ("stderr", W.HANDLE),
    ]


class SIX(C.Structure):
    _fields_ = [("si", SI), ("attrs", P)]


class PI(C.Structure):
    _fields_ = [("process", W.HANDLE), ("thread", W.HANDLE), ("pid", W.DWORD), ("tid", W.DWORD)]


class SA(C.Structure):
    _fields_ = [("sid", P), ("attributes", W.DWORD)]


class SC(C.Structure):
    _fields_ = [("sid", P), ("capabilities", P), ("count", W.DWORD), ("reserved", W.DWORD)]


class BASIC(C.Structure):
    _fields_ = [
        ("process_time", C.c_int64),
        ("job_time", C.c_int64),
        ("flags", W.DWORD),
        ("min_ws", SZ),
        ("max_ws", SZ),
        ("active", W.DWORD),
        ("affinity", SZ),
        ("priority", W.DWORD),
        ("scheduling", W.DWORD),
    ]


class IO(C.Structure):
    _fields_ = [
        (name, C.c_uint64)
        for name in ["read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes"]
    ]


class LIMIT(C.Structure):
    _fields_ = [
        ("basic", BASIC),
        ("io", IO),
        ("process_mem", SZ),
        ("job_mem", SZ),
        ("peak_process", SZ),
        ("peak_job", SZ),
    ]


def proto(dll, name, args, result=W.BOOL):
    f = getattr(dll, name)
    f.argtypes = args
    f.restype = result
    return f


init = proto(K, "InitializeProcThreadAttributeList", [P, W.DWORD, W.DWORD, C.POINTER(SZ)])
update = proto(K, "UpdateProcThreadAttribute", [P, W.DWORD, SZ, P, SZ, P, P])
create = proto(
    K,
    "CreateProcessW",
    [W.LPCWSTR, W.LPWSTR, P, P, W.BOOL, W.DWORD, P, W.LPCWSTR, C.POINTER(SIX), C.POINTER(PI)],
)
close = proto(K, "CloseHandle", [W.HANDLE])
wait = proto(K, "WaitForSingleObject", [W.HANDLE, W.DWORD], W.DWORD)
resume = proto(K, "ResumeThread", [W.HANDLE], W.DWORD)
job_create = proto(K, "CreateJobObjectW", [P, W.LPCWSTR], W.HANDLE)
job_set = proto(K, "SetInformationJobObject", [W.HANDLE, C.c_int, P, W.DWORD])
job_assign = proto(K, "AssignProcessToJobObject", [W.HANDLE, W.HANDLE])
job_kill = proto(K, "TerminateJobObject", [W.HANDLE, W.UINT])
terminate = proto(K, "TerminateProcess", [W.HANDLE, W.UINT])
open_token = proto(A, "OpenProcessToken", [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)])
get_token = proto(A, "GetTokenInformation", [W.HANDLE, C.c_int, P, W.DWORD, C.POINTER(W.DWORD)])
profile = proto(
    U, "CreateAppContainerProfile", [W.LPCWSTR, W.LPCWSTR, W.LPCWSTR, P, W.DWORD, C.POINTER(P)], C.c_long
)
delete_profile = proto(U, "DeleteAppContainerProfile", [W.LPCWSTR], C.c_long)
sid_string = proto(A, "ConvertSidToStringSidW", [P, C.POINTER(W.LPWSTR)])
proto(K, "LocalFree", [P], P)
proto(A, "FreeSid", [P], P)
proto(K, "DeleteProcThreadAttributeList", [P], None)


def check(ok):
    if not ok:
        raise C.WinError(C.get_last_error())


class IsolatedCheck:
    def __init__(
        self,
        root,
        script,
        temporary,
        *,
        timeout=30,
        authorized=None,
        script_sha256=None,
        round_directory=None,
    ):
        self.root, self.script, self.temporary = (
            Path(root).absolute(),
            Path(script).absolute(),
            Path(temporary).absolute(),
        )
        self.round_directory = Path(round_directory).absolute() if round_directory is not None else None
        if self.round_directory is not None and (
            self.round_directory.parent != self.root / "runs"
            or not self.temporary.is_relative_to(self.round_directory)
        ):
            raise ValueError("Round resources must share the managed execution directory")
        self.script_sha256 = script_sha256
        self.timeout = timeout
        self.authorized = authorized or (lambda: True)
        self.cancelled = threading.Event()
        self.launch_id = str(uuid.uuid4())
        self.stopped = False
        self.evidence = {}
        if not 1 <= timeout <= 60:
            raise ValueError("Check timeout must be 1..60 seconds")
        if self.script.parent != self.root / "checks" or not self.temporary.is_relative_to(
            self.root / "runs"
        ):
            raise ValueError("Checks and temporary files require their dedicated managed directories")
        for path in (self.script, self.temporary):
            if not path.is_relative_to(self.root):
                raise ValueError("Check resources must belong to their managed workspace")

    def cancel(self):
        self.cancelled.set()

    @staticmethod
    def acl(path, sid, access, *, remove=False):
        import win32security as security

        current = security.GetNamedSecurityInfo(
            str(path), security.SE_FILE_OBJECT, security.DACL_SECURITY_INFORMATION
        )
        previous = current.GetSecurityDescriptorDacl()
        if previous is None:
            raise PermissionError("Managed check resource has an unrestricted ACL")
        acl = previous
        for i in reversed(range(acl.GetAceCount())):
            ace = acl.GetAce(i)
            if str(ace[-1]) == str(sid):
                acl.DeleteAce(i)
        if not remove:
            acl.AddAccessAllowedAceEx(security.ACL_REVISION, 0, access, sid)
        security.SetNamedSecurityInfo(
            str(path), security.SE_FILE_OBJECT, security.DACL_SECURITY_INFORMATION, None, None, acl, None
        )

    def run(self):
        from .directory import Directory
        import win32security
        import win32con
        import ntsecuritycon
        import win32file
        import subprocess

        name = "PA.Check." + uuid.uuid4().hex
        sid = P()
        attrs = None
        profile_created = False
        native_sid = None
        allocations, handles, granted, pins = [], [], [], []
        job, pi = None, PI()
        output = self.temporary / "check.log"
        script_handle = None
        result = {
            "launch_id": self.launch_id,
            "profile": name,
            "tree_stopped": False,
            "isolation": "windows_lpac_fixed_cmd",
        }
        self.evidence = result
        try:
            # Every ancestor is pinned and non-reparse before ACL or process operations.
            for path in (
                self.root,
                self.root / "work",
                self.root / "source",
                self.root / "checks",
                self.temporary,
            ):
                pins.append(Directory(path))
            script_directory = Directory(self.script.parent)
            pins.append(script_directory)
            script_handle = script_directory.open_file(self.script.name, os.O_RDONLY, frozen=True)
            if self.script_sha256 is not None:
                import hashlib

                with os.fdopen(os.dup(script_handle), "rb") as manifest:
                    actual = hashlib.sha256(manifest.read(65537)).hexdigest()
                if actual != self.script_sha256:
                    raise ValueError("Trusted check script changed after registration")
            hr = profile(name, name, "PA controlled check", None, 0, C.byref(sid))
            if hr < 0:
                raise OSError(f"AppContainer profile unavailable: {hr & 0xFFFFFFFF:08x}")
            profile_created = True
            sidtext = W.LPWSTR()
            check(sid_string(sid, C.byref(sidtext)))
            try:
                native_sid = win32security.ConvertStringSidToSid(sidtext.value)
                result["appcontainer_sid"] = sidtext.value
            finally:
                K.LocalFree(sidtext)
            read = ntsecuritycon.FILE_GENERIC_READ | ntsecuritycon.FILE_GENERIC_EXECUTE
            write = read | ntsecuritycon.FILE_GENERIC_WRITE | win32con.DELETE
            targets = {self.root: read}
            resources = [
                (self.root / "work", write),
                (self.root / "source", read),
                (self.root / "checks", read),
                (self.temporary, write),
            ]
            if self.round_directory is not None:
                resources.extend(
                    [(self.round_directory / "inputs", read), (self.round_directory / "artifacts", write)]
                )
                for path in (self.round_directory / "inputs", self.round_directory / "artifacts"):
                    pins.append(Directory(path))
            for base, access in resources:
                targets[base] = access
                for folder, dirs, files in os.walk(base, followlinks=False):
                    for item in [Path(folder), *(Path(folder) / part for part in dirs + files)]:
                        info = item.stat(follow_symlinks=False)
                        if getattr(info, "st_file_attributes", 0) & 0x400 or (
                            item.is_file() and info.st_nlink != 1
                        ):
                            raise ValueError(
                                "Isolation resources cannot contain reparse points or hard links"
                            )
                        targets[item] = access
            # Traverse only the per-execution temp ancestors inside this workspace.
            for parent in self.temporary.parents:
                if parent == self.root:
                    break
                targets.setdefault(parent, ntsecuritycon.FILE_TRAVERSE | win32con.SYNCHRONIZE)
            for path, access in targets.items():
                self.acl(path, native_sid, access)
                granted.append(path)
            icacls = Path(os.environ["WINDIR"]) / "System32/icacls.exe"
            for path in [base for base, access in resources if access == write]:
                subprocess.run(
                    [str(icacls), str(path), "/setintegritylevel", "(OI)(CI)L", "/T", "/Q"],
                    check=True,
                    capture_output=True,
                    timeout=10,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            size = SZ()
            init(None, 3, 0, C.byref(size))
            attrs = C.create_string_buffer(size.value)
            check(init(attrs, 3, 0, C.byref(size)))
            derive = proto(
                C.WinDLL("kernelbase", use_last_error=True),
                "DeriveCapabilitySidsFromName",
                [W.LPCWSTR, C.POINTER(P), C.POINTER(W.DWORD), C.POINTER(P), C.POINTER(W.DWORD)],
            )
            caps = (SA * 2)()
            for i, cap_name in enumerate(("registryRead", "lpacCom")):
                groups = P()
                ng = W.DWORD()
                capids = P()
                nc = W.DWORD()
                check(derive(cap_name, C.byref(groups), C.byref(ng), C.byref(capids), C.byref(nc)))
                group_list = C.cast(groups, C.POINTER(P))
                cap_list = C.cast(capids, C.POINTER(P))
                caps[i] = SA(cap_list[0], 4)
                allocations.extend(
                    [group_list[j] for j in range(ng.value)]
                    + [groups]
                    + [cap_list[j] for j in range(nc.value)]
                    + [capids]
                )
            sc = SC(sid, C.cast(caps, P), 2, 0)
            policy = W.DWORD(1)
            check(update(attrs, 0, 0x20009, C.byref(sc), C.sizeof(sc), None, None))
            check(update(attrs, 0, 0x2000F, C.byref(policy), C.sizeof(policy), None, None))
            comspec = str(Path(os.environ["WINDIR"]) / "System32/cmd.exe")
            env = {
                "SystemRoot": os.environ["WINDIR"],
                "WINDIR": os.environ["WINDIR"],
                "COMSPEC": comspec,
                "PATH": str(Path(os.environ["WINDIR"]) / "System32"),
                "TEMP": str(self.temporary),
                "TMP": str(self.temporary),
                "USERPROFILE": str(self.temporary),
                "APPDATA": str(self.temporary),
                "LOCALAPPDATA": str(self.temporary),
                "ALLUSERSPROFILE": str(self.root / "source"),
                "ProgramData": str(self.root / "source"),
            }
            if self.round_directory is not None:
                env["PA_INPUTS"] = os.path.relpath(self.round_directory / "inputs", self.root / "work")
                env["PA_ARTIFACTS"] = os.path.relpath(self.round_directory / "artifacts", self.root / "work")
            envbuf = C.create_unicode_buffer("\0".join(k + "=" + v for k, v in sorted(env.items())) + "\0\0")
            log = win32file.CreateFile(
                str(output),
                win32con.GENERIC_WRITE,
                win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
                None,
                win32con.CREATE_NEW,
                0,
                None,
            )
            handles.append(log)
            inherit = proto(K, "SetHandleInformation", [W.HANDLE, W.DWORD, W.DWORD])
            check(inherit(int(log), 1, 1))
            inherited = (W.HANDLE * 1)(int(log))
            check(update(attrs, 0, 0x20002, inherited, C.sizeof(inherited), None, None))
            startup = SIX()
            startup.si.cb = C.sizeof(SIX)
            startup.attrs = C.cast(attrs, P)
            startup.si.flags = 0x100
            startup.si.stdout = int(log)
            startup.si.stderr = int(log)
            job = job_create(None, None)
            check(job)
            limits = LIMIT()
            limits.basic.flags = 0x2000
            check(job_set(job, 9, C.byref(limits), C.sizeof(limits)))
            relative = os.path.relpath(self.script, self.root / "work")
            command = f'"{comspec}" /d /q /c "{relative}"'
            if self.cancelled.is_set() or not self.authorized():
                raise RuntimeError("Isolated check cancelled or unauthorized before process creation")
            check(
                create(
                    comspec,
                    C.create_unicode_buffer(command),
                    None,
                    None,
                    True,
                    0x80000 | 0x400 | 0x8 | 4,
                    envbuf,
                    str(self.root / "work"),
                    C.byref(startup),
                    C.byref(pi),
                )
            )
            check(job_assign(job, pi.process))
            result["pid"] = pi.pid
            token = W.HANDLE()
            check(open_token(pi.process, 8, C.byref(token)))
            try:
                flag = W.DWORD()
                needed = W.DWORD()
                check(get_token(token, 29, C.byref(flag), 4, C.byref(needed)))
                if flag.value != 1:
                    raise PermissionError("Created process is not an AppContainer")
            finally:
                close(token)
            if resume(pi.thread) == 0xFFFFFFFF:
                raise C.WinError(C.get_last_error())
            deadline = time.monotonic() + self.timeout
            reason = None
            while wait(pi.process, 50) != 0:
                if (
                    self.cancelled.is_set()
                    or not self.authorized()
                    or time.monotonic() >= deadline
                    or output.stat().st_size > 1024 * 1024
                ):
                    reason = (
                        "cancelled"
                        if self.cancelled.is_set()
                        else "authorization_expired"
                        if not self.authorized()
                        else "timeout_or_output_limit"
                    )
                    break
            # Kill any descendants even after the leader exits. Keep the job handle
            # until the actual active-process count reaches zero.
            check(job_kill(job, 99))
            query = proto(K, "QueryInformationJobObject", [W.HANDLE, C.c_int, P, W.DWORD, P])
            accounting = C.create_string_buffer(48)
            import struct

            stop_deadline = time.monotonic() + 5
            while True:
                check(query(job, 1, accounting, 48, None))
                active = struct.unpack_from("<I", accounting.raw, 40)[0]
                if active == 0:
                    self.stopped = True
                    break
                if time.monotonic() >= stop_deadline:
                    raise RuntimeError("Isolated process tree stop could not be confirmed")
                time.sleep(0.02)
            value = W.DWORD()
            check(proto(K, "GetExitCodeProcess", [W.HANDLE, C.POINTER(W.DWORD)])(pi.process, C.byref(value)))
            result.update(
                exit_code=value.value,
                tree_stopped=True,
                reason=reason,
                output=output.read_bytes()[: 1024 * 1024].decode("utf-8", errors="replace"),
            )
            return result
        finally:
            if job:
                job_kill(job, 99)
            if pi.process:
                terminate(pi.process, 99)
                wait(pi.process, 5000)
            if not pi.process:
                self.stopped = True
            elif job:
                query = proto(K, "QueryInformationJobObject", [W.HANDLE, C.c_int, P, W.DWORD, P])
                accounting = C.create_string_buffer(48)
                import struct

                until = time.monotonic() + 5
                while query(job, 1, accounting, 48, None):
                    if struct.unpack_from("<I", accounting.raw, 40)[0] == 0:
                        self.stopped = True
                        break
                    if time.monotonic() >= until:
                        break
                    time.sleep(0.02)
            result["tree_stopped"] = self.stopped
            if pi.process:
                close(pi.process)
            if pi.thread:
                close(pi.thread)
            if job:
                close(job)
            for handle in handles:
                handle.Close()
            if attrs is not None:
                K.DeleteProcThreadAttributeList(attrs)
            for allocation in allocations:
                K.LocalFree(allocation)
            if script_handle is not None:
                os.close(script_handle)
            try:
                # An unconfirmed writer keeps the workspace quarantined. Do not
                # follow paths it might still replace while removing grants.
                if self.stopped:
                    cleanup = set(granted)
                    for base in (
                        [base for base, access in resources if access == write]
                        if native_sid is not None and "resources" in locals()
                        else ()
                    ):
                        for folder, dirs, files in os.walk(base, followlinks=False):
                            cleanup.update(Path(folder) / name for name in dirs + files)
                    cleanup_errors = []
                    for path in sorted(cleanup, key=lambda item: len(item.parts), reverse=True):
                        parent_pin = None
                        try:
                            if path.exists():
                                parent_pin = Directory(path.parent)
                                info = parent_pin.stat(path.name)
                                if not path.is_dir() and info.st_nlink != 1:
                                    raise ValueError("Hard-linked cleanup target")
                                self.acl(path, native_sid, 0, remove=True)
                        except (OSError, ValueError) as error:
                            cleanup_errors.append(type(error).__name__)
                        finally:
                            if parent_pin:
                                parent_pin.close()
                    result["cleanup_errors"] = cleanup_errors
                    if profile_created:
                        result["profile_cleanup_hresult"] = delete_profile(name)
            finally:
                for pin in reversed(pins):
                    pin.close()
                if sid:
                    A.FreeSid(sid)

    async def execute(self):
        from assistant_runtime.api.lifecycle import wait_thread

        return await wait_thread(self.run, cancel=self.cancel)
