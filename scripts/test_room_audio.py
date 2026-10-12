"""Short, audible room tests, explicitly recording the UGREEN microphone.

Stop the installed service first. Recordings stay in the private state directory.
Compare original PCM, sink monitor, raw room microphone and AEC output.
"""
import argparse
import asyncio
import json
from pathlib import Path
import time
import wave

import numpy as np
import soxr

from dell_voice.audio import AudioRoute, audio_env, command
from dell_voice.config import Config
from dell_voice.recognition import Recognizer
from dell_voice.store import Store
from dell_voice.stream_audio import StreamPlayer

ROOT=Path(__file__).resolve().parents[1]


def wav(path, pcm, rate):
    with wave.open(str(path),'wb') as f:
        f.setparams((1,2,rate,0,'NONE','not compressed'));f.writeframes(pcm)


def envelope(pcm,rate=16000):
    a=np.frombuffer(pcm,dtype='<i2').astype(float)
    n=rate//100
    a=a[:len(a)//n*n].reshape(-1,n)
    return np.sqrt(np.mean(a*a,axis=1))


async def record(source, result, times):
    p=await asyncio.create_subprocess_exec('pacat','--record','--raw','--format=s16le',
        '--rate=16000','--channels=1','--latency-msec=20',f'--device={source}',
        env=audio_env(),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
    try:
        while True:
            b=await p.stdout.readexactly(320)
            result.extend(b);times.append(time.monotonic())
    finally:
        if p.returncode is None:p.kill();await p.wait()


async def main(args):
    config=Config.load();config.echo_cancel=args.aec
    store=Store(config.state_dir/'assistant.sqlite3')
    route=AudioRoute(config,store)
    dest=config.state_dir/'room-tests'/f'{int(time.time())}-{args.kind}-{"aec" if args.aec else "direct"}'
    dest.mkdir(parents=True)
    player=StreamPlayer(route);tasks=[];report={}
    try:
        await route.ensure()
        if args.external_stimulus:
            # Play a simulated user outside the assistant's AEC reference path.
            route.sink=route.physical_sink
        sources,_=await route.devices()
        raw=next(x['name'] for x in sources if config.source_match in x['name'] and not x['name'].endswith('.monitor'))
        devices={'raw':raw,'monitor':route.sink+'.monitor'}
        recordings={name:bytearray() for name in devices};times={name:[] for name in devices}
        tasks=[asyncio.create_task(record(source,recordings[name],times[name])) for name,source in devices.items()]
        if args.aec:
            recordings['aec']=bytearray()
            async def processed():
                async for pcm in route.frames():recordings['aec'].extend(pcm)
            tasks.append(asyncio.create_task(processed()))
        run=asyncio.create_task(player.run());tasks.append(run)
        if args.kind=='tone':
            t=np.arange(24000*8)/24000
            # Continuous voiced-band signal with identifiable changing frequency.
            pcm=(np.sin(2*np.pi*(500*t+35*t*t))*2500).astype('<i2').tobytes()
        else:
            path=ROOT/'state/acceptance'/('wake.wav' if args.kind=='wake' else 'native-webrtc-task-final.wav')
            with wave.open(str(path)) as f:
                a=np.frombuffer(f.readframes(f.getnframes()),dtype='<i2').astype(float)
                a=soxr.resample(a,f.getframerate(),24000)
            if args.kind=='speech':
                active=np.flatnonzero(np.abs(a)>150)
                a=a[max(0,active[0]-2400):min(len(a),active[-1]+12000)]
                a=a[:24000*15]
            pcm=a.astype('<i2').tobytes()
        if args.repeat>1:
            pcm=(pcm+bytes(24000))*args.repeat
        wav(dest/'reference.wav',pcm,24000)
        await asyncio.sleep(.7)
        start=time.monotonic()
        for i,offset in enumerate(range(0,len(pcm),960)):
            await asyncio.sleep(max(0,start+i*.02-time.monotonic()))
            player.feed(pcm[offset:offset+960])
        # Preserve the last frame and maintain a clocked stream through its tail.
        for _ in range(25):
            player.feed(bytes(960));await asyncio.sleep(.02)
        await player.queue.join()
        await asyncio.sleep(2)
        for name,data in recordings.items():
            wav(dest/f'{name}.wav',data,16000)
            env=envelope(data)
            report[name]={'seconds':len(data)/32000,'rms':round(float(np.sqrt(np.mean(env**2))),2),
                'peak_window_rms':round(float(env.max()),2),'baseline_rms':round(float(np.median(env[:40])),2)}
            if args.kind=='tone':
                threshold=max(3*np.median(env[:40]),.2*np.percentile(env,80))
                active=np.flatnonzero(env>threshold)
                if len(active):
                    mid=env[active[0]+20:active[-1]-20]
                    report[name].update(span=round((active[-1]-active[0]+1)*.01,3),
                        low_windows_ms=int((mid<threshold).sum()*10))
            if args.kind in {'wake','speech'} and name!='monitor':
                recognizer=Recognizer(config);speech_edges=0;was=False;wakes=[]
                a=np.frombuffer(data,dtype='<i2').astype(np.float32)/32768
                for offset in range(0,len(a)-512,512):
                    frame=a[offset:offset+512]
                    word=recognizer.keyword(frame)
                    if word:wakes.append(round(offset/16000,3))
                    recognizer.vad.accept_waveform(frame)
                    voiced=recognizer.vad.is_speech_detected()
                    speech_edges+=int(voiced and not was);was=voiced
                    while not recognizer.vad.empty():recognizer.vad.pop()
                report[name].update(vad_starts=speech_edges,wakes=wakes)
        report.update(kind=args.kind,echo_cancel=args.aec,recordings=str(dest),
            duration=len(pcm)/48000,max_queue_ms=round(player.max_queue_ms,1),
            discarded_bytes=player.discarded_bytes,error=player.last_error)
        (dest/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
    finally:
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        await route.close();store.db.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--aec',action='store_true')
    parser.add_argument('--kind',choices=['tone','speech','wake'],default='tone')
    parser.add_argument('--external-stimulus',action='store_true')
    parser.add_argument('--repeat',type=int,default=1)
    asyncio.run(main(parser.parse_args()))
