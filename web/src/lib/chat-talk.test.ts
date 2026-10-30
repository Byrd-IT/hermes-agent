import { describe, expect, it } from "vitest";

import {
  DASHBOARD_TALK_PREFIX,
  MAX_TALK_AUDIO_BYTES,
  VOICE_TALK_DEFAULTS,
  buildTalkPaste,
  buildTranscribeRequest,
  bytesToBase64,
  deliverTalkTranscript,
  floatRmsToScale,
  mimeBase,
  nextSilenceState,
  normalizeVoiceSubmitMode,
  readVoiceTalkSettings,
  sanitizeTranscriptForPty,
  transcribeUrl,
} from "./chat-talk";

describe("readVoiceTalkSettings", () => {
  it("uses the existing voice keys and defaults to submit-on-stop", () => {
    expect(readVoiceTalkSettings({})).toEqual(VOICE_TALK_DEFAULTS);
    expect(readVoiceTalkSettings(null)).toEqual(VOICE_TALK_DEFAULTS);
    expect(
      readVoiceTalkSettings({
        voice: {
          submit_mode: "draft",
          max_recording_seconds: 30,
          silence_duration: 1.5,
          silence_threshold: 400,
        },
      }),
    ).toEqual({
      submitMode: "draft",
      maxRecordingSeconds: 30,
      silenceDurationSeconds: 1.5,
      silenceThreshold: 400,
    });
  });

  it("treats anything other than draft as direct, matching the TUI", () => {
    expect(normalizeVoiceSubmitMode("DIRECT")).toBe("direct");
    expect(normalizeVoiceSubmitMode(" draft ")).toBe("draft");
    expect(normalizeVoiceSubmitMode("nope")).toBe("direct");
    expect(normalizeVoiceSubmitMode(undefined)).toBe("direct");
  });

  it("rejects non-positive recording caps instead of inventing a second policy", () => {
    expect(
      readVoiceTalkSettings({ voice: { max_recording_seconds: 0 } })
        .maxRecordingSeconds,
    ).toBe(120);
    expect(
      readVoiceTalkSettings({ voice: { silence_duration: 0 } })
        .silenceDurationSeconds,
    ).toBe(0);
  });
});

describe("sanitizeTranscriptForPty", () => {
  it("strips controls so a transcript cannot submit early or inject escapes", () => {
    expect(sanitizeTranscriptForPty("hello\r\nworld\x1b[31m")).toBe(
      "hello world [31m",
    );
    expect(sanitizeTranscriptForPty("  \n\t  ")).toBe("");
    expect(sanitizeTranscriptForPty("ok\u0000there")).toBe("ok there");
  });
});

function decodeTalkPaste(paste: string): { text: string; mode: string } {
  expect(paste.startsWith(`\u001b[200~${DASHBOARD_TALK_PREFIX}`)).toBe(true);
  expect(paste.endsWith("\u001b[201~")).toBe(true);
  expect(paste).not.toContain("\r");
  expect(paste).not.toContain("\u0015");
  const body = paste.slice(
    `\u001b[200~${DASHBOARD_TALK_PREFIX}`.length,
    -"\u001b[201~".length,
  );
  return JSON.parse(atob(body));
}

describe("buildTalkPaste", () => {
  it("sends one paste the TUI submits, with no composer keys", () => {
    const paste = buildTalkPaste("turn the porch light off", "direct");
    expect(decodeTalkPaste(paste)).toEqual({
      text: "turn the porch light off",
      mode: "direct",
    });
  });

  it("draft mode is still one paste and does not press Enter", () => {
    expect(decodeTalkPaste(buildTalkPaste("save this for later", "draft"))).toEqual({
      text: "save this for later",
      mode: "draft",
    });
  });

  it("does not send anything for an empty transcript", () => {
    expect(buildTalkPaste(" \r\n ", "direct")).toBe("");
  });
});

describe("deliverTalkTranscript", () => {
  it("sends one paste and reports closed if the session rejects it", async () => {
    const sent: string[] = [];
    const result = await deliverTalkTranscript({
      transcript: "hello",
      submitMode: "direct",
      send: (data) => {
        sent.push(data);
        return false;
      },
    });
    expect(result).toBe("closed");
    expect(sent).toHaveLength(1);
    expect(decodeTalkPaste(sent[0] ?? "")).toEqual({ text: "hello", mode: "direct" });
  });

  it("reports drafted without a submit byte", async () => {
    const sent: string[] = [];
    const result = await deliverTalkTranscript({
      transcript: "note",
      submitMode: "draft",
      send: (data) => {
        sent.push(data);
        return true;
      },
    });
    expect(result).toBe("drafted");
    expect(decodeTalkPaste(sent[0] ?? "").mode).toBe("draft");
  });
});

describe("transcribe request", () => {
  it("posts a data URL to the existing audio route for the chat profile", () => {
    expect(mimeBase("audio/webm;codecs=opus")).toBe("audio/webm");
    expect(mimeBase("text/plain")).toBe("audio/webm");
    expect(buildTranscribeRequest("audio/webm;codecs=opus", "QQ==")).toEqual({
      data_url: "data:audio/webm;base64,QQ==",
      mime_type: "audio/webm",
    });
    expect(transcribeUrl("work")).toBe(
      "/api/audio/transcribe?profile=work",
    );
    expect(transcribeUrl()).toBe("/api/audio/transcribe");
    expect(bytesToBase64(new Uint8Array([1, 2, 3]))).toBe("AQID");
    expect(MAX_TALK_AUDIO_BYTES).toBe(25 * 1024 * 1024);
  });
});

describe("silence auto-stop", () => {
  it("waits for speech, then stops after the configured silence", () => {
    const quiet = nextSilenceState(
      { speechSeen: false, silentForMs: 0 },
      { rms: 10, elapsedMs: 100, threshold: 200, silenceDurationMs: 3000 },
    );
    expect(quiet.stop).toBe(false);
    expect(quiet.speechSeen).toBe(false);

    const heard = nextSilenceState(quiet, {
      rms: 800,
      elapsedMs: 100,
      threshold: 200,
      silenceDurationMs: 3000,
    });
    expect(heard.speechSeen).toBe(true);
    expect(heard.stop).toBe(false);

    const still = nextSilenceState(heard, {
      rms: 10,
      elapsedMs: 3000,
      threshold: 200,
      silenceDurationMs: 3000,
    });
    expect(still.stop).toBe(true);
  });

  it("maps float samples onto the 16-bit threshold scale", () => {
    expect(floatRmsToScale(new Float32Array([0, 0, 0]))).toBe(0);
    const loud = floatRmsToScale(new Float32Array([1, -1, 1, -1]));
    expect(loud).toBeCloseTo(32767, 0);
  });
});
