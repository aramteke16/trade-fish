import React, { useEffect, useState } from 'react'
import { getConfig, patchConfig, resetConfig } from '../api'
import ResetCapitalModal from './ResetCapitalModal'

function inputFor(item, value, onChange, allItems) {
  const base = {
    width: '100%', padding: '6px 8px', fontSize: 13, borderRadius: 2,
    border: '1px solid #222', background: '#000', color: '#e5e5e5', outline: 'none',
  }

  if (item.input_type === 'time') {
    return <input style={base} type="time" value={value ?? ''} onChange={(e) => onChange(e.target.value)} />
  }

  // Provider-bound model dropdown
  if (item.provider_models) {
    const providerItem = allItems.find((i) => i.key === 'llm_provider')
    const provider = providerItem?.value || ''
    const models = item.provider_models[provider] || []
    if (models.length > 0) {
      return (
        <select style={base} value={value ?? ''} onChange={(e) => onChange(e.target.value)}>
          {!models.includes(value) && value && <option value={value}>{value}</option>}
          {models.map((m) => (
            <option key={m} value={m}>{m}</option>
          ))}
        </select>
      )
    }
    return <input style={base} type="text" value={value ?? ''} placeholder="Enter model name"
                  onChange={(e) => onChange(e.target.value)} />
  }

  if (item.options && Array.isArray(item.options)) {
    return (
      <select style={base} value={value ?? ''} onChange={(e) => {
        const v = e.target.value
        onChange(v === '' || v === 'null' ? null : v)
      }}>
        {item.options.map((opt) => (
          <option key={String(opt)} value={opt ?? 'null'}>{opt === null ? '(none)' : opt}</option>
        ))}
      </select>
    )
  }
  if (item.is_secret) {
    return <input style={base} type="password" placeholder="(unchanged)"
                  onChange={(e) => onChange(e.target.value || null)} />
  }
  if (typeof item.value === 'boolean' || value === true || value === false) {
    return (
      <select style={base} value={String(value)} onChange={(e) => onChange(e.target.value === 'true')}>
        <option value="true">true</option>
        <option value="false">false</option>
      </select>
    )
  }
  if (typeof item.value === 'number') {
    return <input style={base} type="number" value={value ?? ''}
                  onChange={(e) => onChange(e.target.value === '' ? null : Number(e.target.value))} />
  }
  if (typeof item.value === 'object' && item.value !== null) {
    return (
      <textarea
        style={{ ...base, fontFamily: 'monospace', minHeight: 56, fontSize: 12 }}
        value={typeof value === 'string' ? value : JSON.stringify(value, null, 2)}
        onChange={(e) => onChange(e.target.value)}
      />
    )
  }
  return <input style={base} type="text" value={value ?? ''} onChange={(e) => onChange(e.target.value)} />
}

const PROVIDER_KEY_MAP = {
  moonshot: 'moonshot_api_key',
  anthropic: 'anthropic_api_key',
  openai: 'openai_api_key',
  google: 'google_api_key',
  xai: 'xai_api_key',
  deepseek: 'deepseek_api_key',
  qwen: 'dashscope_api_key',
  glm: 'zhipu_api_key',
  openrouter: 'openrouter_api_key',
}

export default function SettingsForm() {
  const [grouped, setGrouped] = useState({})
  const [drafts, setDrafts] = useState({})
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState(null)
  const [resetCapitalOpen, setResetCapitalOpen] = useState(false)

  async function load() {
    const g = await getConfig()
    setGrouped(g)
    setDrafts({})
  }
  useEffect(() => { load() }, [])

  function setDraft(key, val) {
    setDrafts((d) => ({ ...d, [key]: val }))
  }

  // Flat list of all items for cross-referencing (provider lookup)
  const allItems = Object.values(grouped).flat().map((item) => {
    if (drafts.hasOwnProperty(item.key)) return { ...item, value: drafts[item.key] }
    return item
  })

  async function saveOne(item) {
    setBusy(true); setMsg(null)
    try {
      let v = drafts[item.key]
      if (typeof item.value === 'object' && item.value !== null && typeof v === 'string') {
        try { v = JSON.parse(v) } catch { /* keep string */ }
      }
      await patchConfig(item.key, v)
      setMsg(`Saved ${item.key}`)
      await load()
    } catch (e) {
      setMsg(`Error: ${e.message}`)
    } finally {
      setBusy(false)
    }
  }

  async function doReset() {
    if (!confirm('Reset ALL config to defaults?')) return
    setBusy(true)
    try {
      await resetConfig()
      await load()
      setMsg('Reset to defaults.')
    } catch (e) { setMsg(`Error: ${e.message}`) }
    finally { setBusy(false) }
  }

  const cats = Object.keys(grouped).sort()
  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
        <h2 style={{ margin: 0, fontSize: 16, fontWeight: 600 }}>Settings</h2>
        <button onClick={doReset} disabled={busy} style={{
          background: 'transparent', color: '#666', border: '1px solid #222',
          padding: '5px 12px', borderRadius: 2, cursor: 'pointer', fontSize: 12,
        }}>Reset all</button>
      </div>
      {msg && <div style={{ color: '#888', marginBottom: 12, fontSize: 12 }}>{msg}</div>}

      {cats.map((cat) => (
        <div key={cat} style={{ border: '1px solid #1a1a1a', borderRadius: 2, padding: 16, marginBottom: 12 }}>
          <div style={{ fontSize: 11, color: '#555', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 12 }}>
            {cat}
          </div>
          {grouped[cat].map((item) => {
            if (item.is_secret && item.key.endsWith('_api_key')) {
              const currentProvider = allItems.find((i) => i.key === 'llm_provider')?.value
              if (currentProvider) {
                const activeKey = PROVIDER_KEY_MAP[currentProvider]
                if (!activeKey || item.key !== activeKey) return null
              }
            }
            const draft = drafts.hasOwnProperty(item.key) ? drafts[item.key] : item.value
            const dirty = drafts.hasOwnProperty(item.key) && drafts[item.key] !== item.value
            return (
              <div key={item.key} style={{
                display: 'grid', gridTemplateColumns: '220px 1fr auto',
                gap: 12, padding: '8px 0', alignItems: 'center', borderBottom: '1px solid #111',
              }}>
                <div>
                  <div style={{ fontSize: 13, fontWeight: 500 }}>{item.key}</div>
                  <div style={{ color: '#444', fontSize: 11, marginTop: 2 }}>{item.description}</div>
                </div>
                <div>{inputFor(item, draft, (v) => setDraft(item.key, v), allItems)}</div>
                <div>
                  <button
                    onClick={() => saveOne(item)}
                    disabled={!dirty || busy}
                    style={{
                      background: dirty ? '#fff' : '#1a1a1a',
                      color: dirty ? '#000' : '#333',
                      border: 'none', padding: '5px 12px', borderRadius: 2,
                      cursor: dirty ? 'pointer' : 'default', fontWeight: 600, fontSize: 11,
                    }}
                  >Save</button>
                </div>
              </div>
            )
          })}
        </div>
      ))}

      <div style={{ borderTop: '1px solid #1a1a1a', marginTop: 24, paddingTop: 20, display: 'flex', justifyContent: 'flex-end' }}>
        <button
          onClick={() => setResetCapitalOpen(true)}
          style={{
            background: 'transparent', color: '#c33', border: '1px solid #c33',
            padding: '6px 16px', borderRadius: 2, cursor: 'pointer', fontSize: 12, fontWeight: 600,
          }}
        >Reset Paper Capital</button>
      </div>

      <ResetCapitalModal
        open={resetCapitalOpen}
        onClose={() => setResetCapitalOpen(false)}
        onDone={() => setMsg('Capital reset successfully.')}
      />
    </div>
  )
}
