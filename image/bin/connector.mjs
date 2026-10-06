import fs from 'node:fs';
import crypto from 'node:crypto';

const action = process.argv[2] || 'inspect';
const port = process.env.CODEXPP_CDP_PORT || '9224';
const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const target = targets.find(t => t.type === 'page' && t.url.startsWith('https://chatgpt.com/'));
if (!target) throw new Error('No managed ChatGPT tab');
const socket = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  socket.addEventListener('open', resolve, {once: true});
  socket.addEventListener('error', () => reject(new Error('CDP connection failed')), {once: true});
});
let id = 0;
const pending = new Map();
socket.addEventListener('message', event => {
  const data = JSON.parse(event.data);
  if (pending.has(data.id)) {
    const {resolve, reject, timer} = pending.get(data.id);
    pending.delete(data.id);
    clearTimeout(timer);
    if (data.error) reject(new Error('CDP command failed'));
    else resolve(data.result);
  }
});
function call(method, params = {}) {
  const key = ++id;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {pending.delete(key); reject(new Error('CDP timeout'));}, 10000);
    pending.set(key, {resolve, reject, timer});
    socket.send(JSON.stringify({id: key, method, params}));
  });
}
async function evaluate(expression) {
  const result = await call('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
  if (result.exceptionDetails) throw new Error('Page evaluation failed');
  return result.result.value;
}
function printInfo(value) {
  const secret=fs.readFileSync('/data/state/mcp.secret','utf8').trim();
  console.log(JSON.stringify(value).replaceAll(secret,'<secret>'));
}
async function clickElement(expression) {
  const deadline=Date.now()+12000;
  while(!(await evaluate(`Boolean(${expression})`))) {
    if(Date.now()>deadline)throw new Error('Expected control did not appear');
    await new Promise(resolve=>setTimeout(resolve,300));
  }
  const point=await evaluate(`(() => {const e=${expression};if(!e)throw new Error('Expected control missing');e.scrollIntoView({block:'center',behavior:'instant'});const r=e.getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2};})()`);
  await call('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',clickCount:1,...point});
  await call('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',clickCount:1,...point});
  await new Promise(resolve=>setTimeout(resolve,500));
}
try {
  if(action==='task-submit'||action==='task-collect') {
    const taskId=process.argv[3],phase=process.argv[4];
    if(!/^[a-f0-9]{12}$/.test(taskId)||!['plan','review'].includes(phase))throw new Error('Invalid task phase');
    const dir=(process.env.CODEXPP_TASKS||'/data/tasks')+'/'+taskId;
    const marker='CODEXPP_'+phase.toUpperCase()+'_END_'+taskId;
    const submittedFile=dir+'/'+phase+'-submitted.json';
    const collectedFile=dir+'/'+phase+'-collected.json';
    if(action==='task-collect'&&fs.existsSync(collectedFile)) {
      const submitted=JSON.parse(fs.readFileSync(submittedFile,'utf8'));
      const collected=JSON.parse(fs.readFileSync(collectedFile,'utf8'));
      if(!submitted.sent||!/^\/c\/[A-Za-z0-9-]+$/.test(submitted.chatPath||'')||collected.chatPath!==submitted.chatPath)throw new Error('Collected conversation provenance mismatch');
      const response=fs.readFileSync(dir+'/'+phase+'-response.md','utf8');
      if(crypto.createHash('sha256').update(response).digest('hex')!==collected.responseHash)throw new Error('Collected response was changed');
      printInfo({taskId,phase,state:'collected'});
      socket.close();
      process.exit(0);
    }
    if(action==='task-submit') {
      if(fs.existsSync(submittedFile))throw new Error('Task phase already submitted');
      const prompt=fs.readFileSync(dir+'/'+phase+'-prompt.txt','utf8');
      if(!prompt.includes(taskId)||!prompt.includes(marker))throw new Error('Task marker missing');
      await evaluate(`(() => {const e=document.querySelector('#prompt-textarea,[contenteditable=true]');if(!e||e.innerText.trim()!=='codex++'||!e.querySelector('[contenteditable=false]'))throw new Error('Expected only current plugin mention');e.focus();})()`);
      await call('Input.insertText',{text:prompt});
      const submission={submittedAt:new Date().toISOString(),taskId,phase,sent:false};
      fs.writeFileSync(submittedFile,JSON.stringify(submission),{mode:0o600,flag:'wx'});
      await clickElement(`document.querySelector('button[aria-label="Send"]:not(:disabled)')`);
      const deadline=Date.now()+12000;
      let chatPath;
      while(Date.now()<deadline) {
        chatPath=await evaluate(`location.pathname`);
        if(chatPath.startsWith('/c/'))break;
        await new Promise(resolve=>setTimeout(resolve,300));
      }
      if(!chatPath?.startsWith('/c/'))throw new Error('Submission is uncertain; inspect browser before recovery');
      fs.writeFileSync(submittedFile,JSON.stringify({...submission,sent:true,chatPath}),{mode:0o600});
    } else {
      const submitted=JSON.parse(fs.readFileSync(submittedFile,'utf8'));
      if(!submitted.sent||!/^\/c\/[A-Za-z0-9-]+$/.test(submitted.chatPath||''))throw new Error('Wrong or uncertain task conversation');
      if(await evaluate('location.pathname')!==submitted.chatPath) {
        await call('Page.navigate',{url:'https://chatgpt.com'+submitted.chatPath});
        printInfo({taskId,phase,state:'restoring_conversation'});
        socket.close();
        process.exit(0);
      }
      const page=await evaluate(`({text:document.querySelector('main')?.innerText||'',stopped:!document.querySelector('button[aria-label="Stop"]'),path:location.pathname})`);
      if(page.path!==submitted.chatPath)throw new Error('Wrong task conversation');
      if(!page.text.includes('任务编号：'+taskId)) {
        printInfo({taskId,phase,state:'waiting_for_conversation'});
        socket.close();
        process.exit(0);
      }
      const tools=['Workspace Info','Read File','List Directory','Search Workspace','Git Status','Git Diff'];
      const permission=`(() => {const main=document.querySelector('main');const buttons=[...main.querySelectorAll('button')].filter(e=>e.getClientRects().length&&e.innerText.trim().startsWith('Allow once'));if(!buttons.length)return null;if(buttons.length!==1)throw new Error('Ambiguous permission cards');const button=buttons[0];for(let card=button.parentElement;card&&card!==main;card=card.parentElement){if(${JSON.stringify(tools)}.some(name=>card.innerText.includes("ChatGPT will call codex++'s "+name+" tool."))&&card.querySelectorAll('button').length<=3)return button;}throw new Error('Unmatched permission card');})()`;
      if(await evaluate(`Boolean(${permission})`)) {
        await clickElement(permission);
        printInfo({taskId,phase,state:'tool_allowed_once'});
      } else {
        const start=page.text.lastIndexOf('ChatGPT said:');
        const response=start<0?'':page.text.slice(start+'ChatGPT said:'.length).split('ChatGPT can make mistakes.')[0].trim();
        if(page.stopped&&response.includes(marker)) {
          const rows=fs.readFileSync('/data/state/access.log','utf8').trim().split('\n').flatMap(x=>{try{return[JSON.parse(x)];}catch{return[];}});
          const calls=rows.filter(r=>r.ts>=Date.parse(submitted.submittedAt)/1000&&r.path_ok&&r.status===200&&r.tools?.includes('read_file'));
          if(!calls.length)throw new Error('No real read_file evidence for this phase');
          const text=response.replace(marker,'').trim()+'\n';
          if(!text.trim())throw new Error('Empty response; abandon or continue this conversation manually');
          const responseFile=dir+'/'+phase+'-response.md';
          fs.writeFileSync(responseFile+'.tmp',text,{mode:0o600});
          fs.renameSync(responseFile+'.tmp',responseFile);
          fs.writeFileSync(dir+'/'+phase+'-collected.json',JSON.stringify({chatPath:page.path,collectedAt:new Date().toISOString(),readCalls:calls.length,evidenceScope:'service-call-only; not task attribution',responseHash:crypto.createHash('sha256').update(text).digest('hex')}),{mode:0o600});
          printInfo({taskId,phase,state:'collected'});
        } else printInfo({taskId,phase,state:'waiting'});
      }
    }
  }
  if(action==='install-reconnect') {
    if(!fs.existsSync('/data/state/reconnect-submitted.json'))throw new Error('No reconnect request recorded');
    await clickElement(`[...document.querySelectorAll('[role=dialog] button')].find(e=>e.innerText.trim()==='Connect codex++')`);
  }
  if(action==='submit-reconnect') {
    const endpoint=fs.readFileSync('/data/state/public_url','utf8').trim()+'/mcp/'+fs.readFileSync('/data/state/mcp.secret','utf8').trim();
    if(!fs.existsSync('/data/state/connector-previous.json'))throw new Error('Existing connector migration required');
    const valid=await evaluate(`(() => {const d=document.querySelector('[role=dialog]');return d?.querySelector('input[placeholder="Custom Tool"]')?.value==='codex++'&&d.querySelector('input[type=url]')?.value===${JSON.stringify(endpoint)}&&d.querySelector('select')?.value==='NONE';})()`);
    if(!valid)throw new Error('Unexpected reconnect configuration');
    await clickElement(`document.querySelector('[role=dialog] input[type=checkbox]')`);
    await clickElement(`[...document.querySelectorAll('[role=dialog] button')].find(e=>e.innerText.trim()==='Create as a plugin'&&!e.disabled)`);
    fs.writeFileSync('/data/state/reconnect-submitted.json',JSON.stringify({submittedAt:new Date().toISOString()}),{mode:0o600});
  }
  if(action==='retire-name') {
    await evaluate(`(() => {const e=document.querySelector('[role=dialog] input[name="app-name"]');if(!e)throw new Error('Name field missing');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e,'codex++ previous endpoint');e.dispatchEvent(new Event('input',{bubbles:true}));return true;})()`);
    await clickElement(`[...document.querySelectorAll('[role=dialog] button')].find(e=>e.innerText.trim()==='Save')`);
    fs.copyFileSync('/data/state/connector.json','/data/state/connector-previous.json');
    fs.chmodSync('/data/state/connector-previous.json',0o600);
  }
  if(action==='new-directory') {
    await call('Page.navigate',{url:'https://chatgpt.com/plugins'});
    const deadline=Date.now()+12000;
    while(!(await evaluate(`Boolean(document.querySelector('button[aria-label="Add"]'))`))) {
      if(Date.now()>deadline)throw new Error('Directory did not load');
      await new Promise(resolve=>setTimeout(resolve,300));
    }
  }
  if(action==='edit-name') await clickElement(`[...document.querySelectorAll('button')].find(e=>e.innerText.trim()==='Edit'&&e.parentElement.parentElement.innerText.includes('App name'))`);
  if(action==='download-plugin') {
    fs.mkdirSync('/data/state/plugin-download',{recursive:true,mode:0o700});
    await call('Browser.setDownloadBehavior',{behavior:'allow',downloadPath:'/data/state/plugin-download'});
    await clickElement(`[...document.querySelectorAll('[role=menuitem]')].find(e=>e.innerText.trim()==='Download plugin ZIP')`);
    await new Promise(resolve=>setTimeout(resolve,1500));
  }
  if(action==='upload-version') await clickElement(`[...document.querySelectorAll('[role=menuitem]')].find(e=>e.innerText.trim()==='Upload new version')`);
  if(action==='settings-text') printInfo(await evaluate(`({text:document.querySelector('main')?.innerText||document.body.innerText})`));
  if(action==='cancel-dialog') await clickElement(`[...document.querySelectorAll('[role=dialog] button')].find(e=>e.innerText.trim()==='Cancel')`);
  if(action==='connection-settings') {
    await clickElement(`document.querySelector('button[aria-label="Settings for codex++ (no account linked)"]')`);
  }
  if(action==='connection-actions') {
    await clickElement(`document.querySelector('button[aria-label="Actions for codex++ (no account linked)"]')`);
  }
  if(action==='edit-context') {
    printInfo(await evaluate(`(() => [...document.querySelectorAll('button')].filter(e=>e.innerText.trim()==='Edit').map(e=>({nearby:e.parentElement.parentElement.innerText.slice(0,900)})))()`));
  }
  if(action==='manage-plugin') {
    const point=await evaluate(`(() => {const e=[...document.querySelectorAll('[role=menuitem]')].find(e=>e.innerText.trim()==='Manage');if(!e)throw new Error('Manage action missing');const r=e.getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2};})()`);
    await call('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',clickCount:1,...point});
    await call('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',clickCount:1,...point});
    await new Promise(resolve=>setTimeout(resolve,600));
  }
  if(action==='plugin-menu') {
    await clickElement(`document.querySelector('button[aria-label="More actions"]')`);
  }
  if(action==='plugin-page') {
    const registered=JSON.parse(fs.readFileSync('/data/state/connector.json','utf8'));
    if(!registered.pluginPath.startsWith('/plugins/plugin_'))throw new Error('Invalid plugin path');
    await call('Page.navigate',{url:'https://chatgpt.com'+registered.pluginPath});
    await new Promise(resolve=>setTimeout(resolve,1600));
  }
  if(action==='record-verification') {
    const result=await evaluate(`(() => {const main=document.querySelector('main')?.innerText||'';if(!main.includes('ChatGPT said:')||!main.includes('/workspace')||!main.includes('image')||document.querySelector('button[aria-label="Stop"]'))throw new Error('Successful response not complete');return{chatPath:location.pathname};})()`);
    const submitted=JSON.parse(fs.readFileSync('/data/state/verification-submitted.json','utf8'));
    const rows=fs.readFileSync('/data/state/access.log','utf8').trim().split('\n').map(x=>JSON.parse(x));
    const proof=rows.findLast(r=>r.ts>=Date.parse(submitted.submittedAt)/1000&&r.path_ok&&r.status===200&&r.tools?.includes('workspace_info'));
    if(!proof)throw new Error('No matching tool call evidence');
    const registered=JSON.parse(fs.readFileSync('/data/state/connector.json','utf8'));
    registered.toolsVerified=true;registered.verifiedAt=new Date().toISOString();registered.toolCallAt=proof.ts;registered.chatPath=result.chatPath;
    fs.writeFileSync('/data/state/connector.json',JSON.stringify(registered),{mode:0o600});
    console.log(JSON.stringify({toolsVerified:true,tool:'workspace_info',root:'/workspace'}));
  }
  if(action==='allow-verification') {
    if(!fs.existsSync('/data/state/verification-submitted.json'))throw new Error('No verification request recorded');
    const point=await evaluate(`(() => {const main=document.querySelector('main');if(!main?.innerText.includes("ChatGPT will call codex++'s Workspace Info tool."))throw new Error('Unexpected permission request');const e=[...main.querySelectorAll('button')].find(e=>e.innerText.trim().startsWith('Allow once'));if(!e)throw new Error('Allow once missing');const r=e.getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2};})()`);
    await call('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',clickCount:1,...point});
    await call('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',clickCount:1,...point});
    await new Promise(resolve=>setTimeout(resolve,1500));
  }
  if (action === 'select-plugin') {
    const point=await evaluate(`(() => {const e=[...document.querySelectorAll('button,[role=option]')].find(e=>e.getClientRects().length&&e.innerText.trim().startsWith('codex++'));if(!e)throw new Error('Plugin suggestion missing');const r=e.getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2};})()`);
    await call('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',clickCount:1,...point});
    await call('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',clickCount:1,...point});
    await new Promise(resolve=>setTimeout(resolve,600));
  }
  if (action === 'verify-tool') {
    if(fs.existsSync('/data/state/verification-submitted.json')) {
      const old=JSON.parse(fs.readFileSync('/data/state/verification-submitted.json','utf8'));
      const registered=JSON.parse(fs.readFileSync('/data/state/connector.json','utf8'));
      if(registered.toolsVerified||Date.parse(old.submittedAt)>=Date.parse(registered.registeredAt))throw new Error('Verification already submitted; inspect response first');
      fs.renameSync('/data/state/verification-submitted.json','/data/state/verification-previous.json');
    }
    const prompt='请仅调用 codex++ 的 workspace_info，返回工具报告的工作区根目录和项目列表。不修改文件，不调用其他插件。若工具不可用请明确说明，不要推测。';
    await evaluate(`(() => {const e=document.querySelector('#prompt-textarea,[contenteditable=true]');if(!e||e.innerText.trim()!=='codex++'||!e.querySelector('[contenteditable=false]'))throw new Error('Composer must contain only the plugin mention');e.focus();})()`);
    await call('Input.insertText',{text:prompt});
    const point=await evaluate(`(() => {const e=document.querySelector('button[aria-label="Send"]');if(!e||e.disabled)throw new Error('Send unavailable');const r=e.getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2};})()`);
    fs.writeFileSync('/data/state/verification-submitted.json',JSON.stringify({submittedAt:new Date().toISOString(),prompt}),{mode:0o600});
    await call('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',clickCount:1,...point});
    await call('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',clickCount:1,...point});
    await new Promise(resolve=>setTimeout(resolve,1500));
  }
  if(action==='response') {
    console.log(JSON.stringify(await evaluate(String.raw`(() => ({path:location.pathname,assistant:[...document.querySelectorAll('[data-message-author-role="assistant"]')].map(e=>e.innerText.replace(/https?:\/\/\S+/g,'<url>').slice(0,4000)),main:(document.querySelector('main')?.innerText||'').replace(/https?:\/\/\S+/g,'<url>').slice(-5000)}))()`)));
  }
  if (action === 'work-mode') {
    await clickElement(`[...document.querySelectorAll('button')].find(e=>e.innerText.trim()==='Work')`);
  }
  if (action === 'mention-plugin') {
    await evaluate(`(() => {const e=document.querySelector('#prompt-textarea,[contenteditable=true]');if(!e)throw new Error('Composer missing');e.focus();})()`);
    await call('Input.insertText',{text:'@codex++'});
    await new Promise(resolve=>setTimeout(resolve,700));
  }
  if (action === 'try-chat') {
    await clickElement(`[...document.querySelectorAll('button')].find(e=>e.innerText.trim()==='Try in chat')`);
    await new Promise(resolve=>setTimeout(resolve,1500));
  }
  if (action === 'open-plugin') {
    const url=await evaluate(`(() => {const e=[...document.querySelectorAll('a[href]')].find(e=>e.innerText.trim()==='codex++');if(!e)throw new Error('codex++ card missing');return e.href;})()`);
    await call('Page.navigate',{url});
    const pendingUrl=fs.readFileSync('/data/state/pending_connector_url','utf8').trim();
    const currentUrl=fs.readFileSync('/data/state/public_url','utf8').trim()+'/mcp/'+fs.readFileSync('/data/state/mcp.secret','utf8').trim();
    if(pendingUrl!==currentUrl)throw new Error('Connector endpoint changed');
    fs.writeFileSync('/data/state/connector_url',currentUrl,{mode:0o600});
    fs.writeFileSync('/data/state/connector.json',JSON.stringify({name:'codex++',pluginPath:new URL(url).pathname,registeredAt:new Date().toISOString(),toolsVerified:false}),{mode:0o600});
    await new Promise(resolve=>setTimeout(resolve,1800));
  }
  if (action === 'personal-page') {
    const point=await evaluate(`(() => {const e=[...document.querySelectorAll('button')].find(e=>e.innerText.trim()==='Personal');if(!e)throw new Error('Personal tab missing');const r=e.getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2};})()`);
    await call('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',clickCount:1,...point});
    await call('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',clickCount:1,...point});
    await new Promise(resolve=>setTimeout(resolve,800));
  }
  if (action === 'fill' || action === 'prepare-consent') {
    const publicUrl=fs.readFileSync('/data/state/public_url','utf8').trim();
    const secret=fs.readFileSync('/data/state/mcp.secret','utf8').trim();
    const values={name:'codex++',description:'Read-only workspace access for Codex planning and review.',url:publicUrl+'/mcp/'+secret};
    await evaluate(`(() => {const d=document.querySelector('[role=dialog]');if(!d)throw new Error('Dialog missing');const values=${JSON.stringify(values)};const fields=[[d.querySelector('input[placeholder="Custom Tool"]'),values.name],[d.querySelector('textarea'),values.description],[d.querySelector('input[type=url]'),values.url]];for(const [e,value]of fields){if(!e)throw new Error('Field missing');const proto=e.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(proto,'value').set.call(e,value);e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));}return true;})()`);
    fs.writeFileSync('/data/state/pending_connector_url',values.url,{mode:0o600});
    await evaluate(`(() => {const d=document.querySelector('[role=dialog]');const s=d?.querySelector('select');if(!s)throw new Error('Authentication selector missing');s.value='NONE';s.dispatchEvent(new Event('change',{bubbles:true}));return true;})()`);
  }
  if (action === 'mcp-form') {
    const point=await evaluate(`(() => {const e=[...document.querySelectorAll('[role=menuitem]')].find(e=>e.innerText.trim()==='Create custom MCP server');if(!e)throw new Error('MCP menu missing');const r=e.getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2};})()`);
    await call('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',clickCount:1,...point});
    await call('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',clickCount:1,...point});
    await new Promise(resolve=>setTimeout(resolve,700));
  }
  if (action === 'add-menu') {
    const point = await evaluate(`(() => {const b=[...document.querySelectorAll('button')].find(e=>e.getAttribute('aria-label')==='Add');if(!b)throw new Error('Add button missing');const r=b.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);
    await call('Input.dispatchMouseEvent', {type:'mousePressed',button:'left',clickCount:1,...point});
    await call('Input.dispatchMouseEvent', {type:'mouseReleased',button:'left',clickCount:1,...point});
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  if (action === 'directory-page') {
    await evaluate(`(() => {const b=[...document.querySelectorAll('button')].find(e=>e.getAttribute('aria-label')==='Plugins'); if(!b)throw new Error('Plugins navigation missing');b.click();})()`);
    await new Promise(resolve => setTimeout(resolve, 500));
    await evaluate(`(() => {const b=[...document.querySelectorAll('button')].find(e=>e.innerText.trim()==='Browse directory');if(!b)throw new Error('Directory button missing');b.click();})()`);
    await new Promise(resolve => setTimeout(resolve, 2000));
  }
  if (action === 'create-page' || action === 'security-page') {
    const url = action === 'create-page'
      ? 'https://chatgpt.com/plugins#settings/Connectors?create-connector=true&redirectAfter=%2Fplugins'
      : 'https://chatgpt.com/#settings/Security';
    if (action === 'security-page') {
      const clicked = await evaluate(`(() => {const b=[...document.querySelectorAll('button')].find(e=>e.getAttribute('aria-label')==='Security and login'); if(!b)return false; b.click(); return true;})()`);
      if (!clicked) await call('Page.navigate', {url});
    } else await call('Page.navigate', {url});
    await new Promise(resolve => setTimeout(resolve, 2500));
  }
  const info = await evaluate(String.raw`(() => {
    const visible = e => e.getClientRects().length > 0;
    const body = document.body?.innerText || '';
    return {path: location.pathname, readyState: document.readyState, bodyLength: body.length,
      loginPrompt: /log in|sign in|登录|登入/i.test(body), retryPrompt: /retry|重试/i.test(body),
      hasComposer: Boolean(document.querySelector('#prompt-textarea,[contenteditable=true]')),
      composer: (document.querySelector('#prompt-textarea,[contenteditable=true]')?.innerText||'').slice(0,200),
      inputs: [...document.querySelectorAll('input,textarea')].filter(visible).map(e => ({type:e.type,id:e.id,name:e.name,placeholder:e.placeholder,label:[...document.querySelectorAll('label')].find(l=>l.htmlFor===e.id)?.innerText})),
      menu: [...document.querySelectorAll('[role=menuitem]')].filter(visible).map(e=>e.innerText.trim()),
      options: [...document.querySelectorAll('[role=option]')].filter(visible).map(e=>e.innerText.trim().slice(0,160)),
      mode: [...document.querySelectorAll('button')].filter(e=>/^(Chat|Work)$/.test(e.innerText.trim())).map(e=>({text:e.innerText.trim(),selected:e.getAttribute('aria-selected'),state:e.getAttribute('data-state')})),
      pluginLinks: [...document.querySelectorAll('a[href]')].filter(e=>visible(e)&&/codex\+\+/.test(e.innerText)).map(e=>{const u=new URL(e.href);return{text:e.innerText.trim().slice(0,120),path:u.hostname==='chatgpt.com'?u.pathname:'external'};}),
      selects: [...document.querySelectorAll('select')].filter(visible).map(e=>({name:e.name,options:[...e.options].map(o=>({text:o.text,value:o.value}))})),
      dialogText: (document.querySelector('[role=dialog]')?.innerText||'').replace(/https?:\/\/\S+/g,'<url>').slice(0,1300),
      authSelection: document.querySelector('[role=dialog] select')?.value,
      consentChecked: document.querySelector('[role=dialog] input[type=checkbox]')?.checked,
      buttons: [...document.querySelectorAll('button')].filter(visible).map((e,i) => ({index:i,text:(e.innerText||'').trim().slice(0,120),label:e.getAttribute('aria-label'),title:e.title})).slice(-25),
      switches: [...document.querySelectorAll('[role=switch]')].filter(visible).map(e=>{let p=e;const nearby=[];for(let i=0;i<4&&p;i++,p=p.parentElement){const t=(p.innerText||'').trim();if(t&&t.length<500)nearby.push(t);}return {label:e.getAttribute('aria-label'),checked:e.getAttribute('aria-checked'),nearby};}),
      controls: [...document.querySelectorAll('button,[role=button],[role=combobox],[role=switch]')].filter(visible)
        .map(e => (e.innerText || e.getAttribute('aria-label') || '').trim()).filter(t => /connect|plugin|developer|oauth|authentication|create|security|连接|插件|开发|认证|创建|安全|授权|advanced|高级|mode|模式|log in|sign in|登录|登入|settings|设置/i.test(t)).slice(0,30)
    };
  })()`);
  printInfo(info);
} finally {
  socket.close();
}
