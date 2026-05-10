import { useEffect, useState } from 'react'

// Polls an async fn at a fixed interval. Returns {data, error, refresh}.
// Cleans up its timer on unmount and pauses while the tab is hidden.
export default function usePolling(fn, intervalMs = 5000, deps = []) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  async function refresh() {
    try {
      const v = await fn()
      setData(v)
      setError(null)
    } catch (e) {
      setError(e)
    }
  }

  useEffect(() => {
    let stopped = false
    let timer

    async function tick() {
      if (stopped) return
      if (document.visibilityState === 'visible') await refresh()
      timer = setTimeout(tick, intervalMs)
    }
    tick()
    return () => {
      stopped = true
      clearTimeout(timer)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  return { data, error, refresh }
}
