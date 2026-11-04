// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ChatTalkButton } from "./chat-talk-button";
import { buildTalkPaste } from "./chat-talk";

let root: Root | null = null;
let host: HTMLDivElement | null = null;

afterEach(() => {
  act(() => {
    root?.unmount();
  });
  root = null;
  host?.remove();
  host = null;
});

function renderButton(
  props: Partial<React.ComponentProps<typeof ChatTalkButton>> = {},
) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  const ws = { readyState: 1, send: vi.fn() } as unknown as WebSocket;
  const wsRef = { current: ws };
  const ptyStateRef = { current: "open" as const };
  const termRef = { current: { focus: vi.fn() } };
  act(() => {
    root?.render(
      <ChatTalkButton
        color="#ccc"
        profile="work"
        ptyState="open"
        wsRef={wsRef}
        ptyStateRef={ptyStateRef}
        termRef={termRef}
        {...props}
      />,
    );
  });
  return { ws, termRef };
}

describe("ChatTalkButton", () => {
  it("renders a Talk control in the chat overlay style", () => {
    renderButton();
    const button = host?.querySelector("button");
    expect(button?.getAttribute("aria-label")).toBe("Talk");
    expect(button?.textContent).toContain("talk");
    expect(button?.className).toContain("border-current/30");
    expect(button?.className).toContain("left-2");
  });

  it("submits the transcript on stop when voice.submit_mode is direct", async () => {
    const sent: string[] = [];
    const { ws, termRef } = renderButton({
      deps: {
        getConfig: async () => ({ voice: { submit_mode: "direct" } }),
        record: async (_settings, registerStop) => {
          registerStop(() => {});
          return { mimeType: "audio/webm", bytes: new Uint8Array([1, 2, 3]) };
        },
        transcribe: async (profile, body) => {
          expect(profile).toBe("work");
          expect(body.mime_type).toBe("audio/webm");
          expect(body.data_url.startsWith("data:audio/webm;base64,")).toBe(true);
          expect(body.data_url).not.toContain("porch");
          return { transcript: "turn the porch light off" };
        },
      },
    });
    (ws.send as ReturnType<typeof vi.fn>).mockImplementation((data: string) => {
      sent.push(data);
    });
    const button = host?.querySelector("button");
    await act(async () => {
      button?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(sent).toEqual([
      buildTalkPaste("turn the porch light off", "direct"),
    ]);
    expect(termRef.current.focus).toHaveBeenCalled();
    expect(button?.getAttribute("aria-label")).toBe("Talk");
  });

  it("does not press Enter when voice.submit_mode is draft", async () => {
    const sent: string[] = [];
    const { ws } = renderButton({
      deps: {
        getConfig: async () => ({ voice: { submit_mode: "draft" } }),
        record: async (_settings, registerStop) => {
          registerStop(() => {});
          return { mimeType: "audio/webm", bytes: new Uint8Array([9]) };
        },
        transcribe: async () => ({ transcript: "save this" }),
      },
    });
    (ws.send as ReturnType<typeof vi.fn>).mockImplementation((data: string) => {
      sent.push(data);
    });
    await act(async () => {
      host?.querySelector("button")?.dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
    });
    expect(sent).toEqual([buildTalkPaste("save this", "draft")]);
  });

  it("does not deliver an old recording to a replacement PTY socket", async () => {
    const oldSocket = { readyState: 1, send: vi.fn() } as unknown as WebSocket;
    const replacement = { readyState: 1, send: vi.fn() } as unknown as WebSocket;
    const wsRef = { current: oldSocket };
    let finish!: (value: { transcript: string }) => void;
    renderButton({
      wsRef,
      deps: {
        getConfig: async () => ({}),
        record: async () => ({ mimeType: "audio/webm", bytes: new Uint8Array([1]) }),
        transcribe: () => new Promise(resolve => { finish = resolve; }),
      },
    });
    await act(async () => {
      host?.querySelector("button")?.click();
    });
    wsRef.current = replacement;
    await act(async () => { finish({ transcript: "old session words" }); });
    expect(oldSocket.send).not.toHaveBeenCalled();
    expect(replacement.send).not.toHaveBeenCalled();
  });

  it("does not send a transcript or the audio bytes into an error label", async () => {
    renderButton({
      deps: {
        getConfig: async () => ({ voice: { submit_mode: "direct" } }),
        record: async () => {
          throw new Error("blob secret-token-ABC data:audio/webm;base64,QQ==");
        },
        transcribe: async () => ({ transcript: "should-not-send" }),
      },
    });
    await act(async () => {
      host?.querySelector("button")?.dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
    });
    const button = host?.querySelector("button");
    expect(button?.getAttribute("aria-label")).toBe("Talk failed");
    expect(button?.textContent).not.toContain("secret-token");
    expect(button?.textContent).not.toContain("base64");
    expect(button?.textContent).not.toContain("should-not-send");
  });
});
