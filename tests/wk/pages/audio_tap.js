// AudioWorklet tap for the audio acceptance page: passes its input through
// and posts the LEFT channel in 4800-frame blocks tagged with the context
// frame of their first sample (`currentFrame`), so the test can place every
// recorded sample on the AudioContext's own clock.
class Tap extends AudioWorkletProcessor {
  constructor() {
    super()
    this.buf = new Float32Array(4800)
    this.fill = 0
    this.frame0 = -1
    this.port.onmessage = () => this.flush()
  }
  flush() {
    if (this.fill) this.port.postMessage({ frame: this.frame0, L: this.buf.slice(0, this.fill) })
    this.fill = 0
    this.frame0 = -1
  }
  process(inputs, outputs) {
    const inp = inputs[0]
    const out = outputs[0]
    const n = out[0].length
    for (let c = 0; c < out.length; c++) {
      if (inp[c]) out[c].set(inp[c])
    }
    const L = inp[0] || new Float32Array(n)
    for (let i = 0; i < n; i++) {
      if (this.frame0 < 0) this.frame0 = currentFrame + i
      this.buf[this.fill++] = L[i]
      if (this.fill === this.buf.length) this.flush()
    }
    return true
  }
}
registerProcessor('tap', Tap)
