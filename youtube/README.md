# Streamdex — Raccolta dati YouTube

Usa la **YouTube Data API v3 ufficiale** (gratuita, quota 10.000 unità/giorno) per
raccogliere periodicamente, dai canali in `youtube_channels.txt`:

- **iscritti**, **visualizzazioni totali** del canale, **n. video** — per *tutti* i canali
- per un **sottoinsieme** (i più rilevanti): views degli **ultimi 5 video** con data,
  per stimare la **velocità di crescita** reale (views/giorno sui contenuti nuovi)

Salva in `youtube_data.csv`, stessa struttura "a righe nel tempo" di Twitch/TikTok/Instagram
(una riga per canale per ciclo).

## 1. Creare una API key gratuita (guida passo‑passo)

Serve un account Google. Non serve carta di credito.

1. Vai su **https://console.cloud.google.com/** e accedi.
2. In alto, apri il selettore progetti → **"Nuovo progetto"** → dagli un nome
   (es. `streamdex-youtube`) → **Crea**. Aspetta qualche secondo e selezionalo.
3. Abilita l'API: menu **☰ → "API e servizi" → "Libreria"**. Cerca
   **"YouTube Data API v3"**, aprila e premi **"Abilita"**.
4. Crea la chiave: **☰ → "API e servizi" → "Credenziali"** →
   **"+ Crea credenziali" → "Chiave API"**. Copia la stringa che appare.
5. (Consigliato) Premi **"Modifica chiave API"** e sotto *Restrizioni API* scegli
   **"Limita chiave" → YouTube Data API v3**. Così la chiave funziona solo per questa API.
6. Non serve schermata di consenso OAuth: `channels.list`/`videos.list` su dati
   pubblici funzionano con la sola API key.

La chiave è un segreto: non condividerla e non metterla in un file versionato.

## 2. Installazione

```powershell
pip install -r requirements.txt
```

## 3. Impostare la chiave e avviare

```powershell
cd C:\Users\utente\Desktop\streamdex\youtube
$env:YOUTUBE_API_KEY = "LA_TUA_CHIAVE"

python youtube_collector.py --once   # una raccolta di prova
python youtube_collector.py          # loop ogni 15 min
```

## 4. Costo in quota (perché resta bassissimo)

L'errore da evitare è `search.list`, che costa **100 unità** a chiamata. Qui invece:

| Chiamata | Costo | Uso |
|---|---|---|
| `channels.list` (50 ID/chiamata) | 1 | iscritti/views di tutti i canali |
| `playlistItems.list` (uploads) | 1 | ultimi 5 video di ogni canale del sottoinsieme |
| `videos.list` (50 ID/chiamata) | 1 | views dei video recenti, in batch |

Con ~98 canali e sottoinsieme 20 → **~24 unità per ciclo**. A 15 min = ~2.300/giorno,
molto sotto le 10.000. Lo script **traccia la quota** in `youtube_quota.json` (con
reset a mezzanotte ora del Pacifico) e **avvisa a 8.000 unità**; sotto un margine di
sicurezza salta la fase di crescita ma continua a salvare iscritti/views.

## Parametri (in cima a `youtube_collector.py`)

| Parametro | Default | Significato |
|---|---|---|
| `INTERVAL_MIN` | 15 | minuti tra le raccolte |
| `GROWTH_SUBSET_MODE` | `top_subs` | `top_subs` = i più grandi per iscritti · `first_n` = i primi N nel file |
| `GROWTH_SUBSET_SIZE` | 20 | quanti canali nel sottoinsieme crescita |
| `RECENT_VIDEOS` | 5 | ultimi N video per canale |
| `QUOTA_WARN_AT` | 8000 | soglia di avviso quota |
| `QUOTA_RESERVE` | 300 | margine sotto cui si salta la crescita |

## Colonne del CSV

`data_ora, canale, channel_id, iscritti, visualizzazioni_totali, video_totali,
video_recenti, views_recenti_totali, views_recenti_per_giorno, ultimo_video_data,
stato, dettaglio_errore`

- `views_recenti_per_giorno`: media, sugli ultimi `RECENT_VIDEOS` video, di
  `views_video / età_in_giorni` → una stima della **velocità di crescita** su contenuti nuovi.
- `stato`: `ok` · `ok_senza_crescita` (iscritti/views ok, crescita fallita) ·
  `non_trovato` (id errato/rimosso) · `errore` (batch fallito, da ritentare).

## Note

- Un errore su un canale/batch **non ferma gli altri** (stessa resilienza di TikTok).
- `youtube_channels.txt`: `nome<TAB>channel_id`, una riga per canale (righe con `#` ignorate).
