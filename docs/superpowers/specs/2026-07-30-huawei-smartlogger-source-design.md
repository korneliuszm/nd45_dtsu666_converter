# Huawei SmartLogger jako drugie źródło danych — design

Data: 2026-07-30
Źródło dokumentacji: `SmartLogger ModBus Interface Definitions`, Issue 35 (2020-02-20)

## Problem

Klient chce, żeby mostek potrafił czytać dane także z **Huawei SmartLogger**
(Modbus TCP, port 502). Najważniejsza dla niego jest **moc chwilowa produkowana
przez farmę fotowoltaiczną**.

Pytanie do rozstrzygnięcia przed implementacją: czy Huawei pokrywa 100%
rejestrów, które konwerter eksponuje, a jeśli nie — czy braki da się policzyć
lub zasymulować.

## Analiza pokrycia

Konwerter potrzebuje **36 punktów kanonicznych** (`nd45_source.points`, ta sama
lista co `config.STATIC_DEBUG_VALUE_KEYS`) plus 3 aliasy energii liczone przez
`canonical.compute_derived`. Z tych 36 punktów `dtsu_server.update_datastore`
buduje wszystkie trzy mapy wyjściowe (FC03 klasyczny DTSU666, FC04 Sigen OEM,
FC04 Sigen energy).

SmartLogger udostępnia **dwa całkowicie różne zestawy rejestrów**, adresowane
różnymi logic device ID:

| | **plant** (Tab. 2‑1, logic ID **0**) | **licznik za SmartLoggerem** (Tab. 2‑5, logic ID = adres RS485) |
|---|---|---|
| bezpośrednio z rejestru | **10/36** | **20/36** |
| do policzenia z fizyki | 19 | 15 |
| fizycznie brak | 7 | 1 |

**Odpowiedź: nie ma 100% pokrycia w żadnym wariancie.** Ale po dodaniu reguł
pochodnych oba źródła dają pełne 36 punktów — zweryfikowane testami
`test_plant_covers_the_whole_canonical_model` i
`test_meter_covers_the_whole_canonical_model`.

### Wariant „plant" — logic device 0

Rejestry zagregowane przez SmartLogger ze wszystkich falowników.

| kanoniczny | rejestr | typ | Gain | scale w mapie |
|---|---|---|---|---|
| `pv_p_total` | 40525 Active power | I32 | 1000 (kW) | 1.0 → W |
| `pv_q_total` | 40544 Reactive power | I32 | 1000 (kVar) | 1.0 → var |
| `pv_pf_total` | 40532 Power factor | I16 | 1000 | 0.001 |
| `pv_u_l12/l23/l31` | 40575/76/77 Uab/Ubc/Uca | U16 | 10 (V) | 0.1 |
| `pv_i_l1/l2/l3` | 40572/73/74 Phase A/B/C current | I16 | **1** (A) | 1.0 |
| `pv_exp_energy_total` | 40560 E‑Total | U32 | 10 (kWh) | 0.1 |
| `pv_e_daily` | 40562 E‑Daily | U32 | 10 (kWh) | 0.1 (bonus) |
| `pv_dc_power` | 40521 Input power | U32 | 1000 (kW) | 1.0 (bonus) |

Braki: napięcia fazowe, moce per‑faza, moc pozorna, PF per‑faza, energia pobrana,
energia bierna, częstotliwość.

### Wariant „licznik" — logic device = adres RS485 licznika

Tablica 2‑5. Semantycznie najbliższa roli ND45 (punkt przyłączenia).

| kanoniczny | rejestr | typ | Gain | scale |
|---|---|---|---|---|
| `mtr_u_l1/l2/l3` | 32260/62/64 | U32 | 100 | 0.01 |
| `mtr_u_l12/l23/l31` | 32266/68/70 | U32 | 100 | 0.01 |
| `mtr_i_l1/l2/l3` | 32272/74/76 | I32 | 10 | 0.1 |
| `mtr_p_total` | 32278 | I32 | 1000 (kW) | 1.0 |
| `mtr_q_total` | 32280 | I32 | 1000 (kVar) | 1.0 |
| `mtr_pf_total` | 32284 | I16 | 1000 | 0.001 |
| `mtr_s_total` | 32287 Apparent power | I32 | 1000 (kVA) | 1.0 |
| `mtr_p_l1/l2/l3` | 32335/37/39 | I32 | 1000 (kW) | 1.0 |
| `mtr_imp_energy_total` | 32357 Positive active | I64 | 100 (kWh) | 0.01 |
| `mtr_exp_energy_total` | 32349 Negative active | I64 | 100 (kWh) | 0.01 |
| `mtr_reactive_imp_energy_total` | 32361 Positive reactive | I64 | 100 | 0.01 |
| `mtr_reactive_exp_energy_total` | 32353 Negative reactive | I64 | 100 | 0.01 |

Braki: Q/S/PF per‑faza, energia per‑faza, częstotliwość.

### Częstotliwość — jedyny nieodtwarzalny brak

**W całym dokumencie (50 stron) nie ma ani jednego wystąpienia „frequency" ani
„Hz".** SmartLogger nie eksponuje częstotliwości sieci na żadnym poziomie —
ani w rejestrach plant, ani w tablicy licznika, ani w remapowanych rejestrach
falowników (2.7). To jedyna wielkość, której nie da się ani odczytać, ani
policzyć z innych. Podajemy stałą 50.0 Hz przez regułę `constant`.

### „Gain" nie wymaga nowego pojęcia

Huawei dokumentuje Gain jako dzielnik (`wartość = raw / Gain`). Istniejący
`scale` w mapie jest mnożnikiem, więc Gain składa się z konwersją jednostek w
jedną liczbę. Dla mocy wychodzi szczególnie czysto: I32 Gain 1000 w kW → SI w
watach to `raw/1000 × 1000` = `raw`, czyli `scale: 1.0`.

## Decyzje projektowe

### 1. ND45 pozostaje jedynym źródłem rejestrów DTSU

Rejestr 40525 to **produkcja PV**, nie bilans przyłącza. Sigenergy czyta licznik
jako *Power Sensor* w punkcie przyłączenia (import +, eksport −) i na tej
podstawie reguluje baterię. Podanie produkcji tam, gdzie oczekiwany jest bilans,
zafałszowałoby regulację.

Dane z SmartLoggera wchodzą więc jako **osobne punkty kanoniczne** z prefiksami
`pv_*` (plant) i `mtr_*` (licznik), widoczne w Prometheusie i w `monitor`, ale
domyślnie bez celu w mapach DTSU. Mechanizm `from` w `registers.json` i tak je
rozwiąże, gdyby na miejscu trzeba było przekierować `p_total` na `pv_p_total` —
bez zmiany kodu.

### 2. Świeżość bramkowana per źródło (własność bezpieczeństwa)

`canonical.MergedStore` trzyma po jednym `CanonicalStore` na źródło i **deleguje
`age()`/`is_fresh()` wyłącznie do primary (ND45)**. Wyjście DTSU jest wyciszane
tylko wtedy, gdy zestarzeją się dane, na których Sigenergy reguluje.

Powód: SmartLogger to koncentrator agregujący dane z falowników po RS485.
Odświeżanie liczone jest w sekundach, a dokument (4.2.4) dopuszcza 5 s timeout
Modbus. Przy `safety.max_data_age_s: 3.0` wspólna bramka zrobiłaby z mostka
permanentny fail‑safe. Dlatego `huawei.max_data_age_s` jest osobne i domyślnie
60 s — i **nigdy** nie bramkuje wyjścia.

Zweryfikowane na żywo: przy całkowicie nieosiągalnym SmartLoggerze
(`source_connected 0`, `source_data_age_seconds +Inf`, 6 nieudanych pollów)
`nd45_data_fresh` = 1, `dtsu_server_up` = 1, a Sigenergy nadal czyta poprawne
−60000 W.

### 3. Watchdog karmi wyłącznie poller ND45

Callback SmartLoggera świadomie **nie** dotyka `Heartbeat`. Gdyby dotykał, żywy
poller SmartLoggera utrzymywałby watchdoga zadowolonym przy genuinie zawieszonym
pollerze ND45 i systemd nigdy by nie zrestartował usługi.

### 4. Kolejność scalania: primary zawsze wygrywa

Każdy poll zapisuje do datastore **sumę** wszystkich źródeł, żeby poller, który
właśnie się odpalił, nie wyzerował punktów drugiego. `build_on_update` przyjmuje
`beneath=` (wartości scalane pod naszymi) i `above=` (scalane na nasze); poller
SmartLoggera dostaje `above=(store_nd45,)`, więc nie jest w stanie przesłonić
pomiaru z przyłącza nawet przy kolizji nazw.

### 5. `compute_derived` NIE jest wołane w pollerze Huawei

`compute_derived` czyta nieprefiksowane `imp_energy_total`/`exp_energy_total`,
których mapa Huawei nie ma — wpisałoby zera w `active_energy_total` i `net_*` na
miejsce prawdziwej energii z ND45. Test
`test_huawei_poll_never_emits_unprefixed_canonical_names` tego pilnuje.

Sama funkcja przeniosła się z `nd45_poller.py` do `canonical.py` (to logika
modelu kanonicznego wspólna dla wszystkich źródeł, nie logika ND45);
w `nd45_poller` został re‑export dla istniejących importerów.

### 6. Nieprawidłowe wartości nie odrzucają całej próbki

ND45 przy niepoprawnym krytycznym kanale odrzuca cały sample (`PollError`).
SmartLogger — nie: to źródło telemetryczne, które nie bramkuje wyjścia, więc
sentinel „wartość nieprawidłowa" (max typu: `0x7FFF`, `0xFFFF`, `0x7FFFFFFF`, …)
zeruje jeden punkt, loguje raz na epizod, a reszta poll'a ląduje normalnie.
`codec.decode_int_point` zwraca dla sentinela NaN, więc wołający używa tego
samego `math.isfinite`, co ścieżka float32.

### 7. Reguły pochodne deklaratywnie w `registers.json`

Zgodnie z zasadą projektu „mapy edytuje się bez ruszania kodu". Operacje
(`canonical.apply_derive`, wykonywane w kolejności listy):

| `op` | znaczenie | użycie |
|---|---|---|
| `constant` | wartość stała | `pv_freq: 50.0`, zerowe liczniki |
| `copy` | jeden punkt do wielu | PF per‑faza z PF total |
| `split_equal` | `from / n` | P/Q/S per‑faza z totali |
| `phase_from_line` | `U_line / √3` | napięcia fazowe z międzyfazowych |
| `hypot` | `√(a² + b²)` | moc pozorna z P i Q |
| `ratio_split` | rozdział wg wag | Q per‑faza proporcjonalnie do P per‑faza |
| `pf_from_p_s` | `P/S`, `S=0 → 1.0` | PF per‑faza |

`ratio_split` dla licznika przy sumie wag ≈ 0 (noc, brak produkcji) spada na
równy podział — proporcje nie niosą wtedy informacji.

## Nowe/zmienione moduły

| Plik | Zmiana |
|---|---|
| `codec.py` | `INT_DTYPES`, `register_width`, `registers_to_int`, `int_sentinel`, `int_is_invalid`, `decode_int_point`. Ścieżka float32 nietknięta. |
| `config.py` | `SourcePoint.dtype`/`.width`, `ReadGroup`, `DeriveOp`, `SourceSide.read_groups/address_offset/derive` + walidator pokrycia, `HuaweiConf`, opcjonalne `huawei_*_source` w `RegisterMap` |
| `canonical.py` | `MergedStore`, `apply_derive`, przeniesione `compute_derived` |
| `huawei_poller.py` | **nowy** — `poll_once`, `select_sources`, `read_groups`, `decode_source` |
| `nd45_poller.py` | `run_poller(..., poll_once_fn=)`; re‑export `compute_derived` |
| `app.py` | drugi klient/store/poller w `build_pipeline`, `huawei_client=` seam, `FaultReporter(label=)`, `beneath=`/`above=` w `build_on_update` |
| `metrics.py` | rodzina `*_source_*{source="huawei"}`; istniejące `nd45_*` bez zmian |
| `monitor.py` | panel „PV production (SmartLogger, telemetry)" |

## Zgodność wstecz

Przy `huawei.enabled: false` (domyślnie) mostek buduje się i zachowuje dokładnie
jak wcześniej: 2 korutyny, brak `merged_store`, scrape Prometheusa bez żadnej
rodziny `*_source_*`. Sekcje `huawei_*_source` w `RegisterMap` są opcjonalne,
więc wszystkie 6 plików `config_debug_*.json` walidują bez zmian. Pilnują tego
`test_build_pipeline_without_huawei_builds_exactly_two_coros` i
`test_no_secondary_families_when_huawei_is_disabled`.

## Do potwierdzenia przy rozruchu (nie da się z testów)

1. **Offset adresów.** Huawei dokumentuje „40525"; część klientów Modbus wymaga
   `40524` (0‑ vs 1‑based). `SourceSide.address_offset` jest konfigurowalne —
   korekta bez zmiany kodu. Sprawdzić `mbpoll` przed uruchomieniem mostka.
2. **Adres RS485 licznika** za SmartLoggerem — do odczytania z LCD/WebUI; dopóki
   nieznany, `meter_unit_id: null` i działa tylko ścieżka plant.
3. **Realne tempo odświeżania.** Jeśli przekroczy `huawei.max_data_age_s: 60.0`,
   podnieść ten próg — a **nie** `safety.max_data_age_s`.
4. **Znak `pv_p_total`.** Czy 40525 jest dodatni przy produkcji.
5. **Rozdzielczość prądów plant.** Gain 1 (I16) to krok 1 A i przepełnienie
   powyżej 32767 A. Przy większej farmie prądy fazowe z device 0 są orientacyjne;
   wiarygodne wartości daje wyłącznie ścieżka licznika.
6. **Sens biznesowy ścieżki plant.** Produkcja PV ≠ bilans przyłącza. Jeśli
   Sigenergy ma naprawdę regulować na danych z Huawei, właściwym źródłem jest
   licznik na przyłączu (wariant B), nie rejestry plant.
