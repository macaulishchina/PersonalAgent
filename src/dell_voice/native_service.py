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

from .audio import AudioRoute
from .backend import CodexBackend
from .commands import Command, normalize, parse_command
from .native import NativeVoice
from .recognition import Recognizer
from .store import Store
from .stream_audio import StreamPlayer

log = logging.getLogger(__name__)


class NativeService:
    def __init__(self, config, no_mic=False):
        self.config, self.no_mic = config, no_mic
        config.state_dir.mkdir(parents=True,exist_ok=True)
        os.chmod(config.state_dir,0o700)
        self.lock_file=(config.state_dir/'service.lock').open('w')
        fcntl.flock(self.lock_file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        self.store=Store(config.state_dir/'assistant.sqlite3')
        self.route=AudioRoute(config,self.store)
        self.recognizer=Recognizer(config)
        self.player=StreamPlayer(self.route)
        self.backend=CodexBackend(config,self.store,self.speak,self.tool_control)
        self.events=asyncio.Queue()
        self.voice=NativeVoice(config,self.backend,self.audio_received,self.events.put)
        self.backend.on_realtime=self.voice.notification
        self.stop_event=asyncio.Event()
        self.active=False
        self.mode='standby'
        self.started=time.time()
        self.last_activity=time.monotonic()
        self.last_frame=None
        self.last_input=''
        self.input_count=self.wake_count=0
        self.last_error=None
        self.connect_task=None
        self.preroll=deque(maxlen=50)  # approximately one second, memory only
        self.tasks=[]
        self.transition=asyncio.Lock()
        self.last_wake=None
        self.last_output=None
        self.recovering=False
        self.input_rms=0
        self.native_interruptions=0
        self.last_speech=0.0

    def audio_received(self, pcm):
        if not self.active:
            return
        self.player.feed(pcm)
        if np.max(np.abs(np.frombuffer(pcm,dtype='<i2').astype(np.int32)),initial=0)>150:
            self.last_output=time.monotonic()
            self.last_activity=self.last_output

    async def speak(self,text):
        if self.active:
            await self.voice.say(text)

    async def wake(self,preroll=()):
        if self.active:
            return
        self.active=True
        self.mode='connecting'
        self.last_activity=self.last_wake=time.monotonic()
        self.last_error=None
        self.wake_count+=1
        self.player.manual_muted=False
        await self.player.cue()
        self.connect_task=asyncio.create_task(self._connect(tuple(preroll)))
        log.info('wake_detected connecting_native_voice')

    async def _connect(self,preroll):
        async with self.transition:
            try:
                await self.voice.open(preroll)
                if not self.active:
                    await self.voice.close()
                    return
                self.mode='active'
                self.last_error=None
                log.info('native_voice_ready auth=chatgpt transport=webrtc')
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error='Native voice startup failed: '+type(exc).__name__+': '+str(exc)[:180]
                log.warning('%s',self.last_error)
                self.active=False
                self.mode='standby'
                self.recognizer.reset_keyword()
                await self.player.cue(error=True)

    async def sleep(self):
        self.active=False
        self.mode='standby'
        self.preroll.clear()
        self.recognizer.reset_keyword()
        if self.connect_task and not self.connect_task.done():
            self.connect_task.cancel()
            await asyncio.gather(self.connect_task,return_exceptions=True)
        await self.player.stop()
        async with self.transition:
            await self.voice.close()
        log.info('session_standby background_task=%s',self.backend.busy)

    async def _capture(self):
        async for pcm in self.route.frames():
            self.last_frame=time.monotonic()
            samples=np.frombuffer(pcm,dtype='<i2').astype(np.float32)/32768
            self.input_rms=round(float(np.sqrt(np.mean(samples*samples))),5)
            if not self.active:
                self.preroll.append(pcm)
                raw = getattr(self.route, "raw_frame", None) or pcm
                wake_samples = np.frombuffer(raw, dtype="<i2").astype(np.float32)/32768
                if self.recognizer.keyword(wake_samples):
                    await self.wake(tuple(self.preroll))
                    self.preroll.clear()
            else:
                self.voice.feed(pcm)
            self.recognizer.vad.accept_waveform(samples)
            speech=self.recognizer.vad.is_speech_detected()
            self.player.input_speaking=speech
            if speech and self.active:
                self.last_speech=self.last_activity=time.monotonic()
                # Room echo also passes a simple VAD. Let native voice identify
                # a user turn before flushing playback; VAD only tracks idle.
            while not self.recognizer.vad.empty():
                self.recognizer.vad.pop()  # no utterance ASR in the native route

    async def _events(self):
        while True:
            event=await self.events.get()
            try:
                if event['epoch']!=self.voice.epoch or not self.active:
                    continue
                if event['kind']=='transcript':
                    role,text=event['role'],event['text']
                    self.store.transcript(role,text)
                    if role=='user':
                        self.last_input=text
                        self.backend.last_user_text=text
                        self.input_count+=1
                        self.last_activity=time.monotonic()
                        clean=normalize(text)
                        if clean.startswith(normalize(self.config.wake_word)):
                            clean=clean[len(normalize(self.config.wake_word)):]
                        cmd=parse_command(clean)
                        if cmd:
                            await self.control(cmd)
                elif event['kind']=='media':
                    data=event['event']
                    if data.get('type')=='turn.created' and data.get('turn',{}).get('role')=='user':
                        self.player.manual_muted=False
                        # Data-channel events can arrive seconds after their
                        # audio. A stale turn must not cut a newer answer.
                        if time.monotonic()-self.last_speech<1:
                            await self.player.stop(suppress=False)
                        self.native_interruptions+=1
                        self.last_activity=time.monotonic()
                        log.info('native_user_turn playback_flushed')
                elif event['kind']=='disconnected' and self.mode=='active' and not self.recovering:
                    self.recovering=True
                    await self.player.stop()
                    self.mode='reconnecting'
                    # Restore the same native Codex thread; never replay input or
                    # dispatch a task on reconnect. An interrupted user must retry.
                    for delay in (1,3,8):
                        await asyncio.sleep(delay)
                        if not self.active:
                            break
                        try:
                            async with self.transition:
                                await self.voice.open()
                            self.mode='active'
                            self.last_error=None
                            break
                        except Exception as exc:
                            self.last_error='Native reconnect failed: '+type(exc).__name__
                    if self.mode=='reconnecting':
                        await self.sleep()
                        await self.player.cue(error=True)
                    self.recovering=False
            finally:
                self.events.task_done()

    async def control(self,cmd):
        self.last_activity=time.monotonic()
        if cmd.kind=='sleep':
            await self.sleep()
        elif cmd.kind=='stop':
            await self.player.stop()
            self.player.manual_muted=True
            await self.voice.instruct('用户要求停止播报。现在保持安静，等用户下一次讲话后再回应；后台任务继续。刚才被截断的内容用户没有听完。')
        elif cmd.kind=='cancel':
            await self.player.stop()
            cancelled=await self.backend.cancel()
            await self.speak('已请求取消当前任务。' if cancelled else '当前没有正在执行的任务。')
        elif cmd.kind=='room':
            await self.player.stop(suppress=False)
            await self.route.select(cmd.value)
            await self.speak('已切换到书房。' if cmd.value=='study' else '已切换到客厅。')
        elif cmd.kind=='volume':
            await self.route.set_volume(cmd.value)
            await self.speak('音量已调整。')
        elif cmd.kind=='progress':
            await self.speak(self.backend.progress or '当前没有正在执行的任务。')

    async def tool_control(self,args):
        action=args['action']
        if action not in {'room','volume','stop','sleep'}:
            raise ValueError('Unsupported device action')
        await self.control(Command(action,args.get('room') if action=='room' else args.get('volume')))
        return {'ok':True,'room':self.route.room,'volume':self.route.volume,'active':self.active}

    def status(self):
        return {'assistant_name':self.config.assistant_name,'wake_word':self.config.wake_word,
                'service':'running','backend':'native','voice_auth':'chatgpt','transport':'webrtc',
                'webrtc_engine':'chromium','echo_cancel_engine':'webrtc-aec3' if self.route.module else None,
                'mode':self.mode,'microphone_enabled':not self.no_mic,
                'audio_ready':self.route.ready.is_set(),'capture_recent':self.last_frame is not None and time.monotonic()-self.last_frame<3,
                'room':self.route.room,'volume':self.route.volume,'echo_cancel':bool(self.route.module),
                'task_running':self.backend.busy,'speaking':self.player.playing,
                'pending_speech':self.player.pending_voice_frames,'input_count':self.input_count,
                'playback_queue_ms':round(self.player.last_queue_ms,1),
                'max_playback_queue_ms':round(self.player.max_queue_ms,1),
                'voice_connect_ms':self.voice.open_timing_ms,
                'native_user_turns':self.native_interruptions,
                'last_input':self.last_input,'wake_count':self.wake_count,
                'input_rms':self.input_rms,
                'native_output_frames':self.voice.output_frames,'written_audio_bytes':self.player.written_bytes,
                'rtp_inbound':[{k:s.get(k) for k in ('packetsReceived','packetsLost',
                    'concealedSamples','silentConcealedSamples','totalSamplesReceived','packetsDiscarded')}
                    for s in self.voice.rtp_stats if s.get('type')=='inbound-rtp'],
                'software_stop_seconds':self.player.last_stop_seconds,
                'error':self.last_error or self.route.last_error or self.player.last_error or self.voice.last_error or self.backend.last_error,
                'uptime_seconds':round(time.time()-self.started)}

    async def _maintenance(self):
        while True:
            await asyncio.sleep(1)
            if self.active and not self.backend.busy and not self.player.playing and not self.recognizer.vad.is_speech_detected() and time.monotonic()-self.last_activity>=self.config.idle_seconds:
                await self.sleep()
            try:
                await self.route.ensure()
            except (RuntimeError,TimeoutError,OSError) as exc:
                self.route.last_error=type(exc).__name__

    async def _connection(self,reader,writer):
        try:
            d=json.loads(await asyncio.wait_for(reader.readline(),5))
            action=d['command']
            if action=='wake':
                await self.wake()
            elif action in {'sleep','stop','cancel','room','volume','progress'}:
                await self.control(Command(action,d.get('value')))
            elif action in {'say','text','inject-wav'}:
                await self.wake()
                if self.connect_task:
                    await asyncio.shield(self.connect_task)
                if self.mode!='active':
                    raise RuntimeError(self.last_error or 'Voice not connected')
                if action=='say':
                    await self.speak(d['text'])
                elif action=='text':
                    await self.backend.client.request('thread/realtime/appendText',{'threadId':self.backend.thread_id,'role':'user','text':d['text']})
                else:
                    if not self.no_mic:
                        raise ValueError('Inject tests require run --no-mic to avoid mixing live microphone audio')
                    a=await asyncio.to_thread(miniaudio.decode_file,d['path'],output_format=miniaudio.SampleFormat.SIGNED16,nchannels=1,sample_rate=16000)
                    pcm=a.samples.tobytes()
                    if len(pcm)>60*32000:
                        raise ValueError('Test WAV must be at most 60 seconds')
                    for offset in range(0,len(pcm),640):
                        self.voice.feed(pcm[offset:offset+640])
                        self.last_activity=time.monotonic()
                        await asyncio.sleep(.02)
            elif action=='shutdown':
                self.stop_event.set()
            elif action not in {'status','history'}:
                raise ValueError('Unknown command')
            result=self.store.recent() if action=='history' else self.status()
            response={'ok':True,'result':result}
        except Exception as exc:
            response={'ok':False,'error':str(exc)[:200]}
        try:
            writer.write((json.dumps(response,ensure_ascii=False)+'\n').encode())
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def run(self):
        server=None
        waiter=None
        try:
            await self.backend.start()
            account=(await self.backend.client.request('account/read',{})).get('account') or {}
            if account.get('type')!='chatgpt':
                raise RuntimeError('This service requires existing ChatGPT subscription login; API key use is disabled')
            try:
                await self.route.ensure()
            except RuntimeError:
                pass
            self.config.socket.unlink(missing_ok=True)
            server=await asyncio.start_unix_server(self._connection,path=self.config.socket,limit=128*1024)
            os.chmod(self.config.socket,0o600)
            for sig in (signal.SIGINT,signal.SIGTERM):
                asyncio.get_running_loop().add_signal_handler(sig,self.stop_event.set)
            self.tasks=[asyncio.create_task(self.player.run()),asyncio.create_task(self._events()),asyncio.create_task(self._maintenance())]
            if not self.no_mic:
                self.tasks.append(asyncio.create_task(self._capture()))
            waiter=asyncio.create_task(self.stop_event.wait())
            log.info('service_ready backend=native microphone=%s standby',not self.no_mic)
            done,_=await asyncio.wait([*self.tasks,self.backend.events_task,waiter],return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task is not waiter:
                    task.result()
        finally:
            if server:
                server.close()
                await server.wait_closed()
            if waiter:
                waiter.cancel()
            await self.sleep()
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks,return_exceptions=True)
            await self.backend.close()
            await self.route.close()
            self.config.socket.unlink(missing_ok=True)
            self.store.db.close()
            self.lock_file.close()
