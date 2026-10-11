'use strict';
const $ = id => document.getElementById(id);
let token = '', lastStatus = null, gpsWatch = null, historyBusy = false, lastMessageAt = 0;
const editionLabels = {full_access:'フルアクセス版', corporate:'法人利用版', individual:'個人利用版'};
const editionCapabilities = ['can_operate','can_manage_network','can_export','can_view_gps','can_maintain'];
let edition = null;
function allowed(capability) { return edition?.[capability] === true; }
function acceptEdition(value) {
  // Only a missing field is compatible with the already-running legacy service.
  // Once a server supplied an edition, a later missing field cannot elevate it.
  if (value === undefined) {
    if (edition !== null) return;
    edition = {id:'legacy', label:'既存版 · フルアクセス互換'};
    for (const name of editionCapabilities) edition[name] = true;
  } else {
    const valid = value && typeof value === 'object' && !Array.isArray(value) &&
      Object.hasOwn(editionLabels,value.id) && editionCapabilities.every(name=>typeof value[name]==='boolean');
    edition = {id:valid?value.id:'unknown', label:valid?editionLabels[value.id]:'配布種別を確認できません'};
    for (const name of editionCapabilities) edition[name] = Boolean(valid && value[name] &&
      (!['can_maintain','can_manage_network'].includes(name) || value.id==='full_access'));
  }
  editionControls();
}
function editionControls() {
  text('edition-label', edition?.label || '配布種別を確認中');
  const limited = edition && ['corporate','individual'].includes(edition.id);
  text('edition-note', limited
    ? `${edition.label}では監視・走行記録・GPS・履歴出力を利用できます。取得周期の変更・BLE検索・バックアップ・接続管理は管理保守機能のため利用できません。BMS通信は読み取り専用です。`
    : allowed('can_maintain') ? '監視・記録と管理保守を利用できます。BMS通信は読み取り専用です。'
    : '配布種別の確認が必要です。操作を停止しています。BMS通信は読み取り専用です。');
  for (const id of ['monitor-start','monitor-stop','distance','ambient']) $(id).disabled=!allowed('can_operate');
  $('ride-start').disabled=!allowed('can_operate') || Boolean(lastStatus?.active_trip);
  $('ride-stop').disabled=!allowed('can_operate') || !lastStatus?.active_trip;
  for (const id of ['interval','interval-save','scan','backup']) {
    $(id).disabled=!allowed('can_maintain');
    $(id).title=allowed('can_maintain')?'':'この配布版では管理保守機能を利用できません。';
  }
  $('gps-start').hidden=!allowed('can_view_gps') || !lastStatus?.gps_enabled;
  $('gps-start').disabled=!allowed('can_operate') || !allowed('can_view_gps');
  if ((!allowed('can_operate') || !allowed('can_view_gps')) && gpsWatch!==null) {
    navigator.geolocation.clearWatch(gpsWatch);gpsWatch=null;text('gps-state','この配布版では位置情報の記録を利用できません。');
  }
  const link=$('network-management');
  link.hidden=!allowed('can_manage_network');link.setAttribute('aria-disabled',allowed('can_manage_network')?'false':'true');
  if(allowed('can_manage_network')) { link.setAttribute('href','http://127.0.0.1:8767/');link.removeAttribute('tabindex'); }
  else { link.removeAttribute('href');link.setAttribute('tabindex','-1'); }
  document.querySelectorAll('[data-trip-distance]').forEach(control=>control.disabled=!allowed('can_operate'));
  $('export-dataset').disabled=!allowed('can_export');
  exportLinks();
}
function requireCapability(capability) {
  if(!allowed(capability)) throw new Error('この配布版ではこの操作を利用できません。配布種別と利用範囲を確認してください。');
}
function exportLinks() {
  const canExport=allowed('can_export') && ($('export-dataset').value!=='gps' || allowed('can_view_gps'));
  for(const format of ['csv','json']) {
    const link=$('export-'+format);link.setAttribute('aria-disabled',canExport?'false':'true');
    if(canExport) { link.setAttribute('href','/api/export/'+format+'?dataset='+$('export-dataset').value);link.removeAttribute('tabindex'); }
    else { link.removeAttribute('href');link.setAttribute('tabindex','-1'); }
  }
}
const fmt = (value, n=1) => value === null || value === undefined || !Number.isFinite(Number(value)) ? '—' : Number(value).toFixed(n);
const date = value => value ? new Date(value).toLocaleString('ja-JP', {timeZone:'Asia/Tokyo', hour12:false}) : '—';
const duration = seconds => `${Math.floor(seconds/3600)}:${String(Math.floor(seconds/60)%60).padStart(2,'0')}:${String(Math.floor(seconds)%60).padStart(2,'0')}`;
function toast(message) { $('toast').textContent = message; $('toast').hidden = false; setTimeout(() => $('toast').hidden=true, 6000); }
async function api(path, body) {
  if(body!==undefined) requireCapability(['/api/settings/interval','/api/scan','/api/backup'].includes(path)?'can_maintain':'can_operate');
  if(/\/gps(?:\?|$)/.test(path)) requireCapability('can_view_gps');
  const response = await fetch(path, {method:body === undefined ? 'GET':'POST', headers:body === undefined ? {}:{'Content-Type':'application/json','X-GTBMS-Token':token}, body:body === undefined ? undefined:JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '入力値を確認してください');
  return result;
}
function text(id, value) { $(id).textContent = value; }
function update(s) {
  acceptEdition(s.edition);
  lastStatus=s;
  const m=s.measurement, p=s.prediction;
  text('mode', s.mode==='mock' ? '模擬BMS / 実測ではありません':'BLE / 実機検証が必要');
  $('mode').className='badge'+(s.mode==='mock'?' mock':'');
  const states={connected:'接続中', connecting:'接続しています', reconnecting:'再接続待ち', stopped:'監視停止'};
  text('link', states[s.state] || s.state); $('link').className='status'+(s.fresh?' connected':'');
  text('last-update', date(s.last_success_at));
  text('age', s.age_seconds !== null ? `${fmt(s.age_seconds,0)}秒前${s.retry_in_seconds ? ' · 再試行まで '+fmt(s.retry_in_seconds,0)+'秒':''}`:'有効な測定値なし');
  const notes=[];
  if(s.mode==='mock') notes.push('模擬データを表示しています。実際のGTバッテリーの測定値ではありません。');
  if(!s.fresh) notes.push('現在の測定値は取得できていません。以前の値は現在値として表示しません。');
  if(s.mode==='ble' && s.current_direction==='unconfirmed') notes.push('電流の符号方向は未確認です。生の電流だけを表示し、電力量・航続距離の学習には使いません。');
  if(m?.quality_flags?.length) notes.push('異常または不整合を検出：'+m.quality_flags.join(', '));
  text('notice', notes.join(' ') || '読み取り専用で監視中。＋は放電、−は充電です。位置情報・測定値の外部送信は明示設定が必要です。');
  $('notice').className='notice'+(!s.fresh || s.mode==='mock' || m?.quality_flags?.length?' warning':'');
  text('soc', fmt(m?.soc_pct,0)); $('gauge').style.strokeDasharray=`${Math.max(0,Math.min(100,m?.soc_pct||0))} 100`;
  text('remaining-ah',fmt(m?.remaining_ah,2));
  text('voltage',fmt(m?.voltage_v,2));
  text('current',fmt(m?.current_a ?? m?.raw_current_a,2));
  text('direction', m?.current_a===null ? 'BMS生値・符号 未確認':'＋放電 / −充電');
  text('power',fmt(m?.power_w,0));
  text('temperature', m?.temperatures_c?.length ? fmt(Math.max(...m.temperatures_c),1):'—');
  text('charge-state',{discharging:'放電中',charging:'充電中',idle:'待機',unconfirmed:'符号 未確認'}[m?.state] || '測定値なし');
  text('cell-delta',fmt(m?.cell_delta_v===undefined?null:m?.cell_delta_v*1000,0));
  text('cell-min',fmt(m?.cell_min_v,3)); text('cell-max',fmt(m?.cell_max_v,3));
  text('balance', m ? `バランス ${m.balancing_mask ? '作動中':'停止中'}`:'バランス —');
  $('cells').replaceChildren();
  for(let i=0;i<(m?.cell_voltages_v.length || 16);i++) {
    const cell=document.createElement('div'); cell.className='cell'+(m && (m.balancing_mask & 2**i)?' balancing':'');
    const label=document.createElement('small');label.textContent=String(i+1).padStart(2,'0');
    const value=document.createElement('b');value.textContent=fmt(m?.cell_voltages_v[i],3);cell.append(label,value);$('cells').append(cell);
  }
  text('remaining-range',fmt(p.remaining_range_km,1));text('safe-range',fmt(p.safe_remaining_range_km,1)+' km');text('full-range',fmt(p.full_range_km,1)+' km');
  text('range-basis',`${p.capacity_basis} · ${fmt(p.usable_energy_wh,0)}Wh · SOC比例推定 · ${p.consumption_basis || "放電量を基準に推定"}`); text('confidence',p.reliability);
  text('mean-consumption',fmt(p.mean_wh_per_km,1)+' Wh/km');text('latest-consumption',fmt(p.latest_wh_per_km,1)+' Wh/km');text('prediction-error',fmt(p.mae_wh_per_km,1)+' Wh/km');text('training-count',p.sample_trips+'件');
  $('conditions').replaceChildren();
  for(const c of p.conditions){const el=document.createElement('div');el.className='condition';const l=document.createElement('span');l.textContent=c.condition+` (${c.trips}件)`;const v=document.createElement('b');v.textContent=fmt(c.mean_wh_per_km,1)+' Wh/km';el.append(l,v);$('conditions').append(el);}
  if(!p.conditions.length) text('conditions','有効な走行データがまだありません。');
  const trip=s.active_trip; text('ride-label',trip?'走行記録中':'未開始');text('ride-duration',trip?duration((Date.now()-new Date(trip.started_at))/1000):'—');text('ride-start-time',date(trip?.started_at));
  editionControls();
  if(!trip && gpsWatch!==null){navigator.geolocation.clearWatch(gpsWatch);gpsWatch=null;text('gps-state','');}
  text('alarms', m ? (m.alarms.length ? m.alarms.join(' / '):'BMS保護フラグなし') : '最新測定値なし'); $('alarms').className=m?.alarms.length?'bad':'';
  text('mos',m?`MOS 充電 ${m.charge_mos?'ON':'OFF'} / 放電 ${m.discharge_mos?'ON':'OFF'}`:'充放電MOS —');text('cycles',m?`サイクル ${m.cycles}回`:'サイクル —');
  text('system',`CPU ${fmt(s.system.cpu_pct,1)}% · メモリ ${fmt(s.system.memory_mb,1)}MB · 監視プロセス ${s.system.worker_alive?'稼働':'停止'}\n同期 ${s.sync.state} · 最終バックアップ ${date(s.system.maintenance.last_backup_at)}`);
}
const colors=['#b9f36b','#65d6d3','#f6be70','#b3a0ec','#ed91b2','#87b5fa'];
function chart(id, rows, fields, gapSeconds=30) {
  const target=$(id);target.replaceChildren();
  const values=rows.flatMap(row=>fields.map(field=>field.get(row))).filter(v=>v!==null && v!==undefined && Number.isFinite(v));
  if(!rows.length || !values.length){const empty=document.createElement('div');empty.className='empty';empty.textContent='有効な記録がありません';target.append(empty);return;}
  const ns='http://www.w3.org/2000/svg', svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox','0 0 600 180');svg.setAttribute('role','img');svg.setAttribute('aria-label',id.replace('chart-','')+'履歴グラフ');
  const add=(tag,attrs,content)=>{const el=document.createElementNS(ns,tag);for(const [k,v] of Object.entries(attrs))el.setAttribute(k,v);if(content!==undefined)el.textContent=content;svg.append(el);return el;};
  let lo=Math.min(...values),hi=Math.max(...values);const pad=Math.max((hi-lo)*.12,Math.abs(hi)*.005,.01);lo-=pad;hi+=pad;
  const ts=rows.map(r=>new Date(r.received_at || r.started_at || r.at).getTime()),t0=Math.min(...ts),t1=Math.max(...ts);
  const x=t=>46+((t-t0)/(t1-t0 || 1))*535,y=v=>145-(v-lo)/(hi-lo)*120;
  for(let i=0;i<4;i++){const yy=25+i*40;add('line',{x1:46,y1:yy,x2:580,y2:yy,stroke:'#29343e','stroke-width':1});add('text',{x:38,y:yy+4,'text-anchor':'end',fill:'#91a3af','font-size':10},fmt(hi-(hi-lo)*i/3,Math.abs(hi)<10?2:0));}
  fields.forEach((field,i)=>{let path='',previous=null,epoch=null;rows.forEach((r,k)=>{const value=field.get(r);if(value===null || value===undefined || !Number.isFinite(value)){previous=null;return;}const connected=previous!==null && (ts[k]-previous)<=gapSeconds*1000 && (!r.connection_id || epoch===r.connection_id);path+=(connected?'L':'M')+x(ts[k]).toFixed(2)+','+y(value).toFixed(2);previous=ts[k];epoch=r.connection_id;if(rows.length<3)add('circle',{cx:x(ts[k]),cy:y(value),r:3,fill:colors[i%colors.length]});});add('path',{d:path,fill:'none',stroke:colors[i%colors.length],'stroke-width':1.8,'stroke-linejoin':'round'});if(fields.length<=3)add('text',{x:46+i*160,y:12,fill:colors[i%colors.length],'font-size':10},field.name);});
  add('text',{x:46,y:170,fill:'#91a3af','font-size':10},date(rows[0].received_at||rows[0].started_at||rows[0].at));add('text',{x:580,y:170,fill:'#91a3af','font-size':10,'text-anchor':'end'},date(rows.at(-1).received_at||rows.at(-1).started_at||rows.at(-1).at));target.append(svg);
}
async function loadHistory() {
  if(historyBusy)return;historyBusy=true;
  try{
    const start=new Date(Date.now()-Number($('history-window').value)*3600000).toISOString();
    const [rows,trips,predictions,events]=await Promise.all([api('/api/history?limit=5000&start='+encodeURIComponent(start)),api('/api/trips'),api('/api/predictions'),api('/api/events')]);
    text('history-count',`${rows.length}件を表示（最大5,000件、期間内の最新記録）。全記録はCSV/JSONから出力できます。`);
    const field=(name,key)=>({name,get:r=>r[key]});const gap=Math.max(10,(lastStatus?.poll_seconds||2)*3);
    chart('chart-soc',rows,[field('SOC','soc_pct')],gap);chart('chart-voltage',rows,[field('総電圧','voltage_v')],gap);chart('chart-current',rows,[field('電流','current_a')],gap);chart('chart-power',rows,[field('電力','power_w')],gap);
    const count=Math.max(0,...rows.map(r=>r.temperatures_c.length));chart('chart-temperature',rows,Array.from({length:count},(_,i)=>({name:`NTC ${i+1}`,get:r=>r.temperatures_c[i]})),gap);
    const cells=Math.max(0,...rows.map(r=>r.cell_voltages_v.length));chart('chart-cells',rows,Array.from({length:cells},(_,i)=>({name:`セル ${i+1}`,get:r=>r.cell_voltages_v[i]})),gap);
    const completed=trips.filter(t=>t.ended_at).reverse();chart('chart-distance',completed,[field('距離','distance_km')],Infinity);chart('chart-efficiency',completed,[field('正味消費','wh_per_km')],Infinity);chart('chart-range',predictions.reverse(),[field('通常','full_range_km'),field('安全余裕','safe_full_range_km')],Infinity);
    $('trip-rows').replaceChildren();
    for(const t of trips){const row=document.createElement('tr');const values=[date(t.started_at),fmt(t.distance_km,2),t.energy?duration(t.energy.duration_seconds):'記録中',fmt(t.average_speed_kmh,1)+' / '+fmt(t.max_speed_kmh,1),fmt(t.start_soc_pct,0)+' → '+fmt(t.end_soc_pct,0),fmt(t.energy?.net_wh,1),fmt(t.wh_per_km,1),t.energy?fmt(t.energy.coverage*100,0)+'%'+(t.energy.coverage<.9?' · 信頼性低':'')+(t.distance_quality==='partial_gps'?' · GPS部分記録':''):'—'];for(const value of values){const td=document.createElement('td');td.textContent=value;row.append(td);}const action=document.createElement('td');if(t.ended_at){const input=document.createElement('input');input.type='number';input.min='0';input.max='10000';input.step='.1';input.value=t.distance_km??'';input.style.width='85px';input.setAttribute('aria-label','走行距離 km');input.setAttribute('data-trip-distance','');input.disabled=!allowed('can_operate');const button=document.createElement('button');button.setAttribute('data-trip-distance','');button.disabled=!allowed('can_operate');button.textContent='保存';button.onclick=()=>run(async()=>{if(input.value==='')throw new Error('距離を入力してください');await api(`/api/trips/${t.id}/distance`,{distance_km:Number(input.value)});await loadHistory();toast('距離を保存しました');});action.append(input,button);}row.append(action);$('trip-rows').append(row);}
    if(!trips.length){const row=document.createElement('tr'),td=document.createElement('td');td.colSpan=9;td.textContent='まだ走行履歴はありません。';row.append(td);$('trip-rows').append(row);}
    const renderEvents=(id,list,toText)=>{$(id).replaceChildren();for(const event of list){const el=document.createElement('div');el.className='event';const time=document.createElement('time');time.textContent=date(event.at||event.started_at);const detail=document.createElement('span');detail.textContent=toText(event);el.append(time,detail);$(id).append(el);}if(!list.length)text(id,'記録なし');};
    renderEvents('event-list',events.events,e=>e.kind+(e.detail?' · '+e.detail:''));renderEvents('gap-list',events.gaps,e=>`→ ${date(e.ended_at)} · ${e.reason}`);
  }catch(error){toast(error.message);}finally{historyBusy=false;}
}
async function run(work){try{await work();}catch(error){toast(error.message);}}
function bind(id,work){$(id).onclick=()=>{if(!$(id).disabled)return run(work);};}
function numberOrNull(id){return $(id).value===''?null:Number($(id).value);}
document.querySelectorAll('[data-panel]').forEach(button=>button.onclick=()=>{document.querySelectorAll('.panel').forEach(p=>p.hidden=p.id!==button.dataset.panel);document.querySelectorAll('[data-panel]').forEach(b=>b.classList.toggle('selected',b===button));if(button.dataset.panel!=='overview')loadHistory();});
bind('monitor-start',async()=>{await api('/api/monitor/start',{});toast('監視を開始しました');});bind('monitor-stop',async()=>{await api('/api/monitor/stop',{});update(await api('/api/status'));toast('監視を停止しました');});
bind('interval-save',async()=>{await api('/api/settings/interval',{seconds:Number($('interval').value)});toast('取得・記録周期を保存しました');});
bind('ride-start',async()=>{await api('/api/trips/start',{ambient_c:numberOrNull('ambient')});toast('走行の記録を開始しました');});
bind('ride-stop',async()=>{await api('/api/trips/stop',{distance_km:numberOrNull('distance')});$('distance').value='';await loadHistory();toast('走行を保存しました');});
bind('backup',async()=>{const result=await api('/api/backup',{});toast('バックアップ保存・整合性確認済み：'+result.filename);});
bind('diagnose',async()=>text('diagnostics',JSON.stringify(await api('/api/diagnostics'),null,2)));
bind('scan',async()=>{text('scan-result','検索中…');text('scan-result',JSON.stringify(await api('/api/scan',{}),null,2));});
$('history-window').onchange=loadHistory;
$('export-dataset').onchange=exportLinks;
for(const id of ['export-csv','export-json','network-management']) $(id).onclick=event=>{if($(id).getAttribute('aria-disabled')==='true')event.preventDefault();};
bind('gps-start',async()=>{
  requireCapability('can_operate'); requireCapability('can_view_gps');
  if(!lastStatus?.active_trip)throw new Error('先に走行セッションを開始してください');
  if(!navigator.geolocation)throw new Error('このブラウザーで位置情報を利用できません');
  if(gpsWatch!==null){navigator.geolocation.clearWatch(gpsWatch);gpsWatch=null;text('gps-state','位置記録を停止');return;}
  gpsWatch=navigator.geolocation.watchPosition(position=>run(async()=>{const trip=lastStatus?.active_trip;if(!trip)return;const c=position.coords;await api(`/api/trips/${trip.id}/gps`,{measured_at:new Date(position.timestamp).toISOString(),latitude:c.latitude,longitude:c.longitude,altitude_m:c.altitude,speed_kmh:c.speed===null?null:c.speed*3.6,accuracy_m:c.accuracy});text('gps-state',`位置記録中 · 精度 ${fmt(c.accuracy,0)}m`);}),error=>{text('gps-state',error.message);if(gpsWatch!==null)navigator.geolocation.clearWatch(gpsWatch);gpsWatch=null;},{enableHighAccuracy:true,maximumAge:0,timeout:15000});
});
function invalidate(){if(lastStatus){update({...lastStatus,fresh:false,measurement:null,state:'reconnecting',prediction:{...lastStatus.prediction,remaining_range_km:null,safe_remaining_range_km:null}});text('notice','ダッシュボードと監視プログラムの接続が切れました。現在値は表示していません。');}}
function connect(){const ws=new WebSocket(`ws://${location.host}/ws`);ws.onopen=()=>run(async()=>{const session=await api('/api/session');token=session.token;acceptEdition(session.edition);});ws.onmessage=e=>{lastMessageAt=Date.now();update(JSON.parse(e.data));};ws.onclose=()=>{invalidate();setTimeout(connect,3000);};ws.onerror=()=>ws.close();}
editionControls();
run(async()=>{const session=await api('/api/session');token=session.token;acceptEdition(session.edition);update(await api('/api/status'));connect();await loadHistory();});
setInterval(()=>{if(!$('analysis').hidden || !$('operations').hidden)loadHistory();},15000);
setInterval(()=>{if(lastMessageAt && Date.now()-lastMessageAt>5000)invalidate();},1000);
