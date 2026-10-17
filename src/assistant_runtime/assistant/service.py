"""Local devices and wake lifecycle around a single native Codex voice session."""

import asyncio
from collections import deque
import fcntl
import json
import logging
import os
import signal
import time

import miniaudio
import numpy as np

from assistant_runtime.assistant.commands import parse_command
from assistant_runtime.assistant.quiet import QuietHours
from assistant_runtime.assistant.dialogue import Dialogue
from assistant_runtime.store import Store
from assistant_runtime.kernel.contracts import (
    Control,
    MemoryRequest,
    AudioFrame,
    DeviceQuery,
    VideoCapture,
    VideoRecord,
    CameraMotion,
)

log = logging.getLogger(__name__)


class AssistantService:
    def __init__(self, config, no_mic=False):
        self.config, self.no_mic = config, no_mic
        self.quiet_hours = QuietHours.from_settings(config.quiet_hours)
        config.state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(config.state_dir, 0o700)
        self.lock_file = (config.state_dir / "service.lock").open("w")
        fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.store = Store(config.state_dir / "assistant.sqlite3")
        self.events = asyncio.Queue(maxsize=256)
        self.stop_event = asyncio.Event()
        self.active = False
        self.mode = "standby"
        self.started = time.time()
        self.last_activity = time.monotonic()
        self.last_frame = None
        self.last_input = ""
        self.input_count = self.wake_count = 0
        self.last_error = None
        self.connect_task = None
        self.preroll = deque(maxlen=50)  # approximately one second, memory only
        self.tasks = []
        self.transition = asyncio.Lock()
        self.standby_ready = asyncio.Event()
        self.standby_ready.set()
        self.notification_output = False
        self.last_wake = None
        self.last_output = None
        self.recovering = False
        self.input_rms = 0
        self.native_interruptions = 0
        self.last_speech = 0.0
        self.dialogue = Dialogue(config.voice_follow_up_seconds)
        self.reconnect_task = None
        self.voice_decision_task = None
        from assistant_runtime.bootstrap import compose

        self.runtime = compose(self)

    def quiet_active(self):
        policy = getattr(self, "quiet_hours", None)
        return bool(policy and policy.active())

    async def _cue(self, error=False):
        if not self.quiet_active():
            await self.player.cue(error=error)

    async def _enforce_quiet(self):
        if self.quiet_active() and self.active:
            await self.sleep()

    def audio_received(self, pcm):
        if isinstance(pcm, AudioFrame):
            if pcm.generation != self.voice.epoch:
                return
            pcm = pcm.pcm
        if (not self.active and not self.notification_output) or self.quiet_active():
            return
        self.player.feed(pcm)
        if np.max(np.abs(np.frombuffer(pcm, dtype="<i2").astype(np.int32)), initial=0) > 150:
            self.last_output = time.monotonic()
            self.last_activity = self.last_output

    async def speak(self, text):
        if self.active and not self.quiet_active():
            await self.voice.say(text)

    async def notify_task(self, text):
        """Output-only notification; return transport receipt, never a read receipt."""
        if self.quiet_active() or self.active:
            return "deferred"
        async with self.transition:
            if self.active or self.quiet_active():
                return "deferred"
            self.notification_output = True
            self.player.manual_muted = False
            started = time.monotonic()
            try:
                await self.voice.open(notification_only=True)
                await self.voice.say(text[:1000])
                while time.monotonic() - started < 30:
                    if self.active or self.quiet_active() or self.player.manual_muted:
                        return "interrupted"
                    if (
                        self.last_output
                        and self.last_output > started
                        and time.monotonic() - self.last_output > 1.5
                        and not self.player.playing
                    ):
                        return "played"
                    await asyncio.sleep(0.1)
                return "unknown"
            finally:
                self.notification_output = False
                await self.player.stop()
                await self.voice.close()

    def _record_wake(self, kind, correlation, **data):
        reporter = getattr(self, "wake_reporter", None)
        if reporter:
            reporter.offer(
                type="wake." + kind,
                subject="assistant",
                event_id=correlation + ":" + kind,
                data={"correlation": correlation, **data},
            )

    async def wake(self, preroll=(), *, source="other", keyword=None):
        if self.active or self.quiet_active():
            return
        if self.mode == "closing":
            await self.standby_ready.wait()
        if self.active or self.quiet_active():
            return
        if getattr(self, "memory", None):
            self.memory.preempt()
        self.active = True
        self.mode = "connecting"
        self.last_activity = self.last_wake = time.monotonic()
        self.dialogue.wake()
        self.last_error = None
        self.wake_count += 1
        import uuid

        correlation = uuid.uuid4().hex
        self._record_wake("accepted", correlation, entrance=source, keyword=keyword)
        self.player.manual_muted = False
        await self._cue()
        self.connect_task = self.context.spawn(self._connect(tuple(preroll), correlation), "voice-connect")
        log.info("wake_detected connecting_native_voice")
        return True

    async def _connect(self, preroll, correlation=None):
        async with self.transition:
            try:
                self.voice.device_context = self.devices.context_text()
                self.voice.memory_context = self.memory.context_text() if self.memory else ""
                await self.voice.open(preroll)
                if not self.active or self.quiet_active():
                    if correlation:
                        self._record_wake("cancelled", correlation)
                    await self.voice.close()
                    return
                self.mode = "active"
                self.last_error = None
                if correlation:
                    self._record_wake("connected", correlation)
                log.info("native_voice_ready auth=chatgpt transport=webrtc")
            except asyncio.CancelledError:
                if correlation:
                    self._record_wake("cancelled", correlation)
                raise
            except Exception as exc:
                if correlation:
                    self._record_wake("failed", correlation, error=type(exc).__name__)
                self.last_error = "Native voice startup failed: " + type(exc).__name__ + ": " + str(exc)[:180]
                log.warning("%s", self.last_error)
                self.active = False
                self.mode = "standby"
                self.recognizer.reset_keyword()
                await self._cue(error=True)

    async def sleep(self):
        if self.mode == "closing":
            await self.standby_ready.wait()
            return
        self.active = False
        self.mode = "closing"
        self.standby_ready.clear()
        try:
            # Silence locally before memory work, provider shutdown or reconnect cleanup.
            await self.player.stop()
            if getattr(self, "memory", None):
                self.memory.enqueue()
            reconnect = getattr(self, "reconnect_task", None)
            if reconnect and reconnect is not asyncio.current_task() and not reconnect.done():
                reconnect.cancel()
                await asyncio.gather(reconnect, return_exceptions=True)
            self.recovering = False
            self.preroll.clear()
            self.recognizer.reset_keyword()
            if self.connect_task and not self.connect_task.done():
                self.connect_task.cancel()
                await asyncio.gather(self.connect_task, return_exceptions=True)
        finally:
            try:
                async with self.transition:
                    await self.voice.close()
            finally:
                self.mode = "standby"
                self.standby_ready.set()
        log.info("session_standby background_task=%s", self.backend.busy)

    async def _capture(self):
        async for pcm in self.route.frames():
            self.last_frame = time.monotonic()
            if self.quiet_active():
                self.preroll.clear()
                continue
            samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
            self.input_rms = round(float(np.sqrt(np.mean(samples * samples))), 5)
            if not self.active:
                self.preroll.append(pcm)
                raw = getattr(self.route, "raw_frame", None) or pcm
                wake_samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
                keyword = await self.recognizer.detect(wake_samples)
                if keyword and not self.active:
                    log.info("keyword_detected phrase=%s", keyword)
                    await self.wake(tuple(self.preroll), source="keyword", keyword=keyword)
                    self.preroll.clear()
            else:
                self.voice.feed(pcm)
            self.recognizer.vad.accept_waveform(samples)
            speech = self.recognizer.vad.is_speech_detected()
            self.player.input_speaking = speech
            if speech and self.active:
                self.last_speech = time.monotonic()
                # Room echo also passes a simple VAD. Let native voice identify
                # a user turn before flushing playback. VAD never renews attention.
            while not self.recognizer.vad.empty():
                self.recognizer.vad.pop()  # no utterance ASR in the native route

    async def _events(self):
        while True:
            event = await self.events.get()
            try:
                if event["epoch"] != self.voice.epoch or not self.active or self.quiet_active():
                    continue
                if event["kind"] == "transcript":
                    role, text = event["role"], event["text"]
                    self.store.transcript(role, text)
                    if role == "user":
                        self.dialogue.user()
                        self.last_input = text
                        self.backend.last_user_text = text
                        self.input_count += 1
                        self.last_activity = time.monotonic()
                        cmd = parse_command(text, self.config.wake_words)
                        from ..kernel.contracts import DecisionSample

                        self.context.publish(
                            "decision.observed",
                            sample=DecisionSample(
                                "voice_intent",
                                f"voice:{self.backend.session_id}:{event['epoch']}:{event.get('message_id') or time.time_ns()}",
                                {"utterance": text, "execution_active": bool(self.backend.busy)},
                                cmd.kind if cmd else None,
                            ),
                        )
                        if cmd:
                            await self.control(cmd)
                        elif (
                            self.config.decisions.get("external_data_consent", False)
                            and len(text) <= 80
                            and any(
                                word in text
                                for word in ("结束", "退下", "安静", "停", "别说", "取消", "不用", "陪", "聊")
                            )
                        ):
                            # No new external route is enabled before consent.
                            pending = getattr(self, "voice_decision_task", None)
                            if pending and not pending.done():
                                pending.cancel()
                            self.voice_decision_task = self.context.spawn(
                                self._voice_decision(
                                    text,
                                    event["epoch"],
                                    self.input_count,
                                    getattr(self.backend, "active_turn", None),
                                    getattr(
                                        getattr(self, "conversation", None),
                                        "voice_revision",
                                        self.input_count,
                                    ),
                                ),
                                "voice-decision",
                            )
                    elif role == "assistant" and not self.backend.busy:
                        self.dialogue.replied()
                elif event["kind"] == "user_turn":
                    # Data-channel events can arrive seconds after their
                    # audio. A stale turn must not cut a newer answer.
                    if time.monotonic() - self.last_speech < 1:
                        self.player.manual_muted = False
                        await self.player.stop()
                    self.native_interruptions += 1
                    self.last_activity = time.monotonic()
                    log.info("native_user_turn playback_flushed")
                elif event["kind"] == "disconnected" and self.mode == "active" and not self.recovering:
                    self.recovering = True
                    self.mode = "reconnecting"
                    self.reconnect_task = self.context.spawn(self._reconnect(), "voice-reconnect")
            finally:
                self.events.task_done()

    async def _voice_decision(self, text, epoch, revision, turn, focus):
        from ..kernel.contracts import DecisionQuery
        from ..kernel.decisions import decide

        result = await decide(
            self.runtime,
            DecisionQuery(
                "voice_intent",
                f"voice-control:{self.backend.session_id}:{epoch}:{revision}",
                {"utterance": text, "execution_active": bool(turn)},
            ),
        )
        if (
            result["status"] != "ok"
            or not self.active
            or self.quiet_active()
            or self.voice.epoch != epoch
            or self.input_count != revision
            or getattr(getattr(self, "conversation", None), "voice_revision", self.input_count) != focus
        ):
            return
        choice = result["choice"]
        if choice in {"sleep", "stop", "chat"}:
            await self.control(Control(choice))
        elif (
            choice == "cancel"
            and turn
            and self.backend.active_turn == turn
            and any(word in text for word in ("任务", "执行"))
        ):
            await self.control(Control("cancel", turn))

    async def _reconnect(self):
        try:
            await self.player.stop()
            for delay in (1, 3, 8):
                await asyncio.sleep(delay)
                if not self.active or self.quiet_active():
                    return
                try:
                    async with self.transition:
                        await self.voice.open()
                    if not self.active or self.quiet_active():
                        return
                    self.mode = "active"
                    self.last_error = None
                    return
                except Exception as exc:
                    self.last_error = "Native reconnect failed: " + type(exc).__name__
            await self.sleep()
            await self._cue(error=True)
        finally:
            self.recovering = False

    async def control(self, cmd):
        if cmd.kind not in {"sleep", "stop", "cancel", "volume", "progress", "chat"}:
            raise ValueError(
                "Voice input/output are fixed by startup configuration; runtime switching is disabled"
            )
        self.last_activity = time.monotonic()
        if cmd.kind == "sleep":
            await self.sleep()
        elif cmd.kind == "chat":
            self.dialogue.continuous = True
            self.dialogue.user()
            await self.speak("好，我们聊聊。")
        elif cmd.kind == "stop":
            await self.player.stop()
            self.player.manual_muted = True

            async def notify_stop():
                try:
                    await self.voice.instruct(
                        "用户要求停止播报。现在保持安静，等用户下一次讲话后再回应；后台任务继续。刚才被截断的内容用户没有听完。"
                    )
                except Exception:
                    log.info("Stop applied locally; provider notification unavailable")

            self.context.spawn(notify_stop(), "notify-stop")
        elif cmd.kind == "cancel":
            await self.player.stop()
            cancelled = (
                await self.backend.cancel(expected_turn=cmd.value)
                if cmd.value is not None
                else await self.backend.cancel()
            )
            await self.speak("已请求取消当前任务。" if cancelled else "当前没有正在执行的任务。")
        elif cmd.kind == "volume":
            await self.route.set_volume(cmd.value)
            await self.speak("音量已调整。")
        elif cmd.kind == "progress":
            await self.speak(self.backend.progress or "当前没有正在执行的任务。")

    async def tool_control(self, args):
        action = args["action"]
        if action not in {"volume", "stop", "sleep"}:
            raise ValueError("Unsupported device action")
        await self.runtime.invoke(Control(action, args.get("volume")))
        return {"ok": True, "volume": self.route.volume, "active": self.active}

    def status(self):
        return {
            "assistant_name": self.config.assistant_name,
            "wake_words": self.config.wake_words,
            "quiet_hours": {"settings": self.config.quiet_hours, "active": self.quiet_active()},
            "wake_detector": self.recognizer.diagnostics(),
            "service": "running",
            "plugins": self.runtime.health(),
            "devices": self.devices.list(),
            "memory": {
                "ready": bool(self.memory and self.memory.ready),
                "requested_model": self.config.memory_model,
                "requested_reasoning_effort": self.config.memory_reasoning_effort,
                "model": self.backend.resolved_memory_model,
                "reasoning_effort": self.backend.resolved_memory_reasoning_effort,
                "pending_jobs": self.store.db.execute(
                    "SELECT count(*) FROM memory_jobs WHERE status IN ('pending','running')"
                ).fetchone()[0],
                "cached_facts": len(self.memory.cache) if self.memory else 0,
            },
            "session_id": getattr(self.backend, "session_id", None),
            **self.brain_plugin.diagnostics(),
            **self.audio_plugin.diagnostics(),
            "mode": self.mode,
            "voice_dialogue": {
                "mode": "continuous" if self.dialogue.continuous else "short",
                "follow_up_seconds": self.dialogue.follow_up_seconds,
            },
            "microphone_enabled": not self.no_mic,
            "capture_recent": self.last_frame is not None and time.monotonic() - self.last_frame < 3,
            "voice_io": {
                "input_device": self.route.input_device,
                "output_device": self.route.output_device,
                "locked": True,
            },
            "volume": self.route.volume,
            "task_running": self.backend.busy,
            "task_progress": self.backend.progress,
            "speaking": self.player.playing,
            "pending_speech": self.player.pending_voice_frames,
            "input_count": self.input_count,
            "native_user_turns": self.native_interruptions,
            "last_input": self.last_input,
            "wake_count": self.wake_count,
            "input_rms": self.input_rms,
            "error": self.last_error
            or self.route.last_error
            or self.player.last_error
            or self.voice.last_error
            or self.backend.last_error,
            "uptime_seconds": round(time.time() - self.started),
        }

    async def _maintenance(self):
        while True:
            await asyncio.sleep(1)
            await self._enforce_quiet()
            if (
                self.active
                and self.mode == "active"
                and self.dialogue.expired(
                    busy=self.backend.busy,
                    playing=self.player.playing,
                    last_output=self.last_output,
                    timeout=self.config.idle_seconds,
                )
            ):
                await self.sleep()
            try:
                await self.route.ensure()
            except (RuntimeError, TimeoutError, OSError) as exc:
                self.route.last_error = type(exc).__name__

    async def _record_for_client(self, reader, recording):
        async def operation():
            clip = await self.runtime.invoke(recording, timeout=recording.duration_seconds + 35)
            result = await self.conversation.recording_reply(clip)
            return {**result, "operation_id": recording.operation_id}

        return await self._operation_for_client(reader, operation())

    async def _operation_for_client(self, reader, operation):
        work = asyncio.create_task(operation, name="requested-capability")
        disconnected = asyncio.create_task(reader.read(1), name="capability-caller")
        try:
            done, _ = await asyncio.wait((work, disconnected), return_when=asyncio.FIRST_COMPLETED)
            if disconnected in done:
                raise ConnectionError("Caller disconnected; operation canceled, prior effects may be partial")
            return await work
        finally:
            for task in (work, disconnected):
                if not task.done() and not task.cancelling():
                    task.cancel()
            await asyncio.gather(work, disconnected, return_exceptions=True)

    async def _connection(self, reader, writer):
        try:
            d = json.loads(await asyncio.wait_for(reader.readline(), 5))
            action = d["command"]
            if action == "wake":
                await self.wake(source="cli")
            elif action in {"sleep", "stop", "cancel", "volume", "progress"}:
                await self.runtime.invoke(Control(action, d.get("value")))
            elif action == "gesture":
                await self.runtime.plugins["gesture"].simulate(d.get("value", "wave"))
            elif action == "devices":
                operation = {"operation_id": d["operation_id"]} if d.get("operation_id") else {}
                if d.get("action") == "snapshot":
                    capture = VideoCapture(d.get("device_id"), **operation)
                    frame = await self._operation_for_client(reader, self.runtime.invoke(capture))
                    result = {
                        **await self.conversation.capture_reply(frame),
                        "operation_id": capture.operation_id,
                    }
                elif d.get("action") == "record":
                    recording = VideoRecord(d.get("device_id"), d.get("duration_seconds", 10), **operation)
                    result = await self._record_for_client(reader, recording)
                elif d.get("action") in {"move", "stop-camera"}:
                    stopping = d["action"] == "stop-camera"
                    result = await self._operation_for_client(
                        reader,
                        self.runtime.invoke(
                            CameraMotion(
                                d.get("device_id"),
                                "stop" if stopping else d.get("direction"),
                                0 if stopping else d.get("duration_ms", 80),
                                **operation,
                            )
                        ),
                    )
                elif d.get("action") == "volume":
                    from ..kernel.contracts import AudioVolume

                    result = await self._operation_for_client(
                        reader,
                        self.runtime.invoke(AudioVolume(d.get("device_id"), d.get("percent"), **operation)),
                    )
                elif d.get("action") in {"operations", "cancel-operation"}:
                    from ..kernel.contracts import DeviceOperationQuery, DeviceOperationCancel

                    request_type = (
                        DeviceOperationCancel if d["action"] == "cancel-operation" else DeviceOperationQuery
                    )
                    result = await self.runtime.invoke(
                        request_type(d.get("device_id"), d.get("operation_id"))
                    )
                else:
                    result = await self.runtime.invoke(
                        DeviceQuery(d.get("action", "list"), d.get("device_id"))
                    )
                writer.write((json.dumps({"ok": True, "result": result}, ensure_ascii=False) + "\n").encode())
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return
            elif action == "media":
                from ..kernel.contracts import MediaRequest

                result = await self.runtime.invoke(
                    MediaRequest(d["action"], d.get("arguments", {}), d.get("caller_run")), timeout=90
                )
                writer.write((json.dumps({"ok": True, "result": result}, ensure_ascii=False) + "\n").encode())
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return
            elif action == "decisions":
                from ..kernel.contracts import DecisionRequest, DecisionQuery

                request = (
                    DecisionQuery(**d.get("arguments", {}), caller_run=d.get("caller_run"))
                    if d["action"] == "judge"
                    else DecisionRequest(d["action"], d.get("arguments", {}))
                )
                result = await self.runtime.invoke(request)
                writer.write((json.dumps({"ok": True, "result": result}, ensure_ascii=False) + "\n").encode())
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return
            elif action == "events":
                from ..kernel.contracts import EventRequest

                result = await self.runtime.invoke(
                    EventRequest(d["action"], d.get("arguments", {}), d.get("caller_run")), timeout=15
                )
                writer.write((json.dumps({"ok": True, "result": result}, ensure_ascii=False) + "\n").encode())
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return
            elif action == "notifications":
                from ..kernel.contracts import NotificationRequest

                result = await self.runtime.invoke(
                    NotificationRequest(d["action"], d.get("arguments", {}), d.get("caller_run")), timeout=15
                )
                writer.write((json.dumps({"ok": True, "result": result}, ensure_ascii=False) + "\n").encode())
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return
            elif action == "tasks":
                from ..kernel.contracts import TaskRequest

                result = await self.runtime.invoke(
                    TaskRequest(d["action"], d.get("arguments", {}), d.get("caller_run")), timeout=90
                )
                writer.write((json.dumps({"ok": True, "result": result}, ensure_ascii=False) + "\n").encode())
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return
            elif action == "mcp":
                from ..kernel.contracts import IntegrationRequest

                result = await self._operation_for_client(
                    reader,
                    self.runtime.invoke(
                        IntegrationRequest(d["action"], d.get("arguments", {}), d.get("caller_run")),
                        timeout=170,
                    ),
                )
                writer.write((json.dumps({"ok": True, "result": result}, ensure_ascii=False) + "\n").encode())
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return
            elif action == "memory":
                result = await self.runtime.invoke(
                    MemoryRequest(d["action"], d.get("arguments", {})), timeout=90
                )
                response = {"ok": True, "result": result}
                writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode())
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return
            elif action in {"say", "text", "inject-wav"}:
                await self.wake(source="cli:" + action)
                if self.connect_task:
                    await asyncio.shield(self.connect_task)
                if self.mode != "active":
                    raise RuntimeError(self.last_error or "Voice not connected")
                if action == "say":
                    await self.speak(d["text"])
                elif action == "text":
                    await self.voice.send_text(d["text"])
                else:
                    if not self.no_mic:
                        raise ValueError(
                            "Inject tests require run --no-mic to avoid mixing live microphone audio"
                        )
                    a = await asyncio.to_thread(
                        miniaudio.decode_file,
                        d["path"],
                        output_format=miniaudio.SampleFormat.SIGNED16,
                        nchannels=1,
                        sample_rate=16000,
                    )
                    pcm = a.samples.tobytes()
                    if len(pcm) > 60 * 32000:
                        raise ValueError("Test WAV must be at most 60 seconds")
                    for offset in range(0, len(pcm), 640):
                        self.voice.feed(pcm[offset : offset + 640])
                        self.last_activity = time.monotonic()
                        await asyncio.sleep(0.02)
            elif action == "shutdown":
                self.stop_event.set()
            elif action not in {"status", "history"}:
                raise ValueError("Unknown command")
            result = self.store.recent() if action == "history" else self.status()
            response = {"ok": True, "result": result}
        except Exception as exc:
            response = {"ok": False, "error": str(exc)[:200]}
        try:
            writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode())
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def run(self):
        server = None
        waiters = []
        try:
            self.config.socket.unlink(missing_ok=True)
            server = await asyncio.start_unix_server(
                self._connection, path=self.config.socket, limit=128 * 1024
            )
            os.chmod(self.config.socket, 0o600)
            for sig in (signal.SIGINT, signal.SIGTERM):
                asyncio.get_running_loop().add_signal_handler(sig, self.stop_event.set)
            await self.runtime.start()
            self.store.set("session_id", self.backend.session_id)
            waiters = [
                asyncio.create_task(self.stop_event.wait()),
                asyncio.create_task(self.runtime.fatal.wait()),
            ]
            log.info("service_ready backend=native microphone=%s standby", not self.no_mic)
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            if self.runtime.fatal.is_set():
                raise RuntimeError("Critical plugin failed; supervisor will restart without replay")
        finally:
            if server:
                server.close()
                await server.wait_closed()
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
            try:
                await self.sleep()
            finally:
                await self.runtime.close()
                self.config.socket.unlink(missing_ok=True)
                self.store.db.close()
                self.lock_file.close()
