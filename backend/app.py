from __future__ import annotations
import itertools,json,os,random,re,shutil,subprocess,tempfile,threading,time,uuid,zipfile,urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import imageio_ffmpeg
from fastapi import FastAPI,HTTPException,Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

APP_NAME='VideoLabs Worker';ROOT=Path(os.getenv('VIDEOLABS_TMP',tempfile.gettempdir()))/'videolabs';ROOT.mkdir(parents=True,exist_ok=True)
MAX_COMBINATIONS=int(os.getenv('MAX_COMBINATIONS','625'));JOB_TTL_SECONDS=int(os.getenv('JOB_TTL_SECONDS','7200'));TARGET_WIDTH=720;TARGET_HEIGHT=1280;TARGET_FPS=30;FFMPEG=imageio_ffmpeg.get_ffmpeg_exe()
app=FastAPI(title=APP_NAME,version='0.4.0');app.add_middleware(CORSMiddleware,allow_origins=['https://videolabs.vercel.app','http://localhost:5173'],allow_origin_regex=r'https://.*\.vercel\.app',allow_credentials=False,allow_methods=['*'],allow_headers=['*'])
jobs={};lock=threading.Lock();executor=ThreadPoolExecutor(max_workers=1)
def safe_name(v):return re.sub(r'[^a-zA-Z0-9._-]+','-',str(v).strip()).strip('-_')[:80] or 'clip'
def state_path(i):return ROOT/i/'job.json'
def persist(i):
 with lock:
  j=jobs.get(i)
  if not j:return
  data={k:v for k,v in j.items() if k!='zip_path'}
  if j.get('zip_path'):data['zip_path']=j['zip_path']
 try:state_path(i).write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
 except:pass
def set_job(i,**u):
 with lock:
  if i in jobs:jobs[i].update(u)
 persist(i)
def restore_job(i):
 p=state_path(i)
 if not p.exists():return None
 try:
  j=json.loads(p.read_text(encoding='utf-8'))
  if time.time()-j.get('created_at',0)>JOB_TTL_SECONDS:return None
  with lock:jobs[i]=j
  return j
 except:return None
def public_job(i):
 with lock:j=jobs.get(i)
 if not j:j=restore_job(i)
 if not j:raise HTTPException(404,'Job não encontrado ou expirado.')
 return {'id':i,'status':j['status'],'phase':j.get('phase',''),'progress':j.get('progress',0),'total':j.get('total',0),'percent':round(j.get('overall_percent',j.get('progress',0)/max(j.get('total',1),1)*100),1),'error':j.get('error'),'created_at':j['created_at'],'expires_at':j['created_at']+JOB_TTL_SECONDS,'download_url':f'/v1/jobs/{i}/download' if j['status']=='ready' else None}
def probe(p):
 r=subprocess.run([FFMPEG,'-hide_banner','-i',str(p)],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True).stderr
 v=re.search(r'Video:\s*([^,]+).*?,\s*(\d{2,5})x(\d{2,5}).*?(\d+(?:\.\d+)?) fps',r);a=re.search(r'Audio:\s*([^,]+),\s*(\d+) Hz',r)
 return (v.group(1).strip() if v else '',int(v.group(2)) if v else 0,int(v.group(3)) if v else 0,round(float(v.group(4)),2) if v else 0,a.group(1).strip() if a else '',int(a.group(2)) if a else 0,bool(a))
def compatible(probes):
 if not probes or any(not p[0] for p in probes):return False
 first=probes[0]
 return all(p==first for p in probes) and first[0] in ('h264','avc1') and first[6]
def has_audio(p):return b'Audio:' in subprocess.run([FFMPEG,'-hide_banner','-i',str(p)],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE).stderr
def normalize(src,dst):
 vf=f'scale={TARGET_WIDTH}:{TARGET_HEIGHT}:force_original_aspect_ratio=decrease,pad={TARGET_WIDTH}:{TARGET_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps={TARGET_FPS}'
 cmd=[FFMPEG,'-hide_banner','-loglevel','error','-y','-i',str(src)]
 if has_audio(src):cmd+=['-vf',vf,'-c:v','libx264','-preset','ultrafast','-crf','25','-pix_fmt','yuv420p','-c:a','aac','-b:a','128k','-ar','48000','-ac','2',str(dst)]
 else:cmd+=['-f','lavfi','-i','anullsrc=channel_layout=stereo:sample_rate=48000','-map','0:v:0','-map','1:a:0','-shortest','-vf',vf,'-c:v','libx264','-preset','ultrafast','-crf','25','-pix_fmt','yuv420p','-c:a','aac','-b:a','128k','-ar','48000','-ac','2',str(dst)]
 subprocess.run(cmd,check=True)
def concat(combo,out):
 lf=out.with_suffix('.txt');lf.write_text('\n'.join("file '"+str(p.resolve()).replace("'","'\\''")+"'" for p in combo)+'\n')
 try:subprocess.run([FFMPEG,'-hide_banner','-loglevel','error','-y','-f','concat','-safe','0','-i',str(lf),'-c','copy','-movflags','+faststart',str(out)],check=True)
 finally:lf.unlink(missing_ok=True)
def download(url,target):
 req=urllib.request.Request(url,headers={'User-Agent':'VideoLabs/0.4'});total=0
 with urllib.request.urlopen(req,timeout=180) as r,target.open('wb') as f:
  while True:
   b=r.read(2*1024*1024)
   if not b:break
   total+=len(b)
   if total>105*1024*1024:raise RuntimeError('Um vídeo remoto excede o limite atual de 100 MB.')
   f.write(b)
def process_job(i):
 with lock:j=jobs[i];wd=Path(j['workdir']);stages=j['stages'];mode=j['mode'];requested=j['requested']
 try:
  inputs=wd/'inputs';norm=wd/'normalized';inputs.mkdir(exist_ok=True);norm.mkdir(exist_ok=True);raw=[];n=sum(len(s['files']) for s in stages);done=0
  for si,s in enumerate(stages):
   arr=[]
   for fi,f in enumerate(s['files']):
    set_job(i,status='processing',phase=f'Baixando vídeo {done+1}/{n}',overall_percent=round(done/max(n,1)*20,1))
    ext=Path(f.get('name','video.mp4')).suffix or '.mp4';src=inputs/f's{si:02d}_f{fi:03d}{ext}';download(f['url'],src);arr.append(src);done+=1
   raw.append(arr)
  flat=[p for a in raw for p in a];set_job(i,phase='Verificando compatibilidade',overall_percent=20)
  ps=[probe(p) for p in flat];fast=compatible(ps);local=[]
  if fast:
   local=raw;set_job(i,phase='Fast Mode: vídeos compatíveis',overall_percent=25)
  else:
   done=0
   for si,arr in enumerate(raw):
    out=[]
    for fi,src in enumerate(arr):
     set_job(i,phase=f'Preparando vídeo {done+1}/{n}',overall_percent=20+round(done/max(n,1)*30,1));dst=norm/f's{si:02d}_f{fi:03d}.mp4';normalize(src,dst);out.append(dst);done+=1
    local.append(out)
  combos=list(itertools.product(*local));selected=random.Random(i).sample(combos,min(max(1,requested),len(combos))) if mode=='random' else combos
  if len(selected)>MAX_COMBINATIONS:raise RuntimeError(f'Este worker aceita no máximo {MAX_COMBINATIONS} combinações por job.')
  set_job(i,total=len(selected),progress=0,phase='Gerando combinações',overall_percent=25 if fast else 50);zp=wd/'VideoLabs-resultados.zip';manifest={'project':j['project_name'],'mode':mode,'fast_mode':fast,'total_available':len(combos),'generated':len(selected),'stages':[{'name':s['name'],'files':[f['name'] for f in s['files']]} for s in stages],'outputs':[]}
  base=25 if fast else 50
  with zipfile.ZipFile(zp,'w',compression=zipfile.ZIP_STORED,allowZip64=True) as z:
   for x,c in enumerate(selected,1):
    out=wd/f'VideoLabs-{x:04d}.mp4';concat(c,out);z.write(out,arcname=out.name);manifest['outputs'].append({'file':out.name,'sources':[p.name for p in c]});out.unlink(missing_ok=True);set_job(i,progress=x,phase=f'Gerando {x}/{len(selected)}',overall_percent=base+round(x/max(len(selected),1)*(100-base),1))
   z.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2))
  shutil.rmtree(inputs,ignore_errors=True);shutil.rmtree(norm,ignore_errors=True);set_job(i,status='ready',phase='Pronto para baixar',progress=len(selected),overall_percent=100,zip_path=str(zp))
 except Exception as e:set_job(i,status='error',phase='Falha no processamento',error=str(e))
def cleanup():
 while True:
  now=time.time()
  with lock:expired=[(i,j.get('workdir')) for i,j in jobs.items() if now-j['created_at']>JOB_TTL_SECONDS]
  for i,w in expired:
   with lock:jobs.pop(i,None)
   if w:shutil.rmtree(w,ignore_errors=True)
  time.sleep(600)
threading.Thread(target=cleanup,daemon=True).start()
@app.get('/')
def root():return {'name':APP_NAME,'status':'ok','version':'0.4.0'}
@app.get('/health')
def health():return {'ok':True,'ffmpeg':Path(FFMPEG).name,'version':'0.4.0'}
@app.post('/v1/jobs/remote')
async def create_remote(request:Request):
 m=await request.json();stages=m.get('stages') or []
 if len(stages)<2:raise HTTPException(400,'Adicione pelo menos duas etapas.')
 for s in stages:
  if not s.get('files'):raise HTTPException(400,f"A etapa '{s.get('name','Etapa')}' não possui vídeos.")
  for f in s['files']:
   if not str(f.get('url','')).startswith('https://res.cloudinary.com/sskapqzv/'):raise HTTPException(400,'Origem de vídeo não permitida.')
 total=1
 for s in stages:total*=len(s['files'])
 mode=m.get('mode','all');requested=int(m.get('requested',25))
 if mode=='all' and total>MAX_COMBINATIONS:raise HTTPException(400,f'Há {total} combinações. O limite é {MAX_COMBINATIONS}.')
 i=uuid.uuid4().hex[:12];wd=ROOT/i;wd.mkdir(parents=True,exist_ok=True)
 with lock:jobs[i]={'status':'queued','phase':'Na fila','progress':0,'total':total if mode=='all' else min(requested,total),'overall_percent':0,'error':None,'created_at':time.time(),'workdir':str(wd),'stages':stages,'mode':mode,'requested':requested,'project_name':safe_name(m.get('projectName','Projeto VideoLabs'))}
 persist(i);executor.submit(process_job,i);return public_job(i)
@app.get('/v1/jobs/{job_id}')
def get_job(job_id:str):return public_job(job_id)
@app.get('/v1/jobs/{job_id}/download')
def download_job(job_id:str):
 with lock:j=jobs.get(job_id)
 if not j:j=restore_job(job_id)
 if not j:raise HTTPException(404,'Job não encontrado ou expirado.')
 if j['status']!='ready':raise HTTPException(409,'O processamento ainda não terminou.')
 p=Path(j['zip_path']);name=j['project_name']
 if not p.exists():raise HTTPException(404,'Arquivo final não está mais disponível.')
 return FileResponse(p,media_type='application/zip',filename=f'{name}-VideoLabs.zip')
