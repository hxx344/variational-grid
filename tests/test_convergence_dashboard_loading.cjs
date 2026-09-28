/* Exercise real page code with deterministic fetch, timers and DOM nodes. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const M = require('../variational_grid/web/model.js');
const code = fs.readFileSync(path.join(__dirname,'../variational_grid/web/convergence.js'),'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));

function page(search='') {
  const nodes = new Map(), requests = [], timers = new Map(), events = {}, windowEvents = {};
  let serial = 0, active = true, activity, now = 1801200000000;
  function node(id) {
    if (!nodes.has(id)) nodes.set(id,{id,textContent:'',innerHTML:'',children:[],dataset:{},attributes:{},clientWidth:500,clientHeight:260,
      setAttribute(name,value) {this.attributes[name]=String(value);},getAttribute(name) {return this.attributes[name] || id;},
      addEventListener(name,fn) {this[name]=fn;},focus() {document.activeElement=this;},showModal() {this.open=true;},close() {this.open=false;},
      getBoundingClientRect() {return {left:0};},appendChild(child) {this.children.push(child);}});
    return nodes.get(id);
  }
  const tabs = ['positions','trades','parameters'].map(view => Object.assign(node('tab-'+view),{dataset:{view}}));
  const ranges = ['1h','24h','7d'].map(range => Object.assign(node('range-'+range),{dataset:{range}}));
  const location = {search,pathname:'/cl-bz'};
  const document = {getElementById:node,querySelector:node,querySelectorAll:selector => selector.includes('data-range') ? ranges : selector.includes('data-view') ? tabs : [],
    addEventListener:(name,fn) => events[name]=fn,activeElement:null};
  const window = {GridModel:M,GridHub:{active:() => active,subscribe:fn => activity=fn},addEventListener:(name,fn) => windowEvents[name]=fn};
  vm.runInNewContext(code,{window,document,location,URLSearchParams,AbortController,Date:class extends Date {static now() {return now;}},
    history:{pushState:(_state,_unused,url) => location.search=url},ResizeObserver:class {observe() {}},
    setTimeout:(fn,ms) => {timers.set(++serial,{fn,ms}); return serial;},clearTimeout:id => timers.delete(id),setInterval:fn => events.interval=fn,
    fetch:(url,options) => new Promise((resolve,reject) => {
      const request = {url,options,signal:options.signal,resolve,reject,ignoreAbort:false}; requests.push(request);
      options.signal.addEventListener('abort',() => {if (!request.ignoreAbort) reject(Object.assign(new Error('aborted'),{name:'AbortError'}));});
    })});
  return {node,requests,location,tabs,events,windowEvents,
    respond(request,payload,status=200) {request.resolve({ok:status>=200 && status<300,status,json:async() => payload});},
    timer(ms,name) {const item=[...timers].find(([,timer]) => timer.ms===ms && (!name || timer.fn.name===name)); assert.ok(item,`missing timer ${ms} ${name || ''}`); timers.delete(item[0]); now+=ms; item[1].fn();},
    advance(ms) {now+=ms; events.interval();},
    choose(kind,value) {events.click({target:{closest:selector => selector===`[data-${kind}]` ? {dataset:{[kind]:value}} : null}});},
    active(value) {active=value;activity(value);}};
}
function snapshot(mark=70,generation='g1') {
  const ts=1801200000;
  return {kind:'cl_bz_scalper',server_ts:ts,reset:{generation,status:'idle'},reset_token:'cl-only-token',runtime:{status:'running'},details_available:true,
    pair_pause:{active:false,markets:{CL:{state:'open',source_ts:ts},BZ:{state:'open',source_ts:ts}}},
    parameters:{take_profit_percent:'0.1',quantity_barrels:'1',max_batches:30,initial_balance_usdc:'1000',paper_leverage:'5',max_margin_fraction:'.8',wait_seconds:450,fee_bps:'0',slippage_bps:'1'},
    summary:{mode:'cl_bz_scalper',ts,poll_seconds:10,sample_count:5,started_utc:'2026-09-29T00:00:00Z',market:{spread_bz_minus_cl:'4',cl_source_ts:ts,bz_source_ts:ts},
      scenarios:[{name:'cl-bz',cl_mark:String(mark),bz_mark:'74',total_pnl_usdc:'-0.7000',realized_pnl_usdc:'-0.4000',equity_usdc:'999.3',open_pairs:1,cl_barrels:'1',bz_barrels:'-1',margin_usdc:'28.8',margin_limit_usdc:'799.44',position_notional_usdc:'144',
        cl:{total_pnl_usdc:'0.3000'},bz:{total_pnl_usdc:'-1.0000'},scalper:{phase:'cooldown',active_entries:0,active_take_profits:1,occupied_batches:1,max_batches:30,cooldown_seconds:112.5,cooldown_remaining_seconds:80}}]},
    positions:[{id:1,qty:'1',entry_cl:'70',entry_bz:'74',tp_price:'70.07',opened:ts-100,valued_at:ts,unrealized_pnl_usdc:'-0.3',cl_unrealized_pnl_usdc:'0.1',bz_unrealized_pnl_usdc:'-0.4'}],
    trades:[{id:2,qty:'1',entry_cl:'70',entry_bz:'74',exit_cl:'70.08',exit_bz:'74.48',opened:ts-200,closed:ts-100,cl_pnl:'.08',bz_pnl:'-.48',net_pnl:'-.4',reason:'cl_take_profit'}],
    orders:[{id:4,slot:1,side:'sell',price:'70.07',qty:'1',submitted_ts:ts-100}]};
}
function historical(request,generation='g1') {
  const params = new URL(request.url,'http://localhost').searchParams, ts=Number(params.get('through'));
  return {summary_ts:ts,reset_generation:generation,history:{range:params.get('range'),names:['cl-bz'],source_count:3,
    points:[{ts:ts-100,segment:0,pnl:[-.4],spread:4,center:null},{ts:ts-90,segment:0,pnl:[-.5],spread:4.1,center:null},{ts,segment:1,pnl:[-.7],spread:4.2,center:null}]}};
}

test('slow history does not block or restart the snapshot and its failure retains CL/BZ accounting', async() => {
  const p=page(); assert.equal(p.requests[0].url,'/api/cl-bz-snapshot');
  p.respond(p.requests[0],snapshot()); await flush();
  assert.equal(p.node('cl-mark').textContent,'70.0000');
  assert.equal(p.node('take-profit').textContent,'0.10%');
  assert.match(p.node('overview').innerHTML,/CL 多头净收益.*\+0.3000.*BZ 空头净收益.*-1.0000/);
  assert.match(p.node('pnl-chart').innerHTML,/历史曲线正在加载/);
  const h=p.requests[1]; p.node('refresh').onclick(); p.respond(p.requests[2],snapshot(71)); await flush();
  assert.equal(h.signal.aborted,false); assert.equal(p.requests.length,3);
  p.respond(h,{},503); await flush();
  assert.match(p.node('history-note').textContent,/HTTP 503/);
  assert.equal(p.node('cl-mark').textContent,'71.0000');
  assert.equal(p.node('status').textContent,'模拟运行中');
});

test('independent eight-second timeouts preserve successful curves and visibly age the snapshot', async() => {
  const p=page(); p.respond(p.requests[0],snapshot()); await flush();
  p.respond(p.requests[1],historical(p.requests[1])); await flush();
  assert.match(p.node('pnl-chart').innerHTML,/<svg/);
  p.choose('range','7d'); p.timer(8000); await flush();
  assert.match(p.node('history-note').textContent,/历史曲线读取超时/);
  assert.match(p.node('history-note').textContent,/窗口 24 小时.*保留上次成功曲线/);
  assert.match(p.node('pnl-chart').innerHTML,/<svg/);
  p.node('refresh').onclick(); p.timer(8000); await flush();
  assert.match(p.node('notice').textContent,/当前账户读取超时.*保留上次/);
  assert.equal(p.node('cl-mark').textContent,'70.0000');
  p.advance(60000); assert.match(p.node('notice').textContent,/行情已过期/);
});

test('obsolete range and reset generation responses cannot replace the displayed generation', async() => {
  const p=page(); p.respond(p.requests[0],snapshot()); await flush();
  const old=p.requests[1]; old.ignoreAbort=true;
  p.choose('range','1h'); assert.match(p.location.search,/range=1h/);
  p.respond(old,historical(old)); await flush();
  assert.doesNotMatch(p.node('pnl-chart').innerHTML,/<svg/);
  p.timer(0,'refreshHistory'); const current=p.requests.at(-1);
  p.respond(current,historical(current)); await flush();
  assert.match(p.node('history-note').textContent,/窗口 1 小时/);
  p.node('refresh').onclick(); p.respond(p.requests.at(-1),snapshot(72,'g2')); await flush();
  assert.doesNotMatch(p.node('pnl-chart').innerHTML,/<svg/);
  const next=p.requests.at(-1); p.respond(next,historical(next,'g1')); await flush();
  assert.doesNotMatch(p.node('pnl-chart').innerHTML,/<svg/);
  assert.equal(p.node('cl-mark').textContent,'72.0000');
});

test('CL TP with a losing BZ hedge stays negative and source strings cannot inject HTML', async() => {
  const p=page(), data=snapshot(); data.trades[0].reason='<img src=x onerror=alert(1)>';
  p.respond(p.requests[0],data); await flush();
  assert.match(p.node('detail-content').innerHTML,/CL 止盈价.*70.0700/);
  assert.doesNotMatch(p.node('detail-content').innerHTML,/净止盈目标|入场中枢/);
  assert.match(p.node('orders').innerHTML,/卖出止盈.*70.0700/);
  p.choose('view','trades');
  assert.match(p.node('detail-content').innerHTML,/-0.4000/);
  assert.match(p.node('detail-content').innerHTML,/&lt;img src=x/);
  assert.doesNotMatch(p.node('detail-content').innerHTML,/<img/);
  assert.match(p.node('detail-note').textContent,/CL 按自身价格止盈.*双腿合计收益可能为负/);
});

test('keyboard tabs, gap segments and sample slider expose the same current values', async() => {
  const p=page(); p.respond(p.requests[0],snapshot()); await flush();
  p.respond(p.requests[1],historical(p.requests[1])); await flush();
  const chart=p.node('pnl-chart').innerHTML;
  assert.equal((chart.match(/d="M[^\"]* M/g) || []).length,1);
  assert.doesNotMatch(p.node('spread-chart').innerHTML,/stroke-dasharray="6 4"/);
  p.node('sample').oninput({target:{value:'0'}});
  assert.match(p.node('sample-values').textContent,/-0.4000 USDC/);
  assert.match(p.node('sample').attributes['aria-valuetext'],/组合净收益/);
  p.node('.tabs').keydown({target:p.tabs[0],key:'End',preventDefault() {}});
  assert.equal(p.tabs[2].attributes['aria-selected'],'true');
  assert.match(p.node('detail-content').innerHTML,/0.10%/);
  assert.match(p.node('detail-content').innerHTML,/112.5 秒/);
});

test('reset uses only the CL/BZ endpoint and captured generation; a changed account requires a new confirmation', async() => {
  const p=page(); p.respond(p.requests[0],snapshot()); await flush();
  p.node('reset').onclick(); assert.equal(p.node('reset-dialog').open,true);
  p.node('reset-confirm').onclick();
  const reset=p.requests.at(-1); assert.equal(reset.url,'/api/cl-bz-reset');
  assert.equal(reset.options.method,'POST');
  assert.equal(reset.options.headers['X-Reset-Token'],'cl-only-token');
  assert.deepEqual(JSON.parse(reset.options.body),{generation:'g1'});
  p.respond(reset,{reset:{generation:'g2',status:'complete'}}); await flush();
  p.respond(p.requests.at(-1),snapshot(70,'g2')); await flush();
  assert.doesNotMatch(p.node('pnl-chart').innerHTML,/<svg/);
  p.node('reset').onclick(); p.node('refresh').onclick(); p.respond(p.requests.at(-1),snapshot(71,'g3')); await flush();
  const count=p.requests.length; p.node('reset-confirm').onclick();
  assert.equal(p.requests.length,count);
  assert.match(p.node('reset-message').textContent,/账户状态已变化/);
  assert.equal(p.requests.some(request => request.url==='/api/reset'),false);
});

test('hub inactivity aborts both requests and resumes with fresh data, without emptying account cards', async() => {
  const p=page(); p.respond(p.requests[0],snapshot()); await flush();
  const h=p.requests[1]; p.node('refresh').onclick(); const s=p.requests.at(-1);
  p.active(false); await flush();
  assert.equal(h.signal.aborted,true); assert.equal(s.signal.aborted,true);
  assert.equal(p.node('cl-mark').textContent,'70.0000');
  p.active(true); assert.equal(p.requests.at(-1).url,'/api/cl-bz-snapshot');
  p.respond(p.requests.at(-1),snapshot(71)); await flush();
  assert.equal(p.requests.at(-1).url.startsWith('/api/cl-bz-history'),true);
});

test('missing details and expired portal access are distinct from zero holdings and market closure', async() => {
  const p=page(), data=snapshot(); data.details_available=false;
  p.respond(p.requests[0],data); await flush();
  assert.match(p.node('notice').textContent,/当前仅有汇总数据/);
  assert.match(p.node('orders').innerHTML,/暂不可用/);
  assert.match(p.node('detail-content').innerHTML,/暂不可用/);
  p.node('refresh').onclick(); p.respond(p.requests.at(-1),{},401); await flush();
  assert.match(p.node('notice').textContent,/页面访问授权失效/);
  assert.equal(p.node('cl-mark').textContent,'70.0000');
});

test('fresh metadata and new paused frames never refresh old RFQ valuation timestamps', async() => {
  const p=page(), data=snapshot(); data.summary.market.cl_source_ts-=120; data.summary.market.bz_source_ts-=110;
  p.respond(p.requests[0],data); await flush();
  assert.equal(p.node('cl-source').textContent,'报价时间 ' + M.date(data.summary.market.cl_source_ts));
  assert.equal(p.node('status').textContent,'行情已过期');
  assert.match(p.node('freshness').textContent,/120 秒前/);
  assert.match(p.node('notice').textContent,/行情已过期/);
});

test('optional navigation hides on failure and only links server-enabled local strategies', async() => {
  const navigation=fs.readFileSync(path.join(__dirname,'../variational_grid/web/strategies.js'),'utf8');
  for (const failure of [true,false]) {
    const host={hidden:true,children:[],appendChild(value) {this.children.push(value);}};
    vm.runInNewContext(navigation,{AbortController,setTimeout,clearTimeout,location:{pathname:'/cl-bz'},
      document:{getElementById:() => host,createElement:() => ({setAttribute(name,value) {this[name]=value;}})},
      fetch:async() => ({ok:!failure,json:async() => ({strategies:[{id:'primary',label:'QQQ / US100',url:'/'},{id:'cl-bz',label:'CL / BZ 剥头皮',url:'/cl-bz'},{id:'evil',label:'external',url:'https://example.com'}]})})});
    await flush();
    assert.equal(host.hidden,failure); assert.equal(host.children.length,failure ? 0 : 2);
    if (!failure) assert.equal(host.children[1]['aria-current'],'page');
  }
});
