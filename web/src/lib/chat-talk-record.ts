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
  /** Return the latest 16-bit RMS, or null when silence detection is unavailable. */
  readRms?: () => number | null;
  now?: () => number;
  setTimer?: (fn: () => void, ms: number) => number;
  clearTimer?: (id: number) => void;
}

export interface RecordHandle {
  stop: () => void;
  done: Promise<{ mimeType: string; bytes: Uint8Array }>;
}

const SAMPLE_MS = 100;

function blobBytes(blob: Blob): Promise<Uint8Array> {
  return blob.arrayBuffer().then((buffer) => new Uint8Array(buffer));
}

/**
 * Record until the caller stops, the max duration elapses, or configured
 * silence follows speech. Always stops the mic tracks.
 */
export function recordUntilStop(
  opts: {
    maxMs: number;
    silenceThreshold: number;
    silenceDurationMs: number;
  },
  deps: RecordUntilStopDeps,
): RecordHandle {
  let stopRequested = false;
  let recorder: MicRecorder | null = null;
  let stream: MediaStream | null = null;
  let timer: number | null = null;
  let sampleTimer: number | null = null;
  const setTimer = deps.setTimer ?? ((fn, ms) => globalThis.setTimeout(fn, ms) as unknown as number);
  const clearTimer = deps.clearTimer ?? ((id) => globalThis.clearTimeout(id));
  const now = deps.now ?? (() => Date.now());

  const done = (async () => {
    try {
      stream = await deps.getUserMedia({ audio: true });
    } catch {
      throw new TalkRecordError(TALK_MIC_UNAVAILABLE);
    }
    if (stopRequested) {
      stopTracks(stream);
      throw new TalkRecordError(TALK_RECORDING_EMPTY);
    }

    const chunks: Blob[] = [];
    recorder = deps.createRecorder(stream);
    const rec = recorder;
    const captured = new Promise<Blob>((resolve, reject) => {
      rec.ondataavailable = (ev) => {
        if (ev.data && ev.data.size > 0) chunks.push(ev.data);
      };
      rec.onerror = () => {
        reject(new TalkRecordError(TALK_MIC_UNAVAILABLE));
      };
      rec.onstop = () => {
        const type = rec.mimeType || chunks[0]?.type || "audio/webm";
        resolve(new Blob(chunks, { type }));
      };
    });

    try {
      rec.start();
    } catch {
      stopTracks(stream);
      throw new TalkRecordError(TALK_MIC_UNAVAILABLE);
    }

    const started = now();
    let silence: SilenceState = { speechSeen: false, silentForMs: 0 };
    let lastSample = started;

    const finish = () => {
      if (timer !== null) clearTimer(timer);
      if (sampleTimer !== null) clearTimer(sampleTimer);
      timer = null;
      sampleTimer = null;
      if (rec.state === "recording") {
        try {
          rec.stop();
        } catch {
          /* already stopped */
        }
      }
    };

    timer = setTimer(finish, Math.max(1, opts.maxMs));
    const sample = () => {
      const rms = deps.readRms?.() ?? null;
      const t = now();
      const elapsed = t - lastSample;
      lastSample = t;
      if (rms !== null) {
        const next = nextSilenceState(silence, {
          rms,
          elapsedMs: elapsed,
          threshold: opts.silenceThreshold,
          silenceDurationMs: opts.silenceDurationMs,
        });
        silence = next;
        if (next.stop) {
          finish();
          return;
        }
      }
      if (t - started >= opts.maxMs) {
        finish();
        return;
      }
      sampleTimer = setTimer(sample, SAMPLE_MS);
    };
    if (deps.readRms) sampleTimer = setTimer(sample, SAMPLE_MS);

    if (stopRequested) finish();

    let blob: Blob;
    try {
      blob = await captured;
    } finally {
      stopTracks(stream);
    }
    if (blob.size === 0) throw new TalkRecordError(TALK_RECORDING_EMPTY);
    if (blob.size > MAX_TALK_AUDIO_BYTES) {
      throw new TalkRecordError(TALK_RECORDING_TOO_LARGE);
    }
    return { mimeType: blob.type || "audio/webm", bytes: await blobBytes(blob) };
  })();

  return {
    stop: () => {
      stopRequested = true;
      if (timer !== null) clearTimer(timer);
      if (sampleTimer !== null) clearTimer(sampleTimer);
      timer = null;
      sampleTimer = null;
      if (recorder && recorder.state === "recording") {
        try {
          recorder.stop();
        } catch {
          /* already stopped */
        }
      }
    },
    done,
  };
}

function stopTracks(stream: MediaStream | null) {
  stream?.getTracks().forEach((track) => {
    try {
      track.stop();
    } catch {
      /* already stopped */
    }
  });
}

export function rmsFromAnalyser(
  analyser: { getFloatTimeDomainData: (into: Float32Array) => void },
  into: Float32Array,
): number {
  analyser.getFloatTimeDomainData(into);
  return floatRmsToScale(into);
}
