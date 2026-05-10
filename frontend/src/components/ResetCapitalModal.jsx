import React, { useState } from 'react'
import { resetCapital } from '../api'

export default function ResetCapitalModal({ open, onClose, onDone, initialCapital }) {
  const [amount, setAmount] = useState(initialCapital ?? 20000)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)
  if (!open) return null

  async function submit() {
    setBusy(true); setErr(null)
    try {
      await resetCapital(Number(amount))
      onDone?.()
      onClose()
    } catch (e) {
      setErr(String(e.message || e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.8)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100 }}
      onClick={onClose}
    >
      <div
        style={{ background: '#0a0a0a', border: '1px solid #222', borderRadius: 2, padding: 28, width: 380 }}
        onClick={(e) => e.stopPropagation()}
      >
        <h3 style={{ margin: '0 0 8px', fontSize: 15, fontWeight: 600 }}>Reset Paper Capital</h3>
        <p style={{ color: '#777', fontSize: 13, margin: '0 0 16px', lineHeight: 1.5 }}>
          Sets a new starting capital row dated today. Paper-trading only.
        </p>
        <label style={{ fontSize: 12, color: '#555' }}>Amount</label>
        <input
          style={{
            width: '100%', padding: '8px 10px', fontSize: 14, borderRadius: 2, marginTop: 4,
            border: '1px solid #222', background: '#000', color: '#e5e5e5', outline: 'none',
          }}
          type="number" value={amount} onChange={(e) => setAmount(e.target.value)} min={1}
        />
        {err && <div style={{ color: '#999', fontSize: 12, marginTop: 8 }}>{err}</div>}
        <div style={{ marginTop: 20, display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button
            onClick={onClose} disabled={busy}
            style={{ background: 'transparent', color: '#666', border: '1px solid #222', padding: '7px 14px', borderRadius: 2, cursor: 'pointer', fontSize: 13 }}
          >Cancel</button>
          <button
            onClick={submit} disabled={busy}
            style={{ background: '#fff', color: '#000', border: 'none', padding: '7px 14px', borderRadius: 2, cursor: 'pointer', fontSize: 13, fontWeight: 600 }}
          >{busy ? 'Resetting...' : 'Reset'}</button>
        </div>
      </div>
    </div>
  )
}
