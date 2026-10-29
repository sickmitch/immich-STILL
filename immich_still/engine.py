from __future__ import annotations
import copy, time, uuid
from pathlib import Path
from .core import (
    FORMAT,
    Pending,
    Skip,
    Stop,
    canonical_id,
    compact,
    db_sha,
    file_read,
    require,
    sha1,
    sha256,
    timestamp,
    write_payload,
)
from .api import ApiError
from .metadata import DATE_FIELDS, capture_stamp, make_sidecar, target_differences
from .gcam import convert, Unsupported

PROVENANCE='immich-still-native'
# Device/local identifiers belong to the ORIGINAL upload, not to the new still.
DEVICE_METADATA={'mobile-app'}
def transferable_metadata(snapshot):
    return [m for m in snapshot.get('metadata',[]) if m['key'] not in DEVICE_METADATA]
# These are derived data or explicitly copied through native APIs. Unknown references block trashing.
KNOWN_REFS={'album_asset','shared_link_asset','tag_asset','asset_job_status','smart_search','ocr_search',
            'asset_metadata','asset_ocr','asset_face','asset_video','asset_audio','asset_keyframe','album','stack'}


def stable_source(s):
    a=s.get('asset') or {};e=s.get('exif') or {}
    keys=('id','type','ownerId','checksum','checksumAlgorithm','fileCreatedAt','fileModifiedAt','localDateTime',
          'createdAt','originalFileName','isFavorite','isExternal','isOffline','isEdited','visibility','libraryId','livePhotoVideoId','stackId','width','height')
    ex={k:v for k,v in e.items() if k not in ('updatedAt','updateId','lockedProperties')}
    return {'asset':{k:a.get(k) for k in keys},'exif':ex,
            'albums':s.get('albums',[]),'sharedLinks':s.get('sharedLinks',[]),'tags':s.get('tags',[]),
            'metadata':s.get('metadata',[])}


def ensure_supported(s,refs):
    a,e=s.get('asset'),s.get('exif')
    require(a and e,'Asset/EXIF immagine mancanti')
    if a['type']!='IMAGE' or a.get('isExternal') or a.get('libraryId') or a.get('isEdited') or a.get('isOffline'):
        raise Skip('Solo IMAGE JPEG gestite, online e non modificate nell’editor')
    require(a.get('status')=='active' and a.get('deletedAt') is None and a.get('livePhotoVideoId'),'Sorgente non più Motion attiva')
    require(a['visibility'] in ('timeline','archive'),'Visibilità non supportata')
    require(Path(a['originalPath']).suffix.lower() in ('.jpg','.jpeg'),'Formato originale non JPEG')
    require(e.get('fileSizeInByte') is not None and not e.get('livePhotoCID'),'Dimensione mancante o pairing Apple: sorgente non trattabile')
    # Face/OCR rows are treated as derived data: the new still is reprocessed natively by Immich.
    unknown={k:n for k,n in refs.items() if n and (not k.startswith('public.') or k.split('.')[1] not in KNOWN_REFS)}
    if unknown:raise Skip('Riferimenti non trasferibili tramite il flusso implementato: '+compact(unknown))
    if s.get('covers'):raise Skip('Immagine usata come copertina album: conservata, nessun cambio silenzioso di copertina')
    # A stack can carry ordering/primary semantics; a blind copy can merge/delete a stack.
    if a.get('stackId'):raise Skip('Immagine in stack: conservata per evitare modifiche al raggruppamento')
    capture_stamp(s)


class Engine:
    def __init__(self,cfg,api,db,journal,owner):
        self.cfg,self.api,self.db,self.j,self.owner=cfg,api,db,journal,owner
    def save(self,p):self.j.save_pair(p)
    def guard(self):
        self.j.guard()
        self.db.assert_lock()
    def read_source(self,s):return file_read(self.cfg.host_path(s['asset']['originalPath']))
    def assert_source(self,p,allow_trashed=False,read_bytes=True):
        self.guard()
        now=self.db.asset(p['id']);a=now.get('asset')
        require(a is not None,'Sorgente non trovata: non deduco un cestinamento riuscito')
        require(stable_source(now)==stable_source(p['before']),'Dati/associazioni sorgente cambiati durante la run: originale non cestinato automaticamente')
        if allow_trashed:require(a.get('status') in ('active','trashed'),'Stato sorgente inatteso')
        else:require(a.get('deletedAt') is None and a.get('status')=='active','Sorgente già cestinata/modificata esternamente')
        if read_bytes:
            b,_=self.read_source(now)
            require(sha256(b)==p['conversion']['original_sha256'],'Originale JPEG cambiato')
        sidecar_hashes={}
        for f in now.get('files',[]):
            if f['type']=='sidecar':
                content,_=file_read(self.cfg.host_path(f['path']),16*1024*1024);sidecar_hashes[f['path']]=sha256(content)
        require(sidecar_hashes==p.get('source_sidecars',{}),'Sidecar sorgente cambiato durante la run')
        if not allow_trashed:
            ensure_supported(now,self.db.foreign_refs(p['id']))
        return now
    def prepare(self,i):
        s=self.db.asset(i);a=s.get('asset');require(a and a.get('ownerId')==self.owner,'Sorgente di altro proprietario/non trovata')
        info=self.api.asset(i)
        require(info.get('livePhotoVideoId')==a.get('livePhotoVideoId'),'API/DB non allineati')
        ensure_supported(s,self.db.foreign_refs(i))
        original,fs=self.read_source(s)
        require(len(original)==s['exif']['fileSizeInByte'] and sha1(original)==db_sha(a),'Checksum/dimensione originale diversi dal DB')
        still,conv=convert(original)
        v=self.db.video(a['livePhotoVideoId'])
        require(v and v.get('ownerId')==self.owner and v.get('type')=='VIDEO','Record video associato non valido')
        require(sha1(original[-conv['video_bytes_removed']:])==db_sha(v),'Video embedded diverso dal checksum del record VIDEO')
        sidecars=[f for f in s['files'] if f['type']=='sidecar']
        require(len(sidecars)<=1,'Più sidecar: non scelgo alla cieca')
        existing=None;source_sidecars={}
        if sidecars:
            existing,_=file_read(self.cfg.host_path(sidecars[0]['path']),16*1024*1024)
            source_sidecars[sidecars[0]['path']]=sha256(existing)
        sidecar=make_sidecar(s,existing)
        require(stable_source(self.db.asset(i))==stable_source(s),'Sorgente cambiata durante audit')
        stem=Path(a['originalFileName']).stem
        if stem.upper().endswith('.MV'):stem=stem[:-3]
        filename=stem+'.STILL.jpg'
        require(not any(c in filename for c in '\r\n"\\/'),'Nome originale non supportato per upload')
        found=self.db.by_checksum(self.owner,conv['still_sha1'])
        require(i not in found,'Impossibile coincidenza checksum sorgente/still')
        p={'format':FORMAT,'id':i,'owner':self.owner,'before':s,'video_before':v,'conversion':conv,
           'filesystem':fs,'source_sidecars':source_sidecars,'filename':filename,
           'phase':'prepared','target_id':None,'existing_still_ids':found,
           'payload_name':i+'.still.jpg','sidecar_name':i+'.still.xmp','backup_name':i+'.original.jpg',
           'device_metadata_not_copied':[m['key'] for m in s.get('metadata',[]) if m['key'] in DEVICE_METADATA],
           'sidecar_sha256':sha256(sidecar),'upload_token':str(uuid.uuid4()),'run_id':self.j.path.name}
        if self.api.mutating:
            if s['tags']:self.api.need({'tag.asset'})
        return p,original,still,sidecar
    def stage(self,p,original,still,sidecar):
        self.save(p)
        for name,data in [(p['backup_name'],original),(p['payload_name'],still),(p['sidecar_name'],sidecar)]:
            write_payload(self.j.path/name,data)
        p['phase']='staged';self.save(p);self.j.checkpoint(independent=True)
    def payloads(self,p):
        original,_=file_read(self.j.path/p['backup_name']);still,_=file_read(self.j.path/p['payload_name']);sidecar,_=file_read(self.j.path/p['sidecar_name'],16*1024*1024)
        c=p['conversion'];require(sha256(original)==c['original_sha256'] and sha256(still)==c['still_sha256'] and sha256(sidecar)==p['sidecar_sha256'],'Payload/backup journal corrotto')
        generated,proof=convert(original);require(generated==still and proof==c,'Payload non riproducibile dal backup verificato')
        return original,still,sidecar
    def provenance(self,p):
        return {'sourceId':p['id'],'sourceSha1':p['conversion']['original_sha1'],
                'stillSha1':p['conversion']['still_sha1'],'token':p['upload_token'],'runId':p['run_id']}
    def adopt_or_upload(self,p,still,sidecar):
        self.guard()
        if p.get('target_id'):return
        found=self.db.by_checksum(self.owner,p['conversion']['still_sha1'])
        if found:
            require(len(found)==1,'Checksum destinazione non univoco')
            target=self.db.asset(found[0]);own=any(m['key']==PROVENANCE and m['value']==self.provenance(p) for m in target.get('metadata',[]))
            require(own,'Still già presente ma non creata da questa run: originale NON cestinato')
            p['target_id']=found[0];p['phase']='uploaded';self.save(p);return
        a=p['before']['asset'];metadata=copy.deepcopy(transferable_metadata(p['before']))
        require(all(m['key']!=PROVENANCE for m in metadata),'Chiave provenance già presente sulla sorgente')
        metadata.append({'key':PROVENANCE,'value':self.provenance(p)})
        fields={'filename':p['filename'],'fileCreatedAt':a['fileCreatedAt'],'fileModifiedAt':a['fileModifiedAt'],
                'isFavorite':str(a['isFavorite']).lower(),'visibility':a['visibility'],'metadata':compact(metadata)}
        p['phase']='upload_requested';p['upload_requested_at']=timestamp();self.save(p)
        response=self.api.upload(fields,p['filename'],still,sidecar)
        require(response['id']!=p['id'],'Il server ha restituito la sorgente come destinazione')
        p['upload_response']=response;self.save(p)
        if response['status']=='duplicate':
            target=self.db.asset(response['id'])
            own=any(m['key']==PROVENANCE and m['value']==self.provenance(p) for m in target.get('metadata',[]))
            require(own,'Collisione upload esterna: non adotto un asset non autorizzato')
        p['target_id']=response['id'];p['phase']='uploaded';self.save(p)
    def wait_metadata(self,p):
        deadline=time.monotonic()+self.cfg['metadata_timeout'];last=[]
        while time.monotonic()<deadline:
            t=self.db.asset(p['target_id']);last=target_differences(p['before'],t,p['conversion'])
            if not last and (t.get('job') or {}).get('metadataExtractedAt'):
                return t
            time.sleep(self.cfg['thumbnail_poll_interval'])
        raise Pending('Metadati della NUOVA still non equivalenti/pronti: '+', '.join(last)+'. Originale non cestinato')
    def copy_associations(self,p):
        self.guard()
        target=self.db.asset(p['target_id']);before=p['before']
        require(not target_differences(before,target,p['conversion']),'Destinazione non equivalente prima della copia associazioni')
        require(target['asset'].get('stackId') is None,'Destinazione in stack: non unisco stack')
        self.api.request('PUT','/assets/copy',{'sourceId':p['id'],'targetId':p['target_id'],
            'albums':True,'sharedLinks':True,'favorite':False,'sidecar':False,'stack':False})
        # Favorite/visibility are supplied on upload and checked. Never overwrite an existing user's value.
        for tag in before.get('tags',[]):
            self.api.need({'tag.asset'})
            self.api.request('PUT','/tags/'+canonical_id(tag)+'/assets',{'ids':[p['target_id']]})
        p['phase']='copied';self.save(p)
    def verify_associations(self,p,t):
        for k in ('albums','sharedLinks','tags'):
            require(set(p['before'].get(k,[]))<=set(t.get(k,[])),'Associazioni mancanti nella still: '+k)
        available={m['key']:m['value'] for m in t.get('metadata',[])}
        for m in transferable_metadata(p['before']):require(available.get(m['key'])==m['value'],'Metadata custom non copiato: '+m['key'])
    def target_file(self,p,t):
        a=t.get('asset');require(a and a.get('id')==p['target_id'],'Asset destinazione assente')
        data,_=file_read(self.cfg.host_path(a['originalPath']))
        require(sha256(data)==p['conversion']['still_sha256'],'JPEG sul server differente dalla still generata')
        body,info=self.api.media(p['target_id'],'original')
        require(body==data,'Download originale differente dal JPEG still verificato')
        return info
    def thumb_observation(self,p):
        t=self.db.asset(p['target_id']);result={}
        for kind in ('thumbnail','preview'):
            records=[f for f in t['files'] if f['type']==kind and not f.get('isEdited')]
            require(len(records)==1,'Derivato non disponibile/univoco: '+kind)
            f=records[0];require(f['assetId']==p['target_id'],'Derivato di un asset diverso')
            path=self.cfg.host_path(f['path']);b,fs=file_read(path)
            served,info=self.api.media(p['target_id'],kind)
            require(served==b,'Derivato HTTP diverso dal file registrato: '+kind)
            result[kind]={'path':f['path'],'sha256':sha256(b),'mtime_ns':fs['mtime_ns'],'inode':fs['inode'],'bytes':len(b),'http':info}
        return result
    def refresh_thumbnails(self,p):
        self.guard()
        try:baseline=self.thumb_observation(p)
        except (Stop,OSError):baseline=None
        p['thumbnail']={'status':'requested','at':timestamp(),'baseline':baseline};self.save(p)
        self.api.request('POST','/assets/jobs',{'assetIds':[p['target_id']],'name':'regenerate-thumbnail'})
        end=time.monotonic()+self.cfg['thumbnail_timeout'];previous=None;last=''
        while time.monotonic()<end:
            try:
                obs=self.thumb_observation(p)
                changed=baseline is None or all((obs[k]['mtime_ns'],obs[k]['inode'])!=(baseline[k]['mtime_ns'],baseline[k]['inode']) for k in obs)
                if changed and obs==previous:
                    p['thumbnail'].update(status='verified',verified_at=timestamp(),outputs=obs);self.save(p);return
                previous=obs
            except (Stop,OSError) as e:last=str(e);previous=None
            time.sleep(self.cfg['thumbnail_poll_interval'])
        raise Pending('Thumbnail non verificate entro timeout: '+last+'. Originale non cestinato')
    def verify_target(self,p,with_http=True):
        t=self.db.asset(p['target_id']);diff=target_differences(p['before'],t,p['conversion'])
        require(not diff,'Still non equivalente: '+', '.join(diff))
        self.verify_associations(p,t)
        info=self.api.asset(p['target_id'])
        require(info.get('livePhotoVideoId') in (None,'') and not info.get('isTrashed'),'API: destinazione non still/attiva')
        if with_http:self.target_file(p,t);self.thumb_observation(p)
        return t
    def process(self,p):
        require(p['format']==FORMAT and p['owner']==self.owner,'Journal di altra versione/utente')
        if p.get('phase')=='completed':
            self.verify(p)
            return
        if p.get('phase')=='prepared':
            # Crash during local staging: re-derive only from the still-active verified original.
            current=self.assert_source(p)
            original,_=self.read_source(current);still,proof=convert(original)
            require(proof==p['conversion'],'Conversione cambiata fra preparazione e ripresa')
            old_sidecar=None
            for f in current.get('files',[]):
                if f['type']=='sidecar':old_sidecar,_=file_read(self.cfg.host_path(f['path']),16*1024*1024)
            sidecar=make_sidecar(current,old_sidecar)
            require(sha256(sidecar)==p['sidecar_sha256'],'Sidecar non riproducibile durante ripresa')
            self.stage(p,original,still,sidecar)
        _,still,sidecar=self.payloads(p)
        existing=self.db.asset(p['id'])
        if not existing.get('asset'):
            require(p.get('trash_verified_at') is not None,'Sorgente scomparsa prima di una cancellazione verificata')
            self.verify_target(p);p['source_state']='absent_after_recorded_trash';p['phase']='completed';self.save(p);return
        if existing['asset'].get('status')=='trashed':
            require(p.get('trash_requested_at') and p.get('target_id'),'Cestinamento esterno/non autorizzato dal journal')
            self.verify_target(p);self.assert_source(p,allow_trashed=True)
            p['trash_verified_at']=p.get('trash_verified_at') or timestamp();p['phase']='completed';self.save(p);return
        self.assert_source(p)
        self.adopt_or_upload(p,still,sidecar)
        self.j.log('STILL ID='+p['target_id']+'; attendo metadati/date equivalenti')
        self.wait_metadata(p)
        self.assert_source(p)
        self.copy_associations(p)
        self.refresh_thumbnails(p)
        target=self.verify_target(p)
        # Recheck source + trash configuration immediately before allowing the single DELETE.
        self.assert_source(p)
        features=self.api.request('GET','/server/features');settings=self.api.request('GET','/server/config')
        require(features.get('trash') is True and int(settings.get('trashDays',0))>0,'Cestino disattivato durante la run')
        p['verified_target_at']=timestamp();p['target_final']={'id':p['target_id'],'createdAt':target['asset']['createdAt'],
            'originalPath':target['asset']['originalPath'],'dates':{k:target['asset'][k] for k in DATE_FIELDS},
            'checksum':db_sha(target['asset']),'bytes':target['exif']['fileSizeInByte']}
        p['phase']='trash_requested';p['trash_requested_at']=timestamp();p['trash_days_observed']=settings['trashDays'];self.save(p);self.j.checkpoint(independent=True)
        self.guard()
        self.api.allowed_trash_id=p['id']
        try:self.api.trash_source(p['id'])
        except ApiError:
            # A lost response is not a failed commit. Inspect; never issue force:true.
            now=self.db.asset(p['id'])
            if not now.get('asset') or now['asset'].get('status')!='trashed':raise
        finally:self.api.allowed_trash_id=None
        now=self.assert_source(p,allow_trashed=True)
        require(now['asset'].get('status')=='trashed' and now['asset'].get('deletedAt'),'Server non conferma il cestino')
        self.verify_target(p)
        p['phase']='completed';p['trash_verified_at']=timestamp();p['source_deleted_at']=now['asset']['deletedAt'];self.save(p)
        self.j.log('COMPLETATO: still '+p['target_id']+'; Motion '+p['id']+' nel cestino nativo. Video lasciato a Immich.')
    def verify(self,p):
        require(p.get('target_id'),'Journal senza destinazione');self.verify_target(p)
        s=self.db.asset(p['id'])
        if s.get('asset'):
            self.assert_source(p,allow_trashed=True)
            require(s['asset'].get('status')=='trashed' and s['asset'].get('deletedAt'),'Motion non nel cestino')
            state='trashed'
        else:
            require(p.get('trash_verified_at'),'Sorgente assente senza precedente verifica');state='absent_after_recorded_trash'
        return dict(ok=True,source_id=p['id'],still_id=p['target_id'],source_state=state,dates_preserved=True)
