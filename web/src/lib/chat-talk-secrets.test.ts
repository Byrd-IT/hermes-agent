import { describe, expect, it } from "vitest";

import { recordUntilStop, type MicRecorder } from "./chat-talk-record";
import { runTalkTurn } from "./chat-talk-session";

const secrets = [
  process.env.SYNTH_BEARER_TOKEN ?? "probe-bearer-9f8412ABC123",
  process.env.SYNTH_URL_USERINFO ?? "https://probeuser:probepass@host",
  process.env.SYNTH_TOKEN ?? "probe-token-9f8412XYZ789",
  process.env.SYNTH_API_KEY ?? "probe-key-9f8412KLM456",
  process.env.SYNTH_QUERY_URL ?? "https://host/?api_key=probe-query-9f8412",
  process.env.SYNTH_AUTH_HEADER ?? "Authorization: Bearer probe-header-9f8412",
];

function assertClean(output: unknown) {
  const rendered = output instanceof Error
    ? `${output.message}\n${output.stack}\n${JSON.stringify(output)}\n${String(output.cause)}`
    : JSON.stringify(output);
  for (const secret of secrets) {
    for (const variant of [secret, secret.slice(0, 8), secret.slice(-6), encodeURIComponent(secret), btoa(secret)]) {
      expect(rendered.includes(variant)).toBe(false);
    }
  }
}

const detail = secrets.join(" ");
const settings = { maxMs: 1, silenceThreshold: 200, silenceDurationMs: 100 };

class FailingRecorder implements MicRecorder {
  state = "inactive";
  mimeType = "audio/webm";
  ondataavailable: MicRecorder["ondataavailable"] = null;
  onstop: MicRecorder["onstop"] = null;
  onerror: MicRecorder["onerror"] = null;
  private failure: string;
  constructor(failure: string) { this.failure = failure; }
  start() {
    this.state = "recording";
    if (this.failure === "start") throw new Error(detail);
    if (this.failure === "event") this.onerror?.({ error: new Error(detail) });
  }
  stop() {
    this.state = "inactive";
    if (this.failure === "stop") throw new Error(detail);
    this.onstop?.();
  }
}

describe("Talk diagnostics never expose device or service details", () => {
  it.each(["permission", "constructor", "start", "event", "stop", "empty"])("sanitizes recorder %s failures", async failure => {
    let stopped = false;
    const handle = recordUntilStop(settings, {
      getUserMedia: async () => {
        if (failure === "permission") throw new Error(detail);
        return { getTracks: () => [{ stop: () => { stopped = true; } }] } as unknown as MediaStream;
      },
      createRecorder: () => {
        if (failure === "constructor") throw new Error(detail);
        return new FailingRecorder(failure);
      },
    });
    let rejected = false;
    try { await handle.done; } catch (error) { rejected = true; assertClean(error); }
    expect(rejected).toBe(true);
    expect(stopped).toBe(failure !== "permission");
  });

  it.each(["config", "record", "transcribe", "send"])("sanitizes %s errors without diagnostic details", async failure => {
    const fail = () => { throw new Error(detail); };
    const result = await runTalkTurn({
      getConfig: async () => failure === "config" ? fail() : {},
      record: async () => failure === "record" ? fail() : { mimeType: "audio/webm", bytes: new Uint8Array([1]) },
      transcribe: async () => failure === "transcribe" ? fail() : { transcript: "hello" },
      send: () => failure === "send" ? fail() : true,
      cancelled: () => false,
      registerStop: () => {},
    });
    expect(result.phase).toBe("failed");
    assertClean(result);
  });
});
