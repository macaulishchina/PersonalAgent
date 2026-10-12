"""Measure the local PCM path, without microphone capture or model requests.

Default: isolated null sink (inaudible). --speaker: selected real speaker/AEC.
Only generated sine waves are captured from the output monitor, never the mic.
"""
import argparse
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from dell_voice.audio import AudioRoute, audio_env, command
from dell_voice.config import Config
from dell_voice.store import Store
from dell_voice.stream_audio import StreamPlayer

ROOT = Path(__file__).resolve().parents[1]


async def measure(player_class, sink, label, route=None):
    captured = bytearray()
    stderr = bytearray()
    tasks = []
    create = asyncio.create_subprocess_exec

    async def collect(reader, target):
        while data := await reader.read(8192):
            target.extend(data)

    async def spawn(*args, **kwargs):
        if '--playback' in args:
            kwargs['stderr'] = asyncio.subprocess.PIPE
            args = (*args, '--verbose')
        p = await create(*args, **kwargs)
        if '--playback' in args:
            tasks.append(asyncio.create_task(collect(p.stderr, stderr)))
        return p

    if route is None:
        async def ensure(**kwargs): pass
        route = SimpleNamespace(sink=sink, ensure=ensure)
    player = player_class(route)
    rec = await create('pacat', '--record', '--raw', '--format=s16le', '--rate=24000',
                       '--channels=1', '--latency-msec=20', f'--device={sink}.monitor',
                       env=audio_env(), stdout=asyncio.subprocess.PIPE,
                       stderr=asyncio.subprocess.DEVNULL)
    tasks.append(asyncio.create_task(collect(rec.stdout, captured)))
    run = None
    try:
        await asyncio.sleep(.25)
        with patch('asyncio.create_subprocess_exec', spawn):
            run = asyncio.create_task(player.run())
            # Delayed arrivals model scheduling/network jitter; source media
            # timestamps still represent exactly six seconds of continuous audio.
            pcm = (np.sin(np.arange(24000*6)*2*np.pi*440/24000)*1000).astype('<i2').tobytes()
            loop = asyncio.get_running_loop()
            start = loop.time()
            for i, offset in enumerate(range(0, len(pcm), 960)):
                jitter = .035 if i % 17 == 16 else 0
                await asyncio.sleep(max(0, start+i*.02+jitter-loop.time()))
                player.feed(pcm[offset:offset+960])
            await asyncio.wait_for(player.queue.join(), 5)
            await asyncio.sleep(.4)
    finally:
        if run:
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
        rec.terminate()
        await rec.wait()
        await asyncio.gather(*tasks, return_exceptions=True)
    samples = np.frombuffer(captured, dtype='<i2').astype(float)
    frames = samples[:len(samples)//240*240].reshape(-1,240)
    rms = np.sqrt(np.mean(frames*frames, axis=1))
    audible = np.flatnonzero(rms > 100)
    if not len(audible):
        raise RuntimeError('No output captured')
    middle = rms[audible[0]+3:audible[-1]-2]
    quiet = middle < 100
    runs = np.diff(np.r_[False, quiet, False].astype(int))
    lengths = np.flatnonzero(runs == -1) - np.flatnonzero(runs == 1)
    result = {
        'label':label, 'sink':sink, 'source_seconds':6,
        'output_span_seconds':round((audible[-1]-audible[0]+1)*.01,3),
        'internal_quiet_ms':int(quiet.sum()*10),
        'gaps_at_least_20ms':int((lengths>=2).sum()),
        'pacat_underruns':stderr.decode(errors='replace').lower().count('underrun'),
        'tone_rms_ratio':round(float(np.median(middle[middle>100])/(1000/2**.5)),3),
        'written_bytes':player.written_bytes, 'discarded_bytes':player.discarded_bytes,
        'error':player.last_error,
        'max_local_queue_ms':round(getattr(player,'max_queue_ms',0),1),
    }
    print(json.dumps(result,ensure_ascii=False),flush=True)
    return result


async def main(args):
    route = store = None
    module = None
    try:
        if args.speaker:
            config=Config.load()
            store=Store(config.state_dir/'assistant.sqlite3')
            route=AudioRoute(config,store)
            await route.ensure()
            sink=route.sink
        else:
            sink='dudu_diagnostic_output'
            module=await command('pactl','load-module','module-null-sink',
                                 f'sink_name={sink}','rate=24000','channels=1')
        cls=StreamPlayer
        if args.before:
            spec=importlib.util.spec_from_file_location('dell_voice.old_stream',ROOT/'state/diagnostics/stream_audio_before.py')
            old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
            cls=old.StreamPlayer
        result=await measure(cls,sink,'before' if args.before else 'after',route)
        path=ROOT/'state/diagnostics'/f'playback-{result["label"]}{"-speaker" if args.speaker else ""}.json'
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    finally:
        if route: await route.close()
        if store: store.db.close()
        if module: await command('pactl','unload-module',module)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before',action='store_true')
    parser.add_argument('--speaker',action='store_true',help='Audible test. Stop service first.')
    asyncio.run(main(parser.parse_args()))
