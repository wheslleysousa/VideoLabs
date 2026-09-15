import React,{useEffect,useMemo,useState}from'react';
import{createRoot}from'react-dom/client';
import{
 Plus,Upload,Trash2,Film,Sparkles,Download,LoaderCircle,Shuffle,RotateCcw,Gauge,Clapperboard,
 Play,Copy,ArrowUp,ArrowDown,ChevronDown,ChevronUp,Scissors,X,Check,Square,CheckSquare
}from'lucide-react';
import'./styles.css';

type RemoteFile={name:string;url:string;public_id:string;bytes:number};
type Clip={
 id:string;originalName:string;file?:File;previewUrl?:string;status:'uploading'|'uploaded'|'error';progress:number;
 error?:string;remote?:RemoteFile;width?:number;height?:number;duration?:number;bytes?:number;
};
type Stage={id:string;name:string;files:Clip[];collapsed?:boolean};
type Quality='camera4k'|'high'|'fast';
type ResultFile={file:string;size:number;duration:number;sources:Array<{label:string;stage:string;index:number;original:string}>};
type Job={
 id:string;status:string;phase:string;progress:number;total:number;percent:number;quality?:Quality;error?:string|null;
 download_url?:string|null;outputs?:ResultFile[];remove_silence?:boolean;silence_threshold?:number;
};
type Preview={url:string;name:string;meta?:string}|null;

type Draft={
 projectName?:string;mode?:'all'|'random';randomN?:number;quality?:Quality;removeSilence?:boolean;
 silenceThreshold?:number;silencePadding?:number;stages?:Stage[];
};

const API='https://videolabs-worker.onrender.com';
const CLOUD='sskapqzv';
const PRESET='videolabs_upload';
const MAX_BYTES=100*1024*1024;
const DRAFT_KEY='videolabs-draft-v08';
const JOB_KEY='videolabs-job-v08';
const uid=()=>Math.random().toString(36).slice(2)+Date.now().toString(36).slice(-4);
const wait=(ms:number)=>new Promise(r=>setTimeout(r,ms));
const qualityText=(q?:Quality)=>q==='fast'?'720p Rápida':q==='high'?'1080p Alta':'4K Câmera';
const formatBytes=(n=0)=>n>=1024**3?`${(n/1024**3).toFixed(1)} GB`:n>=1024**2?`${(n/1024**2).toFixed(1)} MB`:n>=1024?`${(n/1024).toFixed(0)} KB`:`${n} B`;
const formatDuration=(s=0)=>s>=60?`${Math.floor(s/60)}:${String(Math.round(s%60)).padStart(2,'0')}`:`${s.toFixed(1)}s`;
const stageName=(s:Stage,index:number)=>s.name.trim()||`Etapa ${index+1}`;
const clipLabel=(s:Stage,stageIndex:number,fileIndex:number)=>`${stageName(s,stageIndex)} ${fileIndex+1}`;

const loadDraft=():Draft=>{
 try{return JSON.parse(localStorage.getItem(DRAFT_KEY)||'{}') as Draft}catch{return{}}
};
const stored=loadDraft();
const hydratedStages:Stage[]=(stored.stages||[]).map(s=>({
 ...s,
 files:(s.files||[]).filter(c=>c.remote).map(c=>({...c,file:undefined,previewUrl:c.remote?.url,status:'uploaded',progress:100,error:undefined})),
}));

function App(){
 const[stages,setStages]=useState<Stage[]>(hydratedStages);
 const[projectName,setProjectName]=useState(stored.projectName||'Projeto VideoLabs');
 const[mode,setMode]=useState<'all'|'random'>(stored.mode||'all');
 const[randomN,setRandomN]=useState(stored.randomN||25);
 const[quality,setQuality]=useState<Quality>(stored.quality||'camera4k');
 const[removeSilence,setRemoveSilence]=useState(Boolean(stored.removeSilence));
 const[silenceThreshold,setSilenceThreshold]=useState(stored.silenceThreshold||1);
 const[silencePadding,setSilencePadding]=useState(stored.silencePadding||0.15);
 const[job,setJob]=useState<Job|null>(null);
 const[busy,setBusy]=useState(false);
 const[message,setMessage]=useState('');
 const[preview,setPreview]=useState<Preview>(null);
 const[selected,setSelected]=useState<string[]>([]);
 const[downloadBusy,setDownloadBusy]=useState(false);
 const[dragStage,setDragStage]=useState<string|null>(null);

 const total=useMemo(()=>stages.length>=2&&stages.every(s=>s.files.length)?stages.reduce((n,s)=>n*s.files.length,1):0,[stages]);
 const clips=stages.flatMap(s=>s.files);
 const uploading=clips.filter(c=>c.status==='uploading').length;
 const errors=clips.filter(c=>c.status==='error').length;
 const allReady=Boolean(total)&&clips.length>0&&clips.every(c=>c.status==='uploaded'&&c.remote);
 const aggregateUpload=clips.length?Math.round(clips.reduce((n,c)=>n+c.progress,0)/clips.length):0;
 const outputs=job?.status==='ready'?(job.outputs||[]):[];

 useEffect(()=>{
  const cleanStages=stages.map(stage=>({
   ...stage,
   files:stage.files.filter(c=>c.remote).map(c=>({
    id:c.id,originalName:c.originalName,status:'uploaded' as const,progress:100,remote:c.remote,
    width:c.width,height:c.height,duration:c.duration,bytes:c.bytes,
   })),
  }));
  const draft:Draft={projectName,mode,randomN,quality,removeSilence,silenceThreshold,silencePadding,stages:cleanStages as Stage[]};
  localStorage.setItem(DRAFT_KEY,JSON.stringify(draft));
 },[projectName,mode,randomN,quality,removeSilence,silenceThreshold,silencePadding,stages]);

 useEffect(()=>{
  const oldJob=localStorage.getItem(JOB_KEY);
  if(!oldJob)return;
  void(async()=>{
   try{const r=await fetch(`${API}/v1/jobs/${oldJob}`);if(r.ok)setJob(await r.json());else localStorage.removeItem(JOB_KEY)}catch{}
  })();
 },[]);

 useEffect(()=>{
  if(job?.id)localStorage.setItem(JOB_KEY,job.id);
 },[job?.id]);

 useEffect(()=>{
  if(!job||!['queued','processing'].includes(job.status))return;
  const t=setInterval(async()=>{
   try{
    const r=await fetch(`${API}/v1/jobs/${job.id}`);
    if(r.ok)setJob(await r.json());
    else if(r.status===404){localStorage.removeItem(JOB_KEY);setJob(j=>j?{...j,status:'error',phase:'Processamento interrompido',error:'O servidor reiniciou e este processamento foi perdido. Tente gerar novamente.'}:j)}
   }catch{}
  },2000);
  return()=>clearInterval(t);
 },[job?.id,job?.status]);

 useEffect(()=>{if(job?.status==='ready')setSelected([])},[job?.id,job?.status]);

 const patchClip=(sid:string,cid:string,patch:Partial<Clip>)=>setStages(s=>s.map(stage=>stage.id===sid?{...stage,files:stage.files.map(c=>c.id===cid?{...c,...patch}:c)}:stage));
 const add=()=>setStages(s=>[...s,{id:uid(),name:`Etapa ${s.length+1}`,files:[]}]);
 const moveStage=(id:string,delta:number)=>setStages(current=>{
  const i=current.findIndex(s=>s.id===id),j=i+delta;if(i<0||j<0||j>=current.length)return current;
  const copy=[...current];[copy[i],copy[j]]=[copy[j],copy[i]];return copy;
 });
 const duplicateStage=(id:string)=>setStages(current=>{
  const i=current.findIndex(s=>s.id===id);if(i<0)return current;const source=current[i];
  const copy:Stage={id:uid(),name:`${source.name} cópia`,collapsed:false,files:source.files.filter(c=>c.remote).map(c=>({...c,id:uid(),file:undefined,previewUrl:c.remote?.url,status:'uploaded',progress:100}))};
  return [...current.slice(0,i+1),copy,...current.slice(i+1)];
 });
 const deleteStage=(id:string)=>setStages(current=>current.filter(s=>s.id!==id));
 const removeFile=(sid:string,cid:string)=>setStages(current=>current.map(stage=>{
  if(stage.id!==sid)return stage;
  const clip=stage.files.find(c=>c.id===cid);if(clip?.previewUrl?.startsWith('blob:'))URL.revokeObjectURL(clip.previewUrl);
  return{...stage,files:stage.files.filter(c=>c.id!==cid)};
 }));
 const dropStage=(targetId:string)=>{
  if(!dragStage||dragStage===targetId)return setDragStage(null);
  setStages(current=>{const from=current.findIndex(s=>s.id===dragStage),to=current.findIndex(s=>s.id===targetId);if(from<0||to<0)return current;const copy=[...current];const[item]=copy.splice(from,1);copy.splice(to,0,item);return copy});
  setDragStage(null);
 };

 const readMeta=(file:File,url:string)=>new Promise<{width:number;height:number;duration:number}>((resolve)=>{
  const video=document.createElement('video');video.preload='metadata';video.src=url;
  video.onloadedmetadata=()=>resolve({width:video.videoWidth,height:video.videoHeight,duration:Number.isFinite(video.duration)?video.duration:0});
  video.onerror=()=>resolve({width:0,height:0,duration:0});
 });

 const xhrUpload=(file:File,onProgress:(p:number)=>void)=>new Promise<RemoteFile>((resolve,reject)=>{
  const form=new FormData();form.append('file',file);form.append('upload_preset',PRESET);
  const xhr=new XMLHttpRequest();xhr.open('POST',`https://api.cloudinary.com/v1_1/${CLOUD}/video/upload`);xhr.timeout=300000;
  xhr.upload.onprogress=e=>{if(e.lengthComputable)onProgress(Math.min(99,Math.round(e.loaded/e.total*100)))};
  xhr.onerror=()=>reject(new Error(`Falha de rede ao enviar ${file.name}.`));
  xhr.ontimeout=()=>reject(new Error(`O upload de ${file.name} demorou demais.`));
  xhr.onload=()=>{let data:any={};try{data=JSON.parse(xhr.responseText||'{}')}catch{}if(xhr.status>=200&&xhr.status<300&&data.secure_url){onProgress(100);resolve({name:file.name,url:data.secure_url,public_id:data.public_id,bytes:data.bytes})}else reject(new Error(data?.error?.message||`Cloudinary recusou ${file.name} (${xhr.status||'sem resposta'}).`))};
  xhr.send(form);
 });

 const uploadClip=async(sid:string,clip:Clip)=>{
  if(!clip.file)return;
  if(clip.file.size>MAX_BYTES){patchClip(sid,clip.id,{status:'error',progress:0,error:'Arquivo acima de 100 MB.'});return}
  patchClip(sid,clip.id,{status:'uploading',progress:0,error:undefined});let last:Error|undefined;
  for(let attempt=1;attempt<=3;attempt++){
   try{
    if(attempt>1){patchClip(sid,clip.id,{progress:0});await wait(800*attempt)}
    const remote=await xhrUpload(clip.file,p=>patchClip(sid,clip.id,{progress:p}));
    patchClip(sid,clip.id,{status:'uploaded',progress:100,remote,bytes:remote.bytes,error:undefined});return;
   }catch(e){last=e instanceof Error?e:new Error('Falha no upload.')}
  }
  patchClip(sid,clip.id,{status:'error',progress:0,error:last?.message||'Falha no upload.'});
  setMessage(`${clip.originalName}: falha no upload após 3 tentativas.`);
 };

 const change=async(id:string,list:FileList|null)=>{
  if(!list?.length)return;setMessage('');
  const incoming=Array.from(list).map(file=>({id:uid(),originalName:file.name,file,previewUrl:URL.createObjectURL(file),status:'uploading' as const,progress:0,bytes:file.size}));
  setStages(s=>s.map(x=>x.id===id?{...x,files:[...x.files,...incoming]}:x));
  incoming.forEach(clip=>{void readMeta(clip.file!,clip.previewUrl!).then(meta=>patchClip(id,clip.id,meta))});
  for(let i=0;i<incoming.length;i+=2)await Promise.all(incoming.slice(i,i+2).map(clip=>uploadClip(id,clip)));
 };

 const retryClip=async(sid:string,cid:string)=>{const clip=stages.find(s=>s.id===sid)?.files.find(c=>c.id===cid);if(!clip?.file)return;setMessage('');await uploadClip(sid,clip)};

 const generate=async()=>{
  if(!allReady)return;setBusy(true);setJob(null);setMessage('Criando processamento...');localStorage.removeItem(JOB_KEY);
  try{
   const uploaded=stages.map((s,i)=>({name:stageName(s,i),files:s.files.map(c=>({...c.remote!,name:c.originalName,width:c.width,height:c.height,duration:c.duration}))}));
   const r=await fetch(`${API}/v1/jobs/remote`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
    projectName,mode,requested:randomN,quality,removeSilence,silenceThreshold,silencePadding,stages:uploaded,
   })});
   let data:any={};try{data=await r.json()}catch{}
   if(!r.ok)throw new Error(data.detail||`Falha ao criar processamento (${r.status}).`);
   setJob(data);setMessage('');
  }catch(e){setMessage(e instanceof Error?`Render: ${e.message}`:'Erro inesperado ao iniciar o processamento.')}finally{setBusy(false)}
 };

 const reset=()=>{
  stages.flatMap(s=>s.files).forEach(c=>{if(c.previewUrl?.startsWith('blob:'))URL.revokeObjectURL(c.previewUrl)});
  setStages([]);setJob(null);setMessage('');setProjectName('Projeto VideoLabs');setMode('all');setRandomN(25);setQuality('camera4k');setRemoveSilence(false);setSilenceThreshold(1);setSilencePadding(0.15);setSelected([]);
  localStorage.removeItem(DRAFT_KEY);localStorage.removeItem(JOB_KEY);
 };

 const openSourcePreview=(clip:Clip,label:string)=>{
  const url=clip.previewUrl||clip.remote?.url;if(!url)return;
  const dimensions=clip.width&&clip.height?`${clip.width}×${clip.height}`:'';
  const meta=[dimensions,clip.duration?formatDuration(clip.duration):'',formatBytes(clip.bytes||clip.remote?.bytes||0)].filter(Boolean).join(' • ');
  setPreview({url,name:label,meta});
 };
 const openResultPreview=(file:ResultFile)=>setPreview({url:`${API}/v1/jobs/${job!.id}/outputs/${encodeURIComponent(file.file)}/preview`,name:file.file,meta:`${formatDuration(file.duration)} • ${formatBytes(file.size)}`});

 const toggleSelected=(name:string)=>setSelected(current=>current.includes(name)?current.filter(x=>x!==name):[...current,name]);
 const toggleAll=()=>setSelected(current=>current.length===outputs.length?[]:outputs.map(o=>o.file));
 const downloadSelected=async()=>{
  if(!job||!selected.length)return;setDownloadBusy(true);
  try{
   const r=await fetch(`${API}/v1/jobs/${job.id}/download-selected`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({files:selected})});
   if(!r.ok){let d:any={};try{d=await r.json()}catch{}throw new Error(d.detail||`Falha no download (${r.status}).`)}
   const blob=await r.blob();const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=`${projectName||'VideoLabs'}-selecionados.zip`;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),5000);
  }catch(e){setMessage(e instanceof Error?e.message:'Falha ao baixar selecionados.')}finally{setDownloadBusy(false)}
 };

 return <main>
  <header><div className="brand"><span><Film size={20}/></span>VideoLabs</div><button className="ghost" onClick={reset}>Novo projeto</button></header>
  <section className="hero"><div className="pill"><Sparkles size={14}/> COMBINAÇÕES AUTOMÁTICAS</div><h1>Um upload.<br/><em>Centenas de vídeos.</em></h1><p>Organize as etapas, preserve o 4K da câmera, remova pausas e baixe cada combinação do seu jeito.</p></section>

  <section className="workspace">
   <div className="projectBar">
    <label><small>NOME DO PROJETO</small><input value={projectName} onChange={e=>setProjectName(e.target.value)} placeholder="Ex.: Campanha Setembro"/></label>
    <div className="modes"><button className={mode==='all'?'active':''} onClick={()=>setMode('all')}>Todas</button><button className={mode==='random'?'active':''} onClick={()=>setMode('random')}><Shuffle size={14}/> Aleatórias</button>{mode==='random'&&<input type="number" min="1" max="625" value={randomN} onChange={e=>setRandomN(Math.max(1,Number(e.target.value)||1))}/>}</div>
   </div>

   <div className="settingsGrid">
    <div className="settingCard">
     <div><small>QUALIDADE DE SAÍDA</small><strong>{quality==='camera4k'?'4K — câmera':quality==='high'?'1080p — alta':'720p — rápida'}</strong></div>
     <div className="qualityOptions"><button className={quality==='camera4k'?'active':''} onClick={()=>setQuality('camera4k')}><Sparkles size={15}/><span><b>4K</b><small>Câmera</small></span></button><button className={quality==='high'?'active':''} onClick={()=>setQuality('high')}><Clapperboard size={15}/><span><b>1080p</b><small>Alta</small></span></button><button className={quality==='fast'?'active':''} onClick={()=>setQuality('fast')}><Gauge size={15}/><span><b>720p</b><small>Rápida</small></span></button></div>
    </div>
    <div className={`settingCard silenceCard ${removeSilence?'enabled':''}`}>
     <div><small>REMOVER SILÊNCIO</small><strong>{removeSilence?`Pausas ≥ ${silenceThreshold}s`:'Desativado'}</strong><p>{removeSilence?'O vídeo é recodificado para cortar as pausas com precisão.':'Mantém o Fast Mode quando os clipes forem compatíveis.'}</p></div>
     <div className="silenceControls"><button className={`toggle ${removeSilence?'on':''}`} onClick={()=>setRemoveSilence(v=>!v)} aria-label="Alternar remoção de silêncio"><i/></button>{removeSilence&&<><label><span>Pausa mínima</span><select value={silenceThreshold} onChange={e=>setSilenceThreshold(Number(e.target.value))}><option value={0.5}>0,5s</option><option value={1}>1s</option><option value={1.5}>1,5s</option><option value={2}>2s</option></select></label><label><span>Respiro</span><select value={silencePadding} onChange={e=>setSilencePadding(Number(e.target.value))}><option value={0.1}>0,10s</option><option value={0.15}>0,15s</option><option value={0.25}>0,25s</option></select></label></>}</div>
    </div>
   </div>

   <div className="sectionHead"><div><small>ESTRUTURA</small><h2>Etapas do vídeo</h2></div><button className="add" onClick={add}><Plus size={17}/> Adicionar etapa</button></div>

   {stages.length===0?<div className="emptyState"><div><Plus size={22}/></div><strong>Nenhuma etapa ainda</strong><p>Adicione as etapas na ordem final. Ex.: Hook, Corpo e CTA.</p><button className="add" onClick={add}><Plus size={17}/> Adicionar primeira etapa</button></div>:
   <div className="stages">{stages.map((s,si)=><article className={`stage ${dragStage===s.id?'dragging':''}`} key={s.id} draggable onDragStart={()=>setDragStage(s.id)} onDragOver={e=>e.preventDefault()} onDrop={()=>dropStage(s.id)}>
    <div className="stageTop"><b>{String(si+1).padStart(2,'0')}</b><input value={s.name} onChange={e=>setStages(a=>a.map(x=>x.id===s.id?{...x,name:e.target.value}:x))} placeholder={`Etapa ${si+1}`}/><div className="stageActions"><button disabled={si===0} title="Subir" onClick={()=>moveStage(s.id,-1)}><ArrowUp size={14}/></button><button disabled={si===stages.length-1} title="Descer" onClick={()=>moveStage(s.id,1)}><ArrowDown size={14}/></button><button title="Duplicar" onClick={()=>duplicateStage(s.id)}><Copy size={14}/></button><button title={s.collapsed?'Expandir':'Recolher'} onClick={()=>setStages(a=>a.map(x=>x.id===s.id?{...x,collapsed:!x.collapsed}:x))}>{s.collapsed?<ChevronDown size={14}/>:<ChevronUp size={14}/>}</button><button title="Excluir etapa" onClick={()=>deleteStage(s.id)}><Trash2 size={14}/></button></div></div>
    {!s.collapsed&&<><div className="nameExample">Nomes finais: <b>{stageName(s,si)} 1</b>, <b>{stageName(s,si)} 2</b>...</div><label className="drop"><Upload size={20}/><strong>{s.files.length?`${s.files.length} vídeo(s) adicionado(s)`:'Adicionar vídeos'}</strong><span>MP4, MOV, WebM, M4V • até 100 MB cada</span><input hidden multiple type="file" accept="video/*" onChange={e=>{void change(s.id,e.target.files);e.currentTarget.value=''}}/></label>
     {s.files.length>0&&<div className="files">{s.files.map((c,fi)=><div className="fileRow" key={c.id}><div className="derivedName">{clipLabel(s,si,fi)}</div><div className="fileMeta"><span title={c.originalName}>{c.originalName}</span><em>{[c.width&&c.height?`${c.width}×${c.height}`:'',c.duration?formatDuration(c.duration):'',formatBytes(c.bytes||c.remote?.bytes||0),c.status==='uploaded'?'enviado':c.status==='error'?'erro':`${c.progress}%`].filter(Boolean).join(' • ')}</em></div><button title="Preview" onClick={()=>openSourcePreview(c,clipLabel(s,si,fi))}><Play size={13}/></button>{c.status==='error'&&c.file&&<button title="Tentar novamente" onClick={()=>void retryClip(s.id,c.id)}><RotateCcw size={13}/></button>}<button title="Remover" onClick={()=>removeFile(s.id,c.id)}>×</button></div>)}</div>}</>}
   </article>)}</div>}

   {job?<div className={`job ${job.status}`}><div className="jobTop"><div><small>PROCESSAMENTO</small><strong>{job.phase||job.status}</strong></div><b>{job.percent||0}%</b></div><div className="progress"><i style={{width:`${job.percent||0}%`}}/></div><p>{job.status==='error'?(job.error||'Falha no processamento.'):job.status==='ready'?`${job.total} vídeo(s) finalizado(s) em ${qualityText(job.quality)}${job.remove_silence?` • silêncios ≥ ${job.silence_threshold}s removidos`:''}.`:`${job.progress||0} de ${job.total||0} combinações concluídas.`}</p></div>:
   <div className="summary"><div><small>{uploading?'UPLOAD':errors?'UPLOAD COM ERRO':'COMBINAÇÕES'}</small><strong>{uploading?`${aggregateUpload}%`:total.toLocaleString('pt-BR')}</strong><span>{uploading?`${uploading} arquivo(s) enviando...`:errors?`${errors} arquivo(s) precisam ser reenviados`:stages.length<2?'adicione pelo menos 2 etapas':allReady?'vídeos possíveis':total?'aguardando uploads':'adicione vídeos nas etapas'}</span></div><button disabled={!allReady||busy} onClick={generate}>{busy?<LoaderCircle className="spin" size={17}/>:uploading?<LoaderCircle className="spin" size={17}/>:removeSilence?<Scissors size={17}/>:null}{busy?'Iniciando...':uploading?'Enviando...':mode==='random'?`Gerar ${Math.min(randomN,total||randomN)}`:'Gerar combinações'} <span>→</span></button></div>}

   {job?.status==='ready'&&<section className="results">
    <div className="resultsHead"><div><small>RESULTADOS</small><h2>{outputs.length} vídeos prontos</h2><p>Assista antes, baixe um por um, selecione alguns ou leve tudo em ZIP.</p></div><div className="resultActions"><button className="secondary" onClick={toggleAll}>{selected.length===outputs.length&&outputs.length?<CheckSquare size={16}/>:<Square size={16}/>} {selected.length===outputs.length&&outputs.length?'Desmarcar todos':'Selecionar todos'}</button><button className="secondary" disabled={!selected.length||downloadBusy} onClick={()=>void downloadSelected()}>{downloadBusy?<LoaderCircle className="spin" size={16}/>:<Download size={16}/>} Baixar selecionados {selected.length?`(${selected.length})`:''}</button><a className="download" href={`${API}${job.download_url}`}><Download size={16}/> Baixar todos</a></div></div>
    <div className="resultGrid">{outputs.map(file=><article className={`resultCard ${selected.includes(file.file)?'selected':''}`} key={file.file}><button className="selectBox" title="Selecionar" onClick={()=>toggleSelected(file.file)}>{selected.includes(file.file)?<Check size={15}/>:null}</button><button className="resultPreview" onClick={()=>openResultPreview(file)}><span><Play size={22}/></span><small>Pré-visualizar</small></button><div className="resultInfo"><strong title={file.file}>{file.file}</strong><span>{formatDuration(file.duration)} • {formatBytes(file.size)}</span></div><div className="resultButtons"><button onClick={()=>openResultPreview(file)}><Play size={14}/> Ver</button><a href={`${API}/v1/jobs/${job.id}/outputs/${encodeURIComponent(file.file)}/download`}><Download size={14}/> Baixar</a></div></article>)}</div>
   </section>}

   {message&&<div className="notice">{message}</div>}
  </section>

  {preview&&<div className="modal" onClick={()=>setPreview(null)}><div className="modalCard" onClick={e=>e.stopPropagation()}><button className="modalClose" onClick={()=>setPreview(null)}><X size={18}/></button><div className="modalTitle"><strong>{preview.name}</strong>{preview.meta&&<span>{preview.meta}</span>}</div><video src={preview.url} controls autoPlay playsInline preload="metadata"/></div></div>}

  <footer>VideoLabs • V0.8</footer>
 </main>
}

createRoot(document.getElementById('root')!).render(<App/>);
