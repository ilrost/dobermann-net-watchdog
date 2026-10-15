# API degli adattatori

Come dobermann parla con router e modem, e come aggiungerne uno nuovo.

## L'idea

Non esiste un'interfaccia che tutti i router parlano. La lingua
comune la definisce dobermann:

- **i fatti** (`dobermann/facts.py`): un elenco fisso di informazioni con nome, tipo e significato,
  per esempio `wan.status`, `wan.ip`, `cell.service`, `cell.rat`. Un fatto significa la stessa cosa
  qualunque sia l'apparato che lo produce;
- **gli adattatori** (`dobermann/adapters/`): ognuno traduce una famiglia di apparati (un protocollo
  standard, o l'API di un produttore) in fatti.

La diagnosi legge solo fatti e non sa quale router c'è dietro. Un fatto che manca vale
**sconosciuto**, mai "guasto": un router che dice poco dà una diagnosi meno dettagliata, non sbagliata.

## Adattatori disponibili

| id | Apparati | Livello | Password | Verificato su |
|---|---|---|---|---|
| `upnp_igd` | qualunque router con UPnP IGD attivo | standard | no | FRITZ!Box 7530 AX |
| `zte_goform` | router 4G/5G ZTE | **non ufficiale** | facoltativa | ZTE MC888B, firmware BD_STDPLMC888BV1.0.0B08 |

`zte_goform` senza password legge servizio, tecnologia radio, segnale, operatore, modello e firmware,
senza toccare l'accesso amministratore. Con la password aggiunge cella, banda, SINR e IP WAN. Il
router ammette un solo accesso alla volta: quando ti colleghi tu alla sua pagina, dobermann se ne
accorge e smette di collegarsi per 30 minuti. Una password rifiutata viene riprovata solo dopo
un'ora, così non si rischia il blocco dell'account sul router.

## Provare un apparato

```bash
python3 -m dobermann probe 192.168.1.1                  # chi lo riconosce + un campione di fatti
python3 -m dobermann probe 192.168.2.1 --samples 3      # tre campioni, 5 s l'uno dall'altro
python3 -m dobermann probe 192.168.2.1 --adapter zte_goform --password-file /percorso/file
python3 -m dobermann facts                              # il vocabolario completo
```

La password si legge solo da un file (o da un prompt nascosto con `--ask-password`), mai dalla riga
di comando.

## Scrivere un adattatore

1. Crea `dobermann/adapters/<nome>.py` con una classe che estende `Adapter`:

   ```python
   class MioRouter(Adapter):
       id = "mio_router"                 # univoco, finisce nella configurazione
       name = "Mio Router (API ufficiale)"
       level = LEVEL_STANDARD            # o LEVEL_UNOFFICIAL per API non documentate
       accepts_credentials = False       # True se la password aggiunge dettaglio
       needs_credentials = False         # True solo se senza password non legge niente
       single_session = False            # True se il suo login può sloggare l'utente
       provides = frozenset({"wan.status", "wan.ip"})

       @classmethod
       def detect(cls, host, timeout=3.0):
           ...  # veloce, senza password; Detection(cls.id, confidenza, vendor=..., model=...) o None

       def sample(self):
           ...  # dict di fatti; {} se l'apparato non risponde
   ```

2. Aggiungila a `ADAPTERS` in `dobermann/adapters/__init__.py`.
3. Scrivi i test con un finto apparato (vedi `tests/fakes.py`): mai sulla rete vera.

### Regole del contratto

- `detect()` legge e basta: niente password, niente login, niente modifiche. Restituisce una
  confidenza da 0 a 1; se l'apparato somiglia ma probabilmente non è il router verso internet (es. un
  repeater), confidenza bassa e una nota che lo spiega.
- `sample()` non solleva eccezioni e non si blocca: ogni chiamata di rete ha un timeout breve.
  `run_sample()` lo garantisce comunque: un'eccezione diventa un errore, non ferma il campionamento.
- Solo fatti del vocabolario e dichiarati in `provides`. Il resto viene scartato e segnalato.
- Valori "vuoti" del dispositivo (`""`, `N/A`, `0.0.0.0` per un IP) non sono fatti: lasciali fuori.
- Se servono credenziali, l'adattatore restituisce comunque quello che può leggere senza.
- Se `single_session = True`, l'adattatore deve accorgersi quando l'utente prende la sessione e
  ritirarsi per un po', come fa `zte_goform`.
- Un fatto nuovo si aggiunge a `facts.py` solo se ha lo stesso significato per ogni apparato che
  potrebbe produrlo. Un campo che esiste solo su un modello non è un fatto.
