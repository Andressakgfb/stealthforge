const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium, firefox, webkit} = require('playwright');

(async()=>{
  let checks=0;
  for(const [engineName,engine] of Object.entries({chromium,firefox,webkit})) {
    const browser=await engine.launch();
    try {
      for(const [width,zoom] of [[1365,1],[390,1],[1365,2]]) {
        const page=await browser.newPage({viewport:{width,height:900}});
        await page.route('**/*',async route=>{
          const url=new URL(route.request().url());
          if(url.hostname!=='stealthforge.test')return route.abort();
          if(url.pathname==='/api/account/me')return route.fulfill({json:{id:1,name:'Conta QA',role:'admin',status:'approved'}});
          if(url.pathname==='/api/account/notifications')return route.fulfill({json:{unread_count:24,notifications:Array.from({length:24},(_,i)=>({title:'Arquivo de teste '+i,created_at:1793121226,read_at:null,download_url:'/api/download/qa.txt'}))}});
          if(url.pathname.startsWith('/api/'))return route.fulfill({json:{processed_today:25,protected_files:25,plan_used:25,recent_activities:[]}});
          return route.fulfill({path:path.resolve(__dirname,'../public',url.pathname==='/'?'index.html':url.pathname.slice(1))});
        });
        await page.goto('http://stealthforge.test/');
        await page.evaluate(()=>window.sfAccountReady);
        await page.locator('body').evaluate((e,value)=>{e.style.zoom=String(value);},zoom);
        const bell=page.getByRole('button',{name:/^Notificações:/});
        const panel=page.locator('#notification-panel');
        await bell.click();await panel.waitFor({state:'visible'});
        const hits=await panel.evaluate(panel=>{
          const p=panel.getBoundingClientRect(),points=[];
          for(const card of document.querySelectorAll('.stats-grid .stat-card')) {
            const c=card.getBoundingClientRect();
            const left=Math.max(p.left,c.left,0),right=Math.min(p.right,c.right,innerWidth);
            const top=Math.max(p.top,c.top,0),bottom=Math.min(p.bottom,c.bottom,innerHeight);
            if(right-left>4&&bottom-top>4) {
              const x=(left+right)/2,y=(top+bottom)/2;
              points.push({x,y,inPanel:panel.contains(document.elementFromPoint(x,y))});
            }
          }
          return points;
        });
        assert(hits.length>0,`${engineName} ${width}px zoom ${zoom}: falta sobreposição representativa`);
        assert(hits.every(p=>p.inPanel),`${engineName} ${width}px zoom ${zoom}: cartão encobre notificações`);checks++;
        await page.keyboard.press('Escape');assert(await panel.isHidden());checks++;
        await bell.click();await page.getByRole('heading',{name:'Bem-vindo ao Painel'}).click();assert(await panel.isHidden());checks++;
        await page.getByRole('button',{name:/^Camuflador de imagem/}).click();
        const dialog=page.getByRole('dialog');await dialog.waitFor({state:'visible'});
        assert(await page.getByRole('button',{name:'Fechar ferramenta',exact:true}).evaluate(e=>{const r=e.getBoundingClientRect();return e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));}));checks++;
        if(engineName==='chromium'&&width===1365&&zoom===1) {
          await page.keyboard.press('Escape');await bell.click();
          await page.screenshot({path:path.join(__dirname,'../notification-fixed-preview.png')});
        }
        await page.close();
      }
      console.log(`${engineName}: notificações acima dos cartões, celular e zoom 200% aprovados.`);
    } finally {await browser.close();}
  }
  console.log(`${checks} verificações de sobreposição e abertura/fechamento aprovadas.`);
})().catch(e=>{console.error(e);process.exitCode=1;});
