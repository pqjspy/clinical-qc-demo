// Redact request headers/cookies and all user payloads before showing live tail.
import {spawn} from 'node:child_process';
const child=spawn('./node_modules/.bin/wrangler',['tail','clinical-qc-public-demo','--config','wrangler.jsonc','--format','json'],{env:{...process.env,WRANGLER_SEND_METRICS:'false'}});
let pending='';
child.stdout.on('data',chunk=>{
  pending+=chunk.toString();
  if(pending.length>500000){pending='';return;}
  // Wrangler emits one pretty-printed JSON object per event.
  const boundary=pending.indexOf('\n}\n');
  if(boundary<0)return;
  const raw=pending.slice(0,boundary+2); pending=pending.slice(boundary+3);
  try {
    const e=JSON.parse(raw.slice(raw.indexOf('{')));
    console.log(JSON.stringify({outcome:e.outcome,cpuTime:e.cpuTime,wallTime:e.wallTime,
      status:e.event?.response?.status,path:e.event?.request?.url ? new URL(e.event.request.url).pathname : undefined,
      exceptions:e.exceptions?.map(x=>({name:x.name,message:x.message?.slice(0,300)})),
      diagnostic:e.logs?.filter(x=>String(x.message?.[0])==='cloud_ai_failure').map(x=>x.message.slice(0,3))}));
  }catch{}
});
child.stderr.on('data',chunk=>{
  const t=chunk.toString(); if(t.includes('ERROR'))console.error('Tail connection error; inspect CLI permissions without exposing request data.');
});
setTimeout(()=>child.kill('SIGTERM'),120000);
process.on('SIGINT',()=>child.kill('SIGTERM'));
