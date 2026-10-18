"""Script-owned Mandarin cues and a separately leased, configured PipeWire output."""

import asyncio
import hashlib
import io
import json
import time
import wave

from assistant_runtime.devices.outputs import AudioOutputs
from assistant_runtime.plugins.pipewire.io import PipeWireEndpoint

READING = "今天上午天气不错，我准备整理书桌，然后看看窗外。"
POSITIONS = {
    "living_center": "客厅中央",
    "living_far": "客厅远处",
    "kitchen": "厨房",
    "study": "书房",
}

CUES = {
    "ready": "现在开始语音录入测试，不用操作键盘。请先关掉电视和音乐，保持安静，给你十秒准备。",
    "intro": "请用平时的音量和语速跟着提示说话，不碰麦克风。先测安静环境，再测日常背景声。",
    "quiet": "先测背景。请保持安静，不碰麦克风。",
    "living_center": "请到客厅中央，给你十五秒准备。",
    "living_far": "请到客厅远处，给你十五秒准备。",
    "kitchen": "请到厨房，给你十五秒准备。",
    "study": "请到书房，给你十五秒准备。",
    "read": "下一段不叫助手名字，只读一次：" + READING + "听下一条开始提示再说。",
    "wake_read": "下一段先叫助手名字，紧接着说："
    + READING
    + "整句只说一遍，留二十秒录音，听下一条开始提示再说。",
    "start": "两秒后，开始朗读。",
    "end": "本段结束。",
    "complete": "录入测试结束，请查看终端结果。",
    "noise": "接下来测日常背景声。请用别的设备播放平时的电视或音乐，保持日常音量，不含唤醒词。给你二十秒准备。",
    "restore_quiet": "请关闭刚才的背景声，恢复安静，给你十秒时间。",
}
RATE = 24000
VOICE_MODEL = "kokoro-int8-multi-lang-v1_1"
VOICE_ID = 46  # zf_079, Mandarin female; see the upstream speaker table.
VOICE_SPEED = 0.95
VOICE_REVISION = hashlib.sha256(
    json.dumps([CUES, VOICE_MODEL, VOICE_ID, VOICE_SPEED, RATE], ensure_ascii=False).encode()
).hexdigest()[:16]


def prompt_folder(state_dir):
    return state_dir / "audio-input-check" / "prompts" / VOICE_REVISION


def load_prompt(state_dir, cue, *, folder=None):
    if cue not in CUES:
        raise ValueError("Unknown audio input diagnostic cue")
    path = (folder if folder is not None else prompt_folder(state_dir)) / (cue + ".wav")
    try:
        with path.open("rb") as file:
            data = file.read(RATE * 2 * 25 + 4097)
    except FileNotFoundError as exc:
        raise RuntimeError("先准备提示音：python -m scripts.check_audio_input --prepare-guide") from exc
    if len(data) > RATE * 2 * 25 + 4096:
        raise ValueError("Diagnostic cue exceeds 25 seconds")
    try:
        with wave.open(io.BytesIO(data)) as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, RATE):
                raise ValueError("Diagnostic cue must be mono PCM16 at 24 kHz")
            count = audio.getnframes()
            if not 0 < count <= RATE * 25:
                raise ValueError("Diagnostic cue must be 0..25 seconds")
            pcm = audio.readframes(count)
            if len(pcm) != count * 2:
                raise ValueError("Truncated diagnostic cue")
            return pcm
    except (wave.Error, EOFError) as exc:
        raise ValueError("Invalid diagnostic cue WAV") from exc


class GuidePlayer:
    def __init__(self, config):
        self.state_dir = config.state_dir
        self.output = config.default_output
        spec = config.devices[self.output]
        if spec["adapter"] != "pipewire":
            raise RuntimeError("当前语音引导只支持 PipeWire 输出；其他输出请显式使用 --text-only。")
        self.outputs = AudioOutputs(config.devices)
        self.outputs.register("pipewire", self.output, PipeWireEndpoint(spec))

    async def play(self, cue, check_idle):
        await check_idle()
        transport = None
        try:
            pcm = await asyncio.to_thread(load_prompt, self.state_dir, cue)
            transport = await self.outputs.open(self.output, RATE, owner="audio-input-check")
            started = time.monotonic()
            next_check = started
            for offset in range(0, len(pcm), 960):
                if time.monotonic() >= next_check:
                    await check_idle()
                    next_check = time.monotonic() + 1
                await transport.write(pcm[offset : offset + 960].ljust(960, b"\0"))
                await asyncio.sleep(max(0, (offset + 960) / (RATE * 2) - (time.monotonic() - started)))
            # EOF lets pacat drain its own buffer. Closing the lease immediately
            # kills pacat and can truncate short spoken cues.
            process = transport.transport.process
            process.stdin.close()
            await asyncio.wait_for(process.wait(), 8)
            if process.returncode:
                raise RuntimeError("提示音播放进程失败。")
            await check_idle()
        finally:
            if transport is not None:
                await transport.close()

    async def close(self):
        await self.outputs.close()
