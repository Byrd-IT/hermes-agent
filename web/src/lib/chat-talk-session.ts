import {
  buildTranscribeRequest,
  bytesToBase64,
  deliverTalkTranscript,
  readVoiceTalkSettings,
  type VoiceTalkSettings,
} from "./chat-talk";
import { TalkRecordError } from "./chat-talk-record";

export type TalkUiPhase = "idle" | "recording" | "sending" | "failed" | "empty";

export interface TalkTurnResult {
  phase: TalkUiPhase;
  focus: boolean;
}

export function phaseFromTalkError(err: unknown): TalkUiPhase {
  if (err instanceof TalkRecordError && err.message === "Recording was empty") {
    return "empty";
  }
  return "failed";
}

export function phaseFromDelivery(
  delivery: "submitted" | "drafted" | "empty" | "closed" | "toolong",
): TalkTurnResult {
  if (delivery === "empty") return { phase: "empty", focus: false };
  if (delivery === "closed" || delivery === "toolong") return { phase: "failed", focus: false };
  return { phase: "idle", focus: true };
}

async function loadTalkSettings(
  getConfig: (profile?: string) => Promise<unknown>,
  profile: string | undefined,
): Promise<VoiceTalkSettings> {
  return readVoiceTalkSettings(await getConfig(profile));
}

/**
 * Record, transcribe with the existing audio route, then submit or draft.
 * Does not touch the DOM. The button applies the returned phase.
 */
export async function runTalkTurn(opts: {
  profile?: string;
  getConfig: (profile?: string) => Promise<unknown>;
  record: (
    settings: VoiceTalkSettings,
    registerStop: (stop: () => void) => void,
  ) => Promise<{ mimeType: string; bytes: Uint8Array }>;
  transcribe: (
    profile: string | undefined,
    body: { data_url: string; mime_type: string },
  ) => Promise<{ transcript?: string }>;
  send: (data: string) => boolean;
  cancelled: () => boolean;
  registerStop: (stop: () => void) => void;
  onPhase?: (phase: TalkUiPhase) => void;
}): Promise<TalkTurnResult> {
  try {
    return await runTalkTurnBody(opts);
  } catch (err) {
    // A cancelled stop can surface as an empty recording. That is a failed
    // turn, not "no speech" — the user did not finish a recording.
    if (opts.cancelled()) return { phase: "failed", focus: false };
    return { phase: phaseFromTalkError(err), focus: false };
  }
}

async function runTalkTurnBody(opts: {
  profile?: string;
  getConfig: (profile?: string) => Promise<unknown>;
  record: (
    settings: VoiceTalkSettings,
    registerStop: (stop: () => void) => void,
  ) => Promise<{ mimeType: string; bytes: Uint8Array }>;
  transcribe: (
    profile: string | undefined,
    body: { data_url: string; mime_type: string },
  ) => Promise<{ transcript?: string }>;
  send: (data: string) => boolean;
  cancelled: () => boolean;
  registerStop: (stop: () => void) => void;
  onPhase?: (phase: TalkUiPhase) => void;
}): Promise<TalkTurnResult> {
  const onPhase = opts.onPhase ?? (() => {});
  const settings = await loadTalkSettings(opts.getConfig, opts.profile);
  if (opts.cancelled()) return { phase: "idle", focus: false };
  const recorded = await opts.record(settings, opts.registerStop);
  // A recording already exists. Idle would look like a successful discard.
  if (opts.cancelled()) return { phase: "failed", focus: false };
  onPhase("sending");
  const body = buildTranscribeRequest(
    recorded.mimeType,
    bytesToBase64(recorded.bytes),
  );
  const result = await opts.transcribe(opts.profile, body);
  if (opts.cancelled()) return { phase: "failed", focus: false };
  const delivery = await deliverTalkTranscript({
    transcript: result.transcript ?? "",
    submitMode: settings.submitMode,
    send: opts.send,
  });
  return phaseFromDelivery(delivery);
}
