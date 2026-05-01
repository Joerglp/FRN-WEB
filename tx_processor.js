/**
 * AudioWorklet processor for FRN PTT transmit.
 * Runs in the audio rendering thread — collects Float32 samples,
 * converts to Int16, and posts to the main thread via MessagePort.
 */
class TxProcessor extends AudioWorkletProcessor {
    process(inputs) {
        const channel = inputs[0]?.[0];
        if (!channel || channel.length === 0) return true;

        // Convert Float32 [-1..1] → Int16
        const out = new Int16Array(channel.length);
        for (let i = 0; i < channel.length; i++) {
            const s = Math.max(-1, Math.min(1, channel[i]));
            out[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
        }
        // Transfer the buffer (zero-copy)
        this.port.postMessage(out.buffer, [out.buffer]);
        return true;
    }
}
registerProcessor("tx-processor", TxProcessor);
