const state = { templates: [], template: null, project: null, renders: [] };
const $ = (id) => document.getElementById(id);
async function api(path, options = {}) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json', ...(options.headers || {}) }, ...options });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}
function renderTemplates() {
  $('templates').innerHTML = state.templates.map(t => `<button class="card template ${state.template?.id===t.id?'active':''}" data-id="${t.id}"><h3>${t.name}</h3><p>${t.description}</p><p class="muted">${t.defaultOutput.width}×${t.defaultOutput.height}</p></button>`).join('');
  document.querySelectorAll('.template').forEach(btn => btn.onclick = () => selectTemplate(btn.dataset.id));
}
async function selectTemplate(id) {
  state.template = state.templates.find(t => t.id === id); renderTemplates();
  state.project = await api('/api/projects', { method:'POST', body: JSON.stringify({ templateId:id }) });
  $('fields').innerHTML = state.template.fields.map(f => `<label>${f.label}${f.type==='textarea'?`<textarea name="${f.key}" maxlength="${f.maxLength||''}" ${f.required?'required':''}></textarea>`:`<input name="${f.key}" maxlength="${f.maxLength||''}" ${f.required?'required':''}>`}</label>`).join('');
  $('assets').innerHTML = '<h3>Assets</h3>' + state.template.requiredAssets.map(a => `<label>${a.label}<input type="file" data-asset="${a.key}" accept="${a.accept||''}" ${a.multiple?'multiple':''}></label>`).join('');
  $('player').setAttribute('src', `/api/projects/${state.project.id}/preview`);
  $('player').setAttribute('width', state.template.defaultOutput.width); $('player').setAttribute('height', state.template.defaultOutput.height);
  document.querySelector('.preview').classList.toggle('vertical', state.template.defaultOutput.height > state.template.defaultOutput.width);
}
async function saveDraft() {
  const copy = Object.fromEntries(new FormData($('projectForm')).entries());
  const assets = [];
  for (const input of document.querySelectorAll('[data-asset]')) {
    for (const file of input.files) {
      const ticket = await api('/api/assets/upload-url', { method:'POST', body: JSON.stringify({ projectId: state.project.id, key: input.dataset.asset, filename: file.name, contentType: file.type }) });
      await fetch(ticket.uploadUrl, { method:'PUT', headers:{'Content-Type':file.type}, body:file });
      assets.push(ticket.asset);
    }
  }
  state.project = await api(`/api/projects/${state.project.id}`, { method:'PATCH', body: JSON.stringify({ copy, assets }) });
  $('player').setAttribute('src', `/api/projects/${state.project.id}/preview?ts=${Date.now()}`); $('status').textContent = 'Draft saved.';
}
async function refreshRender(id) { const r = await api(`/api/renders/${id}`); state.renders = [r, ...state.renders.filter(x=>x.id!==id)]; drawHistory(); return r; }
function drawHistory(){ $('history').innerHTML = state.renders.map(r => `<div class="row"><span>${r.templateId} · ${r.status}</span><span>${r.status==='failed'?`<button class="secondary" onclick="retry('${r.id}')">Retry</button>`:''}${r.downloadUrl?`<a href="${r.downloadUrl}">Download</a>`:''}</span></div>`).join(''); }
async function startRender(){ await saveDraft(); const r = await api(`/api/projects/${state.project.id}/render`, { method:'POST' }); state.renders.unshift(r); drawHistory(); $('status').textContent='Queued…'; const timer=setInterval(async()=>{ const latest=await refreshRender(r.id); $('status').textContent=latest.status; if(['complete','failed'].includes(latest.status)){ clearInterval(timer); if(latest.downloadUrl){ $('download').hidden=false; $('download').href=latest.downloadUrl; } } }, 2500); }
window.retry = async (id) => { const r = await api(`/api/renders/${id}/retry`, { method:'POST' }); await refreshRender(r.id); };
$('signin').onclick = async()=>{ try{ await api('/api/login',{method:'POST',body:JSON.stringify({password:$('password').value})}); $('login').hidden=true; $('studio').hidden=false; $('logout').hidden=false; state.templates=(await api('/api/templates')).templates; renderTemplates(); }catch(e){$('loginStatus').textContent=e.message;} };
$('logout').onclick=async()=>{await api('/api/logout',{method:'POST'}); location.reload();};
$('projectForm').onsubmit=async(e)=>{e.preventDefault(); await saveDraft();}; $('render').onclick=startRender;
