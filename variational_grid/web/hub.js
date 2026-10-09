(() => {
  'use strict';
  const embedded = window.parent !== window && /^p-[a-f0-9]{24}\.hub\.localhost$/.test(location.hostname);
  const origin = `${location.protocol}//hub.localhost${location.port ? ':' + location.port : ''}`;
  const listeners = new Set();
  let hostActive = !embedded, connected = false, backgroundUpdates = !embedded;
  const background = () => !hostActive || document.hidden;
  const active = () => navigator.onLine !== false && (!background() || backgroundUpdates);
  let previous = `${active()}:${background()}`;
  const notify = (force = false) => {
    const next = `${active()}:${background()}`;
    if (next === previous && !force) return;
    previous = next;
    for (const listener of listeners) listener(active());
  };
  const post = message => window.parent.postMessage({channel:'project-hub', version:1, ...message}, origin);
  window.GridHub = {active, background, delay:ms => background() ? Math.max(30000, ms) : ms,
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); }};
  document.addEventListener('visibilitychange', () => notify());
  window.addEventListener('online', () => notify());
  window.addEventListener('offline', () => notify());
  window.addEventListener('focus', () => notify(true));
  window.addEventListener('pageshow', () => notify(true));
  if (!embedded) return;
  window.addEventListener('message', event => {
    if (event.source !== window.parent || event.origin !== origin) return;
    const message = event.data;
    if (!message || typeof message !== 'object' || Array.isArray(message) || message.channel !== 'project-hub' || message.version !== 1) return;
    if (message.type === 'ready' && message.role === 'host') {
      connected = true;
      post({type:'ready', role:'module', capabilities:['activity']});
    } else if (connected && message.type === 'activity' && typeof message.active === 'boolean') {
      hostActive = message.active;
      backgroundUpdates = message.backgroundUpdates === true;
      notify();
    }
  });
  post({type:'ready', role:'module'});
})();
