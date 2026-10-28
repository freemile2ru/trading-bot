import { useEffect, useRef } from 'react'

/**
 * One combined-stream websocket to a Binance futures endpoint. `onMessage(stream, data)` gets
 * every message; reconnects with backoff. Pass a new `streams` list (e.g. on symbol change)
 * to reconnect. Messages are delivered via a ref, so the handler can change without reconnecting.
 */
export function useBinanceStream(base: string | undefined, streams: string[],
                                 onMessage: (stream: string, data: any) => void) {
  const handler = useRef(onMessage)
  handler.current = onMessage
  const key = streams.join('/')

  useEffect(() => {
    if (!base || !key) return
    let ws: WebSocket | null = null
    let closed = false
    let delay = 1000
    let timer: ReturnType<typeof setTimeout> | undefined
    let lastMsg = Date.now()
    // A socket can go silent without ever closing (laptop sleep, network switch) — the book
    // and chart then freeze with no error. Book and trade streams tick several times a
    // second, so 20s of silence means the connection is dead: drop it and reconnect.
    const watchdog = setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN && Date.now() - lastMsg > 20_000) ws.close()
    }, 5000)

    const open = () => {
      lastMsg = Date.now()
      ws = new WebSocket(`${base}?streams=${key}`)
      ws.onopen = () => { delay = 1000 }
      ws.onerror = () => ws?.close()
      ws.onmessage = ev => {
        lastMsg = Date.now()
        try {
          const m = JSON.parse(ev.data)
          if (m.stream) handler.current(m.stream, m.data)
        } catch { /* ignore malformed frames */ }
      }
      ws.onclose = () => {
        if (closed) return
        timer = setTimeout(open, delay)
        delay = Math.min(delay * 2, 15000)
      }
    }
    open()
    return () => {
      closed = true
      clearInterval(watchdog)
      if (timer) clearTimeout(timer)
      ws?.close()
    }
  }, [base, key])
}
