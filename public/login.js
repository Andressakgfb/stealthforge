(() => {
  const byId = id => document.getElementById(id);
  let register = false;
  function mode(value) {
    register = value;
    byId('login-tab').setAttribute('aria-selected', String(!value)); byId('register-tab').setAttribute('aria-selected', String(value));
    byId('name-field').hidden = !value; byId('name').required = value; byId('name').disabled = !value;
    byId('password-hint').hidden = !value; byId('register-note').hidden = !value;
    byId('password').minLength = value ? 12 : 1; byId('password').autocomplete = value ? 'new-password' : 'current-password';
    byId('auth-title').textContent = value ? 'Conheça o StealthForge.' : 'Bem-vindo de volta.';
    byId('auth-description').textContent = value ? 'Crie sua conta para explorar o painel e o estúdio.' : 'Entre para acessar suas ferramentas e seus arquivos.';
    byId('account-submit').textContent = value ? 'Criar minha conta' : 'Entrar'; byId('account-error').textContent = '';
  }
  byId('login-tab').addEventListener('click', () => mode(false)); byId('register-tab').addEventListener('click', () => mode(true));
  byId('account-form').addEventListener('submit', async event => {
    event.preventDefault(); const button = byId('account-submit'); button.disabled = true; byId('account-error').textContent = '';
    const payload = {email: byId('email').value.trim(), password: byId('password').value};
    if (register) payload.name = byId('name').value.trim();
    try {
      const response = await fetch('/api/account/' + (register ? 'register' : 'login'), {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Confira os campos e use uma senha de pelo menos 12 caracteres.');
      byId('password').value = '';
      const next = new URLSearchParams(location.search).get('next');
      location.replace(['/studio.html', '/clients.html'].includes(next) ? next : '/');
    } catch (error) { byId('account-error').textContent = error instanceof TypeError ? 'Não foi possível conectar. Tente novamente.' : error.message; }
    finally { button.disabled = false; }
  });
})();
