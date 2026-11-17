// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ChatTalkButton } from "./chat-talk-button";
import { buildTalkPaste } from "./chat-talk";
import * as api from "@/lib/api";

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

function requireSignal(value: AbortSignal | undefined): AbortSignal {
  if (!(value instanceof AbortSignal)) {
    throw new Error("missing abort signal");
  }
  return value;
}

function renderButton(
  props: Partial<React.ComponentProps<typeof ChatTalkButton>> = {},
) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  const ws = props.wsRef?.current ?? ({ readyState: 1, send: vi.fn() } as unknown as WebSocket);
  const wsRef = props.wsRef ?? { current: ws };
  const ptyStateRef = props.ptyStateRef ?? { current: "open" as const };
  const termRef = props.termRef ?? { current: { focus: vi.fn() } };
  const render = (
    next: Partial<React.ComponentProps<typeof ChatTalkButton>> = {},
  ) => {
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
          {...next}
        />,
      );
    });
  };
  render();
  return { ws: wsRef.current ?? ws, termRef, wsRef, rerender: render };
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
    expect(termRef.current?.focus).toHaveBeenCalled();
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
    expect(host?.querySelector("button")?.getAttribute("aria-label")).toBe(
      "Talk failed",
    );
    expect(host?.textContent).not.toContain("old session words");
  });

  it("stops a running recorder when the PTY socket is replaced", async () => {
    const oldSocket = { readyState: 1, send: vi.fn() } as unknown as WebSocket;
    const replacement = { readyState: 1, send: vi.fn() } as unknown as WebSocket;
    const wsRef = { current: oldSocket };
    let stopped = false;
    const { rerender } = renderButton({
      wsRef,
      deps: {
        getConfig: async () => ({}),
        record: (_settings, registerStop) =>
          new Promise((resolve) => {
            registerStop(() => {
              stopped = true;
              resolve({ mimeType: "audio/webm", bytes: new Uint8Array([4]) });
            });
          }),
        transcribe: async () => ({ transcript: "should-not-send" }),
      },
    });
    await act(async () => {
      host?.querySelector("button")?.click();
    });
    expect(stopped).toBe(false);
    wsRef.current = replacement;
    rerender({ ptyState: "reconnecting" });
    await act(async () => {
      await Promise.resolve();
    });
    expect(stopped).toBe(true);
    expect(oldSocket.send).not.toHaveBeenCalled();
    expect(replacement.send).not.toHaveBeenCalled();
    expect(host?.querySelector("button")?.getAttribute("aria-label")).toBe(
      "Talk failed",
    );
    expect(host?.textContent).not.toContain("should-not-send");
  });

  it("does not transcribe or send when the same open socket's ptyState closes", async () => {
    const transcribe = vi.fn(async () => ({ transcript: "should-not-send" }));
    const ptyStateRef = { current: "open" as const };
    const { ws, rerender } = renderButton({
      ptyStateRef,
      deps: {
        getConfig: async () => ({}),
        record: (_settings, registerStop) =>
          new Promise((resolve) => {
            registerStop(() => {
              resolve({ mimeType: "audio/webm", bytes: new Uint8Array([4]) });
            });
          }),
        transcribe,
      },
    });
    await act(async () => {
      host?.querySelector("button")?.click();
    });
    rerender({ ptyState: "closed" });
    await act(async () => {
      await Promise.resolve();
    });
    expect(ptyStateRef.current).toBe("open");
    expect(ws.readyState).toBe(1);
    expect(transcribe).not.toHaveBeenCalled();
    expect(ws.send).not.toHaveBeenCalled();
    expect(host?.querySelector("button")?.getAttribute("aria-label")).toBe(
      "Talk failed",
    );
    expect(host?.textContent).not.toContain("should-not-send");
  });

  it("invokes a stop registered after the PTY already closed", async () => {
    let registerLater: ((stop: () => void) => void) | undefined;
    let finish: ((value: { mimeType: string; bytes: Uint8Array }) => void) | undefined;
    const transcribe = vi.fn(async () => ({ transcript: "should-not-send" }));
    const { rerender } = renderButton({
      deps: {
        getConfig: async () => ({}),
        record: (_settings, registerStop) =>
          new Promise((resolve) => {
            finish = resolve;
            registerLater = registerStop;
          }),
        transcribe,
      },
    });
    await act(async () => {
      host?.querySelector("button")?.click();
    });
    expect(registerLater).toEqual(expect.any(Function));
    rerender({ ptyState: "closed" });
    let stopped = false;
    await act(async () => {
      registerLater?.(() => {
        stopped = true;
        finish?.({ mimeType: "audio/webm", bytes: new Uint8Array([1]) });
      });
    });
    expect(stopped).toBe(true);
    expect(transcribe).not.toHaveBeenCalled();
    expect(host?.querySelector("button")?.getAttribute("aria-label")).toBe(
      "Talk failed",
    );
  });

  it("stops the recorder on unmount without painting a late phase", async () => {
    let stopped = false;
    renderButton({
      deps: {
        getConfig: async () => ({}),
        record: (_settings, registerStop) =>
          new Promise((resolve) => {
            registerStop(() => {
              stopped = true;
              resolve({ mimeType: "audio/webm", bytes: new Uint8Array([1]) });
            });
          }),
        transcribe: async () => ({ transcript: "gone" }),
      },
    });
    await act(async () => {
      host?.querySelector("button")?.click();
    });
    expect(stopped).toBe(false);
    await act(async () => {
      root?.unmount();
      root = null;
    });
    expect(stopped).toBe(true);
    expect(host?.textContent ?? "").not.toContain("gone");
  });

  it("does not upload when an open session's socket stays null", async () => {
    const transcribe = vi.fn(async () => ({ transcript: "should-not-send" }));
    const stop = vi.fn();
    const wsRef = { current: null as WebSocket | null };
    renderButton({
      wsRef,
      ptyState: "open",
      deps: {
        getConfig: async () => ({}),
        record: async (_settings, registerStop) => {
          registerStop(stop);
          return { mimeType: "audio/webm", bytes: new Uint8Array([1, 2, 3]) };
        },
        transcribe,
      },
    });
    await act(async () => {
      host?.querySelector("button")?.click();
    });
    expect(transcribe).not.toHaveBeenCalled();
    expect(stop).toHaveBeenCalledTimes(1);
    expect(host?.querySelector("button")?.getAttribute("aria-label")).toBe(
      "Talk failed",
    );
    expect(host?.textContent).not.toContain("should-not-send");
  });

  it("stops the recorder when the socket is replaced without a rerender", async () => {
    vi.useFakeTimers();
    try {
      const oldSocket = { readyState: 1, send: vi.fn() } as unknown as WebSocket;
      const replacement = { readyState: 1, send: vi.fn() } as unknown as WebSocket;
      const wsRef = { current: oldSocket };
      const stop = vi.fn();
      let finishRecord: ((value: { mimeType: string; bytes: Uint8Array }) => void) | undefined;
      renderButton({
        wsRef,
        deps: {
          getConfig: async () => ({}),
          record: (_settings, registerStop) =>
            new Promise((resolve) => {
              registerStop(() => {
                stop();
                finishRecord?.({
                  mimeType: "audio/webm",
                  bytes: new Uint8Array([4]),
                });
              });
              finishRecord = resolve;
            }),
          transcribe: async () => ({ transcript: "should-not-send" }),
        },
      });
      await act(async () => {
        host?.querySelector("button")?.click();
      });
      expect(stop).not.toHaveBeenCalled();
      wsRef.current = replacement;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(49);
      });
      expect(stop).not.toHaveBeenCalled();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1);
      });
      expect(stop).toHaveBeenCalledTimes(1);
      expect(oldSocket.send).not.toHaveBeenCalled();
      expect(replacement.send).not.toHaveBeenCalled();
      expect(host?.querySelector("button")?.getAttribute("aria-label")).toBe(
        "Talk failed",
      );
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it("aborts a pending upload on unmount and on a socket swap", async () => {
    vi.useFakeTimers();
    try {
      const oldSocket = { readyState: 1, send: vi.fn() } as unknown as WebSocket;
      const replacement = { readyState: 1, send: vi.fn() } as unknown as WebSocket;
      const wsRef = { current: oldSocket };
      let signal: AbortSignal | undefined;
      const transcribe = (
        _profile: string | undefined,
        _body: { data_url: string; mime_type: string },
        next?: AbortSignal,
      ) => {
        signal = next;
        return new Promise<{ transcript?: string }>((_resolve, reject) => {
          next?.addEventListener("abort", () => {
            reject(new DOMException("The operation was aborted.", "AbortError"));
          });
        });
      };
      renderButton({
        wsRef,
        deps: {
          getConfig: async () => ({}),
          record: async () => ({
            mimeType: "audio/webm",
            bytes: new Uint8Array([1]),
          }),
          transcribe,
        },
      });
      await act(async () => {
        host?.querySelector("button")?.click();
      });
      expect(signal).toBeInstanceOf(AbortSignal);
      const unmountSignal = requireSignal(signal);
      expect(unmountSignal.aborted).toBe(false);
      await act(async () => {
        root?.unmount();
        root = null;
      });
      expect(unmountSignal.aborted).toBe(true);
      expect(host?.textContent ?? "").not.toContain("should-not-send");

      signal = undefined;
      wsRef.current = oldSocket;
      renderButton({
        wsRef,
        deps: {
          getConfig: async () => ({}),
          record: async () => ({
            mimeType: "audio/webm",
            bytes: new Uint8Array([1]),
          }),
          transcribe,
        },
      });
      await act(async () => {
        host?.querySelector("button")?.click();
      });
      const swapSignal = requireSignal(signal);
      expect(swapSignal.aborted).toBe(false);
      wsRef.current = replacement;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(49);
      });
      expect(swapSignal.aborted).toBe(false);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1);
      });
      expect(swapSignal.aborted).toBe(true);
      expect(oldSocket.send).not.toHaveBeenCalled();
      expect(replacement.send).not.toHaveBeenCalled();
      expect(host?.textContent).not.toContain("should-not-send");
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it("stops and does not upload when the same socket is no longer open", async () => {
    vi.useFakeTimers();
    try {
      const socket = { readyState: 1, send: vi.fn() };
      const wsRef = { current: socket as unknown as WebSocket };
      const stop = vi.fn();
      const transcribe = vi.fn(async () => ({ transcript: "should-not-send" }));
      renderButton({
        wsRef,
        ptyState: "open",
        deps: {
          getConfig: async () => ({}),
          record: (_settings, registerStop) =>
            new Promise((resolve) => {
              registerStop(() => {
                stop();
                resolve({ mimeType: "audio/webm", bytes: new Uint8Array([4]) });
              });
            }),
          transcribe,
        },
      });
      await act(async () => {
        host?.querySelector("button")?.click();
      });
      expect(stop).not.toHaveBeenCalled();
      socket.readyState = 3;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(49);
      });
      expect(stop).not.toHaveBeenCalled();
      expect(transcribe).not.toHaveBeenCalled();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1);
      });
      expect(stop).toHaveBeenCalledTimes(1);
      expect(transcribe).not.toHaveBeenCalled();
      expect(socket.send).not.toHaveBeenCalled();
      expect(host?.querySelector("button")?.getAttribute("aria-label")).toBe(
        "Talk failed",
      );
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it("aborts the audio upload fetch when that same socket closes", async () => {
    vi.useFakeTimers();
    const fetchJSON = vi.spyOn(api, "fetchJSON").mockImplementation(
      (_url: string, init?: RequestInit) =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => {
            reject(new DOMException("The operation was aborted.", "AbortError"));
          });
        }),
    );
    try {
      const socket = { readyState: 1, send: vi.fn() };
      renderButton({
        wsRef: { current: socket as unknown as WebSocket },
        deps: {
          getConfig: async () => ({}),
          record: async () => ({
            mimeType: "audio/webm",
            bytes: new Uint8Array([1]),
          }),
        },
      });
      await act(async () => {
        host?.querySelector("button")?.click();
      });
      expect(fetchJSON).toHaveBeenCalledTimes(1);
      const init = fetchJSON.mock.calls[0]?.[1] as RequestInit | undefined;
      expect(init?.signal).toBeInstanceOf(AbortSignal);
      expect(init?.signal?.aborted).toBe(false);
      socket.readyState = 3;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(49);
      });
      expect(init?.signal?.aborted).toBe(false);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1);
      });
      expect(init?.signal?.aborted).toBe(true);
      expect(socket.send).not.toHaveBeenCalled();
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      fetchJSON.mockRestore();
      vi.useRealTimers();
    }
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
