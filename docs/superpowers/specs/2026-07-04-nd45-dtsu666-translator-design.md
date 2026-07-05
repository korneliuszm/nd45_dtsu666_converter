# Spec: Translator Modbus ND45 → DTSU666 (Sigenergy Power Sensor)

**Data:** 2026-07-04
**Status:** zatwierdzony (design), przed planem implementacji
**Repo docelowe:** https://github.com/korneliuszm/nd45_dtsu666_converter.git

## 1. Cel

Most protokołowy działający jako pojedyncza usługa na Seeed reComputer R1000 (RPi CM4, Ubuntu),
tłumaczący dane z analizatora energii **Lumel ND45** (odpytywany po **Modbus TCP** jako klient/master)
na mapę rejestrów licznika **DTSU666**, którą magazyn energii **Sigenergy** odpytuje po **Modbus RTU
(RS-485)** jako master, oczekując licznika w roli "Power Sensor".

Program **nie jest** gatewayem 1:1 — tłumaczy między dwiema różnymi mapami rejestrów (adresy, typy,
skalowanie, kolejność faz, konwencja znaku) przez pośredni **model kanoniczny** w jednostkach SI.

## 2. Kontekst i priorytety

- **Rozwiązanie tymczasowe/pomostowe**, jedno urządzenie, żywotność kilka miesięcy — do czasu dostawy
  fizycznego licznika DTSU666.
- Priorytet: **krótki czas do bezpiecznego, działającego rozwiązania**. Nie architektura pod flotę,
  nie długoterminowe utrzymanie. YAGNI.
- Język: **Python** + **pymodbus** (jedna dojrzała biblioteka: TCP client + RTU server).

## 3. Architektura i model współbieżności

**Decyzja: pojedynczy proces `asyncio`, jeden event loop.**

Uzasadnienie:
- `pymodbus` 3.x ma natywnie asynchroniczny serwer RTU (`StartAsyncSerialServer`) i asynchronicznego
  klienta TCP — jedna biblioteka, jeden model współbieżności.
- Jeden event loop = **brak zamków na współdzielonym cache**; poller pisze, serwer czyta, wszystko
  w jednym wątku.
- Serwer RTU odpowiada **natychmiast z cache**, nigdy nie czeka na TCP (warunek krytyczny #1).

**Fallback (plan B):** gdyby async serial okazał się niestabilny na sprzętowym UART reComputera —
serwer RTU w wątku (sync `StartSerialServer`) + poller w drugim wątku z jednym `threading.Lock`
na cache. Nie jest to ścieżka domyślna.

**Przepływ danych:**
```
ND45 (TCP slave) --FC03--> [poller] --decode--> [model kanoniczny SI + timestamp]
                                                          |
                                          [watchdog: wiek danych]
                                                          |
Sigenergy (RTU master) --FC03--> [serwer RTU] <--encode-- (czyta z cache)
```

## 4. Struktura modułów

```
nd45_dtsu666/
  __main__.py        # argparse: run | diag | selftest
  config.py          # ładowanie + walidacja JSON (pydantic)
  canonical.py       # dataclass: wartości SI + timestamp + valid; jedno źródło prawdy
  codec.py           # float32 word/byte order, scale, offset, sign, kompozyt 2-rejestrowy
  nd45_poller.py     # async klient TCP: read → decode → zapis do canonical
  dtsu_server.py     # async serwer RTU: canonical → rejestry DTSU, serwowanie + watchdog
  diagnostics.py     # renderowanie tabeli diagnostycznej
config/
  config.json        # runtime: IP/port/unit ND45, /dev/tty, baud, slave id, interwały, progi
  registers.json     # mapy rejestrów: nd45_source + dtsu_target, seed z PDF (edytowalny)
systemd/nd45-dtsu666.service
tests/               # testy codec, mapowania, fail-safe, e2e in-process
.github/workflows/ci.yml
pyproject.toml, README.md
```

Każdy moduł ma jedną odpowiedzialność i jest testowalny osobno: `codec` bez sieci, `nd45_poller`
z mockiem klienta, `dtsu_server` in-process po TCP w testach.

## 5. Model kanoniczny (jednostki SI)

Dataclass z polami + `timestamp: float` + `valid: bool`. Klucze:

| Klucz | Opis | Jednostka |
|-------|------|-----------|
| `u_l1`, `u_l2`, `u_l3` | napięcia fazowe | V |
| `u_l12`, `u_l23`, `u_l31` | napięcia międzyfazowe | V |
| `i_l1`, `i_l2`, `i_l3` | prądy | A |
| `p_l1`, `p_l2`, `p_l3`, `p_total` | moc czynna | W |
| `q_l1`, `q_l2`, `q_l3`, `q_total` | moc bierna | var |
| `pf_l1`, `pf_l2`, `pf_l3`, `pf_total` | współczynnik mocy | - |
| `freq` | częstotliwość | Hz |
| `imp_energy_total`, `exp_energy_total` | energia czynna import/eksport | kWh |

## 6. Mapy rejestrów (seed z dokumentacji PDF)

### 6.1. ND45 (źródło — my jesteśmy klientem TCP)

- Modbus TCP/IP Slave, port 502 (natywny Ethernet).
- float32, byte order **B4 B3 B2 B1 = big-endian (ABCD)**, FC03/FC04, adresy **dziesiętne**.
- Zakres poza pomiarem → wartość `2e20` (traktować jako brak/invalid).
- Uwaga: adresy fizyczne; niektóre narzędzia stosują adresację logiczną (+1) → punkt do weryfikacji.

### 6.2. DTSU666 (cel — my jesteśmy slave RTU, Sigenergy pyta)

- Modbus RTU, FC03 (odczyt), domyślnie 9600 8N1, slave addr 1.
- float32 IEEE754 **ABCD (high word first)**, ze skalowaniem (patrz tabela).

### 6.3. Pełna tabela mapowania (ND45 → kanoniczny → DTSU666)

Adresy DTSU podane dziesiętnie (z heksadecymalnych z manuala).

| Kanoniczny | ND45 addr (dec) | DTSU addr (dec / hex) | DTSU symbol | Skala DTSU |
|-----------|-----------------|-----------------------|-------------|-----------|
| `u_l12`  | 140 | 8192 / 0x2000 | Uab  | ×10 (0.1V) |
| `u_l23`  | 142 | 8194 / 0x2002 | Ubc  | ×10 |
| `u_l31`  | 144 | 8196 / 0x2004 | Uca  | ×10 |
| `u_l1`   | 50  | 8198 / 0x2006 | Ua   | ×10 |
| `u_l2`   | 74  | 8200 / 0x2008 | Ub   | ×10 |
| `u_l3`   | 98  | 8202 / 0x200A | Uc   | ×10 |
| `i_l1`   | 52  | 8204 / 0x200C | Ia   | ×1000 (0.001A) |
| `i_l2`   | 76  | 8206 / 0x200E | Ib   | ×1000 |
| `i_l3`   | 100 | 8208 / 0x2010 | Ic   | ×1000 |
| `p_total`| 128 | 8210 / 0x2012 | Pt   | ×10 (0.1W) ⚑ znak |
| `p_l1`   | 56  | 8212 / 0x2014 | Pa   | ×10 ⚑ znak |
| `p_l2`   | 80  | 8214 / 0x2016 | Pb   | ×10 ⚑ znak |
| `p_l3`   | 104 | 8216 / 0x2018 | Pc   | ×10 ⚑ znak |
| `q_total`| 130 | 8218 / 0x201A | Qt   | ×10 (0.1var) |
| `q_l1`   | 58  | 8220 / 0x201C | Qa   | ×10 |
| `q_l2`   | 82  | 8222 / 0x201E | Qb   | ×10 |
| `q_l3`   | 106 | 8224 / 0x2020 | Qc   | ×10 |
| `pf_total`| 136 | 8234 / 0x202A | PFt | ×1000 (0.001) |
| `pf_l1`  | 64  | 8236 / 0x202C | PFa  | ×1000 |
| `pf_l2`  | 88  | 8238 / 0x202E | PFb  | ×1000 |
| `pf_l3`  | 112 | 8240 / 0x2030 | PFc  | ×1000 |
| `freq`   | 818 | 8260 / 0x2044 | Freq | ×100 (0.01Hz) |
| `imp_energy_total` | compose(912,914)×(1000,1) | 4128 / 0x1020 | ImpEp (forward active energy) | ×1 (kWh) |
| `exp_energy_total` | compose(928,930)×(1000,1) | 4130 / 0x1022 | ExpEp (reverse active energy) | ×1 (kWh) |

**Kompozyt energii ND45:** energia jest w dwóch rejestrach (MWh + kWh):
`kWh = reg_high × 1000 + reg_low`. Suma import = (912,914), suma eksport = (928,930).

## 7. Format JSON konfiguracji

**`registers.json`** — dwie mapy spięte kluczem kanonicznym; per rejestr: adres, word/byte order,
skala, offset, znak. Przykład (skrót):

```jsonc
{
  "nd45_source": {
    "word_order": "big", "byte_order": "big",
    "points": {
      "u_l1":   {"addr": 50,  "scale": 1, "sign": 1},
      "i_l1":   {"addr": 52,  "scale": 1, "sign": 1},
      "p_total":{"addr": 128, "scale": 1, "sign": 1},
      "freq":   {"addr": 818, "scale": 1, "sign": 1},
      "imp_energy_total": {"compose": [912, 914], "factors": [1000, 1], "unit": "kWh"}
    }
  },
  "dtsu_target": {
    "word_order": "big", "byte_order": "big",
    "points": {
      "u_l1":   {"addr": 8198, "from": "u_l1",    "scale": 10,   "offset": 0, "sign": 1},
      "i_l1":   {"addr": 8204, "from": "i_l1",    "scale": 1000, "offset": 0, "sign": 1},
      "p_total":{"addr": 8210, "from": "p_total", "scale": 10,   "offset": 0, "sign": 1},
      "freq":   {"addr": 8260, "from": "freq",    "scale": 100,  "offset": 0, "sign": 1}
    }
  }
}
```

**Semantyka transformacji (jednoznaczna):**
- Odczyt ND45 → kanoniczny: `SI = (raw_float × scale × sign) + offset`.
- Kanoniczny → rejestr DTSU: `register_float = (SI × sign × scale) + offset`.
- `sign ∈ {+1, -1}`: `-1` odwraca znak (do korekty konwencji import/eksport). `scale` domyślnie 1,
  `offset` domyślnie 0.

**`config.json`** — parametry runtime:
```jsonc
{
  "nd45":  {"host": "192.168.1.10", "port": 502, "unit_id": 1, "poll_interval_s": 0.3, "timeout_s": 1.0},
  "dtsu":  {"port": "/dev/ttyAMA0", "baudrate": 9600, "parity": "N", "stopbits": 1, "slave_id": 1},
  "safety":{"max_data_age_s": 3.0}
}
```

## 8. Obsługa błędów i fail-safe

- **Poller**: timeout/wyjątek TCP → NIE zeruje cache, tylko przestaje odświeżać `timestamp`.
  Retry z backoffem, log do journald.
- **Watchdog wieku danych**: `now - timestamp > max_data_age_s` (domyślnie **3 s**) → model `invalid`.
- **Fail-safe RTU**: gdy `invalid`, serwer **nie odpowiada** na ramki (drop odpowiedzi na poziomie
  handlera) → Sigenergy widzi timeout i sam wchodzi w bezpieczny tryb (warunek #3). Po powrocie
  świeżych danych — automatyczne wznowienie.
- **Mechanizm "ciszy"** w pymodbus (custom request handler vs zatrzymanie transportu) — do potwierdzenia
  na sprzęcie; **czy Sigenergy faktycznie wchodzi w safe mode przy timeoutcie metra to najważniejsza
  niewiadoma** (patrz §11).
- **systemd**: `Restart=always`, `RestartSec=2`, logi do journald (stdout/stderr).

## 9. Tryb diagnostyczny (CLI, argparse)

- `python -m nd45_dtsu666 run` — normalna praca.
- `python -m nd45_dtsu666 diag` — tabela odświeżana na żywo: `klucz kanoniczny | ND45 addr/raw/SI |
  DTSU addr/wartość wystawiana | wiek danych | status`. Główne narzędzie rozruchu (warunek #7).
- `python -m nd45_dtsu666 selftest` — wystawia RTU z syntetycznymi danymi bez ND45 (do testu mbpoll).

## 10. Plan testów przed podłączeniem do Sigenergy

1. **Unit (CI, bez sprzętu):** codec float32 / word order / scale / offset / sign, kompozyt energii,
   logika watchdoga i fail-safe.
2. **E2E in-process (CI):** klient TCP ↔ serwer (in-process) — wartość wchodząca po ND45 wychodzi
   poprawnie po stronie DTSU.
3. **Bench z `mbpoll` (na reComputerze):** mbpoll jako master RTU czyta nasz serwer; weryfikacja
   adresów, typów, skali i word order na realnym RS-485 — **przed** podłączeniem magazynu.

## 11. Założenia do weryfikacji na żywym sprzęcie Sigenergy

| # | Założenie | Domyślne | Ryzyko |
|---|-----------|----------|--------|
| 1 | Konwencja znaku P (import+/export−) zgodna z oczekiwaniem Sigenergy | sign=+1 | **Wysokie** — błąd = ładowanie zamiast rozładowania |
| 2 | Kolejność faz L1/L2/L3 ND45 = A/B/C Sigenergy | 1:1 | Średnie |
| 3 | Skalowanie DTSU (×0.1V/×0.001A/×0.1W…) akceptowane przez Sigenergy | wg manuala | Średnie |
| 4 | Word/byte order (ABCD) po obu stronach | big/big | Klasyczny byteswap |
| 5 | Slave ID / baud DTSU których pyta Sigenergy | 1 / 9600 8N1 | Niskie |
| 6 | Sigenergy wchodzi w safe mode przy timeoutcie metra | zakładamy tak | **Wysokie** |
| 7 | Off-by-one adresów ND45 (physical vs logical +1) | physical | Niskie |
| 8 | RS-485 kierunek na reComputerze: auto-hw czy RTS/TIOCSRS485 | sprawdzić | Średnie |
| 9 | Function code / interwał odpytywania Sigenergy | FC03 / dowolny | Niskie |

## 12. Szacunek czasu

- **~2 dni robocze** do prototypu zweryfikowanego na benchu (mbpoll potwierdza poprawne rejestry).
- **+1 sesja on-site** na strojenie znaku/faz/skali przy żywym przepływie mocy i potwierdzenie fail-safe.
- Realnie **3–4 dni kalendarzowe** do zostawienia bez nadzoru (bufor na async serial i zachowanie Sigenergy).

## 13. CI/CD (GitHub Actions)

`.github/workflows/ci.yml`: push/PR → `ruff` (lint) + `pytest` (unit + e2e in-process),
opcjonalnie `mypy`. Bez sprzętu w CI. Wersja Pythona dopasowana do Ubuntu na reComputerze.

## 14. Wdrożenie

- systemd unit z `ExecStart` wskazującym interpreter z **virtualenv** (unikamy konfliktu z systemowym
  Pythonem Ubuntu).
- Logi do journald.

## 15. Domyślne decyzje (zatwierdzone)

1. Liczniki energii (kWh import/eksport) uwzględnione od razu (kompozyt 2-rejestrowy ND45).
2. Próg fail-safe = 3 s; interwał pollingu ND45 = 0.3 s.
3. `pydantic` do walidacji configu przy starcie.
