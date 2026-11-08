import {
  MAX_TALK_AUDIO_BYTES,
  floatRmsToScale,
  nextSilenceState,
  type SilenceState,
} from "./chat-talk";

export class TalkRecordError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "TalkRecordError";
  }
}

export const TALK_MIC_UNAVAILABLE = "Microphone is not available";
export const TALK_RECORDING_EMPTY = "Recording was empty";
export const TALK_RECORDING_TOO_LARGE = "Recording is too large";

export interface MicRecorder {
  mimeType: string;
  state: string;
  ondataavailable: ((ev: { data: Blob }) => void) | null;
  onstop: (() => void) | null;
  onerror: ((ev: { error?: Error }) => void) | null;
  start(): void;
  stop(): void;
}

export interface RecordUntilStopDeps {
  getUserMedia: (constraints: MediaStreamConstraints) => Promise<MediaStream>;
  createRecorder: (stream: MediaStream) => MicRecorder;
  readRms?: () => number | null;
  now?: () => number;
  setTimer?: (fn: () => void, ms: number) => number;
  clearTimer?: (id: number) => void;
}

export interface RecordHandle {
  stop: () => void;
  done: Promise<{ mimeType: string; bytes: Uint8Array }>;
}

/** Every exit releases the microphone, including constructor and recorder failures. */
export function recordUntilStop(
  opts: { maxMs: number; silenceThreshold: number; silenceDurationMs: number },
  deps: RecordUntilStopDeps,
): RecordHandle {
  let stopRequested = false;
  let recorder: MicRecorder | null = null;
  let stream: MediaStream | null = null;
  let timer: number | null = null;
  let sampleTimer: number | null = null;
  let fail: (() => void) | undefined;
  const setTimer = deps.setTimer ?? ((fn, ms) => globalThis.setTimeout(fn, ms) as unknown as number);
  const clearTimer = deps.clearTimer ?? ((id) => globalThis.clearTimeout(id));
  const now = deps.now ?? (() => Date.now());
  const clearTimers = () => {
    if (timer !== null) clearTimer(timer);
    if (sampleTimer !== null) clearTimer(sampleTimer);
    timer = null;
    sampleTimer = null;
  };
  const stop = () => {
    stopRequested = true;
    clearTimers();
    if (recorder?.state === "recording") {
      try {
        recorder.stop();
      } catch {
        fail?.();
      }
    }
  };

  const done = (async () => {
    try {
      stream = await deps.getUserMedia({ audio: true });
      if (stopRequested) throw new TalkRecordError(TALK_RECORDING_EMPTY);
      const rec = deps.createRecorder(stream);
      recorder = rec;
      const chunks: Blob[] = [];
      let bytes = 0;
      const captured = new Promise<Blob>((resolve, reject) => {
        fail = () => reject(new TalkRecordError(TALK_MIC_UNAVAILABLE));
        rec.ondataavailable = ({ data }) => {
          bytes += data.size;
          if (bytes > MAX_TALK_AUDIO_BYTES) {
            reject(new TalkRecordError(TALK_RECORDING_TOO_LARGE));
          } else if (data.size) {
            chunks.push(data);
          }
        };
        rec.onerror = fail;
        rec.onstop = () => resolve(new Blob(chunks, { type: rec.mimeType || chunks[0]?.type || "audio/webm" }));
        // Keep synchronous start failures on the same captured rejection path.
        try {
          rec.start();
        } catch {
          fail();
        }
      });
      let silence: SilenceState = { speechSeen: false, silentForMs: 0 };
      let lastSample = now();
      timer = setTimer(stop, Math.max(1, opts.maxMs));
      const sample = () => {
        try {
          const rms = deps.readRms?.() ?? null;
          const t = now();
          if (rms !== null) {
            const next = nextSilenceState(silence, {
              rms, elapsedMs: t - lastSample, threshold: opts.silenceThreshold,
              silenceDurationMs: opts.silenceDurationMs,
            });
            silence = next;
            if (next.stop) return stop();
          }
          lastSample = t;
          sampleTimer = setTimer(sample, 100);
        } catch {
          fail?.();
        }
      };
      if (deps.readRms) sampleTimer = setTimer(sample, 100);
      const blob = await captured;
      if (!blob.size) throw new TalkRecordError(TALK_RECORDING_EMPTY);
      return { mimeType: blob.type || "audio/webm", bytes: new Uint8Array(await blob.arrayBuffer()) };
    } catch (error) {
      if (error instanceof TalkRecordError) throw error;
      throw new TalkRecordError(TALK_MIC_UNAVAILABLE);
    } finally {
      clearTimers();
      if (recorder) {
        recorder.ondataavailable = null;
        recorder.onstop = null;
        recorder.onerror = null;
      }
      stop();
      stream?.getTracks().forEach((track) => {
        try { track.stop(); } catch { /* A disconnected device may already be stopped. */ }
      });
    }
  })();
  return { stop, done };
}

export function rmsFromAnalyser(
  analyser: { getFloatTimeDomainData: (into: Float32Array) => void },
  into: Float32Array,
): number {
  analyser.getFloatTimeDomainData(into);
  return floatRmsToScale(into);
}
