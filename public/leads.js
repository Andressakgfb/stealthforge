(() => {
  'use strict';
  const container = document.querySelector('#leads-container');
  const status = document.querySelector('#leads-status');
  const banner = document.querySelector('#lgpd-banner');
  const importButton = document.querySelector('#import-csv');
  const syncButton = document.querySelector('#sync-external');
  const fileInput = document.querySelector('#csv-file');
  const filters = [...document.querySelectorAll('select[data-filter]')];
  let leads = [], consent = null, busy = false, epoch = 0, pending = new Set();
  const controllers = new Set();
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  function read(key, fallback) {
    try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; }
  }
  function save(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); }
    catch { status.textContent = 'O navegador não permitiu salvar sua preferência neste dispositivo.'; }
  }
  const savedConsent = read('sf_lgpd_consent', null);
  consent = savedConsent === true ? true : savedConsent === false ? false : null;
  let favorites = read('sf_leads_favs', {});
  if (!favorites || typeof favorites !== 'object' || Array.isArray(favorites)) favorites = {};
  async function request(path, options = {}) {
    if (consent !== true) throw new Error('Autorize o uso de dados para acessar os leads.');
    const generation = epoch, controller = new AbortController();
    controllers.add(controller);
    const timeout = setTimeout(() => controller.abort(), 60000);
    try {
      const response = await fetch('/api/leads' + path, {
        ...options, credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
        headers: { ...options.headers, 'X-LGPD-Consent': 'true' }
      });
      if (generation !== epoch) throw new Error('Preferências alteradas.');
      if (response.status === 401) { location.assign('/login.html?next=%2Fleads.html'); throw new Error('Entre na sua conta.'); }
      const data = await response.json();
      if (generation !== epoch || consent !== true) throw new Error('Preferências alteradas.');
      if (!response.ok) {
        const detail = data.detail;
        const message = typeof detail === 'string' ? detail : detail?.message || data.error || 'Não foi possível concluir a operação.';
        throw new Error(message + (detail?.errors ? ' ' + detail.errors.join(' ') : ''));
      }
      return data;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('Operação interrompida ou tempo de espera excedido. Confira o resultado antes de repetir.');
      throw error;
    } finally { clearTimeout(timeout); controllers.delete(controller); }
  }
  function renderCards(items) {
    const fragment = document.createDocumentFragment();
    for (const lead of items) {
      const card = node('article', undefined, 'lead-card');
      card.classList.toggle('is-favorited', lead.is_favorite);
      const top = node('div', undefined, 'lead-card-heading');
      const favorite = node('button', lead.is_favorite ? '★' : '☆', 'lead-favorite');
      favorite.type = 'button'; favorite.dataset.favorite = lead.id;
      favorite.setAttribute('aria-label', `Favoritar ${lead.name}`);
      favorite.setAttribute('aria-pressed', String(lead.is_favorite)); favorite.disabled = pending.has(lead.id);
      top.append(node('h2', lead.name), favorite);
      card.append(top, node('p', lead.company, 'lead-company'), node('p', lead.role || 'Cargo não informado'), node('p', lead.email));
      const score = Math.max(0, Math.min(100, Number(lead.intent.score) || 0));
      card.append(node('p', `${lead.intent.stage} · Score ${score}/100`));
      const track = node('div', undefined, 'intent-track');
      track.setAttribute('role', 'meter'); track.setAttribute('aria-label', 'Propensão à compra');
      track.setAttribute('aria-valuemin', '0'); track.setAttribute('aria-valuemax', '100'); track.setAttribute('aria-valuenow', String(score));
      const bar = node('div', undefined, 'intent-bar'); bar.style.width = `${score}%`;
      bar.classList.add(score < 40 ? 'low' : score <= 70 ? 'medium' : 'high'); track.append(bar); card.append(track);
      const signals = node('ul');
      lead.intent.signals.slice(0, 3).forEach(signal => signals.append(node('li', signal)));
      card.append(signals);
      if (lead.intent.signals.length > 3) {
        const more = node('details'); more.title = lead.intent.signals.slice(3).join('\n');
        more.append(node('summary', `Mais ${lead.intent.signals.length - 3} sinais`));
        const rest = node('ul'); lead.intent.signals.slice(3).forEach(signal => rest.append(node('li', signal))); more.append(rest); card.append(more);
      }
      if (lead.technographics.length) card.append(node('p', lead.technographics.join(' · '), 'lead-meta'));
      const date = new Date(lead.intent.last_activity || NaN);
      const activity = node('time', Number.isNaN(date.getTime()) ? 'Sem atividade registrada' : `Última atividade: ${date.toLocaleString('pt-BR')}`, 'lead-meta');
      if (!Number.isNaN(date.getTime())) activity.dateTime = date.toISOString();
      card.append(activity, node('p', `${lead.firmographics.industry || 'Setor não informado'} · ${lead.source}`, 'lead-meta'));
      fragment.append(card);
    }
    container.replaceChildren(fragment);
  }
  function filterLeads() {
    const values = Object.fromEntries(filters.map(select => [select.dataset.filter, select.value]));
    const result = leads.filter(lead => (!values.intent || lead.intent.stage === values.intent) && (!values.source || lead.source === values.source) &&
      (!values.firmographic || (() => { const [key, value] = JSON.parse(values.firmographic); return lead.firmographics[key] === value; })()));
    renderCards(result);
    status.textContent = leads.length ? `${result.length} de ${leads.length} leads exibidos.` : 'Nenhum lead disponível. Importe um CSV ou sincronize uma fonte.';
  }
  function populateProfiles() {
    const select = filters[0], selected = select.value;
    select.replaceChildren(node('option', 'Todos os perfis')); select.firstChild.value = '';
    for (const [key, label] of [['industry', 'Setor'], ['company_size', 'Porte'], ['revenue', 'Receita']]) {
      const group = node('optgroup'); group.label = label;
      [...new Set(leads.map(lead => lead.firmographics[key]).filter(Boolean))].sort().forEach(value => {
        const option = node('option', `${label}: ${value}`); option.value = JSON.stringify([key, value]); group.append(option);
      });
      select.append(group);
    }
    if ([...select.options].some(option => option.value === selected)) select.value = selected;
  }
  async function fetchLeads() {
    const generation = epoch;
    const data = await request('');
    if (generation !== epoch || consent !== true) return;
    leads = data.leads;
    favorites = Object.fromEntries(leads.map(lead => [lead.id, lead.is_favorite])); save('sf_leads_favs', favorites);
    populateProfiles(); filterLeads();
    const stats = await request('/stats');
    if (generation !== epoch || consent !== true) return;
    document.querySelector('#leads-stats').textContent = `${stats.total} leads · Score médio ${stats.avg_score} · Awareness ${stats.by_stage.Awareness} · Consideration ${stats.by_stage.Consideration} · Decision ${stats.by_stage.Decision}`;
  }
  async function operation(action) {
    if (busy || consent !== true) return;
    const generation = epoch; busy = true; updateControls(); container.setAttribute('aria-busy', 'true');
    try { await action(); } catch (error) { if (generation === epoch) status.textContent = error.message; }
    finally { if (generation === epoch) { busy = false; updateControls(); container.setAttribute('aria-busy', 'false'); } }
  }
  function updateControls() {
    importButton.disabled = syncButton.disabled = busy || consent !== true;
    syncButton.querySelector('.spinner').hidden = !busy;
    filters.forEach(select => { select.disabled = busy || consent !== true; });
  }
  function importCSV() { fileInput.click(); }
  async function syncExternal() {
    status.textContent = 'Sincronizando dados…';
    const data = await request('/sync', {method: 'POST'});
    await fetchLeads(); status.textContent += ` ${data.imported} leads sincronizados.`;
  }
  container.addEventListener('click', async event => {
    const button = event.target.closest('button[data-favorite]');
    if (!button || consent !== true || busy || pending.has(button.dataset.favorite)) return;
    const id = button.dataset.favorite, generation = epoch; pending.add(id); button.disabled = true;
    let failure;
    try {
      const updated = await request(`/${encodeURIComponent(id)}/favorite`, {method: 'PATCH'});
      if (generation !== epoch) return;
      const lead = leads.find(item => item.id === id);
      if (lead) lead.is_favorite = updated.is_favorite;
      favorites[id] = updated.is_favorite; save('sf_leads_favs', favorites);
    } catch (error) { failure = error.message; }
    finally { pending.delete(id); if (generation === epoch && consent === true) { filterLeads(); if (failure) status.textContent = failure; } }
  });
  function handleCSVUpload() {
    const file = fileInput.files[0]; fileInput.value = ''; if (!file) return;
    operation(async () => {
      if (file.size > 5 * 1024 * 1024) throw new Error('O arquivo deve ter até 5 MB.');
      const form = new FormData(); form.append('file', file);
      const result = await request('/import', {method: 'POST', body: form});
      const errors = document.querySelector('#import-errors'); errors.replaceChildren();
      result.errors.forEach(error => errors.append(node('li', error)));
      errors.hidden = !result.errors.length;
      await fetchLeads(); status.textContent += ` ${result.success.length} importados; ${result.errors.length} erros.`;
    });
  }
  fileInput.addEventListener('change', handleCSVUpload);
  function chooseConsent(value) {
    epoch++; controllers.forEach(controller => controller.abort()); busy = false; pending.clear(); consent = value; save('sf_lgpd_consent', value); banner.hidden = true;
    leads = []; favorites = {}; save('sf_leads_favs', favorites); renderCards([]);
    document.querySelector('#leads-stats').textContent = ''; document.querySelector('#import-errors').hidden = true;
    updateControls();
    if (value) operation(fetchLeads);
    else status.textContent = 'Uso de dados recusado. Os leads não serão consultados neste dispositivo.';
  }
  document.querySelector('#lgpd-accept').addEventListener('click', () => chooseConsent(true));
  document.querySelector('#lgpd-decline').addEventListener('click', () => chooseConsent(false));
  document.querySelector('#lgpd-settings').addEventListener('click', () => { banner.hidden = false; document.querySelector('#lgpd-accept').focus(); });
  importButton.addEventListener('click', importCSV);
  syncButton.addEventListener('click', () => operation(syncExternal));
  filters.forEach(select => select.addEventListener('change', filterLeads));
  window.addEventListener('storage', event => { if (event.key === 'sf_lgpd_consent' || event.key === null) { const value = read('sf_lgpd_consent', null); if (value !== consent) chooseConsent(value === true); } });
  banner.hidden = consent !== null; updateControls();
  if (consent === true) operation(fetchLeads);
  else if (consent === false) status.textContent = 'Uso de dados recusado. Os leads não serão consultados neste dispositivo.';
  setInterval(() => { if (consent === true && !document.hidden && !pending.size) operation(fetchLeads); }, 5 * 60 * 1000);
})();
