(() => {
  const states = {pending: 'Aguardando liberação', approved: 'Liberado', suspended: 'Suspenso'};
  const create = (tag, className, text) => { const element = document.createElement(tag); if (className) element.className = className; if (text !== undefined) element.textContent = text; return element; };
  const status = document.getElementById('clients-status');
  async function load() {
    const response = await fetch('/api/account/clients', {cache: 'no-store'});
    if (!response.ok) throw new Error('Não foi possível carregar os clientes. Verifique seu acesso administrativo.');
    const data = await response.json(); const list = document.getElementById('client-list'); list.replaceChildren();
    if (!data.clients.length) list.append(create('p', '', 'Nenhum cliente cadastrado por enquanto.'));
    for (const client of data.clients) {
      const card = create('article', 'client-card'); const info = create('div'); info.append(create('h2', '', client.name), create('p', '', client.email), create('p', '', 'Cadastro: ' + new Date(client.created_at * 1000).toLocaleString('pt-BR')));
      const actions = create('div', 'client-actions'); actions.append(create('span', 'client-state ' + client.status, states[client.status]));
      for (const [value, label] of [['approved', 'Liberar acesso'], ['suspended', 'Suspender']]) {
        if (client.status === value) continue;
        const button = create('button', 'portal-button ' + (value === 'suspended' ? 'secondary' : ''), label); button.type = 'button';
        button.addEventListener('click', async () => {
          button.disabled = true; status.textContent = '';
          try {
            const response = await fetch(`/api/account/clients/${client.id}/status`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({status: value})});
            if (!response.ok) throw new Error('Não foi possível atualizar o acesso.');
            await load(); status.textContent = value === 'approved' ? 'Acesso do cliente liberado.' : 'Acesso do cliente suspenso.';
          } catch (error) { status.textContent = error.message; button.disabled = false; }
        });
        actions.append(button);
      }
      card.append(info, actions); list.append(card);
    }
  }
  window.sfAccountReady.then(customer => { if (customer?.role === 'admin') return load(); }).catch(error => { status.textContent = error.message; });
})();
