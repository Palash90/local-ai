import { useEffect, useRef, useState, useCallback } from 'react'

export default function ImageLightbox({ src, onClose }) {
  const [zoom, setZoom] = useState(1)
  const [panX, setPanX] = useState(0)
  const [panY, setPanY] = useState(0)
  const dragging = useRef(false)
  const startX = useRef(0)
  const startY = useRef(0)
  const pinchDist = useRef(0)

  function handleClose() {
    onClose()
    if (window.history.state && window.history.state.lightboxOpen) {
      window.history.back()
    }
  }

  useEffect(() => {
    if (!src) return
    const onPopState = () => onClose()
    window.addEventListener('popstate', onPopState)
    if (!(window.history.state && window.history.state.lightboxOpen)) {
      window.history.pushState({ lightboxOpen: true }, '')
    }
    return () => window.removeEventListener('popstate', onPopState)
  }, [src, onClose])

  const updateTransform = useCallback(() => {
    const img = document.getElementById('fullscreen-img')
    if (img) {
      img.style.transform = `scale(${zoom}) translate(${panX}px, ${panY}px)`
    }
    const label = document.getElementById('zoom-label')
    if (label) label.textContent = Math.round(zoom * 100) + '%'
  }, [zoom, panX, panY])

  useEffect(() => {
    if (!src) return
    setZoom(1)
    setPanX(0)
    setPanY(0)
    const img = document.getElementById('fullscreen-img')
    if (img) {
      img.src = src
      img.style.transform = ''
    }
    const label = document.getElementById('zoom-label')
    if (label) label.textContent = '100%'
  }, [src])

  useEffect(() => {
    if (!src) return
    const overlay = document.getElementById('image-overlay')

    function handleWheel(e) {
      e.preventDefault()
      const delta = e.deltaY > 0 ? -0.1 : 0.1
      setZoom(z => Math.max(0.25, Math.min(10, z + delta)))
    }

    function handleMouseDown(e) {
      const img = document.getElementById('fullscreen-img')
      if (e.target === img) {
        dragging.current = true
        startX.current = e.clientX - panX
        startY.current = e.clientY - panY
        overlay.style.cursor = 'grabbing'
        e.preventDefault()
      } else {
        handleClose()
      }
    }

    function handleMouseMove(e) {
      if (!dragging.current) return
      setPanX(e.clientX - startX.current)
      setPanY(e.clientY - startY.current)
    }

    function handleMouseUp() {
      if (dragging.current) {
        dragging.current = false
        overlay.style.cursor = 'grab'
      }
    }

    function handleTouchStart(e) {
      if (e.touches.length === 2) {
        const dx = e.touches[0].clientX - e.touches[1].clientX
        const dy = e.touches[0].clientY - e.touches[1].clientY
        pinchDist.current = Math.sqrt(dx * dx + dy * dy)
        dragging.current = false
        e.preventDefault()
      } else if (e.touches.length === 1 && e.target === document.getElementById('fullscreen-img')) {
        dragging.current = true
        startX.current = e.touches[0].clientX - panX
        startY.current = e.touches[0].clientY - panY
        e.preventDefault()
      }
    }

    function handleTouchMove(e) {
      if (e.touches.length === 2) {
        const dx = e.touches[0].clientX - e.touches[1].clientX
        const dy = e.touches[0].clientY - e.touches[1].clientY
        const dist = Math.sqrt(dx * dx + dy * dy)
        if (pinchDist.current > 0) {
          setZoom(z => Math.max(0.25, Math.min(10, z * (dist / pinchDist.current))))
          pinchDist.current = dist
        }
        e.preventDefault()
      } else if (e.touches.length === 1 && dragging.current) {
        setPanX(e.touches[0].clientX - startX.current)
        setPanY(e.touches[0].clientY - startY.current)
        e.preventDefault()
      }
    }

    function handleTouchEnd(e) {
      if (dragging.current && e.touches.length === 0) {
        dragging.current = false
      }
      if (e.changedTouches.length === 1 && e.target !== document.getElementById('fullscreen-img')) {
        handleClose()
      }
    }

    function handleKeyDown(e) {
      if (e.key === 'Escape') handleClose()
    }

    overlay.addEventListener('wheel', handleWheel, { passive: false })
    overlay.addEventListener('mousedown', handleMouseDown)
    document.addEventListener('mousemove', handleMouseMove)
    document.addEventListener('mouseup', handleMouseUp)
    overlay.addEventListener('touchstart', handleTouchStart, { passive: false })
    overlay.addEventListener('touchmove', handleTouchMove, { passive: false })
    overlay.addEventListener('touchend', handleTouchEnd)
    document.addEventListener('keydown', handleKeyDown)

    return () => {
      overlay.removeEventListener('wheel', handleWheel)
      overlay.removeEventListener('mousedown', handleMouseDown)
      document.removeEventListener('mousemove', handleMouseMove)
      document.removeEventListener('mouseup', handleMouseUp)
      overlay.removeEventListener('touchstart', handleTouchStart)
      overlay.removeEventListener('touchmove', handleTouchMove)
      overlay.removeEventListener('touchend', handleTouchEnd)
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [src, onClose, panX, panY])

  useEffect(() => {
    updateTransform()
  }, [zoom, panX, panY, updateTransform])

  if (!src) return null

  return (
    <div id="image-overlay" className="open">
      <div className="img-wrap"><img id="fullscreen-img" /></div>
      <div className="zoom-label" id="zoom-label">100%</div>
    </div>
  )
}
