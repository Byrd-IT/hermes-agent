/**
 * Talk control for the web dashboard chat.
 *
 * The chat pane is an xterm mirror of the TUI. This module does not add a
 * second speech stack: it reads the existing `voice` config (the same keys
 * ConfigPage edits) and posts audio to the existing `/api/audio/transcribe`
 * route. On stop, the transcript is one bracketed paste the dashboard TUI
 * owns: it clears the whole composer and submits only that text in direct
 * mode, and only when no menu is open. Draft mode inserts and does not submit.
 */

export type VoiceSubmitMode = "direct" | "draft";

/** Defaults match hermes_cli.config_defaults voice.* */
export const VOICE_TALK_DEFAULTS = {
  submitMode: "direct" as VoiceSubmitMode,
  maxRecordingSeconds: 120,
  silenceDurationSeconds: 3,
  silenceThreshold: 200,
};

/**
 * The TUI recognizes this prefix inside one bracketed paste and applies the
 * transcript itself (clear the whole composer, then submit). Raw keystrokes
 * are not used: Ctrl+U only clears one line, and Enter can land in a menu.
 */
export const DASHBOARD_TALK_PREFIX = "HERMES_DASH_TALK:";

/** Server rejects audio over 25 MB. Fail before the POST. */
export const MAX_TALK_AUDIO_BYTES = 25 * 1024 * 1024;

export interface VoiceTalkSettings {
  submitMode: VoiceSubmitMode;
  maxRecordingSeconds: number;
  silenceDurationSeconds: number;
  silenceThreshold: number;
}

export function normalizeVoiceSubmitMode(value: unknown): VoiceSubmitMode {
  return typeof value === "string" && value.trim().toLowerCase() === "draft"
    ? "draft"
    : "direct";
}

function positiveNumber(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0
    ? value
    : fallback;
}

function nonNegativeNumber(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : fallback;
}

/** Read the voice section the Config page already edits. Missing keys use defaults. */
export function readVoiceTalkSettings(config: unknown): VoiceTalkSettings {
  const voice =
    config && typeof config === "object" && "voice" in config
      ? (config as { voice?: unknown }).voice
      : undefined;
  const section =
    voice && typeof voice === "object"
      ? (voice as Record<string, unknown>)
      : {};
  return {
    submitMode: normalizeVoiceSubmitMode(section.submit_mode),
    maxRecordingSeconds: positiveNumber(
      section.max_recording_seconds,
      VOICE_TALK_DEFAULTS.maxRecordingSeconds,
    ),
    silenceDurationSeconds: nonNegativeNumber(
      section.silence_duration,
      VOICE_TALK_DEFAULTS.silenceDurationSeconds,
    ),
    silenceThreshold: nonNegativeNumber(
      section.silence_threshold,
      VOICE_TALK_DEFAULTS.silenceThreshold,
    ),
  };
}

/**
 * A transcript is typed into the TUI composer. Strip controls so it cannot
 * inject an early Enter, a Ctrl sequence, or a terminal escape.
 */
export function sanitizeTranscriptForPty(text: string): string {
  let cleaned = "";
  for (const ch of text) {
    const code = ch.codePointAt(0) ?? 0;
    // C0 controls, DEL, and Unicode line separators. A regex class would
    // trip no-control-regex and is easier to get wrong here.
    if (code <= 0x1f || code === 0x7f || code === 0x2028 || code === 0x2029) {
      cleaned += " ";
    } else {
      cleaned += ch;
    }
  }
  return cleaned.replace(/[ \t\n\f\v]+/g, " ").trim();
}

export function mimeBase(mimeType: string): string {
  const base = (mimeType || "audio/webm").split(";", 1)[0].trim().toLowerCase();
  return base.startsWith("audio/") ? base : "audio/webm";
}

export function buildTranscribeRequest(
  mimeType: string,
  base64Audio: string,
): { data_url: string; mime_type: string } {
  const mime = mimeBase(mimeType);
  return {
    data_url: `data:${mime};base64,${base64Audio}`,
    mime_type: mime,
  };
}

export function transcribeUrl(profile?: string): string {
  if (!profile) return "/api/audio/transcribe";
  return `/api/audio/transcribe?profile=${encodeURIComponent(profile)}`;
}

/**
 * One bracketed paste the dashboard TUI owns. Direct and draft both travel
 * this way; the TUI clears the whole composer and submits only in direct mode,
 * and only when no menu is open.
 */
export function buildTalkPaste(
  transcript: string,
  mode: VoiceSubmitMode,
): string {
  const text = sanitizeTranscriptForPty(transcript);
  if (!text) return "";
  const body = bytesToBase64(
    new TextEncoder().encode(JSON.stringify({ text, mode })),
  );
  return `\u001b[200~${DASHBOARD_TALK_PREFIX}${body}\u001b[201~`;
}

export type TalkDelivery = "submitted" | "drafted" | "empty" | "closed";

export async function deliverTalkTranscript(opts: {
  transcript: string;
  submitMode: VoiceSubmitMode;
  send: (data: string) => boolean;
}): Promise<TalkDelivery> {
  const paste = buildTalkPaste(opts.transcript, opts.submitMode);
  if (!paste) return "empty";
  if (!opts.send(paste)) return "closed";
  return opts.submitMode === "draft" ? "drafted" : "submitted";
}

export interface SilenceState {
  speechSeen: boolean;
  silentForMs: number;
}

export function nextSilenceState(
  prev: SilenceState,
  sample: {
    rms: number;
    elapsedMs: number;
    threshold: number;
    silenceDurationMs: number;
  },
): SilenceState & { stop: boolean } {
  const elapsed = sample.elapsedMs > 0 ? sample.elapsedMs : 0;
  if (sample.rms >= sample.threshold) {
    return { speechSeen: true, silentForMs: 0, stop: false };
  }
  if (!prev.speechSeen) {
    return { speechSeen: false, silentForMs: 0, stop: false };
  }
  const silentForMs = prev.silentForMs + elapsed;
  return {
    speechSeen: true,
    silentForMs,
    stop: silentForMs >= sample.silenceDurationMs,
  };
}

/** Map Web Audio float samples (-1..1) onto the 16-bit RMS scale voice.silence_threshold uses. */
export function floatRmsToScale(samples: ArrayLike<number>): number {
  if (samples.length === 0) return 0;
  let sum = 0;
  for (let i = 0; i < samples.length; i++) {
    const sample = samples[i] ?? 0;
    sum += sample * sample;
  }
  return Math.sqrt(sum / samples.length) * 32767;
}

export function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    const slice = bytes.subarray(i, i + chunk);
    binary += String.fromCharCode(...slice);
  }
  return btoa(binary);
}
