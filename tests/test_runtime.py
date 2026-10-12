import asyncio
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from assistant_runtime.kernel.contracts import Control, Event, Trigger, AudioFrame
from assistant_runtime.kernel.media import AudioStream
from assistant_runtime.kernel.runtime import Runtime, Manifest
from assistant_runtime.plugins.gesture import GesturePlugin
from assistant_runtime.store import Store


class Plugin:
    manifest = Manifest("test")

    async def start(self, context):
        self.context = context
        context.register(Control, self.control)

    async def control(self, request):
        return request.kind


class RuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_slow_subscriber_does_not_block_others(self):
        r = Runtime(None)
        slow = r.subscribe("slow", 1)
        fast = r.subscribe("fast", 2)
        r.publish(Event("first", "test"))
        r.publish(Event("second", "test"))
        self.assertEqual(slow.get_nowait().kind, "second")
        self.assertEqual(fast.get_nowait().kind, "first")
        self.assertEqual(r.dropped["slow"], 1)

    async def test_request_timeout_and_stale_completion(self):
        r = Runtime(None)
        p = Plugin()
        r.add(p)
        await r.start()
        self.assertEqual(await r.invoke(Control("stop")), "stop")
        barrier = asyncio.Event()

        async def slow(request):
            await barrier.wait()
            return "old"

        r.handlers[Control] = ("test", slow)
        with self.assertRaises(TimeoutError):
            await r.invoke(Control("stop"), timeout=0.01)
        task = asyncio.create_task(r.invoke(Control("stop")))
        await asyncio.sleep(0)
        p.context.generation += 1
        barrier.set()
        with self.assertRaisesRegex(RuntimeError, "Stale"):
            await task
        await r.close()

    async def test_plugin_failure_isolated_and_resources_closed(self):
        r = Runtime(None)
        p = Plugin()
        r.add(p)
        await r.start()
        closed = AsyncMock()
        p.context.own(closed)

        async def fail():
            raise RuntimeError("device offline")

        p.context.spawn(fail())
        await asyncio.sleep(0.01)
        self.assertEqual(r.health()["test"]["state"], "degraded")
        self.assertFalse(r.fatal.is_set())
        self.assertEqual(await r.invoke(Control("stop")), "stop")
        await r.close()
        closed.assert_awaited_once()

    async def test_dependencies_reject_cycles_and_duplicate_handlers(self):
        r = Runtime(None)
        p = Plugin()
        p.manifest = Manifest("test", ("missing",))
        r.add(p)
        with self.assertRaises(ValueError):
            await r.start()
        r = Runtime(None)
        r.register("x", Control, AsyncMock())
        with self.assertRaises(ValueError):
            r.register("y", Control, AsyncMock())

    async def test_gesture_uses_trigger_contract_without_provider_changes(self):
        r = Runtime(None)
        p = Plugin()
        g = GesturePlugin()
        r.add(p)
        r.add(g)
        await r.start()
        trigger = AsyncMock(return_value={"accepted": True})
        p.context.register(Trigger, trigger)
        await g.simulate("wave")
        trigger.assert_awaited_once_with(Trigger("gesture", "wake"))
        with self.assertRaises(ValueError):
            await g.simulate("unconfigured")
        await r.close()

    async def test_media_old_generation_dropped_and_capacity_bounded(self):
        stream = AudioStream(1)
        self.assertTrue(stream.put(AudioFrame(bytes(640), 16000, 0)))
        stream.put(AudioFrame(bytes(640), 16000, 0))
        self.assertEqual(stream.dropped, 1)
        stream.reset()
        self.assertFalse(stream.put(AudioFrame(bytes(640), 16000, 0)))
        self.assertTrue(stream.queue.empty())

    async def test_logical_provider_mapping_stable_without_task_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "db"
            s = Store(path)
            ident = s.logical_session("codex", "opaque-thread")
            s.create_task("task", "side effect")
            s.update_task("task", "unknown")
            s.db.close()
            s = Store(path)
            self.assertEqual(s.logical_session("codex", "opaque-thread"), ident)
            self.assertEqual(s.unfinished(), [("task", None, "unknown")])
            s.db.close()

    async def test_stop_does_not_wait_for_device_reconnect(self):
        import numpy as np
        from assistant_runtime.plugins.pipewire.stream_audio import StreamPlayer

        waiting = asyncio.Event()
        release = asyncio.Event()

        async def ensure(**kw):
            waiting.set()
            await release.wait()

        player = StreamPlayer(SimpleNamespace(ensure=ensure))
        task = asyncio.create_task(player.run())
        player.feed(np.full(480, 1000, dtype="<i2").tobytes())
        await asyncio.wait_for(waiting.wait(), 1)
        await asyncio.wait_for(player.stop(), 0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_old_plugin_generation_event_cannot_escape_after_close(self):
        r = Runtime(None)
        p = Plugin()
        r.add(p)
        await r.start()
        q = r.subscribe("observer")
        old = p.context.generation
        await r.close()
        r.publish(Event("old", "test", generation=old))
        self.assertTrue(q.empty())
