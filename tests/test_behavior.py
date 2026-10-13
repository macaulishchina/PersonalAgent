import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from assistant_runtime.plugins.pipewire.audio import AudioRoute
from assistant_runtime.config import Config
from assistant_runtime.assistant.commands import parse_command
from assistant_runtime.store import Store
from assistant_runtime.plugins.codex.backend import CodexBackend


class CommandsTest(unittest.TestCase):
    def test_conversational_goodbyes_end_the_session_without_cancelling_work(self):
        for text in ["先这样吧", "好了，先这样吧，回头再聊。", "今天就到这里", "下次再聊"]:
            self.assertEqual(parse_command(text).kind, "sleep", text)
        for text in ["不要结束聊天", "解释一下先这样吧的意思", "谢谢", "好的", "文件先这样吧，继续下一步"]:
            self.assertIsNone(parse_command(text), text)

    def test_stop_does_not_cancel_task(self):
        self.assertEqual(parse_command("停一下。").kind, "stop")
        self.assertEqual(parse_command("取消当前任务").kind, "cancel")

    def test_content_about_commands_is_not_executed(self):
        for text in [
            "不要取消任务",
            "给我解释一下结束对话是什么意思",
            "把文件里的客厅改成书房",
            "不要把音量调到最大",
        ]:
            self.assertIsNone(parse_command(text), text)

    def test_chinese_volume_and_rooms(self):
        self.assertEqual(parse_command("请把音量调到百分之四十。").value, 40)
        self.assertEqual(parse_command("切换到书房音箱", {"study": "书房"}).value, "study")
        self.assertIsNone(parse_command("音量调到百分之一百二十"))


class PersistenceTest(unittest.TestCase):
    def test_unknown_dispatch_survives_restart_without_replay(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            a = Store(path)
            a.create_task("one", "write a file")
            a.update_task("one", "unknown")
            a.set("room", "study")
            a.db.close()
            b = Store(path)
            self.assertEqual(b.unfinished(), [("one", None, "unknown")])
            self.assertEqual(b.get("room"), "study")
            self.assertEqual(len(b.recent()), 1)
            b.db.close()


class TaskTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_terminates_only_the_current_tasks_shells(self):
        b = CodexBackend(Config(), None, AsyncMock(), AsyncMock())
        b.thread_id, b.active_turn, b.last_turn = "thread", "current", "current"
        b.command_items = {"current": {"current-shell"}, "older": {"older-server"}}
        b.client.request = AsyncMock(
            side_effect=[
                {},
                {
                    "data": [
                        {"itemId": "current-shell", "processId": "one"},
                        {"itemId": "older-server", "processId": "two"},
                    ]
                },
                {},
            ]
        )
        self.assertTrue(await b.cancel())
        self.assertEqual(
            [x.args[0] for x in b.client.request.await_args_list],
            ["turn/interrupt", "thread/backgroundTerminals/list", "thread/backgroundTerminals/terminate"],
        )
        self.assertEqual(b.client.request.await_args_list[-1].args[1]["processId"], "one")

    async def test_stopping_voice_does_not_interrupt_codex(self):
        b = CodexBackend.__new__(CodexBackend)
        b.active_turn = "t1"
        b.muted_turns = set()
        b.mute_current()
        self.assertEqual(b.active_turn, "t1")
        self.assertIn("t1", b.muted_turns)


def device_config(**kw):
    return Config(
        input_device="living_microphone",
        default_output="living",
        devices={
            "living_microphone": {
                "adapter": "pipewire",
                "kind": "microphone",
                "label": "客厅麦克风",
                "connection": {"match": "UGREEN_Camera_2K"},
            },
            "living": {
                "adapter": "pipewire",
                "kind": "speaker",
                "label": "客厅音箱",
                "connection": {"address": "4C:C6:4C:97:78:4A"},
            },
            "study": {
                "adapter": "pipewire",
                "kind": "speaker",
                "label": "书房音箱",
                "connection": {"address": "8C:53:C3:7F:18:7C"},
            },
        },
        **kw,
    )


class DeviceTest(unittest.IsolatedAsyncioTestCase):
    async def test_crash_cleanup_only_unloads_owned_modules_in_reverse_order(self):
        route = AudioRoute(device_config(), SimpleNamespace(get=lambda key, default: default))
        listing = "1\tmodule-null-sink\tsink_name=unrelated\n536870913\tmodule-null-sink\tsink_name=personal_agent_echo_output\n536870914\tmodule-loopback\tsource=personal_agent_echo_output.monitor\n"
        with patch(
            "assistant_runtime.plugins.pipewire.audio.command",
            new_callable=AsyncMock,
            side_effect=[listing, "", ""],
        ) as command:
            await route._clean_stale_modules()
            self.assertEqual(
                [c.args for c in command.await_args_list[1:]],
                [("pactl", "unload-module", "536870914"), ("pactl", "unload-module", "536870913")],
            )

    async def test_volume_floor_preserves_loud_channels_and_skips_unknown_values(self):
        scenarios = [
            ([13107, 13107], ["26214", "26214"], 40),
            ([49152, 49152], None, 75),
            ([26214, 26214], None, 40),
            ([13107, 49152], ["26214", "49152"], 57),
            ([], None, None),
        ]
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "state.db")
            store.set("volume", 10)  # Old preferences must never lower real volume.
            route = AudioRoute(device_config(minimum_volume=40), store)
            route.physical_sink = "living-speaker"
            for values, changed, observed in scenarios:
                data = [
                    {
                        "name": "living-speaker",
                        "mute": True,
                        "volume": {str(i): {"value": v} for i, v in enumerate(values)},
                    }
                ]
                with patch(
                    "assistant_runtime.plugins.pipewire.audio.command",
                    new_callable=AsyncMock,
                    return_value=json.dumps(data),
                ) as command:
                    await route.ensure_minimum_volume()
                    self.assertEqual(route.volume, observed)
                    self.assertEqual(command.await_count, 2 if changed else 1)
                    if changed:
                        self.assertEqual(
                            command.await_args_list[-1].args,
                            ("pactl", "set-sink-volume", "living-speaker", *changed),
                        )
            for failure in [RuntimeError("Unavailable"), ValueError("Invalid output")]:
                with patch(
                    "assistant_runtime.plugins.pipewire.audio.command",
                    new_callable=AsyncMock,
                    side_effect=failure,
                ) as command:
                    await route.ensure_minimum_volume()
                    self.assertIsNone(route.volume)
                    self.assertEqual(command.await_count, 1)
            with patch("assistant_runtime.plugins.pipewire.audio.command", new_callable=AsyncMock) as command:
                await route.set_volume(0)
                self.assertEqual(
                    command.await_args_list[0].args, ("pactl", "set-sink-mute", "living-speaker", "1")
                )
                self.assertEqual(command.await_count, 2)
            store.db.close()

    async def test_missing_target_speaker_never_uses_other_room_and_keeps_mic(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "state.db")
            route = AudioRoute(device_config(), store)
            source = "alsa_input.usb_UGREEN_Camera_2K"
            route.devices = AsyncMock(
                return_value=([{"name": source}], [{"name": "bluez_output.8C_53_C3_7F_18_7C.1"}])
            )
            with patch("assistant_runtime.plugins.pipewire.audio.command", new_callable=AsyncMock) as command:
                with self.assertRaisesRegex(RuntimeError, "living speaker unavailable"):
                    await route.ensure()
                self.assertEqual(route.source, source)
                self.assertIsNone(route.sink)
                self.assertFalse(route.ready.is_set())
                command.assert_awaited_once_with("bluetoothctl", "connect", "4C:C6:4C:97:78:4A", timeout=8)
            store.db.close()
