import React,{useEffect,useMemo,useState}from'react';
import{createRoot}from'react-dom/client';
import{Plus,Upload,Trash2,Film,Sparkles,Download,LoaderCircle,Shuffle,RotateCcw}from'lucide-react';
import'./styles.css';

type RemoteFile={name:string;url:string;public_id:string;bytes:number};
type Clip={id:string;file:File;status:'uploading'|'uploaded'|'error';progress:number;error?:string;remote?:RemoteFile};
type Stage={id:string;name:string;files:Clip[]};
type Job={id:string;status:string;phase:string;progress:number;total:number;percent:number;error?:string|null;download_url?:string|null};

const API='https://videolabs-worker.onrender.com';
const CLOUD='sskapqzv';
const PRESET='videolabs_upload';
const MAX_BYTES=100*1024*1024;
const uid=()=>Math.random().toString(36).slice(2);
const defaults=():Stage[]=>[
 {id:uid(),name:'Hook',files:[]},
 {id:uid(),name:'Corpo',files:[]},
 {id:uid(),name:'Desenvolvimento',files:[]},
 {id:uid(),name:'CTA',files:[]},
];
const wait=(ms:number)=>new Promise(r=>setTimeout(r,ms));

function App(){
 const[stages,setStages]=useState<Stage[]>(defaults());
 const[projectName,setProjectName]=useState('Projeto VideoLabs');
 const[mode,setMode]=useState<'all'|'random'>('all');
 const[randomN,setRandomN]=useState(25);
 const[job,setJob]=useState<Job|null>(null);
 const[busy,setBusy]=useState(false);
 const[message,setMessage]=useState('');

 const total=useMemo(()=>stages.length&&stages.every(s=>s.files.length)?stages.reduce((n,s)=>n*s.files.length,1):0,[stages]);
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
  const form=new FormData();
  form.append('file',file);
  form.append('upload_preset',PRESET);
  const xhr=new XMLHttpRequest();
  xhr.open('POST',`https://api.cloudinary.com/v1_1/${CLOUD}/video/upload`);
  xhr.timeout=300000;
  xhr.upload.onprogress=e=>{if(e.lengthComputable)onProgress(Math.min(99,Math.round(e.loaded/e.total*100)))};
  xhr.onerror=()=>reject(new Error(`Falha de rede ao enviar ${file.name}.`));
  xhr.ontimeout=()=>reject(new Error(`O upload de ${file.name} demorou demais.`));
  xhr.onload=()=>{
   let data:any={};
   try{data=JSON.parse(xhr.responseText||'{}')}catch{}
   if(xhr.status>=200&&xhr.status<300&&data.secure_url){
    onProgress(100);
    resolve({name:file.name,url:data.secure_url,public_id:data.public_id,bytes:data.bytes});
   }else reject(new Error(data?.error?.message||`Cloudinary recusou ${file.name} (${xhr.status||'sem resposta'}).`));
  };
  xhr.send(form);
 });

 const uploadClip=async(sid:string,clip:Clip)=>{
  if(clip.file.size>MAX_BYTES){
   patchClip(sid,clip.id,{status:'error',progress:0,error:'Arquivo acima de 100 MB.'});
   return;
  }
  patchClip(sid,clip.id,{status:'uploading',progress:0,error:undefined});
  let last:Error|undefined;
  for(let attempt=1;attempt<=3;attempt++){
   try{
    if(attempt>1){patchClip(sid,clip.id,{progress:0});await wait(800*attempt)}
    const remote=await xhrUpload(clip.file,p=>patchClip(sid,clip.id,{progress:p}));
    patchClip(sid,clip.id,{status:'uploaded',progress:100,remote,error:undefined});
    return;
   }catch(e){last=e instanceof Error?e:new Error('Falha no upload.');}
  }
  patchClip(sid,clip.id,{status:'error',progress:0,error:last?.message||'Falha no upload.'});
  setMessage(`${clip.file.name}: falha no upload após 3 tentativas. Toque em tentar novamente.`);
 };

 const change=async(id:string,list:FileList|null)=>{
  if(!list?.length)return;
  setMessage('');
  const incoming=Array.from(list).map(file=>({id:uid(),file,status:'uploading' as const,progress:0}));
  setStages(s=>s.map(x=>x.id===id?{...x,files:[...x.files,...incoming]}:x));
  for(const clip of incoming)await uploadClip(id,clip);
 };

 const retryClip=async(sid:string,cid:string)=>{
  const clip=stages.find(s=>s.id===sid)?.files.find(c=>c.id===cid);
  if(!clip)return;
  setMessage('');
  await uploadClip(sid,clip);
 };

 const generate=async()=>{
  if(!allReady)return;
  setBusy(true);setJob(null);setMessage('Criando processamento...');
  try{
   const uploaded=stages.map(s=>({name:s.name,files:s.files.map(c=>c.remote!)}));
   const r=await fetch(`${API}/v1/jobs/remote`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({projectName,mode,requested:randomN,stages:uploaded})});
   let data:any={};
   try{data=await r.json()}catch{}
   if(!r.ok)throw new Error(data.detail||`Falha ao criar processamento (${r.status}).`);
   setJob(data);setMessage('');
  }catch(e){
   setMessage(e instanceof Error?`Render: ${e.message}`:'Erro inesperado ao iniciar o processamento.');
  }finally{setBusy(false)}
 };

 const reset=()=>{setStages(defaults());setJob(null);setMessage('')};

 return <main>
  <header><div className="brand"><span><Film size={20}/></span>VideoLabs</div><button className="ghost" onClick={reset}>Novo projeto</button></header>
  <section className="hero"><div className="pill"><Sparkles size={14}/> COMBINAÇÕES AUTOMÁTICAS</div><h1>Um upload.<br/><em>Centenas de vídeos.</em></h1><p>Adicione suas etapas, envie os clipes e deixe o VideoLabs criar as combinações automaticamente.</p></section>
  <section className="workspace">
   <div className="projectBar"><label><small>NOME DO PROJETO</small><input value={projectName} onChange={e=>setProjectName(e.target.value)}/></label><div className="modes"><button className={mode==='all'?'active':''} onClick={()=>setMode('all')}>Todas</button><button className={mode==='random'?'active':''} onClick={()=>setMode('random')}><Shuffle size={14}/> Aleatórias</button>{mode==='random'&&<input type="number" min="1" max="625" value={randomN} onChange={e=>setRandomN(Math.max(1,Number(e.target.value)||1))}/>}</div></div>
   <div className="sectionHead"><div><small>ESTRUTURA</small><h2>Etapas do vídeo</h2></div><button className="add" onClick={add}><Plus size={17}/> Adicionar etapa</button></div>
   <div className="stages">{stages.map((s,i)=><article className="stage" key={s.id}>
    <div className="stageTop"><b>{String(i+1).padStart(2,'0')}</b><input value={s.name} onChange={e=>setStages(a=>a.map(x=>x.id===s.id?{...x,name:e.target.value}:x))}/>{stages.length>2&&<button onClick={()=>setStages(a=>a.filter(x=>x.id!==s.id))}><Trash2 size={16}/></button>}</div>
    <label className="drop"><Upload size={20}/><strong>{s.files.length?`${s.files.length} vídeo(s) adicionado(s)`:'Adicionar vídeos'}</strong><span>MP4, MOV, WebM, M4V • até 100 MB cada</span><input hidden multiple type="file" accept="video/*" onChange={e=>{void change(s.id,e.target.files);e.currentTarget.value=''}}/></label>
    {s.files.length>0&&<div className="files">{s.files.map(c=><span key={c.id}>{c.file.name} · {c.status==='uploaded'?'enviado':c.status==='error'?'erro':`${c.progress}%`}{c.status==='error'&&<button title="Tentar novamente" onClick={()=>void retryClip(s.id,c.id)}><RotateCcw size={13}/></button>}<button onClick={()=>removeFile(s.id,c.id)}>×</button></span>)}</div>}
   </article>)}</div>
   {job?<div className={`job ${job.status}`}><div className="jobTop"><div><small>PROCESSAMENTO</small><strong>{job.phase||job.status}</strong></div><b>{job.percent||0}%</b></div><div className="progress"><i style={{width:`${job.percent||0}%`}}/></div><p>{job.status==='error'?(job.error||'Falha no processamento.'):job.status==='ready'?`${job.total} vídeo(s) finalizado(s).`:`${job.progress||0} de ${job.total||0} combinações concluídas.`}</p>{job.status==='ready'&&<a className="download" href={`${API}${job.download_url}`}><Download size={17}/> Baixar ZIP</a>}</div>:
   <div className="summary"><div><small>{uploading?'UPLOAD':errors?'UPLOAD COM ERRO':'COMBINAÇÕES'}</small><strong>{uploading?`${aggregateUpload}%`:total.toLocaleString('pt-BR')}</strong><span>{uploading?`${uploading} arquivo(s) enviando...`:errors?`${errors} arquivo(s) precisam ser reenviados`:allReady?'vídeos possíveis':total?'aguardando uploads':'vídeos possíveis'}</span></div><button disabled={!allReady||busy} onClick={generate}>{busy?<LoaderCircle className="spin" size={17}/>:uploading?<LoaderCircle className="spin" size={17}/>:null}{busy?'Iniciando...':uploading?'Enviando...':mode==='random'?`Gerar ${Math.min(randomN,total||randomN)}`:'Gerar combinações'} <span>→</span></button></div>}
   {message&&!job&&<div className="notice">{message}</div>}
  </section>
  <footer>VideoLabs • V0.5</footer>
 </main>
}

createRoot(document.getElementById('root')!).render(<App/>);
