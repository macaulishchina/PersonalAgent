"""Async pipes plus a startup gate: no target code runs outside its owned job."""

import asyncio
import os
import sys
import uuid


class OwnedProcess:
    def __init__(self, process, job, gate):
        self.process, self.job, self.gate = process, job, gate
        self.reaper = asyncio.create_task(self.reap())

    async def reap(self):
        # asyncio wait()/communicate() can wait for pipe EOF after the leader
        # exits. Descendants may still hold those pipes, so reap the job from
        # the OS exit status independently of the pipe transport's completion.
        try:
            while self.process.returncode is None:
                await asyncio.sleep(0.02)
        finally:
            self.close_job()

    def __getattr__(self, name):
        return getattr(self.process, name)

    async def wait(self):
        result = await self.process.wait()
        self.close_job()
        await self.reaper
        return result

    async def communicate(self, input=None):
        result = await self.process.communicate(input)
        self.close_job()
        await self.reaper
        return result

    def terminate(self):
        self.kill()

    def kill(self):
        import win32job

        if self.job is not None:
            win32job.TerminateJobObject(self.job, 1)

    def close_job(self):
        if self.gate is not None:
            self.gate.Close()
            self.gate = None
        if self.job is not None:
            self.job.Close()
            self.job = None


async def spawn(*args, **kwargs):
    import win32api
    import win32con
    import win32event
    import win32job
    from .windows import security_attributes
    from .executables import executable_command, python_command

    args = executable_command(args)

    if kwargs.get("env") is not None:
        environment = dict(kwargs["env"])
        for key in (
            "SystemRoot",
            "WINDIR",
            "COMSPEC",
            "PATHEXT",
            "TEMP",
            "TMP",
            "USERPROFILE",
            "LOCALAPPDATA",
            "APPDATA",
        ):
            if key not in environment and key in os.environ:
                environment[key] = os.environ[key]
        kwargs["env"] = environment

    kwargs.pop("start_new_session", None)
    # A unique named gate also works for console-less service processes. The
    # wrapper cannot spawn its target until AssignProcessToJobObject succeeds.
    gate_name = "Local\\PersonalAgent-" + uuid.uuid4().hex
    gate = None
    job = None
    process = None
    creation = None
    try:
        gate = win32event.CreateEvent(security_attributes(), True, False, gate_name)
        # pywin32 requires a string; an empty name creates an unnamed job.
        job = win32job.CreateJobObject(None, "")
        limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
        command, kwargs["env"] = python_command(
            [
                sys.executable,
                "-m",
                "assistant_runtime.platforms.process_gate",
                gate_name,
                *map(os.fspath, args),
            ],
            kwargs.get("env"),
        )
        creation = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *command,
                **kwargs,
            )
        )
        # If the caller cancels while asyncio creates pipes, retrieve and reap
        # the gated process before releasing its job/gate handles.
        process = await asyncio.shield(creation)
        handle = win32api.OpenProcess(
            win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, process.pid
        )
        try:
            win32job.AssignProcessToJobObject(job, handle)
        finally:
            handle.Close()
        win32event.SetEvent(gate)
        return OwnedProcess(process, job, gate)
    except BaseException:
        if process is None and creation is not None:
            result = await asyncio.gather(creation, return_exceptions=True)
            if not isinstance(result[0], BaseException):
                process = result[0]
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        if job is not None:
            job.Close()
        if gate is not None:
            gate.Close()
        raise
