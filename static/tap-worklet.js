// Runs on the audio thread: hands microphone audio to the page in ~40 ms pieces.
class Tap extends AudioWorkletProcessor {
  constructor() { super(); this.parts = []; this.size = 0; }
  process(inputs) {
    const channel = inputs[0][0];
    if (channel) {
      this.parts.push(channel.slice());
      this.size += channel.length;
      if (this.size >= 2048) {
        const out = new Float32Array(this.size);
        let at = 0;
        for (const part of this.parts) { out.set(part, at); at += part.length; }
        this.port.postMessage(out, [out.buffer]);
        this.parts = []; this.size = 0;
      }
    }
    return true;
  }
}
registerProcessor("tap", Tap);
