#!/usr/bin/env python3
"""Native workflow: new verified still -> normal trash of original IMAGE.
Never stop containers, never mutate catalog SQL, never unlink/recreate/purge the motion VIDEO.
"""
from __future__ import annotations
import argparse, json, os, shutil, signal, stat, sys
from pathlib import Path
from immich_still.core import (
    Config,
    FORMAT,
    Pending,
    Skip,
    Stop,
    canonical_id,
    checked_run,
    compact,
    instant,
    require,
    timestamp,
)
from immich_still.api import Api,ApiError
from immich_still.db import Database
from immich_still.engine import Engine,Unsupported
from immich_still import journal as J
from immich_still import core as S

SANITY_WALL_TS='2026-09-23T00:00:00+00:00'
CURSOR_SCHEMA='immich-still-native/cursor-1'
CURSOR_FILE='cursor.json'


def parse(argv=None):
    p=argparse.ArgumentParser(description=__doc__,allow_abbrev=False)
    p.add_argument('--version',action='version',version='1.1.0 (native API upload + trash + cursor)')
    p.add_argument('--config',type=Path,default=Path(__file__).resolve().with_name('conf.json'),help='Configurazione (default: conf.json accanto allo script)')
    mode=p.add_mutually_exclusive_group()
    mode.add_argument('--audit',action='store_true');mode.add_argument('--convert-to-still','--convert',dest='convert',action='store_true')
    mode.add_argument('--resume',metavar='RUN',type=Path);mode.add_argument('--verify',metavar='RUN',type=Path)
    mode.add_argument('--verify-journal',metavar='RUN');mode.add_argument('--list-runs',action='store_true')
    mode.add_argument('--check-db',action='store_true')
    p.add_argument('--asset-id')
    p.add_argument('--limit',type=int,default=0,help='Numero massimo di candidati (default: 0, tutti)');p.add_argument('--include-archived',action='store_true')
    p.add_argument('--sanity-wall',action=argparse.BooleanOptionalAction,default=True,help='Limita la selezione a fileCreatedAt >= 2026-09-23T00:00:00Z (attivo di default)')
    p.add_argument('--non-interactive',action='store_true',help=argparse.SUPPRESS)
    p.add_argument('--state-dir',type=Path)
    a=p.parse_args(argv)
    if not any((a.audit,a.convert,a.resume,a.verify,a.verify_journal,a.list_runs,a.check_db)):
        a.convert=True
    if a.limit<0:p.error('--limit deve essere >= 0')
    if (a.resume or a.verify) and a.asset_id:p.error('Con RUN non si riselezionano asset')
    if a.asset_id:
        try:canonical_id(a.asset_id)
        except Stop as e:p.error(str(e))
    return a


def pin_docker():
    require(shutil.which('docker'),'Docker CLI non disponibile nel PATH')
    def cmd(*args):return checked_run(['docker',*args],timeout=30).stdout.decode().strip()
    if os.environ.get('DOCKER_CONTEXT'):
        context=os.environ['DOCKER_CONTEXT'];host=json.loads(cmd('context','inspect',context))[0]['Endpoints']['docker']['Host']
    elif os.environ.get('DOCKER_HOST'):host=os.environ['DOCKER_HOST']
    else:host=json.loads(cmd('context','inspect',cmd('context','show')))[0]['Endpoints']['docker']['Host']
    require(host.startswith('unix:///'),'Richiesto Docker locale rootless via socket UNIX')
    socket=Path(host[len('unix://'):]);require(socket.resolve()==socket,'Socket Docker non canonico')
    st=socket.stat();require(stat.S_ISSOCK(st.st_mode) and st.st_uid==os.geteuid() and not st.st_mode&0o002,'Socket Docker non privato/proprio')
    os.environ['DOCKER_HOST']=host
    for k in ('DOCKER_CONTEXT','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH'):os.environ.pop(k,None)
    info=json.loads(cmd('info','--format','{{json .}}'))
    require('name=rootless' in info.get('SecurityOptions',[]),'Daemon Docker non rootless')
    return {'endpoint':host,'id':info['ID'],'security_options':info['SecurityOptions']}


def validate_mount(cfg):
    names=cfg.data['app_containers'];require(isinstance(names,list) and names,'app_containers assente')
    result=json.loads(checked_run(['docker','inspect',*names],timeout=30).stdout)
    for c in result:
        require(c.get('State',{}).get('Running'),'Immich deve rimanere acceso per questo workflow')
    mounts=[m for c in result for m in c.get('Mounts',[]) if m['Destination']==cfg.server_root]
    require(mounts and all(Path(m['Source']).resolve()==cfg.root for m in mounts),'Mapping filesystem diverso dal container Immich')
    return [{'name':c['Name'].lstrip('/'),'id':c['Id'],'restart_policy':c.get('HostConfig',{}).get('RestartPolicy')} for c in result]


def local_mode(a):
    data=S.configure(a.config) if a.config.exists() else {}
    root=S.state_path(a.config,a.state_dir,data)
    if a.list_runs:
        for x in J.entries(root):
            if x.get('event')=='created':print(x['path'])
    else:
        if a.verify_journal=='latest':
            runs=[r for r in J.entries(root) if r.get('event')=='created'];require(runs,'Nessuna run');path=Path(runs[-1]['path'])
        else:path=Path(a.verify_journal).expanduser().absolute()
        result=J.proof(path);print(json.dumps(result,indent=2));J.write(path/'persistence-recheck.json',result)
    return 0


def cursor_path(state_root: Path) -> Path:
    return state_root/CURSOR_FILE


def load_cursor(state_root: Path):
    path=cursor_path(state_root)
    if not path.exists():
        return None
    data=J.read(path)
    require(data.get('schema')==CURSOR_SCHEMA,'Cursor non riconosciuto: cancellare '+str(path)+' oppure aggiornarlo')
    canonical_id(data['id'])
    instant(data['fileCreatedAt'])
    return data


def save_cursor(state_root: Path, *, asset_id: str, file_created_at: str, note: str, run_path: Path | None):
    canonical_id(asset_id)
    instant(file_created_at)
    payload={
        'schema':CURSOR_SCHEMA,
        'updatedAt':timestamp(),
        'id':asset_id,
        'fileCreatedAt':file_created_at,
        'note':note,
        'run':str(run_path) if run_path else None,
    }
    J.write(cursor_path(state_root),payload)
    J.fsync_dir(state_root)
    return payload


def candidate_marker(snapshot):
    asset=(snapshot or {}).get('asset') or {}
    require(asset.get('id') and asset.get('fileCreatedAt'),'Snapshot asset incompleto per il cursor')
    return {'id':canonical_id(asset['id']),'fileCreatedAt':instant(asset['fileCreatedAt']).isoformat(),'originalFileName':asset.get('originalFileName') or ''}


def main(argv=None):
    a=parse(argv);os.umask(0o077)
    require(os.geteuid()!=0,'Avvia come mike, senza sudo. Non servono privilegi root.')
    if a.list_runs or a.verify_journal:return local_mode(a)
    old=a.resume or a.verify
    if old:
        old=old.expanduser().absolute();J.proof(old)
    cfg=Config(a.config,a.state_dir or (old.parent if old else None))
    docker=pin_docker();containers=validate_mount(cfg)
    api=Api(cfg,mutating=bool(a.convert or a.resume))
    metadata=api.preflight();owner=metadata['owner'];db=Database(cfg,'immich_still_native')
    j=None
    try:
        db.acquire();identity=db.discover()['identity']
        if a.check_db:
            print(json.dumps({'ok':True,'database':identity,'auth_source':db.auth_source,'sudo_required':False},indent=2));return 0
        mode='resume' if a.resume else 'verify' if a.verify else 'convert' if a.convert else 'audit'
        j=J.Journal(cfg,mode,existing=old)
        if old:
            require(j.info.get('docker')==docker and j.info.get('database')==identity and j.info.get('owner')==owner,'Journal riferito a altro Docker/database/utente')
            require(j.info.get('mode')=='convert','Questo RUN non è una conversione nativa')
        else:
            j.info.update(docker=docker,database=identity,owner=owner,containers=containers,server=metadata)
            j.save()
        engine=Engine(cfg,api,db,j,owner)
        if a.verify:
            pairs=j.pairs();pairs=pairs[:a.limit] if a.limit else pairs
            for p in pairs:
                if p.get('phase')=='skipped':continue
                j.log('VERIFICA: '+compact(engine.verify(p)))
            return 0
        if a.resume:
            durable={p['id'] for p in j.pairs()}
            for source_id in j.info.get('selection',[]):
                if source_id not in durable:
                    j.save_pair({'id':source_id,'format':FORMAT,'phase':'skipped',
                                 'reason':'Non iniziato prima dell’interruzione; originale non toccato'})
            pairs=j.pairs();pairs=pairs[:a.limit] if a.limit else pairs
            for p in pairs:
                if p.get('phase') in ('skipped','audit'):continue
                engine.process(p)
                marker=candidate_marker(p['before'])
                save_cursor(cfg.state,asset_id=marker['id'],file_created_at=marker['fileCreatedAt'],note='resume-completed',run_path=j.path)
            j.info['status']='completed' if all(p.get('phase') in ('completed','skipped') for p in j.pairs()) and len(j.pairs())==len(j.info['selection']) else 'pending'
            j.save();return 0 if j.info['status']=='completed' else 3
        if a.convert:j.unresolved_other_runs()
        after=None
        if a.convert and not a.asset_id:
            after=load_cursor(cfg.state)
            if after:
                j.log('Cursor: riparto dopo '+after['id']+' @ '+after['fileCreatedAt'])
        sanity_wall=SANITY_WALL_TS if a.sanity_wall else None
        if sanity_wall:
            j.log('Sanity wall attiva: asset con fileCreatedAt >= '+SANITY_WALL_TS)
        ids=db.selection(owner,a.limit,a.include_archived,a.asset_id,after=after,sanity_wall=sanity_wall)
        j.info['selection']=ids;j.info['selection_cursor_after']=after;j.info['sanity_wall']=sanity_wall;j.save()
        j.log('Quota (sola osservazione): '+compact(db.quota(owner)))
        j.log('Nessun downtime / SQL di modifica / sudo. Cestino nativo: '+str(metadata['trash_days'])+' giorni.')
        if not ids:
            j.info['status']='completed' if a.convert else 'audit';j.save();return 2 if a.asset_id else 0
        skipped=0
        for i in ids:
            source_snapshot=db.asset(i)
            marker=candidate_marker(source_snapshot)
            try:
                p,original,still,sidecar=engine.prepare(i)
            except ApiError:raise
            except (Skip,Unsupported,Stop,OSError) as e:
                skipped+=1;j.save_pair({'id':i,'format':FORMAT,'phase':'skipped','reason':S.redact(str(e))});j.log('SKIP '+i+': '+str(e))
                if a.convert:save_cursor(cfg.state,asset_id=marker['id'],file_created_at=marker['fileCreatedAt'],note='skipped',run_path=j.path)
                continue
            j.log('CANDIDATO '+i+': '+str(p['conversion']['original_bytes'])+' -> '+str(p['conversion']['still_bytes'])+' byte; nuova still '+p['filename'])
            if p['existing_still_ids']:
                p['phase']='skipped';p['reason']='Still già presente con checksum identico: nessun riuso automatico in questa release';j.save_pair(p);skipped+=1
                j.log('SKIP '+i+': still già presente '+compact(p['existing_still_ids']))
                if a.convert:save_cursor(cfg.state,asset_id=marker['id'],file_created_at=marker['fileCreatedAt'],note='skipped-existing-still',run_path=j.path)
                continue
            if not a.convert:
                p['phase']='audit';j.save_pair(p)
                continue
            engine.stage(p,original,still,sidecar)
            engine.process(p)
            save_cursor(cfg.state,asset_id=marker['id'],file_created_at=marker['fileCreatedAt'],note='completed',run_path=j.path)
        j.info['status']='completed' if a.convert else 'audit';j.info['skipped']=skipped;j.save();j.checkpoint(independent=True)
        return 2 if skipped else 0
    except (Exception,KeyboardInterrupt) as e:
        if j:
            try:
                if not a.verify:
                    j.info['status']='pending' if api.mutating and any(p.get('phase')!='skipped' for p in j.pairs()) else 'aborted';j.info['error']=S.redact(str(e));j.save()
                j.log('STOP: '+str(e));j.log('Immich non è stato fermato; nessun DELETE viene forzato.')
                if api.mutating:
                    import shlex
                    command=[str(Path(__file__).with_name('immich-still.sh')),'--config',str(cfg.path),'--resume',str(j.path),'--limit','0','--non-interactive']
                    j.log('RIPRESA: '+shlex.join(command))
            except Exception:pass
        if isinstance(e,KeyboardInterrupt):return 130
        if isinstance(e,Pending) or (j and api.mutating):return 3
        raise
    finally:
        if j:j.close()
        db.close()

if __name__=='__main__':
    def interrupted(sig,frame):raise KeyboardInterrupt('Interruzione: riprendere il journal')
    signal.signal(signal.SIGTERM,interrupted)
    try:raise SystemExit(main())
    except (OSError,RuntimeError,ValueError) as e:print('ERRORE: '+S.redact(str(e)),file=sys.stderr);raise SystemExit(1)
