/* The server owns prices, costs and accounting. This page only formats published values. */
(function (root) {
  'use strict';
  const M = typeof module !== 'undefined' && module.exports ? require('./model.js') : root.GridModel;
  const valid = value => typeof value !== 'boolean' && M.finite(value);
  const subtract = (a,b) => valid(a) && valid(b) ? Number(a) - Number(b) : null;
  const percent = value => valid(value) ? M.number(Number(value) * 100,2) + '%' : '—';
  const tpLabel = value => valid(value) ? M.number(value,2) + '%' : '—';
  const time = value => valid(value) ? Number(value) : typeof value === 'string' && Number.isFinite(Date.parse(value)) ? Date.parse(value) / 1000 : null;
  const reason = value => ({cl_take_profit:'CL 价格止盈，BZ 同步回补',market_closed:'市场休市',pre_close:'临近休市',close_only:'市场仅允许减仓',unknown:'市场状态未知',margin_limit:'保证金不足',capacity:'达到批数上限',cooldown:'等待冷却',entry_pending:'开仓订单处理中',quote_stale:'报价过期',quote_skew:'双侧报价时间偏差过大'}[value] || value || '—');
  function state(search) {
    const parsed = M.state(search, []);
    return {range:parsed.range,view:parsed.view};
  }
  function floating(row) {
    return valid(row?.unrealized_pnl_usdc) ? Number(row.unrealized_pnl_usdc) : subtract(row?.total_pnl_usdc,row?.realized_pnl_usdc);
  }
  function freshness(data, received, now = Date.now(), disconnected = false) {
    const server = valid(data?.server_ts) ? Number(data.server_ts) : received / 1000;
    const sources = [data?.summary?.ts,data?.summary?.market?.cl_source_ts,data?.summary?.market?.bz_source_ts];
    const known = sources.every(value => valid(value) && Number(value)>0 && Number(value)<=server+2);
    const stamp = known ? Math.min(...sources.map(Number)) : null;
    const age = stamp === null ? null : Math.max(0,server-stamp+(now-received)/1000);
    const stale = age !== null && age > Math.max(60,valid(data?.summary?.poll_seconds) ? Number(data.summary.poll_seconds) * 3 : 60);
    const runtime = data?.runtime?.status;
    const label = disconnected ? '页面连接中断' : stale ? '行情已过期' : data?.pair_pause?.active === true ? '双腿交易暂停' : data?.summary && !known ? '报价时间未知' : ({running:'模拟运行中',paused:'行情暂停',stopped:'模拟已停止',starting:'等待行情',resetting:'正在重置'}[runtime] || '状态未知');
    return {age,stamp,stale,label,warning:disconnected || stale || !known || data?.pair_pause?.active === true || runtime !== 'running'};
  }
  function schedule(data) {
    const markets = data?.pair_pause?.markets || {};
    return ['CL','BZ'].map(symbol => `${symbol} ${valid(time(markets[symbol]?.closes_at)) ? '预定休市 ' + M.date(time(markets[symbol].closes_at)) : '关闭时间未知'}`).join(' · ');
  }
  function execution(data) {
    const pause = data?.pair_pause, row = data?.summary?.scenarios?.[0];
    if (pause?.active === true) return `双腿暂停：${reason(pause.reason)}。已撤销模拟开仓及止盈，保留两腿持仓。${valid(time(pause.resume_after_ts)) ? '最早恢复检查 ' + M.date(time(pause.resume_after_ts)) + '；' : ''}等待双侧市场状态及新报价满足恢复条件；恢复首帧只重建订单。`;
    if (!row) return '等待有效报价与策略状态。';
    if (row.halted) return `策略已停止开仓：${reason(row.halted)}。`;
    if (pause?.active !== false) return '市场联动状态尚未确认，以服务端下一份有效采样为准。';
    const scalper = row.scalper || {}, phases = {cooldown:'等待冷却',entry_pending:'等待开仓报价满足限价',entry_working:'等待开仓报价满足限价',working:'等待新报价',ready:'等待下一次资格检查',capacity:'批次容量已满',full:'批次容量已满',paused:'交易暂停',resuming:'恢复首帧重建订单',margin_limit:'保证金不足'};
    return `${row.open_allowed === false ? '当前不允许新开仓：' + reason(row.skip_reason) + '。' : ''}CL 开仓状态：${phases[scalper.phase] || scalper.phase || '—'}。冷却剩余（采样值）${M.number(scalper.cooldown_remaining_seconds,1)} 秒 / 当前等待 ${M.number(scalper.cooldown_seconds,1)} 秒。CL 每批按自身价格止盈，BZ 随成交等桶对冲；组合净收益可能为负。`;
  }
  function acceptableHistory(payload, range, generation, through, names) {
    const h = payload?.history;
    return payload?.reset_generation === generation && valid(payload?.summary_ts) && Number(payload.summary_ts) <= Number(through)
      && h?.range === range && Array.isArray(h.names) && h.names.length === names.length && new Set(h.names).size === h.names.length
      && names.every(name => h.names.includes(name)) && Array.isArray(h.points)
      && h.points.every((point,index) => valid(point.ts) && Number(point.ts) <= Number(payload.summary_ts) && (!index || Number(point.ts) >= Number(h.points[index-1].ts)) && Array.isArray(point.pnl));
  }
  const model = {state,subtract,percent,tpLabel,time,reason,floating,freshness,schedule,execution,acceptableHistory};
  if (typeof module !== 'undefined' && module.exports) {module.exports = model; return;}
  root.ConvergenceModel = model;
  const $ = id => document.getElementById(id), E = M.escape, hub = root.GridHub;
  let data = null, chartData = null, ui = state(location.search), selectedTs = null, page = 0, snapshotGeneration;
  let timer, historyTimer, snapshotRequest = null, historyRequest = null, historyDue = 0;
  let received = 0, disconnected = false, connectionError = '', historyError = '', resetError = '', resetSending = false, resetIdentity = null;
  const row = () => data?.summary?.scenarios?.[0] || {};
  const names = () => (data?.summary?.scenarios || []).map(item => item.name);
  const points = () => chartData?.points || [];
  const pnl = value => `<span class="${M.tone(value)}">${M.signed(value,4)}</span>`;
  const stat = (label,value) => `<div><span>${E(label)}</span><b>${value}</b></div>`;
  const rangeLabel = range => ({'1h':'1 小时','24h':'24 小时','7d':'7 天'}[range] || '—');
  function resetStatus() {
    const reset = data?.reset, busy = ['pending','archiving','clearing'].includes(reset?.status);
    $('reset').disabled = !reset || !data?.reset_token || reset.generation == null || busy || resetSending || disconnected;
    let message = resetError;
    if (resetSending) message = '正在提交本组重置请求…';
    else if (busy) message = 'CL / BZ 本组重置请求处理中；QQQ / US100 继续保持原有账本。';
    else if (reset?.status === 'complete') message = 'CL / BZ 旧账本已归档，本组已开始新一轮。' + (reset.archive_id ? '归档编号 ' + reset.archive_id : '');
    else if (reset?.status === 'failed') message = '本组重置失败，请检查服务日志与当前账本状态后重试。';
    $('reset-message').textContent = message; $('reset-message').hidden = !message;
  }
  function status() {
    resetStatus();
    if (!data) return;
    const f = freshness(data,received,Date.now(),disconnected);
    $('status').textContent = f.label; $('status').className = 'status' + (disconnected ? ' error' : f.warning ? ' warn' : '');
    $('freshness').textContent = f.age === null ? '双腿报价时间尚未完整确认' : `估值来源 ${M.date(f.stamp)} · ${Math.floor(f.age)} 秒前`;
    const messages = [];
    if (data.summary?.data_kind === 'synthetic') messages.push('合成行情演示；这些采样不是实际市场行情。');
    if (disconnected) messages.push(connectionError + '保留上次成功读取的数据。');
    if (f.stale) messages.push('行情已过期，收益与仓位估值停留在最后有效采样。');
    if (data.summary && f.age === null) messages.push('缺少有效双腿报价来源时间，无法确认估值新鲜度。');
    if (data.runtime?.status === 'paused') messages.push('行情暂停：' + reason(data.runtime.reason));
    if (data.runtime?.status === 'stopped') messages.push('模拟进程已停止，显示最后保存的数据。');
    if (data.pair_pause?.active === true) messages.push(execution(data));
    if (data.summary && data.details_available !== true) messages.push('当前仅有汇总数据，仓位与成交明细暂不可用。');
    $('notice').textContent = messages.join(' '); $('notice').hidden = !messages.length;
  }
  function renderOverview() {
    const r = row(), s = data?.summary, p = data?.parameters || {};
    for (const symbol of ['CL','BZ']) {
      const key = symbol.toLowerCase(), market = data?.pair_pause?.markets?.[symbol];
      $(key + '-mark').textContent = M.number(r[key + '_mark'] ?? r[key]?.mark ?? s?.market?.[key + '_mark'],4);
      $(key + '-state').textContent = ({open:'市场开市',closed:'市场休市',close_only:'仅允许减仓',unknown:'市场状态未知'}[market?.state] || '市场状态未知');
      $(key + '-source').textContent = '报价时间 ' + M.date(time(s?.market?.[key + '_source_ts']));
    }
    $('spread-value').textContent = M.number(s?.market?.spread_bz_minus_cl ?? r.spread_bz_minus_cl,4);
    $('take-profit').textContent = tpLabel(p.take_profit_percent ?? r.take_profit_percent);
    $('session-schedule').textContent = schedule(data) + ' · 预定休市前 5 分钟撤销模拟开仓及止盈，保留两腿持仓';
    $('execution-state').textContent = execution(data);
    if (!s || !s.scenarios.length) {$('overview').innerHTML = '<div class="empty">等待本组第一份有效采样，已有配置和账本以服务端为准。</div>'; return;}
    $('experiment-info').textContent = `${M.number(s.sample_count,0)} 次采样 · 每 ${M.number(s.poll_seconds,0)} 秒 · 本轮开始 ${M.date(time(s.started_utc))}`;
    const qty = p.quantity_barrels ?? r.quantity_barrels, max = p.max_batches ?? r.scalper?.max_batches;
    $('overview').innerHTML = `<article class="strategy"><h3>双腿累计净收益 / USDC</h3><strong class="big-pnl ${M.tone(r.total_pnl_usdc)}">${M.signed(r.total_pnl_usdc,4)}</strong><div class="strategy-stats">${stat('CL 多头净收益 / USDC',pnl(r.cl?.total_pnl_usdc))}${stat('BZ 空头净收益 / USDC',pnl(r.bz?.total_pnl_usdc))}${stat('组合已实现 / USDC',pnl(r.realized_pnl_usdc))}${stat('组合持仓浮盈亏 / USDC',pnl(floating(r)))}</div></article>`
      + `<article class="strategy"><h3>CL 当前持仓</h3><strong class="capacity">${M.number(r.open_pairs,0)} <small>批 · 最多占用 ${M.number(max,0)} 批</small></strong><div class="strategy-stats">${stat('CL 多头 / 桶',M.number(r.cl_barrels,3))}${stat('BZ 空头 / 桶',M.signed(r.bz_barrels,3))}${stat('已占用（含开仓单）',M.number(r.scalper?.occupied_batches,0) + ' 批')}${stat('开仓单 / 止盈单',M.number(r.scalper?.active_entries,0) + ' / ' + M.number(r.scalper?.active_take_profits,0))}${stat('每批每腿 / 桶',M.number(qty,3))}${stat('CL 逐批价格止盈',E(tpLabel(p.take_profit_percent ?? r.take_profit_percent)))}</div></article>`
      + `<article class="strategy"><h3>模拟权益 / USDC</h3><strong class="big-pnl">${M.number(r.equity_usdc,2)}</strong><div class="strategy-stats">${stat('保证金占用 / USDC',M.number(r.margin_usdc,2))}${stat('当前保证金上限 / USDC',M.number(r.margin_limit_usdc,2))}${stat('双腿名义金额 / USDC',M.number(r.position_notional_usdc,2))}${stat('模拟杠杆',M.number(p.paper_leverage ?? r.paper_leverage,1) + ' 倍')}</div></article>`;
  }
  function pointIndex() {
    const list = points();
    if (selectedTs === null || !list.length) return Math.max(0,list.length-1);
    let result = 0;
    list.forEach((point,index) => {if (Math.abs(point.ts-selectedTs) < Math.abs(list[result].ts-selectedTs)) result = index;});
    return result;
  }
  function drawChart(id,series,zero) {
    const host = $(id), list = points();
    if (!list.length) {host.innerHTML = `<div class="empty">${historyRequest ? '历史曲线正在加载…' : historyError ? '历史曲线暂不可用，当前账户数据独立更新。' : '等待有效历史采样'}</div>`; host.onclick = null; return;}
    const width = Math.max(260,host.clientWidth || 500), height = host.clientHeight || 260;
    const left = width < 420 ? 67 : 73, right = width-13, top = 18, bottom = height-35;
    const values = list.flatMap(point => series.map(item => item.get(point))), [lo,hi] = M.domain(values,zero);
    const first = Number(list[0].ts), last = Number(list.at(-1).ts);
    const x = value => first === last ? (left+right)/2 : left+(Number(value)-first)/(last-first)*(right-left);
    const y = value => bottom-(Number(value)-lo)/(hi-lo)*(bottom-top);
    const digits = hi-lo < .05 ? 4 : hi-lo < 5 ? 3 : 2;
    let svg = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${E(host.getAttribute('aria-label'))}"><title>${E(host.getAttribute('aria-label'))}，${E(M.date(first))} 至 ${E(M.date(last))}；使用下方采样条查看数值</title>`;
    for (let i=0;i<4;i++) {
      const value = lo+(hi-lo)*i/3;
      svg += `<line class="gridline" x1="${left}" x2="${right}" y1="${y(value)}" y2="${y(value)}"/><text x="${left-8}" y="${y(value)+4}" text-anchor="end">${M.number(value,digits)}</text>`;
    }
    if (zero && lo<0 && hi>0) svg += `<line class="zero" x1="${left}" x2="${right}" y1="${y(0)}" y2="${y(0)}"/>`;
    svg += `<text x="${left}" y="${height-8}" text-anchor="start">${E(M.date(first,true))}</text>`;
    if (first !== last) svg += `<text x="${right}" y="${height-8}" text-anchor="end">${E(M.date(last,true))}</text>`;
    for (const item of series) svg += `<path d="${M.path(list,item.get,x,y)}" stroke="${item.color}"${item.dashed ? ' stroke-dasharray="6 4"' : ''}/>`;
    const point = list[pointIndex()];
    svg += `<line class="cursor" x1="${x(point.ts)}" x2="${x(point.ts)}" y1="${top}" y2="${bottom}"/>`;
    for (const item of series) if (valid(item.get(point))) svg += `<circle cx="${x(point.ts)}" cy="${y(item.get(point))}" r="3.5" fill="${item.color}" stroke="white" stroke-width="1.5"/>`;
    host.innerHTML = svg + '</svg>';
    host.onclick = event => {
      const ratio = Math.max(0,Math.min(1,(event.clientX-host.getBoundingClientRect().left-left)/(right-left)));
      selectedTs = first+ratio*(last-first); renderCharts();
    };
  }
  function renderCharts() {
    const list = points(), index = chartData?.names?.indexOf(row().name) ?? -1;
    drawChart('pnl-chart',[{get:point => point.pnl?.[index],color:'#285de5'}],true);
    drawChart('spread-chart',[{get:point => point.spread,color:'#416c97'}],false);
    $('sample').max = Math.max(0,list.length-1); $('sample').value = pointIndex(); $('sample').disabled = !list.length;
    const point = list[pointIndex()];
    $('sample-time').textContent = point ? M.date(point.ts) : '—';
    $('sample-values').textContent = point ? `组合净收益 ${M.signed(point.pnl?.[index],4)} USDC · 观察价差 ${M.number(point.spread,4)} USDC/桶` : '';
    $('sample').setAttribute('aria-valuetext',point ? `${M.date(point.ts)}，${$('sample-values').textContent}` : '暂无历史采样');
    let note = chartData ? `窗口 ${rangeLabel(chartData.range)} · 原始 ${M.number(chartData.source_count,0)} 点 / 显示 ${list.length} 点${list.length ? ' · 曲线截至 ' + M.date(list.at(-1).ts) : ''}。` : '曲线每分钟刷新，当前账户独立更新。';
    note += '行情缺口断线显示；轻触曲线或使用采样条与方向键查看数值。';
    if (chartData && (chartData.range !== ui.range || historyError)) note += `保留上次成功曲线${list.length ? '，截至 ' + M.date(list.at(-1).ts) : ''}。`;
    if (historyError) note += historyError;
    else if (historyRequest) note += `正在加载 ${rangeLabel(ui.range)} 历史曲线…`;
    $('history-note').textContent = note;
    document.querySelectorAll('[data-range]').forEach(button => button.setAttribute('aria-pressed',button.dataset.range === ui.range));
  }
  function renderDetails() {
    document.querySelectorAll('[data-view]').forEach(button => {const active = button.dataset.view === ui.view; button.setAttribute('aria-selected',active); button.tabIndex = active ? 0 : -1;});
    $('detail-content').setAttribute('aria-labelledby','tab-' + ui.view);
    $('export').hidden = ui.view !== 'trades'; $('export').disabled = data?.details_available !== true || !data?.trades?.length;
    $('pagination').hidden = true;
    if (ui.view === 'parameters') {
      const p = data?.parameters || {}, r = row();
      const wait = valid(p.wait_seconds) ? Number(p.wait_seconds) : null;
      const values = [['方向','CL 多头剥头皮 / BZ 等桶空头对冲'],['每批每腿',M.number(p.quantity_barrels ?? r.quantity_barrels,3) + ' 桶'],['容量上限',M.number(p.max_batches ?? r.scalper?.max_batches,0) + ' 批（含待成交开仓单，受保证金约束）'],['CL 逐批价格止盈目标',tpLabel(p.take_profit_percent ?? r.take_profit_percent)],['CL 止盈取价','本批 CL 实际入场价 ×（1 + TP%）'],['初始资金',M.number(p.initial_balance_usdc ?? r.initial_balance_usdc,2) + ' USDC'],['模拟杠杆',M.number(p.paper_leverage ?? r.paper_leverage,1) + ' 倍'],['保证金预算',percent(p.max_margin_fraction ?? r.max_margin_fraction)],['每腿每次手续费',M.number(p.fee_bps,2) + ' bps'],['每腿每次滑点',M.number(p.slippage_bps,2) + ' bps'],['基础等待',M.number(wait,1) + ' 秒'],['按待止盈批数等待',`0–4 批 ${M.number(wait === null ? null : wait/4,1)} 秒；5–9 批 ${M.number(wait === null ? null : wait/2,1)} 秒；10–19 批 ${M.number(wait,1)} 秒；20–29 批 ${M.number(wait === null ? null : wait*2,1)} 秒`],['免冷却条件','首次开仓；CL 止盈关闭后的本轮资格检查'],['开仓改价',`挂出 ${M.number(p.reprice_after_seconds,0)} 秒后，每 ${M.number(p.reprice_poll_seconds,0)} 秒允许向上改价；实际按采样检查`],['报价最大年龄',M.number(p.max_quote_age_seconds,0) + ' 秒'],['双侧报价最大时间偏差',M.number(p.max_pair_skew_seconds,0) + ' 秒'],['模拟开仓条件','后续新 RFQ 的 CL ask 加滑点 ≤ 开仓限价；BZ 同步按 bid 扣滑点卖出'],['模拟止盈条件','后续新 RFQ 的 CL bid 扣滑点 ≥ TP 价；BZ 同步按 ask 加滑点回补'],['报价成交容量','每次一桶 RFQ 采样最多一笔一桶 CL 成交及对应 BZ 动作'],['价差与旧批次价格距离','不设开仓或止盈门槛'],['休市联动','预定休市前 5 分钟撤销模拟开仓及止盈，保留持仓；恢复首帧重建订单'],['资金费与隔夜费','未计入损益']];
      $('detail-content').innerHTML = `<dl class="parameters">${values.map(([key,value]) => `<div class="parameter"><dt>${E(key)}</dt><dd>${E(value)}</dd></div>`).join('')}</dl>`;
      $('detail-note').textContent = '参数以本组服务端配置为准。保证金仅作模拟估算；最多批数不代表一定能够全部开满。'; return;
    }
    const trades = ui.view === 'trades', records = (trades ? data?.trades : data?.positions) || [];
    $('detail-note').textContent = trades ? `CL 按自身价格止盈，BZ 同步回补；双腿合计收益可能为负。各腿净收益已计开平仓手续费和模拟滑点。导出包含已加载的 ${records.length} 条。` : '每批 CL 多头对应等桶 BZ 空头。TP 仅以 CL 实际入场价计算；浮盈亏含预计退出成本。价格单位为 USDC/桶。';
    if (data?.details_available !== true) {$('detail-content').innerHTML = '<div class="empty">仓位与成交明细暂不可用，请参考上方汇总数据。</div>'; return;}
    if (!records.length) {$('detail-content').innerHTML = `<div class="empty">${trades ? '暂无已平仓记录' : '当前没有未平仓的配对仓位'}</div>`; return;}
    const size = 15, pages = Math.ceil(records.length/size); page = Math.max(0,Math.min(page,pages-1));
    const headers = trades ? ['批次编号','每腿数量 / 桶','入场 CL / BZ','退出 CL / BZ','开仓 / 平仓时间','平仓原因','CL / BZ 净收益 / USDC','组合净收益 / USDC'] : ['批次编号','每腿数量 / 桶','入场 CL / BZ','CL 止盈价','开仓 / 估值时间','CL / BZ 浮盈亏 / USDC','组合浮盈亏 / USDC'];
    const body = records.slice(page*size,(page+1)*size).map(record => {
      const values = [E(record.id ?? '—'),M.number(record.qty,3),`CL ${M.number(record.entry_cl,4)}<span class="secondary">BZ ${M.number(record.entry_bz,4)}</span>`,trades ? `CL ${M.number(record.exit_cl,4)}<span class="secondary">BZ ${M.number(record.exit_bz,4)}</span>` : M.number(record.tp_price,4),`${M.date(record.opened)}<span class="secondary">${M.date(trades ? record.closed : record.valued_at)}</span>`,...(trades ? [E(reason(record.reason))] : []),`CL ${pnl(trades ? record.cl_pnl : record.cl_unrealized_pnl_usdc)}<span class="secondary">BZ ${pnl(trades ? record.bz_pnl : record.bz_unrealized_pnl_usdc)}</span>`,pnl(trades ? record.net_pnl : record.unrealized_pnl_usdc)];
      return '<tr>' + values.map((value,index) => `<td data-label="${E(headers[index])}">${value}</td>`).join('') + '</tr>';
    }).join('');
    $('detail-content').innerHTML = `<div class="table-wrap" tabindex="0" aria-label="${trades ? '已平仓记录' : '逐批配对持仓'}"><table class="records-table"><thead><tr>${headers.map(header => `<th scope="col">${E(header)}</th>`).join('')}</tr></thead><tbody>${body}</tbody></table></div>`;
    $('pagination').hidden = pages <= 1; $('page-label').textContent = `第 ${page+1} / ${pages} 页 · 已加载 ${records.length} 条`;
    $('previous').disabled = page === 0; $('next').disabled = page === pages-1;
  }
  function renderOrders() {
    const orders = data?.orders;
    $('orders-note').textContent = `候选 CL 开仓价 ${M.number(row().scalper?.candidate_entry_price,4)} · 候选 CL 止盈价 ${M.number(row().scalper?.candidate_tp_price,4)} USDC/桶。价格、订单与冷却均为末次采样状态；新建或改价的订单不在同一帧成交。`;
    if (data?.details_available !== true || !Array.isArray(orders)) {$('orders').innerHTML = '<div class="empty">模拟订单明细暂不可用</div>'; return;}
    if (!orders.length) {$('orders').innerHTML = '<div class="empty">当前没有有效 CL 模拟挂单</div>'; return;}
    const headers = ['订单编号','批次编号','CL 方向','限价 / USDC/桶','数量 / 桶','提交时间'];
    $('orders').innerHTML = `<div class="table-wrap" tabindex="0" aria-label="CL 模拟挂单明细"><table class="records-table"><thead><tr>${headers.map(header => `<th scope="col">${E(header)}</th>`).join('')}</tr></thead><tbody>${orders.map(order => '<tr>' + [E(order.id ?? '—'),E(order.slot ?? '—'),E(({buy:'买入开仓',sell:'卖出止盈',BUY:'买入开仓',SELL:'卖出止盈'}[order.side] || '—')),M.number(order.price,4),M.number(order.qty,3),M.date(order.submitted_ts)].map((value,index) => `<td data-label="${E(headers[index])}">${value}</td>`).join('') + '</tr>').join('')}</tbody></table></div>`;
  }
  function render() {renderOverview(); renderCharts(); renderOrders(); renderDetails(); status();}
  function setState(change,push = true) {
    const previousRange = ui.range; ui = state('?' + new URLSearchParams({...ui,...change}).toString()); page = 0;
    if (push) {const query = new URLSearchParams(location.search); query.set('range',ui.range); query.set('view',ui.view); history.pushState({},'','?' + query.toString());}
    if (ui.range !== previousRange) {selectedTs = null; historyDue = 0; historyError = ''; historyRequest?.abort();}
    render(); refreshHistory();
  }
  function loadError(error,historical = false) {
    if ([401,403].includes(error.status)) return '页面访问授权失效，请从工作台重新打开此项目。';
    if (error.name === 'AbortError') return historical ? '历史曲线读取超时，稍后重试；当前账户不受影响。' : '当前账户读取超时，页面将自动重试。';
    return `${historical ? '历史曲线' : '监控接口'}暂不可用${error.status ? '（HTTP ' + error.status + '）' : ''}，稍后自动重试。`;
  }
  async function readJSON(url,signal) {
    const response = await fetch(url,{cache:'no-store',signal});
    if (!response.ok) {const error = new Error('HTTP ' + response.status); error.status = response.status; throw error;}
    return response.json();
  }
  async function refreshHistory() {
    if (!data?.summary || !hub.active() || historyRequest || Date.now() < historyDue) return;
    clearTimeout(historyTimer);
    const range = ui.range, generation = data.reset?.generation, through = data.summary.ts, expectedNames = names();
    const request = historyRequest = new AbortController(), timeout = setTimeout(() => request.abort(),8000);
    let succeeded = false; historyError = ''; renderCharts();
    try {
      const next = await readJSON('/api/cl-bz-history?range=' + encodeURIComponent(range) + '&through=' + encodeURIComponent(through),request.signal);
      if (request !== historyRequest || !hub.active() || range !== ui.range || generation !== data?.reset?.generation) return;
      if (!acceptableHistory(next,range,generation,through,expectedNames)) throw new Error('Unexpected history payload');
      chartData = next.history; succeeded = true;
    } catch (error) {
      if (hub.active() && range === ui.range && generation === data?.reset?.generation) historyError = loadError(error,true);
    } finally {
      clearTimeout(timeout);
      if (request === historyRequest) {
        historyRequest = null;
        const changed = range !== ui.range || generation !== data?.reset?.generation;
        historyDue = changed ? 0 : Date.now() + (succeeded ? 60000 : 10000);
        if (hub.active()) {renderCharts(); historyTimer = setTimeout(refreshHistory,Math.max(0,historyDue-Date.now()));}
      }
    }
  }
  async function refresh() {
    clearTimeout(timer); snapshotRequest?.abort();
    if (!hub.active()) return;
    const request = snapshotRequest = new AbortController(), timeout = setTimeout(() => request.abort(),8000);
    $('refresh').disabled = true;
    try {
      const next = await readJSON('/api/cl-bz-snapshot',request.signal);
      if (request !== snapshotRequest || !hub.active()) return;
      if (next.kind !== 'cl_bz_scalper' || (next.summary && (next.summary.mode !== 'cl_bz_scalper' || !Array.isArray(next.summary.scenarios) || !valid(next.summary.ts)))) throw new Error('Unexpected snapshot payload');
      if (snapshotGeneration !== next.reset?.generation || !next.summary) {chartData = null; selectedTs = null; page = 0; historyDue = 0; historyError = ''; historyRequest?.abort();}
      data = next; snapshotGeneration = next.reset?.generation; received = Date.now(); disconnected = false; connectionError = '';
      render(); refreshHistory();
    } catch (error) {
      if (request !== snapshotRequest || !hub.active()) return;
      disconnected = true; connectionError = loadError(error);
      if (data) status();
      else {$('notice').hidden = false; $('notice').textContent = connectionError; $('status').textContent = '连接中断'; $('status').className = 'status error';}
    } finally {
      clearTimeout(timeout);
      if (request === snapshotRequest) {snapshotRequest = null; $('refresh').disabled = false; if (hub.active()) timer = setTimeout(refresh,10000);}
    }
  }
  $('refresh').onclick = () => {historyDue = 0; refresh();};
  $('sample').oninput = event => {selectedTs = points()[Number(event.target.value)]?.ts ?? null; renderCharts();};
  $('latest-sample').onclick = () => {selectedTs = null; renderCharts();};
  $('previous').onclick = () => {page--; renderDetails();}; $('next').onclick = () => {page++; renderDetails();};
  document.addEventListener('click',event => {
    const range = event.target.closest('[data-range]'), view = event.target.closest('[data-view]');
    if (range) setState({range:range.dataset.range});
    if (view) setState({view:view.dataset.view});
  });
  document.querySelector('.tabs').addEventListener('keydown',event => {
    const tabs = Array.from(document.querySelectorAll('.tabs [data-view]')), at = tabs.indexOf(event.target);
    if (at<0 || !['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length-1 : (at+(event.key === 'ArrowRight' ? 1 : -1)+tabs.length)%tabs.length;
    setState({view:tabs[next].dataset.view}); tabs[next].focus();
  });
  $('reset').onclick = () => {
    if ($('reset').disabled) return;
    resetIdentity = {generation:data.reset.generation,token:data.reset_token}; $('reset-dialog').showModal();
  };
  $('reset-cancel').onclick = () => {$('reset-dialog').close(); resetIdentity = null;};
  $('reset-confirm').onclick = async () => {
    if (resetSending || !resetIdentity) return;
    const identity = resetIdentity; resetIdentity = null; $('reset-dialog').close();
    if (identity.generation !== data?.reset?.generation || disconnected) {resetError = '账户状态已变化，请刷新后重新确认重置本组。'; resetStatus(); return;}
    resetSending = true; resetError = ''; resetStatus();
    const request = new AbortController(), timeout = setTimeout(() => request.abort(),8000);
    try {
      const response = await fetch('/api/cl-bz-reset',{method:'POST',headers:{'Content-Type':'application/json','X-Reset-Token':identity.token},body:JSON.stringify({generation:identity.generation}),signal:request.signal});
      if (!response.ok) throw new Error('Reset rejected');
      const result = await response.json();
      if (result.reset) data.reset = result.reset;
    } catch {resetError = '本组重置请求结果尚未确认。请刷新查看状态；不会自动重复提交。';}
    finally {clearTimeout(timeout); resetSending = false; resetStatus(); refresh();}
  };
  $('export').onclick = () => {
    const values = [['批次','每腿桶数','CL入场价','BZ入场价','CL止盈价','CL退出价','BZ退出价','开仓北京时间','平仓北京时间','CL净收益USDC','BZ净收益USDC','组合净收益USDC','原因'],...(data?.trades || []).map(record => [record.id,record.qty,record.entry_cl,record.entry_bz,record.tp_price,record.exit_cl,record.exit_bz,M.date(record.opened,false,true),M.date(record.closed,false,true),record.cl_pnl,record.bz_pnl,record.net_pnl,reason(record.reason)])];
    const url = URL.createObjectURL(new Blob([M.csv(values)],{type:'text/csv;charset=utf-8'})), link = document.createElement('a');
    link.href = url; link.download = `cl-bz-trades-${Date.now()}.csv`; link.click(); setTimeout(() => URL.revokeObjectURL(url),1000);
  };
  root.addEventListener('popstate',() => {setState(state(location.search),false);});
  let resize;
  new ResizeObserver(() => {clearTimeout(resize); resize = setTimeout(renderCharts,80);}).observe($('pnl-chart'));
  hub.subscribe(active => {if (active) {historyDue = 0; refresh();} else {clearTimeout(timer); clearTimeout(historyTimer); snapshotRequest?.abort(); historyRequest?.abort();}});
  setInterval(() => {if (hub.active()) status();},1000);
  render(); refresh();
})(typeof window !== 'undefined' ? window : globalThis);
