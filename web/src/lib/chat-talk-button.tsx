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

async function defaultGetConfig(profile?: string): Promise<unknown> {
  const { api } = await import("@/lib/api");
  return api.getConfig(profile);
}

async function defaultTranscribe(
  profile: string | undefined,
  body: { data_url: string; mime_type: string },
): Promise<{ transcript?: string }> {
  return fetchJSON(transcribeUrl(profile), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
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

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      stopRef.current?.();
    };
  }, []);

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
    running.current = true;
    cancelBeforeRecord.current = false;
    setPhase("recording");
    try {
      const result = await runTalkTurn({
        profile,
        getConfig: deps?.getConfig ?? defaultGetConfig,
        record: deps?.record ?? defaultRecord,
        transcribe: deps?.transcribe ?? defaultTranscribe,
        cancelled: () => !alive.current || cancelBeforeRecord.current,
        registerStop: (stop) => {
          stopRef.current = stop;
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
