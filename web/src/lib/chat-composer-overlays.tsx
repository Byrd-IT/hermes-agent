import { CopyLastButton } from "@/lib/chat-copy-last-button";
import { ChatTalkButton } from "@/lib/chat-talk-button";
import type { PtyConnectionState } from "@/lib/pty-reconnect";
import type { RefObject } from "react";

/** Copy-last and Talk, so ChatPage does not grow for the voice control. */
export function ChatComposerOverlays(props: {
  onCopyLast: () => void;
  copied: boolean;
  color: string;
  profile?: string;
  ptyState: PtyConnectionState;
  wsRef: RefObject<WebSocket | null>;
  ptyStateRef: RefObject<PtyConnectionState>;
  termRef: RefObject<{ focus: () => void } | null>;
}) {
  return (
    <>
      <CopyLastButton
        onClick={props.onCopyLast}
        copied={props.copied}
        color={props.color}
      />
      <ChatTalkButton
        key={props.profile ?? ""}
        color={props.color}
        profile={props.profile}
        ptyState={props.ptyState}
        wsRef={props.wsRef}
        ptyStateRef={props.ptyStateRef}
        termRef={props.termRef}
      />
    </>
  );
}
