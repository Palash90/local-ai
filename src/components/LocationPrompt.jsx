export default function LocationPrompt({ onAllow, onDeny }) {
  return (
    <div id="location-overlay" onClick={onDeny}>
      <div id="location-dialog" onClick={e => e.stopPropagation()}>
        <div id="location-icon">📍</div>
        <div id="location-title">Share your location?</div>
        <div id="location-desc">
          Local AI can use your location to provide location-aware responses and search results. You can change this anytime in your browser settings.
        </div>
        <div id="location-actions">
          <button id="location-deny-btn" onClick={onDeny}>Deny</button>
          <button id="location-allow-btn" onClick={onAllow}>Allow</button>
        </div>
      </div>
    </div>
  )
}
