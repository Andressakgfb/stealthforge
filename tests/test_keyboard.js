const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium, firefox, webkit } = require('playwright');

(async () => {
  let checks = 0;
  for (const [name, engine] of Object.entries({ chromium, firefox, webkit })) {
    const browser = await engine.launch();
    try {
      const page = await browser.newPage();
      const posts = [];
      page.setDefaultTimeout(5000);
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.route('**/*', async route => {
        const url = new URL(route.request().url());
        if (url.hostname !== 'stealthforge.test') return route.abort();
        if (url.pathname.startsWith('/api/')) {
          if (route.request().method() === 'POST') posts.push(route.request().postData());
          return route.fulfill({json: {recent_activities: [], status: 'success', filename: 'qa.mp4', download_url: '/api/download/qa.mp4', duration: '121 segundos', new_sha256: 'qa'}});
        }
        // The account script is outside these local UI tests; no live requests or credentials.
        if (url.pathname === '/session.js') return route.fulfill({contentType: 'text/javascript', body: ''});
        const relative = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
        const file = path.resolve(__dirname, '../public', relative);
        assert(file.startsWith(path.resolve(__dirname, '../public') + path.sep));
        if (!fs.existsSync(file)) return route.fulfill({status: 404, body: ''});
        return route.fulfill({path: file});
      });
      await page.goto('http://stealthforge.test/');
      const dialog = page.getByRole('dialog');
      const close = page.getByRole('button', {name: 'Fechar ferramenta', exact: true});
      for (const label of ['Preparar vídeo','Camuflador de imagem','Clonador de página','Camuflador de texto','Limpar metadados','Revisão de termos do anúncio','Cloaker Meta & TikTok','Modelos de roteiro']) {
        const trigger = page.getByRole('button', {name: new RegExp('^'+label)});
        await trigger.focus(); await page.keyboard.press('Enter');
        assert(await dialog.isVisible(),label);
        assert.equal(await close.evaluate(e=>e===document.activeElement),true);
        assert(await dialog.getAttribute('aria-labelledby'));
        await page.keyboard.press('Shift+Tab');
        assert(await dialog.evaluate(e=>e.contains(document.activeElement)),'Focus escaped '+label);
        await page.keyboard.press('Tab');
        assert.equal(await close.evaluate(e=>e===document.activeElement),true);
        await page.keyboard.press('Escape');
        await dialog.waitFor({state:'hidden'});
        assert.equal(await trigger.evaluate(e=>e===document.activeElement),true);
        assert.equal(await page.locator('body').evaluate(e=>e.classList.contains('modal-open')),false);
        checks++;
      }
      for (const [label,upload,id] of [
        ['Camuflador de imagem',/^Clique ou arraste uma imagem/,'file-input'],
        ['Limpar metadados',/^Arraste foto ou vídeo/,'file-input-m'],
        ['Preparar vídeo',/^Clique ou arraste seu vídeo/,'video-file-input'],
      ]) {
        console.log(`${name}: upload por teclado em ${label}`);
        const trigger=page.getByRole('button',{name:new RegExp('^'+label)});
        await trigger.click();const button=page.getByRole('button',{name:upload});await button.focus();
        const promise=page.waitForEvent('filechooser');await button.press('Enter');const chooser=await promise;
        await chooser.setFiles({name:'qa.png',mimeType:'image/png',buffer:Buffer.from('qa')});
        assert.equal(await page.locator('#'+id).evaluate(e=>e.files.length),1);
        await close.click();checks++;
      }
      await page.getByRole('button',{name:/^Preparar vídeo/}).click();
      await page.getByRole('button',{name:'📁 Upload Capa',exact:true}).click();
      const cover=page.getByRole('button',{name:/^Selecionar imagem de capa/});await cover.focus();
      const chooseCover=page.waitForEvent('filechooser');await page.keyboard.press('Space');
      await (await chooseCover).setFiles({name:'qa.png',mimeType:'image/png',buffer:Buffer.from('qa')});
      assert.equal(await page.locator('#cover-file-input').evaluate(e=>e.files.length),1);checks++;
      await close.click();
      const trigger=page.getByRole('button',{name:/^Preparar vídeo/});await trigger.click();
      await page.locator('#video-file-input').setInputFiles({name:'qa.mp4',mimeType:'video/mp4',buffer:Buffer.from('qa')});
      const toggle=page.getByRole('checkbox',{name:/Estender duração/});
      const minutes=page.getByRole('spinbutton',{name:'Tempo adicional (minutos)',exact:true});
      assert(!(await toggle.isChecked()));assert(!(await minutes.isVisible()));checks++;
      await toggle.check();await minutes.fill('2');
      const submit=page.getByRole('button',{name:'Camuflar Vídeo Agora',exact:true});
      let request=page.waitForRequest(r=>r.url().endsWith('/api/camouflage/video'));
      await submit.click();assert((await request).postData().includes('name="extension_minutes"\r\n\r\n2'));checks++;
      for(const value of ['0','31','1.5','']) {
        await minutes.fill(value);const before=posts.length;await submit.click();assert.equal(posts.length,before);assert.equal(await minutes.evaluate(e=>e.checkValidity()),false);checks++;
      }
      await toggle.uncheck();assert(await page.locator('#extension-minutes').isDisabled());
      request=page.waitForRequest(r=>r.url().endsWith('/api/camouflage/video'));
      await submit.click();const data=(await request).postData();
      assert(data.includes('name="timeline_padding"\r\n\r\nfalse'));assert(!data.includes('name="extension_minutes"'));checks++;
      await close.click();assert.equal(await trigger.evaluate(e=>e===document.activeElement),true);
      await trigger.click();assert.equal(await page.locator('#extension-minutes').inputValue(),'10');assert(!(await toggle.isChecked()));
      await page.mouse.click(2,2);await dialog.waitFor({state:'hidden'});checks++;
      assert.deepEqual(errors,[]);
      if(name==='chromium') {
        await trigger.click();await toggle.check();await minutes.fill('2');
        await page.screenshot({path:path.join(__dirname,'../keyboard-extension-preview.png')});
        await page.setViewportSize({width:390,height:844});
        assert(await minutes.isVisible());await page.screenshot({path:path.join(__dirname,'../keyboard-extension-mobile.png')});
      }
      console.log(`${name}: teclado, uploads e extensão aprovados.`);
    } finally {await browser.close();}
  }
  console.log(`${checks} cenarios de interface aprovados em tres navegadores.`);
})().catch(error=>{console.error(error);process.exitCode=1;});
