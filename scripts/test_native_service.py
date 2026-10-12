"""Real subscription voice + local devices, driven by synthetic mic samples.

Stop the installed service first to avoid two processes owning the audio route.
No API key. Does play test speech on the selected room speaker.
"""
import asyncio
import json
import logging
from pathlib import Path
import time
import wave

from dell_voice.config import Config
from dell_voice.native_service import NativeService
from dell_voice.commands import Command

ROOT=Path(__file__).resolve().parents[1]


async def main():
    root=ROOT/'state'/f'n{int(time.time())}'  # Unix sockets have a 108-byte path limit
    root.mkdir(parents=True)
    (root/'models').symlink_to(ROOT/'models',target_is_directory=True)
    work=root/'work';work.mkdir()
    config=Config.load();config.backend='native';config.root=root;config.workspace=str(work)
    service=NativeService(config,no_mic=True)
    incoming=asyncio.Queue()
    async def frames():
        while True:
            try:pcm=incoming.get_nowait()
            except asyncio.QueueEmpty:pcm=bytes(1024)
            yield pcm
            await asyncio.sleep(.032)
    service.route.frames=frames
    run=asyncio.create_task(service.run())
    capture=None
    report={}
    async def wait(predicate,timeout=40):
        deadline=time.monotonic()+timeout
        while not predicate():
            if run.done():run.result();raise RuntimeError('Service stopped')
            if service.last_error:raise RuntimeError(service.last_error)
            if time.monotonic()>deadline:raise TimeoutError('Condition timed out: '+json.dumps(service.status(),ensure_ascii=False))
            await asyncio.sleep(.1)
    async def inject(name):
        with wave.open(str(ROOT/'state/acceptance'/f'{name}.wav'),'rb') as f:
            assert f.getframerate()==16000 and f.getnchannels()==1
            pcm=f.readframes(f.getnframes())
        for offset in range(0,len(pcm),1024):
            incoming.put_nowait(pcm[offset:offset+1024].ljust(1024,b'\0'))
        await wait(lambda:incoming.empty(),timeout=40)
        return time.monotonic()
    try:
        await wait(lambda:config.socket.exists())
        capture=asyncio.create_task(service._capture())
        await inject('file')
        await asyncio.sleep(1)
        report['standby_ignored_non_wake']=not service.active and not service.voice.opened
        assert report['standby_ignored_non_wake']
        end=await inject('wake')
        await wait(lambda:service.mode=='active')
        await wait(lambda:service.input_count>0)
        await wait(lambda:service.last_output and service.last_output>end)
        report['question_end_to_first_model_audio_seconds']=round(service.last_output-end,3)
        await asyncio.sleep(4)
        report['question_status']=service.status()
        assert service.player.written_bytes>24000
        print('Native wake, question, and speaker output passed',flush=True)
        count=service.input_count
        end=await inject('wake')
        await wait(lambda:service.input_count>count)
        await wait(lambda:service.last_output and service.last_output>end)
        report['warm_question_end_to_model_audio_seconds']=round(service.last_output-end,3)
        await asyncio.sleep(3)
        await inject('file')
        await wait(lambda:service.backend.busy or any(work.glob('*.txt')))
        await wait(lambda:not service.backend.busy and any(work.glob('*.txt')),timeout=100)
        report['file_task']=any('语音助手测试成功' in p.read_text() for p in work.glob('*.txt'))
        assert report['file_task']
        print('Native voice task created a real file',flush=True)
        await service.voice.say('这是打断测试，我会继续说一段话。现在正在验证你开口后能否及时停止播报。')
        await wait(lambda:service.player.playing)
        await inject('stop')
        await wait(lambda:service.player.manual_muted)
        await asyncio.sleep(1)
        report['voice_stop']=not service.player.playing
        assert report['voice_stop']
        report['software_stop_seconds']=service.player.last_stop_seconds
        await service.sleep()
        count=service.input_count
        await inject('file')
        await asyncio.sleep(.5)
        report['sleep_gate']=not service.active and service.input_count==count
        assert report['sleep_gate']
        report['passed']=True
    except Exception as exc:
        report['passed']=False
        report['error']=str(exc)
        raise
    finally:
        if capture:
            capture.cancel();await asyncio.gather(capture,return_exceptions=True)
        report['final_status']=service.status()
        (ROOT/'state/acceptance/native-service.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
        service.stop_event.set()
        await asyncio.wait_for(run,20)


if __name__=='__main__':
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
