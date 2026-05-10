import React, { useEffect, useState } from 'react'
import { getDebates, getToday, getTokenStats, getDayFiles } from '../api'
import TokenUsagePanel from '../components/TokenUsagePanel'
import DayFiles from '../components/DayFiles'

const section = {
  marginBottom: 16,
  border: '1px solid #1a1a1a',
  borderRadius: 2,
  padding: 16,
}
const label = { fontSize: 11, color: '#555', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 12 }
const tdc = { padding: '6px 8px', borderBottom: '1px solid #111', fontSize: 12, fontVariantNumeric: 'tabular-nums' }

export default function HistoryDate() {
  const [date, setDate] = useState(new Date().toISOString().slice(0, 10))
  const [today, setToday] = useState(null)
  const [debates, setDebates] = useState([])
  const [tokens, setTokens] = useState([])
  const [files, setFiles] = useState([])
  const [err, setErr] = useState(null)

  useEffect(() => {
    let alive = true
    setErr(null)
    Promise.all([getToday(date), getDebates(date), getTokenStats(date), getDayFiles(date)])
      .then(([t, d, k, f]) => {
        if (!alive) return
        setToday(t); setDebates(d.debates || []); setTokens(k.rows || []); setFiles(f.files || [])
      })
      .catch((e) => alive && setErr(String(e.message || e)))
    return () => { alive = false }
  }, [date])

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16, flexWrap: 'wrap', gap: 8 }}>
        <h1 style={{ margin: 0, fontSize: 18, fontWeight: 600 }}>History</h1>
        <input
          type="date" value={date} onChange={(e) => setDate(e.target.value)}
          style={{
            padding: '6px 10px', fontSize: 13, borderRadius: 2,
            border: '1px solid #222', background: '#000', color: '#e5e5e5', outline: 'none',
          }}
        />
      </div>

      {err && <div style={{ color: '#888', marginBottom: 12, fontSize: 12 }}>{err}</div>}

      <div style={section}>
        <div style={label}>Trade plans — {date}</div>
        {(today?.trade_plans || []).length === 0 ? (
          <div style={{ color: '#333', fontSize: 13 }}>No plans for this date</div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', minWidth: 500 }}>
              <thead>
                <tr>
                  {['Ticker', 'Rating', 'Conf', 'Entry', 'SL', 'T1', 'Skip'].map((h) => (
                    <th key={h} style={{
                      textAlign: 'left', color: '#555', fontSize: 10, textTransform: 'uppercase',
                      letterSpacing: '0.06em', borderBottom: '1px solid #1a1a1a', padding: '6px 8px', fontWeight: 500,
                    }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {today.trade_plans.map((p) => (
                  <tr key={p.id}>
                    <td style={tdc}><span style={{ fontWeight: 500 }}>{p.ticker}</span></td>
                    <td style={tdc}>{p.rating}</td>
                    <td style={tdc}>{p.confidence_score}/10</td>
                    <td style={tdc}>{p.entry_zone_low?.toFixed(2)}–{p.entry_zone_high?.toFixed(2)}</td>
                    <td style={tdc}>{p.stop_loss?.toFixed(2)}</td>
                    <td style={tdc}>{p.target_1?.toFixed(2)}</td>
                    <td style={tdc}>{p.skip_rule || '--'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div style={section}>
        <div style={label}>Debates — {date}</div>
        {debates.length === 0 ? (
          <div style={{ color: '#333', fontSize: 13 }}>No debates recorded</div>
        ) : debates.map((d) => (
          <details key={d.id} style={{ marginBottom: 6, padding: 10, border: '1px solid #111', borderRadius: 2 }}>
            <summary style={{ cursor: 'pointer', fontWeight: 500, fontSize: 13 }}>
              {d.ticker} — round {d.round_num} — {d.verdict}
            </summary>
            <div style={{ marginTop: 8, fontSize: 12 }}>
              <div style={{ fontWeight: 600, marginBottom: 4, color: '#888' }}>Bull</div>
              <pre style={{
                whiteSpace: 'pre-wrap', fontFamily: 'monospace', fontSize: 11,
                color: '#777', background: '#0a0a0a', padding: 8, borderRadius: 2,
                maxHeight: 260, overflow: 'auto', border: '1px solid #111',
              }}>{d.bull_argument || '--'}</pre>
              <div style={{ fontWeight: 600, margin: '8px 0 4px', color: '#888' }}>Bear</div>
              <pre style={{
                whiteSpace: 'pre-wrap', fontFamily: 'monospace', fontSize: 11,
                color: '#777', background: '#0a0a0a', padding: 8, borderRadius: 2,
                maxHeight: 260, overflow: 'auto', border: '1px solid #111',
              }}>{d.bear_argument || '--'}</pre>
            </div>
          </details>
        ))}
      </div>

      <div style={section}>
        <div style={label}>LLM usage — {date}</div>
        <TokenUsagePanel rows={tokens} />
      </div>

      <div style={section}>
        <div style={label}>Reports & files — {date}</div>
        <DayFiles files={files} date={date} />
      </div>
    </div>
  )
}
