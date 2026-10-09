const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const M = require('../variational_grid/web/model.js');
const flush = () => new Promise(resolve => setImmediate(resolve));

function fixture(embedded = true) {
  const events = new Map(), sent = [], changes = [];
  const on = (name, fn) => events.set(name, [...(events.get(name) || []), fn]);
  const document = {hidden:false, addEventListener:on};
  const navigator = {onLine:true};
  const window = {addEventListener:on};
  window.parent = embedded ? {postMessage:(data, origin) => sent.push({data, origin})} : window;
  const context = {window, document, navigator, location:{hostname:embedded ? `p-${'a'.repeat(24)}.hub.localhost` : '127.0.0.1', protocol:'http:', port:'3100'}};
  vm.runInNewContext(fs.readFileSync('variational_grid/web/hub.js', 'utf8'), context);
  window.GridHub.subscribe(active => changes.push(active));
  const fire = (name, value) => {for (const fn of events.get(name) || []) fn(value);};
  const message = (data, extra = {}) => fire('message', {source:window.parent, origin:'http://hub.localhost:3100', data:{channel:'project-hub', version:1, ...data}, ...extra});
  return {...context, sent, changes, fire, message, active:window.GridHub.active};
}

test('embedded page stays idle until exact parent handshakes; forged activity cannot start work', () => {
  const f = fixture();
  assert.equal(f.active(), false);
  f.message({type:'activity', active:true});
  assert.equal(f.active(), false);
  f.message({type:'ready', role:'host'}, {origin:'http://other.localhost:3100'});
  f.message({type:'ready', role:'host'}, {source:{}});
  f.message({type:'activity', active:true});
  assert.equal(f.active(), false);
  f.message({type:'ready', role:'host'});
  assert.deepEqual(Array.from(f.sent.at(-1).data.capabilities), ['activity']);
  assert.equal(f.sent.at(-1).origin, 'http://hub.localhost:3100');
  f.message({type:'activity', active:true});
  f.message({type:'activity', active:true});
  f.message({type:'activity', active:false}, {origin:'http://hub.localhost:9999'});
  assert.deepEqual(f.changes, [true]);
  f.message({type:'activity', active:false});
  assert.deepEqual(f.changes, [true, false]);
});

test('old hosts still pause hidden reads while standalone keeps reading; offline always pauses', () => {
  for (const embedded of [true, false]) {
    const f = fixture(embedded);
    if (embedded) {f.message({type:'ready', role:'host'}); f.message({type:'activity', active:true});}
    assert.equal(f.active(), true);
    f.document.hidden = true; f.fire('visibilitychange');
    assert.equal(f.active(), !embedded);
    f.navigator.onLine = false; f.fire('offline');
    f.document.hidden = false; f.fire('visibilitychange');
    assert.equal(f.active(), false);
    f.navigator.onLine = true; f.fire('online');
    assert.equal(f.active(), true);
    assert.deepEqual(f.changes.slice(-2), [false, true]);
    if (!embedded) assert.deepEqual(f.sent, []);
  }
});

test('trusted background permission keeps hidden pages reading at a bounded cadence and wakes promptly', () => {
  const f = fixture();
  f.message({type:'activity', active:false, backgroundUpdates:true});
  assert.equal(f.active(), false, 'permission before the handshake is ignored');
  f.message({type:'ready', role:'host'});
  f.message({type:'activity', active:false, backgroundUpdates:true}, {origin:'http://evil.localhost:3100'});
  assert.equal(f.active(), false, 'permission from another origin is ignored');
  f.message({type:'activity', active:false, backgroundUpdates:true});
  assert.equal(f.active(), true);
  assert.equal(f.window.GridHub.delay(10000), 30000);
  assert.equal(f.window.GridHub.delay(60000), 60000);
  f.document.hidden = true; f.fire('visibilitychange');
  assert.equal(f.active(), true);
  f.navigator.onLine = false; f.fire('offline');
  assert.equal(f.active(), false);
  f.navigator.onLine = true; f.fire('online');
  assert.equal(f.active(), true);
  f.document.hidden = false; f.fire('visibilitychange');
  f.message({type:'activity', active:true, backgroundUpdates:true});
  assert.equal(f.window.GridHub.delay(10000), 10000);
  const before = f.changes.length;
  f.message({type:'activity', active:true, backgroundUpdates:true});
  assert.equal(f.changes.length, before, 'identical activity does not restart reads');
  f.fire('focus'); f.fire('pageshow');
  assert.deepEqual(f.changes.slice(before), [true, true]);
  f.message({type:'activity', active:false});
  assert.equal(f.active(), false, 'permission can be revoked without relaxing the old protocol');
});

function dashboard(name) {
  const f = fixture(), nodes = new Map(), timers = new Map(), requests = [];
  let serial = 0;
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, { id, textContent:'', innerHTML:'', dataset:{}, clientWidth:500, clientHeight:260,
      setAttribute() {}, getAttribute() { return id; }, addEventListener() {}, querySelectorAll() { return []; }, focus() {} });
    return nodes.get(id);
  };
  Object.assign(f.document, { hidden:true, getElementById:node, querySelector:node, querySelectorAll:() => [], activeElement:null });
  f.window.GridModel = M;
  f.location.search = ''; f.location.pathname = '/';
  vm.runInNewContext(fs.readFileSync(`variational_grid/web/${name}.js`, 'utf8'), { ...f,
    URLSearchParams, URL, AbortController, Date, performance, ResizeObserver:class {observe() {}}, history:{pushState() {}},
    setTimeout(fn,ms) {timers.set(++serial,{fn,ms}); return serial;}, clearTimeout:id => timers.delete(id), setInterval() {},
    fetch:(url,options) => new Promise(resolve => requests.push({url,signal:options.signal,resolve})),
  });
  return { ...f, requests, node, timers,
    respond(request,status) {request.resolve({ok:true,status:200,json:async() => ({kind:'cl_bz_scalper',summary:null,server_ts:Date.now()/1000,
      history:{points:[],names:[],range:'24h',source_count:0},parameters:{},runtime:{status},reset:{generation:'g1',status:'idle'},positions:[],trades:[]})});},
    poll(ms) {const entry = [...timers].find(([,timer]) => timer.fn.name === 'refresh' && timer.ms === ms); assert.ok(entry,`missing ${ms}ms refresh`); timers.delete(entry[0]); entry[1].fn();},
  };
}

for (const name of ['app','inventory','qqq','convergence']) test(`${name}: real bridge keeps hidden reads running and wake rejects obsolete requests`, async () => {
  const p = dashboard(name);
  assert.equal(p.requests.length,0);
  p.message({type:'ready',role:'host'}); p.message({type:'activity',active:false,backgroundUpdates:true});
  assert.equal(p.requests.length,1);
  p.respond(p.requests[0],'starting'); await flush();
  assert.doesNotMatch(p.node('status').textContent,/连接中断/);
  p.poll(30000); const old = p.requests.at(-1);
  p.fire('focus'); assert.equal(old.signal.aborted,true);
  const fresh = p.requests.at(-1); assert.notEqual(fresh,old);
  p.respond(fresh,'stopped'); await flush(); const freshStatus = p.node('status').textContent;
  p.respond(old,'running'); await flush();
  assert.equal(p.node('status').textContent,freshStatus);
  p.navigator.onLine = false; p.fire('offline');
  assert.equal([...p.timers.values()].some(timer => timer.fn.name === 'refresh'),false);
  const count = p.requests.length;
  p.navigator.onLine = true; p.fire('online'); assert.equal(p.requests.length,count+1);
  p.document.hidden = false; p.fire('visibilitychange'); p.message({type:'activity',active:true,backgroundUpdates:true});
  p.respond(p.requests.at(-1),'running'); await flush(); p.poll(10000);
  p.fire('pageshow'); assert.equal(p.requests.at(-2).signal.aborted,true);
  assert.ok(p.requests.every(request => !request.url.includes('reset')), 'wakes only issue read requests');
});
