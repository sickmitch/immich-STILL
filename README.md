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
`thumbnail_poll_interval` (`2`), `recovery_attempts` (`3`, da 1 a 10),
`recovery_delay` (`5`, da 1 a 60). I tempi sono in secondi.
La password viene letta dal file host protetto, se indicato, oppure dalle
variabili `POSTGRES_PASSWORD_FILE` / `POSTGRES_PASSWORD` del container;
in loro assenza viene usata l’autenticazione libpq già configurata.

## Uso

```bash
./immich-still.sh --help
./immich-still.sh --show-paths
./immich-still.sh --convert
./immich-still.sh --audit --limit 10
./immich-still.sh --convert --limit 10
./immich-still.sh --verify /percorso/state/RUN
./immich-still.sh --list-runs
./immich-still.sh --verify-journal latest
./immich-still.sh --check-db
```

**Senza opzioni viene avviata la conversione**, con limite `0` (tutti i candidati).
La normale invocazione, anche da cron, è sufficiente:

```bash
/home/mike/data/tech/script/server/immich-still-native-v1.1.0/immich-still.sh --convert
```

`conf.json` viene cercato accanto allo script, indipendentemente dalla directory
corrente. Per lo stato la precedenza è: `--state-dir`, directory padre del RUN
per i comandi su una run esplicita, `native_state_dir` nel JSON, infine `state/`
accanto a `conf.json`. Un `native_state_dir` relativo parte dalla directory del
JSON; un `--state-dir` relativo parte dalla directory corrente. Le opzioni
`--config` e `--state-dir` restano disponibili per gli override.

`--show-paths` mostra in JSON i percorsi richiesti ed effettivi, la loro esistenza,
la provenienza della directory di stato e le opzioni della selezione. Non espone
credenziali, non contatta Docker/Immich e non crea directory o journal. Può essere
combinato con le altre opzioni per controllarle senza eseguirle. Se `conf.json`
manca, mostra i default e `exists: false`; se esiste, ne controlla i permessi.

`--non-interactive` rimane accettato per compatibilità, ma non serve: lo script
non chiede conferme interattive. `--audit` prepara e controlla i candidati, scrive
il journal locale e non modifica Immich né avvia recuperi di conversioni pendenti.

## Filtro temporale e cursore

**Non esiste più una data minima fissata nel codice.** Per impostarla, aggiungere
facoltativamente a `conf.json` una data ISO con fuso orario, per esempio:

```json
"sanity_wall": "2026-01-01T00:00:00Z"
```

Se la chiave è assente o vale `null`, non viene applicato alcun filtro temporale.
`--no-sanity-wall` ignora il valore configurato per quel singolo avvio;
`--sanity-wall` applica il valore del JSON, se presente, senza introdurre un default.
Valori vuoti, date senza fuso e valori non validi vengono rifiutati.

La scansione principale parte dalla foto più vecchia nella timeline, oppure dal
cursore, ordinando per data di scatto e UUID. Un secondo riferimento ai caricamenti
recupera i nuovi upload che hanno una data di scatto precedente al cursore. Anche
questo recupero rispetta l'eventuale filtro temporale. `--include-archived` include
gli asset archiviati; `--asset-id UUID` tratta una singola foto senza spostare il
cursore condiviso, anche se l'operazione viene recuperata successivamente.

Al primo avvio con un cursore della vecchia versione, o cambiando i filtri della
selezione, vengono riesaminati gli originali ancora attivi. Il vecchio cursore
viene sostituito solo dopo un avanzamento registrato. Gli scarti espliciti di
formati non supportati vengono registrati; gli errori temporanei restano pendenti.
Una still già presente non viene adottata a meno che il journal ne provi la
provenienza dalla stessa operazione.

## Recupero automatico

Ogni avvio in conversione recupera prima le run pendenti e poi seleziona nuove
foto. Non occorre più eseguire `--resume`: l'opzione resta disponibile per trattare
esplicitamente una singola run. `--limit` è condiviso fra recuperi e nuovi candidati;
gli asset già completati non consumano il limite.

Per errori di rete/I/O e metadati o miniature ancora in elaborazione vengono fatti
fino a `recovery_attempts` tentativi per asset nello stesso avvio, con attese di
`recovery_delay`, poi due volte quel valore, fino a un massimo di 60 secondi.
Ogni tentativo rilegge lo stato persistito prima di decidere se caricare o adottare
la still. Un upload riuscito con risposta persa viene riconosciuto dalla provenienza;
un cestinamento già confermato dal server viene verificato senza ripetere il DELETE.
I file di staging mancanti possono essere rigenerati dall'originale ancora attivo,
soltanto se checksum, metadati e risultato coincidono con il journal.

Esauriti i tentativi, l'asset rimane nella coda del journal, viene escluso dalle
nuove selezioni e sarà ritentato al prossimo avvio normale. Gli altri asset possono
proseguire. Il processo termina: non è un demone e non installa un cron; per
riprovare nel tempo usa la normale pianificazione già presente.

Un cursore corrotto viene archiviato come `cursor-invalid-*.json` e ricostruito con
una nuova scansione. Journal o backup alterati, perdita del lock e identità diverse
non autorizzano mai cancellazioni forzate. Un asset non verificabile resta intatto;
la perdita delle prove del journal può impedire il recupero sicuro. Il recupero
automatico riprende operazioni verificabili, non ricrea credenziali o dati perduti.

`state/` conserva journal, cursore e copie necessarie alla ripresa: non è una
cache da eliminare. Le run della versione 1.1 possono essere recuperate; i cursori
v1 vengono migrati alla nuova scansione. Non usare la vecchia versione dello script
sulla directory di stato dopo la migrazione del cursore.
`logs/`, `state/`, credenziali e cache Python sono esclusi dai nuovi file Git;
le copie già tracciate nella cronologia restano tali.

Codici di uscita: `0` completato, `1` errore preliminare, `2` candidati saltati
o asset richiesto non trovato, `3` operazioni ancora pendenti, riprese automaticamente al prossimo avvio,
`130` interruzione gestita.

## Log

Log compatti: un esito per asset (`OK`, `SKIP`, `PENDING`) e un riepilogo per run
(`END`, `AUDIT`, `RECOVER`). I tentativi intermedi non producono messaggi ripetuti.
Ogni messaggio è su una riga, massimo 200 caratteri oltre al timestamp UTC.
Errori completi e dettagli operativi restano nei JSON del journal.

## Struttura

- `immich_native.py`: punto di ingresso mantenuto per script e cron esistenti.
- `immich_still/cli.py`: argomenti, diagnostica, controlli Docker e avvio.
- `workflow.py`: selezione, tentativi e recupero automatico delle run.
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
