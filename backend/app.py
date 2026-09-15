from __future__ import annotations
import itertools,json,os,random,re,shutil,subprocess,tempfile,threading,time,uuid,zipfile,urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import imageio_ffmpeg
from fastapi import FastAPI,HTTPException,Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

APP_NAME='VideoLabs Worker'
ROOT=Path(os.getenv('VIDEOLABS_TMP',tempfile.gettempdir()))/'videolabs'; ROOT.mkdir(parents=True,exist_ok=True)
MAX_COMBINATIONS=int(os.getenv('MAX_COMBINATIONS','625')); JOB_TTL_SECONDS=int(os.getenv('JOB_TTL_SECONDS','7200'))
TARGET_FPS=30; FFMPEG=imageio_ffmpeg.get_ffmpeg_exe()
PROFILES={
    'high':{'width':1080,'height':1920,'crf':'21','audio':'128k','label':'1080p Alta'},
    'fast':{'width':720,'height':1280,'crf':'23','audio':'112k','label':'720p Rápida'},
}
app=FastAPI(title=APP_NAME,version='0.6.0')
app.add_middleware(CORSMiddleware,allow_origins=['https://videolabs.vercel.app','http://localhost:5173'],allow_origin_regex=r'https://.*\.vercel\.app',allow_credentials=False,allow_methods=['*'],allow_headers=['*'])
jobs={}; lock=threading.Lock(); executor=ThreadPoolExecutor(max_workers=1)

def safe_name(v):
    s=re.sub(r'[^a-zA-Z0-9._-]+','-',str(v).strip()).strip('-_')
    return s[:80] or 'clip'
def state_path(i): return ROOT/i/'job.json'
def persist(i):
    with lock:
        j=jobs.get(i)
        if not j:return
        data=dict(j)
    try:
        state_path(i).parent.mkdir(parents=True,exist_ok=True)
        tmp=state_path(i).with_suffix('.tmp'); tmp.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8'); tmp.replace(state_path(i))
    except Exception: pass

def set_job(i,**u):
    with lock:
        if i in jobs: jobs[i].update(u)
    persist(i)

def restore_job(i):
    p=state_path(i)
    if not p.exists(): return None
    try:
        j=json.loads(p.read_text(encoding='utf-8'))
        if time.time()-j.get('created_at',0)>JOB_TTL_SECONDS:return None
        with lock: jobs[i]=j
        return j
    except Exception:return None

def public_job(i):
    with lock:j=jobs.get(i)
    if not j:j=restore_job(i)
    if not j:raise HTTPException(404,'Job não encontrado ou expirado.')
    return {'id':i,'status':j['status'],'phase':j.get('phase',''),'progress':j.get('progress',0),'total':j.get('total',0),'percent':round(j.get('overall_percent',0),1),'error':j.get('error'),'quality':j.get('quality','high'),'created_at':j['created_at'],'expires_at':j['created_at']+JOB_TTL_SECONDS,'download_url':f'/v1/jobs/{i}/download' if j['status']=='ready' else None}

def run(args):
    # Keep filters conservative on the 512 MB free instance. Encoder threads are capped separately.
    full=[FFMPEG,'-hide_banner','-loglevel','error','-filter_threads','1']+args
    r=subprocess.run(full,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    if r.returncode:
        msg=r.stderr.decode('utf-8','ignore').strip()
        raise RuntimeError(msg[-1200:] or f'FFmpeg saiu com código {r.returncode}.')

def probe(p):
    r=subprocess.run([FFMPEG,'-hide_banner','-threads','1','-i',str(p)],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True).stderr
    v=re.search(r'Video:\s*([^,]+).*?,\s*(\d{2,5})x(\d{2,5}).*?(\d+(?:\.\d+)?) fps',r); a=re.search(r'Audio:\s*([^,]+),\s*(\d+) Hz',r)
    return (v.group(1).strip() if v else '',int(v.group(2)) if v else 0,int(v.group(3)) if v else 0,round(float(v.group(4)),2) if v else 0,a.group(1).strip() if a else '',int(a.group(2)) if a else 0,bool(a))

def exact_compatible(ps):
    if not ps or any(not p[0] for p in ps):return False
    first=ps[0]
    return all(p==first for p in ps) and first[0] in ('h264','avc1') and first[6]

def video_compatible(ps):
    if not ps or any(not p[0] for p in ps):return False
    first=ps[0]
    base=first[:4]
    return first[0] in ('h264','avc1') and all(p[:4]==base for p in ps)

def has_audio(p):
    return b'Audio:' in subprocess.run([FFMPEG,'-hide_banner','-threads','1','-i',str(p)],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE).stderr

def normalize(src,dst,quality):
    q=PROFILES.get(quality,PROFILES['high'])
    vf=f"scale={q['width']}:{q['height']}:force_original_aspect_ratio=decrease:flags=bilinear,pad={q['width']}:{q['height']}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps={TARGET_FPS}"
    args=['-y','-i',str(src)]
    enc=['-vf',vf,'-c:v','libx264','-preset','ultrafast','-crf',q['crf'],'-pix_fmt','yuv420p','-threads','2','-c:a','aac','-b:a',q['audio'],'-ar','48000','-ac','2','-movflags','+faststart']
    if has_audio(src):
        args+=enc+[str(dst)]
    else:
        args+=['-f','lavfi','-i','anullsrc=channel_layout=stereo:sample_rate=48000','-map','0:v:0','-map','1:a:0','-shortest']+enc+[str(dst)]
    run(args)

def normalize_audio_only(src,dst):
    if has_audio(src):
        run(['-y','-i',str(src),'-c:v','copy','-c:a','aac','-b:a','128k','-ar','48000','-ac','2','-movflags','+faststart',str(dst)])
    else:
        run(['-y','-i',str(src),'-f','lavfi','-i','anullsrc=channel_layout=stereo:sample_rate=48000','-map','0:v:0','-map','1:a:0','-shortest','-c:v','copy','-c:a','aac','-b:a','128k','-ar','48000','-ac','2','-movflags','+faststart',str(dst)])

def concat(combo,out):
    lf=out.with_suffix('.txt'); lf.write_text('\n'.join("file '"+str(item['path'].resolve()).replace("'","'\\''")+"'" for item in combo)+'\n')
    try: run(['-y','-f','concat','-safe','0','-i',str(lf),'-c','copy','-movflags','+faststart',str(out)])
    finally: lf.unlink(missing_ok=True)

def download(url,target):
    req=urllib.request.Request(url,headers={'User-Agent':'VideoLabs/0.6'}); total=0
    with urllib.request.urlopen(req,timeout=180) as r,target.open('wb') as f:
        while True:
            b=r.read(256*1024)
            if not b:break
            total+=len(b)
            if total>105*1024*1024:raise RuntimeError('Um vídeo remoto excede o limite atual de 100 MB.')
            f.write(b)

def output_name(combo,seen):
    base='-'.join(safe_name(item.get('label') or Path(item.get('original_name','video')).stem) for item in combo)
    base=base[:180] or 'VideoLabs'
    n=seen.get(base,0)+1; seen[base]=n
    return f'{base}.mp4' if n==1 else f'{base}-{n}.mp4'

def process_job(i):
    with lock:
        j=jobs.get(i)
        if not j:return
        wd=Path(j['workdir']); stages=j['stages']; mode=j['mode']; requested=j['requested']; quality=j.get('quality','high')
    try:
        inputs=wd/'inputs'; prepared=wd/'prepared'
        shutil.rmtree(inputs,ignore_errors=True); shutil.rmtree(prepared,ignore_errors=True)
        for old in wd.glob('*.mp4'):old.unlink(missing_ok=True)
        (wd/'VideoLabs-resultados.zip').unlink(missing_ok=True)
        inputs.mkdir(exist_ok=True); prepared.mkdir(exist_ok=True)
        set_job(i,status='processing',phase='Preparando processamento',error=None,progress=0,overall_percent=1)

        raw=[]; n=sum(len(s['files']) for s in stages); done=0
        for si,s in enumerate(stages):
            arr=[]
            for fi,f in enumerate(s['files']):
                set_job(i,phase=f'Baixando vídeo {done+1}/{n}',overall_percent=2+round(done/max(n,1)*18,1))
                ext=Path(f.get('name','video.mp4')).suffix or '.mp4'; src=inputs/f's{si:02d}_f{fi:03d}{ext}'
                download(f['url'],src)
                arr.append({'path':src,'label':safe_name(f.get('label') or f"{safe_name(s.get('name','V'))}{fi+1}"),'original_name':f.get('name','video.mp4')})
                done+=1
            raw.append(arr)

        flat=[item for a in raw for item in a]
        set_job(i,phase='Verificando compatibilidade',overall_percent=20)
        ps=[probe(item['path']) for item in flat]
        exact=exact_compatible(ps); video_ok=video_compatible(ps)
        local=[]
        prep_mode='copy' if exact else 'audio' if video_ok else 'normalize'

        if exact:
            local=raw
            set_job(i,phase='Fast Mode: qualidade original',overall_percent=28)
        else:
            done=0
            for si,arr in enumerate(raw):
                out=[]
                for fi,item in enumerate(arr):
                    if prep_mode=='audio':
                        phase=f'Ajustando áudio {done+1}/{n}'
                        pct=20+round(done/max(n,1)*18,1)
                    else:
                        q=PROFILES.get(quality,PROFILES['high'])
                        phase=f"Preparando {q['label']} {done+1}/{n}"
                        pct=20+round(done/max(n,1)*30,1)
                    set_job(i,phase=phase,overall_percent=pct)
                    dst=prepared/f's{si:02d}_f{fi:03d}.mp4'
                    if prep_mode=='audio': normalize_audio_only(item['path'],dst)
                    else: normalize(item['path'],dst,quality)
                    out.append({**item,'path':dst})
                    item['path'].unlink(missing_ok=True); done+=1
                local.append(out)
            set_job(i,phase='Preparação concluída',overall_percent=40 if prep_mode=='audio' else 50)

        total_available=1
        for a in local: total_available*=len(a)
        if mode=='random':
            wanted=min(max(1,requested),total_available)
            indices=random.Random(i).sample(range(total_available),wanted)
            selected=[]
            sizes=[len(a) for a in local]
            for idx in indices:
                pick=[]
                for a,size in zip(reversed(local),reversed(sizes)):
                    pick.append(a[idx%size]); idx//=size
                selected.append(tuple(reversed(pick)))
        else:
            if total_available>MAX_COMBINATIONS:raise RuntimeError(f'Este worker aceita no máximo {MAX_COMBINATIONS} combinações por job.')
            selected=itertools.product(*local)

        count=min(requested,total_available) if mode=='random' else total_available
        base=28 if exact else 40 if prep_mode=='audio' else 50
        set_job(i,total=count,progress=0,phase='Gerando combinações',overall_percent=base)
        zp=wd/'VideoLabs-resultados.zip'; seen={}
        manifest={'project':j['project_name'],'mode':mode,'quality':quality,'preparation_mode':prep_mode,'total_available':total_available,'generated':count,'stages':[{'name':s['name'],'files':[{'name':f['name'],'label':f.get('label')} for f in s['files']]} for s in stages],'outputs':[]}
        with zipfile.ZipFile(zp,'w',compression=zipfile.ZIP_STORED,allowZip64=True) as z:
            for x,c in enumerate(selected,1):
                filename=output_name(c,seen); out=wd/filename
                concat(c,out); z.write(out,arcname=out.name)
                manifest['outputs'].append({'file':out.name,'sources':[{'label':item['label'],'original':item['original_name']} for item in c]})
                out.unlink(missing_ok=True)
                set_job(i,progress=x,phase=f'Gerando {x}/{count}',overall_percent=base+round(x/max(count,1)*(100-base),1))
            z.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2))

        shutil.rmtree(inputs,ignore_errors=True); shutil.rmtree(prepared,ignore_errors=True)
        set_job(i,status='ready',phase='Pronto para baixar',progress=count,overall_percent=100,zip_path=str(zp))
    except Exception as e:
        set_job(i,status='error',phase='Falha no processamento',error=str(e) or type(e).__name__)

def recover_interrupted():
    for p in ROOT.glob('*/job.json'):
        try:
            j=json.loads(p.read_text(encoding='utf-8')); i=p.parent.name
            if time.time()-j.get('created_at',0)>JOB_TTL_SECONDS:continue
            j['workdir']=str(p.parent)
            with lock:jobs[i]=j
            if j.get('status') in ('queued','processing'):
                set_job(i,status='queued',phase='Retomando após reinício do worker',overall_percent=0,error=None)
                executor.submit(process_job,i)
        except Exception:continue

def cleanup():
    while True:
        now=time.time()
        with lock:expired=[(i,j.get('workdir')) for i,j in jobs.items() if now-j['created_at']>JOB_TTL_SECONDS]
        for i,w in expired:
            with lock:jobs.pop(i,None)
            if w:shutil.rmtree(w,ignore_errors=True)
        time.sleep(600)

recover_interrupted(); threading.Thread(target=cleanup,daemon=True).start()

@app.get('/')
def root():return {'name':APP_NAME,'status':'ok','version':'0.6.0'}
@app.get('/health')
def health():return {'ok':True,'ffmpeg':Path(FFMPEG).name,'version':'0.6.0','profiles':PROFILES}
@app.post('/v1/jobs/remote')
async def create_remote(request:Request):
    m=await request.json(); stages=m.get('stages') or []
    if len(stages)<2:raise HTTPException(400,'Adicione pelo menos duas etapas.')
    for s in stages:
        if not s.get('files'):raise HTTPException(400,f"A etapa '{s.get('name','Etapa')}' não possui vídeos.")
        for f in s['files']:
            if not str(f.get('url','')).startswith('https://res.cloudinary.com/sskapqzv/'):raise HTTPException(400,'Origem de vídeo não permitida.')
    total=1
    for s in stages:total*=len(s['files'])
    mode=m.get('mode','all'); requested=int(m.get('requested',25)); quality=m.get('quality','high')
    if quality not in PROFILES:quality='high'
    if mode=='all' and total>MAX_COMBINATIONS:raise HTTPException(400,f'Há {total} combinações. O limite é {MAX_COMBINATIONS}.')
    i=uuid.uuid4().hex[:12]; wd=ROOT/i; wd.mkdir(parents=True,exist_ok=True)
    with lock:jobs[i]={'status':'queued','phase':'Na fila','progress':0,'total':total if mode=='all' else min(requested,total),'overall_percent':0,'error':None,'created_at':time.time(),'workdir':str(wd),'stages':stages,'mode':mode,'requested':requested,'quality':quality,'project_name':safe_name(m.get('projectName','Projeto VideoLabs'))}
    persist(i); executor.submit(process_job,i); return public_job(i)

@app.get('/v1/jobs/{job_id}')
def get_job(job_id:str):return public_job(job_id)

@app.get('/v1/jobs/{job_id}/download')
def download_job(job_id:str):
    with lock:j=jobs.get(job_id)
    if not j:j=restore_job(job_id)
    if not j:raise HTTPException(404,'Job não encontrado ou expirado.')
    if j['status']!='ready':raise HTTPException(409,'O processamento ainda não terminou.')
    p=Path(j['zip_path']); name=j['project_name']
    if not p.exists():raise HTTPException(404,'Arquivo final não está mais disponível.')
    return FileResponse(p,media_type='application/zip',filename=f'{name}-VideoLabs.zip')
