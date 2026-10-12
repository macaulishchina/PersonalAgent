"""Probe the Codex WebRTC transport with subscription auth and a synthetic WAV.

No API key, microphone, speaker playback, global config change or token extraction.
"""
import argparse
import asyncio
from fractions import Fraction
import json
from pathlib import Path
import time
import wave

import av
import numpy as np
import soxr
from aiortc import AudioStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription

from dell_voice.rpc import CodexClient


class FixtureTrack(AudioStreamTrack):
    def __init__(self, path):
        super().__init__()
        with wave.open(str(path), 'rb') as f:
            assert f.getsampwidth() == 2
            pcm = np.frombuffer(f.readframes(f.getnframes()), dtype='<i2')
            pcm = pcm.reshape(-1, f.getnchannels()).mean(axis=1).astype(np.float32)
            self.samples = soxr.resample(pcm, f.getframerate(), 48000).astype('<i2')
        self.offset = 0
        self.started = None
        self.sent = 0

    async def recv(self):
        if self.started is None:
            self.started = time.monotonic()
        await asyncio.sleep(max(0, self.started+self.offset/48000-time.monotonic()))
        # Let the peer complete setup before starting the synthetic speech.
        start = self.offset-48000
        data = np.zeros(960, dtype='<i2')
        if start >= 0 and start < len(self.samples):
            part = self.samples[start:start+960]
            data[:len(part)] = part
            self.sent += len(part)
        frame = av.AudioFrame.from_ndarray(data.reshape(1,-1), format='s16', layout='mono')
        frame.sample_rate = 48000
        frame.pts = self.offset
        frame.time_base = Fraction(1,48000)
        self.offset += 960
        return frame


async def main(args):
    client=CodexClient(cwd=str(Path(__file__).resolve().parents[1]))
    peer=RTCPeerConnection(RTCConfiguration(iceServers=[]))
    fixture=FixtureTrack(args.wav)
    peer.addTrack(fixture)
    channel=peer.createDataChannel('oai-events')
    output=[]
    tasks=[]
    report={'transport':'webrtc','auth':'existing Codex login','events':{},'errors':[], 'audio_input_samples':0,'output_frames':0,'nonzero_output_samples':0,'transcripts':[], 'wire_examples':{}}
    thread=None
    @channel.on('message')
    def message(raw):
        try:
            d=json.loads(raw); k=d.get('type','unknown')
            report['events'][k]=report['events'].get(k,0)+1
            if k not in report['wire_examples'] and k != 'session.started':
                report['wire_examples'][k]=d
        except (ValueError,TypeError):pass
    @peer.on('track')
    def track_received(track):
        async def consume():
            resampler=av.AudioResampler(format='s16',layout='mono',rate=24000)
            while True:
                frame=await track.recv()
                for item in resampler.resample(frame):
                    samples=item.to_ndarray().reshape(-1)
                    report['output_frames']+=1
                    report['nonzero_output_samples']+=int(np.count_nonzero(np.abs(samples.astype(np.int32))>100))
                    output.append(samples.tobytes())
        tasks.append(asyncio.create_task(consume()))
    try:
        await client.start()
        auth=(await client.request('account/read',{})).get('account') or {}
        report['auth']=auth.get('type')
        if auth.get('type')!='chatgpt':
            raise RuntimeError('Subscription-only probe requires ChatGPT login')
        r=await client.request('thread/start',{'cwd':str(Path(args.cwd).resolve()),'ephemeral':True,'sandbox':'danger-full-access' if args.task else 'read-only','approvalPolicy':'never'})
        thread=r['thread']['id']
        await peer.setLocalDescription(await peer.createOffer())
        await client.request('thread/realtime/start',{'threadId':thread,'version':'v3','outputModality':'audio',
            'transport':{'type':'webrtc','sdp':peer.localDescription.sdp},
            'prompt':'你是嘟嘟，中文语音助手。听到用户问题请简短回答。需要执行文件工作时委派给 Codex，收到真实结果后再告诉用户，不得假装完成。'})
        deadline=time.monotonic()+args.seconds
        while time.monotonic()<deadline:
            try:e=await asyncio.wait_for(client.events.get(),2)
            except TimeoutError:
                continue
            m,p=e.get('method',''),e.get('params',{})
            report['events'][m]=report['events'].get(m,0)+1
            if m=='thread/realtime/sdp':
                await peer.setRemoteDescription(RTCSessionDescription(sdp=p['sdp'],type='answer'))
            elif m=='thread/realtime/error':
                report['errors'].append(p.get('message','unknown'));break
            elif m=='thread/realtime/closed':
                report['close_reason']=p.get('reason');break
            elif m=='thread/realtime/transcript/done':
                report['transcripts'].append({k:p[k] for k in ['role','text']})
            elif m=='item/completed' and p.get('item',{}).get('type')=='commandExecution':
                report.setdefault('commands',[]).append({k:p['item'].get(k) for k in ['command','exitCode','aggregatedOutput']})
        report['peer_state']=peer.connectionState
    except Exception as exc:
        report['errors'].append(type(exc).__name__+': '+str(exc)[:300])
    finally:
        report['audio_input_samples']=fixture.sent
        if thread:
            try:await client.request('thread/realtime/stop',{'threadId':thread},timeout=3)
            except Exception:pass
        for t in tasks:t.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        await peer.close()
        await client.close()
    report['passed']=report['nonzero_output_samples']>5000 and fixture.sent>0 and not report['errors']
    if args.task:
        report['files']=[str(p) for p in Path(args.cwd).glob('*') if p.is_file()]
        report['task_passed']=any('语音助手测试成功' in Path(p).read_text() for p in report['files'])
    dest=Path(args.output);dest.parent.mkdir(parents=True,exist_ok=True)
    dest.with_suffix('.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    if output:
        with wave.open(str(dest.with_suffix('.wav')),'wb') as f:
            f.setparams((1,2,24000,0,'NONE','not compressed'));f.writeframes(b''.join(output))
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--wav',default='state/acceptance/wake.wav')
    p.add_argument('--output',default='state/acceptance/native-webrtc')
    p.add_argument('--seconds',type=int,default=20)
    p.add_argument('--cwd',default='state/acceptance/native-work')
    p.add_argument('--task',action='store_true')
    args=p.parse_args();Path(args.cwd).mkdir(parents=True,exist_ok=True)
    asyncio.run(main(args))
