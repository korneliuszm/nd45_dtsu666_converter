# Huawei SmartLogger jako drugie źródło danych — design

Data: 2026-07-30
Źródło dokumentacji: `SmartLogger ModBus Interface Definitions`, Issue 35 (2020-02-20)

> **Nota po wdrożeniu (2026-08-04).** Dokument zostaje w wersji projektowej jako
> zapis decyzji; poniższe szczegóły zmieniły się w rzeczywistym wdrożeniu:
> port RS485 mostka to **`/dev/ttyAMA4`**, nie `ttyAMA3` (potwierdzone na
> okablowaniu), a `poll_interval_s` wynosi **1.0 s**, nie 5.0 s. Stan bieżący:
> `config/config.json`, `docs/smartlogger-dtsu-map.md` i `docs/DEPLOY.md`.
> Analiza pokrycia rejestrów i reguły `derive` poniżej pozostają aktualne.

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
| `p_total` | 40525 Active power | I32 | 1000 (kW) | 1.0 → W |
| `q_total` | 40544 Reactive power | I32 | 1000 (kVar) | 1.0 → var |
| `pf_total` | 40532 Power factor | I16 | 1000 | 0.001 |
| `u_l12/l23/l31` | 40575/76/77 Uab/Ubc/Uca | U16 | 10 (V) | 0.1 |
| `i_l1/l2/l3` | 40572/73/74 Phase A/B/C current | I16 | **1** (A) | 1.0 |
| `exp_energy_total` | 40560 E‑Total | U32 | 10 (kWh) | 0.1 |
| `e_daily` | 40562 E‑Daily | U32 | 10 (kWh) | 0.1 (bonus, poza mapą DTSU) |
| `dc_power` | 40521 Input power | U32 | 1000 (kW) | 1.0 (bonus, poza mapą DTSU) |

Braki: napięcia fazowe, moce per‑faza, moc pozorna, PF per‑faza, energia pobrana,
energia bierna, częstotliwość.

### Wariant „licznik" — logic device = adres RS485 licznika

Tablica 2‑5. Semantycznie najbliższa roli ND45 (punkt przyłączenia).

| kanoniczny | rejestr | typ | Gain | scale |
|---|---|---|---|---|
| `u_l1/l2/l3` | 32260/62/64 | U32 | 100 | 0.01 |
| `u_l12/l23/l31` | 32266/68/70 | U32 | 100 | 0.01 |
| `i_l1/l2/l3` | 32272/74/76 | I32 | 10 | 0.1 |
| `p_total` | 32278 | I32 | 1000 (kW) | 1.0 |
| `q_total` | 32280 | I32 | 1000 (kVar) | 1.0 |
| `pf_total` | 32284 | I16 | 1000 | 0.001 |
| `s_total` | 32287 Apparent power | I32 | 1000 (kVA) | 1.0 |
| `p_l1/l2/l3` | 32335/37/39 | I32 | 1000 (kW) | 1.0 |
| `imp_energy_total` | 32357 Positive active | I64 | 100 (kWh) | 0.01 |
| `exp_energy_total` | 32349 Negative active | I64 | 100 (kWh) | 0.01 |
| `reactive_imp_energy_total` | 32361 Positive reactive | I64 | 100 | 0.01 |
| `reactive_exp_energy_total` | 32353 Negative reactive | I64 | 100 | 0.01 |

Braki: Q/S/PF per‑faza, energia per‑faza, częstotliwość.

### Częstotliwość — jedyny nieodtwarzalny brak

**W całym dokumencie (50 stron) nie ma ani jednego wystąpienia „frequency" ani
„Hz".** SmartLogger nie eksponuje częstotliwości sieci na żadnym poziomie —
ani w rejestrach plant, ani w tablicy licznika, ani w remapowanych rejestrach
falowników (2.7). To jedyna wielkość, której nie da się ani odczytać, ani
policzyć z innych. Podajemy stałą 50.0 Hz przez regułę `constant`.

Punkty w obu tabelach noszą **zwykłe nazwy kanoniczne**, bez prefiksów: mostek
SmartLoggera jest pełnoprawnym DTSU666 i wypełnia własny model. W metrykach
rozdziela je etykieta `bridge`.

### „Gain" nie wymaga nowego pojęcia

Huawei dokumentuje Gain jako dzielnik (`wartość = raw / Gain`). Istniejący
`scale` w mapie jest mnożnikiem, więc Gain składa się z konwersją jednostek w
jedną liczbę. Dla mocy wychodzi szczególnie czysto: I32 Gain 1000 w kW → SI w
watach to `raw/1000 × 1000` = `raw`, czyli `scale: 1.0`.

## Architektura: dwa niezależne mostki w jednym procesie

Klient potrzebuje **dwóch osobnych liczników**, każdego na własnym sprzętowym
porcie RS485, a nie jednego licznika łączącego dwa źródła:

| | mostek `nd45` | mostek `smartlogger` |
|---|---|---|
| źródło | Lumel ND45 (Modbus TCP, float32) | Huawei SmartLogger (Modbus TCP, logic ID 0) |
| wyjście | DTSU666 / RS485 `/dev/ttyAMA2` | DTSU666 / RS485 `/dev/ttyAMA3` |
| co widzi Sigenergy | bilans przyłącza | produkcja farmy PV |
| `safety.max_data_age_s` | 3.0 s (poll 0,3 s) | 30.0 s (poll 5 s) |

```
                      ┌─ bridge "nd45" ──────────────────────────────────┐
ND45 (TCP) ──FC03──>  │ poller → CanonicalStore → datastore → RTU server │──> Sigenergy A
                      │              │                          ▲        │    /dev/ttyAMA2
                      │        HealthGate(3.0s) ─────────────────┘        │
                      └──────────────────────────────────────────────────┘
                      ┌─ bridge "smartlogger" ───────────────────────────┐
SmartLogger ──FC03──> │ poller → CanonicalStore → datastore → RTU server │──> Sigenergy B
                      │              │                          ▲        │    /dev/ttyAMA3
                      │        HealthGate(30.0s) ────────────────┘        │
                      └──────────────────────────────────────────────────┘
wdrożenie: osobna usługa systemd na mostek (`nd45-dtsu666@nd45`, `@smartlogger`)
```

Mostki nie dzielą **niczego**: osobny klient, `CanonicalStore`, datastore, bramka
świeżości, transport, licznik pollów, stan serwera. Kod warstwy serwera
(`supervise_server`, `build_context`, `make_server`, `update_datastore`) był już w
pełni sparametryzowany, więc dwa mostki to zmiana w wiring, nie w tej warstwie.

## Decyzje projektowe

### 1. Fail-safe jest per mostek

Zestarzenie danych mostka wycisza **wyłącznie jego** RS485, żeby Sigenergy po tej
szynie zobaczyło timeout i weszło we własny safe mode. Zweryfikowane na żywo w
obie strony (`tests/test_bridge_isolation.py` + przebieg na prawdziwych socketach):
zabicie SmartLoggera zostawia port A serwujący −60000 W, a zabicie ND45 zostawia
port B serwujący 1,2345 MW.

To odwraca decyzję z wcześniejszej iteracji tego dokumentu, gdzie dane z Huawei
były telemetrią, która celowo *nie* bramkowała wyjścia. Przy dwóch niezależnych
licznikach właściwym zachowaniem jest normalny fail-safe każdego z nich.

### 2. Progi świeżości muszą być osobne

`safety.max_data_age_s` = 3.0 s jest właściwe dla ND45 (poll 0,3 s), ale dla
SmartLoggera odpytywanego co 5 s oznaczałoby permanentny fail-safe. Stąd walidator
`AppConfig._check_freshness_threshold_allows_polling`: **`max_data_age_s ≥ 2 ×
poll_interval_s`** dla każdego mostka. Łapie dokładnie ten błąd, który w polu
wygląda jak awaria źródła.

### 3. Kolizja portów RS485 odrzucana przy starcie

Najgroźniejsza pomyłka konfiguracyjna: dwa mostki na tym samym `/dev/tty*` biłyby
się o urządzenie, a `listen()` w pymodbus 3.6.9 zjada `OSError` — przegrywający
zawiesiłby się w ciszy, zamiast paść. `_check_output_transports_do_not_collide`
odrzuca to przy wczytaniu configu (i analogicznie kolizję `host:port` dla TCP).
`slave_id` **może** się powtarzać, bo szyny są elektrycznie niezależne.

### 4. Odzysk zawieszonego pollera: najpierw w procesie, potem systemd

`app.supervise_poller` nadzoruje
poller każdego mostka osobno: gdy `heartbeat` nie ruszy się dłużej niż
`source.stall_timeout_s`, anuluje task, zamyka klienta, buduje nowego przez
`client_factory`, łączy i startuje poller od nowa. Licznik `recovery.restarts` idzie
do Prometheusa.

Ważne rozróżnienie: **źródło nieosiągalne to nie zawieszenie**. `run_poller`
przechodzi wtedy przez swoją ścieżkę błędu, dotyka heartbeatu i kręci się dalej —
dane się starzeją, `supervise_server` wycisza wyjście, i nic nie jest odbudowywane.
Odzysk dotyczy tylko `await`, który nigdy nie wraca. Pilnuje tego test
`test_a_healthy_poll_loop_is_never_rebuilt`.

**Rozwiązane przez rozdzielenie na osobne usługi.** Początkowo odzysk w procesie był
jedynym mechanizmem, bo restart przez systemd ubijał oba mostki — i trzeba było
zaakceptować brak zewnętrznej siatki bezpieczeństwa. Po rozdzieleniu na
`nd45-dtsu666@nd45` i `nd45-dtsu666@smartlogger` restart dotyka **tylko jednej**
usługi, więc watchdog może wrócić do pilnowania postępu pollera. Kolejność:
`stall_timeout_s` (60 s) < `WatchdogSec` (90 s) — najpierw tani odzysk klienta w
procesie, a jeśli się zapętli, systemd restartuje tę jedną usługę. Zastrzeżenie o
braku siatki bezpieczeństwa **już nie obowiązuje**; `bridge_poller_restarts_total`
pozostaje przydatny jako sygnał, ale nie jest jedyną linią obrony.

**Izolacja na dwóch poziomach.** Wewnątrz procesu mostki nie dzielą niczego poza
event loopem. Między procesami — `run --bridge <nazwa>` ogranicza proces do jednego
mostka, co zamyka ostatnią wspólną zależność: crash, OOM czy zwykły `systemctl
restart` na jednej usłudze nie dotyka drugiej. Zweryfikowane na żywo: `SIGKILL` na
usłudze smartlogger zostawia usługę nd45 serwującą −60000 W i odwrotnie.

**Obie usługi czytają ten sam `config.json` — świadomie.** Walidatory odrzucające dwa
mostki na tym samym porcie szeregowym działają tylko wtedy, gdy jedna konfiguracja
widzi oba mostki; rozdzielenie plików odebrałoby tę kontrolę, a pymodbus zjada
wynikający błąd bindowania. Osobne porty metryk (`bridges[].metrics_port`)
bo dwa procesy nie mogą dzielić jednego portu.

`watchdog_loop` śledzi postęp pętli odpytywania przez `app.SlowestHeartbeat`
(najwolniej postępujący mostek w tym procesie — przy jednym mostku na usługę to po
prostu jego poller). Do systemd eskaluje dopiero to, czego odzysk w procesie nie
naprawił. `WatchdogSec=90` bez zmian.

### 5. Konfiguracja: lista mostków bez migracji istniejących plików

Klucze `nd45`/`dtsu`/`safety` **zostają na wierzchu** jako pierwszy mostek
(`AppConfig.PRIMARY_BRIDGE_NAME = "nd45"`), więc `config.json` i wszystkie 6 plików
`config_debug_*.json` walidują bez zmian. Kolejne mostki dochodzą w liście
`bridges`. Kod czyta wyłącznie `AppConfig.bridge_specs`, które składa jedno z
drugim, więc jest jedna ścieżka wykonania niezależnie od formatu pliku.

Nazwa `nd45` jest zarezerwowana, nazwy muszą być unikalne, a wpis wyłączony
(`enabled: false`) może mieć puste `host` — dzięki temu mostek B jest wysłany
gotowy do włączenia jednym polem.

### 6. Nazwy kanoniczne bez prefiksów

Mostek B jest pełnoprawnym DTSU666, więc wypełnia **własne 36 punktów** zwykłymi
nazwami (`p_total`, `u_l1`, …) — żadnych `pv_*`/`mtr_*`. Rozdziela je etykieta
`bridge` w metrykach, nie nazwa punktu. Mapy celu (`dtsu_target`,
`dtsu_sigen_ext_target`, `dtsu_sigen_ext_energy`, `dtsu_sigen_identity`) są
**wspólne** dla obu mostków; różnicuje je tylko `ct_ratio` z własnego
`dtsu.identity.ir_at`, przekazywany do `update_datastore` jako parametr runtime.

Poller Huawei **wywołuje** `compute_derived` — ma własny model kanoniczny i musi
wypełnić `active_energy_total` oraz `net_*`, do których odwołują się mapy wyjściowe.

### 7. Nieprawidłowe wartości nie odrzucają całej próbki

ND45 przy niepoprawnym krytycznym kanale odrzuca cały sample (`PollError`).
SmartLogger — nie: sentinel „wartość nieprawidłowa" (max typu: `0x7FFF`, `0xFFFF`,
`0x7FFFFFFF`, …) zeruje jeden punkt, loguje raz na epizod, a reszta poll'a ląduje.
Przy źródle odświeżanym co 5 s utrata całej próbki z powodu jednego odłączonego
falownika byłaby zbyt kosztowna. Źródło faktycznie nieosiągalne nadal rzuca z
`read_groups`, więc dane się starzeją i wyjście jest wyciszane jak należy.
`codec.decode_int_point` zwraca dla sentinela NaN, więc wołający używa tego samego
`math.isfinite`, co ścieżka float32.

### 8. Reguły pochodne deklaratywnie w `registers.json`

Zgodnie z zasadą projektu „mapy edytuje się bez ruszania kodu". Operacje
(`canonical.apply_derive`, wykonywane w kolejności listy):

| `op` | znaczenie | użycie |
|---|---|---|
| `constant` | wartość stała | `freq: 50.0`, zerowe liczniki |
| `copy` | jeden punkt do wielu | PF per-faza z PF total |
| `split_equal` | `from / n` | P/Q/S per-faza z totali |
| `phase_from_line` | `U_line / √3` | napięcia fazowe z międzyfazowych |
| `hypot` | `√(a² + b²)` | moc pozorna z P i Q |
| `ratio_split` | rozdział wg wag | Q per-faza proporcjonalnie do P per-faza |
| `pf_from_p_s` | `P/S`, `S=0 → 1.0` | PF per-faza |

`ratio_split` przy sumie wag ≈ 0 (noc, brak produkcji) spada na równy podział —
proporcje nie niosą wtedy informacji.

## Nowe/zmienione moduły

| Plik | Zmiana |
|---|---|
| `codec.py` | `INT_DTYPES`, `register_width`, `registers_to_int`, `int_sentinel`, `int_is_invalid`, `decode_int_point`. Ścieżka float32 nietknięta. |
| `config.py` | `SourcePoint.dtype`/`.width`, `ReadGroup`, `DeriveOp`, `SourceSide.read_groups/address_offset/derive`, `Nd45SourceConf`/`HuaweiSourceConf`/`BridgeConf`, `AppConfig.bridges` + `bridge_specs`, walidatory kolizji i progów, `RegisterMap.targets`/`source_by_name` |
| `canonical.py` | `apply_derive`, przeniesione `compute_derived` |
| `huawei_poller.py` | **nowy** — `poll_once` zgodne sygnaturą z ND45, `read_groups`, `decode_source` |
| `nd45_poller.py` | `run_poller(..., poll_once_fn=)`; re-export `compute_derived` |
| `app.py` | `BridgeRuntime`, `Pipeline` z listą mostków + akcesory zgodności, `build_pipeline` w pętli, `select_specs` (filtr `--bridge`), `supervise_poller` z odzyskiem, `SlowestHeartbeat`, `_POLL_ONCE` |
| `watchdog.py` | `watchdog_loop` karmiony postępem pollerów (przez `SlowestHeartbeat`) |
| `metrics.py` | `BridgeMetrics`, `RecoveryStats`, rodziny `_bridge_*{bridge=...}`, `/healthz` po wszystkich mostkach, aliasy mostka A |
| `monitor.py` | para paneli na mostek |
| `rtudebug.py`, `diagnostics.py`, `static_debug.py`, `__main__.py` | wybór mostka przez `--bridge` |

## Zgodność wstecz

Przy `config.json` bez włączonych dodatkowych mostków proces buduje dokładnie jeden
mostek i 2 korutyny, a scrape Prometheusa zawiera wszystkie dotychczasowe
nieetykietowane rodziny `nd45_*`/`dtsu_*` (emitowane jako aliasy pierwszego mostka,
żeby wdrożone dashboardy nie padły). Pilnują tego
`test_legacy_config_without_bridges_builds_exactly_one_bridge`,
`test_single_bridge_emits_no_second_bridge_series` i
`test_primary_aliases_keep_the_original_unlabelled_families`.

## Do potwierdzenia przy rozruchu (nie da się z testów)

1. **Offset adresów.** Huawei dokumentuje „40525"; część klientów Modbus wymaga
   `40524` (0‑ vs 1‑based). `SourceSide.address_offset` jest konfigurowalne —
   korekta bez zmiany kodu. Sprawdzić `mbpoll` przed uruchomieniem mostka.
2. **Adres RS485 licznika** za SmartLoggerem — do odczytania z LCD/WebUI; dopóki
   nieznany, `meter_unit_id: null` i działa tylko ścieżka plant.
3. **Realne tempo odświeżania.** Jeśli przekroczy `huawei.max_data_age_s: 60.0`,
   podnieść ten próg — a **nie** `safety.max_data_age_s`.
4. **Znak `p_total` na mostku B.** Czy 40525 jest dodatni przy produkcji.
5. **Rozdzielczość prądów plant.** Gain 1 (I16) to krok 1 A i przepełnienie
   powyżej 32767 A. Przy większej farmie prądy fazowe z device 0 są orientacyjne;
   wiarygodne wartości daje wyłącznie ścieżka licznika.
6. **Czy `/dev/ttyAMA3` jest włączony** w device tree reComputera R1000
   (`ls -l /dev/ttyAMA*`). Jeśli go nie ma, trzeba włączyć overlay — zadanie
   systemowe, nie kod. Konfiguracja odrzuci start, jeśli oba mostki trafią na ten
   sam port, ale nie potrafi wyczarować portu, którego nie ma.
7. **Kierunek RS-485 na drugim porcie** — ten sam sprawdzian co dla `ttyAMA2`.
8. **Przekładnia CT mostka B** (`ir_at: 200`, skopiowana z mostka A). Mostek B
   reprezentuje inny licznik i przy 1,2 MW warto sprawdzić, czy Sigenergy po tej
   szynie czyta sensowne wartości na mapie FC03 (dzielonej przez `ir_at`).
9. **Sens biznesowy ścieżki plant.** Produkcja PV ≠ bilans przyłącza. Jeśli
   Sigenergy na szynie B ma regulować, a nie tylko raportować, właściwym źródłem
   jest licznik na przyłączu (`register_map: "huawei_meter_source"`), nie rejestry
   plant. Przełączenie to jedno pole w `config.json`.
