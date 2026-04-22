/**
 * AgentWatch - offscreen.js
 * Plays notification chime. MV3 service workers cannot play audio directly,
 * so the background script creates an offscreen document which receives
 * PLAY_CHIME messages and plays the bundled MP3.
 */
chrome.runtime.onMessage.addListener((msg) => {
  if (msg?.target !== 'offscreen') return;
  if (msg.type === 'PLAY_CHIME') {
    const audio = document.getElementById('chime');
    if (!audio) return;
    try {
      audio.volume = typeof msg.volume === 'number' ? msg.volume : 0.7;
      audio.currentTime = 0;
      const p = audio.play();
      if (p && typeof p.catch === 'function') p.catch(() => {});
    } catch { /* noop */ }
  }
  if (msg.type === 'COPY_TO_CLIPBOARD') {
    navigator.clipboard.writeText(msg.text).catch(() => {
      // Fallback: create temp textarea
      const ta = document.createElement('textarea');
      ta.value = msg.text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
    });
  }
});
