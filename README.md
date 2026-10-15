# dobermann-net-watchdog

Il cane da guardia della tua connessione a internet. *(Cercato come "doberman"? È questo, con due n.)*

dobermann sorveglia il percorso fra la tua rete e internet (router, modem, provider). Quando la
connessione si guasta non ti dice soltanto "non va": ti manda una mail con **dove** si è rotto,
**perché**, le **prove** raccolte, **quanto** è durato e **cosa fare**.

Funziona con qualunque router che parli UPnP (quasi tutti), legge in più alcuni modem 4G/5G, e si
installa con un comando come container su Proxmox o su un qualunque Linux. Notifiche solo via email
(anche con Gmail), con regole per tipo di evento e un riepilogo periodico.

- **Installare e usare**: [`docs/GUIDA.md`](docs/GUIDA.md), compresa la configurazione di Gmail
- Scrivere un adattatore per un nuovo apparato: [`docs/ADAPTERS.md`](docs/ADAPTERS.md)
