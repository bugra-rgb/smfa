"""Same-origin panel extension. External assets; no inline scripts or user HTML."""
HTML='''<!doctype html><html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Hashtag havuzları · SMF</title><link rel="stylesheet" href="/api/control/hashtags/panel.css"><script src="/api/control/hashtags/panel.js" defer></script></head><body>
<header><a href="/" class="brand">SMF <span>Social Media Fabrica</span></a><a href="/#ideas" class="back">← Ana panele dön</a></header>
<main><div class="intro"><div><p class="eyebrow">TÜM PLATFORMLAR · HESABA ÖZEL ETİKETLER</p><h1>Hashtag havuzları</h1><p>Her paylaşımda hesabın havuzundan 3 etiket seçilir.<br>Sonuna <strong>#fyp #benionecıkart</strong> eklenir. Toplam 5 hashtag.</p></div><span class="formula">3 + 2 <small>= 5 hashtag</small></span></div>
<div id="status" role="status" aria-live="polite">Hesaplar yükleniyor…</div>
<section id="login" class="card" hidden><h2>Panel oturumunu aç</h2><p>Havuzları düzenlemek için mevcut SMF hesabınla giriş yap. Ardından bu sayfaya dön.</p><a class="primary" href="/">Panele giriş yap</a></section>
<div class="layout" id="workspace" hidden><aside class="card"><label for="search">Hesap ara</label><input id="search" type="search" placeholder="Hesap adı veya kullanıcı adı"><label for="platform">Platform</label><select id="platform"><option value="all">Tüm platformlar</option><option value="instagram">Instagram</option><option value="youtube">YouTube</option><option value="tiktok">TikTok</option><option value="facebook">Facebook</option><option value="x">X</option></select><div id="accounts" class="accounts" aria-label="Hesaplar"></div></aside>
<section class="card editor"><div class="account-head"><div><p id="account-platform" class="eyebrow"></p><h2 id="account-name">Hesap seç</h2></div><span id="account-state" class="badge"></span></div>
<form id="editor"><label for="new-tags">Havuza etiket ekle</label><div class="add-row"><input id="new-tags" autocomplete="off" placeholder="#LimonMaceralari #SariNese"><button id="add" type="button">Ekle</button></div><p class="hint">Bir veya birden fazla etiket yazabilirsin. Boşluk, virgül veya yeni satırla ayır; # işareti otomatik eklenir.</p>
<div class="pool-heading"><h3>Konu etiketleri</h3><span id="count"></span></div><div id="tags" class="chips"></div><p class="hint">En az 3 farklı konu etiketi gerekli. Bir etiketi çıkarmak için yanındaki × düğmesine bas.</p>
<h3>Her paylaşımın sonuna eklenenler</h3><div class="chips fixed"><span>#fyp</span><span>#benionecıkart</span></div>
<div class="preview"><p class="eyebrow">PAYLAŞIM ÖRNEĞİ · 5 HASHTAG</p><p id="preview"></p></div>
<div id="validation" role="alert"></div><div class="actions"><button id="save" class="primary" type="submit">Havuzu kaydet</button><button id="reset" type="button">Değişiklikleri geri al</button></div><p class="hint">Kaydettiklerin henüz gönderimi başlamamış paylaşımlarda kullanılır. Yayınlanmış gönderilerin metni değişmez.</p></form></section></div></main></body></html>'''

CSS='''*{box-sizing:border-box}body{margin:0;background:#111519;color:#edf1f5;font:15px/1.6 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}a{color:inherit;text-decoration:none}button,input,select{font:inherit}button,a.primary{cursor:pointer}button:disabled{cursor:not-allowed;opacity:.45}header{max-width:1280px;margin:auto;display:flex;align-items:center;justify-content:space-between;padding:24px 30px;border-bottom:1px solid #30373c}.brand{font-size:24px;font-weight:800;letter-spacing:-1px}.brand span{display:block;font-size:11px;color:#a1afb9;letter-spacing:0;font-weight:500}.back{color:#acbac4;font-size:13px}main{max-width:1280px;padding:36px 30px 70px;margin:auto}.intro{display:flex;justify-content:space-between;gap:24px;align-items:center;margin-bottom:22px}h1{font-size:36px;letter-spacing:-1px;line-height:1.2;margin:8px 0 12px}h2{font-size:23px;line-height:1.3;margin:4px 0}h3{font-size:14px;margin:24px 0 12px}.intro p{color:#b1bec7}.eyebrow{font-size:11px;letter-spacing:1.4px;color:#9ce0d1!important;font-weight:700;margin:0}.formula{border:1px solid #34574f;background:#172923;border-radius:18px;padding:22px 30px;font-size:36px;white-space:nowrap;font-weight:800;color:#b1f1df}.formula small{display:block;font-size:12px;color:#b0c9c1;font-weight:500}.layout{display:grid;grid-template-columns:310px 1fr;gap:22px;align-items:start}.card{background:#191f24;border:1px solid #30383e;border-radius:18px;padding:24px}.accounts{display:grid;gap:8px;margin-top:18px;max-height:580px;overflow:auto}.account{display:block;text-align:left;background:#1d252b;border:1px solid transparent;padding:12px;color:#edf1f5;border-radius:10px;width:100%}.account small{display:block;color:#9aabb5;font-size:11px}.account.selected{border-color:#73d4b4;background:#203a32}label{display:block;font-size:13px;font-weight:600;margin:0 0 7px}input,select{width:100%;background:#10161b;border:1px solid #3b4650;color:#edf1f5;padding:11px 12px;border-radius:9px;outline:none}input:focus,select:focus,button:focus-visible,a:focus-visible{outline:2px solid #8be0c6;outline-offset:3px}select{margin-bottom:2px}#search{margin-bottom:16px}.account-head{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:28px}.badge{color:#b2c3ce;background:#283039;border-radius:20px;padding:5px 10px;font-size:11px;white-space:nowrap}.add-row{display:flex;gap:10px}button{padding:10px 15px;color:#e7eef2;background:#2b353d;border:1px solid #46535d;border-radius:9px}.primary{background:#adf0d2;border:1px solid #adf0d2;color:#14241d;font-weight:750;padding:11px 18px;display:inline-block;border-radius:9px}.hint{color:#91a4b1;font-size:12px;margin:9px 0 16px}.chips{display:flex;flex-wrap:wrap;gap:9px;min-height:35px}.chips>span{display:inline-flex;gap:9px;align-items:center;border:1px solid #3c4a53;background:#212c33;color:#e7f0f4;padding:5px 9px;border-radius:8px;overflow-wrap:anywhere;max-width:100%}.chips button{border:0;padding:0 3px;line-height:1.3;color:#a2bac7;background:none}.fixed>span{border-color:#396858;background:#1b352b;color:#aaf1d1}.pool-heading{display:flex;justify-content:space-between;align-items:center}.pool-heading span{color:#a6b7c3;font-size:12px}.preview{margin:26px 0 20px;padding:20px;background:#10171b;border:1px solid #34434b;border-radius:12px}.preview p:last-child{margin:12px 0 0;font-size:17px;overflow-wrap:anywhere;color:#d9f6e9}.actions{display:flex;gap:12px;margin-top:18px;flex-wrap:wrap}#status{margin:0 0 18px;color:#b9efda}#validation{color:#ffb4a8;font-size:13px}#status.error{color:#ffb4a8}[hidden]{display:none!important}@media(max-width:760px){header{padding:18px}.brand{font-size:22px}main{padding:24px 16px}.layout{grid-template-columns:1fr}.accounts{max-height:230px}.intro{align-items:flex-start}h1{font-size:29px}.formula{font-size:26px;padding:15px}.formula small{font-size:10px}.card{padding:18px}.account-head{align-items:flex-start}.badge{white-space:normal}.add-row input{min-width:0}.intro br{display:none}}'''

JS=r'''"use strict";
(()=>{
const base='/api/control/hashtags',fixed=['#fyp','#benionecıkart'],names={instagram:'Instagram',youtube:'YouTube',tiktok:'TikTok',facebook:'Facebook',x:'X'};
const $=id=>document.getElementById(id);
let all=[],selected=null,draft=[],busy=false,dirty=false,canEdit=false;
const fold=v=>v.normalize('NFC').toLowerCase();
function status(text,error=false){$('status').textContent=text;$('status').className=error?'error':'';}
async function api(path,method='GET',body){
 let headers={'Accept':'application/json'};
 if(method!=='GET'){
  const r=await fetch('/api/auth/csrf',{credentials:'same-origin',cache:'no-store'});
  if(!r.ok)throw new Error('Oturum doğrulanamadı. Panele yeniden giriş yap.');
  const c=await r.json();headers['X-CSRF-Token']=c.csrf_token;headers['Content-Type']='application/json';
 }
 const r=await fetch(base+path,{method,headers,credentials:'same-origin',cache:'no-store',body:body?JSON.stringify(body):undefined});
 if(r.status===401){$('login').hidden=false;$('workspace').hidden=true;throw new Error('Panel oturumu gerekli.');}
 let data;try{data=await r.json();}catch{throw new Error('Sunucu yanıtı okunamadı. Güncellemenin tamamlandığını kontrol et.');}
 if(!r.ok)throw new Error(typeof data.detail==='string'?data.detail:'Etiketler kaydedilemedi; girilen değerleri kontrol et.');
 return data;
}
function validTag(t){return /^#[\p{L}\p{N}_]{2,40}$/u.test(t);}
function example(){return [...draft].sort((a,b)=>a.length-b.length).slice(0,3).concat(fixed).join(' ');}
function validation(){
 if(draft.length<3)return 'Sabit etiketler dışında en az 3 farklı konu etiketi ekle.';
 if(draft.length>98)return 'Havuza en fazla 98 konu etiketi eklenebilir.';
 if(draft.some(t=>!validTag(t)))return 'Etiketlerde 2–40 harf, rakam veya alt çizgi kullan.';
 if(example().length>100)return 'Üç konu etiketi ve sabit etiketler 100 karaktere sığmalı. Daha kısa etiketler ekle.';
 return '';
}
function setDirty(){dirty=JSON.stringify(draft)!==JSON.stringify(selected?.tags||[]);}
function renderEditor(){
 $('tags').replaceChildren();
 for(const [i,t] of draft.entries()){
  const chip=document.createElement('span'),label=document.createElement('span'),remove=document.createElement('button');
  label.textContent=t;remove.type='button';remove.textContent='×';remove.setAttribute('aria-label',t+' etiketini çıkar');remove.disabled=busy||!canEdit;
  remove.addEventListener('click',()=>{draft.splice(i,1);setDirty();renderEditor();});chip.append(label,remove);$('tags').append(chip);
 }
 $('count').textContent=draft.length+' konu etiketi';$('preview').textContent=draft.length>=3?example():'En az 3 konu etiketi ekle.';
 $('validation').textContent=validation();$('save').disabled=busy||!canEdit||!!validation()||!dirty;
 $('reset').disabled=busy||!dirty;$('add').disabled=busy||!canEdit;$('new-tags').disabled=busy||!canEdit;
 $('save').textContent=busy?'Kaydediliyor…':'Havuzu kaydet';
}
function select(a){
 if(busy)return;
 if(dirty&&!confirm('Kaydedilmemiş etiket değişikliklerini bırakıp diğer hesaba geçilsin mi?'))return;
 selected=a;draft=[...a.tags];dirty=false;$('new-tags').value='';
 $('account-name').textContent=a.username?'@'+a.username.replace(/^@/,''):a.name;
 $('account-platform').textContent=a.platforms.map(p=>names[p]||p).join(' · ');
 $('account-state').textContent=a.active&&a.auto_publish?'Otomatik yayın açık':'Otomatik yayın kapalı';
 renderList();renderEditor();
}
function renderList(){
 const q=fold($('search').value),p=$('platform').value;$('accounts').replaceChildren();
 const visible=all.filter(a=>(p==='all'||a.platforms.includes(p))&&fold((a.username||'')+' '+a.name).includes(q));
 for(const a of visible){
  const button=document.createElement('button'),label=document.createElement('strong'),sub=document.createElement('small');
  button.type='button';button.className='account'+(selected?.id===a.id?' selected':'');button.setAttribute('aria-pressed',String(selected?.id===a.id));
  label.textContent=a.username?'@'+a.username.replace(/^@/,''):a.name;sub.textContent=a.platforms.map(p=>names[p]||p).join(' · ');
  button.append(label,sub);button.addEventListener('click',()=>select(a));$('accounts').append(button);
 }
 if(!visible.length){const text=document.createElement('p');text.textContent='Bu aramada hesap bulunamadı.';$('accounts').append(text);}
}
function add(){
 if(busy||!canEdit)return;const values=$('new-tags').value.split(/[\s,;]+/u).filter(Boolean),seen=new Set(draft.map(fold));
 const next=[];
 for(let v of values){v=v.normalize('NFC');if(!v.startsWith('#'))v='#'+v;
  if(!validTag(v)){status('Geçersiz etiket: '+v+' — 2–40 harf, rakam veya alt çizgi kullan.',true);return;}
  if(fixed.map(fold).includes(fold(v))||seen.has(fold(v)))continue;seen.add(fold(v));next.push(v);
 }
 if(draft.length+next.length>98){status('En fazla 98 konu etiketi kaydedebilirsin.',true);return;}
 draft.push(...next);$('new-tags').value='';setDirty();renderEditor();status(next.length?'Etiketler taslağa eklendi. Kalıcı olması için Havuzu kaydet düğmesine bas.':'Bu etiketler zaten havuzda veya sabit etiketler arasında.');
}
$('add').addEventListener('click',add);$('new-tags').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();add();}});
$('search').addEventListener('input',renderList);$('platform').addEventListener('change',renderList);
$('reset').addEventListener('click',()=>{draft=[...selected.tags];dirty=false;$('new-tags').value='';renderEditor();status('Kaydedilmemiş değişiklikler geri alındı.');});
$('editor').addEventListener('submit',async e=>{
 e.preventDefault();if(!selected||busy||!canEdit)return;
 if($('new-tags').value.trim()){add();return;}
 if(validation()){status(validation(),true);return;}
 const id=selected.id;busy=true;renderEditor();
 try{const result=await api('/accounts/'+encodeURIComponent(id),'PUT',{tags:draft,expected_revision:selected.revision});
  const index=all.findIndex(a=>a.id===id);all[index]=result.account;selected=result.account;draft=[...selected.tags];dirty=false;status('Hashtag havuzu kaydedildi. Yeni paylaşımlar 3 konu etiketi + #fyp #benionecıkart kullanacak.');
 }catch(e){status(e.message,true);}finally{busy=false;renderList();renderEditor();}
});
window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
api('/accounts').then(data=>{all=data.accounts;canEdit=data.can_edit;$('workspace').hidden=false;$('login').hidden=true;
 status(canEdit?all.length+' hesap yüklendi. Düzenlemek istediğin hesabı seç.':'Havuzları görüntüleyebilirsin. Değişiklik için yönetici veya operatör yetkisi gerekli.');
 if(all.length)select(all[0]);else{$('editor').hidden=true;renderList();}
}).catch(e=>status(e.message,true));
})();'''
