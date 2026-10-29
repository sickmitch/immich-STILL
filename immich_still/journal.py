#!/usr/bin/env python3
"""Durable local journals, owned by the unprivileged coordinator.
No temporary directories, no effective configs, no automatic recreation of runs.
The registry detects missing journals on later processes as well as the active run.
"""
from __future__ import annotations
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import uuid
from . import core as S

from .core import FORMAT, VERSION
MAX_JSON=32*1024*1024
TERMINAL={'completed','audit','restored','aborted'}

def need(ok,message):
    if not ok: raise RuntimeError(message)

def stamp(): return dt.datetime.now(dt.timezone.utc).isoformat()

def encode(value): return (json.dumps(value,sort_keys=True,ensure_ascii=False,indent=2)+'\n').encode()

def fsync_dir(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)

def read(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as f:
        st=os.fstat(f.fileno())
        need(stat.S_ISREG(st.st_mode) and st.st_nlink==1,'JSON non regolare: '+str(path))
        need(st.st_size<=MAX_JSON,'JSON troppo grande: '+str(path))
        return json.load(f)

def write(path,data):
    tmp=path.with_name('.'+path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as f:
            f.write(encode(data));f.flush();os.fsync(f.fileno())
        os.replace(tmp,path);fsync_dir(path.parent)
    finally: tmp.unlink(missing_ok=True)

def fingerprint(path):
    st=path.lstat()
    need(stat.S_ISDIR(st.st_mode) and not path.is_symlink(),'Directory non canonica: '+str(path))
    need(st.st_uid==os.geteuid() and stat.S_IMODE(st.st_mode)==0o700,
         'Journal deve appartenere al chiamante con permessi 0700: '+str(path))
    return [st.st_dev,st.st_ino]

def entries(root):
    p=root/'runs.jsonl'
    if not p.exists(): return []
    with os.fdopen(os.open(p,os.O_RDONLY|os.O_NOFOLLOW),'r') as f:
        return [json.loads(x) for x in f if x.strip()]

def append(root,value):
    fd=os.open(root/'runs.jsonl',os.O_WRONLY|os.O_APPEND|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        need(os.fstat(fd).st_uid==os.geteuid(),'Registro appartenente a un altro utente')
        data=(json.dumps(value,ensure_ascii=False)+'\n').encode()
        while data:
            n=os.write(fd,data);need(n>0,'Scrittura registro incompleta');data=data[n:]
        os.fsync(fd)
    finally: os.close(fd)
    fsync_dir(root)

def proof(path):
    path=Path(path).expanduser().absolute()
    need(path.resolve()==path,'Symlink nel percorso journal')
    identity=fingerprint(path);root_id=fingerprint(path.parent)
    marker=read(path/'marker.json');info=read(path/'run.json')
    need(marker.get('format')==FORMAT and info.get('format')==FORMAT,'Versione journal non riconosciuta')
    need(marker.get('owner_uid')==os.geteuid(),'Identità journal diversa')
    records=[r for r in entries(path.parent) if r.get('event')=='created' and r.get('path')==str(path)]
    need(len(records)==1,'Run non registrata univocamente')
    record=records[0]
    digest=hashlib.sha256(encode(marker)).hexdigest()
    need(record['directory_identity']==identity and record['root_identity']==root_id and
         record['marker_sha256']==digest,'Directory o marker cambiati rispetto alla creazione')
    for value in info.get('durable_pairs',[]):
        p=path/(str(uuid.UUID(value))+'.json')
        pair=read(p)
        need(pair.get('id')==value,'Record coppia non valido: '+str(p))
    return dict(ok=True,path=str(path),observer_uid=os.geteuid(),directory_identity=identity,
                root_identity=root_id,marker_sha256=digest,status=info.get('status'),
                same_run_as_creation=True,selection=info.get('selection',[]),
                durable_pairs=info.get('durable_pairs',[]))

class Journal:
    def __init__(self,cfg,mode,existing=None):
        self.root=cfg.state
        need(self.root.resolve()==self.root,'Root journal non canonica')
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.root_id=fingerprint(self.root)
        lockfd=os.open(self.root/'.lock',os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
        self.lock=os.fdopen(lockfd,'a')
        fcntl.flock(lockfd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if existing:
            self.path=Path(existing).expanduser().absolute()
            need(self.path.parent==self.root,'RUN deve essere figlio diretto della root journal')
            proof(self.path)
            self.info=read(self.path/'run.json')
            old=self.info['config']
            for k in ('immich_url','host_storage_path','container_storage_path','db_container','db_name','db_user',
                      'db_host','db_port'):
                need(old.get(k)==cfg.data.get(k),'Config diversa dal journal: '+k)
            self.marker=read(self.path/'marker.json')
        else:
            name=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ-')+uuid.uuid4().hex[:10]
            self.path=self.root/name;self.path.mkdir(mode=0o700);fsync_dir(self.root)
            self.marker=dict(format=FORMAT,version=VERSION,owner_uid=os.geteuid(),run_id=name,
                             nonce=uuid.uuid4().hex,created=stamp(),mode=mode)
            write(self.path/'marker.json',self.marker)
            append(self.root,dict(event='created',mode=mode,path=str(self.path),created=stamp(),
                                 directory_identity=fingerprint(self.path),root_identity=self.root_id,
                                 marker_sha256=hashlib.sha256(encode(self.marker)).hexdigest()))
            self.info=dict(format=FORMAT,version=VERSION,mode=mode,created=stamp(),status='planning',
                           config=cfg.public(),config_path=str(cfg.path),selection=[],durable_pairs=[])
        self.path_id=fingerprint(self.path)
        self.logfile=os.fdopen(os.open(self.path/'run.log',os.O_WRONLY|os.O_APPEND|os.O_CREAT|os.O_NOFOLLOW,0o600),
                               'a',encoding='utf-8',buffering=1)
        self.save();self.checkpoint(independent=True)
        self.log('Modalità='+mode+'; journal='+str(self.path)+'; proprietario UID='+str(os.geteuid()))

    def guard(self):
        need(fingerprint(self.root)==self.root_id,'Root journal sparita/sostituita: '+str(self.root))
        need(fingerprint(self.path)==self.path_id,'Run sparita/sostituita: '+str(self.path))
        need(read(self.path/'marker.json')==self.marker,'Marker run modificato')

    def save(self):
        self.guard();write(self.path/'run.json',self.info)

    def checkpoint(self,independent=False):
        self.guard()
        need(read(self.path/'run.json')==self.info,'Stato disco diverso dallo stato in memoria')
        if independent:
            r=subprocess.run([sys.executable,'-m','immich_still.journal',str(self.path)],
                             cwd=Path(__file__).resolve().parent.parent,
                             capture_output=True,timeout=30)
            need(r.returncode==0,'Rilettura indipendente journal fallita: '+r.stderr.decode(errors='replace'))
            p=json.loads(r.stdout)
            need(p['directory_identity']==self.path_id and p['root_identity']==self.root_id and p['ok'],
                 'Secondo processo non vede lo stesso journal')
        fsync_dir(self.path);fsync_dir(self.root)

    def log(self,message):
        self.guard()
        line='['+stamp()+'] '+S.redact(message)
        self.logfile.write(line+'\n');self.logfile.flush();os.fsync(self.logfile.fileno())
        print(line,flush=True)

    def save_pair(self,pair):
        self.guard();pair['journal_updated']=stamp();key=str(uuid.UUID(pair['id']))
        need(key==pair['id'],'ID coppia non canonico')
        write(self.path/(key+'.json'),pair)
        if key not in self.info.setdefault('durable_pairs',[]):
            self.info['durable_pairs'].append(key)
        self.save()

    def pairs(self):
        self.guard();result=[]
        for key in self.info['selection']:
            need(str(uuid.UUID(key))==key,'ID selezione non valido')
            path=self.path/(key+'.json')
            if path.exists(): result.append(read(path))
            else: need(key not in self.info.get('durable_pairs',[]),'Record coppia scomparso: '+str(path))
        return result

    def unresolved_other_runs(self):
        for r in entries(self.root):
            if r.get('event')!='created' or r.get('path')==str(self.path): continue
            p=Path(r['path'])
            need(p.parent==self.root,'Registro con run fuori root')
            if r.get('mode') in ('audit','check-write'): continue
            need(p.is_dir(),'Journal di manutenzione registrato ma mancante: '+str(p)+'. Non inizio nuove scritture.')
            proof(p)
            info=read(p/'run.json')
            need(info.get('status') in TERMINAL,'Manutenzione precedente incompleta: '+str(p)+'. Usa --resume RUN.')

    def close(self):
        try:
            self.guard();append(self.root,dict(event='closed',path=str(self.path),at=stamp(),status=self.info.get('status')))
        except Exception as e:
            print('ATTENZIONE: persistenza journal non confermata: '+str(e),file=sys.stderr)
        finally:
            self.logfile.close();self.lock.close()

if __name__=='__main__':
    try: print(json.dumps(proof(Path(sys.argv[1])),indent=2))
    except Exception as e: print(str(e),file=sys.stderr);sys.exit(1)
