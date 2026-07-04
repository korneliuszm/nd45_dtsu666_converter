# Spec: Interactive `monitor` mode (commissioning dashboard)

**Data:** 2026-07-04
**Status:** zatwierdzony design
**Kontekst:** rozszerzenie istniejącego mostu ND45→DTSU666 o czytelny tryb rozruchowy.

## 1. Cel

Subkomenda CLI `monitor`, która uruchamia **żywy most** (poller ND45 + serwer RTU + fail-safe)
i jednocześnie pokazuje na żywo w terminalu:
- tabelę podstawowych wartości czytanych z ND45 (per faza + sumy + kierunek mocy),
- aktywność zapytań Modbus RTU przychodzących z magazynu Sigenergy.

Cel: proste, czytelne narzędzie do debugowania na rozruchu — bez dodatkowych zależności.

Uruchomienie: `python -m nd45_dtsu666 monitor`. Działa jak `run` (steruje realnym RS-485,
odpowiada Sigenergy), plus dashboard odświeżany ~1×/s.

## 2. Zakres (YAGNI)

- BEZ bibliotek TUI (curses/rich) — zwykły `print` + czyszczenie ekranu ANSI, jak istniejący `diag`.
- BEZ pasywnego sniffera magistrali — aktywność RTU widzimy będąc serwerem (przechwytujemy dostęp do datastore).
- Podczas fail-safe (dane nieświeże) serwer jest zatrzymany zgodnie z projektem → panel RTU pokazuje
  stan `FAIL-SAFE SILENT` i rosnący "last seen". To pożądany sygnał, nie błąd.

## 3. Przechwytywanie zapytań RTU

`RecordingSlaveContext(ModbusSlaveContext)` — nadpisuje `getValues(fc, address, count)`: zapisuje
rekord `(ts, fc, address, count)` do `RtuActivity`, a następnie deleguje do rodzica (zwraca realne
rejestry). To publiczny punkt rozszerzeń pymodbus — czystsze niż parsowanie logów pymodbus czy
hakowanie handlera żądań.

`RtuActivity`:
- `record(fc, address, count, ts)` — dodaje rekord; utrzymuje licznik całkowity, tally per blok
  `(fc, address, count)`, ostatni timestamp i bufor ostatnich N rekordów (deque, N=8).
- `summary(now)` — zwraca dane do renderu: total, rate (z okna czasu), last_seen_age, lista bloków
  z liczbą trafień, ostatnie rekordy.

## 4. Współdzielenie pipeline

W `app.py` wydzielić `build_pipeline(config, registers, stop_event, activity=None, client=None)`
zwracające komponenty (store, context, client, coros=[poller, supervisor]). `run_app` staje się
cienką nakładką (connect → gather(coros) → close). `monitor` używa tego samego z `activity` i dokłada
korutynę wyświetlania. `build_context(target, slave_id, activity=None)` — gdy `activity` podane,
używa `RecordingSlaveContext`; domyślnie zachowanie bez zmian.

## 5. Układ dashboardu

```
 ND45 -> DTSU666  monitor                     21:30:05   state: SERVING
────────────────────────────────────────────────────────────────────
 ND45 (source)                        data age: 0.31s   poll: OK
   Phase     U [V]    I [A]     P [W]    Q [var]    PF
   L1        230.1     5.02    1153.0    180.0    0.988
   L2        231.0     4.98    1140.0    175.0    0.987
   L3        229.4     5.10    1160.0    182.0    0.986
   TOTAL                       3453.0    537.0    0.987     f = 50.01 Hz
   Direction: IMPORT (P>0)        E_imp = 1234.5   E_exp = 67.8  kWh
────────────────────────────────────────────────────────────────────
 Sigenergy RTU  (slave 1)                        state: SERVING
   requests: 412     rate: 1.9/s     last seen: 0.2s ago
   blocks read:  FC03 @8192 x64 (312)    FC03 @4126 x24 (100)
   recent:  21:30:05.1 @8192x64   21:30:04.9 @4126x24   21:30:04.6 @8192x64
────────────────────────────────────────────────────────────────────
 Ctrl-C to quit
```

- Panel ND45: per faza U/I/P/Q/PF (z canonical: u_l1.., i_l1.., p_l1.., q_l1.., pf_l1..); TOTAL z
  p_total/q_total/pf_total; f=freq; linia **Direction: IMPORT/EXPORT** wg znaku p_total (kluczowe do
  weryfikacji konwencji znaku na rozruchu); E_imp/E_exp z imp/exp_energy_total. `data age` i `poll: OK/STALE`.
- Panel RTU: `state` (SERVING gdy dane świeże / FAIL-SAFE SILENT gdy nie), total żądań, rate,
  last seen, tally bloków `FC @addr xcount (hits)`, lista ostatnich żądań.

## 6. Moduły

- `dtsu_server.py`: + `RtuActivity`, + `RecordingSlaveContext`, `build_context(..., activity=None)`.
- `app.py`: + `build_pipeline(...)`; `run_app` przerobione na nakładkę (bez zmiany sygnatury).
- `monitor.py` (nowy): `render_dashboard(canonical, age, healthy, activity, slave_id, now) -> str`
  (czysta funkcja; czyta stałe klucze canonical + `activity.summary(now)` w środku) +
  `run_monitor(config, registers, stop_event)` (pętla wyświetlania na gather z pipeline).
- `__main__.py`: + subkomenda `monitor` → `run_monitor` (lazy import).

## 7. Testy

- `RecordingSlaveContext`: po `getValues(3, 8192, 64)` — `RtuActivity` zarejestrowało rekord (total=1,
  tally bloku, last_seen) **oraz** zwrócone zostały realne rejestry z datastore (delegacja działa).
- `RtuActivity.summary`: tally i last_seen liczone poprawnie.
- `render_dashboard`: output zawiera wartości faz, TOTAL P, etykietę IMPORT/EXPORT wg znaku, licznik
  żądań i tally bloków; przy braku danych/`healthy=False` pokazuje STALE/SILENT.
- `build_context(activity=...)` używa `RecordingSlaveContext`.
- Pętla `run_monitor` bez testów (spójnie z istniejącym `diag`/`selftest`).

## 8. Założenia / uwagi

- `monitor` wymaga realnego portu szeregowego (jak `run`) — to narzędzie rozruchowe przy podłączonym
  Sigenergy; do testu bez ND45/Sigenergy służy `selftest`.
- Odświeżanie ~1 s; `rate` liczone z okna ostatnich rekordów.
