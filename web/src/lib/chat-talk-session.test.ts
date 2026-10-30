import { describe, expect, it } from "vitest";

import { phaseFromTalkError, runTalkTurn } from "./chat-talk-session";

describe("runTalkTurn error boundary", () => {
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
