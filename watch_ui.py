# -*- coding: utf-8 -*-
"""
結果ページのウォッチリスト（画面部分）。
銘柄リストと取得単価・株数は、見ている端末のブラウザ（localStorage）にだけ保存し、どこにも送らない。
表示するデータは毎日の全銘柄データ docs/data/all.json（公開情報のみ）から読む。
"""

CSS = """
.star{border:0;background:none;color:var(--mut);font-size:16px;line-height:1;padding:0 4px 0 0;cursor:pointer;vertical-align:-1px}
.star.on{color:#d97706}
.wctl{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}
.wctl input{font:inherit;padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg);width:9em}
.wctl button,.wbtn{font:inherit;padding:7px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg);cursor:pointer}
.wbtn{padding:2px 8px;font-size:12px}
.bdg{display:inline-block;padding:0 6px;margin:0 2px 0 0;border-radius:6px;background:var(--line);font-size:12px;font-weight:600}
"""

HTML = """
<h2 id="w">★ ウォッチリスト <span id="wn" class="sub"></span></h2>
<div class="wctl">
  <input id="wi" placeholder="コード 例 7203" inputmode="text" autocomplete="off" onkeydown="if(event.key==='Enter')wAdd()">
  <button onclick="wAdd()">追加</button>
  <button onclick="wBulk()">まとめて追加</button>
  <button onclick="wCopy()">コード一覧をコピー</button>
</div>
<p class="note">各表の銘柄名の左の☆でも追加できます。リストと取得単価は、この端末のブラウザの中だけに保存されます（公開されません。別の端末とは共有されません）。
売買代金の条件に関係なく、全銘柄から表示します。「取得単価」を入れると損益を表示。線や安値までの%がオレンジ＝終値がその下（手仕舞いの目安を割っている）。<span id="wdate"></span></p>
<div id="wt"></div>
"""

JS = r"""
const WK='kabu_watch_v1';let WD=null,WDATE='';
function wl(){try{const a=JSON.parse(localStorage.getItem(WK)||'[]');return Array.isArray(a)?a:[]}catch(e){return []}}
function ws(a){try{localStorage.setItem(WK,JSON.stringify(a))}catch(e){alert('この端末では保存できません（プライベートブラウズなど）')}wRender()}
function wNorm(s){s=String(s||'').trim().toUpperCase().replace(/[０-９Ａ-Ｚ]/g,c=>String.fromCharCode(c.charCodeAt(0)-0xFEE0));return /^[0-9][0-9A-Z]{3}$/.test(s)?s:null}
function wHas(c){return wl().some(x=>x.c===c)}
function tw(b){const c=b.dataset.c;const a=wl();const i=a.findIndex(x=>x.c===c);if(i>=0)a.splice(i,1);else a.push({c});ws(a)}
function wAdd(){const el=document.getElementById('wi');const c=wNorm(el.value);if(!c){alert('4桁のコードを入れてください');return}
  const a=wl();if(!a.some(x=>x.c===c))a.push({c});el.value='';ws(a)}
function wDel(c){if(!confirm(c+' をウォッチから外しますか？'))return;ws(wl().filter(x=>x.c!==c))}
function wCost(c){const a=wl();const it=a.find(x=>x.c===c);if(!it)return;
  const v=prompt('取得単価（空欄で消去）',it.cost??'');if(v===null)return;
  const cost=parseFloat(String(v).replace(/,/g,''));if(isNaN(cost)){delete it.cost;delete it.qty;ws(a);return}
  it.cost=cost;const q=prompt('株数（省略可）',it.qty??'');const qty=parseFloat(String(q||'').replace(/,/g,''));
  if(!isNaN(qty))it.qty=qty;else delete it.qty;ws(a)}
function wBulk(){const v=prompt('コードをスペースか改行で区切って入力（取得単価は 7203@2500 の形で付けられます）');if(!v)return;
  const a=wl();v.split(/[\s,、]+/).forEach(t=>{const [cc,pp]=t.split('@');const c=wNorm(cc);if(!c)return;
    let it=a.find(x=>x.c===c);if(!it){it={c};a.push(it)}const p=parseFloat(pp);if(!isNaN(p))it.cost=p});ws(a)}
function wCopy(){const t=wl().map(x=>x.cost!=null?x.c+'@'+x.cost:x.c).join(' ');if(!t){alert('ウォッチが空です');return}
  const done=()=>alert('コピーしました:\n'+t+'\n\nメールにも載せる場合は、GitHub の Secrets「WATCHLIST」にこの内容を貼り付けてください。');
  if(navigator.clipboard)navigator.clipboard.writeText(t).then(done,()=>prompt('コピーしてください',t));else prompt('コピーしてください',t)}
function f1(v,nd=1){return v==null||isNaN(v)?'-':Number(v).toLocaleString('ja-JP',{minimumFractionDigits:nd,maximumFractionDigits:nd})}
function pc(v){return v==null||isNaN(v)?'-':(v>=0?'+':'')+v.toFixed(1)+'%'}
function E(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function wLine(p,ref,label){if(ref==null)return '<td data-v="">-</td>';const d=(p/ref-1)*100;
  return `<td data-v="${d}"><span class="${d<0?'warn':''}">${pc(d)}</span><div class="sub">${f1(ref,0)}</div></td>`}
function wSpark(sp,line){if(!sp||sp.length<2)return '';const w=120,h=32,vals=sp.filter(v=>v!=null);
  const all=line?vals.concat([line]):vals,lo=Math.min(...all),hi=Math.max(...all),rg=(hi-lo)||1;
  const pts=sp.map((v,i)=>`${(i*w/(sp.length-1)).toFixed(1)},${(h-(v-lo)/rg*h).toFixed(1)}`).join(' ');
  const ref=line?`<line x1="0" x2="${w}" y1="${(h-(line-lo)/rg*h).toFixed(1)}" y2="${(h-(line-lo)/rg*h).toFixed(1)}" stroke="#d97706" stroke-dasharray="3 2"/>`:'';
  const col=sp[sp.length-1]>=sp[0]?'#e5484d':'#30a46c';
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">${ref}<polyline fill="none" stroke="${col}" stroke-width="1.5" points="${pts}"/></svg>`}
function wRender(){const a=wl();document.querySelectorAll('.star').forEach(b=>{const on=a.some(x=>x.c===b.dataset.c);b.textContent=on?'★':'☆';b.classList.toggle('on',on)});
  document.getElementById('wn').textContent=a.length?a.length+'銘柄':'';
  const tl=document.getElementById('wtab');if(tl)tl.textContent='★ ウォッチ '+a.length;
  document.getElementById('wdate').textContent=WDATE?'（データ '+WDATE+'）':'';
  const box=document.getElementById('wt');
  if(!a.length){box.innerHTML='<p class="empty">まだありません。上の欄にコードを入れるか、各表の☆を押してください。</p>';return}
  if(!WD){box.innerHTML='<p class="empty">データを読み込み中…</p>';return}
  const head=['銘柄','終値','前日比','リスト','25日線','50日線','10日安値','上値','信用倍率','買残','年高まで','取得単価','60日'];
  const rows=a.map(it=>{const x=WD[it.c];
    if(!x)return `<tr><td data-v="${it.c}">${it.c}<div class="sub">データなし <button class="wbtn" onclick="wDel('${it.c}')">外す</button></div></td>`+'<td>-</td>'.repeat(head.length-1)+'</tr>';
    const p=x.p,L=(x.L||'').split('').map(k=>`<span class="bdg">${k}${k==='D'&&x.dt?' '+x.dt:''}</span>`).join('')||'-';
    const tv='https://jp.tradingview.com/chart/?symbol=TSE:'+x.c;
    let cost='<td data-v=""><button class="wbtn" onclick="wCost(\''+x.c+'\')">入力</button></td>';
    if(it.cost!=null){const g=(p/it.cost-1)*100,amt=it.qty?(p-it.cost)*it.qty:null;
      cost=`<td data-v="${g}"><span class="${g>=0?'up':'dn'}">${pc(g)}</span>${amt!=null?` <span class="sub">${amt>=0?'+':''}${f1(amt,0)}円</span>`:''}`+
        `<div class="sub">${f1(it.cost,1)}${it.qty?' × '+f1(it.qty,0):''} <button class="wbtn" onclick="wCost('${x.c}')">変更</button></div></td>`}
    const ur=x.res!=null?(x.res/p-1)*100:null,hy=x.hy!=null?(x.hy/p-1)*100:null;
    const mr=x.mr==null?(x.mb!=null?'<span class="sub">売残0</span>':'-'):`<span class="${x.mr>=5?'warn':''}">${x.mr.toFixed(2)}倍</span>`;
    const mw=x.mwb==null?'-':`<span class="${x.mw!=null&&x.mw>=10?'warn':''}">週 ${f1(x.mwb/1e4,1)}万${x.mw!=null?'（'+(x.mw>=0?'+':'')+x.mw+'%）':''}</span>`;
    return `<tr><td data-v="${x.c}"><a class="nm" href="https://shikiho.toyokeizai.net/stocks/${x.c}" target="_blank">${E(x.n)}</a>`+
      `<div class="sub"><a class="nm" href="${tv}" target="_blank">${x.c}</a>・${E((x.s||'').replace(/・/g,''))}・<a href="https://kabutan.jp/stock/chart?code=${x.c}" target="_blank">株探</a> <button class="wbtn" onclick="wDel('${x.c}')">外す</button></div></td>`+
      `<td data-v="${p}">${f1(p,1)}</td><td data-v="${x.ch}"><span class="${x.ch>=0?'up':'dn'}">${pc(x.ch)}</span></td>`+
      `<td data-v="${x.L||''}">${L}</td>`+wLine(p,x.m25)+wLine(p,x.m50)+wLine(p,x.l10)+
      `<td data-v="${ur??''}">${ur==null?'-':(ur>0?'あと '+ur.toFixed(1)+'%':'<span class="up">上抜け</span>')}<div class="sub">${f1(x.res,0)}</div></td>`+
      `<td data-v="${x.mr??''}">${mr}</td><td data-v="${x.mw??''}">${mw}</td>`+
      `<td data-v="${hy??''}">${hy==null?'-':(hy<=0?'<span class="up">更新</span>':`<span class="${hy<5?'warn':''}">${hy.toFixed(1)}%</span>`)}</td>`+
      cost+`<td><a class="spark" href="${tv}" target="_blank">${wSpark(x.sp,it.cost??x.m25)}</a></td></tr>`});
  box.innerHTML=`<div class="wrap"><table><thead><tr>${head.map(h=>`<th onclick="sortT(this)">${h}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`}
fetch('data/all.json',{cache:'no-cache'}).then(r=>r.json()).then(j=>{WD={};j.stocks.forEach(x=>WD[x.c]=x);WDATE=j.date;wRender()})
  .catch(()=>{document.getElementById('wt').innerHTML='<p class="empty">全銘柄データを読み込めませんでした。</p>'});
wRender();
"""


def star(code):
    return f'<button class="star" data-c="{code}" onclick="tw(this)" title="ウォッチに追加">☆</button>'
