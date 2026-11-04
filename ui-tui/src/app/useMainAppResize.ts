import { useEffect } from 'react'

import { RESIZE_COALESCE_MS } from '../config/timing.js'
import { createResizeCoalescer } from '../lib/resizeCoalescer.js'

const BRACKET_PASTE_ON = '\x1b[?2004h'
const BRACKET_PASTE_OFF = '\x1b[?2004l'

type ResizeStdout = {
  columns?: number
  isTTY?: boolean
  on: (event: 'resize', listener: () => void) => void
  off: (event: 'resize', listener: () => void) => void
  write: (data: string) => void
}

/**
 * Coalesce drag-resize bursts and keep bracketed paste on while the TUI
 * is attached. Lives outside useMainApp so that function can shrink
 * instead of sharing declaration lines to stay under its cap.
 */
export function useMainAppResize(stdout: ResizeStdout | undefined, setCols: (cols: number) => void): void {
  useEffect(() => {
    if (!stdout) {
      return
    }

    // A drag-resize emits a burst of 'resize' events; syncing `cols` on every
    // one remounts the visible transcript rows each tick (they're keyed on
    // cols so yoga re-measures), turning a smooth drag into a flickering
    // remount storm. Coalesce the burst with a leading+trailing throttle: the
    // first event reflows immediately (the drag stays responsive), the rest
    // collapse to at most one reflow per RESIZE_COALESCE_MS, and the trailing
    // edge always applies the final width so the settled layout is exact.
    const coalescer = createResizeCoalescer(() => setCols(stdout.columns ?? 80), RESIZE_COALESCE_MS)
    const sync = () => coalescer.schedule()

    stdout.on('resize', sync)

    if (stdout.isTTY) {
      stdout.write(BRACKET_PASTE_ON)
    }

    return () => {
      coalescer.cancel()
      stdout.off('resize', sync)

      if (stdout.isTTY) {
        stdout.write(BRACKET_PASTE_OFF)
      }
    }
  }, [setCols, stdout])
}
