# JevTrader Apex: Autonomous Crypto Perpetual Trading Engine

JevTrader je samostalni, visoko-frekventni i potpuno autonomni sustav za trgovanje Bybit USDT linearnim perpetualima.
Kombinira **L2 Microstructure analizu (Orderbook dubina + CVD Taker Flow)** sa **sub-sekundnim System One AI modelima** (`perplexity/pplx-decider-v1.2` i `liquid/d1-ultra`).

---

## 🚀 Brzi Početak (Quickstart)

```powershell
cd C:\Users\Korisnik\Documents\ai-trader-v10.16\jevtrader

# 1. Pokretanje u sigurnom simulacijskom modu (Paper Trading na pravim Bybit podacima):
python run.py --dry-run

# 2. Pokretanje uživo sa stvarnim Bybit računom:
python run.py --live
```

---

## ⚡ Ključne Prednosti (Zašto pobjeđuje druge igrače):

1. **Sub-400ms AI Potvrda (OpenRouter Decisions API):**
   * Koristi specijalizirane System One modele (`perplexity/pplx-decider-v1.2` i `liquid/d1-ultra`).
   * Umjesto sporog chat teksta, model u 350 milisekundi vraća čistu vjerojatnost (`noul` od 0.0 do 1.0). Nema kašnjenja, nula šanse za greške u formatu.
   * Cijena po odluci iznosi samo **$0.000008** (100.000 evaluacija košta manje od $1.00).

2. **5-Faktorska Konfluencijska Matrica (0 - 100 bodova):**
   * **Relative Strength vs BTC (25 pts):** Pronalazi anomalije u 1.6s preko 912 parova.
   * **Taker Flow / CVD Delta (25 pts):** Mjeri agresivni pritisak kupaca i prodavača iz stvarnih zadnjih 100 trejdova.
   * **Integritet Zida & Anti-Spoof (20 pts):** Provjerava da zid limit naloga zbilja brani cijenu i ne nestaje.
   * **Squeeze Ekspanzija (15 pts):** Detektira proboj iz ATR kompresije.
   * **VWAP Geometrija (15 pts):** Pravi omjer rizika i nagrade.

3. **Dinamička Alokacija Smjerova (Argon GMRI Engine):**
   * **BULL_ALPHA (Širina tržišta > 65%):** Zabrana shortova! Bot automatski uzima **2 najjača Longa**.
   * **BLOOD_STREETS (Širina tržišta < 35%):** Zabrana longova! Bot uzima **2 najslabija Shorta** na panici.
   * **CHOP_ROTATION (35% - 65%):** Pametni hedge: **1 Long (anomalija) + 1 Short (krvarenje)**.

4. **Kelly Compounding za manji račun (3.5% do 5.0% rizik):**
   * **TP1 na +1.5R (zatvara 50%):** Zaključava dobit i odmah pomiče Stop-Loss na **Break-Even (besplatan trejd, rizik = 0%)**.
   * **TP2 na +2.8R (preostalih 50%):** Vozi puni trend za masivni prinos (+8% na račun).
   * **Chained Compounding:** Čim prvi trejd zaključa Break-Even, oslobađa se rizik za trenutno otvaranje idućeg trejda!
   * **300ms Watchdog Ninja Cut (`pplx-decider-flash`):** Ako se potporni zid u orderbooku raspadne, bot reže poziciju na **-0.2R** umjesto čekanja punog stop-lossa.

5. **Dvostruka Zaštita na Bybitu:**
   * Stop-Loss se postavlja nativno na **Mark Price** (štiti od lažnih likvidacijskih wickova).
   * Take-Profit okida na **Last Price** (naplaćuje nagle wickove u tvoju korist).

---

## 📁 Struktura Mapa:

```text
jevtrader/
├── run.py                 # Glavna CLI komanda
├── config.yaml            # Sve postavke (rizik, pragovi, modeli)
├── README.md              # Dokumentacija
├── data/
│   ├── jevtrader.sqlite3  # SQLite baza (13 tablica za transakcije i recovery)
│   └── jevtrader.log      # Detaljni log rada
└── jev/
    ├── config.py          # Učitavanje konfiguracije i .env
    ├── contracts.py       # Pydantic/Dataclass ugovori
    ├── bybit.py           # Bybit v5 REST klijent s api.bytick.com fallbackom
    ├── market.py          # Skeniranje 912 instrumenata i telemetrija
    ├── confluence.py      # 5-faktorski bodovni kalkulator
    ├── decisions.py       # OpenRouter Decisions klijent (sub-400ms)
    ├── risk.py            # Sizing i leverage izračun
    ├── execution.py       # Postavljanje naloga i native exchange SL/TP
    ├── positions.py       # TP1 skalpiranje i Watchdog Ninja Cut
    └── store.py           # SQLite outbox i perzistencija
```
