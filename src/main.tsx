import React,{useEffect,useMemo,useState}from'react';
import{createRoot}from'react-dom/client';
import{Plus,Upload,Trash2,Film,Sparkles,Download,LoaderCircle,Shuffle,RotateCcw,Gauge,Clapperboard}from'lucide-react';
import'./styles.css';

type RemoteFile={name:string;url:string;public_id:string;bytes:number};
type Clip={id:string;file:File;label:string;status:'uploading'|'uploaded'|'error';progress:number;error?:string;remote?:RemoteFile};
type Stage={id:string;name:string;files:Clip[]};
type Quality='camera4k'|'high'|'fast';
type Job={id:string;status:string;phase:string;progress:number;total:number;percent:number;quality?:Quality;error?:string|null;download_url?:string|null};

const API='https://videolabs-worker.onrender.com';
const CLOUD='sskapqzv';
const PRESET='videolabs_upload';
const MAX_BYTES=100*1024*1024;
const uid=()=>Math.random().toString(36).slice(2);
const defaults=():Stage[]=>[];
const wait=(ms:number)=>new Promise(r=>setTimeout(r,ms));
const qualityText=(q?:Quality)=>q==='fast'?'720p Rápida':q==='high'?'1080p Alta':'4K Câmera';

const stagePrefix=(name:string)=>{
 const clean=name.trim();
 if(!clean||/^Etapa\s+\d+$/i.test(clean))return'V';
 const compact=clean.replace(/[^a-zA-Z0-9]/g,'');
 if(clean===clean.toUpperCase()&&compact.length>1&&compact.length<=5)return compact.toUpperCase();
 const words=clean.split(/\s+/).filter(Boolean);
 if(words.length>1)return words.map(w=>w[0]).join('').replace(/[^a-zA-Z0-9]/g,'').slice(0,4).toUpperCase()||'V';
 return (compact[0]||'V').toUpperCase();
};

function App(){
 const[stages,setStages]=useState<Stage[]>(defaults());
 const[projectName,setProjectName]=useState('Projeto VideoLabs');
 const[mode,setMode]=useState<'all'|'random'>('all');
 const[randomN,setRandomN]=useState(25);
 const[quality,setQuality]=useState<Quality>('camera4k');
 const[job,setJob]=useState<Job|null>(null);
 const[busy,setBusy]=useState(false);
 const[message,setMessage]=useState('');

 const total=useMemo(()=>stages.length>=2&&stages.every(s=>s.files.length)?stages.reduce((n,s)=>n*s.files.length,1):0,[stages]);
 const clips=stages.flatMap(s=>s.files);
 const uploading=clips.filter(c=>c.status==='uploading').length;
 const errors=clips.filter(c=>c.status==='error').length;
 const allReady=Boolean(total)&&clips.length>0&&clips.every(c=>c.status==='uploaded');
 const aggregateUpload=clips.length?Math.round(clips.reduce((n,c)=>n+c.progress,0)/clips.length):0;

 const patchClip=(sid:string,cid:string,patch:Partial<Clip>)=>setStages(s=>s.map(stage=>stage.id===sid?{...stage,files:stage.files.map(c=>c.id===cid?{...c,...patch}:c)}:stage));
 const add=()=>setStages(s=>[...s,{id:uid(),name:`Etapa ${s.length+1}`,files:[]}]);
 const removeFile=(sid:string,cid:string)=>setStages(s=>s.map(x=>x.id===sid?{...x,files:x.files.filter(c=>c.id!==cid)}:x));

 useEffect(()=>{
  if(!job||!['queued','processing'].includes(job.status))return;
  const t=setInterval(async()=>{
   try{
    const r=await fetch(`${API}/v1/jobs/${job.id}`);
    if(r.ok)setJob(await r.json());
    else if(r.status===404)setJob(j=>j?{...j,status:'error',phase:'Processamento interrompido',error:'O servidor reiniciou e este processamento foi perdido. Tente gerar novamente.'}:j);
   }catch{}
  },2000);
  return()=>clearInterval(t);
 },[job?.id,job?.status]);

 const xhrUpload=(file:File,onProgress:(p:number)=>void)=>new Promise<RemoteFile>((resolve,reject)=>{
  const form=new FormData(); form.append('file',file); form.append('upload_preset',PRESET);
  const xhr=new XMLHttpRequest(); xhr.open('POST',`https://api.cloudinary.com/v1_1/${CLOUD}/video/upload`); xhr.timeout=300000;
  xhr.upload.onprogress=e=>{if(e.lengthComputable)onProgress(Math.min(99,Math.round(e.loaded/e.total*100)))};
  xhr.onerror=()=>reject(new Error(`Falha de rede ao enviar ${file.name}.`));
  xhr.ontimeout=()=>reject(new Error(`O upload de ${file.name} demorou demais.`));
  xhr.onload=()=>{let data:any={};try{data=JSON.parse(xhr.responseText||'{}')}catch{}if(xhr.status>=200&&xhr.status<300&&data.secure_url){onProgress(100);resolve({name:file.name,url:data.secure_url,public_id:data.public_id,bytes:data.bytes})}else reject(new Error(data?.error?.message||`Cloudinary recusou ${file.name} (${xhr.status||'sem resposta'}).`))};
  xhr.send(form);
 });

 const uploadClip=async(sid:string,clip:Clip)=>{
  if(clip.file.size>MAX_BYTES){patchClip(sid,clip.id,{status:'error',progress:0,error:'Arquivo acima de 100 MB.'});return}
  patchClip(sid,clip.id,{status:'uploading',progress:0,error:undefined}); let last:Error|undefined;
  for(let attempt=1;attempt<=3;attempt++){
   try{if(attempt>1){patchClip(sid,clip.id,{progress:0});await wait(800*attempt)}const remote=await xhrUpload(clip.file,p=>patchClip(sid,clip.id,{progress:p}));patchClip(sid,clip.id,{status:'uploaded',progress:100,remote,error:undefined});return}catch(e){last=e instanceof Error?e:new Error('Falha no upload.')}
  }
  patchClip(sid,clip.id,{status:'error',progress:0,error:last?.message||'Falha no upload.'}); setMessage(`${clip.file.name}: falha no upload após 3 tentativas. Toque em tentar novamente.`);
 };

 const change=async(id:string,list:FileList|null)=>{
  if(!list?.length)return;setMessage('');const stage=stages.find(s=>s.id===id);if(!stage)return;
  const base=stage.files.length,prefix=stagePrefix(stage.name);const incoming=Array.from(list).map((file,index)=>({id:uid(),file,label:`${prefix}${base+index+1}`,status:'uploading' as const,progress:0}));
  setStages(s=>s.map(x=>x.id===id?{...x,files:[...x.files,...incoming]}:x));
  for(let i=0;i<incoming.length;i+=2)await Promise.all(incoming.slice(i,i+2).map(clip=>uploadClip(id,clip)));
 };
 const retryClip=async(sid:string,cid:string)=>{const clip=stages.find(s=>s.id===sid)?.files.find(c=>c.id===cid);if(!clip)return;setMessage('');await uploadClip(sid,clip)};

 const generate=async()=>{
  if(!allReady)return;setBusy(true);setJob(null);setMessage('Criando processamento...');
  try{const uploaded=stages.map(s=>({name:s.name,files:s.files.map(c=>({...c.remote!,label:c.label.trim()||stagePrefix(s.name)}))}));const r=await fetch(`${API}/v1/jobs/remote`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({projectName,mode,requested:randomN,quality,stages:uploaded})});let data:any={};try{data=await r.json()}catch{}if(!r.ok)throw new Error(data.detail||`Falha ao criar processamento (${r.status}).`);setJob(data);setMessage('')}catch(e){setMessage(e instanceof Error?`Render: ${e.message}`:'Erro inesperado ao iniciar o processamento.')}finally{setBusy(false)}
 };
 const reset=()=>{setStages(defaults());setJob(null);setMessage('')};

 return <main>
  <header><div className="brand"><span><Film size={20}/></span>VideoLabs</div><button className="ghost" onClick={reset}>Novo projeto</button></header>
  <section className="hero"><div className="pill"><Sparkles size={14}/> COMBINAÇÕES AUTOMÁTICAS</div><h1>Um upload.<br/><em>Centenas de vídeos.</em></h1><p>Preserve a qualidade 4K da câmera, nomeie os clipes e gere combinações prontas para baixar.</p></section>
  <section className="workspace">
   <div className="projectBar"><label><small>NOME DO PROJETO</small><input value={projectName} onChange={e=>setProjectName(e.target.value)}/></label><div className="modes"><button className={mode==='all'?'active':''} onClick={()=>setMode('all')}>Todas</button><button className={mode==='random'?'active':''} onClick={()=>setMode('random')}><Shuffle size={14}/> Aleatórias</button>{mode==='random'&&<input type="number" min="1" max="625" value={randomN} onChange={e=>setRandomN(Math.max(1,Number(e.target.value)||1))}/>}</div></div>
   <div className="qualityBar"><div><small>QUALIDADE DE SAÍDA</small><strong>{quality==='camera4k'?'4K — preserva a qualidade da câmera':quality==='high'?'1080p — alta qualidade':'720p — processamento mais rápido'}</strong></div><div className="qualityOptions"><button className={quality==='camera4k'?'active':''} onClick={()=>setQuality('camera4k')}><Sparkles size={15}/><span><b>4K</b><small>Câmera</small></span></button><button className={quality==='high'?'active':''} onClick={()=>setQuality('high')}><Clapperboard size={15}/><span><b>1080p</b><small>Alta</small></span></button><button className={quality==='fast'?'active':''} onClick={()=>setQuality('fast')}><Gauge size={15}/><span><b>720p</b><small>Rápida</small></span></button></div></div>
   <div className="sectionHead"><div><small>ESTRUTURA</small><h2>Etapas do vídeo</h2></div><button className="add" onClick={add}><Plus size={17}/> Adicionar etapa</button></div>
   {stages.length===0?<div className="emptyState"><div><Plus size={22}/></div><strong>Nenhuma etapa ainda</strong><p>Comece adicionando a primeira etapa. Ex.: Hook, Corpo, CTA.</p><button className="add" onClick={add}><Plus size={17}/> Adicionar primeira etapa</button></div>:<div className="stages">{stages.map((s,i)=><article className="stage" key={s.id}><div className="stageTop"><b>{String(i+1).padStart(2,'0')}</b><input value={s.name} onChange={e=>setStages(a=>a.map(x=>x.id===s.id?{...x,name:e.target.value}:x))}/><button onClick={()=>setStages(a=>a.filter(x=>x.id!==s.id))}><Trash2 size={16}/></button></div><label className="drop"><Upload size={20}/><strong>{s.files.length?`${s.files.length} vídeo(s) adicionado(s)`:'Adicionar vídeos'}</strong><span>MP4, MOV, WebM, M4V • até 100 MB cada</span><input hidden multiple type="file" accept="video/*" onChange={e=>{void change(s.id,e.target.files);e.currentTarget.value=''}}/></label>{s.files.length>0&&<div className="files">{s.files.map(c=><div className="fileRow" key={c.id}><input className="clipName" value={c.label} maxLength={32} onChange={e=>patchClip(s.id,c.id,{label:e.target.value})} aria-label={`Nome de ${c.file.name}`}/><div className="fileMeta"><span title={c.file.name}>{c.file.name}</span><em>{c.status==='uploaded'?'enviado':c.status==='error'?'erro':`${c.progress}%`}</em></div>{c.status==='error'&&<button title="Tentar novamente" onClick={()=>void retryClip(s.id,c.id)}><RotateCcw size={13}/></button>}<button title="Remover" onClick={()=>removeFile(s.id,c.id)}>×</button></div>)}</div>}</article>)}</div>}
   {job?<div className={`job ${job.status}`}><div className="jobTop"><div><small>PROCESSAMENTO</small><strong>{job.phase||job.status}</strong></div><b>{job.percent||0}%</b></div><div className="progress"><i style={{width:`${job.percent||0}%`}}/></div><p>{job.status==='error'?(job.error||'Falha no processamento.'):job.status==='ready'?`${job.total} vídeo(s) finalizado(s) em ${qualityText(job.quality)}.`:`${job.progress||0} de ${job.total||0} combinações concluídas.`}</p>{job.status==='ready'&&<a className="download" href={`${API}${job.download_url}`}><Download size={17}/> Baixar ZIP</a>}</div>:<div className="summary"><div><small>{uploading?'UPLOAD':errors?'UPLOAD COM ERRO':'COMBINAÇÕES'}</small><strong>{uploading?`${aggregateUpload}%`:total.toLocaleString('pt-BR')}</strong><span>{uploading?`${uploading} arquivo(s) enviando...`:errors?`${errors} arquivo(s) precisam ser reenviados`:stages.length<2?'adicione pelo menos 2 etapas':allReady?'vídeos possíveis':total?'aguardando uploads':'adicione vídeos nas etapas'}</span></div><button disabled={!allReady||busy} onClick={generate}>{busy?<LoaderCircle className="spin" size={17}/>:uploading?<LoaderCircle className="spin" size={17}/>:null}{busy?'Iniciando...':uploading?'Enviando...':mode==='random'?`Gerar ${Math.min(randomN,total||randomN)}`:'Gerar combinações'} <span>→</span></button></div>}
   {message&&!job&&<div className="notice">{message}</div>}
  </section>
  <footer>VideoLabs • V0.7</footer>
 </main>
}
createRoot(document.getElementById('root')!).render(<App/>);
