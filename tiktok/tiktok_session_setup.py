"""
Streamdex - Setup sessione TikTok (una tantum)
==============================================

Apre un browser Playwright VISIBILE su tiktok.com e ti lascia "scaldare" la
sessione: accetta i cookie, guarda un paio di video, passa eventuali captcha.
Quando premi INVIO nel terminale, salva lo stato della sessione (cookie +
storage) in `tiktok_session.json`. Il collector riuserà quel file per rigenerare
da solo il msToken a ogni ciclo, senza che tu debba ripescarlo da DevTools.

IMPORTANTE (scelta di sicurezza):
  - NON serve fare login, ed e' SCONSIGLIATO. Una sessione anonima "calda"
    basta per ottenere un msToken valido e NON mette a rischio nessun account.
  - Se un domani deciderai di loggarti (perche' l'anonimo non basta), usa un
    account TikTok USA-E-GETTA, mai il tuo personale: automatizzare una sessione
    loggata viola i ToS di TikTok e rischia il ban dell'account.

Il file `tiktok_session.json` e' un SEGRETO: non condividerlo e non committarlo
(e' gia' in .gitignore).

Uso:
    python tiktok_session_setup.py
"""

import os
import sys

from playwright.sync_api import sync_playwright

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_FILE = os.path.join(BASE_DIR, "tiktok_session.json")

# Pagina PUBBLICA (non richiede login) su cui aprire il browser.
START_URL = "https://www.tiktok.com/explore"

# Fingerprint "umano" per non farsi sbattere sul muro di login/captcha:
# user-agent reale, locale/timezone italiani, viewport da desktop.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

# Rimuove i segnali di automazione che TikTok rileva (navigator.webdriver ecc.).
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['it-IT', 'it', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || { runtime: {} };
"""

ISTRUZIONI = """
────────────────────────────────────────────────────────────────────────
  SETUP SESSIONE TIKTOK  (solo navigazione anonima, NIENTE login)
────────────────────────────────────────────────────────────────────────
  Si e' aperta una finestra del browser sulla pagina PUBBLICA "Esplora".
  Nella finestra:

    1. Accetta i cookie se compare il banner.
    2. Naviga tra i CONTENUTI PUBBLICI: apri qualche video, torna indietro,
       scorri "Esplora". (serve a far generare i cookie anti-bot, incl. msToken)
    3. Se compare un captcha, risolvilo a mano.
    4. Se compare un popup / una pagina di LOGIN: chiudilo se puoi (la X, o
       "Continua come ospite"). MA anche se resti sulla pagina di login VA BENE
       LO STESSO: i cookie (incluso msToken) vengono generati comunque.
       >>> L'UNICA cosa da NON fare mai e' DIGITARE LE CREDENZIALI. <<<
       (inserire email/password non serve e puo' far scattare blocchi temporanei)

  Quando hai finito di navigare (anche solo 20-30 secondi), torna QUI e premi
  INVIO per salvare la sessione.
────────────────────────────────────────────────────────────────────────
"""


def main():
    print(ISTRUZIONI)
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as e:
            print(f"Impossibile avviare il browser Playwright: {e}")
            print("Hai installato il browser? Esegui:  python -m playwright install chromium")
            sys.exit(1)

        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="it-IT",
            timezone_id="Europe/Rome",
            viewport={"width": 1280, "height": 800},
        )
        context.add_init_script(STEALTH_JS)
        page = context.new_page()
        try:
            page.goto(START_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"Avviso: caricamento iniziale lento/fallito ({e}). Puoi comunque navigare a mano.")

        input(">>> Premi INVIO qui quando hai finito di scaldare la sessione... ")

        context.storage_state(path=SESSION_FILE)
        browser.close()

    print(f"\n✓ Sessione salvata in: {SESSION_FILE}")
    print("  ⚠️  Questo file contiene dati di sessione: NON condividerlo e NON committarlo.")
    print("  Ora puoi avviare il collector normalmente (non serve piu' TIKTOK_MS_TOKEN):")
    print("      python tiktok_collector.py")


if __name__ == "__main__":
    main()
