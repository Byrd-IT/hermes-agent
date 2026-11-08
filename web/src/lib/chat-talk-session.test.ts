import { describe, expect, it } from "vitest";

import { TalkRecordError, TALK_RECORDING_EMPTY } from "./chat-talk-record";
import { phaseFromTalkError, runTalkTurn } from "./chat-talk-session";

describe("runTalkTurn error boundary", () => {
  it.each(["record", "transcribe"])(
    "reports generic failure for cancellation after %s, with no transcript text",
    async (boundary) => {
      let cancelled = false;
      let transcribed = false;
      let sent = false;
      const result = await runTalkTurn({
        getConfig: async () => ({}),
        record: async () => {
          cancelled = boundary === "record";
          return { mimeType: "audio/webm", bytes: new Uint8Array([1]) };
        },
        transcribe: async () => {
          transcribed = true;
          cancelled = true;
          return { transcript: "private words" };
        },
        send: () => {
          sent = true;
          return true;
        },
        cancelled: () => cancelled,
        registerStop: () => {},
      });
      expect(result).toEqual({ phase: "failed", focus: false });
      expect(sent).toBe(false);
      expect(JSON.stringify(result)).not.toContain("private words");
      expect(transcribed).toBe(boundary === "transcribe");
    },
  );

  it("reports failure when cancellation makes the recording empty", async () => {
    let cancelled = false;
    let transcribed = false;
    const result = await runTalkTurn({
      getConfig: async () => ({}),
      record: async () => {
        cancelled = true;
        throw new TalkRecordError(TALK_RECORDING_EMPTY);
      },
      transcribe: async () => {
        transcribed = true;
        return { transcript: "private words" };
      },
      send: () => true,
      cancelled: () => cancelled,
      registerStop: () => {},
    });
    expect(result).toEqual({ phase: "failed", focus: false });
    expect(transcribed).toBe(false);
    expect(JSON.stringify(result)).not.toContain("private words");
    expect(phaseFromTalkError(new TalkRecordError(TALK_RECORDING_EMPTY))).toBe(
      "empty",
    );
  });

  it("still reports no speech when an empty recording was not cancelled", async () => {
    const result = await runTalkTurn({
      getConfig: async () => ({}),
      record: async () => {
        throw new TalkRecordError(TALK_RECORDING_EMPTY);
      },
      transcribe: async () => ({ transcript: "private words" }),
      send: () => true,
      cancelled: () => false,
      registerStop: () => {},
    });
    expect(result).toEqual({ phase: "empty", focus: false });
  });

  it("stays idle when cancelled before recording starts", async () => {
    let recorded = false;
    const result = await runTalkTurn({
      getConfig: async () => ({}),
      record: async () => {
        recorded = true;
        return { mimeType: "audio/webm", bytes: new Uint8Array([1]) };
      },
      transcribe: async () => ({ transcript: "private words" }),
      send: () => true,
      cancelled: () => true,
      registerStop: () => {},
    });
    expect(recorded).toBe(false);
    expect(result).toEqual({ phase: "idle", focus: false });
    expect(JSON.stringify(result)).not.toContain("private words");
  });

  it("never records when voice settings cannot be loaded", async () => {
    let recorded = false;
    const result = await runTalkTurn({
      getConfig: async () => { throw new Error("unavailable"); },
      record: async () => {
        recorded = true;
        return { mimeType: "audio/webm", bytes: new Uint8Array([1]) };
      },
      transcribe: async () => ({ transcript: "do not send" }),
      send: () => { throw new Error("unexpected send"); },
      cancelled: () => false,
      registerStop: () => {},
    });
    expect(recorded).toBe(false);
    expect(result).toEqual({ phase: "failed", focus: false });
  });

  it("does not rethrow a provider or mic error, and the result has no secret text", async () => {
    const secret = "Bearer synth-token-ABC123 https://user:pass@host X-Api-Key";
    const result = await runTalkTurn({
      getConfig: async () => ({ voice: { submit_mode: "direct" } }),
      record: async () => {
        throw new Error(secret);
      },
      transcribe: async () => ({ transcript: secret }),
      send: () => true,
      cancelled: () => false,
      registerStop: () => {},
    });
    const encoded = JSON.stringify(result);
    expect(result.phase).toBe("failed");
    expect(encoded).not.toContain("synth-token");
    expect(encoded).not.toContain("user:pass");
    expect(phaseFromTalkError(new Error(secret))).toBe("failed");
  });
});
