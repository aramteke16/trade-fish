import { useEffect, useRef, useState } from 'react'

// Reconnecting WebSocket hook. Returns the latest message + a connected flag.
// Caller passes an onMessage callback if it wants every message; useful for
// streaming UIs that want to append (LiveDebateStream).
export default function useWebSocket(path = '/ws/live', { onMessage } = {}) {
  const [connected, setConnected] = useState(false)
  const [lastMessage, setLastMessage] = useState(null)
  const wsRef = useRef(null)
  const backoffRef = useRef(1000)
  const onMessageRef = useRef(onMessage)
  onMessageRef.current = onMessage

  useEffect(() => {
    let stopped = false

    function connect() {
      const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
      const url = `${proto}://${window.location.host}${path}`
      const ws = new WebSocket(url)
      wsRef.current = ws

      ws.onopen = () => {
        setConnected(true)
        backoffRef.current = 1000
      }
      ws.onmessage = (evt) => {
        let msg
        try {
          msg = JSON.parse(evt.data)
        } catch {
          msg = { type: 'raw', data: evt.data }
        }
        setLastMessage(msg)
        onMessageRef.current?.(msg)
      }
      ws.onclose = () => {
        setConnected(false)
        if (stopped) return
        const delay = Math.min(backoffRef.current, 10000)
        backoffRef.current = Math.min(backoffRef.current * 2, 10000)
        setTimeout(connect, delay)
      }
      ws.onerror = () => ws.close()
    }
    connect()
    return () => {
      stopped = true
      wsRef.current?.close()
    }
  }, [path])

  return { connected, lastMessage, send: (msg) => wsRef.current?.send(JSON.stringify(msg)) }
}
