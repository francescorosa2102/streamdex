# Streamdex — Raccolta dati TikTok

Raccoglie periodicamente da una lista di account TikTok:
- **follower**
- **like totali** del profilo
- **engagement medio** (best-effort) sugli ultimi video

I dati vengono salvati in `tiktok_data.csv`, in formato "lungo": **una riga per
(data_ora, account)**, con `data_ora` nello stesso formato del foglio Twitch
(`gg/mm/aaaa HH:MM:SS`). Così puoi trattarlo come le tue serie storiche Twitch.

> ⚠️ Usa la libreria **non ufficiale** [TikTok-Api](https://github.com/davidteather/TikTok-Api).
> Può smettere di funzionare se TikTok cambia qualcosa. Lo script è scritto per
> essere **resiliente**: se un account fallisce, gli altri proseguono; se fallisce
> solo l'engagement, follower e like vengono comunque salvati.

## 1. Installazione (già fatta in questo ambiente)

```powershell
pip install -r requirements.txt
python -m playwright install chromium
```

## 2. Configurare gli account

Modifica `accounts.txt`: un account per riga, **senza @**. Righe con `#` e righe
vuote sono ignorate.

```
khaby.lame
elisatoffoli
# commento ignorato
```

## 3. Impostare il msToken (da DevTools) ed eseguire

Metodo attuale: `msToken` **manuale**, che sappiamo funzionare bene.

1. Apri `tiktok.com` nel browser → DevTools (F12) → **Application → Cookies →
   `https://www.tiktok.com`** → copia il valore di **`msToken`**.
2. Impostalo come variabile d'ambiente e avvia:

```powershell
$env:TIKTOK_MS_TOKEN = "il_tuo_token"

python tiktok_collector.py --once   # una raccolta di prova
python tiktok_collector.py          # loop ogni 15 min
```

Il token **scade**: quando ricominciano i timeout, lo script si ferma con
`INTERROTTO ... il msToken e' probabilmente scaduto` — rigeneralo da DevTools e
rilancia.

> Nota: esiste anche `tiktok_session_setup.py`, un tentativo (accantonato) di
> automatizzare il refresh via sessione persistente Playwright. TikTok però
> reindirizza al login e bot-flagga il browser headless, quindi per ora si usa il
> token manuale. Il file resta come riferimento, ma **non è usato dal collector**.

## 4. Proxy — scaffolding pronto (commentato) per il futuro

Se un domani il token manuale non bastasse e servisse un proxy (meglio se
residenziale), è già predisposto in `tiktok_collector.py` ma **commentato**. Per
attivarlo (2 passi, spiegati nei commenti in cima al file e in `create_sessions`):

1. togli il commento al blocco `build_proxies()` / `TIKTOK_PROXY` in cima al file;
2. togli il commento alla riga `# proxies=build_proxies(),` dentro `create_sessions()`.

Poi imposti `$env:TIKTOK_PROXY = "http://utente:password@host:porta"` (o `socks5://…`).

## Parametri (in cima a `tiktok_collector.py`)

| Parametro | Default | Significato |
|---|---|---|
| `INTERVAL_MIN` | 15 | minuti tra una raccolta e l'altra |
| `COLLECT_ENGAGEMENT` | True | calcolare l'engagement sugli ultimi video |
| `VIDEOS_TO_ANALYZE` | 10 | quanti ultimi video usare per l'engagement |
| `SLEEP_BETWEEN_ACCOUNTS` | (3, 7) | pausa casuale (s) tra account, riduce i blocchi |
| `INFO_RETRIES` | 2 | tentativi sul recupero profilo prima di segnare errore |

## Colonne del CSV

`data_ora, account, followers, likes_totali, video_totali, engagement_medio,
avg_views, avg_likes, avg_commenti, avg_condivisioni, video_analizzati, stato,
dettaglio_errore`

- `engagement_medio`: media, in %, di `(like+commenti+condivisioni)/views` sugli
  ultimi `VIDEOS_TO_ANALYZE` video.
- `stato`: `ok` · `ok_senza_engagement` · `errore`. In caso di problemi il motivo
  è in `dettaglio_errore` (utile perché la libreria è non ufficiale).

## Risoluzione problemi

### `Failed to load tiktok after 5 attempts` / `Failed to get msToken from cookies`
È il caso più comune con questa libreria non ufficiale: TikTok rifiuta le
richieste che non hanno un **`msToken`** valido. In un browser headless "pulito"
(come in fase di test qui) il token spesso **non viene rilasciato** e tutte le
richieste vanno in timeout. Lo script gestisce la cosa senza crashare (segna
`stato=errore` e prosegue), ma per ottenere dati reali serve:

1. **Fornire un `msToken` reale** preso da una sessione browser vera loggata su
   `tiktok.com` (DevTools → Application → Cookies → `msToken`):
   ```powershell
   $env:TIKTOK_MS_TOKEN = "il_tuo_token"
   python tiktok_collector.py
   ```
   Il token scade: se ricominciano i timeout, rigeneralo.

2. **Spesso serve anche un proxy residenziale/rotante.** Gli IP di data center e
   molti IP domestici headless vengono bloccati. Il proxy si passa a
   `create_sessions(..., proxies=[...])` dentro `run_cycle()` — vedi la doc di
   TikTok-Api per il formato.

In breve: il codice e la resilienza sono a posto; l'accesso ai dati dipende da
`msToken` (+ eventuale proxy), che è una condizione esterna imposta da TikTok.

## Note

- `tiktok_collector.log` contiene lo storico delle esecuzioni.
- Per passare in futuro a Google Sheet: la funzione `append_rows()` è l'unico
  punto da cui si scrive; basta aggiungere lì una scrittura via `gspread`.
