const $=id=>document.getElementById(id);
let lastInbox=new Set(), lastOutbox=new Set(), latest=null, raw=false, initialized=false;
const readableSize=n=>n==null?'':n<1024?`${n} B`:n<1048576?`${(n/1024).toFixed(1)} KB`:`${(n/1048576).toFixed(1)} MB`;
function el(tag,cls,text){const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n}
function fileList(id,rows,old){let host=$(id);host.replaceChildren();host.classList.toggle('empty',!rows.length);if(!rows.length){host.textContent='No items yet';return;}for(const f of rows){const item=el('div','file'+(initialized&&!old.has(f.name)?' new':''));item.append(el('div','file-icon',f.kind==='folder'?'▣':'▤'));const d=el('div','file-details');d.append(el('div','file-title',f.name),el('div','file-meta',`${f.modified.replace('T','  ')}  ${readableSize(f.size)}`));item.append(d,el('span','folder-badge',f.kind==='folder'?'FOLDER':'FILE'));host.append(item)}}
// Render Markdown into DOM nodes (never innerHTML), so document content cannot inject HTML.
// Supports headings, paragraphs, nested lists, task lists, tables, blockquotes, code, rules, links and inline emphasis.
function inline(text){
  const frag=document.createDocumentFragment();
  const re=/(`[^`]+`)|(\*\*[^*]+\*\*|__[^_]+__)|(\*[^*\s][^*]*\*|_[^_\s][^_]*_)|(~~[^~]+~~)|(\[[^\]]+\]\([^)\s]+\))/;
  let rest=text;
  while(rest){
    const m=rest.match(re);
    if(!m){frag.append(rest);break}
    if(m.index)frag.append(rest.slice(0,m.index));
    const t=m[0];
    if(m[1])frag.append(el('code','',t.slice(1,-1)));
    else if(m[2]){const b=el('strong');b.append(inline(t.slice(2,-2)));frag.append(b)}
    else if(m[3]){const i=el('em');i.append(inline(t.slice(1,-1)));frag.append(i)}
    else if(m[4]){const s=el('del');s.append(inline(t.slice(2,-2)));frag.append(s)}
    else{const lm=t.match(/^\[([^\]]+)\]\(([^)\s]+)\)$/);if(/^https?:\/\//i.test(lm[2])){const a=el('a');a.href=lm[2];a.target='_blank';a.rel='noopener noreferrer';a.append(inline(lm[1]));frag.append(a)}else frag.append(lm[1])}
    rest=rest.slice(m.index+t.length);
  }
  return frag;
}
function splitRow(line){return line.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(c=>c.trim())}
function formatMarkdown(input){
  const host=document.createDocumentFragment();
  const lines=input.replace(/\r\n?/g,'\n').split('\n');
  const isList=l=>/^\s*([-*+]|\d+[.)])\s+/.test(l);
  const isTableSep=l=>/^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l);
  const isBlockStart=(l,next)=>/^\s*(#{1,6}\s|```|~~~|>|(-{3,}|\*{3,}|_{3,})\s*$)/.test(l)||isList(l)||(l.includes('|')&&next!==undefined&&isTableSep(next));
  let i=0;
  while(i<lines.length){
    const line=lines[i];
    if(!line.trim()){i++;continue}
    let m;
    if((m=line.match(/^\s*(```|~~~)/))){
      const fence=m[1],code=[];i++;
      while(i<lines.length&&!lines[i].trim().startsWith(fence))code.push(lines[i++]);
      i++;const pre=el('pre');pre.append(el('code','',code.join('\n')));host.append(pre);continue;
    }
    if((m=line.match(/^\s*(#{1,6})\s+(.*?)\s*#*\s*$/))){const h=el('h'+m[1].length);h.append(inline(m[2]));host.append(h);i++;continue}
    if(/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)){host.append(el('hr'));i++;continue}
    if(/^\s*>/.test(line)){
      const q=[];while(i<lines.length&&/^\s*>/.test(lines[i]))q.push(lines[i++].replace(/^\s*>\s?/,''));
      const bq=el('blockquote');bq.append(formatMarkdown(q.join('\n')));host.append(bq);continue;
    }
    if(line.includes('|')&&i+1<lines.length&&isTableSep(lines[i+1])){
      const head=splitRow(line),align=splitRow(lines[i+1]).map(c=>c.startsWith(':')&&c.endsWith(':')?'center':c.endsWith(':')?'right':'');
      i+=2;
      const wrap=el('div','table-wrap'),table=el('table'),thead=el('thead'),tr=el('tr');
      head.forEach((c,k)=>{const th=el('th');if(align[k])th.style.textAlign=align[k];th.append(inline(c));tr.append(th)});
      thead.append(tr);table.append(thead);const tbody=el('tbody');
      while(i<lines.length&&lines[i].includes('|')&&lines[i].trim()){
        const row=el('tr');splitRow(lines[i]).forEach((c,k)=>{const td=el('td');if(align[k])td.style.textAlign=align[k];td.append(inline(c));row.append(td)});tbody.append(row);i++;
      }
      table.append(tbody);wrap.append(table);host.append(wrap);continue;
    }
    if(isList(line)){
      // Stack of open lists keyed by indentation, so nested bullets render as nested lists.
      const stack=[];let lastLi=null;
      while(i<lines.length&&(isList(lines[i])||(lastLi&&lines[i].trim()&&/^\s{2,}/.test(lines[i])))){
        const l=lines[i];const lm=l.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
        if(!lm){lastLi.append(' ',inline(l.trim()));i++;continue}
        const indent=lm[1].replace(/\t/g,'    ').length,tag=/\d/.test(lm[2])?'ol':'ul';
        while(stack.length&&indent<stack[stack.length-1].indent)stack.pop();
        let top=stack[stack.length-1];
        if(!top||indent>top.indent||top.tag!==tag){
          let parent=null;
          if(top&&indent>top.indent)parent=lastLi;
          else if(top){stack.pop();parent=top.parent}
          const list=el(tag);
          if(tag==='ol'&&parseInt(lm[2])!==1)list.start=parseInt(lm[2]);
          (parent||host).append(list);
          top={indent,tag,list,parent};stack.push(top);
        }
        const li=el('li');let content=lm[3];
        const task=content.match(/^\[([ xX])\]\s+(.*)$/);
        if(task){li.className='task'+(task[1]!==' '?' done':'');li.append(el('span','check',task[1]!==' '?'✓':''));content=task[2]}
        li.append(inline(content));top.list.append(li);lastLi=li;i++;
      }
      continue;
    }
    const para=[];
    while(i<lines.length&&lines[i].trim()&&(!para.length||!isBlockStart(lines[i],lines[i+1])))para.push(lines[i++].trim());
    const p=el('p');para.forEach((t,k)=>{if(k)p.append(el('br'));p.append(inline(t))});host.append(p);
  }
  return host;
}
function renderResult(){const box=$('result');box.replaceChildren();box.classList.toggle('raw',raw);if(!latest){box.textContent='Waiting for the first completed document...';return}if(raw){box.textContent=latest.text;return}box.append(formatMarkdown(latest.text))}
function stageLabel(s){return ({waiting:'Waiting',detected:'Detected',stabilizing:'Syncing',processing:'Processing',completed:'Completed',failed:'Failed'})[s]||s}
function update(data){$('connection').textContent='Local engine online';$('inbox-count').textContent=data.counts.inbox;$('outbox-count').textContent=data.counts.outbox;$('last-update').textContent=new Date().toLocaleTimeString();const s=data.status.stage;$('state-label').textContent=stageLabel(s);$('state-folder').textContent=data.status.folder||'OneDrive watcher';const radar=$('radar');radar.className='radar'+(s==='processing'?' busy':s==='completed'?' done':'');$('pulse-title').textContent=s==='waiting'?'Scanning for new content':s==='processing'?'AI extraction in progress':s==='completed'?'Document complete':s==='failed'?'Processing error':`${stageLabel(s)} new content`;$('pulse-detail').textContent=data.status.error|| (data.status.folder?`Folder: ${data.status.folder}`:'Waiting for a new subfolder in OneDrive inbox...');for(const name of ['detected','stabilizing','processing','completed'])$('s-'+name).classList.toggle('active',name===s);fileList('inbox-items',data.inbox,lastInbox);fileList('outbox-items',data.outbox,lastOutbox);lastInbox=new Set(data.inbox.map(x=>x.name));lastOutbox=new Set(data.outbox.map(x=>x.name));initialized=true;const next=data.latest;if(next&&(!latest||next.modified!==latest.modified||next.name!==latest.name)){latest=next;$('result-info').textContent=`${next.folder} / ${next.name} · ${next.modified.replace('T',' ')}`;renderResult()}const logs=$('logs');logs.replaceChildren();for(const e of data.status.events){const row=el('div','log'+(e.stage==='failed'?' failed':''));row.append(el('time','',e.time.split('T')[1]),el('span','',`${stageLabel(e.stage)}${e.folder?' · '+e.folder:''}${e.error?' · '+e.error:''}`));logs.append(row)}if(!data.status.events.length)logs.textContent='Waiting for activity...'}
async function refresh(){try{const r=await fetch('/api/state',{cache:'no-store'});if(!r.ok)throw Error(`HTTP ${r.status}`);update(await r.json())}catch(err){$('connection').textContent='Connection lost';$('state-label').textContent='Offline';$('pulse-detail').textContent=String(err)}}
// Subtle 3D tilt that follows the pointer on metric cards (skipped for touch and reduced-motion users).
if(matchMedia('(hover: hover) and (prefers-reduced-motion: no-preference)').matches){
  for(const card of document.querySelectorAll('.tilt')){
    card.addEventListener('pointermove',e=>{const r=card.getBoundingClientRect();const x=(e.clientX-r.left)/r.width-.5,y=(e.clientY-r.top)/r.height-.5;card.style.transform=`perspective(700px) rotateX(${(-y*7).toFixed(2)}deg) rotateY(${(x*9).toFixed(2)}deg) translateY(-4px)`;card.style.setProperty('--gx',`${(x+.5)*100}%`);card.style.setProperty('--gy',`${(y+.5)*100}%`)});
    card.addEventListener('pointerleave',()=>{card.style.transform=''});
  }
}
$('view-formatted').onclick=()=>{raw=false;$('view-formatted').classList.add('selected');$('view-raw').classList.remove('selected');renderResult()};$('view-raw').onclick=()=>{raw=true;$('view-raw').classList.add('selected');$('view-formatted').classList.remove('selected');renderResult()};$('copy').onclick=async()=>{if(latest)try{await navigator.clipboard.writeText(latest.text)}catch(err){alert('Clipboard unavailable')}};$('download').onclick=()=>{if(!latest)return;const a=document.createElement('a');const u=URL.createObjectURL(new Blob([latest.text],{type:'text/markdown;charset=utf-8'}));a.href=u;a.download=latest.name;a.click();setTimeout(()=>URL.revokeObjectURL(u),500)};refresh();setInterval(refresh,2000);
