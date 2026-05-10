import React, { useState } from 'react'
import { getFileContent, downloadFileUrl } from '../api'

export default function DayFiles({ files = [], date }) {
  const [viewing, setViewing] = useState(null)
  const [content, setContent] = useState('')
  const [loading, setLoading] = useState(false)

  async function viewFile(f) {
    setLoading(true)
    try {
      const data = await getFileContent(f.path)
      setContent(data.content || '')
      setViewing(f)
    } catch (e) {
      setContent(`Error: ${e.message}`)
      setViewing(f)
    } finally {
      setLoading(false)
    }
  }

  if (files.length === 0) {
    return <div style={{ color: '#333', fontSize: 13 }}>No files for this date</div>
  }

  return (
    <div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
        {files.map((f) => (
          <div key={f.path} style={{
            display: 'flex', justifyContent: 'space-between', alignItems: 'center',
            padding: '8px 0', borderBottom: '1px solid #111', fontSize: 12,
            flexWrap: 'wrap', gap: 8,
          }}>
            <div style={{ minWidth: 0, flex: 1 }}>
              <div style={{ fontWeight: 500, fontSize: 13, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {f.name}
              </div>
              <div style={{ color: '#333', fontSize: 11, marginTop: 2 }}>{f.size_display}</div>
            </div>
            <div style={{ display: 'flex', gap: 6, flexShrink: 0 }}>
              {f.viewable && (
                <button
                  onClick={() => viewFile(f)}
                  style={{
                    background: 'transparent', color: '#888', border: '1px solid #222',
                    padding: '3px 10px', borderRadius: 2, cursor: 'pointer', fontSize: 11,
                  }}
                >View</button>
              )}
              <a
                href={downloadFileUrl(f.path)}
                download={f.name}
                style={{
                  background: 'transparent', color: '#888', border: '1px solid #222',
                  padding: '3px 10px', borderRadius: 2, cursor: 'pointer', fontSize: 11,
                  textDecoration: 'none', display: 'inline-block',
                }}
              >Download</a>
            </div>
          </div>
        ))}
      </div>

      {viewing && (
        <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.8)', zIndex: 100, display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 16 }}
             onClick={() => setViewing(null)}>
          <div
            style={{
              background: '#0a0a0a', border: '1px solid #222', borderRadius: 2,
              width: '100%', maxWidth: 720, maxHeight: '80vh', display: 'flex', flexDirection: 'column',
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <div style={{
              display: 'flex', justifyContent: 'space-between', alignItems: 'center',
              padding: '10px 16px', borderBottom: '1px solid #1a1a1a',
            }}>
              <span style={{ fontSize: 13, fontWeight: 500 }}>{viewing.name}</span>
              <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                <a
                  href={downloadFileUrl(viewing.path)}
                  download={viewing.name}
                  style={{ color: '#888', fontSize: 11, textDecoration: 'none', border: '1px solid #222', padding: '2px 8px', borderRadius: 2 }}
                >Download</a>
                <button
                  onClick={() => setViewing(null)}
                  style={{ background: 'transparent', color: '#555', border: 'none', fontSize: 18, cursor: 'pointer', padding: 0 }}
                >x</button>
              </div>
            </div>
            <pre style={{
              margin: 0, padding: 16, fontSize: 12, fontFamily: 'monospace', color: '#999',
              overflow: 'auto', flex: 1, whiteSpace: 'pre-wrap', lineHeight: 1.5,
            }}>
              {loading ? 'Loading...' : content}
            </pre>
          </div>
        </div>
      )}
    </div>
  )
}
