const test = require('node:test');
const assert = require('node:assert/strict');
const C = require('../variational_grid/web/convergence.js');

test('CL TP is a percentage while margin budget is a fraction; missing economics stay missing', () => {
  assert.equal(C.tpLabel('0.1'),'0.10%');
  assert.equal(C.percent('.8'),'80.00%');
  for (const value of [undefined,null,'',false,NaN,Infinity]) {
    assert.equal(C.tpLabel(value),'—');
    assert.equal(C.floating({total_pnl_usdc:value,realized_pnl_usdc:'2'}),null);
  }
  assert.equal(C.floating({total_pnl_usdc:'-0.7',realized_pnl_usdc:'0.2'}),-.8999999999999999);
  assert.equal(C.floating({unrealized_pnl_usdc:'0',total_pnl_usdc:'5'}),0);
});

test('state shares range and detail view without importing an unrelated strategy selection', () => {
  assert.deepEqual(C.state('?range=7d&view=trades&strategy=qqq'),{range:'7d',view:'trades'});
  assert.deepEqual(C.state('?range=no&view=no'),{range:'24h',view:'positions'});
});

test('freshness uses server time and elapsed time; closed and unknown markets are explicit', () => {
  const data = {server_ts:1010,summary:{ts:1000,poll_seconds:10,market:{cl_source_ts:1000,bz_source_ts:1000}},runtime:{status:'running'}};
  assert.equal(C.freshness(data,5000,56000).stale,true);
  assert.equal(C.freshness(data,5000,55000).age,60);
  assert.equal(C.freshness(data,5000,5000,true).label,'页面连接中断');
  assert.equal(C.freshness({summary:null},5000,5000).age,null);
  data.summary.ts = 1010;
  data.summary.market.bz_source_ts = 900;
  assert.equal(C.freshness(data,5000,5000).stale,true);
  assert.equal(C.freshness(data,5000,5000).stamp,900);
  data.summary.market.bz_source_ts = null;
  assert.equal(C.freshness(data,5000,5000).label,'报价时间未知');
  assert.match(C.schedule({pair_pause:{markets:{CL:{closes_at:null},BZ:{closes_at:'2026-09-29T12:00:00Z'}}}}),/CL 关闭时间未知.*BZ 预定休市/);
});

test('execution describes CL price TP independently of combined PnL and pauses preserve holdings', () => {
  const data = {pair_pause:{active:false},summary:{scenarios:[{total_pnl_usdc:'-5',scalper:{phase:'cooldown',cooldown_seconds:112.5,cooldown_remaining_seconds:80}}]}};
  assert.match(C.execution(data),/CL 每批按自身价格止盈/);
  assert.match(C.execution(data),/组合净收益可能为负/);
  assert.match(C.execution(data),/80.0 秒.*112.5 秒/);
  data.pair_pause = {active:true,reason:'pre_close'};
  assert.match(C.execution(data),/已撤销模拟开仓及止盈，保留两腿持仓/);
  assert.match(C.execution(data),/恢复首帧只重建订单/);
  assert.equal(C.reason('cl_take_profit'),'CL 价格止盈，BZ 同步回补');
});

test('history rejects the wrong reset generation, range, account, future or unordered samples', () => {
  const payload = {summary_ts:100,reset_generation:'g1',history:{range:'24h',names:['cl-bz'],points:[{ts:90,pnl:[-1],spread:null,segment:0},{ts:100,pnl:[0],spread:4,segment:1}]}};
  const valid = value => C.acceptableHistory(value,'24h','g1',100,['cl-bz']);
  assert.equal(valid(payload),true);
  for (const patch of [{reset_generation:'old'},{summary_ts:101},{history:{...payload.history,range:'7d'}},{history:{...payload.history,names:['qqq']}},{history:{...payload.history,points:[{ts:100,pnl:[0]},{ts:90,pnl:[1]}]}},{history:{...payload.history,points:[{ts:101,pnl:[0]}]}}]) assert.equal(valid({...payload,...patch}),false);
});
