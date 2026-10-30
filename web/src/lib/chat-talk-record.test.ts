import { describe, expect, it, vi } from "vitest";

import {
  TalkRecordError,
  recordUntilStop,
  type MicRecorder,
} from "./chat-talk-record";

class FakeStream {
  stopped = false;
  getTracks() {
    return [{ stop: () => { this.stopped = true; } }];
  }
}

class FakeRecorder implements MicRecorder {
  mimeType = "audio/webm";
  state = "inactive";
  ondataavailable: ((ev: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  onerror: ((ev: { error?: Error }) => void) | null = null;
  start() {
    this.state = "recording";
    this.ondataavailable?.({
      data: new Blob([new Uint8Array([1, 2, 3])], { type: "audio/webm" }),
    });
  }
  stop() {
    if (this.state !== "recording") return;
    this.state = "inactive";
    this.onstop?.();
  }
}

describe("recordUntilStop", () => {
  it("stops the mic and returns bytes when the caller stops", async () => {
    const stream = new FakeStream();
    const recorder = new FakeRecorder();
    const handle = recordUntilStop(
      { maxMs: 5000, silenceThreshold: 200, silenceDurationMs: 3000 },
      {
        getUserMedia: async () => stream as unknown as MediaStream,
        createRecorder: () => recorder,
      },
    );
    await vi.waitFor(() => expect(recorder.state).toBe("recording"));
    handle.stop();
    const recorded = await handle.done;
    expect(Array.from(recorded.bytes)).toEqual([1, 2, 3]);
    expect(recorded.mimeType).toBe("audio/webm");
    expect(stream.stopped).toBe(true);
  });

  it("auto-stops after configured silence once speech was heard", async () => {
    const stream = new FakeStream();
    const recorder = new FakeRecorder();
    const samples: Array<() => void> = [];
    let clock = 0;
    const rms = [0, 800, 10];
    const handle = recordUntilStop(
      { maxMs: 60_000, silenceThreshold: 200, silenceDurationMs: 100 },
      {
        getUserMedia: async () => stream as unknown as MediaStream,
        createRecorder: () => recorder,
        readRms: () => rms.shift() ?? 10,
        now: () => clock,
        setTimer: (fn, ms) => {
          if (ms === 100) samples.push(fn);
          return samples.length + 1;
        },
        clearTimer: () => {},
      },
    );
    await vi.waitFor(() => expect(recorder.state).toBe("recording"));
    expect(samples.length).toBeGreaterThan(0);
    clock = 100;
    samples.shift()?.();
    clock = 200;
    samples.shift()?.();
    clock = 300;
    await samples.shift()?.();
    const recorded = await handle.done;
    expect(recorded.bytes.byteLength).toBe(3);
    expect(stream.stopped).toBe(true);
  });

  it("does not include the microphone error detail", async () => {
    const handle = recordUntilStop(
      { maxMs: 1000, silenceThreshold: 200, silenceDurationMs: 3000 },
      {
        getUserMedia: async () => {
          throw new Error("device secret-token-ABC");
        },
        createRecorder: () => new FakeRecorder(),
      },
    );
    await expect(handle.done).rejects.toBeInstanceOf(TalkRecordError);
    await expect(handle.done).rejects.toThrow("Microphone is not available");
    await expect(handle.done).rejects.not.toThrow("secret-token");
  });
});
