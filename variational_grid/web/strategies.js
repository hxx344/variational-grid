/* Optional navigation. A missing catalog never affects the existing monitor. */
(() => {
  'use strict';
  const host = document.getElementById('strategy-navigation');
  if (!host) return;
  const request = new AbortController(), timeout = setTimeout(() => request.abort(), 8000);
  fetch('/api/strategies', {cache:'no-store', signal:request.signal}).then(response => {
    if (!response.ok) throw new Error('Strategy catalog unavailable');
    return response.json();
  }).then(data => {
    const routes = new Map([['primary','/'], ['cl-bz','/cl-bz']]);
    const items = Array.isArray(data.strategies) ? data.strategies.filter(item => item && routes.get(item.id) === item.url && typeof item.label === 'string') : [];
    if (items.length < 2) return;
    for (const item of items) {
      const link = document.createElement('a');
      link.className = 'button'; link.href = item.url; link.textContent = item.label;
      if (location.pathname === item.url || (item.url === '/' && location.pathname === '/index.html')) link.setAttribute('aria-current','page');
      host.appendChild(link);
    }
    host.hidden = false;
  }).catch(() => { host.hidden = true; }).finally(() => clearTimeout(timeout));
})();
