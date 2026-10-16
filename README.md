# X Repost Downloader

Salva localmente i video dei repost visibili di un profilo X configurato. Playwright osserva la timeline nella sessione autorizzata; yt-dlp scarica e combina i media. Stato SQLite e archivio locale evitano duplicati e permettono di riprendere il lavoro. Non automatizza password, CAPTCHA, checkpoint o impostazioni dell’account.

## Installazione e configurazione

Richiede Python3.12+, le dipendenze fissate in pyproject.toml, Chromium e ffmpeg/ffprobe. Per un ambiente Python isolato installare il pacchetto con `python -m pip install .`; il comando è `x-repost-downloader`. Per un runtime di sistema amministrato è disponibile la capsula opzionale descritta sotto.

La configurazione non segreta è in `~/.config/x-repost-downloader/config.toml` (0600). Account, percorsi e limiti sono personalizzabili; `config.example.toml` mostra un archivio su supporto rimovibile. Il profilo pubblico predefinito del progetto resta configurabile tramite `account`.

Senza configurazione, l’archivio è sotto `~/.local/share/x-repost-downloader/archive`; video, metadati, SQLite, stato e log hanno sottodirectory dedicate. Il mount predefinito è la root del filesystem; per un disco esterno impostare esplicitamente `required_mount` e `output_dir`. La guardia sul mount e sullo spazio libero rimane attiva: non usare esempi di percorso come configurazione reale. Stato, profili, cookie e media non appartengono al repository.

## Autenticazione

Il setup può leggere la sessione del browser configurato attraverso yt-dlp e il provider nativo del keyring, limitando l’import ai domini X/Twitter in un profilo Chromium dedicato. Il browser principale non viene pilotato o chiuso. Il profilo dedicato è una sessione autenticata privata; proteggerlo e conservarlo fuori Git. Non inserire cookie, password o token in questo repository.

`setup --reauthenticate` aggiorna il profilo dalla sessione autorizzata. Il fallback `setup --direct-x-login` è esplicito e richiede login manuale; non aggira controlli del servizio.

## Uso

```bash
x-repost-downloader --help
x-repost-downloader setup
x-repost-downloader diagnose --max-scrolls 4
x-repost-downloader check
x-repost-downloader backfill
x-repost-downloader db-backfill
x-repost-downloader availability-check
x-repost-downloader status
x-repost-downloader doctor
```

`diagnose` conta elementi della timeline senza scaricare media o modificare lo stato dei download; `check --dry-run` ha invece un comportamento diverso e può contattare yt-dlp. Setup, diagnosi live, doctor e download richiedono accesso esplicito alla sessione e alla rete: i test CI non li eseguono.

La discovery riconosce i repost e distingue i post citati annidati. Check elabora nuovi elementi e retry; backfill scorre fino al termine o a un limite tecnico. Il download diventa completato solo dopo successo di yt-dlp, esistenza del file e verifica ffprobe. Il formato preferisce video fino a720p, con fallback al migliore disponibile. Lock, pacing, cooldown e stato atomico restano attivi.

Ogni repost è indicizzato per URL/ID univoco con stato pending/downloaded/failed. db-backfill indicizza file già presenti senza spostarli o riscaricarli; le associazioni non certe rimangono intatte. availability-check esegue controlli senza download sui record dovuti; errori tecnici o di autenticazione restano unknown, distinti dalla cancellazione esplicita del contenuto.

## Servizi opzionali

I file systemd sono **template da configurare**, non una descrizione di un’installazione attiva. Sostituire REPLACE_WITH_SERVICE_USER, verificare HOME, configurazione, profilo e mount dell’archivio. `/srv/media` è solo un esempio. Conservare RequireMountsFor, ProtectSystem, ProtectHome e ReadWritePaths coerenti con la propria configurazione privata. archive.fstab.example contiene soltanto un placeholder; non installarlo senza un’identità filesystem verificata.

I timer preservano le cadenze e la ripresa previste; non attivarli durante una revisione del codice. Configurazione del sistema, autenticazione e dati sono responsabilità dell’operatore e rimangono esterni.

## Runtime di sistema

La capsula `capsules/system_runtime` installa soltanto il codice e il launcher sotto `/usr/local`, con dipendenze amministrate globalmente; non installa dipendenze e non legge stato o profili. Richiede root e un runtime fermo per l’aggiornamento. Il checkout non aggiorna automaticamente un servizio installato. Una pubblicazione del codice non costituisce un deploy o una prova di download.

## Versione e verifiche offline

NV.txt conserva il numero di versione del progetto; le modifiche ordinarie non lo incrementano. Binding host e catalogo comandi rimangono presso l’owner privato dell’integrazione. Storia e attribuzioni originali sono conservate privatamente; l’identità di contatto dei commit di questo candidato è dichiaratamente redatta. Non viene inventata una licenza.

```bash
PYTHONPATH=src python -m unittest discover -s tests -p test_config.py
```

Questi test usano solo configurazioni sintetiche e directory temporanee: nessun browser, cookie, rete, dispositivo o servizio reale.

## Rimozione

Disabilitare i propri timer e servizi prima di rimuovere il codice installato. Non cancellare automaticamente archivi, SQLite, configurazioni o profili: contengono dati e sessioni private da conservare o rimuovere separatamente.
