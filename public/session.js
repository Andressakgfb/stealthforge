(() => {
  const create = (tag, className, text) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  };
  let root, bell, count, panel, list, person;
  let open = false;
  const labels = {pending: 'Aguardando liberação', approved: 'Acesso liberado', suspended: 'Acesso suspenso'};
  const icon = name => { const element = create('i'); element.dataset.lucide = name; element.setAttribute('aria-hidden', 'true'); return element; };
  const icons = () => window.lucide?.createIcons();

  async function refreshNotifications() {
    const response = await fetch('/api/account/notifications', {cache: 'no-store'});
    if (!response.ok) return;
    const data = await response.json();
    count.textContent = String(data.unread_count);
    count.hidden = !data.unread_count;
    bell.setAttribute('aria-label', `Notificações: ${data.unread_count} não lidas`);
    list.replaceChildren();
    if (!data.notifications.length) list.append(create('p', '', 'Nenhuma notificação por enquanto. Seus arquivos prontos aparecerão aqui.'));
    for (const item of data.notifications) {
      const entry = create('li', item.read_at ? '' : 'unread');
      entry.append(create('strong', '', item.title));
      const date = create('time', '', new Date(item.created_at * 1000).toLocaleString('pt-BR'));
      entry.append(date);
      if (typeof item.download_url === 'string' && item.download_url.startsWith('/api/download/')) {
        const link = create('a', '', 'Baixar arquivo'); link.href = item.download_url; link.setAttribute('download', ''); entry.append(link);
      }
      list.append(entry);
    }
  }
  window.sfRefreshNotifications = refreshNotifications;

  function renderCustomer(customer) {
    window.sfCustomer = customer;
    person.replaceChildren(create('strong', '', customer.name), create('small', '', customer.role === 'admin' ? 'Administração' : labels[customer.status]));
    let banner = document.getElementById('access-banner');
    if (customer.status === 'pending' && !banner) {
      banner = create('aside', 'access-banner'); banner.id = 'access-banner'; banner.setAttribute('role', 'status');
      banner.append(create('strong', '', 'Seu cadastro aguarda liberação.'), create('span', '', 'Conheça o painel e o estúdio. A geração e o processamento serão liberados após a aprovação da administração.'));
      document.querySelector('header').after(banner);
    }
    if (customer.status !== 'pending') banner?.remove();
    document.querySelectorAll('.btn-new').forEach(button => { button.disabled = customer.status !== 'approved'; });
    if (customer.role !== 'admin') {
      for (const action of ['createApiKey()', 'rotateDomain()', 'exportToGoogleSheets()']) {
        document.querySelector(`button[onclick="${action}"]`)?.closest('.stat-card')?.setAttribute('hidden', '');
      }
      document.getElementById('admin-key')?.parentElement.setAttribute('hidden', '');
    }
    document.dispatchEvent(new CustomEvent('sf:account', {detail: customer}));
  }

  window.sfAccountReady = (async () => {
    const response = await fetch('/api/account/me', {cache: 'no-store'});
    if (!response.ok) {
      location.replace('/login.html?next=' + encodeURIComponent(location.pathname));
      return null;
    }
    const customer = await response.json();
    root = document.getElementById('account-tools');
    if (!root) { root = create('div', 'account-tools'); root.id = 'account-tools'; document.querySelector('.header-actions')?.append(root); }
    if (!root.parentElement) throw new Error('Área da conta indisponível.');
    if (customer.role === 'admin') { const link = create('a', 'clients-link', 'Clientes'); link.href = '/clients.html'; root.append(link); }
    bell = create('button', 'account-bell'); bell.type = 'button'; bell.setAttribute('aria-label', 'Notificações'); bell.setAttribute('aria-expanded', 'false'); bell.setAttribute('aria-controls', 'notification-panel'); bell.append(icon('bell'));
    count = create('span', 'notification-count', '0'); count.hidden = true; bell.append(count);
    person = create('div', 'account-person');
    const exit = create('button', 'account-exit'); exit.type = 'button'; exit.title = 'Sair da conta'; exit.setAttribute('aria-label', 'Sair da conta'); exit.append(icon('log-out'));
    panel = create('section', 'notification-panel'); panel.id = 'notification-panel'; panel.hidden = true; panel.setAttribute('aria-label', 'Notificações'); panel.append(create('h2', '', 'Notificações'));
    list = create('ul'); panel.append(list);
    const read = create('button', '', 'Marcar como lidas'); read.type = 'button'; panel.append(read);
    root.append(bell, person, exit, panel);
    bell.addEventListener('click', () => { open = !open; panel.hidden = !open; bell.setAttribute('aria-expanded', String(open)); if (open) refreshNotifications().catch(() => {}); });
    document.addEventListener('keydown', event => { if (event.key === 'Escape') { open = false; panel.hidden = true; bell.setAttribute('aria-expanded', 'false'); } });
    document.addEventListener('click', event => { if (!root.contains(event.target)) { open = false; panel.hidden = true; bell.setAttribute('aria-expanded', 'false'); } });
    read.addEventListener('click', async () => { const response = await fetch('/api/account/notifications/read', {method: 'POST'}); if (response.ok) await refreshNotifications(); });
    exit.addEventListener('click', async () => { const response = await fetch('/api/account/logout', {method: 'POST'}); if (response.ok) location.replace('/login.html'); });
    renderCustomer(customer); icons();
    await refreshNotifications();
    setInterval(async () => {
      if (document.hidden) return;
      try {
        const response = await fetch('/api/account/me', {cache: 'no-store'});
        if (!response.ok) { location.replace('/login.html'); return; }
        renderCustomer(await response.json());
        await refreshNotifications();
      } catch { /* A próxima consulta retoma quando a conexão voltar. */ }
    }, 20000);
    return customer;
  })().catch(() => {
    const error = create('aside', 'access-banner', 'Não foi possível verificar sua conta. Atualize a página para tentar novamente.');
    error.setAttribute('role', 'alert'); document.querySelector('header')?.after(error); return null;
  });
})();
