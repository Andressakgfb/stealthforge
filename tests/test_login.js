const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');

(async () => {
  const browser = await chromium.launch({headless: true, executablePath: process.env.AUDIT_CHROMIUM});
  try {
    const page = await browser.newPage();
    let submissions = 0;
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/api/account/login') {
        submissions++;
        return route.fulfill({status: 401, json: {detail: 'E-mail ou senha incorretos.'}});
      }
      const file = path.join(__dirname, '../public', path.basename(url.pathname));
      if (!fs.existsSync(file)) return route.abort();
      return route.fulfill({path: file});
    });
    await page.goto('https://audit.invalid/login.html');
    await page.getByRole('tab', {name: 'Criar conta'}).click();
    await page.getByLabel('Seu nome').fill('A');
    await page.getByLabel('E-mail', {exact: true}).fill('audit@example.invalid');
    await page.getByLabel('Senha', {exact: true}).fill('test-only-password');
    await page.getByRole('tab', {name: 'Entrar', exact: true}).click();
    await page.getByRole('button', {name: 'Entrar', exact: true}).click();
    await page.getByRole('alert').filter({hasText: 'E-mail ou senha incorretos.'}).waitFor();
    assert.equal(submissions, 1, 'Nome oculto não pode bloquear o login');
    await page.getByRole('tab', {name: 'Criar conta'}).click();
    assert.equal(await page.getByLabel('Seu nome').isEnabled(), true);
    assert.equal(await page.locator('#account-form').evaluate(form => form.checkValidity()), false);
    assert.equal(submissions, 1, 'Cadastro ainda deve validar o nome curto');
    console.log('PASS: login após cadastro incompleto; validação do cadastro preservada.');
    if (process.env.AUDIT_SCREENSHOTS) {
      await page.reload();
      for (const [name, width, height] of [['desktop',1280,800],['tablet',768,1024],['mobile',390,844]]) {
        await page.setViewportSize({width,height});
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
        await page.screenshot({path:path.join(process.env.AUDIT_SCREENSHOTS, `login-${name}-local.png`),fullPage:true});
      }
      console.log('PASS: sem rolagem horizontal em 1280, 768 e 390 pixels.');
    }
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
