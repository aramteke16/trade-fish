import React, { useState } from 'react'
import usePolling from '../hooks/usePolling'
import { getAnalyses, getAnalysisReport, downloadFileUrl } from '../api'

export default function OnDemandList({ refreshKey }) {
  const { data } = usePolling(getAnalyses, 5000, [refreshKey])
  const analyses = data?.analyses || []
  const [expanded, setExpanded] = useState(null)
  const [report, setReport] = useState(null)
  const [loading, setLoading] = useState(false)

  async function toggle(a) {
    if (expanded === a.id) {
      setExpanded(null)
      setReport(null)
      return
    }
    setExpanded(a.id)
    setReport(null)
    if (a.status === 'done') {
      setLoading(true)
      try {
        const r = await getAnalysisReport(a.id)
        setReport(r.content)
      } catch { setReport(null) }
      finally { setLoading(false) }
    }
  }

  if (analyses.length === 0) return null

  return (
    <div style={{ border: '1px solid #1a1a1a', borderRadius: 2, padding: 16 }}>
      <div style={{ fontSize: 11, color: '#555', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 12 }}>
        Recent analyses
      </div>
      {analyses.map((a) => (
        <div key={a.id} style={{ borderBottom: '1px solid #111' }}>
          <div
            onClick={() => toggle(a)}
            style={{
              display: 'flex', justifyContent: 'space-between', alignItems: 'center',
              padding: '10px 0', fontSize: 13, cursor: 'pointer',
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
              <span style={{ fontWeight: 600 }}>{a.ticker}</span>
              <span style={{ color: '#333', fontSize: 11 }}>{a.requested_at?.replace('T', ' ').slice(0, 19)}</span>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              {a.status === 'done' && a.report_path && (
                <a
                  href={downloadFileUrl(a.report_path + '/complete_report.md')}
                  download
                  onClick={(e) => e.stopPropagation()}
                  style={{
                    fontSize: 11, color: '#888', border: '1px solid #222',
                    padding: '2px 8px', borderRadius: 2, textDecoration: 'none',
                  }}
                >Download</a>
              )}
              {a.status === 'done' ? (
                <span style={{
                  fontSize: 11, fontWeight: 500, color: '#888',
                  border: '1px solid #222', padding: '2px 8px', borderRadius: 2,
                }}>
                  {expanded === a.id ? 'Hide' : 'View report'}
                </span>
              ) : a.status === 'error' ? (
                <span style={{
                  fontSize: 11, fontWeight: 500, color: '#666',
                  border: '1px solid #1a1a1a', padding: '2px 8px', borderRadius: 2,
                }}>
                  {expanded === a.id ? 'Hide' : 'Error'}
                </span>
              ) : (
                <span style={{ fontSize: 11, fontWeight: 500, color: '#555' }}>
                  {a.status}
                </span>
              )}
            </div>
          </div>

          {expanded === a.id && (
            <div style={{ paddingBottom: 12 }}>
              {a.status === 'running' || a.status === 'pending' ? (
                <div style={{ color: '#444', fontSize: 12, padding: '8px 0' }}>
                  Analysis in progress...
                </div>
              ) : a.status === 'error' ? (
                <pre style={{
                  margin: 0, color: '#777', fontSize: 11, fontFamily: 'monospace',
                  whiteSpace: 'pre-wrap', padding: 10, background: '#0a0a0a',
                  borderRadius: 2, border: '1px solid #111',
                }}>{a.error || 'Unknown error'}</pre>
              ) : a.status === 'done' ? (
                <>
                  {a.summary && (
                    <div style={{ color: '#888', fontSize: 12, marginBottom: 8 }}>{a.summary}</div>
                  )}
                  {loading ? (
                    <div style={{ color: '#444', fontSize: 12 }}>Loading report...</div>
                  ) : report ? (
                    <pre style={{
                      margin: 0, color: '#999', fontSize: 11, fontFamily: 'monospace',
                      whiteSpace: 'pre-wrap', maxHeight: 500, overflow: 'auto',
                      padding: 12, background: '#0a0a0a', borderRadius: 2,
                      border: '1px solid #111', lineHeight: 1.5,
                    }}>{report}</pre>
                  ) : (
                    <div style={{ color: '#444', fontSize: 12 }}>Report not available</div>
                  )}
                </>
              ) : null}
            </div>
          )}
        </div>
      ))}
    </div>
  )
}
