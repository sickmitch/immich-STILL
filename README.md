# Immich MV to STILL

Converte le foto **GCam legacy MicroVideo 1** in JPEG still senza ricomprimere
l’immagine. Carica la nuova foto e il sidecar tramite le API native di Immich,
verifica metadati, associazioni e miniature, poi sposta l’originale nel cestino.
Il video associato resta gestito da Immich. PostgreSQL viene consultato in sola
lettura; i container restano accesi.

## Requisiti e configurazione

- Python 3.10 o successivo, senza dipendenze Python esterne.
- Docker locale rootless, eseguito dallo stesso utente dello script.
- Immich e PostgreSQL già avviati; API key con i permessi richiesti dal workflow.
- Accesso ai media tramite il percorso host del volume Immich.

`conf.json`, accanto allo script, deve appartenere all’utente corrente, avere
permessi `0600` ed essere un file regolare, senza symlink o hardlink.

Esempio con valori da adattare:

```json
{
  "immich_url": "http://localhost:2283",
  "api_key": "INSERIRE_LA_CHIAVE",
  "host_storage_path": "/percorso/host/immich",
  "container_storage_path": "/usr/src/app/upload",
  "app_containers": ["immich"],
  "db_container": "immich_postgres",
  "db_user": "postgres",
  "db_name": "immich"
}
```

Opzioni aggiuntive: `db_host` (default `127.0.0.1`), `db_port` (`5432`),
`db_password_file`, `native_state_dir`, `http_timeout` (`60`), `db_timeout`
(`120`), `metadata_timeout` e `thumbnail_timeout` (`300`),
`thumbnail_poll_interval` (`2`). I tempi sono in secondi.
La password viene letta dal file host protetto, se indicato, oppure dalle
variabili `POSTGRES_PASSWORD_FILE` / `POSTGRES_PASSWORD` del container;
in loro assenza viene usata l’autenticazione libpq già configurata.

## Uso

```bash
./immich-still.sh --help
./immich-still.sh --audit --limit 10
./immich-still.sh --convert --limit 10
./immich-still.sh --resume /percorso/state/RUN
./immich-still.sh --verify /percorso/state/RUN
./immich-still.sh --list-runs
./immich-still.sh --verify-journal latest
./immich-still.sh --check-db
```

**Senza opzioni viene avviata la conversione**, con limite `0` (tutti i candidati).
`--audit` prepara e controlla i candidati, scrive il journal locale e non modifica
Immich. `--config` e `--state-dir` permettono di cambiare i percorsi.

La selezione parte, per default, dal **23 settembre 2026 UTC** (`--no-sanity-wall`
disabilita questo filtro). `--include-archived` include gli asset archiviati;
`--asset-id UUID` seleziona una singola foto. Le conversioni senza `--asset-id`
ripartono dal cursore salvato, ordinato per data di acquisizione e UUID.
Un candidato saltato aggiorna il cursore; una conversione interrotta richiede
la ripresa della run. Una still già presente non viene adottata automaticamente.

`state/` conserva journal, cursore e copie necessarie alla ripresa: non è una
cache da eliminare. Journal e cursori della versione 1.1 restano compatibili.
`logs/`, `state/`, credenziali e cache Python sono esclusi dai nuovi file Git;
le copie già tracciate nella cronologia restano tali.

Codici di uscita: `0` completato, `1` errore preliminare, `2` candidati saltati
o asset richiesto non trovato, `3` operazione pendente/da riprendere,
`130` interruzione gestita.

## Struttura

- `immich_native.py`: punto di ingresso mantenuto per script e cron esistenti.
- `immich_still/cli.py`: argomenti, controlli Docker, selezione, audit e ripresa.
- `engine.py`: conversione di un asset, upload, verifiche e cestinamento.
- `gcam.py` e `metadata.py`: codec JPEG/MicroVideo e conservazione dei metadati.
- `api.py` e `db.py`: accesso a Immich e osservazioni PostgreSQL.
- `journal.py`: persistenza delle run e del cursore.
- `core.py`: configurazione e primitive condivise.

## Verifiche locali

```bash
python3 -m unittest discover -s tests -v
ruff check .
ruff format --check .
```

I test usano dati sintetici, directory temporanee e servizi simulati: non
richiedono credenziali, Docker o un server Immich. Ruff serve soltanto allo
sviluppo. Le verifiche locali non sostituiscono un audit sulla propria versione
di Immich, da cui dipendono API e schema PostgreSQL.
