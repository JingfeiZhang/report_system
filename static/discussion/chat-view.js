'use strict';

// Render persisted discussion records. Position identifies a participant, not a stance.
function agentLabel(id, round) {
  const card = (round.cards || []).find(c => c.agent_id === id);
  const issue = (round.issues || []).find(i => (i.agents || []).includes(id));
  const members = issue?.agents || [];
  const index = members.indexOf(id);
  return (card?.role_name || names[issue?.role] || '分析智能体') +
    (members.length > 1 && index >= 0 ? ` · ${String.fromCharCode(65 + index)}` : '');
}

function agentSide(id, round) {
  const issue = (round.issues || []).find(i => (i.agents || []).includes(id));
  const members = issue?.agents || [];
  const index = members.indexOf(id);
  return index >= 0 ? (index % 2 ? 'right' : 'left') : 'left';
}

function messageFrame({id, agent, role, label, phase, body, side = 'left', kind = ''}) {
  return `<article id="chat-${esc(id)}" data-chat-key="${esc(id)}" data-card-id="${esc(id)}" class="chat-message ${side} ${kind}">
    <div class="speaker-avatar avatar ${esc(role)}" aria-hidden="true">${esc((names[role] || label || '研')[0])}</div>
    <div class="message-body"><header class="message-meta"><b>${esc(label)}</b><span>${esc(phase)}</span></header>
    <div class="message-bubble" ${agent ? `data-agent="${esc(agent)}"` : ''}>${body}</div></div></article>`;
}

function chatCard(c, round) {
  round = round || state.rounds.find(r => r.number === c.round) || {cards: [], issues: []};
  const latest = !(round.cards || []).some(x => x.agent_id === c.agent_id && (x.version || 1) > (c.version || 1));
  const extra = (c.evidence_ids || []).filter(id => !String(c.conclusion).includes(id));
  const claims = c.claims || [];
  const checked = claims.filter(cl => cl.evidence_status && cl.evidence_status !== 'unverified').length;
  const detail = claims.length ? `<details class="claim-details" data-detail-key="claims-${esc(c.id || c.task_id)}">
    <summary>${latest ? `主张与核验 · ${checked}/${claims.length} 项已核验` : '初稿主张 · 已有后续修订'}</summary>
    <div class="claim-list">${claims.map(cl => `<div class="claim-row" id="claim-${esc(cl.id)}">
      <span class="tag status-${esc(cl.evidence_status || 'unverified')}">${esc(claimStatus(c, cl))}</span>
      <p>${evidenceText(cl.text)}</p>${cl.scope ? `<small>${esc(cl.scope)}</small>` : ''}
      ${cl.verification_reason ? `<p class="fine">核验依据：${evidenceText(cl.verification_reason)}</p>` : ''}
      ${citations(cl.evidence_ids)}<button class="text-button ask-button" data-ask-claim="${esc(cl.id)}">追问这条结论 ↗</button>
    </div>`).join('')}</div></details>` : '';
  const body = `<p class="answer-prose">${evidenceText(c.conclusion)}</p>
    ${c.phase === 'revision' && c.changes ? `<div class="change-line"><span>本次修订</span>${evidenceText(c.changes)}</div>` : ''}
    ${extra.length ? `<details class="source-tray" data-detail-key="sources-${esc(c.id || c.task_id)}"><summary>引用来源 · ${extra.length} 项</summary><div class="citation-tray">${citations(extra)}</div></details>` : ''}
    ${detail}<details class="answer-details" data-detail-key="details-${esc(c.id || c.task_id)}"><summary>完整任务、回应与局限</summary>
    ${c.task ? `<p><b>本轮任务</b><br>${evidenceText(c.task)}</p>` : ''}
    ${c.response ? `<p><b>对其他观点的回应</b><br>${evidenceText(c.response)}</p>` : ''}
    ${c.changes && c.phase !== 'revision' ? `<p>${evidenceText(c.changes)}</p>` : ''}
    ${(c.limitations || []).length ? `<p><b>局限</b><br>${c.limitations.map(evidenceText).join('<br>')}</p>` : ''}</details>`;
  return messageFrame({id: c.id || c.task_id, agent: c.agent_id, role: c.role,
    label: c.agent_id ? agentLabel(c.agent_id, round) : c.role_name,
    phase: (phaseNames[c.phase] || '分析回答') + (c.version ? ` · v${c.version}` : '') + (!latest ? ' · 历史版本' : '') + (c.carried_from_round ? ` · 沿用第 ${c.carried_from_round} 轮` : ''),
    body, side: agentSide(c.agent_id, round), kind: 'card answer-message'});
}

function chatUserCard(m) {
  const recipient = names[m.target] ? ` · 发给${names[m.target]}` : '';
  return `<article id="chat-${esc(m.id)}" data-chat-key="${esc(m.id)}" class="chat-message right user-card">
    <div class="message-body"><div class="message-bubble"><p class="answer-prose">${esc(m.content)}</p></div>
    <header class="message-meta"><span>你${recipient} · ${esc(messageStatuses[m.status] || '已发送')}</span></header></div></article>`;
}

function toolRecord(t) {
  return `<details class="tool-record" data-detail-key="tool-${esc(t.id)}"><summary><span aria-hidden="true">⌕</span> 定向补证 · ${esc(t.query)} <span class="tag">新增 ${t.new_evidence_ids?.length || 0} 条</span></summary>
    <p>${esc(t.message)}</p>${citations(t.new_evidence_ids)}
    ${t.request_count !== undefined ? `<p class="fine">外部请求 ${t.request_count} 次${t.raw_count != null ? ` · 返回 ${t.raw_count} 条` : ''}${t.excluded ? ` · 排除 ${t.excluded} 条` : ''}</p>` : ''}
    ${t.local_evidence_ids?.length ? `<p class="fine">已有资料召回 ${t.local_evidence_ids.length} 条</p>${citations(t.local_evidence_ids)}` : ''}</details>`;
}

function reviewConversation(review, round) {
  const author = agentLabel(review.author, round), target = agentLabel(review.target_agent, round);
  const role = (round.cards || []).find(c => c.agent_id === review.author)?.role || 'review';
  const challenges = review.challenges || [];
  let html = '';
  if ((review.agreements || []).length) {
    html += messageFrame({id: review.id + '-agreement', agent: review.author, role, label: author,
      phase: '互审 · 认可与补充', side: agentSide(review.author, round), kind: 'review-message',
      body: `<div class="reply-context">回应 ${esc(target)}</div><ul class="prose-list">${review.agreements.map(a => `<li>${evidenceText(a)}</li>`).join('')}</ul>
      ${review.own_revision ? `<details data-detail-key="reflection-${esc(review.id)}"><summary>对自身答案的启发</summary><p>${evidenceText(review.own_revision)}</p></details>` : ''}`});
  }
  challenges.forEach(ch => {
    const claim = (round.cards || []).flatMap(c => c.claims || []).find(c => c.id === ch.claim_id);
    html += messageFrame({id: ch.id, agent: review.author, role, label: author,
      phase: `向 ${target} 提出质疑` + (review.carried_from_round ? ` · 原第 ${review.carried_from_round} 轮质询` : ''), side: agentSide(review.author, round), kind: 'challenge-message challenge-item',
      body: `<div class="reply-context"><span>针对对方主张</span><p>${evidenceText(claim?.text || '历史记录未提供主张原文')}</p></div>
        <p class="challenge-question">${evidenceText(ch.question)}</p><p>${evidenceText(ch.reason)}</p>
        ${ch.resolution ? `<details data-detail-key="resolution-${esc(ch.id)}"><summary>需要如何核实</summary><p>${evidenceText(ch.resolution)}</p></details>` : ''}
        <span class="tag status-${ch.status === 'resolved' ? 'supported' : 'partial'}">${ch.status==='responded'&&ch.verification?'已核验，仍有分歧':({open:'待回应',responded:'已回应，待核验',resolved:'经核验已解决'})[ch.status] || '待处理'}</span>`});
    html += (round.tool_runs || []).filter(t => t.challenge_id === ch.id).map(toolRecord).join('');
    if (ch.reply) {
      html += messageFrame({id: ch.id + '-reply', agent: review.target_agent,
        role: (round.cards || []).find(c => c.agent_id === review.target_agent)?.role || role,
        label: target, phase: '回应质疑' + (({maintain:' · 维持',qualify:' · 限定',revise:' · 修订',withdraw:' · 撤回'})[ch.reply.action] || ''), side: agentSide(review.target_agent, round), kind: 'reply-message',
        body: `<button class="reply-context reply-link" data-chat-jump="${esc(ch.id)}">↳ 回应 ${esc(author)}：${esc(ch.question)}</button>
          <p>${evidenceText(ch.reply.response)}</p>${citations(ch.reply.evidence_ids)}
          ${ch.verification ? `<div class="verification-note"><b>核验反馈</b><p>${evidenceText(ch.verification.reason)}</p></div>` : ''}`});
    }
  });
  if (!challenges.length && !(review.agreements || []).length) {
    html += `<div class="system-event">${esc(author)} ${review.complete === false ? '互审记录不完整，不能视为达成共识' : '已完成互审 · 未提出具体质疑'}</div>`;
  }
  return html;
}

function interactionState(issue, round, reviews) {
  if (issue.status === 'failed') return {label:'分析未完成', detail:'本议题没有生成有效回答，具体原因见下方任务记录。'};
  if (issue.mode === 'single') return {label: '专项分析 + 核验', detail: '本轮仅安排一个分析实例，没有相互质疑环节。'};
  if (issue.mode === 'degraded') return {label: '互审未完成', detail: '独立实例未全部完成，保留已有答案与核验记录。'};
  if (!reviews.length) return {label: '独立分析 + 双向互审', detail:
    round.status === 'running' ? '独立答案提交后开始互审。' : '本轮没有完成互审，详见议题结束原因。'};
  const complete = (issue.agents || []).every(id => reviews.some(r => r.author === id && r.complete !== false));
  if (!complete) return {label: '互审未完成', detail: '部分实例的互审记录缺失或不完整，不能视为已达成共识。'};
  const count = reviews.reduce((n,r) => n + (r.challenges || []).length, 0);
  return {label: '独立分析 + 双向互审', detail: count ? `已记录 ${count} 项具体质疑，展开对话查看回应和修订。` : '已完成相互评议，未提出实质质疑；保留认可意见与核验记录。'};
}

function issueExecutionNotice(issue, round, cards) {
  const tasks=(round.tasks||[]).filter(t=>t.issue_id===issue.id && t.phase);
  const failures=tasks.filter(t=>['failed','interrupted','cancelled'].includes(t.status));
  if(failures.length){
    const recovered=remediationFor(round);
    const repaired=new Set((recovered?.remediation.results||[]).map(x=>x.source_task_id));
    if(failures.every(t=>repaired.has(t.id)))return `<div class="system-event remediation-link">这 ${failures.length} 项历史失败已在补救版本完成修订与核验。<button class="text-button" data-round="${recovered.number}">查看第 ${recovered.number} 轮补救结果 ↗</button></div>`;
    const details=failures.map(t=>{
      const validation=t.error_type==='response_validation'||/未逐项回应|回应校验|互审质询缺少/.test(t.error||'');
      const cause=validation?'回应校验未通过':t.error_type==='timeout'||/Timeout/i.test(t.error||'')?'模型请求超时':taskStatuses[t.status]||'执行失败';
      const preserved=validation&&t.phase==='revision'?'本次修订未被采纳，对话中仍保留此前的有效答案。':'';
      const pending=t.reply_repair?.pending||[];
      const known=t.response_validation?.errors||[];
      return `<details data-detail-key="failure-${esc(t.id)}"><summary>${esc(agentLabel(t.agent_id,round))} · ${t.exchange?`第 ${Number(t.exchange)} 次交锋 · `:''}${esc(phaseNames[t.phase]||'分析')}：${esc(cause)}</summary><p>${esc(t.error||'未取得有效输出')}${t.attempts?` · 已尝试 ${Number(t.attempts)} 次`:''}${t.elapsed_seconds?` · 累计 ${Math.round(t.elapsed_seconds)} 秒`:''}</p>${preserved?`<p>${preserved}</p>`:''}${t.reply_repair?`<p>单项补答已保存 ${Number(t.reply_repair.completed)}/${Number(t.reply_repair.total)} 项，全部通过后才采纳修订。</p>`:''}${pending.length?`<p>仍需补齐的回应：</p><ul>${pending.map(c=>`<li>${esc(c.question)}${c.resolution?`<br><small>解决条件：${esc(c.resolution)}</small>`:''}</li>`).join('')}</ul>`:''}${known.length?`<details><summary>查看具体校验记录</summary><ul>${known.map(e=>`<li>${esc(e)}</li>`).join('')}</ul></details>`:validation?'<p class="fine">此历史任务未保存具体缺失项，无法逐项还原被拒绝的回复。</p>':''}</details>`;
    }).join('');
    return `<div class="issue-failure" role="status"><strong>第 ${Number(round.number)} 轮${round.status==='running'?'':'记录'} · ${failures.length} 项任务未完成</strong><p>${cards.length?'已有有效回答保留在对话中；以下步骤未完成。':'本议题尚未生成有效回答。'}</p>${details}<p>${round.status==='complete'?'本轮已汇总并冻结，未完成的交锋仍需后续处理。':'可恢复本轮未完成阶段；成功补答会按相同输入复用。'}</p><button class="text-button" data-retry-issue="${esc(issue.id)}">将未解决质询带入下一轮 ↗</button></div>`;
  }
  const repairing=tasks.filter(t=>t.status==='running'&&t.reply_repair);
  if(repairing.length)return `<div class="system-event" role="status">${repairing.map(t=>`${esc(agentLabel(t.agent_id,round))}正在逐项补答 · 已保存 ${Number(t.reply_repair.completed)}/${Number(t.reply_repair.total)} 项`).join('<br>')}</div>`;
  if(cards.length)return '';
  const active=round.status==='running'&&tasks.some(t=>['pending','running'].includes(t.status));
  return `<div class="system-event">${active?'智能体正在分析，完成后将在这里展示回答。':['cancelled','interrupted','failed'].includes(round.status)?'本轮已中断，尚未生成该议题的回答。':'本议题没有可展示的回答，任务当前未在运行。'}</div>`;
}

function issueFollowup(issue, round) {
  const questions=[...new Set((round.peer_reviews||[]).filter(r=>r.issue_id===issue.id)
    .flatMap(r=>r.challenges||[]).filter(c=>c.status!=='resolved').map(c=>c.question).filter(Boolean))];
  if(!questions.length)return '请重新分析此前未完成的议题：'+issue.question+' 请依据已有材料核查，需要时补充检索，并完成相互质询、回应和修订。';
  return `接着讨论第 ${round.number} 轮的“${issue.question}”。请优先处理这些还没解决的问题：\n`+
    questions.map((q,i)=>`${i+1}. ${q}`).join('\n')+'\n请结合已有回答和资料逐项回应，需要时补充查证，说明哪些结论保持、哪些需要修改，再由双方核验。';
}

function incompleteTasks(round) {
  return (round.tasks||[]).filter(t=>t.phase&&['failed','interrupted','cancelled'].includes(t.status)).length;
}

function remediationFor(round) {
  return (state?.rounds||[]).find(r=>r.status==='complete' && r.remediation?.status==='complete' && r.remediation.source_round===round.number);
}

function roundTitle(round) {
  return `第 ${round.number} 轮` + (round.remediation ? ` · 第 ${round.remediation.source_round} 轮补救` : '');
}

function completionLabel(round) {
  const recovered=remediationFor(round);
  if(recovered)return `已汇总 · 已补救，见第 ${recovered.number} 轮`;
  return incompleteTasks(round)?`已汇总 · ${incompleteTasks(round)} 项任务未完成`:'本轮结束';
}

function chatDebateView(round) {
  return round.issues.map((issue, index) => {
    const cards = (round.cards || []).filter(c => c.issue_id === issue.id && ['proposal','revision'].includes(c.phase));
    const latest = new Map();
    cards.forEach(c => {if (!latest.has(c.agent_id) || (c.version || 1) >= (latest.get(c.agent_id).version || 1)) latest.set(c.agent_id, c);});
    const reviews = (round.peer_reviews || []).filter(r => r.issue_id === issue.id);
    const challenges = reviews.flatMap(r => r.challenges || []);
    const claims = [...latest.values()].flatMap(c => c.claims || []);
    const checked = claims.filter(c => c.evidence_status && c.evidence_status !== 'unverified').length;
    const interaction = interactionState(issue, round, reviews);
    let html = `<section id="issue-${esc(issue.id)}" class="debate-issue" data-role="${esc(issue.role)}" data-chat-key="issue-${esc(issue.id)}">
      <header class="issue-heading"><div class="issue-kicker"><span class="issue-number">${String(index + 1).padStart(2, '0')}</span><span>${esc(names[issue.role] || '分析')}议题</span><span class="issue-metrics">${latest.size} 份回答 · ${challenges.filter(c => c.status !== 'resolved').length} 项待解质疑 · 核验 ${checked}/${claims.length}</span></div>
      <details class="issue-question" data-detail-key="question-${esc(issue.id)}"><summary title="${esc(issue.question)}">${esc(issue.question)}</summary><p>${esc(issue.question)}</p></details>
      <details class="collaboration-details" data-detail-key="collaboration-${esc(issue.id)}"><summary>${esc(interaction.label)}<span> · 协作安排</span></summary>
        <p>${esc(issue.reason || '历史记录未提供调度原因')}</p><p>${esc(interaction.detail)}</p>
        ${Object.entries(issue.agent_focus || {}).map(([id, focus]) => `<p><b>${esc(agentLabel(id, round))}</b> · ${esc(focus)}</p>`).join('')}</details></header>`;
    html += issue.mode === 'single' ? '<div class="phase-divider"><span>专项分析</span><small>本轮单实例作答，随后核验</small></div>' : '<div class="phase-divider"><span>独立研究</span><small>各自作答后，再交换观点</small></div>';
    const proposals = cards.filter(c => c.phase === 'proposal').sort((a,b) => (issue.agents || []).indexOf(a.agent_id) - (issue.agents || []).indexOf(b.agent_id));
    html += proposals.map(c => chatCard(c, round)).join('');
    const exchanges = [...new Set([...reviews.map(r => r.exchange || 1), ...cards.filter(c => c.phase === 'revision').map(c => c.exchange || 1)])].sort((a,b) => a-b);
    exchanges.forEach(exchange => {
      html += `<div class="phase-divider"><span>第 ${exchange} 次交锋</span><small>互审 · 补证 · 修订</small></div>`;
      html += reviews.filter(r => (r.exchange || 1) === exchange).map(r => reviewConversation(r, round)).join('');
      html += cards.filter(c => c.phase === 'revision' && (c.exchange || 1) === exchange).map(c => chatCard(c, round)).join('');
    });
    const verifications = (round.cards || []).filter(c => c.issue_id === issue.id && c.phase === 'verification');
    if (verifications.length) html += `<details class="verification-records" data-detail-key="verification-${esc(issue.id)}"><summary>交叉核验 · ${verifications.length} 份记录 · 主张状态见修订答案</summary>${verifications.map(c => chatCard(c, round)).join('')}</details>`;
    html += issueExecutionNotice(issue,round,cards);
    // Keep tool runs without a linked challenge accessible as well.
    const unlinked = (round.tool_runs || []).filter(t => t.issue_id === issue.id && !challenges.some(ch => ch.id === t.challenge_id));
    if (unlinked.length) html += `<details class="tool-record" data-detail-key="tools-${esc(issue.id)}"><summary>其他补证记录 · ${unlinked.length} 项</summary>${unlinked.map(toolRecord).join('')}</details>`;
    return html + `<p class="issue-result">${esc(issue.stop_reason || '议题处理中')}</p></section>`;
  }).join('');
}

function roundConclusion(round) {
  const summary = round.summary;
  const complete = round.status === 'complete';
  return `<article id="round-summary" data-chat-key="round-summary" class="round-conclusion host-card">
    <header class="card-header"><span class="avatar host">主</span><div class="meta"><span class="eyebrow">ROUND ${round.number} / SYNTHESIS</span><h3>主持人 · 本轮结论</h3></div><span class="tag">${complete ? completionLabel(round) : '已有汇总 · 见任务状态'}</span></header>
    <p class="conclusion-overview">${evidenceText(summary.overview)}</p>
    <div class="conclusion-grid">${[['agreements','形成的共识'],['disagreements','仍有分歧']].map(([key,title]) => `<section><h4>${title}</h4><ul>${(summary[key] || []).map(t => `<li>${evidenceText(t)}</li>`).join('') || '<li>本轮未列出</li>'}</ul></section>`).join('')}</div>
    ${(summary.gaps || []).length ? `<details data-detail-key="summary-gaps"><summary>待补充的问题 · ${summary.gaps.length} 项</summary><ul class="prose-list">${summary.gaps.map(t => `<li>${evidenceText(t)}</li>`).join('')}</ul></details>` : ''}
    ${(summary.next_questions || []).length ? `<div class="next-directions"><h4>下一轮可以继续追问</h4>${summary.next_questions.map((q,i) => `<button class="direction-button" data-next-question="${i}"><span>${esc(q)}</span><span aria-hidden="true">↗</span></button>`).join('')}</div>` : ''}
    <footer><span class="dot"></span>${complete ? '等待你的指导' : '请检查本轮执行状态'}<small>你可以提出质疑、继续深入，或使用已完成轮次生成报告。</small></footer></article>`;
}
