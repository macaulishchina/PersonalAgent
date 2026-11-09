"""Dormant voice lifecycle. Kept separate from application assembly pending acceptance."""

import asyncio
import time
import logging
from assistant_runtime.api.contracts import AudioFrame, Control
from assistant_runtime.core.speech.commands import parse_command

log = logging.getLogger(__name__)


class VoiceLifecycle:
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
        import numpy as np

        interaction = getattr(self, "interaction", None)
        if interaction and (
            interaction.output.active or interaction.path == "command" and self.mode == "command"
        ):
            return
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
        interaction = getattr(self, "interaction", None)
        if interaction and self.mode == "command" and interaction.state.current:
            await interaction.feedback(interaction.state.current, "control", text)
            return
        if self.active and not self.quiet_active():
            await self.voice.say(text)

    async def notify_task(self, text):
        """Output-only notification; return transport receipt, never a read receipt."""
        if self.quiet_active() or self.active:
            return "deferred"
        async with self.transition:
            if self.active or self.quiet_active():
                return "deferred"
            interaction = getattr(self, "interaction", None)
            if interaction and interaction.ready:
                from assistant_runtime.api.speech import FeedbackIntent, RequestRef
                import uuid

                intent = FeedbackIntent(
                    RequestRef(uuid.uuid4().hex, 1), "brief", text[:240], time.monotonic() + 30
                )
                result = await interaction.output.say(intent, lambda: not self.active)
                return "played" if result == "completed" else result
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
                        self.voice.speech_completed
                        and self.last_output
                        and self.last_output >= started
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
        if getattr(self.voice, "available", True) is False:
            self.last_error = "Voice is not enabled; use the web conversation"
            return False
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
        self.native_pending.clear()
        self.context.spawn(self._cue(), "wake-cue")
        interaction = getattr(self, "interaction", None)
        if interaction and interaction.ready:
            self.dialogue.follow_up_seconds = interaction.settings.follow_up_seconds
            chunks = (
                tuple(c for c in self.input_buffer.frames if c.sequence <= self.capture_sequence)[
                    -len(preroll) :
                ]
                if preroll
                else ()
            )
            self.connect_task = self.context.spawn(
                self._connect_command(chunks, correlation), "command-connect"
            )
        else:
            self.connect_task = self.context.spawn(
                self._connect(tuple(preroll), correlation), "voice-connect"
            )
        log.info("wake_detected connecting_native_voice")
        return True

    async def _connect_command(self, chunks, correlation):
        try:
            await self.interaction.begin(chunks)
            if self.active and self.interaction.path == "command":
                self.mode = "command"
                self._record_wake("connected", correlation)
        except Exception as exc:
            self.last_error = "Command input unavailable: " + type(exc).__name__
            self.interaction.ready = False
            self.active = False
            self.mode = "standby"
            self._record_wake("failed", correlation, error=type(exc).__name__)
            await self._cue(error=True)

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
                for pcm in self.native_pending:
                    self.voice.feed(pcm)
                self.native_pending.clear()
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
                self.native_pending.clear()
                await self.voice.close()
                self.recognizer.reset_keyword()
                await self._cue(error=True)

    async def sleep(self):
        if self.mode == "closing":
            await self.standby_ready.wait()
            return
        self.active = False
        if getattr(self, "interaction", None):
            await self.interaction.end_attention()
        self.mode = "closing"
        self.standby_ready.clear()
        try:
            # Silence locally before memory work, provider shutdown or reconnect cleanup.
            if self.player is not None:
                await self.player.stop()
            if getattr(self, "memory", None):
                self.memory.enqueue()
            reconnect = getattr(self, "reconnect_task", None)
            if reconnect and reconnect is not asyncio.current_task() and not reconnect.done():
                reconnect.cancel()
                await asyncio.gather(reconnect, return_exceptions=True)
            self.recovering = False
            self.preroll.clear()
            self.native_pending.clear()
            self.input_buffer.clear()
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
                self.input_buffer.clear()
                self.preroll.clear()
                continue
            self.input_buffer.offer(pcm, self.last_frame, getattr(self.route, "raw_frame", None))

    async def _process_capture(self):
        while True:
            chunk, raw, interrupted = await self.input_buffer.queue.get()
            try:
                await self._process_frame(chunk, raw, interrupted)
            finally:
                self.input_buffer.queue.task_done()

    async def _process_frame(self, chunk, raw, interrupted):
        import numpy as np

        pcm = chunk.pcm
        self.capture_sequence = chunk.sequence
        if self.quiet_active():
            self.preroll.clear()
            return
        interaction = getattr(self, "interaction", None)
        if interrupted:
            self.preroll.clear()
            self.last_error = "Audio input interrupted; partial request discarded"
            if self.active:
                if interaction and self.mode == "command":
                    await interaction.gap()
                else:
                    await self.sleep()
                    await self._cue(error=True)
                return
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
        self.input_rms = round(float(np.sqrt(np.mean(samples * samples))), 5)
        if not self.active:
            self.preroll.append(pcm)
            wake_samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
            gaps = self.input_buffer.gaps
            keyword = await self.recognizer.detect(wake_samples)
            if self.input_buffer.gaps != gaps:
                self.recognizer.reset_keyword()
                return
            if keyword and not self.active:
                log.info("keyword_detected phrase=%s", keyword)
                await self.wake(tuple(self.preroll), source="keyword", keyword=keyword)
                self.preroll.clear()
        elif self.mode == "command":
            await interaction.accept(chunk)
        elif self.mode in {"connecting", "reconnecting"}:
            if interaction and interaction.ready and interaction.path == "command":
                if self.connect_task:
                    await asyncio.shield(self.connect_task)
                if self.mode == "command":
                    await interaction.accept(chunk)
            else:
                if len(self.native_pending) == self.native_pending.maxlen:
                    self.last_error = "Voice connection input buffer exhausted; request not replayed"
                    await self.sleep()
                    await self._cue(error=True)
                    return
                self.native_pending.append(pcm)
        elif self.mode == "active":
            try:
                self.voice.feed(pcm)
            except BufferError:
                self.last_error = "Voice input buffer exhausted; partial request discarded"
                await self.sleep()
                await self._cue(error=True)
                return
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
                if self.mode == "command":
                    continue  # Old native callbacks cannot dispatch a second command.
                if event["epoch"] != self.voice.epoch or not self.active or self.quiet_active():
                    continue
                if event["kind"] == "transcript":
                    role, text = event["role"], event["text"]
                    self.store.transcripts.append(role, text)
                    if role == "user":
                        self.dialogue.user()
                        self.last_input = text
                        self.backend.last_user_text = text
                        self.input_count += 1
                        self.last_activity = time.monotonic()
                        cmd = parse_command(text, self.config.wake_words)
                        from assistant_runtime.api.contracts import DecisionSample

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
                            self.config.plugin_settings.get("decisions", {}).get(
                                "external_data_consent", False
                            )
                            and 0 < len(text) <= 800
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
                                    self.voice_control.capture_turn()
                                    if hasattr(self, "voice_control")
                                    else None,
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
                elif event["kind"] == "input_gap":
                    self.last_error = "Voice input incomplete; please repeat the request"
                    await self.sleep()
                    await self._cue(error=True)
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
        from assistant_runtime.api.contracts import DecisionQuery
        from assistant_runtime.api.decisions import decide, report_decision

        result = await decide(
            self.runtime,
            DecisionQuery(
                "voice_intent",
                f"voice-control:{self.backend.session_id}:{epoch}:{revision}",
                {"utterance": text, "execution_active": bool(turn)},
                summary=text[:800],
                input_version=f"{epoch}:{revision}:{focus}",
            ),
        )
        if result["status"] != "ok":
            await report_decision(self.runtime, result, "rejected", "original_voice_route", turn)
            return
        if (
            not self.active
            or self.quiet_active()
            or self.voice.epoch != epoch
            or self.input_count != revision
            or self.conversation.voice_revision != focus
        ):
            await report_decision(self.runtime, result, "rejected", "stale_input", turn)
            return
        choice = result["choice"]
        if choice in {"sleep", "stop", "chat"}:
            await self.control(Control(choice))
            await report_decision(self.runtime, result, "adopted", "local_voice_control", turn)
        elif choice in {"correct", "supplement"}:
            await self.voice_control.offer(result, text, epoch, revision, turn, focus)
        elif choice in {"cancel", "takeover"}:
            if not hasattr(self, "voice_control"):
                # Minimal hosts without native coordination cannot claim scoped takeover.
                await report_decision(self.runtime, result, "rejected", "scope_unavailable", turn)
                return
            await self.player.stop()

            def current():
                return (
                    self.active
                    and self.voice.epoch == epoch
                    and self.input_count == revision
                    and self.conversation.voice_revision == focus
                )

            confirmed = await self.voice_control.stop(turn, current)
            if not current():
                await report_decision(self.runtime, result, "unknown", "input_changed_during_stop", turn)
                return
            await report_decision(
                self.runtime,
                result,
                "adopted" if confirmed else "unknown",
                "scoped_stop_confirmed" if confirmed else "stop_unconfirmed",
                turn,
            )
            await self.speak(
                "已停止后续操作，已经发出的动作可能已生效。"
                if confirmed
                else "还不能确认当前操作已停止，请先核对。"
            )
        else:
            await report_decision(self.runtime, result, "rejected", "native_handling", turn)

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
                    for pcm in self.native_pending:
                        self.voice.feed(pcm)
                    self.native_pending.clear()
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
        if cmd.kind not in {"sleep", "stop", "cancel", "takeover", "volume", "progress", "chat"}:
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
            if getattr(self, "interaction", None):
                await self.interaction.output.interrupt()
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
        elif cmd.kind == "takeover" or (
            cmd.kind == "cancel" and getattr(self, "active", False) and hasattr(self, "voice_control")
        ):
            owner = getattr(self, "voice_control", None)
            turn = cmd.value or (owner.capture_turn() if owner else None)
            epoch, revision = self.voice.epoch, self.input_count
            await self.player.stop()

            def current():
                return self.active and self.voice.epoch == epoch and self.input_count == revision

            stopped = await owner.stop(turn, current) if owner else False
            if current():
                await self.speak(
                    "已停止后续操作，已经发出的动作可能已生效。"
                    if stopped
                    else "还不能确认当前操作已停止，请先核对。"
                )
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
        return {"ok": True, "volume": self.route.volume if self.route else None, "active": self.active}

    async def _maintenance(self):
        while True:
            await asyncio.sleep(1)
            from assistant_runtime.api.lifecycle import configuration_boundary

            await configuration_boundary(self.context)
            await self._enforce_quiet()
            if (
                self.active
                and self.mode in {"active", "command"}
                and self.dialogue.expired(
                    busy=self.backend.busy,
                    playing=self.player.playing,
                    last_output=self.last_output,
                    timeout=self.config.idle_seconds,
                )
            ):
                await self.sleep()
            try:
                if self.route and self.config.input_device and self.config.default_output:
                    await self.route.ensure()
            except (RuntimeError, TimeoutError, OSError) as exc:
                self.route.last_error = type(exc).__name__
