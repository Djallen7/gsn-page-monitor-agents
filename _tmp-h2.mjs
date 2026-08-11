import { chromium, webkit } from 'playwright';
for (const [e,eng] of [['chromium',chromium],['webkit',webkit]]) {
  const b=await eng.launch();
  for (const vp of [{width:1280,height:900,n:'desktop'},{width:390,height:844,n:'mobile'}]) {
    for (const [n,u] of [['stations','https://genesissciencenetwork.com/stations/'],
                         ['mg-intern','https://genesissciencenetwork.com/motiongraphics-internship/'],
                         ['mg-slash','https://genesissciencenetwork.com/motiongraphics/internship']]) {
      const p=await b.newPage({viewport:{width:vp.width,height:vp.height}});
      await p.goto(u,{waitUntil:'load'}); await p.waitForTimeout(3000);
      const off=await p.evaluate(()=>{const f=document.querySelector('iframe');return Math.round(f.getBoundingClientRect().top);});
      const fr=p.frames().find(f=>f!==p.mainFrame());
      const c=await fr.evaluate(()=>{const h=document.getElementById('gsn-moved-notice');
        const s=h.shadowRoot; const card=s.querySelector('.card').getBoundingClientRect();
        const cta=s.querySelector('a.cta').getBoundingClientRect();
        return {cardTop:Math.round(card.top),ctaBottom:Math.round(cta.bottom)};}).catch(()=>null);
      const ctaAbs = c? off+c.ctaBottom : null;
      console.log(`${ctaAbs!==null&&ctaAbs<vp.height?'PASS':'FAIL'} ${e}/${vp.n} ${n.padEnd(10)} cardTop=${c&&c.cardTop} ctaBottomOnScreen=${ctaAbs} (window ${vp.height})`);
      await p.close();
    }
  }
  await b.close();
}
