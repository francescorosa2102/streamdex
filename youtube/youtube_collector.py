"""
Streamdex - Raccolta dati YouTube
=================================

Usa la YouTube Data API v3 UFFICIALE (gratuita, quota giornaliera 10.000 unita').
Per una lista di canali raccoglie periodicamente:
  - iscritti, visualizzazioni totali del canale, n. video totali (per TUTTI i canali)
  - per un sottoinsieme (i piu' rilevanti): le views degli ultimi 5 video con data,
    per stimare la "velocita' di crescita" reale (views/giorno sui contenuti nuovi)

Salva in youtube_data.csv, stessa struttura "a righe nel tempo" di Twitch/TikTok/Instagram
(formato lungo: una riga per canale per ciclo).

Efficienza quota (fondamentale: la quota e' 10.000 unita'/giorno):
  - channels.list  -> 1 unita' per chiamata, fino a 50 ID per chiamata (batch)
  - per la crescita si NON usa search.list (costa 100!): si usa la "uploads playlist"
    di ogni canale con playlistItems.list (1 unita') + videos.list (1 unita', batch)
  Con ~98 canali e 20 nel sottoinsieme crescita: ~24 unita' per ciclo.

Resilienza: un errore su un canale/batch non ferma gli altri (come per TikTok).

Uso:
    set YOUTUBE_API_KEY (vedi README), poi:
    python youtube_collector.py            # loop ogni INTERVAL_MIN minuti
    python youtube_collector.py --once     # una sola raccolta (per testare)
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone

import requests

# --------------------------------------------------------------------------- #
# Configurazione
# --------------------------------------------------------------------------- #
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CHANNELS_FILE = os.path.join(BASE_DIR, "youtube_channels.txt")
CSV_FILE = os.path.join(BASE_DIR, "youtube_data.csv")
LOG_FILE = os.path.join(BASE_DIR, "youtube_collector.log")
QUOTA_FILE = os.path.join(BASE_DIR, "youtube_quota.json")

API_KEY = os.environ.get("YOUTUBE_API_KEY")
API_BASE = "https://www.googleapis.com/youtube/v3/"

INTERVAL_MIN = 15            # minuti tra una raccolta e l'altra

# Sottoinsieme "crescita": per quali canali scaricare gli ultimi video.
# 'top_subs' = i piu' grandi per iscritti (default, auto-adattivo);
# 'first_n'  = i primi N nel file youtube_channels.txt (controllo manuale).
GROWTH_SUBSET_MODE = "top_subs"
GROWTH_SUBSET_SIZE = 20      # quanti canali nel sottoinsieme crescita
RECENT_VIDEOS = 5            # ultimi N video per canale del sottoinsieme

# Quota giornaliera e soglie di avviso
QUOTA_DAILY_LIMIT = 10000
QUOTA_WARN_AT = 8000         # avviso quando si supera questa soglia
QUOTA_RESERVE = 300          # margine: sotto questo residuo si salta la crescita

HTTP_TIMEOUT = 30

# --- Indice YOUTUBE_INDICE_FINALE (stessa metodologia J/K/L di Twitch/TikTok) ---
# VALORE BASE (J): snapshot statico 0-100, media pesata (somma = 1)
BASE_WEIGHTS = {
    "dimensione": 0.40,      # iscritti totali del paniere (log)
    "concentrazione": 0.25,  # meno dominio dei top 3 = piu' salute diffusa
    "qualita": 0.35,         # velocita' di crescita ponderata per iscritti (log)
}
# Trasformazione finale identica a Twitch:
#   INDICE = base + (base - base_prec) * (1 + min(0.4, VOLATILITA/30))
VOLATILITY_WINDOW = 10   # cicli su cui calcolare la dev. std. del valore base
VOL_DIVISOR = 30.0
VOL_MAX_AMP = 0.4


# --------------------------------------------------------------------------- #
# Utility
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def now_italian() -> str:
    """Timestamp come Twitch/TikTok: gg/mm/aaaa HH:MM:SS."""
    return datetime.now().strftime("%d/%m/%Y %H:%M:%S")


def to_int(value):
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def to_float(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def parse_dt(s: str) -> datetime:
    """Interpreta il timestamp italiano gg/mm/aaaa HH:MM:SS del CSV."""
    return datetime.strptime(s.strip(), "%d/%m/%Y %H:%M:%S")


def parse_iso(s: str) -> datetime | None:
    """Interpreta il publishedAt ISO 8601 di YouTube (UTC), es. 2024-01-15T10:00:00Z."""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


CSV_FIELDS = [
    "data_ora",                  # gg/mm/aaaa HH:MM:SS
    "canale",
    "channel_id",
    "iscritti",
    "visualizzazioni_totali",
    "video_totali",
    "video_recenti",             # n. di video recenti analizzati (0 se fuori sottoinsieme)
    "views_recenti_totali",      # somma views degli ultimi RECENT_VIDEOS video
    "views_recenti_per_giorno",  # velocita' di crescita: media views/giorno sui recenti
    "ultimo_video_data",         # data di pubblicazione del video piu' recente (gg/mm/aaaa)
    "stato",                     # ok | ok_senza_crescita | non_trovato | errore
    "dettaglio_errore",
    "YOUTUBE_VALORE_BASE",       # J: snapshot statico delle grandezze (0-100)
    "YOUTUBE_VOLATILITA",        # K: dev. std. del valore base sugli ultimi cicli
    "YOUTUBE_INDICE_FINALE",     # L: base + momentum amplificato dalla volatilita'
]


def load_channels() -> list[tuple[str, str]]:
    """Legge youtube_channels.txt: 'nome<TAB>channel_id' (tollera anche piu' spazi)."""
    if not os.path.exists(CHANNELS_FILE):
        log(f"ATTENZIONE: file canali non trovato: {CHANNELS_FILE}")
        return []
    out = []
    with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            # l'ID e' l'ultimo token (nessuno spazio interno); il resto e' il nome
            parts = line.rsplit(maxsplit=1)
            if len(parts) != 2:
                log(f"  riga canale ignorata (formato): {line!r}")
                continue
            name, cid = parts[0].strip(), parts[1].strip()
            if not cid.startswith("UC"):
                log(f"  channel_id sospetto ignorato: {cid!r} ({name})")
                continue
            out.append((name, cid))
    return out


def ensure_csv_header():
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(CSV_FIELDS)


def append_rows(rows: list[dict]):
    ensure_csv_header()
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in CSV_FIELDS})


# --------------------------------------------------------------------------- #
# Tracciamento quota (persistente, reset giornaliero ora del Pacifico)
# --------------------------------------------------------------------------- #
def _pacific_date() -> str:
    """Data corrente nel fuso del Pacifico (la quota YouTube si azzera a mezzanotte PT)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    except Exception:
        # fallback senza tzdata: PT ~ UTC-8 (ignora l'ora legale, va bene per l'avviso)
        from datetime import timedelta
        return (datetime.now(timezone.utc) - timedelta(hours=8)).strftime("%Y-%m-%d")


class QuotaTracker:
    """Conta le unita' di quota usate nel giorno, con persistenza su file."""

    def __init__(self):
        self.date = _pacific_date()
        self.used = 0
        self._warned = False
        self._load()

    def _load(self):
        try:
            with open(QUOTA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("date") == self.date:
                self.used = int(data.get("used", 0))
        except (OSError, ValueError, KeyError):
            pass

    def _save(self):
        try:
            with open(QUOTA_FILE, "w", encoding="utf-8") as f:
                json.dump({"date": self.date, "used": self.used}, f)
        except OSError:
            pass

    def roll_day_if_needed(self):
        today = _pacific_date()
        if today != self.date:
            log(f"Nuovo giorno ({today}): quota azzerata (era {self.used} unita').")
            self.date, self.used, self._warned = today, 0, False
            self._save()

    @property
    def remaining(self) -> int:
        return QUOTA_DAILY_LIMIT - self.used

    def can_afford(self, cost: int) -> bool:
        return self.used + cost <= QUOTA_DAILY_LIMIT

    def add(self, cost: int, what: str = ""):
        self.used += cost
        self._save()
        if self.used >= QUOTA_WARN_AT and not self._warned:
            self._warned = True
            log(
                f"*** ATTENZIONE QUOTA: usate {self.used}/{QUOTA_DAILY_LIMIT} unita' "
                f"(residue {self.remaining}). Ci si avvicina al limite giornaliero. ***"
            )


class YouTubeApiError(Exception):
    def __init__(self, status, reason, message):
        self.status = status
        self.reason = reason  # es. 'quotaExceeded', 'keyInvalid'
        super().__init__(f"HTTP {status} [{reason}]: {message}")


def api_get(quota: QuotaTracker, endpoint: str, params: dict, cost: int = 1) -> dict:
    """
    Chiama un endpoint della Data API contando la quota. Solleva YouTubeApiError
    sugli errori HTTP (incl. quotaExceeded / keyInvalid), TimeoutError sui timeout.
    """
    if not quota.can_afford(cost):
        raise YouTubeApiError(0, "localQuotaGuard",
                              f"chiamata saltata: supererebbe il limite ({quota.used}/{QUOTA_DAILY_LIMIT})")
    full = {**params, "key": API_KEY}
    try:
        r = requests.get(API_BASE + endpoint, params=full, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise TimeoutError(f"errore di rete su {endpoint}: {e}")

    # La quota viene consumata dal server anche su alcune risposte: contiamo sempre
    # per le 200; per gli errori contiamo solo se NON e' un rifiuto pre-esecuzione.
    if r.status_code == 200:
        quota.add(cost, endpoint)
        return r.json()

    reason, message = "", r.text[:300]
    try:
        err = r.json().get("error", {})
        message = err.get("message", message)
        if err.get("errors"):
            reason = err["errors"][0].get("reason", "")
    except ValueError:
        pass
    # quotaExceeded non consuma ulteriore quota utile; keyInvalid nemmeno.
    raise YouTubeApiError(r.status_code, reason, message)


# --------------------------------------------------------------------------- #
# Raccolta
# --------------------------------------------------------------------------- #
def fetch_channels(quota: QuotaTracker, cids: list[str]) -> tuple[dict, set, bool]:
    """
    channels.list in batch da 50. Ritorna (fetched, failed_cids, quota_esaurita).
    fetched: cid -> item (con statistics e contentDetails). failed_cids: batch falliti.
    """
    fetched: dict = {}
    failed: set = set()
    quota_exhausted = False

    for batch in chunks(cids, 50):
        try:
            data = api_get(quota, "channels", {
                "part": "snippet,statistics,contentDetails",
                "id": ",".join(batch),
                "maxResults": 50,
            })
            for item in data.get("items", []):
                fetched[item["id"]] = item
        except YouTubeApiError as e:
            log(f"  channels.list batch fallito: {e}")
            failed.update(batch)
            if e.reason in ("quotaExceeded", "dailyLimitExceeded", "localQuotaGuard"):
                quota_exhausted = True
                break
        except TimeoutError as e:
            log(f"  channels.list batch timeout: {e}")
            failed.update(batch)
    return fetched, failed, quota_exhausted


def pick_growth_subset(channel_list, fetched) -> list[str]:
    """Sceglie i cid del sottoinsieme crescita secondo GROWTH_SUBSET_MODE."""
    available = [cid for _, cid in channel_list if cid in fetched]
    if GROWTH_SUBSET_MODE == "first_n":
        return available[:GROWTH_SUBSET_SIZE]

    def subs(cid):
        return to_int(fetched[cid].get("statistics", {}).get("subscriberCount")) or 0

    return sorted(available, key=subs, reverse=True)[:GROWTH_SUBSET_SIZE]


def fetch_recent_videos(quota: QuotaTracker, subset_cids, fetched) -> dict:
    """
    Per ogni canale del sottoinsieme prende gli ultimi RECENT_VIDEOS video dalla
    "uploads playlist" (1 unita' a canale) e poi le loro statistiche in batch.
    Ritorna: cid -> dict(video_recenti, views_recenti_totali, views_recenti_per_giorno,
    ultimo_video_data, errore). Ogni canale e' isolato: un errore non ferma gli altri.
    """
    result: dict = {}
    # cid -> lista di (video_id, published_dt)
    per_channel_videos: dict = {}

    for cid in subset_cids:
        try:
            uploads = (fetched[cid].get("contentDetails", {})
                       .get("relatedPlaylists", {}).get("uploads"))
            if not uploads:
                result[cid] = {"errore": "nessuna uploads playlist"}
                continue
            data = api_get(quota, "playlistItems", {
                "part": "contentDetails",
                "playlistId": uploads,
                "maxResults": RECENT_VIDEOS,
            })
            vids = []
            for it in data.get("items", []):
                cd = it.get("contentDetails", {})
                vid = cd.get("videoId")
                pub = parse_iso(cd.get("videoPublishedAt"))
                if vid:
                    vids.append((vid, pub))
            per_channel_videos[cid] = vids
        except (YouTubeApiError, TimeoutError) as e:
            result[cid] = {"errore": f"playlistItems: {e}"}

    # videos.list batch per tutte le view degli ultimi video raccolti
    all_vids = [v for vids in per_channel_videos.values() for (v, _) in vids]
    stats: dict = {}
    for batch in chunks(all_vids, 50):
        try:
            data = api_get(quota, "videos", {"part": "statistics", "id": ",".join(batch)})
            for item in data.get("items", []):
                stats[item["id"]] = to_int(item.get("statistics", {}).get("viewCount"))
        except (YouTubeApiError, TimeoutError) as e:
            log(f"  videos.list batch fallito (crescita parziale): {e}")

    now = datetime.now(timezone.utc)
    for cid, vids in per_channel_videos.items():
        views_tot = 0
        per_day = []
        last_pub = None
        used = 0
        for vid, pub in vids:
            vc = stats.get(vid)
            if vc is None:
                continue
            used += 1
            views_tot += vc
            if pub:
                if last_pub is None or pub > last_pub:
                    last_pub = pub
                age_days = max((now - pub).total_seconds() / 86400.0, 0.5)
                per_day.append(vc / age_days)
        if used == 0:
            result[cid] = {"errore": result.get(cid, {}).get("errore", "nessuna statistica video")}
            continue
        result[cid] = {
            "video_recenti": used,
            "views_recenti_totali": views_tot,
            "views_recenti_per_giorno": round(sum(per_day) / len(per_day)) if per_day else "",
            "ultimo_video_data": last_pub.strftime("%d/%m/%Y") if last_pub else "",
        }
    return result


# --------------------------------------------------------------------------- #
# Indice YOUTUBE_INDICE_FINALE (stessa metodologia J/K/L di Twitch/TikTok)
# --------------------------------------------------------------------------- #
# VALORE BASE (J), snapshot statico 0-100:
#   Dimensione   : log10(iscritti totali) mappato 1M..1B -> 0..100
#   Concentrazione: (1 - quota_top3_iscritti) * 100
#   Qualita      : velocita' di crescita (views/giorno recenti) PONDERATA per
#                  iscritti, log-normalizzata 1k..10M/giorno -> 0..100
# INDICE (L) = base + (base - base_prec) * (1 + min(0.4, VOLATILITA/30))
def _cycle_batches(rows: list[dict]) -> list[list[dict]]:
    """Raggruppa in cicli: nuovo ciclo quando un channel_id gia' visto ricompare."""
    ordered = sorted(rows, key=lambda r: parse_dt(r["data_ora"]))
    batches, cur, seen = [], None, None
    for r in ordered:
        if cur is None or r["channel_id"] in seen:
            cur, seen = [], set()
            batches.append(cur)
        cur.append(r)
        seen.add(r["channel_id"])
    return batches


def _valore_base(subs: dict, vel: dict, total: int):
    """Valore base 0-100 (snapshot statico delle grandezze YouTube)."""
    import math
    if total <= 0 or not subs:
        return None
    dimensione = _clamp((math.log10(total) - 6.0) / 3.0 * 100.0, 0, 100)

    top3 = sum(sorted(subs.values(), reverse=True)[:3])
    concentrazione = _clamp((1.0 - top3 / total) * 100.0, 0, 100)

    num = sum(vel[c] * subs[c] for c in subs if c in vel)
    den = sum(subs[c] for c in subs if c in vel)
    weighted_vel = (num / den) if den > 0 else 0.0
    # log-normalizzazione: 1.000/giorno -> 0, 10.000.000/giorno -> 100
    qualita = _clamp((math.log10(weighted_vel) - 3.0) / 4.0 * 100.0, 0, 100) if weighted_vel > 0 else 0.0

    w = BASE_WEIGHTS
    base = w["dimensione"] * dimensione + w["concentrazione"] * concentrazione + w["qualita"] * qualita
    return round(base, 2)


def annotate_index(all_rows: list[dict]) -> list[dict]:
    """
    Scrive in place YOUTUBE_VALORE_BASE, YOUTUBE_VOLATILITA, YOUTUBE_INDICE_FINALE
    su ogni riga (valore per ciclo). Carry-forward di iscritti e velocita' per canale.
    """
    if not all_rows:
        return all_rows
    import statistics

    subs: dict = {}
    vel: dict = {}
    base_history: list[float] = []
    prev_base = None

    for batch in _cycle_batches(all_rows):
        for r in batch:
            s = to_int(r.get("iscritti"))
            if s is not None:
                subs[r["channel_id"]] = s
            v = to_int(r.get("views_recenti_per_giorno"))
            if v is not None:
                vel[r["channel_id"]] = v

        total = sum(subs.values())
        base = _valore_base(subs, vel, total)
        if base is None:
            for r in batch:
                r["YOUTUBE_VALORE_BASE"] = ""
                r["YOUTUBE_VOLATILITA"] = ""
                r["YOUTUBE_INDICE_FINALE"] = ""
            continue

        base_history.append(base)
        window = base_history[-VOLATILITY_WINDOW:]
        vol = round(statistics.stdev(window), 4) if len(window) >= 2 else 0.0

        if prev_base is None:
            indice = base
        else:
            amp = 1.0 + min(VOL_MAX_AMP, vol / VOL_DIVISOR)
            indice = base + (base - prev_base) * amp
        indice = round(indice, 2)

        for r in batch:
            r["YOUTUBE_VALORE_BASE"] = base
            r["YOUTUBE_VOLATILITA"] = vol
            r["YOUTUBE_INDICE_FINALE"] = indice
        prev_base = base

    return all_rows


def read_existing_rows() -> list[dict]:
    if not os.path.exists(CSV_FILE):
        return []
    with open(CSV_FILE, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def migrate_csv_schema() -> None:
    """Se il CSV non ha ancora le colonne J/K/L, le aggiunge ricalcolando lo storico."""
    if not os.path.exists(CSV_FILE):
        return
    with open(CSV_FILE, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        rows = list(reader)
    if "YOUTUBE_VALORE_BASE" in header:
        return
    annotate_index(rows)
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in CSV_FIELDS})
    log(f"CSV migrato al nuovo schema J/K/L ({len(rows)} righe storiche).")


def build_rows(channel_list, fetched, failed, growth) -> list[dict]:
    """Costruisce una riga per canale (formato lungo), stessa struttura degli altri collector."""
    ts = now_italian()
    rows = []
    subset = set(growth.keys())
    for name, cid in channel_list:
        row = {"data_ora": ts, "canale": name, "channel_id": cid}
        item = fetched.get(cid)
        if item is None:
            row["stato"] = "errore" if cid in failed else "non_trovato"
            row["dettaglio_errore"] = (
                "canale in un batch fallito (ritenta)" if cid in failed
                else "canale non trovato (id errato o rimosso)"
            )
            rows.append(row)
            continue

        st = item.get("statistics", {})
        row["iscritti"] = to_int(st.get("subscriberCount"))
        row["visualizzazioni_totali"] = to_int(st.get("viewCount"))
        row["video_totali"] = to_int(st.get("videoCount"))
        row["stato"] = "ok"

        if cid in subset:
            g = growth[cid]
            if "errore" in g:
                row["stato"] = "ok_senza_crescita"
                row["dettaglio_errore"] = g["errore"]
            else:
                row["video_recenti"] = g["video_recenti"]
                row["views_recenti_totali"] = g["views_recenti_totali"]
                row["views_recenti_per_giorno"] = g["views_recenti_per_giorno"]
                row["ultimo_video_data"] = g["ultimo_video_data"]
        rows.append(row)
    return rows


def run_cycle(quota: QuotaTracker) -> None:
    channels = load_channels()
    if not channels:
        log("Nessun canale da elaborare. Controlla youtube_channels.txt.")
        return
    if not API_KEY:
        log("ERRORE: variabile YOUTUBE_API_KEY non impostata. Vedi README.")
        return

    quota.roll_day_if_needed()
    migrate_csv_schema()  # porta eventuali CSV vecchi al nuovo schema (idempotente)
    log(f"Inizio raccolta su {len(channels)} canali (quota usata: {quota.used}/{QUOTA_DAILY_LIMIT}).")

    cids = [cid for _, cid in channels]
    fetched, failed, exhausted = fetch_channels(quota, cids)
    log(f"  channels.list: {len(fetched)} trovati, {len(failed)} in batch falliti.")

    growth = {}
    if exhausted:
        log("  quota esaurita: salto la fase di crescita (salvo comunque iscritti/views).")
    else:
        subset = pick_growth_subset(channels, fetched)
        # stima costo crescita: 1 per canale (playlistItems) + video.list in batch
        est = len(subset) + max(1, (len(subset) * RECENT_VIDEOS + 49) // 50)
        if quota.remaining - est < QUOTA_RESERVE:
            log(f"  quota residua bassa ({quota.remaining}): salto la crescita per sicurezza.")
        elif subset:
            log(f"  crescita su {len(subset)} canali (costo stimato ~{est} unita').")
            growth = fetch_recent_videos(quota, subset, fetched)

    rows = build_rows(channels, fetched, failed, growth)

    # Indice J/K/L per questo ciclo (usa anche lo storico per momentum e volatilita')
    combined = read_existing_rows() + rows
    annotate_index(combined)
    base = rows[0].get("YOUTUBE_VALORE_BASE", "") if rows else ""
    vol = rows[0].get("YOUTUBE_VOLATILITA", "") if rows else ""
    indice = rows[0].get("YOUTUBE_INDICE_FINALE", "") if rows else ""

    append_rows(rows)

    ok = sum(1 for r in rows if r["stato"].startswith("ok"))
    ncr = sum(1 for r in rows if r["stato"] == "non_trovato")
    err = sum(1 for r in rows if r["stato"] == "errore")
    log(
        f"Raccolta completata: {ok} ok, {ncr} non trovati, {err} errori (su {len(rows)}). "
        f"base={base} volatilita={vol} INDICE={indice}. "
        f"Quota usata oggi: {quota.used}/{QUOTA_DAILY_LIMIT} (residue {quota.remaining}). "
        f"Righe salvate in {CSV_FILE}."
    )


def main_loop():
    quota = QuotaTracker()
    log(f"Avvio in modalita' loop (ogni {INTERVAL_MIN} min). Ctrl+C per fermare.")
    while True:
        try:
            run_cycle(quota)
        except Exception as e:  # noqa: BLE001 - il loop non deve mai morire
            log(f"ERRORE inatteso nel ciclo: {type(e).__name__}: {e}")
            log(traceback.format_exc())
        log(f"Prossima raccolta tra {INTERVAL_MIN} minuti.\n")
        time.sleep(INTERVAL_MIN * 60)


def main():
    if "--once" in sys.argv:
        run_cycle(QuotaTracker())
    else:
        main_loop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Interrotto dall'utente. Arrivederci.")
