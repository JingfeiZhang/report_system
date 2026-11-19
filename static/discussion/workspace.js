'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const names = {facts:'事实研究',media:'媒体观点',sentiment:'舆情分析',review:'交叉核验',host:'主持协调'};
const statuses = {analyzing_images:'识别图片中',collecting_images:'补取图片中',ready:'待开始',planning:'拟定议程中',running:'研讨进行中',waiting:'等待你的决策',reporting:'生成报告中',error:'需要处理',interrupted:'任务已中断'};
const taskStatuses = {cancelled:'已终止',pending:'待处理',running:'执行中',complete:'已完成',failed:'未完成',interrupted:'已中断'};
const messageStatuses = {pending:'下一轮处理',assigned:'本轮已分配',answered:'已有回复',unresolved:'仍待解决',recorded:'仅记录'};
const platformNames = {dy:'抖音',xhs:'小红书',wb:'微博'};
const collectionStatuses = {running:'采集中',complete:'已采集',partial:'部分完成',empty:'无可用结果',needs_login:'需要登录 / 验证',rate_limited:'平台限流，已暂停',timeout:'已超时',failed:'采集失败',unavailable:'依赖未就绪',busy:'平台使用中',login_open:'等待登录',session_saved:'会话已保存',interrupted:'已中断'};
Object.assign(collectionStatuses,{model_stopped:'协调模型结束采集',assessment_failed:'需求评估失败，待重试',pending:'等待轮转',reserved_for_supplement:'已预留辩论补证时间',budget_exhausted:'历史：时间预算用完',exhausted:'当前查询结束',stalled:'查询停滞',satisfied:'材料覆盖，待核验',user_stopped:'已手动停止',cursor_invalid:'断点失效',content_unavailable:'此帖或评论不可访问'});
let state = null, stream = null, selectedRound = null, busyAction = false, refreshTimer = null, refreshing = false, refreshAgain = false;
let evidenceRequest = 0, evidenceCache = new Map();
let citationNumbers=new Map(), citationLoads=new Map(), citationFailures=new Set(), citationQueue=[], citationActive=0, citationObserver=null;
let citationMeeting=null, sourceOpenRequest=0;
let collectionPage = 0, collectionTotal = 0, lastEvidenceSignature = '';
let lastSignature = '', toastTimer, evidencePage = 0, guidedClaim = null;
let sendingMessage = false;
const phaseNames={proposal:'独立答案',peer_review:'相互评议',revision:'修订答案',verification:'证据核验'};
const evidenceStatuses={unverified:'尚未核验',supported:'材料支持',partial:'部分支持',insufficient:'证据不足',conflicting:'证据冲突',refuted:'已被反驳'};

// Flask can still serve a cached template until its next restart.
document.querySelectorAll('.reading-tools [data-jump="latest"], .reading-tools [data-jump="challenges"]').forEach(button=>button.remove());
document.querySelector('.reading-tools [data-jump="conversation"]').textContent='对话记录';
document.querySelector('.reading-tools [data-jump="summary"]').textContent='跳到本轮总结 ↓';

// Keep already-running Flask instances compatible with their cached template.
if (!$('message-form')) {
  const composer=document.querySelector('.composer'), form=document.createElement('form'), toolbar=document.createElement('div');
  form.id='message-form';form.className='chat-input';toolbar.className='chat-input-tools';
  const hint=document.createElement('span');hint.className='keyboard-hint';hint.textContent='Enter 发送 · Shift+Enter 换行';
  toolbar.append($('target'),hint,$('send-message'));form.append($('message'),toolbar);
  composer.prepend($('demo-tip'));$('demo-tip').after(form);
  const help=$('message-help');help.className='composer-hint';form.after(help);
  $('toggle-guidance')?.remove();$('guidance-fields')?.remove();composer.classList.remove('writing-guidance');
  $('target').replaceChildren(new Option('发给全体','all'),...Object.entries(names).map(([value,label])=>new Option(label,value)));
}
const messageInput=$('message'), sendButton=$('send-message');
messageInput.rows=1;messageInput.placeholder='发送消息，提出质疑或下一轮方向…';
messageInput.setAttribute('aria-label','消息');messageInput.setAttribute('aria-describedby','message-help');
sendButton.type='submit';sendButton.className='send-button';sendButton.setAttribute('aria-label','发送消息');sendButton.title='发送消息';
sendButton.innerHTML='<svg viewBox="0 0 24 24" width="21" height="21" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 19V5m-6 6 6-6 6 6"/></svg>';
function updateComposer(){
  if(messageInput.getClientRects().length){
    messageInput.style.height='auto';
    messageInput.style.height=Math.min(messageInput.scrollHeight,parseFloat(getComputedStyle(messageInput).maxHeight)||160)+'px';
  }
  sendButton.disabled=sendingMessage||!state||!messageInput.value.trim();
  $('message-form').setAttribute('aria-busy',String(sendingMessage));
}

async function api(path, options = {}) {
  const response = await fetch(path, {...options, headers: options.body instanceof FormData ? options.headers : {'Content-Type':'application/json',...options.headers}});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '操作未完成，请重试');
  return result;
}
function toast(message) { $('toast').textContent = message; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 6000); }
async function action(fn) { if (busyAction) return; busyAction = true; try { await fn(); } catch (e) { toast(e.message); } finally { busyAction = false; } }
function isBusy() { return ['planning','running','reporting','collecting_images','analyzing_images'].includes(state?.status); }
function reportProgress(value) { return ({template_sliced:'正在整理报告章节',chapters_compiled:'正在整理完整报告',document_layout:'正在设计报告结构',storage_ready:'报告章节已准备',word_plan_ready:'报告篇幅已规划',chapter_chunk:'正在撰写报告',chapter_status:'正在处理报告章节'})[value] || value; }
function meetingPath(suffix='') { return `/api/discussions/${state.id}${suffix}`; }
function saveState(s) { state = s; for(const e of s.evidence||[])evidenceCache.set(e.id,e); render(); }
function mobile(panel) { if(panel==='minutes'){$('insights-dialog').showModal();return;} document.querySelectorAll('.workspace-grid>.panel').forEach(e=>e.classList.toggle('mobile-active',e.id===panel)); document.querySelectorAll('[data-panel]').forEach(e=>e.classList.toggle('active',e.dataset.panel===panel)); if(panel==='forum')updateComposer(); }
async function loadHistory() {
  const {items} = await api('/api/discussions');
  $('welcome-history').innerHTML = items.length ? items.slice(0,9).map(s=>`<button class="history-card" data-meeting="${esc(s.id)}"><b>${esc(s.title)}</b><small>${s.mode==='demo'?'演示案例':'真实分析'} · ${esc(statuses[s.status])}</small></button>`).join('') : '<p class="fine">还没有研讨记录。可以先体验演示案例。</p>';
  $('meeting-list').innerHTML = items.map(s=>`<button data-meeting="${esc(s.id)}">${esc(s.title)}</button>`).join('');
}
async function openMeeting(id) {
  if (stream) stream.close();
  state = await api(`/api/discussions/${encodeURIComponent(id)}`);
  selectedRound = null; lastSignature = ''; evidencePage = 0; evidenceCache.clear(); guidedClaim = null;
  historyReplace(id);
  $('welcome').hidden = true; $('workspace').hidden = false;
  let saved={};try{saved=JSON.parse(sessionStorage.getItem('discussion-view-'+id)||'{}');}catch(_){}
  selectedRound=saved.round??null;
  render(); mobile('forum');
  if(Number.isFinite(saved.scroll))$('timeline').scrollTop=saved.scroll;
  else focusConversationStart();
  stream = new EventSource(meetingPath('/events'));
  stream.onopen = () => $('connection').textContent = '会议已同步';
  stream.onerror = () => $('connection').textContent = '连接恢复中';
  stream.onmessage = () => {clearTimeout(refreshTimer);refreshTimer=setTimeout(refresh,150);};
  loadHistory().catch(()=>{});
}
function historyReplace(id) { window.history.replaceState(null,'',`/discussion?meeting=${encodeURIComponent(id)}`); }
function focusConversationStart(){
  const timeline=$('timeline'), first=timeline.querySelector('.user-card, .debate-issue .answer-message');
  if(first)timeline.scrollTop+=first.getBoundingClientRect().top-timeline.getBoundingClientRect().top-12;
}
async function refresh() {
  if (!state) return;
  if (refreshing) { refreshAgain = true; return; }
  refreshing = true;
  const id = state.id;
  try { const next=await api(`/api/discussions/${id}`); if(state?.id===id)saveState(next); } catch(e){toast(e.message);} finally { refreshing=false;if(refreshAgain){refreshAgain=false;refresh();} }
}
function citation(id) {
  const e=evidenceCache.get(id)||state.evidence.find(e=>e.id===id);
  const info=sourceInfo(e), number=citationNumber(id);
  const title=e?`${info.site} · ${info.title}\n${info.kind} · 发布：${info.date}${e.locator?'\n'+e.locator:''}`:citationFailures.has(id)?'来源读取失败，点击重试':'正在读取这条引用的出处';
  return `<button type="button" class="citation" data-evidence="${esc(id)}" title="${esc(title)}" aria-label="引用 ${number}：${esc(title)}" aria-haspopup="dialog"><span class="citation-number">${number}</span><span class="citation-site">${esc(e?info.site:'来源')}</span><span class="citation-title">${esc(e?info.label:citationFailures.has(id)?'读取失败':'加载出处…')}</span><span class="citation-kind">${esc(e?info.shortKind:'')}</span></button>`;
}

function sourceInfo(e){
  if(!e)return {};
  let host='';try{const u=new URL(e.url);if(['http:','https:'].includes(u.protocol))host=u.hostname.replace(/^www\./,'');}catch(_){}
  const domains={'weibo.cn':'微博','weibo.com':'微博','xiaohongshu.com':'小红书','douyin.com':'抖音','toutiao.com':'今日头条','163.com':'网易','qq.com':'腾讯','sohu.com':'搜狐','zhihu.com':'知乎','bilibili.com':'哔哩哔哩'};
  const domainName=Object.entries(domains).find(([d])=>host===d||host.endsWith('.'+d))?.[1];
  const site=platformNames[e.platform]||domainName||host||({upload:'上传资料',comment:'上传评论',demo:'演示资料'}[e.source_type])||'来源未标注';
  const summary=e.source_type==='web_summary'||e.content_scope==='search_summary';
  const kind=summary?'搜索摘要':({image_analysis:'图片机器提取',web_page:'网页正文片段',social_post:'帖子文字',social_comment:'评论样本',upload:'上传文件片段',comment:'CSV 评论',demo:'虚构演示资料'}[e.source_type]||'资料片段');
  const shortKind=summary?'摘要':({image_analysis:'识图',web_page:'正文',social_post:'帖子',social_comment:'评论',upload:'文档',comment:'评论',demo:'演示'}[e.source_type]||'片段');
  const title=e.title||'未提供标题';
  const label=['social_comment','comment'].includes(e.source_type)?'“'+String(e.text||title).replace(/\s+/g,' ').slice(0,70)+'”':title;
  return {site,host,title,label,kind,shortKind,date:e.date||'未提供'};
}
function citationNumber(id){
  if(!citationNumbers.has(id)){
    citationNumbers.set(id,citationNumbers.size+1);
    try{sessionStorage.setItem('discussion-citations-'+state.id,JSON.stringify([...citationNumbers.keys()]));}catch(_){}
  }
  return citationNumbers.get(id);
}
function prepareCitations(){
  if(citationMeeting!==state.id){
    citationMeeting=state.id;citationLoads=new Map();citationFailures=new Set();citationQueue=[];citationNumbers=new Map();
    try{const ids=JSON.parse(sessionStorage.getItem('discussion-citations-'+state.id)||'[]');if(Array.isArray(ids))for(const id of ids)if(/^E-[a-f0-9]{12}$/.test(id)&&!citationNumbers.has(id))citationNumbers.set(id,citationNumbers.size+1);}catch(_){}
  }
  // Include historical rounds so navigation and repeated citations keep one number.
  for(const r of state.rounds){
    const text=JSON.stringify([r.cards,r.peer_reviews,r.summary,r.tool_runs]);
    for(const id of text.match(/E-[a-f0-9]{12}/g)||[])citationNumber(id);
  }
}
function refreshCitationLabels(id){
  document.querySelectorAll('.citation[data-evidence]').forEach(button=>{
    if(button.dataset.evidence!==id)return;
    const template=document.createElement('template');template.innerHTML=citation(id);
    const next=template.content.firstElementChild;
    button.innerHTML=next.innerHTML;button.title=next.title;button.setAttribute('aria-label',next.getAttribute('aria-label'));
  });
}
function fetchCitation(id){
  const cached=evidenceCache.get(id)||state.evidence.find(e=>e.id===id);
  if(cached)return Promise.resolve(cached);
  if(citationLoads.has(id))return citationLoads.get(id);
  const mid=state.id, loads=citationLoads;
  const promise=api(`/api/discussions/${mid}/evidence/${encodeURIComponent(id)}`).then(e=>{
    if(state.id===mid&&loads===citationLoads){evidenceCache.set(id,e);citationFailures.delete(id);refreshCitationLabels(id);}
    return e;
  }).catch(error=>{if(state.id===mid&&loads===citationLoads){citationFailures.add(id);refreshCitationLabels(id);}throw error;}).finally(()=>loads.delete(id));
  loads.set(id,promise);return promise;
}
function loadVisibleCitations(){
  while(citationActive<4&&citationQueue.length){
    const {id,mid}=citationQueue.shift();
    if(mid!==state.id||citationFailures.has(id))continue;
    citationActive++;
    fetchCitation(id).catch(()=>{}).finally(()=>{citationActive--;loadVisibleCitations();});
  }
}
function observeCitations(){
  if(!citationObserver)citationObserver=new IntersectionObserver(entries=>{
    for(const entry of entries)if(entry.isIntersecting){
      citationObserver.unobserve(entry.target);
      const id=entry.target.dataset.evidence;
      if(!evidenceCache.has(id)&&!citationLoads.has(id)&&!citationFailures.has(id)&&!citationQueue.some(t=>t.id===id&&t.mid===state.id))citationQueue.push({id,mid:state.id});
    }
    loadVisibleCitations();
  },{rootMargin:'120px'});
  citationObserver.disconnect();
  document.querySelectorAll('.citation[data-evidence]').forEach(button=>citationObserver.observe(button));
}
function evidenceText(value) {
  // Escape prose first; only recognized evidence markers become our own buttons.
  return esc(value).replace(/\[\s*E-[a-f0-9]{12}(?:\s*[,，、;；]\s*E-[a-f0-9]{12})*\s*\]|E-[a-f0-9]{12}/g,
    marker=>[...new Set(marker.match(/E-[a-f0-9]{12}/g))].map(citation).join(''));
}
function citations(ids) { return [...new Set(ids||[])].map(citation).join(''); }
function claimStatus(c, cl) {
  if(cl.action==='withdraw') return '已撤回';
  if(cl.evidence_status && cl.evidence_status!=='unverified') return evidenceStatuses[cl.evidence_status]||'尚未核验';
  const r=state.rounds.find(r=>r.number===c.round);
  if((r?.cards||[]).some(x=>x.agent_id===c.agent_id && (x.version||1)>(c.version||1))) return '历史版本 · 见后续修订';
  const failed=(r?.tasks||[]).filter(t=>t.issue_id===c.issue_id && t.phase==='verification' && t.status==='failed');
  if(failed.length) return failed.some(t=>String(t.error).includes('Timeout'))?'核验超时 · 仍待确认':'核验未完成';
  return r?.status==='running'?'等待核验':'尚未取得有效核验结果';
}
function card(c) { return chatCard(c); }
function userCard(m) { return chatUserCard(m); }
function render() {
  if (!state) return;
  prepareCitations();
  const busy=isBusy();
  const last=state.rounds.at(-1);
  const round = selectedRound===null ? last : state.rounds.find(r=>r.number===selectedRound);
  const snapshot=state.snapshots.find(s=>s.round===round?.number);
  $('mode-label').textContent=state.mode==='demo'?'DEMO / 虚构演示案例':'LIVE / 真实协同分析';
  $('meeting-title').textContent=state.title;
  $('meeting-subtitle').textContent=`${state.report_type==='brief'?'事件简报':'舆情分析报告'} · ${state.time_range||'未限定时间'} · ${state.mode==='demo'?'固定虚构资料':state.web_enabled?'联网 + 会议资料':'仅会议资料'}`;
  $('meeting-status').textContent=statuses[state.status]||state.status;
  $('error-banner').hidden=!state.error;
  $('error-banner').innerHTML=state.error?`<details><summary>有一项任务需要处理 · 查看原因</summary><p>${esc(state.error)}</p>${!state.rounds.length?'<button id="retry-plan" class="text-button">重试规划</button>':''}</details>`:'';
  $('agenda-list').innerHTML=state.agenda.map(x=>`<li>${esc(x)}</li>`).join('');
  $('edit-agenda').disabled=busy;
  $('edit-sources').disabled=busy||state.mode==='demo';
  $('source-settings').innerHTML=`<p class="fine">${state.mode==='demo'?'演示模式不连接平台':(state.crawl_platforms||[]).length?(state.crawl_platforms.map(p=>platformNames[p]).join(' / ')+' · 下一轮按任务采集'):'未启用平台采集，可在设置中选择'}<br>当前 ${state.comment_count} 条评论样本</p>`;
  if(state.mode==='live'){
    const images=state.image_collection;
    $('source-settings').insertAdjacentHTML('beforeend',`<button type="button" class="secondary small" id="collect-images" ${busy?'disabled':''}>补取已有帖子配图</button>${state.status==='collecting_images'?'<button type="button" class="text-button" id="stop-images">停止补取</button>':''}<p class="fine" aria-live="polite">${esc(images?.message||'配图下载不调用模型；相关配图将在下轮自动识别。')}</p><button type="button" class="secondary small" id="analyze-images" ${busy?'disabled':''}>识别已保存的图片</button>${state.image_analysis?.status==='running'?'<button type="button" class="text-button" id="stop-image-analysis">停止识别</button>':''}<p class="fine" aria-live="polite">${esc(state.image_analysis?.message||'识别图片文字与可见信息，关联原帖后纳入检索与核验。')}</p>`);
    $('collect-images').onclick=()=>action(async()=>{saveState(await api(meetingPath('/images/collect'),{method:'POST',body:'{}'}));});
    if($('stop-images'))$('stop-images').onclick=()=>action(async()=>{saveState(await api(meetingPath('/images/stop'),{method:'POST',body:'{}'}));});
    $('analyze-images').onclick=()=>action(async()=>{saveState(await api(meetingPath('/images/analyze'),{method:'POST',body:'{}'}));});
    if($('stop-image-analysis'))$('stop-image-analysis').onclick=()=>action(async()=>{saveState(await api(meetingPath('/images/analysis/stop'),{method:'POST',body:'{}'}));});
  }
  const allRoles=[...new Set([...state.roles,...(round?.tasks||[]).map(t=>t.role),'review','host'])];
  $('role-list').innerHTML=allRoles.map(role=>{
    const task=round?.tasks.filter(t=>t.role===role).at(-1);
    const carried=round&&state.rounds.some(r=>r.number<round.number&&r.status==='complete'&&r.cards.some(c=>c.role===role));
    const instanceCount=new Set((round?.tasks||[]).filter(t=>t.role===role&&t.agent_id&&t.phase!=='verification').map(t=>t.agent_id)).size;
    const status=task?taskStatuses[task.status]:role==='host'?(round?.summary?'已汇总':'组织议程与汇总'):carried?'沿用上一轮结论':round?'本轮未分配':'准备就绪';
    return `<div class="role-row"><span class="avatar ${role}">${names[role][0]}</span><div><b>${names[role]}${instanceCount>1?` · ${instanceCount}个实例`:""}</b><small>${esc(status)}</small></div></div>`;
  }).join('');
  $('round-list').innerHTML=state.rounds.length?state.rounds.map(r=>`<button class="nav-item ${r.number===round?.number?'active':''}" data-round="${r.number}">${roundTitle(r)} <small>${r.status==='complete'?(remediationFor(r)||incompleteTasks(r)?completionLabel(r):r.verification_summary?`已汇总 · 核验 ${r.verification_summary.checked}/${r.verification_summary.total}`:'已汇总 · 历史核验见详情'):taskStatuses[r.status]||'进行中'}</small></button>`).join(''):'<p class="fine">首轮尚未开始</p>';
  $('report-list').innerHTML=state.reports.length?[...state.reports].reverse().map(r=>r.status==='complete'?`<a class="nav-item" href="/discussions/${state.id}/reports/${r.id}" target="_blank" rel="noopener">▤ 第 ${r.round} 轮报告${r.superseded_by?" · 历史版":r.supersedes?" · 修订版":""} ↗<small>打开预览 / 下载 / 打印</small></a>`:`<div class="report-progress">第 ${r.round} 轮 · ${esc(taskStatuses[r.status]||r.status)}<details><summary>查看任务详情</summary>${esc(r.error||reportProgress(r.progress))}</details></div>`).join(''):'<p class="fine">讨论结束后由你决定生成。</p>';
  $('material-list').innerHTML=state.materials.map(m=>`<div class="material-item">${esc(m.name)}<small>${m.chunks} 个片段${m.comment_count?` · ${m.comment_count} 条评论`:''}</small></div>`).join('') || '<p class="fine">暂无上传资料</p>';
  $('upload').disabled=state.mode==='demo';
  $('round-caption').textContent=round?`/ 第 ${round.number} 轮`:'';
  $('round-stage').textContent=state.status==='analyzing_images'?'正在识别已保存的配图，成功结果纳入证据库':state.status==='collecting_images'?'正在补取已保存帖子的图片，不运行研讨':state.status==='planning'?'主持人正在拟定议程':round?.stage||'确认议程后开始第一轮';
  $('activity-dot').classList.toggle('busy',busy);
  const roundMessages=state.messages.filter(m=>m.round && m.round===round?.number);
  const pendingMessages=(!round||round.number===last?.number)?state.messages.filter(m=>!m.round):[];
  const messages=[...roundMessages,...pendingMessages];
  renderCollectionSummary(round);
  const signature=JSON.stringify([round?.number,round?.status,round?.cards,round?.issues,round?.peer_reviews,round?.summary,round?.tool_runs,round?.remediation,round&&remediationFor(round)?.remediation,(round?.tasks||[]).map(t=>[t.id,t.status,t.error,t.reply_repair,t.response_validation]),messages,state.status]);
  if(signature!==lastSignature){
    const timeline=$('timeline'), oldScroll=timeline.scrollTop;
    const detailKey=e=>e.dataset.detailKey||((e.closest('[data-card-id]')?.dataset.cardId||e.closest('.debate-issue')?.id||'round')+'|'+(e.className||e.querySelector('summary')?.textContent));
    const details=new Set([...timeline.querySelectorAll('details[open]')].map(detailKey));
    const oldCount=timeline.querySelectorAll('.chat-message').length;
    const rect=timeline.getBoundingClientRect();
    const inView=e=>{const r=e.getBoundingClientRect();return r.bottom>rect.top+5&&r.top<rect.bottom;};
    const anchor=[...timeline.querySelectorAll('.chat-message[data-chat-key],#round-summary')].find(inView)||[...timeline.querySelectorAll('[data-chat-key]')].find(inView);
    const anchorKey=anchor?.dataset.chatKey, anchorOffset=anchor?anchor.getBoundingClientRect().top-rect.top:0;
    const tasks=round?.tasks||[];
    let body=round?`<div class="round-divider">${roundTitle(round)} · ${esc(taskStatuses[round.status]||'研讨中')}</div>`:'';
    if(round?.remediation)body+=`<div class="system-event">沿用第 ${round.remediation.source_round} 轮的冻结资料和已完成对话，补跑 ${round.remediation.source_task_ids.length} 项失败修订，再完成核验与主持汇总。<button class="text-button" data-round="${round.remediation.source_round}">查看原轮次 ↗</button></div>`;
    let taskDetails=""; if(tasks.length)taskDetails+=`<details class="task-strip"><summary>任务执行记录 · ${tasks.filter(t=>t.status==='complete').length} / ${tasks.length} 项完成</summary>${tasks.map(t=>`<div>${esc(names[t.role])}${t.phase?' / '+esc(phaseNames[t.phase]||t.phase):' / 任务分派'} · ${esc(taskStatuses[t.status])}${t.feedback_ids?.length?' · 已关联用户指导':''}${t.error?`<br>${esc(t.error)}`:''}</div>`).join('')}</details>`;
    body+=roundMessages.map(userCard).join('');
    body+=round?.issues?.length?debateView(round):(round?.cards||[]).map(card).join('');
    body+=taskDetails;
    if(round?.summary)body+=roundConclusion(round);
    body+=pendingMessages.map(userCard).join('');
    if(!round&&!messages.length)body='<div class="empty"><div class="empty-icon">◎</div><h3>议题已就位，讨论由你开启</h3><p>检查左侧议程，也可以先上传资料。<br>点击下方“开始第一轮”，观察各角色的分析与汇总。</p></div>';
    $('timeline').innerHTML=body;
    [...timeline.querySelectorAll('details')].forEach(e=>e.open=details.has(detailKey(e)));
    timeline.scrollTop=oldScroll;
    if(anchorKey){const next=[...timeline.querySelectorAll('[data-chat-key]')].find(e=>e.dataset.chatKey===anchorKey);if(next)timeline.scrollTop+=next.getBoundingClientRect().top-timeline.getBoundingClientRect().top-anchorOffset;}
    if(oldCount && timeline.querySelectorAll('.chat-message').length>oldCount)$('new-answers').hidden=false;
    lastSignature=signature;
  }
  const summary=round?.summary;
  $('summary-content').innerHTML=summary?`<p class="summary-overview">${evidenceText(summary.overview)}</p>`+[['agreements','已形成的共识'],['disagreements','分歧与待核实'],['gaps','资料与任务缺口'],['next_questions','建议继续追问']].map(([k,title])=>`<div class="summary-block"><h3>${title}</h3><ul>${(summary[k]||[]).map(t=>`<li>${evidenceText(t)}</li>`).join('')||'<li>本轮未提出</li>'}</ul></div>`).join(''):'<p class="fine">主持人将在本轮完成后汇总共识、分歧和下一步建议。</p>';
  if(round?.sentiment){const s=round.sentiment;$('summary-content').innerHTML+=`<div class="summary-block"><h3>评论样本 · ${s.unique} 条去重后</h3><div class="stats-grid">${Object.entries(s.counts).map(([k,v])=>`<div class="stat"><b>${v}</b><span>${esc(k)} · ${s.percentages[k]===null?'未统计':s.percentages[k]+'%'}</span></div>`).join('')}</div><p>${esc(s.scope)}</p><p>原始 ${s.total} 条；实际抽样 ${s.sampled??s.unique} 条；分类成功 ${s.classified} 条，失败 ${s.failed} 条</p><p>${esc(s.method)}</p></div>`;}
  $('feedback-list').innerHTML=state.messages.map(m=>`<div class="feedback-item"><span class="tag">${esc(messageStatuses[m.status])}${m.resolution==='partial'?' · 仍有缺口':''}</span><p>${esc(m.content)}</p><details><summary>查看处理记录</summary><p>${evidenceText(m.response)}</p></details></div>`).join('')||'<p class="fine">你的指导会在这里追踪处理进展。</p>';
  if(round?.sentiment?.sampling){const x=round.sentiment.sampling;$('summary-content').innerHTML+=`<details class="summary-block"><summary>抽样范围与可复现信息</summary><p>算法：${esc(x.version)} · 种子：${esc(x.seed)}</p><p>${Object.entries(x.allocations||{}).map(([k,v])=>esc(k)+'：'+v+' 条').join('<br>')}</p><p>未覆盖分层：${(x.uncovered_strata||[]).map(esc).join('；')||'无'}</p></details>`;}
  const evidenceSignature=JSON.stringify([state.id,state.evidence_count,state.image_collection?.started_at,state.image_collection?.processed,state.image_analysis?.started_at,state.image_analysis?.processed]);
  if(evidenceSignature!==lastEvidenceSignature){lastEvidenceSignature=evidenceSignature;evidenceCache.clear();renderEvidence();}
  $('demo-tip').hidden=state.mode!=='demo';
  $('message-help').textContent=busy?'新消息会留在对话中，下一轮处理。':'发送后，点击“开始下一轮”继续研讨。';
  updateComposer();
  $('generate-report').disabled=busy||!snapshot;
  $('stop-collection').hidden=state.status!=='running'||state.mode==='demo';$('stop-collection').disabled=!!last?.collection_stop_requested;
  $('stop-round').hidden=state.status!=='running';$('stop-round').disabled=!!last?.exchange_stop_requested;
  $('stop-round').textContent=last?.exchange_stop_requested?'交锋将结束，继续核验':'结束交锋并汇总';
  $('terminate-round').hidden=state.status!=='running';
  $('resume-round').hidden=busy||!['failed','interrupted','cancelled'].includes(last?.status);
  $('remediate-round').hidden=busy||round?.number!==last?.number||last?.status!=='complete'||
    !(last?.tasks||[]).some(t=>t.phase==='revision'&&['failed','interrupted','cancelled'].includes(t.status));
  const existing=snapshot&&[...state.reports].reverse().find(r=>r.snapshot_id===snapshot.id&&r.status==='complete');
  $('generate-report').textContent=existing?'查看本轮报告':'生成本轮报告';
  $('start-round').disabled=busy;
  $('start-round').textContent=busy?'任务进行中…':state.rounds.length?'开始下一轮 →':'开始第一轮 →';
  $('decision-title').textContent=busy?'协作进行中':state.snapshots.length?'现在，由你决定':'准备开始';
  $('decision-description').textContent=busy?'可发送指导，新消息将在下一轮处理。':state.snapshots.length?'生成选中轮次的报告，或提交指导继续研讨。':'检查议程与资料，开始第一轮研讨。';
  observeCitations();
}
function debateView(round) { return chatDebateView(round); }
function renderCollectionSummary(round){
  const el=$('collection-summary'), b=round?.collection_budget, counts=state.collection_summary||{};
  el.hidden=!b&&!round?.collections?.length;
  if(el.hidden)return;
  const active=Boolean(b?.active);
  const initial=b?.phase==='initial';
  el.innerHTML=`<div><b>${active?(initial?'正在初始采集':'正在补证'):(initial?'初始采集已结束':'采集已结束')}</b><span> · 会议累计 ${state.evidence_count||0} 个片段 · ${state.comment_count||0} 条评论</span><small>${b?.mode==='adaptive'?'模型按需调度 · ':''}已采集 <span id="collection-elapsed">${Math.round(b?.used_seconds||0)}</span> 秒 · ${b?.request_count||0} 次外部请求 · ${round?.collection_summary?.attention??counts.attention??0} 项待处理</small></div><button id="open-collection" class="text-button">采集详情 ↗</button>`;
  $('open-collection').onclick=()=>{collectionPage=0;$('collection-dialog').showModal();loadCollections();};
  if($('collection-dialog').open)loadCollections();
}
let collectionTicket=0;
async function loadCollections(){
  const ticket=++collectionTicket, n=selectedRound??state.rounds.at(-1)?.number;
  const params=new URLSearchParams({round:n||'',platform:$('collection-platform').value,status:$('collection-status').value,page:collectionPage,size:12});
  try{
    const result=await api(meetingPath('/collections?'+params));if(ticket!==collectionTicket)return;
    collectionTotal=result.total;
    const openIds=[...$('collection-items').querySelectorAll('details[open]')].map(e=>e.dataset.task);
    $('collection-items').innerHTML=result.items.map(c=>`<details class="collection-row" data-task="${esc(c.id)}" ${openIds.includes(c.id)?'open':''}><summary><strong>${esc(c.platform_name)}</strong> · ${esc(c.kind==='comments'?'评论':c.kind==='web'?'网页':'帖子')} · 第 ${c.page||0} 页 <span class="tag">${esc(collectionStatuses[c.status]||c.status)}</span></summary><p>问题：${esc(c.question||c.reason)}</p><p>关键词：${esc(c.query)}</p>${c.search_providers?.length?`<p>检索服务：${c.search_providers.map(esc).join(" / ")}</p>`:""}<p>完成条件：${esc(c.condition||'见本轮任务')}</p><p>累计抓取 ${c.post_count||0} 帖 / ${c.comment_count||0} 评论；新增 ${c.new_evidence_count||0} 个片段；重复 ${c.duplicates||0} / 排除 ${c.excluded||0}</p>${c.comment_coverage?`<p>评论覆盖：${Number(c.comment_coverage.post_count)||0} 个帖子 · ${Number(c.comment_coverage.unique_text_count)||0} 条不同文本 · 单帖最多 ${Number(c.comment_coverage.largest_post_comments)||0} 条</p>`:''}<p>${esc(c.message)}</p>${c.result_diagnostic?`<p>${esc(c.result_diagnostic)}</p>`:''}${c.item_failures?.length?`<details><summary>${c.item_failures.length} 篇详情未取得</summary>${c.item_failures.map(f=>`<p>帖子 ${esc(f.post_id)} · ${esc(f.message)}（${esc(f.error_type)}）</p>`).join('')}</details>`:''}${c.assessment?`<p>需求评估：${esc(c.assessment.reason)}<br>${(c.assessment.gaps||[]).map(esc).join('；')}</p>`:''}${c.web_pages?.length?`<details><summary>网页正文读取 ${c.web_pages.filter(p=>p.status==='fetched').length}/${c.web_pages.length}</summary>${c.web_pages.map(p=>`<p>${esc(p.url)} · ${esc(p.status)} ${p.http_status||''}</p>`).join('')}</details>`:''}${c.resumable&&!isBusy()?`<button class="secondary small" data-resume-collection="${esc(c.id)}">下一轮继续采集</button>`:''}</details>`).join('')||'<p class="fine">暂无匹配任务。</p>';
    $('collection-page-label').textContent=`${collectionPage+1} / ${Math.max(1,Math.ceil(result.total/12))}`;
    $('collection-prev').disabled=!collectionPage;$('collection-next').disabled=(collectionPage+1)*12>=result.total;
  }catch(e){toast(e.message);}
}
async function renderEvidence(){
  const ticket=++evidenceRequest, mid=state.id;
  const query=$('evidence-search').value, type=$('evidence-filter').value;
  try{
    const params=new URLSearchParams({page:evidencePage,size:30,q:query});
    params.set(platformNames[type]?'platform':'source',type);
    const result=await api(`/api/discussions/${mid}/evidence?${params}`);
    if(ticket!==evidenceRequest||state.id!==mid)return;
    const pages=Math.max(1,Math.ceil(result.total/30));
    if(evidencePage>=pages){evidencePage=pages-1;return renderEvidence();}
    result.items.forEach(e=>{evidenceCache.set(e.id,e);refreshCitationLabels(e.id);});
    $('evidence-count').textContent=`${result.total} / ${state.evidence_count??state.evidence.length} 个片段`;
    $('evidence-list').innerHTML=result.items.map(e=>`<button class="evidence-item" data-evidence="${esc(e.id)}"><b>${esc(e.title)}</b><small>${esc(e.locator)} · ${esc(e.date)}${e.duplicate_of?' · 与其他来源内容重复':''}</small>${e.images?.length?`<span class="image-count">配图 ${e.images.filter(i=>i.status==='downloaded').length} / ${e.images.length} 张已保存 · ${e.images.filter(i=>i.analysis_status==='complete').length} 张已识别</span>`:''}</button>`).join('')||'<p class="fine">尚无匹配证据</p>';
    $('evidence-pagination').innerHTML=`<button class="secondary small" data-evidence-page="-1" ${evidencePage===0?'disabled':''}>上一页</button><span>${evidencePage+1} / ${pages}</span><button class="secondary small" data-evidence-page="1" ${evidencePage+1===pages?'disabled':''}>下一页</button>`;
  }catch(e){if(ticket===evidenceRequest)toast(e.message);}
}
async function showEvidence(id){
  const ticket=++sourceOpenRequest;
  try{
    const mid=state.id;
    const e=await fetchCitation(id);
    if(state.id!==mid||ticket!==sourceOpenRequest)return;
    evidenceCache.set(id,e);
    const info=sourceInfo(e);
    $('source-title').textContent=`引用 ${citationNumber(id)} · ${info.title}`;
    const oldMeta=$('source-meta');if(oldMeta.tagName==='P'){const div=document.createElement('div');div.id='source-meta';oldMeta.replaceWith(div);}
    const fields=[['来源',info.site],['资料类型',info.kind],['发布时间',info.date],['文本位置',e.locator||'未提供'],...(e.collected_at?[['采集时间',e.collected_at]]:[]),...(e.query?[['采集关键词',e.query]]:[])];
    $('source-meta').innerHTML=`<dl class="source-fields">${fields.map(([k,v])=>`<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join('')}</dl>${info.shortKind==='摘要'?'<p class="source-scope">此处保存的是搜索返回摘要，未作为全文展示。</p>':''}${e.date_scope?`<p class="source-scope">${esc(e.date_scope)}</p>`:''}<details class="source-id"><summary>证据定位编号</summary><code>${esc(id)}</code></details><h3 class="source-excerpt-title">本条引用保存的原文片段</h3>`;
    $('source-text').textContent=e.text;
    if(e.source_type==='image_analysis'){
      $('source-meta').querySelector('.source-excerpt-title').textContent='图片文字与可见信息';
      $('source-meta').insertAdjacentHTML('beforeend',`<p class="source-scope">图片机器提取 · ${esc(e.model)} · ${esc(e.extraction_scope)}。图中文字不等于已证实的现实事实。</p><button type="button" class="text-button" id="image-parent">查看所属帖子</button>`);
      $('image-parent').onclick=()=>showEvidence(e.parent_evidence_id);
    }
    renderSourceImages(e,mid);
    const linked=/^https?:\/\//i.test(e.url);
    $('source-link').hidden=!linked;$('source-link').href=linked?e.url:'#';
    $('source-link').textContent=e.source_type==='social_comment'?`打开所属帖子 · ${info.site} ↗`:`打开原始页面 · ${info.site} ↗`;
    if(!$('evidence-dialog').open)$('evidence-dialog').showModal();
  }catch(e){toast(e.message);}
}
function renderSourceImages(e,mid){
  let gallery=$('source-images');
  if(!gallery){gallery=document.createElement('section');gallery.id='source-images';}
  if(e.source_type==='image_analysis')$('source-text').after(gallery);else $('source-text').before(gallery);
  const images=e.images||[];
  gallery.hidden=!images.length;
  gallery.innerHTML=images.length?`<h3>帖子配图 <span class="tag">${images.some(i=>i.analysis_status==='complete')?'已有图片识别结果':'尚未识别图片内容'}</span></h3><p class="fine">点击预览可放大。识别内容作为独立图片证据参与后续研讨，已完成的历史回答保持不变。</p>${e.source_type==='social_post'?`<button type="button" class="secondary small" id="analyze-source-images" ${isBusy()?'disabled':''}>识别本帖配图 / 重试失败项</button>`:''}<div class="source-image-grid">${images.map(i=>{
    const saved=i.status==='downloaded'&&/^[a-f0-9]{64}$/.test(i.sha256);
    const url=saved?`/api/discussions/${encodeURIComponent(mid)}/images/${i.sha256}`:'';
    return `<figure>${saved?`<a href="${url}" target="_blank" rel="noopener" aria-label="查看第 ${Number(i.index)} 张配图"><img loading="lazy" src="${url}" alt="${esc(i.kind)} ${Number(i.index)}"></a>`:'<div class="image-unavailable">未下载</div>'}<figcaption><b>图 ${Number(i.index)} · ${esc(i.kind||'帖子配图')}</b>${saved?`<span>${Number(i.width)} × ${Number(i.height)} · ${(i.size/1024).toFixed(0)} KB</span><a href="${url}?download=1">下载原始文件 ↗</a>`:`<span>${esc(i.message||'图片尚未下载')}</span>`}<span>${i.analysis_status==='complete'?`已识别 · ${esc(i.analysis_model)} · ${Number(i.analysis_regions)} 个区域`:i.analysis_status==='failed'?`识别失败：${esc(i.analysis_error)}`:'尚未识别'}</span>${(i.analysis_evidence_ids||[]).map((id,n)=>`<button type="button" class="text-button image-extraction" data-extraction="${esc(id)}">查看识别片段 ${n+1}</button>`).join('')}<small>${esc(i.collected_at||'')}${i.preview_note?' · '+esc(i.preview_note):''}</small>${i.origin_post_id?`<details><summary>图片来源定位</summary><small>原帖 ${esc(i.origin_post_id)}${saved?' · SHA-256 '+esc(i.sha256):''}</small></details>`:''}</figcaption></figure>`;
  }).join('')}</div>`:'';
  gallery.querySelectorAll('img').forEach(img=>img.addEventListener('error',()=>{const text=document.createElement('span');text.className='image-unavailable';text.textContent='预览暂不可用，请确认服务已加载图片接口';img.replaceWith(text);},{once:true}));
  gallery.querySelectorAll('[data-extraction]').forEach(button=>button.onclick=()=>showEvidence(button.dataset.extraction));
  if($('analyze-source-images'))$('analyze-source-images').onclick=()=>action(async()=>{saveState(await api(meetingPath('/images/analyze'),{method:'POST',body:JSON.stringify({evidence_ids:[e.id]})}));$('evidence-dialog').close();toast('图片识别已开始，完成后可查看识别片段');});
}
if(!document.querySelector('#evidence-filter option[value="image"]'))$('evidence-filter').add(new Option('含图片的帖子','image'));
if(!document.querySelector('#evidence-filter option[value="image_analysis"]'))$('evidence-filter').add(new Option('图片识别内容','image_analysis'));
function openCreate(){ $('create-dialog').showModal(); }
$('new-meeting').onclick=openCreate;$('welcome-create').onclick=openCreate;
$('quick-demo').onclick=()=>action(async()=>{const s=await api('/api/discussions',{method:'POST',body:JSON.stringify({title:'演示案例',mode:'demo',report_type:'brief',web_enabled:false})});await openMeeting(s.id);});
$('create-form').onsubmit=e=>{e.preventDefault();action(async()=>{const form=new FormData(e.target);const s=await api('/api/discussions',{method:'POST',body:JSON.stringify({...Object.fromEntries(form),web_enabled:form.has('web_enabled'),crawl_platforms:form.getAll('crawl_platforms')})});$('create-dialog').close();await openMeeting(s.id);});};
$('message-form').onsubmit=e=>{e.preventDefault();action(async()=>{
  const content=messageInput.value;
  if(!content.trim()||sendingMessage)return;
  sendingMessage=true;updateComposer();
  try{
    const next=await api(meetingPath('/messages'),{method:'POST',body:JSON.stringify({content,target:$('target').value,claim_id:guidedClaim})});
    selectedRound=null;saveState(next);
    if(messageInput.value===content){messageInput.value='';guidedClaim=null;}
    $('timeline').querySelector('.user-card:last-child')?.scrollIntoView({block:'end'});
    $('new-answers').hidden=true;persistView();
    toast('消息已发送，开始下一轮后由智能体处理');
  }finally{sendingMessage=false;updateComposer();messageInput.focus();}
});};
$('demo-followup').onclick=()=>{guidedClaim=null;$('message').value=state.demo_followup;updateComposer();$('message').focus();};
$('start-round').onclick=()=>action(async()=>{if($('message').value.trim())throw new Error('输入框中有未发送的指导，请先发送，确保它进入下一轮。');selectedRound=null;saveState(await api(meetingPath('/rounds'),{method:'POST',body:JSON.stringify({expected_round:state.rounds.length})}));mobile('forum');});
$('generate-report').onclick=()=>action(async()=>{const n=selectedRound??state.rounds.at(-1)?.number;const snapshot=state.snapshots.find(s=>s.round===n);if(!snapshot)return;const existing=[...state.reports].reverse().find(r=>r.snapshot_id===snapshot.id&&r.status==='complete');if(existing){window.open(`/discussions/${state.id}/reports/${existing.id}`,'_blank','noopener');return;}saveState(await api(meetingPath('/reports'),{method:'POST',body:JSON.stringify({snapshot_id:snapshot.id})}));toast('正在根据第 '+n+' 轮快照生成报告，完成后可在左侧打开。');});
$('upload').onchange=()=>action(async()=>{const file=$('upload').files[0];if(!file)return;const body=new FormData();body.append('file',file);saveState(await api(meetingPath('/materials'),{method:'POST',body}));$('upload').value='';toast('资料已入库，将用于下一轮研讨');});
$('edit-agenda').onclick=()=>{$('agenda-input').value=state.agenda.join('\n');$('debate-exchanges').value=state.debate_exchanges||2;$('report-depth').value=state.report_depth||'standard';document.querySelectorAll('#agenda-form input[type=checkbox]').forEach(e=>e.checked=state.roles.includes(e.value));$('agenda-dialog').showModal();};
$('agenda-form').onsubmit=e=>{e.preventDefault();action(async()=>{saveState(await api(meetingPath(),{method:'PATCH',body:JSON.stringify({report_depth:$('report-depth').value,debate_exchanges:Number($('debate-exchanges').value),agenda:$('agenda-input').value.split('\n').filter(x=>x.trim()),roles:[...document.querySelectorAll('#agenda-form input:checked')].map(e=>e.value)})}));$('agenda-dialog').close();});};
$('evidence-search').oninput=()=>{evidencePage=0;renderEvidence();};
$('evidence-filter').onchange=()=>{evidencePage=0;renderEvidence();};
$('stop-collection').onclick=()=>action(async()=>{saveState(await api(meetingPath('/collections/stop'),{method:'POST',body:'{}'}));toast('当前请求结束后停止采集，已取得资料将用于分析，本轮不再补采。');});
$('stop-round').onclick=()=>action(async()=>{saveState(await api(meetingPath('/stop'),{method:'POST',body:'{}'}));toast('结束追加交锋，继续核验已有主张后汇总。');});
async function refreshConnections(initial=false){
  const selected=initial?(state.crawl_platforms||[]):[...document.querySelectorAll('#platform-connections input:checked')].map(e=>e.value);
  const {items}=await api('/api/discussion-crawler/connections');
  $('platform-connections').innerHTML=items.map(p=>`<div class="platform-row"><div class="platform-title"><label class="checkbox"><input type="checkbox" value="${p.platform}" ${selected.includes(p.platform)?'checked':''}>${esc(p.name)}</label><span class="tag">${esc(collectionStatuses[p.status]||p.status)}</span></div><p class="fine">${esc(p.message)}</p><div class="platform-actions"><button type="button" class="secondary small" data-login="${p.platform}" ${['running','login_open'].includes(p.status)?'disabled':''}>打开登录窗口</button><button type="button" class="secondary small" data-login-finish="${p.platform}" ${p.status!=='login_open'?'disabled':''}>保存登录</button></div></div>`).join('');
}
$('edit-sources').onclick=()=>action(async()=>{$('sentiment-sample').value=state.sentiment_sample_size||500;await refreshConnections(true);$('sources-dialog').showModal();});
$('refresh-connections').onclick=()=>action(()=>refreshConnections());
$('sources-form').onsubmit=e=>{e.preventDefault();action(async()=>{const crawl_platforms=[...document.querySelectorAll('#platform-connections input:checked')].map(e=>e.value);saveState(await api(meetingPath('/sources'),{method:'PATCH',body:JSON.stringify({crawl_platforms,sentiment_sample_size:Number($('sentiment-sample').value)})}));$('sources-dialog').close();toast('采集设置已保存，开始下一轮后由智能体安排采集');});};
setInterval(()=>{if($('sources-dialog').open&&!busyAction)refreshConnections().catch(()=>{});},4000);
function persistView(){if(state)sessionStorage.setItem('discussion-view-'+state.id,JSON.stringify({round:selectedRound,scroll:$('timeline').scrollTop}));}
$('timeline').addEventListener('scroll',persistView);
$('collection-platform').onchange=$('collection-status').onchange=()=>{collectionPage=0;loadCollections();};
$('collection-prev').onclick=()=>{collectionPage--;loadCollections();};$('collection-next').onclick=()=>{collectionPage++;loadCollections();};
$('resume-round').onclick=()=>action(async()=>saveState(await api(meetingPath('/rounds/resume'),{method:'POST',body:'{}'})));
$('remediate-round').onclick=()=>action(async()=>{selectedRound=null;saveState(await api(meetingPath('/rounds/remediate'),{method:'POST',body:JSON.stringify({expected_round:state.rounds.length})}));mobile('forum');});
$('terminate-round').onclick=()=>action(async()=>saveState(await api(meetingPath('/stop'),{method:'POST',body:JSON.stringify({action:'terminate'})})));
$('toggle-minutes').onclick=()=>{$('insights-dialog').showModal();};
$('new-answers').onclick=()=>{$('new-answers').hidden=true;const cards=$('timeline').querySelectorAll('.chat-message');cards[cards.length-1]?.scrollIntoView({block:'start'});};
document.addEventListener('click',e=>{
  const retryIssue=e.target.closest('[data-retry-issue]');if(retryIssue){const round=state.rounds.find(r=>(r.issues||[]).some(i=>i.id===retryIssue.dataset.retryIssue));const issue=round?.issues.find(i=>i.id===retryIssue.dataset.retryIssue);if(issue){guidedClaim=null;$('target').value=issue.role;$('message').value=issueFollowup(issue,round);mobile('forum');updateComposer();$('message').focus();}}
  const jump=e.target.closest('[data-jump]');if(jump){if(jump.dataset.jump==='summary')$('round-summary')?.scrollIntoView({block:'start'});else $('timeline').scrollTop=0;}
  const reply=e.target.closest('[data-chat-jump]');if(reply){const el=document.getElementById('chat-'+reply.dataset.chatJump);el?.scrollIntoView({block:'center'});el?.classList.add('highlight-message');setTimeout(()=>el?.classList.remove('highlight-message'),1600);}
  const nextQuestion=e.target.closest('[data-next-question]');if(nextQuestion){const r=state.rounds.find(r=>r.number===(selectedRound??state.rounds.at(-1)?.number));const q=r?.summary?.next_questions?.[Number(nextQuestion.dataset.nextQuestion)];if(q){guidedClaim=null;$('message').value=[$('message').value.trim(),q].filter(Boolean).join('\n');updateComposer();$('message').focus();}}


  const resume=e.target.closest('[data-resume-collection]');if(resume)action(async()=>{selectedRound=null;saveState(await api(meetingPath('/collections/resume'),{method:'POST',body:JSON.stringify({task_ids:[resume.dataset.resumeCollection],expected_round:state.rounds.length})}));});
  const pageButton=e.target.closest('[data-evidence-page]');if(pageButton){evidencePage+=Number(pageButton.dataset.evidencePage);renderEvidence();}
  const ask=e.target.closest('[data-ask-claim]');if(ask){const claim=state.rounds.flatMap(r=>r.cards||[]).flatMap(c=>c.claims||[]).find(c=>c.id===ask.dataset.askClaim);if(claim){guidedClaim=claim.id;$('message').value='请进一步核查这条结论：'+claim.text+'。请提供支持与反对证据，说明适用范围。';mobile('forum');updateComposer();$('message').focus();}}
  const close=e.target.closest('.close-dialog');if(close)close.closest('dialog').close();
  const meeting=e.target.closest('[data-meeting]');if(meeting)action(()=>openMeeting(meeting.dataset.meeting));
  const round=e.target.closest('[data-round]');if(round){selectedRound=Number(round.dataset.round);lastSignature='';render();mobile('forum');$('timeline').scrollTop=0;focusConversationStart();persistView();}
  const evidence=e.target.closest('[data-evidence]');if(evidence)showEvidence(evidence.dataset.evidence);
  const panel=e.target.closest('[data-panel]');if(panel)mobile(panel.dataset.panel);
  if(e.target.id==='retry-plan')action(async()=>saveState(await api(meetingPath('/plan'),{method:'POST',body:'{}'})));
  const login=e.target.closest('[data-login],[data-login-finish]');
  if(login)action(async()=>{const finish=!!login.dataset.loginFinish;const p=login.dataset.loginFinish||login.dataset.login;await api(`/api/discussion-crawler/${p}/login`,{method:'POST',body:JSON.stringify({action:finish?'finish':'start',query:state.title})});await refreshConnections();toast(finish?'正在保存本机会话':'正在打开本机浏览器，请完成登录或搜索验证后回到这里保存');});
});
function setTheme(theme){document.documentElement.dataset.theme=theme;$('theme-toggle').textContent=theme==='light'?'☾':'☼';$('theme-toggle').setAttribute('aria-label',theme==='light'?'切换深色主题':'切换浅色主题');try{localStorage.setItem('discussion-theme',theme);}catch(_){} }
let preferredTheme='dark';try{preferredTheme=localStorage.getItem('discussion-theme')||'dark';}catch(_){}setTheme(preferredTheme);
$('theme-toggle').onclick=()=>setTheme(document.documentElement.dataset.theme==='light'?'dark':'light');
messageInput.addEventListener('input',updateComposer);
window.addEventListener('resize',updateComposer);
messageInput.addEventListener('keydown',e=>{
  if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing&&e.keyCode!==229){e.preventDefault();if(!sendButton.disabled)$('message-form').requestSubmit();}
});
updateComposer();
loadHistory().catch(e=>toast(e.message));
const initial=new URLSearchParams(location.search).get('meeting');if(initial)action(()=>openMeeting(initial));
window.addEventListener('beforeunload',()=>stream?.close());


// Display elapsed time only; duration does not stop acquisition.
setInterval(()=>{
  const round=selectedRound===null?state?.rounds.at(-1):state?.rounds.find(r=>r.number===selectedRound);
  const b=round?.collection_budget, display=$('collection-elapsed');
  if(!b||!display)return;
  const elapsed=b.active&&b.updated_at?Math.max(0,(Date.now()-Date.parse(b.updated_at))/1000):0;
  display.textContent=Math.round((b.used_seconds||0)+elapsed);
},1000);
