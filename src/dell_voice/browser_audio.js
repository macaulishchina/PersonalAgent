/* Device bridge only. Chromium owns all WebRTC playout / NetEQ processing. */
let peer, context, socket;
async function startVoice() {
  socket = new WebSocket(location.href.replace('http:', 'ws:') + 'pcm');
  socket.binaryType = 'arraybuffer';
  await new Promise((resolve,reject) => { socket.onopen=resolve; socket.onerror=reject; });
  context = new AudioContext({sampleRate:48000,latencyHint:'interactive'});
  const code = `
  class Input extends AudioWorkletProcessor {
    constructor() {
      super(); this.blocks=[]; this.offset=0; this.enabled=false;
      this.port.onmessage=e=>{
        if(e.data.enabled!==undefined) this.enabled=e.data.enabled;
        else { this.blocks.push(new Int16Array(e.data)); if(this.blocks.length>500)this.blocks.shift(); }
      };
    }
    process(inputs,outputs) {
      const out=outputs[0][0];
      if(!this.enabled) return true;
      // Catch up after connection setup by removing only leading silence.
      // Never speed up speech or drop voiced samples from the opening question.
      while(this.offset===0 && this.blocks.length>4 && this.blocks[0].every(v=>Math.abs(v)<80))this.blocks.shift();
      for(let i=0;i<out.length;i++) {
        // Python's soxr stream supplies 48 kHz PCM, already resampled.
        if(!this.blocks.length) break;
        const a=this.blocks[0];
        out[i]=a[this.offset++]/32768;
        if(this.offset>=a.length){this.blocks.shift();this.offset=0;}
      }
      return true;
    }
  }
  class Output extends AudioWorkletProcessor {
    constructor(){super();this.buffer=new Float32Array(960);this.used=0;}
    process(inputs,outputs){
      if(inputs[0] && inputs[0][0]) {
        const input=inputs[0][0];
        for(let i=0;i<input.length;i++) {
          this.buffer[this.used++]=input[i];
          if(this.used===960){this.port.postMessage(this.buffer.buffer,[this.buffer.buffer]);this.buffer=new Float32Array(960);this.used=0;}
        }
      }
      // Keep the graph clock running with silence, never play the browser itself.
      return true;
    }
  }
  registerProcessor('dudu-input',Input);registerProcessor('dudu-output',Output);`;
  const url=URL.createObjectURL(new Blob([code],{type:'application/javascript'}));
  await context.audioWorklet.addModule(url);URL.revokeObjectURL(url);
  const microphone=new AudioWorkletNode(context,'dudu-input',{numberOfInputs:0,outputChannelCount:[1]});
  socket.onmessage=e=>{if(e.data instanceof ArrayBuffer)microphone.port.postMessage(e.data,[e.data]);};
  const stream=context.createMediaStreamDestination();microphone.connect(stream);
  peer=new RTCPeerConnection({iceServers:[]});
  peer.addTrack(stream.stream.getAudioTracks()[0]);
  peer.onconnectionstatechange=()=>{
    microphone.port.postMessage({enabled:peer.connectionState==='connected'});
    socket.send(JSON.stringify({type:'connection',state:peer.connectionState}));
  };
  const channel=peer.createDataChannel('oai-events');
  channel.onmessage=e=>{try{socket.send(JSON.stringify({type:'media',event:JSON.parse(e.data)}));}catch{}};
  peer.ontrack=e=>{
    const remote=new MediaStream([e.track]);
    // Chromium requires a media element to start pulling remote WebRTC audio
    // even when its samples are consumed through Web Audio (crbug 933677).
    const element=document.createElement('audio');
    element.srcObject=remote;element.autoplay=true;element.volume=0;
    document.body.append(element);element.play().catch(()=>{});
    const source=context.createMediaStreamSource(remote);
    const output=new AudioWorkletNode(context,'dudu-output',{channelCount:1,channelCountMode:'explicit',outputChannelCount:[1]});
    output.port.onmessage=e=>{if(socket.readyState===WebSocket.OPEN)socket.send(e.data);};
    source.connect(output);output.connect(context.destination);
  };
  await context.resume();
  await peer.setLocalDescription(await peer.createOffer());
  if(peer.iceGatheringState!=='complete') await new Promise(resolve=>{
    const done=()=>{if(peer.iceGatheringState==='complete'){peer.removeEventListener('icegatheringstatechange',done);resolve();}};
    peer.addEventListener('icegatheringstatechange',done);setTimeout(resolve,1500);
  });
  return peer.localDescription.sdp;
}
