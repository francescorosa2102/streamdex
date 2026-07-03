"""
Streamdex - Raccolta dati TikTok
================================

Raccoglie periodicamente, per una lista di account TikTok, il numero di
follower, i like totali del profilo e (best-effort) l'engagement medio sugli
ultimi video. Salva i dati in un CSV locale con la stessa struttura "a righe
nel tempo" usata per Twitch: formato "lungo" con una riga per (data_ora, account).

IMPORTANTE: usa la libreria NON ufficiale TikTok-Api
(https://github.com/davidteather/TikTok-Api). Puo' rompersi se TikTok cambia
qualcosa. Per questo lo script e' scritto in modo che l'errore su un singolo
account NON blocchi gli altri: ogni account e' isolato in un try/except, e la
parte "engagement" (piu' fragile) e' isolata a sua volta dal recupero di
follower/like. Se una parte fallisce, si salva comunque cio' che si e' ottenuto.

Uso:
    python tiktok_collector.py            # gira in loop ogni INTERVAL_MIN minuti
    python tiktok_collector.py --once     # una sola raccolta (utile per testare)

Opzionale ma consigliato: impostare un msToken per sessioni piu' stabili.
    - Windows (PowerShell):  $env:TIKTOK_MS_TOKEN = "il_tuo_token"
    - come ottenerlo: apri tiktok.com, DevTools > Application > Cookies > msToken.
"""

from __future__ import annotations

import asyncio
import csv
import os
import random
import sys
import traceback
from datetime import datetime

from TikTokApi import TikTokApi
from TikTokApi.exceptions import (
    EmptyResponseException,
    InvalidJSONException,
    InvalidResponseException,
    NotFoundException,
)

# --------------------------------------------------------------------------- #
# Configurazione
# --------------------------------------------------------------------------- #
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS_FILE = os.path.join(BASE_DIR, "accounts.txt")
CSV_FILE = os.path.join(BASE_DIR, "tiktok_data.csv")
LOG_FILE = os.path.join(BASE_DIR, "tiktok_collector.log")

INTERVAL_MIN = 15            # ogni quanti minuti ripetere la raccolta
COLLECT_ENGAGEMENT = True    # raccogliere engagement sugli ultimi video?
VIDEOS_TO_ANALYZE = 10       # quanti degli ultimi video usare per l'engagement
NUM_SESSIONS = 1             # sessioni browser da aprire (1 va bene per uso base)

# Pausa casuale tra un account e l'altro (secondi): riduce il rischio di blocchi.
SLEEP_BETWEEN_ACCOUNTS = (3, 7)
# Tentativi per il recupero delle info profilo prima di dichiarare l'errore.
INFO_RETRIES = 2
# Fail-fast: se i primi account falliscono TUTTI per errore di sessione, senza
# nessun successo, il msToken e' probabilmente scaduto -> interrompi il ciclo
# invece di macinare tutta la lista a vuoto (ogni timeout costa minuti).
SESSION_ABORT_THRESHOLD = 5

# msToken manuale, preso da DevTools (tiktok.com -> Application -> Cookies -> msToken).
#   PowerShell:  $env:TIKTOK_MS_TOKEN = "il_tuo_token"
MS_TOKEN = os.environ.get("TIKTOK_MS_TOKEN")

# --- SCAFFOLDING PROXY (commentato, per il futuro) ---------------------------
# Se un domani il token manuale non bastasse e servisse un proxy (meglio se
# residenziale), e' gia' pronto: bastano 2 passi.
#   1) togli il commento a queste due righe e imposta la variabile d'ambiente:
# import os as _os
# TIKTOK_PROXY = _os.environ.get("TIKTOK_PROXY")  # es. "http://utente:password@host:porta"
#   2) togli il commento alla riga "proxies=..." dentro create_sessions() (vedi run_cycle).
# Formato accettato: http(s)://[utente:password@]host:porta  oppure  socks5://host:porta
#
# def build_proxies():
#     """Converte TIKTOK_PROXY nel formato Playwright (una voce per sessione)."""
#     if not TIKTOK_PROXY:
#         return None
#     from urllib.parse import urlparse
#     u = urlparse(TIKTOK_PROXY)
#     server = f"{u.scheme}://{u.hostname}:{u.port}" if u.port else f"{u.scheme}://{u.hostname}"
#     proxy = {"server": server}
#     if u.username:
#         proxy["username"] = u.username
#     if u.password:
#         proxy["password"] = u.password
#     return [proxy] * NUM_SESSIONS

# Pesi del VALORE BASE (equivalente di FORMULA_VALORE_GRANDEZZE su Twitch): uno
# snapshot statico delle grandezze, scala 0-100, media pesata (somma = 1).
# Il momentum e la volatilita' NON stanno qui: li aggiunge la formula finale.
BASE_WEIGHTS = {
    "dimensione": 0.40,      # quanto e' grande il paniere (follower totali, log)
    "concentrazione": 0.25,  # quanto e' "diffusa" la salute (meno dominio dei top 3)
    "qualita": 0.35,         # engagement ponderato per follower
}
# Parametri della trasformazione finale, IDENTICI alla formula Twitch:
#   INDICE = base + (base - base_prec) * (1 + min(0.4, VOLATILITA/30))
VOLATILITY_WINDOW = 10   # n. di cicli su cui calcolare la dev. std. del valore base
VOL_DIVISOR = 30.0       # come Twitch (K/30)
VOL_MAX_AMP = 0.4        # come Twitch (min 0.4 -> amplificazione max +40%)

# Ordine delle colonne del CSV (formato "lungo": una riga per account).
# Le tre colonne finali rispecchiano J/K/L del foglio Twitch (VALORE_BASE,
# VOLATILITA, INDICE_FINALE); sono valori per CICLO, ripetuti identici su tutte
# le righe della stessa scansione. Vanno tenute IN CODA per retro-compatibilita'.
CSV_FIELDS = [
    "data_ora",           # gg/mm/aaaa HH:MM:SS (come il foglio Twitch)
    "account",
    "followers",
    "likes_totali",
    "video_totali",
    "engagement_medio",   # % media (like+commenti+condivisioni)/views sugli ultimi video
    "avg_views",
    "avg_likes",
    "avg_commenti",
    "avg_condivisioni",
    "video_analizzati",
    "stato",              # ok | ok_senza_engagement | non_trovato | errore_sessione
    "dettaglio_errore",
    "TIKTOK_VALORE_BASE",   # J: snapshot statico delle grandezze (0-100)
    "TIKTOK_VOLATILITA",    # K: dev. std. del valore base sugli ultimi cicli
    "TIKTOK_INDICE_FINALE", # L: base + momentum amplificato dalla volatilita'
]


# --------------------------------------------------------------------------- #
# Utility
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    """Stampa a video e appende sul file di log, con timestamp."""
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass  # il log non deve mai far crashare la raccolta


def now_italian() -> str:
    """Timestamp nello stesso formato del foglio Twitch: gg/mm/aaaa HH:MM:SS."""
    return datetime.now().strftime("%d/%m/%Y %H:%M:%S")


def to_int(value) -> int | None:
    """Converte in int i valori numerici di TikTok (spesso stringhe). None se non valido."""
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def to_float(value) -> float | None:
    """Come to_int ma mantiene i decimali (per l'engagement). None se non valido."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def parse_dt(s: str) -> datetime:
    """Interpreta il timestamp italiano gg/mm/aaaa HH:MM:SS del CSV."""
    return datetime.strptime(s.strip(), "%d/%m/%Y %H:%M:%S")


# --------------------------------------------------------------------------- #
# Indice TIKTOK_INDICE_FINALE (stessa metodologia dell'INDICE_FINALE Twitch)
# --------------------------------------------------------------------------- #
# Due strati, come sul foglio Twitch:
#
#   VALORE BASE (J)  -> snapshot statico delle "grandezze", scala 0-100:
#     1. Dimensione   : log10(follower totali) mappato 1M..1B -> 0..100
#     2. Concentrazione: (1 - quota_top3) * 100  (meno dominio dei top 3 = meglio)
#     3. Qualita      : engagement PONDERATO per follower, 0..20% -> 0..100
#
#   VOLATILITA (K)   -> dev. std. del valore base sugli ultimi VOLATILITY_WINDOW cicli
#
#   INDICE FINALE (L) = base + (base - base_prec) * (1 + min(0.4, VOLATILITA/30))
#     cioe' momentum (quanto si e' mosso il valore base) amplificato dalla
#     volatilita'. Formula e costanti identiche a quelle del foglio Twitch.

def _cycle_batches(rows: list[dict]) -> list[list[dict]]:
    """
    Raggruppa le righe in cicli di raccolta. Un nuovo ciclo inizia quando un
    account gia' visto nel ciclo corrente ricompare (ogni account appare una
    volta per ciclo). Richiede 'data_ora' e 'account' su ogni riga.
    """
    ordered = sorted(rows, key=lambda r: parse_dt(r["data_ora"]))
    batches: list[list[dict]] = []
    cur: list[dict] | None = None
    seen: set | None = None
    for r in ordered:
        if cur is None or r["account"] in seen:
            cur, seen = [], set()
            batches.append(cur)
        cur.append(r)
        seen.add(r["account"])
    return batches


def _valore_base(foll: dict, eng: dict, total: int):
    """Valore base 0-100 (equivalente di FORMULA_VALORE_GRANDEZZE): snapshot statico."""
    import math
    if total <= 0 or not foll:
        return None

    # 1. Dimensione (log): 1M -> 0, 1B -> 100
    dimensione = _clamp((math.log10(total) - 6.0) / 3.0 * 100.0, 0, 100)

    # 2. Concentrazione: meno dominio dei top 3 = piu' salute diffusa
    top3 = sum(sorted(foll.values(), reverse=True)[:3])
    concentrazione = _clamp((1.0 - top3 / total) * 100.0, 0, 100)

    # 3. Qualita: engagement ponderato per follower, 0..20% -> 0..100
    num = sum(eng[a] * foll[a] for a in foll if a in eng)
    den = sum(foll[a] for a in foll if a in eng)
    weighted_eng = (num / den) if den > 0 else 0.0
    qualita = _clamp(weighted_eng * 5.0, 0, 100)

    w = BASE_WEIGHTS
    base = (
        w["dimensione"] * dimensione
        + w["concentrazione"] * concentrazione
        + w["qualita"] * qualita
    )
    return round(base, 2)


def annotate_index(all_rows: list[dict]) -> list[dict]:
    """
    Aggiunge/aggiorna in place TIKTOK_VALORE_BASE, TIKTOK_VOLATILITA e
    TIKTOK_INDICE_FINALE su OGNI riga. Sono valori per ciclo: tutte le righe
    dello stesso ciclo ricevono lo stesso valore. Usa carry-forward dell'ultimo
    follower/engagement noto per account (un timeout occasionale non falsa il totale).

    INDICE = base + (base - base_prec) * (1 + min(0.4, volatilita/30)),
    dove la volatilita' e' la dev. std. del valore base sugli ultimi cicli.
    """
    if not all_rows:
        return all_rows
    import statistics

    foll: dict = {}
    eng: dict = {}
    base_history: list[float] = []
    prev_base: float | None = None

    for batch in _cycle_batches(all_rows):
        for r in batch:
            f = to_int(r.get("followers"))
            if f is not None:
                foll[r["account"]] = f
            e = to_float(r.get("engagement_medio"))
            if e is not None:
                eng[r["account"]] = e

        total = sum(foll.values())
        base = _valore_base(foll, eng, total)

        if base is None:
            for r in batch:
                r["TIKTOK_VALORE_BASE"] = ""
                r["TIKTOK_VOLATILITA"] = ""
                r["TIKTOK_INDICE_FINALE"] = ""
            continue

        base_history.append(base)
        window = base_history[-VOLATILITY_WINDOW:]
        vol = round(statistics.stdev(window), 4) if len(window) >= 2 else 0.0

        if prev_base is None:
            indice = base  # primo ciclo: nessun momentum (come riga 2 su Twitch)
        else:
            amp = 1.0 + min(VOL_MAX_AMP, vol / VOL_DIVISOR)
            indice = base + (base - prev_base) * amp
        indice = round(indice, 2)

        for r in batch:
            r["TIKTOK_VALORE_BASE"] = base
            r["TIKTOK_VOLATILITA"] = vol
            r["TIKTOK_INDICE_FINALE"] = indice
        prev_base = base

    return all_rows


def read_existing_rows() -> list[dict]:
    """Legge le righe gia' presenti nel CSV (vuoto se il file non esiste)."""
    if not os.path.exists(CSV_FILE):
        return []
    with open(CSV_FILE, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def migrate_csv_schema() -> None:
    """
    Auto-migrazione: se il CSV esiste ma non ha ancora le colonne J/K/L nuove
    (rileva l'assenza di TIKTOK_VALORE_BASE), lo riscrive con lo schema nuovo
    ricalcolando base/volatilita'/indice per tutti i cicli storici. Idempotente.
    Copre anche i CSV con il vecchio TIKTOK_INDICE_FINALE a 4 componenti: i valori
    vengono ricalcolati con la nuova metodologia (momentum + volatilita').
    """
    if not os.path.exists(CSV_FILE):
        return
    with open(CSV_FILE, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        rows = list(reader)
    if "TIKTOK_VALORE_BASE" in header:
        return
    annotate_index(rows)
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in CSV_FIELDS})
    log(f"CSV migrato al nuovo schema J/K/L ({len(rows)} righe storiche).")


def load_accounts() -> list[str]:
    """
    Legge accounts.txt ignorando righe vuote e commenti. Toglie eventuali @ e
    gli eventuali commenti inline dopo l'handle (es. "ornellazocco  # nota").
    Gli username TikTok non contengono '#', quindi tagliare al primo '#' e' sicuro.
    """
    if not os.path.exists(ACCOUNTS_FILE):
        log(f"ATTENZIONE: file account non trovato: {ACCOUNTS_FILE}")
        return []
    accounts = []
    with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
        for raw in f:
            name = raw.split("#", 1)[0].strip().lstrip("@")
            if name:
                accounts.append(name)
    return accounts


def ensure_csv_header() -> None:
    """Crea il CSV con l'intestazione se non esiste ancora."""
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(CSV_FIELDS)


def append_rows(rows: list[dict]) -> None:
    """Appende le righe raccolte al CSV, in ordine di colonna fisso."""
    ensure_csv_header()
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in CSV_FIELDS})


# --------------------------------------------------------------------------- #
# Raccolta per singolo account
# --------------------------------------------------------------------------- #
def extract_profile_stats(info: dict) -> dict:
    """
    Estrae follower/like/video dalla risposta di user.info().

    Si usa 'statsV2' come fonte primaria: 'stats' contiene interi a 32 bit che
    vanno in OVERFLOW per gli account grandi (es. i like di khaby.lame diventano
    negativi). 'statsV2' ha gli stessi campi come stringhe non overflowate.
    """
    user_info = info.get("userInfo", {})
    stats_v2 = user_info.get("statsV2") or {}
    stats = user_info.get("stats") or {}

    def pick(key, *alts):
        for src in (stats_v2, stats):
            for k in (key, *alts):
                if src.get(k) not in (None, ""):
                    return src.get(k)
        return None

    return {
        "followers": to_int(pick("followerCount")),
        "likes_totali": to_int(pick("heartCount", "heart")),
        "video_totali": to_int(pick("videoCount")),
    }


async def collect_engagement(user) -> dict:
    """
    Calcola l'engagement medio sugli ultimi VIDEOS_TO_ANALYZE video.
    Engagement per video = (like + commenti + condivisioni) / views.
    Ritorna medie aggregate. Isolato dal chiamante: qui puo' fallire senza
    compromettere follower/like gia' raccolti.
    """
    views, likes, comments, shares, ratios = [], [], [], [], []

    async for video in user.videos(count=VIDEOS_TO_ANALYZE):
        s = video.as_dict.get("statsV2") or video.as_dict.get("stats") or {}
        v = to_int(s.get("playCount"))
        li = to_int(s.get("diggCount"))
        c = to_int(s.get("commentCount"))
        sh = to_int(s.get("shareCount"))
        if v is None:
            continue
        views.append(v)
        likes.append(li or 0)
        comments.append(c or 0)
        shares.append(sh or 0)
        if v > 0:
            ratios.append((( li or 0) + (c or 0) + (sh or 0)) / v)

    n = len(views)
    if n == 0:
        return {"video_analizzati": 0}

    avg = lambda xs: round(sum(xs) / len(xs), 2) if xs else ""
    return {
        "video_analizzati": n,
        "avg_views": avg(views),
        "avg_likes": avg(likes),
        "avg_commenti": avg(comments),
        "avg_condivisioni": avg(shares),
        "engagement_medio": round(sum(ratios) / len(ratios) * 100, 2) if ratios else "",
    }


def classify_error(exc: Exception) -> tuple[str, str]:
    """
    Distingue i due tipi di fallimento su un account:

    - 'non_trovato'   : la richiesta e' andata a buon fine ma l'utente non
                        esiste (handle sbagliato/cambiato). Segnale deterministico:
                        la risposta arriva ma manca l'oggetto 'user' -> KeyError,
                        oppure NotFoundException. NON ha senso ritentare.
    - 'errore_sessione': non si e' ottenuta una risposta valida (timeout, risposta
                        vuota per bot-detection, msToken scaduto, errore browser/rete).
                        Ha senso RITENTARE, l'account probabilmente e' valido.

    Ritorna (stato, descrizione).
    """
    if isinstance(exc, (KeyError, NotFoundException)):
        return "non_trovato", "utente non trovato (handle inesistente o cambiato)"
    return "errore_sessione", f"{type(exc).__name__}: {exc}"


async def collect_account(api: TikTokApi, username: str) -> dict:
    """
    Raccoglie i dati di un singolo account. NON solleva mai eccezioni: qualsiasi
    problema viene catturato e riportato nel campo 'stato'/'dettaglio_errore',
    cosi' il ciclo prosegue con gli altri account.
    """
    row = {"data_ora": now_italian(), "account": username, "stato": "errore_sessione"}

    # --- Info profilo (follower / like / video) ---
    # Ritentiamo solo gli errori di sessione; un 'non_trovato' e' deterministico.
    info = None
    last_exc: Exception | None = None
    for attempt in range(1, INFO_RETRIES + 1):
        try:
            user = api.user(username=username)
            info = await user.info()
            break
        except Exception as e:  # noqa: BLE001 - vogliamo davvero non far crashare nulla
            last_exc = e
            stato, descr = classify_error(e)
            if stato == "non_trovato":
                log(f"  [{username}] NON TROVATO (non ritento).")
                row["stato"] = "non_trovato"
                row["dettaglio_errore"] = descr
                return row
            log(f"  [{username}] errore sessione, tentativo {attempt}/{INFO_RETRIES}: {descr}")
            if attempt < INFO_RETRIES:
                await asyncio.sleep(random.uniform(2, 5))

    if info is None:
        row["stato"] = "errore_sessione"
        row["dettaglio_errore"] = f"info profilo fallita: {type(last_exc).__name__}: {last_exc}"
        return row

    row.update(extract_profile_stats(info))
    row["stato"] = "ok"

    # --- Engagement (best-effort, isolato) ---
    if COLLECT_ENGAGEMENT:
        try:
            row.update(await collect_engagement(user))
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
            log(f"  [{username}] engagement fallito (follower/like salvati comunque): {err}")
            row["stato"] = "ok_senza_engagement"
            row["dettaglio_errore"] = f"engagement fallito: {err}"

    return row


# --------------------------------------------------------------------------- #
# Ciclo di raccolta
# --------------------------------------------------------------------------- #
async def run_cycle() -> None:
    """Esegue una raccolta completa su tutti gli account e appende al CSV."""
    accounts = load_accounts()
    if not accounts:
        log("Nessun account da elaborare. Controlla accounts.txt.")
        return

    migrate_csv_schema()  # porta eventuali CSV vecchi al nuovo schema (idempotente)

    log(f"Inizio raccolta su {len(accounts)} account.")
    rows: list[dict] = []
    successi = 0

    # Una sessione browser "fresca" a ogni ciclo: evita token stalli e leak di memoria
    # su esecuzioni lunghe. Anche l'apertura sessione e' protetta: se fallisce,
    # il ciclo si chiude senza crashare il loop esterno.
    try:
        async with TikTokApi() as api:
            await api.create_sessions(
                num_sessions=NUM_SESSIONS,
                headless=True,
                sleep_after=3,
                ms_tokens=[MS_TOKEN] if MS_TOKEN else None,
                # --- SCAFFOLDING PROXY (commentato): per attivarlo, togli il commento
                #     qui sotto e definisci build_proxies()/TIKTOK_PROXY in cima al file.
                # proxies=build_proxies(),
                browser="chromium",
            )

            errori_sessione_iniziali = 0
            for i, username in enumerate(accounts, 1):
                log(f"({i}/{len(accounts)}) {username} ...")
                row = await collect_account(api, username)
                rows.append(row)
                log(
                    f"  -> stato={row['stato']} follower={row.get('followers','?')} "
                    f"like={row.get('likes_totali','?')} eng={row.get('engagement_medio','')}"
                )

                if row["stato"].startswith("ok"):
                    successi += 1
                elif row["stato"] == "errore_sessione":
                    errori_sessione_iniziali += 1

                # Fail-fast: nessun successo e troppi errori di sessione di fila
                # all'inizio => msToken probabilmente scaduto. Inutile proseguire.
                if successi == 0 and errori_sessione_iniziali >= SESSION_ABORT_THRESHOLD:
                    log(
                        f"INTERROTTO: primi {errori_sessione_iniziali} account tutti falliti "
                        f"per errore di sessione e nessun successo. Il msToken e' "
                        f"probabilmente scaduto: rigeneralo da DevTools e rilancia."
                    )
                    break

                if i < len(accounts):
                    await asyncio.sleep(random.uniform(*SLEEP_BETWEEN_ACCOUNTS))
    except Exception as e:  # noqa: BLE001
        log(f"ERRORE di sessione/browser: {type(e).__name__}: {e}")
        log(traceback.format_exc())
        # Salviamo comunque cio' che eventualmente abbiamo gia' raccolto.

    if rows:
        # Calcola base/volatilita'/indice per questo ciclo usando anche lo storico
        # (servono i valori base precedenti per momentum e volatilita').
        # annotate_index scrive gli stessi valori su tutte le righe correnti.
        combined = read_existing_rows() + rows
        annotate_index(combined)
        base = rows[0].get("TIKTOK_VALORE_BASE", "")
        vol = rows[0].get("TIKTOK_VOLATILITA", "")
        indice = rows[0].get("TIKTOK_INDICE_FINALE", "")

        append_rows(rows)
        ok = sum(1 for r in rows if r["stato"].startswith("ok"))
        non_trovati = sum(1 for r in rows if r["stato"] == "non_trovato")
        sessione = sum(1 for r in rows if r["stato"] == "errore_sessione")
        log(
            f"Raccolta completata: {ok} ok, {non_trovati} non trovati, "
            f"{sessione} errori di sessione (su {len(rows)}). "
            f"base={base} volatilita={vol} INDICE={indice}. Righe salvate in {CSV_FILE}."
        )
    else:
        log("Nessuna riga raccolta in questo ciclo.")


async def main_loop() -> None:
    """Loop infinito: una raccolta ogni INTERVAL_MIN minuti."""
    log(f"Avvio in modalita' loop (ogni {INTERVAL_MIN} min). Ctrl+C per fermare.")
    while True:
        try:
            await run_cycle()
        except Exception as e:  # noqa: BLE001 - il loop non deve mai morire
            log(f"ERRORE inatteso nel ciclo: {type(e).__name__}: {e}")
            log(traceback.format_exc())
        log(f"Prossima raccolta tra {INTERVAL_MIN} minuti.\n")
        await asyncio.sleep(INTERVAL_MIN * 60)


def main() -> None:
    once = "--once" in sys.argv
    try:
        if once:
            asyncio.run(run_cycle())
        else:
            asyncio.run(main_loop())
    except KeyboardInterrupt:
        log("Interrotto dall'utente. Arrivederci.")


if __name__ == "__main__":
    main()
