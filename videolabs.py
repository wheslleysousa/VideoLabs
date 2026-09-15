#!/usr/bin/env python3
"""VideoLabs V0 — combina etapas de vídeo usando FFmpeg."""
from __future__ import annotations
import argparse, hashlib, itertools, json, re, shutil, subprocess, sys, tempfile
from pathlib import Path

VIDEO_EXTENSIONS={'.mp4','.mov','.mkv','.webm','.m4v'}

def key(p):
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r'(\d+)',p.name)]

def stages(project):
    result=[]
    for folder in sorted((p for p in project.iterdir() if p.is_dir()),key=key):
        clips=sorted((p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS),key=key)
        if clips: result.append((folder.name,clips))
    if not result: raise ValueError('Nenhuma etapa com vídeos encontrada.')
    return result

def cid(combo):
    return hashlib.sha1('|'.join(str(p.resolve()) for p in combo).encode()).hexdigest()[:12]

def concat(combo,dest):
    with tempfile.NamedTemporaryFile('w',suffix='.txt',delete=False,encoding='utf-8') as f:
        listfile=Path(f.name)
        for clip in combo:
            escaped=str(clip.resolve()).replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")
    try:
        subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','concat','-safe','0','-i',str(listfile),'-c','copy','-movflags','+faststart',str(dest)],check=True)
    finally:
        listfile.unlink(missing_ok=True)

def main():
    ap=argparse.ArgumentParser(description='VideoLabs V0')
    ap.add_argument('project',type=Path)
    ap.add_argument('--output',type=Path,default=Path('output'))
    ap.add_argument('--limit',type=int)
    ap.add_argument('--dry-run',action='store_true')
    a=ap.parse_args()
    if not a.project.exists(): sys.exit(f'Projeto não encontrado: {a.project}')
    if not shutil.which('ffmpeg'): sys.exit('FFmpeg não encontrado.')
    ss=stages(a.project)
    total=1
    for _,clips in ss: total*=len(clips)
    print('VideoLabs')
    for name,clips in ss: print(f'- {name}: {len(clips)} vídeo(s)')
    print(f'Total: {total} combinação(ões)')
    a.output.mkdir(parents=True,exist_ok=True)
    mp=a.output/'manifest.json'
    manifest={'completed':{}}
    if mp.exists():
        try: manifest=json.loads(mp.read_text(encoding='utf-8'))
        except Exception: pass
    done=manifest.setdefault('completed',{})
    processed=0
    for i,combo in enumerate(itertools.product(*(clips for _,clips in ss)),1):
        if a.limit is not None and processed>=a.limit: break
        ident=cid(combo)
        filename=f"{i:05d}-{'__'.join(p.stem for p in combo)}.mp4"
        dest=a.output/filename
        if ident in done and dest.exists():
            print(f'[{i}/{total}] já existe: {filename}')
            continue
        if a.dry_run:
            print(f"[{i}/{total}] {' + '.join(p.name for p in combo)}")
            processed+=1; continue
        print(f'[{i}/{total}] gerando {filename}')
        try: concat(combo,dest)
        except subprocess.CalledProcessError:
            dest.unlink(missing_ok=True); print('Falha: clipes possivelmente incompatíveis.',file=sys.stderr); continue
        done[ident]={'file':filename,'sources':[str(p) for p in combo]}
        mp.write_text(json.dumps(manifest,indent=2,ensure_ascii=False),encoding='utf-8')
        processed+=1
    print(f'Concluído: {processed} processada(s).')

if __name__=='__main__': main()
