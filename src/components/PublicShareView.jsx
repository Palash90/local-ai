import { useState, useEffect, useCallback } from 'react'
import { fetchPublicShare } from '../api'
import Message from './Message'
import ImageLightbox from './ImageLightbox'

export default function PublicShareView({ token, onExit }) {
  const [state, setState] = useState({ loading: true, error: '', message: null, sharedBy: '' })
  const [lightboxSrc, setLightboxSrc] = useState(null)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    let alive = true
    fetchPublicShare(token)
      .then(data => {
        if (!alive) return
        if (data && data.message) {
          setState({ loading: false, error: '', message: data.message, sharedBy: data.shared_by || '' })
        } else {
          setState({
            loading: false,
            error: (data && data.error) || 'This shared message is no longer available.',
            message: null,
            sharedBy: '',
          })
        }
      })
      .catch(() => {
        if (alive) setState({ loading: false, error: 'Could not load the shared message.', message: null, sharedBy: '' })
      })
    return () => { alive = false }
  }, [token])

  const openImage = useCallback(src => setLightboxSrc(src), [])
  const closeLightbox = useCallback(src => setLightboxSrc(null), [])

  const copyLink = useCallback(async () => {
    const url = window.location.href
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(url)
      } else {
        const ta = document.createElement('textarea')
        ta.value = url
        document.body.appendChild(ta)
        ta.select()
        document.execCommand('copy')
        document.body.removeChild(ta)
      }
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch { }
  }, [])

  return (
    <div id="public-share-view">
      <div className="public-share-topbar">
        <span className="public-share-label">Shared message</span>
        <button type="button" className="public-share-copylink" onClick={copyLink} title="Copy link to this shared message">
          {copied ? 'Copied!' : 'Copy link'}
        </button>
      </div>
      {state.loading ? (
        <div className="public-share-status">Loading…</div>
      ) : state.error ? (
        <div className="public-share-status error">{state.error}</div>
      ) : (
        <div className="public-share-body">
          <div className="public-share-meta">Shared by <strong>{state.sharedBy || 'someone'}</strong></div>
          <Message msg={state.message} onImageOpen={openImage} hideMeta shareToken={token} />
        </div>
      )}
      <ImageLightbox src={lightboxSrc} onClose={closeLightbox} />
    </div>
  )
}
