# Guida di dobermann

dobermann sorveglia la strada fra la tua rete e internet: il router, l'eventuale modem, l'operatore.
Misura di continuo, e quando la connessione si guasta ti scrive **dove** si è rotta, **perché**, con
quali **prove**, **quanto** è durata e **cosa fare**. Ogni settimana (o ogni giorno) ti manda un
riepilogo.

Questa guida è per chi lo installa e lo usa. Per insegnargli a leggere un nuovo apparato c'è [`ADAPTERS.md`](ADAPTERS.md).

- [1. Cosa serve](#1-cosa-serve)
- [2. Installazione su Proxmox](#2-installazione-su-proxmox)
- [3. Installazione su un altro Linux](#3-installazione-su-un-altro-linux)
- [4. Primo accesso](#4-primo-accesso)
- [5. La catena verso internet](#5-la-catena-verso-internet)
- [6. Le notifiche email](#6-le-notifiche-email)
- [7. Gmail come sistema di invio, passo per passo](#7-gmail-come-sistema-di-invio-passo-per-passo)
- [8. Quando avvisare e il riepilogo](#8-quando-avvisare-e-il-riepilogo)
- [9. Leggere un referto](#9-leggere-un-referto)
- [10. Il pannello](#10-il-pannello)
- [11. Manutenzione](#11-manutenzione)
- [12. Problemi comuni](#12-problemi-comuni)
- [13. Dove stanno le tue password](#13-dove-stanno-le-tue-password)

---

## 1. Cosa serve

- **Una macchina sempre accesa nella rete di casa**: un server Proxmox (consigliato: dobermann vive in
  un suo container) oppure un qualunque Linux con Python 3.11 o più recente.
- **Un indirizzo IP libero** della tua rete per il container, fuori dall'intervallo che il router assegna
  da solo (DHCP). È l'indirizzo dove aprirai il pannello.
- **Una casella di posta da cui spedire** le notifiche: va bene Gmail (vedi il [capitolo 7](#7-gmail-come-sistema-di-invio-passo-per-passo))
  o il server SMTP di qualunque altro fornitore.

dobermann sorveglia la connessione **dalla macchina su cui gira**: se quella macchina è spenta, la
sorveglianza si ferma con lei.

## 2. Installazione su Proxmox

Sull'host Proxmox, come root, in una copia del repository:

```bash
git clone https://github.com/ilrost/dobermann-net-watchdog.git /opt/dobermann-net-watchdog
cd /opt/dobermann-net-watchdog

# 1) prova a vuoto: mostra cosa farebbe, non cambia nulla
python3 tools/proxmox_ct.py --ip 192.168.1.50/24

# 2) se il piano va bene, crea il container e installa
python3 tools/proxmox_ct.py --ip 192.168.1.50/24 --apply
```

Sostituisci `192.168.1.50/24` con l'indirizzo libero scelto (la parte `/24` è la maschera della rete
di casa, quasi sempre `/24`). Lo script:

- crea un container **non privilegiato** con il modello Debian già presente sull'host (lo scarica
  solo se non ce n'è nessuno), 1 core, 256 MB di memoria, 4 GB di disco, avvio automatico con l'host;
- installa dentro Python, ping, openssl e **nullmailer** (il piccolo server di posta che tiene in coda
  le mail mentre internet è giù);
- copia dobermann in `/opt/dobermann` e lo installa con due servizi: `dobermann` (le misure) e
  `dobermann-web` (il pannello), entrambi senza privilegi di root;
- crea un certificato HTTPS per il pannello e ne stampa l'**impronta SHA-256**: annotala, ti serve al
  primo accesso.

Opzioni utili: `--id 210` (numero del container), `--gw 192.168.1.1` (gateway, se non è quello
dell'host), `--bridge vmbr1`, `--storage`, `--disk`, `--ip dhcp` (sconsigliato: l'indirizzo del pannello
potrebbe cambiare). `python3 tools/proxmox_ct.py --help` le elenca tutte.

## 3. Installazione su un altro Linux

Su una macchina Debian/Ubuntu, come root:

```bash
apt install python3 iputils-ping openssl ca-certificates nullmailer
git clone https://github.com/ilrost/dobermann-net-watchdog.git /opt/dobermann
cd /opt/dobermann
python3 tools/install.py            # prova a vuoto
python3 tools/install.py --apply    # installa
```

Se l'installazione di nullmailer fa domande, le risposte predefinite vanno bene: i dati del fornitore
di posta li inserirai dal pannello.

## 4. Primo accesso

1. Apri **`https://<indirizzo del container>/`** (l'indirizzo con `http://` ti rimanda lì da solo).
2. Il browser avvisa che il certificato **non è riconosciuto**: è normale, è un certificato creato da
   dobermann e non da un'autorità pubblica. Apri i dettagli del certificato e **confronta l'impronta
   SHA-256** con quella stampata durante l'installazione. Se coincide, accetta. Va fatto una volta per
   ogni dispositivo.
3. **Scegli subito la password del pannello** (almeno 8 caratteri): finché non lo fai, chiunque nella
   rete di casa potrebbe sceglierla al posto tuo.
4. Si apre la pagina **Impostazioni**: prosegui con la catena (capitolo 5) e le notifiche (capitolo 6).

## 5. La catena verso internet

La catena è l'elenco degli apparati fra dobermann e internet, in ordine: per esempio
`dobermann → FRITZ!Box → modem 5G → internet`. Serve a dobermann per capire **dove** si rompe la
connessione.

- Premi **Rileva**: dobermann trova il router (il gateway) e, se il router ha un indirizzo esterno
  privato, l'apparato sopra di lui.
- **Un modem in modalità bridge o IP passthrough non si vede da solo**: scrivi il suo indirizzo nel
  campo "Apparato a monte da aggiungere" (per esempio `192.168.2.1`) e premi di nuovo **Rileva**.
- Puoi **rinominare** gli apparati e **togliere** quelli che non c'entrano; premi **Salva la catena**.
- La colonna **Lettura** dice come dobermann legge ogni apparato: *standard* (UPnP, il modo che quasi
  tutti i router offrono), *non ufficiale* (l'interfaccia web di un modello specifico) oppure *solo
  dall'esterno* (dobermann lo sorveglia con ping e misure verso internet).
- Il campo **Password** compare solo per gli apparati che con la password danno più dati. È
  facoltativo; alcuni apparati accettano un solo accesso alla volta e dobermann si ritira per qualche
  minuto quando entri tu.

Sotto la catena dobermann scrive il **tipo di linea**: *radio* (l'ultimo apparato, quello più vicino a
internet, è un modem 4G/5G) oppure *cablata* (fibra, DSL, cavo). Solo con linea radio compaiono il
grafico del segnale, le notizie sulla rete mobile nelle mail e la categoria "Ripiego sul 3G".

## 6. Le notifiche email

In **Impostazioni → Notifiche email**:

| Campo | Cosa mettere |
|---|---|
| Invia le notifiche | spunta per attivarle |
| Destinatario | l'indirizzo dove vuoi ricevere le mail |
| Mittente | lascialo vuoto: viene usato l'utente SMTP (molti fornitori, Gmail compreso, accettano solo quello) |
| Invio tramite | **Relay locale (consigliato)**: le mail scritte mentre internet è giù restano in coda e partono appena torna. **SMTP diretto**: dobermann parla da solo con il fornitore, senza coda |
| SMTP: server, porta, sicurezza, utente, password | i dati SMTP del tuo fornitore di posta (nelle sue istruzioni per Outlook/Thunderbird, alla voce "server in uscita") |
| Indirizzo di questo pannello | per esempio `https://192.168.1.50/watchdash`: finisce nelle mail come link |

Poi:

1. premi **Invia una mail di prova**: si collega direttamente al server del fornitore, così un dato
   sbagliato si vede subito. Se fallisce, il pannello scrive il motivo (server non trovato, porta o
   sicurezza sbagliate, password rifiutata…);
2. se la mail arriva (guarda anche nello spam la prima volta), premi **Salva**.

La password salvata vale **solo per il server e l'utente con cui l'hai salvata**: se cambi uno dei
due, il pannello ti chiede di riscriverla. Non viene mai mostrata né rimandata al browser.

**Inviare "da solo", senza un fornitore di posta, non è possibile in pratica**: le mail che partono
da una connessione di casa (IP dinamico, CGNAT, porta 25 spesso bloccata) vengono rifiutate o finiscono
nello spam. Serve sempre un server SMTP esistente da cui farle partire: Gmail è il più semplice.

## 7. Gmail come sistema di invio, passo per passo

Gmail non accetta la password con cui entri nel tuo account per spedire da un programma: vuole una
**password per app**, una password di **16 lettere** creata apposta e revocabile in qualunque momento.

**Consiglio**: usa un **account Gmail dedicato** solo all'invio, creato apposta per dobermann, e fai arrivare le notifiche al tuo indirizzo di tutti i giorni. Così la
password salvata in dobermann non dà accesso alla tua posta personale.

### 7.1 Attiva la verifica in due passaggi

Le password per app esistono solo se l'account ha la verifica in due passaggi.

1. Entra con l'account che spedirà le mail.
2. Apri **<https://myaccount.google.com/signinoptions/twosv>** e attivala, se non lo è già.

### 7.2 Crea la password per app (16 caratteri)

1. Apri **<https://myaccount.google.com/apppasswords>** (sempre con l'account che spedisce).
2. Come nome scrivi `dobermann` e premi **Crea**.
3. Google mostra la password **una sola volta**, in quattro gruppi da quattro lettere, per esempio
   `abcd efgh ijkl mnop`. Copiala **senza spazi**: `abcdefghijklmnop`.

Se la pagina dice che l'impostazione non è disponibile: la verifica in due passaggi non è attiva,
oppure l'account usa solo chiavi di sicurezza / la "Protezione avanzata", oppure è un account di
lavoro o scuola il cui amministratore ha disattivato le password per app. Le istruzioni di Google:
<https://support.google.com/accounts/answer/185833>.

### 7.3 Inseriscila in dobermann

In **Impostazioni → Notifiche email**:

| Campo | Valore |
|---|---|
| Invia le notifiche | ✔ |
| Destinatario | il tuo indirizzo di tutti i giorni (anche non Gmail) |
| Mittente | vuoto |
| Invio tramite | Relay locale (consigliato) |
| SMTP: server | `smtp.gmail.com` |
| SMTP: porta | `587` |
| SMTP: sicurezza | STARTTLS |
| SMTP: utente | l'indirizzo Gmail che spedisce, per intero |
| SMTP: password | le 16 lettere, **senza spazi** |

Premi **Invia una mail di prova**, controlla che arrivi (la prima volta anche nello spam: segnala "non
spam"), poi **Salva**. Con il relay locale il pannello scrive i dati anche nella configurazione di
nullmailer.

In alternativa a 587/STARTTLS funziona anche la porta `465` con sicurezza **SSL/TLS**.

### 7.4 Se un giorno vuoi revocarla

Da <https://myaccount.google.com/apppasswords> elimina la password `dobermann`: da quel momento
dobermann non può più spedire. Anche **cambiare la password dell'account Google revoca tutte le
password per app**: dopo un cambio, creane una nuova e inseriscila nel pannello.

### 7.5 Errori tipici con Gmail

| Il pannello dice | Cosa fare |
|---|---|
| "il server ha chiuso la connessione al momento del login" oppure "ha rifiutato utente o password" | stai usando la password dell'account invece di quella per app, oppure l'hai copiata con gli spazi o incompleta (deve essere di 16 lettere) |
| "non può contenere spazi" | togli gli spazi dalla password per app |
| "server o utente SMTP sono cambiati: riscrivi la password" | hai cambiato server o utente: la password salvata vale solo per quelli vecchi, riscrivila |
| "il server non risponde su quella porta" | controlla porta e sicurezza: 587 con STARTTLS, oppure 465 con SSL/TLS |

## 8. Quando avvisare e il riepilogo

In **Impostazioni → Quando avvisare** scegli, per ogni tipo di evento, quando ricevere una mail:

| Scelta | Significato |
|---|---|
| **Subito** | una mail appena l'evento comincia |
| **Se dura più di…** | una mail solo se l'evento è ancora in corso dopo quei minuti; se finisce prima, compare nel riepilogo |
| **Solo nel riepilogo** | nessuna mail, solo una riga nel riepilogo |
| **Mai** | resta solo nel pannello |

Le impostazioni iniziali: **Internet assente** se dura più di 2 minuti; **DNS che non risponde** se
dura più di 5 minuti; **Rinnovo / cambio IP**, **Rete lenta o instabile** e **Ripiego sul 3G** (solo
con linea radio) solo nel riepilogo. Un rinnovo dell'IP da parte dell'operatore dura in genere pochi
secondi: con queste impostazioni non ti scrive. Quando parte la mail "è giù", a guasto finito arriva
anche quella "è tornata" con il referto completo.

**Riepilogo**: spento, ogni giorno o ogni settimana, al giorno e all'ora che scegli (iniziale: lunedì
alle 8). Contiene la disponibilità della connessione, gli eventi per tipo confrontati con il periodo
prima, il più lungo, i cambi di IP e, con linea radio, il tempo su ogni tecnologia (5G/4G/3G), le cadute
su tecnologie peggiori, il segnale tipico e peggiore. Con la spunta "anche se non è successo nulla"
arriva anche nelle settimane tranquille: conferma che dobermann è vivo e che le mail arrivano.
**Invia ora un riepilogo degli ultimi 7 giorni** ti mostra subito com'è fatto.

## 9. Leggere un referto

```
DOVE
  dobermann ✔ → FRITZ!Box ✔ → modem ✔ → operatore ✘ → internet ?
  Il guasto era fra il modem e l'operatore: in casa tutto funzionava.
PERCHÉ   la causa in parole semplici, e la sequenza se ce n'è stata più di una
PROVE    quello che dobermann ha misurato o gli apparati hanno riportato
QUANTO   inizio, fine, durata, e quante volte è già successo negli ultimi 7 e 30 giorni
COSA FARE
```

Segni: **✔** funzionava, **✘** lì si è rotto, **⚠** funzionava ma degradato, **?** non verificabile
(oltre il punto rotto). dobermann scrive solo ciò che ha misurato: se un dato manca, la frase manca,
non viene indovinata. La mail inviata mentre il guasto è in corso ha la stessa struttura con i dati
dell'ultimo campione; se internet è giù parte (dalla coda) al ritorno della linea.

## 10. Il pannello

- **Oggi**: incidenti della giornata, grafici di banda, latenza, perdita e (con linea radio) segnale,
  cambi di IP. Si aggiorna da solo; il cursore in alto cambia l'intervallo delle misure.
- **Storico**: giorno, settimana, mese, anno; clic su un incidente per vederlo sul grafico.
- **Esporta**: i campioni o i riepiloghi orari in CSV.
- **Impostazioni**: catena, notifiche, quando avvisare, misure (server di riferimento, DNS, soglie,
  giorni di conservazione dei campioni: gli incidenti e i riepiloghi orari restano per sempre),
  password del pannello.

Le modifiche si applicano al campione successivo, senza riavviare nulla.

## 11. Manutenzione

**Aggiornare dobermann** (sull'host Proxmox, nella copia del repository aggiornata):

```bash
cd /opt/dobermann-net-watchdog && git pull
python3 tools/proxmox_ct.py --update <numero del CT> --apply
```

Sostituisce il codice (il precedente resta in `/opt/dobermann.old`), installa eventuali pacchetti
nuovi e riavvia i servizi. Configurazione, password e dati non vengono toccati.

**Dentro il container** (`pct enter <numero del CT>`):

```bash
systemctl status dobermann dobermann-web     # stato dei servizi
journalctl -u dobermann -n 50                # ultime righe del registro
dobermann mail-test                          # una mail di prova con le impostazioni salvate
dobermann probe 192.168.1.1                  # cosa dobermann sa leggere da un apparato
```

**Password del pannello dimenticata**: dentro il container cancella `/etc/dobermann/admin.json` e
riapri il pannello: ti chiede di sceglierne una nuova. Le altre impostazioni restano.

**Nuovo certificato HTTPS** (per esempio dopo un cambio di indirizzo):

```bash
cd /opt/dobermann && python3 tools/make_cert.py --force --san 192.168.1.50
systemctl restart dobermann-web
```

**Backup**: i backup del container fatti da Proxmox comprendono configurazione, password e storico.

## 12. Problemi comuni

| Problema | Cosa fare |
|---|---|
| Il browser avvisa del certificato | normale al primo accesso: confronta l'impronta e accetta (capitolo 4) |
| "Troppi tentativi sbagliati: riprova fra un minuto" | dopo 5 password errate in un minuto il pannello si ferma per un minuto |
| La mail di prova arriva ma gli avvisi no | controlla "Invia le notifiche" e le regole in **Quando avvisare**: molti eventi vanno solo nel riepilogo |
| Le mail arrivano in ritardo | se internet era giù, partono dalla coda al ritorno della linea: è voluto |
| "Rileva" trova solo il router | un modem in bridge o IP passthrough non si vede da solo: aggiungi il suo indirizzo |
| Un apparato è "solo dall'esterno" | dobermann non sa leggerlo (per esempio UPnP spento sul router): lo sorveglia comunque con ping e misure verso internet |
| Nel pannello non c'è il grafico del segnale | compare solo con linea radio e se il modem fornisce i dati |

## 13. Dove stanno le tue password

- **Password del pannello**: in `/etc/dobermann/admin.json`, non in chiaro ma come impronta (PBKDF2).
- **Password SMTP e degli apparati**: in `/etc/dobermann/secrets.json`, leggibile solo dall'utente
  `dobermann`. Con il relay locale, la password SMTP è anche in `/etc/nullmailer/remotes` (leggibile
  solo da `dobermann` e da nullmailer), perché nullmailer deve poterla usare.
- Le password **non vengono mai** rimandate al browser, scritte nei registri o nelle mail.
- Servono in chiaro per essere usate: chi è amministratore del container o dell'host Proxmox può
  leggerle, e sono nei backup del container. Per questo conviene una password per app dedicata e
  revocabile (capitolo 7).
