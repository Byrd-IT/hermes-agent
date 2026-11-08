import { useEffect, useRef, useState, type RefObject } from "react";
import { Mic, Square } from "lucide-react";

import { fetchJSON } from "@/lib/api";
import type { VoiceTalkSettings } from "./chat-talk";
import { transcribeUrl } from "./chat-talk";
import { sendPtyShortcutSequence } from "@/lib/pty-keyboard-shortcuts";
import type { PtyConnectionState } from "@/lib/pty-reconnect";

import {
  phaseFromTalkError,
  runTalkTurn,
} from "./chat-talk-session";
import {
  recordUntilStop,
  rmsFromAnalyser,
  type MicRecorder,
  type RecordHandle,
} from "./chat-talk-record";

type TalkPhase = "idle" | "recording" | "sending" | "failed" | "empty";

export interface ChatTalkButtonProps {
  color: string;
  profile?: string;
  ptyState: PtyConnectionState;
  wsRef: RefObject<WebSocket | null>;
  ptyStateRef: RefObject<PtyConnectionState>;
  termRef: RefObject<{ focus: () => void } | null>;
  /** Test seam. Production uses the existing config and audio routes. */
  deps?: {
    getConfig?: (profile?: string) => Promise<unknown>;
    transcribe?: (
      profile: string | undefined,
      body: { data_url: string; mime_type: string },
      signal?: AbortSignal,
    ) => Promise<{ transcript?: string }>;
    record?: (
      settings: VoiceTalkSettings,
      registerStop: (stop: () => void) => void,
    ) => Promise<{ mimeType: string; bytes: Uint8Array }>;
  };
}

const LABEL: Record<TalkPhase, string> = {
  idle: "talk",
  recording: "stop",
  sending: "sending",
  failed: "failed",
  empty: "no speech",
};

const ARIA: Record<TalkPhase, string> = {
  idle: "Talk",
  recording: "Stop",
  sending: "Sending voice",
  failed: "Talk failed",
  empty: "No speech heard",
};

const WS_OPEN = 1;

function socketUnusable(
  ws: WebSocket | null,
  expected: WebSocket | null,
): boolean {
  // Null and a non-OPEN socket keep the same identity, so a replacement
  // check never sees them. Only an OPEN socket can take a paste.
  if (ws == null || expected == null) return true;
  return ws.readyState !== WS_OPEN;
}

async function defaultGetConfig(profile?: string): Promise<unknown> {
  const { api } = await import("@/lib/api");
  return api.getConfig(profile);
}

async function defaultTranscribe(
  profile: string | undefined,
  body: { data_url: string; mime_type: string },
  signal?: AbortSignal,
): Promise<{ transcript?: string }> {
  return fetchJSON(transcribeUrl(profile), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
}

function defaultRecord(
  settings: VoiceTalkSettings,
  registerStop: (stop: () => void) => void,
): Promise<{ mimeType: string; bytes: Uint8Array }> {
  const AudioCtx =
    window.AudioContext ||
    (window as unknown as { webkitAudioContext?: typeof AudioContext })
      .webkitAudioContext;
  let readRms: (() => number | null) | undefined;
  let audioCtx: AudioContext | null = null;
  const handlePromise = (async () => {
    const handle = recordUntilStop(
      {
        maxMs: settings.maxRecordingSeconds * 1000,
        silenceThreshold: settings.silenceThreshold,
        silenceDurationMs: settings.silenceDurationSeconds * 1000,
      },
      {
        getUserMedia: (constraints) =>
          navigator.mediaDevices.getUserMedia(constraints),
        createRecorder: (stream) => {
          const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
            ? "audio/webm;codecs=opus"
            : "";
          const rec = mime
            ? new MediaRecorder(stream, { mimeType: mime })
            : new MediaRecorder(stream);
          if (AudioCtx) {
            try {
              audioCtx = new AudioCtx();
              const source = audioCtx.createMediaStreamSource(stream);
              const analyser = audioCtx.createAnalyser();
              analyser.fftSize = 2048;
              source.connect(analyser);
              const into = new Float32Array(analyser.fftSize);
              readRms = () =>
                rmsFromAnalyser(
                  {
                    getFloatTimeDomainData: (buf) =>
                      analyser.getFloatTimeDomainData(
                        buf as Float32Array<ArrayBuffer>,
                      ),
                  },
                  into,
                );
            } catch {
              readRms = undefined;
            }
          }
          return rec as unknown as MicRecorder;
        },
        readRms: () => readRms?.() ?? null,
      },
    );
    registerStop(handle.stop);
    return handle;
  })();

  return handlePromise.then(async (handle: RecordHandle) => {
    try {
      return await handle.done;
    } finally {
      if (audioCtx) {
        void audioCtx.close().catch(() => {});
      }
    }
  });
}

/**
 * Talk / Stop overlay for the dashboard chat. Same chrome as the other
 * chat overlays (lowercase, border-current). Uses the profile's existing
 * voice settings and submits on stop unless voice.submit_mode is draft.
 */
export function ChatTalkButton({
  color,
  profile,
  ptyState,
  wsRef,
  ptyStateRef,
  termRef,
  deps,
}: ChatTalkButtonProps) {
  const [phase, setPhase] = useState<TalkPhase>("idle");
  const alive = useRef(true);
  const stopRef = useRef<(() => void) | null>(null);
  const running = useRef(false);
  const cancelBeforeRecord = useRef(false);
  const turnCancelled = useRef(false);
  const turnSocket = useRef<WebSocket | null>(null);
  const recordStarted = useRef(false);
  const uploadAbort = useRef<AbortController | null>(null);

  function latchCancellation() {
    // A closed or replaced PTY is not a user Stop. Stop submits; cancel
    // must be latched before the recorder resolves or the turn transcribes.
    const already = turnCancelled.current;
    turnCancelled.current = true;
    if (!already) stopRef.current?.();
    uploadAbort.current?.abort();
  }

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      stopRef.current?.();
      uploadAbort.current?.abort();
    };
  }, []);

  useEffect(() => {
    // record() does not poll cancellation, so a replaced or closed PTY
    // would leave the microphone open until silence or the cap.
    if (!running.current) return;
    if (ptyState !== "open" || wsRef.current !== turnSocket.current) {
      latchCancellation();
    }
  }, [ptyState, wsRef]);

  useEffect(() => {
    if (phase !== "recording" && phase !== "sending") return;
    // ChatPage assigns wsRef.current inside the connect effect. That does
    // not change ptyState or the ref object, so this effect is the only
    // observer of a replacement that never re-renders the button. It stays
    // armed through the upload so a swap can abort the request in flight.
    const watchSocket = () => {
      if (
        running.current &&
        recordStarted.current &&
        (wsRef.current !== turnSocket.current ||
          socketUnusable(wsRef.current, turnSocket.current))
      ) {
        latchCancellation();
      }
    };
    watchSocket();
    const id = window.setInterval(watchSocket, 50);
    return () => window.clearInterval(id);
  }, [phase, wsRef]);

  const sessionOpen = ptyState === "open";
  const locked =
    phase === "sending" || (phase !== "recording" && !sessionOpen);

  async function onClick() {
    if (phase === "recording") {
      if (stopRef.current) stopRef.current();
      else cancelBeforeRecord.current = true;
      return;
    }
    if (running.current || !sessionOpen) return;
    const targetSocket = wsRef.current;
    const controller = new AbortController();
    uploadAbort.current = controller;
    turnSocket.current = targetSocket;
    turnCancelled.current = false;
    recordStarted.current = false;
    running.current = true;
    cancelBeforeRecord.current = false;
    setPhase("recording");
    try {
      const result = await runTalkTurn({
        profile,
        getConfig: deps?.getConfig ?? defaultGetConfig,
        record: (settings, registerStop) => {
          // Arm before the recorder registers stop. A socket that stays null
          // must not skip record(), and an upload must still be cancellable
          // when the recorder never registered a stop handle.
          recordStarted.current = true;
          return (deps?.record ?? defaultRecord)(settings, registerStop);
        },
        transcribe: (profileName, body) =>
          (deps?.transcribe ?? defaultTranscribe)(
            profileName,
            body,
            controller.signal,
          ),
        cancelled: () =>
          !alive.current ||
          cancelBeforeRecord.current ||
          turnCancelled.current ||
          wsRef.current !== targetSocket ||
          (recordStarted.current &&
            socketUnusable(wsRef.current, turnSocket.current)),
        registerStop: (stop) => {
          stopRef.current = stop;
          recordStarted.current = true;
          // Close can land after click and before the mic handle exists.
          // A null socket is already dead; stop now so the upload never starts.
          // Call stop directly: an earlier latch had no handle to invoke.
          if (turnCancelled.current || socketUnusable(wsRef.current, turnSocket.current)) {
            turnCancelled.current = true;
            stop();
          }
        },
        onPhase: (next) => {
          if (alive.current) setPhase(next);
        },
        send: (data) =>
          sendPtyShortcutSequence(
            wsRef.current,
            ptyStateRef.current ?? "closed",
            data,
          ),
      });
      if (!alive.current) return;
      setPhase(result.phase);
      if (result.focus) termRef.current?.focus();
    } catch (err) {
      if (!alive.current) return;
      setPhase(phaseFromTalkError(err));
    } finally {
      stopRef.current = null;
      running.current = false;
      uploadAbort.current = null;
    }
  }

  const label = LABEL[phase];
  return (
    <button
      type="button"
      onClick={() => {
        void onClick();
      }}
      disabled={locked}
      aria-label={ARIA[phase]}
      aria-pressed={phase === "recording"}
      title={
        phase === "recording"
          ? "Stop and send"
          : sessionOpen
            ? "Talk using voice settings"
            : "Waiting for the session"
      }
      className={[
        "absolute z-10",
        "bottom-2 left-2 px-2 py-1 text-xs sm:bottom-3 sm:left-3",
        "lg:bottom-4 lg:left-4",
        "rounded border border-current/30",
        "bg-black/20",
        "normal-case tracking-normal font-normal",
        "opacity-70 hover:opacity-100 hover:border-current/60",
        "transition-opacity duration-150",
        "disabled:opacity-40 disabled:pointer-events-none",
        phase === "recording" ? "opacity-100" : "",
      ].join(" ")}
      style={{ color }}
    >
      <span className="inline-flex items-center gap-1">
        {phase === "recording" ? (
          <Square className="h-3 w-3 shrink-0" />
        ) : (
          <Mic className="h-3 w-3 shrink-0" />
        )}
        <span className="hidden min-[400px]:inline tracking-wide">{label}</span>
      </span>
    </button>
  );
}
