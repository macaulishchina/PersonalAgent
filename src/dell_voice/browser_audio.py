"""Headless Chromium WebRTC/NetEQ with a private loopback PCM device bridge.

Chromium handles RTP, jitter and packet-loss concealment. No browser account,
API key, microphone permission or real speaker is given to the browser.
"""
import asyncio
from pathlib import Path
import secrets

from aiohttp import web, WSMsgType
import numpy as np
from playwright.async_api import async_playwright
import soxr


class BrowserAudio:
    def __init__(self,on_audio,on_event):
        self.on_audio,self.on_event=on_audio,on_event
        self.playwright=self.browser=self.page=self.runner=self.socket=None
        self.ready=asyncio.Event()
        self.socket_ready=asyncio.Event()
        self.queue=asyncio.Queue(maxsize=500)
        self.writer=None
        self.closed=False
        self.overflows=0
        self.resampler=soxr.ResampleStream(48000,24000,1,dtype='float32')
        self.input_resampler=soxr.ResampleStream(16000,48000,1,dtype='float32')

    async def _websocket(self,request):
        if self.socket is not None:
            raise web.HTTPForbidden()
        ws=web.WebSocketResponse(max_msg_size=128*1024)
        await ws.prepare(request)
        self.socket=ws;self.socket_ready.set()
        async for message in ws:
            if message.type==WSMsgType.BINARY:
                samples=np.frombuffer(message.data,dtype='<f4')
                data=self.resampler.resample_chunk(samples)
                self.on_audio((data*32768).clip(-32768,32767).astype('<i2').tobytes())
            elif message.type==WSMsgType.TEXT:
                data=message.json()
                if data['type']=='connection' and data['state']=='connected':self.ready.set()
                await self.on_event(data)
        if not self.closed:
            await self.on_event({'type':'connection','state':'failed'})
        return ws

    async def start(self):
        token=secrets.token_urlsafe(24)
        app=web.Application()
        # All endpoints are scoped to a random session token, loopback only.
        async def page(request):
            return web.Response(text='<html><body><script src="bridge.js"></script></body></html>',content_type='text/html')
        async def source(request):
            return web.Response(text=Path(__file__).with_name('browser_audio.js').read_text(),content_type='application/javascript')
        app.router.add_get('/'+token+'/',page)
        app.router.add_get('/'+token+'/bridge.js',source)
        app.router.add_get('/'+token+'/pcm',self._websocket)
        self.runner=web.AppRunner(app,access_log=None);await self.runner.setup()
        site=web.TCPSite(self.runner,'127.0.0.1',0);await site.start()
        port=site._server.sockets[0].getsockname()[1]
        self.playwright=await async_playwright().start()
        self.browser=await self.playwright.chromium.launch(headless=True,args=[
            '--autoplay-policy=no-user-gesture-required','--mute-audio',
            '--disable-background-timer-throttling','--disable-renderer-backgrounding'])
        self.page=await self.browser.new_page()
        await self.page.goto(f'http://127.0.0.1:{port}/{token}/')
        sdp=await self.page.evaluate('startVoice()')
        await asyncio.wait_for(self.socket_ready.wait(),5)
        self.writer=asyncio.create_task(self._write())
        return sdp

    def feed(self,pcm):
        if self.closed:return
        data=self.input_resampler.resample_chunk(np.frombuffer(pcm,dtype='<i2').astype(np.float32))
        if not len(data):return
        pcm=data.clip(-32768,32767).astype('<i2').tobytes()
        try:self.queue.put_nowait(pcm)
        except asyncio.QueueFull:
            self.overflows+=1
            # Keep a bounded backlog and report it; never block capture/control.
            self.queue.get_nowait();self.queue.put_nowait(pcm)

    async def _write(self):
        while True:
            pcm=await self.queue.get()
            await self.socket.send_bytes(pcm)

    async def answer(self,sdp):
        await self.page.evaluate('sdp => peer.setRemoteDescription({type:"answer",sdp})',sdp)
        await asyncio.wait_for(self.ready.wait(),15)

    async def get_stats(self):
        if not self.page:return []
        return await self.page.evaluate('async () => Array.from((await peer.getStats()).values()).filter(x => ["inbound-rtp","outbound-rtp","remote-inbound-rtp"].includes(x.type))')

    async def close(self):
        self.closed=True
        if self.writer:
            self.writer.cancel();await asyncio.gather(self.writer,return_exceptions=True)
        if self.socket:await self.socket.close()
        if self.browser:await self.browser.close()
        if self.playwright:await self.playwright.stop()
        if self.runner:await self.runner.cleanup()
