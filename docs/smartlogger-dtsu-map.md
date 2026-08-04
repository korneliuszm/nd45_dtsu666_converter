# Mapa rejestrów mostka Huawei SmartLogger → DTSU666 (dla Sigenergy)

Ten dokument opisuje **pełną ścieżkę** danych mostka `smartlogger`: od rejestru
w Huawei SmartLoggerze, przez model kanoniczny w jednostkach SI, po rejestr
wystawiany Sigenergy jako licznik DTSU666 „Sigen Sensor TPX-CH".

Odpowiednik dla drugiego mostka (Lumel ND45) to [`register-map.md`](register-map.md).
Uzasadnienie decyzji projektowych i analiza pokrycia:
[`superpowers/specs/2026-07-30-huawei-smartlogger-source-design.md`](superpowers/specs/2026-07-30-huawei-smartlogger-source-design.md).

**Źródło danych o rejestrach Huawei:** `SmartLogger ModBus Interface Definitions`,
Issue 35 (2020-02-20) — Tabela 2‑1 (SmartLogger) i Tabela 2‑5 (Power Meter).

> **Tabele w tym pliku są generowane** z `config/registers.json` przez
> `scripts/gen_smartlogger_map_doc.py`, żeby nie mogły się rozjechać z tym, co
> mostek naprawdę robi. Po edycji sekcji `huawei_*` w mapie uruchom:
> ```bash
> python scripts/gen_smartlogger_map_doc.py
> ```
> Nie edytuj ręcznie treści między znacznikami `<!-- BEGIN/END GENERATED -->`.

---

## 1. Przegląd ścieżki

```
Huawei SmartLogger                    mostek "smartlogger"                 Sigenergy B
──────────────────                    ────────────────────                 ───────────
rejestry U16/I16/U32/I32/I64   ──►  poll_once (huawei_poller)
   Modbus TCP :502, FC03             ├─ read_groups   (bloki z mapy)
   logic device 0 lub adres          ├─ decode_source (dtype + scale)
   RS485 licznika                    ├─ apply_derive  (uzupełnia braki)
                                     └─ compute_derived (aliasy energii)
                                              │
                                     CanonicalStore (SI: V, A, W, var, VA, Hz, kWh)
                                              │
                                     update_datastore (encode + CT)
                                              │
                                     ┌─ FC03 0x2000/0x1000  (klasyczny DTSU666)
                                     ├─ FC04 0x150A/0x1800  (OEM Sigen)      ──►  RS485
                                     └─ FC03 0xF100/0xF114  (tożsamość)          /dev/ttyAMA4
```

Dwie transformacje, obie zaimplementowane w `codec.py` i obowiązujące dosłownie:

```text
źródło → kanoniczne :  SI       = (raw * scale * sign) + offset
kanoniczne → DTSU   :  register = (SI  * sign * scale) + offset      [/ CT jeśli divide_by_ct]
```

### „Gain" Huawei a `scale` w mapie

Huawei dokumentuje **Gain jako dzielnik**: `wartość_fizyczna = raw / Gain`.
W mapie nie ma osobnego pola na Gain — składa się on z konwersją jednostek do
jednego mnożnika `scale`:

```text
scale = (1 / Gain) * (przelicznik jednostki Huawei → jednostka SI)
```

### Odwrócony znak mocy (`sign: -1`)

Obie sekcje Huawei mają **`sign: -1` na mocy czynnej, biernej i współczynniku
mocy** (`p_*`, `q_total`, `pf_total`). SmartLogger raportuje produkcję jako
wartość dodatnią; ten mostek udaje licznik DTSU666, a konwencja kierunku na tej
instalacji jest odwrotna, więc znak odwracany jest **na wejściu** — cały model
kanoniczny, metryki i monitor pokazują już wartość po odwróceniu.

Moc pozorna (`s_*`) i energie **nie** są odwracane: to wielkości nieujemne, a
kierunek niosą osobne liczniki import/eksport. Odwrócenie dotyczy wyłącznie
sekcji `huawei_*` — mapy wyjściowe DTSU są wspólne dla obu mostków, więc zmiana
w nich przestawiłaby także mostek ND45.

Przykłady z tabel poniżej:

| Rejestr | Gain | Jedn. Huawei | Jedn. SI | Wyliczenie | `scale` |
|---|---:|---|---|---|---:|
| 40525 Active power | 1000 | kW | W | (1/1000) × 1000 | **1** |
| 40532 Power factor | 1000 | – | – | 1/1000 | **0.001** |
| 40575 Uab | 10 | V | V | 1/10 | **0.1** |
| 40560 E‑Total | 10 | kWh | kWh | 1/10 | **0.1** |
| 32260 Phase A voltage | 100 | V | V | 1/100 | **0.01** |
| 32357 Positive active electricity | 100 | kWh | kWh | 1/100 | **0.01** |

Dla mocy wychodzi szczególnie czysto: surowy rejestr I32 jest **wprost w watach**.

### Wartości nieprawidłowe

SmartLogger zwraca **maksimum typu** dla kanału, którego chwilowo nie ma
(odłączony falownik, nieobsadzony rejestr): `0x7FFF`, `0xFFFF`, `0x7FFFFFFF`,
`0xFFFFFFFF`, `0x7FFFFFFFFFFFFFFF`. `codec.decode_int_point` zamienia to na NaN,
a poller **zeruje pojedynczy punkt** i loguje raz na epizod — reszta próbki
ląduje normalnie. Świadoma różnica względem ND45, który przy nieprawidłowej
wartości krytycznej odrzuca cały sample: przy źródle odświeżanym co 5 s utrata
całej próbki z powodu jednego falownika byłaby zbyt kosztowna. Źródło faktycznie
nieosiągalne nadal rzuca błąd → dane się starzeją → wyjście jest wyciszane.

---

## 2. Wariant A — rejestry „plant" (`huawei_plant_source`)

Rejestry zagregowane przez SmartLogger ze **wszystkich falowników**, adresowane
jako **logic device ID = 0**. To domyślne źródło mostka B i to, co realizuje
pierwotny cel: **moc chwilowa produkowana przez farmę PV**.

Konfiguracja: `source.register_map: "huawei_plant_source"`, `source.unit_id: 0`.

### 2.1. Bloki odczytu na drucie

<!-- BEGIN GENERATED: plant-blocks -->

| Blok | Rejestr początkowy | Liczba rejestrów | Zakres | logic device ID |
|---:|---:|---:|---|---|
| 1 | 40521 | 57 | 40521–40577 | 0 (SmartLogger) |

<!-- END GENERATED: plant-blocks -->

Jeden odczyt FC03 pokrywa wszystkie punkty. Do adresu na drucie doliczany jest
`address_offset` (domyślnie 0) — patrz sekcja 6.

### 2.2. Rejestr → punkt kanoniczny

<!-- BEGIN GENERATED: plant-source -->

| Rejestr | Hex | Sygnał wg dokumentacji Huawei | Typ | Gain | Jedn. Huawei | → punkt kanoniczny | `scale` | `sign` | Jedn. SI |
|---:|---|---|---|---:|---|---|---:|---:|---|
| 40521 | 0x9E49 | Input power | U32 | 1000 | kW | `dc_power` | ×1 | +1 | W |
| 40525 | 0x9E4D | Active power | I32 | 1000 | kW | `p_total` | ×1 | **−1** | W |
| 40532 | 0x9E54 | Power factor | I16 | 1000 | - | `pf_total` | ×0.001 | **−1** | - |
| 40544 | 0x9E60 | Reactive power | I32 | 1000 | kVar | `q_total` | ×1 | **−1** | var |
| 40560 | 0x9E70 | E-Total | U32 | 10 | kWh | `exp_energy_total` | ×0.1 | +1 | kWh |
| 40562 | 0x9E72 | E-Daily | U32 | 10 | kWh | `e_daily` | ×0.1 | +1 | kWh |
| 40572 | 0x9E7C | Phase A current | I16 | 1 | A | `i_l1` | ×1 | +1 | A |
| 40573 | 0x9E7D | Phase B current | I16 | 1 | A | `i_l2` | ×1 | +1 | A |
| 40574 | 0x9E7E | Phase C current | I16 | 1 | A | `i_l3` | ×1 | +1 | A |
| 40575 | 0x9E7F | Uab | U16 | 10 | V | `u_l12` | ×0.1 | +1 | V |
| 40576 | 0x9E80 | Ubc | U16 | 10 | V | `u_l23` | ×0.1 | +1 | V |
| 40577 | 0x9E81 | Uca | U16 | 10 | V | `u_l31` | ×0.1 | +1 | V |

<!-- END GENERATED: plant-source -->

**10 z 36** punktów kanonicznych pochodzi wprost z rejestru. `e_daily` i
`dc_power` to bonus — nie mają celu w mapie DTSU, trafiają tylko do metryk
Prometheusa i na ekran `monitor_hsm`.

> ⚠️ **Prądy fazowe mają Gain 1 (I16)** — krok 1 A i przepełnienie powyżej
> 32767 A. Przy większej farmie traktuj je jako orientacyjne; wiarygodne wartości
> daje wyłącznie wariant B (licznik).

### 2.3. Punkty uzupełniane regułami `derive`

<!-- BEGIN GENERATED: plant-derive -->

| Punkt(y) kanoniczne | Operacja | Źródła | Wzór |
|---|---|---|---|
| `u_l1`, `u_l2`, `u_l3` | `phase_from_line` | `u_l12`, `u_l23`, `u_l31` | = napięcie międzyfazowe / √3 |
| `p_l1`, `p_l2`, `p_l3` | `split_equal` | `p_total` | = `p_total` / 3 |
| `q_l1`, `q_l2`, `q_l3` | `split_equal` | `q_total` | = `q_total` / 3 |
| `s_total` | `hypot` | `p_total`, `q_total` | = √(a² + b²) |
| `s_l1`, `s_l2`, `s_l3` | `split_equal` | `s_total` | = `s_total` / 3 |
| `pf_l1`, `pf_l2`, `pf_l3` | `copy` | `pf_total` | = `pf_total` |
| `exp_energy_l1`, `exp_energy_l2`, `exp_energy_l3` | `split_equal` | `exp_energy_total` | = `exp_energy_total` / 3 |
| `freq` | `constant` | – | = 50 (stała) |
| `imp_energy_total`, `imp_energy_l1`, `imp_energy_l2`, `imp_energy_l3` | `constant` | – | = 0 (stała) |
| `reactive_imp_energy_total`, `reactive_exp_energy_total` | `constant` | – | = 0 (stała) |

<!-- END GENERATED: plant-derive -->

Reguły wykonywane są **w kolejności listy**, więc krok może korzystać z wyniku
poprzedniego (`hypot` buduje `s_total`, dopiero potem `split_equal` rozdziela go
na fazy). Uzasadnienie wyborów:

- **`phase_from_line` (÷√3)** — tabela plant podaje wyłącznie napięcia
  międzyfazowe; przy symetrycznej sieci napięcie fazowe to `U_ll/√3`.
- **`split_equal` dla P/Q/S** — plant podaje tylko sumy. Farma PV pracuje
  symetrycznie, więc równy podział jest bliski prawdy.
- **`constant` dla `imp_energy_*`** — punkt pomiarowy farmy nie pobiera energii,
  a SmartLogger nie ma takiego licznika.
- **`constant` dla energii biernej** — brak w tabeli plant.
- **`constant` 50.0 Hz dla `freq`** — patrz niżej.

### 2.4. Częstotliwość — jedyny nieodtwarzalny brak

**W całym dokumencie Huawei (50 stron) nie ma ani jednego wystąpienia
„frequency" ani „Hz".** SmartLogger nie eksponuje częstotliwości sieci na żadnym
poziomie: ani w rejestrach plant, ani w tabeli licznika, ani w remapowanych
rejestrach falowników (sekcja 2.7 dokumentacji). To jedyna wielkość, której nie
da się ani odczytać, ani policzyć z innych — dlatego jest stałą 50.0 Hz.

---

## 3. Wariant B — licznik za SmartLoggerem (`huawei_meter_source`)

Tabela 2‑5 dokumentacji, adresowana **logic device ID = adres RS485 licznika**.
Semantycznie najbliższa roli ND45 (mierzy punkt przyłączenia, nie produkcję).

Konfiguracja: `source.register_map: "huawei_meter_source"`,
`source.unit_id: <adres RS485 licznika>` (do odczytania z LCD/WebUI SmartLoggera).

### 3.1. Bloki odczytu na drucie

<!-- BEGIN GENERATED: meter-blocks -->

| Blok | Rejestr początkowy | Liczba rejestrów | Zakres | logic device ID |
|---:|---:|---:|---|---|
| 1 | 32260 | 30 | 32260–32289 | adres RS485 licznika |
| 2 | 32335 | 30 | 32335–32364 | adres RS485 licznika |

<!-- END GENERATED: meter-blocks -->

### 3.2. Rejestr → punkt kanoniczny

<!-- BEGIN GENERATED: meter-source -->

| Rejestr | Hex | Sygnał wg dokumentacji Huawei | Typ | Gain | Jedn. Huawei | → punkt kanoniczny | `scale` | `sign` | Jedn. SI |
|---:|---|---|---|---:|---|---|---:|---:|---|
| 32260 | 0x7E04 | Phase A voltage | U32 | 100 | V | `u_l1` | ×0.01 | +1 | V |
| 32262 | 0x7E06 | Phase B voltage | U32 | 100 | V | `u_l2` | ×0.01 | +1 | V |
| 32264 | 0x7E08 | Phase C voltage | U32 | 100 | V | `u_l3` | ×0.01 | +1 | V |
| 32266 | 0x7E0A | A-B line voltage | U32 | 100 | V | `u_l12` | ×0.01 | +1 | V |
| 32268 | 0x7E0C | B-C line voltage | U32 | 100 | V | `u_l23` | ×0.01 | +1 | V |
| 32270 | 0x7E0E | C-A line voltage | U32 | 100 | V | `u_l31` | ×0.01 | +1 | V |
| 32272 | 0x7E10 | Phase A current | I32 | 10 | A | `i_l1` | ×0.1 | +1 | A |
| 32274 | 0x7E12 | Phase B current | I32 | 10 | A | `i_l2` | ×0.1 | +1 | A |
| 32276 | 0x7E14 | Phase C current | I32 | 10 | A | `i_l3` | ×0.1 | +1 | A |
| 32278 | 0x7E16 | Active power | I32 | 1000 | kW | `p_total` | ×1 | **−1** | W |
| 32280 | 0x7E18 | Reactive power | I32 | 1000 | kVar | `q_total` | ×1 | **−1** | var |
| 32284 | 0x7E1C | Power factor | I16 | 1000 | - | `pf_total` | ×0.001 | **−1** | - |
| 32287 | 0x7E1F | Apparent power | I32 | 1000 | kVA | `s_total` | ×1 | +1 | VA |
| 32335 | 0x7E4F | Phase A active power | I32 | 1000 | kW | `p_l1` | ×1 | **−1** | W |
| 32337 | 0x7E51 | Phase B active power | I32 | 1000 | kW | `p_l2` | ×1 | **−1** | W |
| 32339 | 0x7E53 | Phase C active power | I32 | 1000 | kW | `p_l3` | ×1 | **−1** | W |
| 32349 | 0x7E5D | Negative active electricity | I64 | 100 | kWh | `exp_energy_total` | ×0.01 | +1 | kWh |
| 32353 | 0x7E61 | Negative reactive electricity | I64 | 100 | kvarh | `reactive_exp_energy_total` | ×0.01 | +1 | kvarh |
| 32357 | 0x7E65 | Positive active electricity | I64 | 100 | kWh | `imp_energy_total` | ×0.01 | +1 | kWh |
| 32361 | 0x7E69 | Positive reactive electricity | I64 | 100 | kvarh | `reactive_imp_energy_total` | ×0.01 | +1 | kvarh |

<!-- END GENERATED: meter-source -->

**20 z 36** punktów wprost z rejestru — dwa razy więcej niż wariant plant.
Kierunek energii: „Positive" = pobór (`imp_*`), „Negative" = oddanie (`exp_*`).

### 3.3. Punkty uzupełniane regułami `derive`

<!-- BEGIN GENERATED: meter-derive -->

| Punkt(y) kanoniczne | Operacja | Źródła | Wzór |
|---|---|---|---|
| `q_l1`, `q_l2`, `q_l3` | `ratio_split` | `q_total`, `p_l1`, `p_l2`, `p_l3` | = `q_total` × \|waga\| / Σ\|wagi\|; przy Σ≈0 podział równy |
| `s_l1`, `s_l2`, `s_l3` | `hypot` | `p_l1`, `q_l1`, `p_l2`, `q_l2`, `p_l3`, `q_l3` | = √(a² + b²) |
| `pf_l1`, `pf_l2`, `pf_l3` | `pf_from_p_s` | `p_l1`, `s_l1`, `p_l2`, `s_l2`, `p_l3`, `s_l3` | = P / S; przy S=0 → 1.0 |
| `imp_energy_l1`, `imp_energy_l2`, `imp_energy_l3` | `split_equal` | `imp_energy_total` | = `imp_energy_total` / 3 |
| `exp_energy_l1`, `exp_energy_l2`, `exp_energy_l3` | `split_equal` | `exp_energy_total` | = `exp_energy_total` / 3 |
| `freq` | `constant` | – | = 50 (stała) |

<!-- END GENERATED: meter-derive -->

Licznik podaje moc czynną **per faza**, więc `ratio_split` rozdziela moc bierną
proporcjonalnie do udziału fazy w mocy czynnej — bliżej rzeczywistego
niesymetrycznego obciążenia niż równy podział. Nocą, gdy wszystkie `p_l*` = 0,
proporcje nie niosą informacji i reguła spada na podział równy.

---

## 4. Model kanoniczny — pokrycie obu wariantów

Wszystkie punkty, do których odwołuje się którakolwiek mapa wyjściowa przez
`from`. **Oba warianty pokrywają komplet** — różnią się tylko tym, ile pochodzi
wprost z rejestru, a ile z reguły.

<!-- BEGIN GENERATED: coverage -->

| Punkt kanoniczny | Jedn. | `huawei_plant_source` | `huawei_meter_source` |
|---|---|---|---|
| `active_energy_total` | kWh | `compute_derived` | `compute_derived` |
| `exp_energy_l1` | kWh | `split_equal` | `split_equal` |
| `exp_energy_l2` | kWh | `split_equal` | `split_equal` |
| `exp_energy_l3` | kWh | `split_equal` | `split_equal` |
| `exp_energy_total` | kWh | rejestr 40560 | rejestr 32349 |
| `freq` | Hz | `constant` | `constant` |
| `i_l1` | A | rejestr 40572 | rejestr 32272 |
| `i_l2` | A | rejestr 40573 | rejestr 32274 |
| `i_l3` | A | rejestr 40574 | rejestr 32276 |
| `imp_energy_l1` | kWh | `constant` | `split_equal` |
| `imp_energy_l2` | kWh | `constant` | `split_equal` |
| `imp_energy_l3` | kWh | `constant` | `split_equal` |
| `imp_energy_total` | kWh | `constant` | rejestr 32357 |
| `net_exp_energy_total` | kWh | `compute_derived` | `compute_derived` |
| `net_imp_energy_total` | kWh | `compute_derived` | `compute_derived` |
| `p_l1` | W | `split_equal` | rejestr 32335 |
| `p_l2` | W | `split_equal` | rejestr 32337 |
| `p_l3` | W | `split_equal` | rejestr 32339 |
| `p_total` | W | rejestr 40525 | rejestr 32278 |
| `pf_l1` | - | `copy` | `pf_from_p_s` |
| `pf_l2` | - | `copy` | `pf_from_p_s` |
| `pf_l3` | - | `copy` | `pf_from_p_s` |
| `pf_total` | - | rejestr 40532 | rejestr 32284 |
| `q_l1` | var | `split_equal` | `ratio_split` |
| `q_l2` | var | `split_equal` | `ratio_split` |
| `q_l3` | var | `split_equal` | `ratio_split` |
| `q_total` | var | rejestr 40544 | rejestr 32280 |
| `reactive_exp_energy_total` | kvarh | `constant` | rejestr 32353 |
| `reactive_imp_energy_total` | kvarh | `constant` | rejestr 32361 |
| `s_l1` | VA | `split_equal` | `hypot` |
| `s_l2` | VA | `split_equal` | `hypot` |
| `s_l3` | VA | `split_equal` | `hypot` |
| `s_total` | VA | `hypot` | rejestr 32287 |
| `u_l1` | V | `phase_from_line` | rejestr 32260 |
| `u_l12` | V | rejestr 40575 | rejestr 32266 |
| `u_l2` | V | `phase_from_line` | rejestr 32262 |
| `u_l23` | V | rejestr 40576 | rejestr 32268 |
| `u_l3` | V | `phase_from_line` | rejestr 32264 |
| `u_l31` | V | rejestr 40577 | rejestr 32270 |

<!-- END GENERATED: coverage -->

Trzy aliasy energii liczy `canonical.compute_derived`, wspólne dla wszystkich
źródeł:

```text
active_energy_total   = imp_energy_total + exp_energy_total
net_imp_energy_total  = imp_energy_total
net_exp_energy_total  = exp_energy_total
```

---

## 5. Model kanoniczny → rejestry DTSU666

Od tego miejsca **mostek SmartLoggera i mostek ND45 są identyczne** — obie
emulują ten sam licznik i dzielą te same mapy wyjściowe z `registers.json`.
Różnicuje je wyłącznie przekładnia CT z **własnego** `dtsu.identity.ir_at`
(dla mostka B domyślnie 200), przekazywana do `update_datastore` w czasie
działania.

Kodowanie: `register_float = (SI × sign × scale) + offset`, poprzedzone
dzieleniem przez CT dla pozycji z „/CT". Każdy punkt to **float32 = 2 rejestry**,
kolejność słów/bajtów big/big (ABCD).

### 5.1. FC03 — klasyczna mapa DTSU666 (strona wtórna, po CT)

<!-- BEGIN GENERATED: fc03-measurements -->

| Adres | Hex | `from` (punkt kanoniczny) | `scale` | /CT | Uwagi |
|---:|---|---|---:|:--:|---|
| 4096 | 0x1000 | `active_energy_total` | ×1 | ✓ | tylko starsze słowo IEEE754, kWh |
| 4106 | 0x100A | `reactive_exp_energy_total` | ×1 | ✓ | tylko starsze słowo IEEE754, kvarh |
| 4116 | 0x1014 | `reactive_imp_energy_total` | ×1 | ✓ | tylko starsze słowo IEEE754, kvarh |
| 4126 | 0x101E | `imp_energy_total` | ×1 | ✓ | kWh |
| 4128 | 0x1020 | `imp_energy_l1` | ×1 | ✓ | kWh |
| 4130 | 0x1022 | `imp_energy_l2` | ×1 | ✓ | kWh |
| 4132 | 0x1024 | `imp_energy_l3` | ×1 | ✓ | kWh |
| 4134 | 0x1026 | `net_imp_energy_total` | ×1 | ✓ | kWh |
| 4136 | 0x1028 | `exp_energy_total` | ×1 | ✓ | kWh |
| 4138 | 0x102A | `exp_energy_l1` | ×1 | ✓ | kWh |
| 4140 | 0x102C | `exp_energy_l2` | ×1 | ✓ | kWh |
| 4142 | 0x102E | `exp_energy_l3` | ×1 | ✓ | kWh |
| 4144 | 0x1030 | `net_exp_energy_total` | ×1 | ✓ | kWh |
| 4156 | 0x103C | `reactive_imp_energy_total` | ×1 | ✓ | tylko starsze słowo IEEE754, kvarh |
| 4176 | 0x1050 | `reactive_exp_energy_total` | ×1 | ✓ | tylko starsze słowo IEEE754, kvarh |
| 8192 | 0x2000 | `u_l12` | ×10 | – | V |
| 8194 | 0x2002 | `u_l23` | ×10 | – | V |
| 8196 | 0x2004 | `u_l31` | ×10 | – | V |
| 8198 | 0x2006 | `u_l1` | ×10 | – | V |
| 8200 | 0x2008 | `u_l2` | ×10 | – | V |
| 8202 | 0x200A | `u_l3` | ×10 | – | V |
| 8204 | 0x200C | `i_l1` | ×1000 | ✓ | A |
| 8206 | 0x200E | `i_l2` | ×1000 | ✓ | A |
| 8208 | 0x2010 | `i_l3` | ×1000 | ✓ | A |
| 8210 | 0x2012 | `p_total` | ×10 | ✓ | W |
| 8212 | 0x2014 | `p_l1` | ×10 | ✓ | W |
| 8214 | 0x2016 | `p_l2` | ×10 | ✓ | W |
| 8216 | 0x2018 | `p_l3` | ×10 | ✓ | W |
| 8218 | 0x201A | `q_total` | ×10 | ✓ | var |
| 8220 | 0x201C | `q_l1` | ×10 | ✓ | var |
| 8222 | 0x201E | `q_l2` | ×10 | ✓ | var |
| 8224 | 0x2020 | `q_l3` | ×10 | ✓ | var |
| 8226 | 0x2022 | `s_total` | ×10 | ✓ | VA |
| 8228 | 0x2024 | `s_l1` | ×10 | ✓ | VA |
| 8230 | 0x2026 | `s_l2` | ×10 | ✓ | VA |
| 8232 | 0x2028 | `s_l3` | ×10 | ✓ | VA |
| 8234 | 0x202A | `pf_total` | ×1000 | – | - |
| 8236 | 0x202C | `pf_l1` | ×1000 | – | - |
| 8238 | 0x202E | `pf_l2` | ×1000 | – | - |
| 8240 | 0x2030 | `pf_l3` | ×1000 | – | - |
| 8260 | 0x2044 | `freq` | ×100 | – | Hz |

<!-- END GENERATED: fc03-measurements -->

### 5.2. FC04 — mapa OEM Sigen, pomiary (baza `0x150A`)

Strona **pierwotna**, bez dzielenia przez CT. U/I/PF/Freq w SI (×1);
**moc w kW/kvar/kVA** (×0.001). To ta mapa, którą Sigenergy czyta w normalnej pracy.

<!-- BEGIN GENERATED: fc04-measurements -->

| Adres | Hex | `from` (punkt kanoniczny) | `scale` | /CT | Uwagi |
|---:|---|---|---:|:--:|---|
| 5386 | 0x150A | `u_l12` | ×1 | – | V |
| 5388 | 0x150C | `u_l23` | ×1 | – | V |
| 5390 | 0x150E | `u_l31` | ×1 | – | V |
| 5392 | 0x1510 | `u_l1` | ×1 | – | V |
| 5394 | 0x1512 | `u_l2` | ×1 | – | V |
| 5396 | 0x1514 | `u_l3` | ×1 | – | V |
| 5398 | 0x1516 | `i_l1` | ×1 | – | A |
| 5400 | 0x1518 | `i_l2` | ×1 | – | A |
| 5402 | 0x151A | `i_l3` | ×1 | – | A |
| 5404 | 0x151C | `p_total` | ×0.001 | – | W |
| 5406 | 0x151E | `p_l1` | ×0.001 | – | W |
| 5408 | 0x1520 | `p_l2` | ×0.001 | – | W |
| 5410 | 0x1522 | `p_l3` | ×0.001 | – | W |
| 5412 | 0x1524 | `q_total` | ×0.001 | – | var |
| 5414 | 0x1526 | `q_l1` | ×0.001 | – | var |
| 5416 | 0x1528 | `q_l2` | ×0.001 | – | var |
| 5418 | 0x152A | `q_l3` | ×0.001 | – | var |
| 5420 | 0x152C | `s_total` | ×0.001 | – | VA |
| 5422 | 0x152E | `s_l1` | ×0.001 | – | VA |
| 5424 | 0x1530 | `s_l2` | ×0.001 | – | VA |
| 5426 | 0x1532 | `s_l3` | ×0.001 | – | VA |
| 5428 | 0x1534 | `pf_total` | ×1 | – | - |
| 5430 | 0x1536 | `pf_l1` | ×1 | – | - |
| 5432 | 0x1538 | `pf_l2` | ×1 | – | - |
| 5434 | 0x153A | `pf_l3` | ×1 | – | - |
| 5454 | 0x154E | `freq` | ×1 | – | Hz |

<!-- END GENERATED: fc04-measurements -->

### 5.3. FC04 — mapa OEM Sigen, energia (baza `0x1800`)

<!-- BEGIN GENERATED: fc04-energy -->

| Adres | Hex | `from` (punkt kanoniczny) | `scale` | /CT | Uwagi |
|---:|---|---|---:|:--:|---|
| 6144 | 0x1800 | `active_energy_total` | ×1 | – | tylko starsze słowo IEEE754, kWh |
| 6154 | 0x180A | `reactive_exp_energy_total` | ×1 | – | tylko starsze słowo IEEE754, kvarh |
| 6164 | 0x1814 | `reactive_imp_energy_total` | ×1 | – | tylko starsze słowo IEEE754, kvarh |
| 6174 | 0x181E | `imp_energy_total` | ×1 | – | kWh |
| 6176 | 0x1820 | `imp_energy_l1` | ×1 | – | kWh |
| 6178 | 0x1822 | `imp_energy_l2` | ×1 | – | kWh |
| 6180 | 0x1824 | `imp_energy_l3` | ×1 | – | kWh |
| 6182 | 0x1826 | `net_imp_energy_total` | ×1 | – | kWh |
| 6184 | 0x1828 | `exp_energy_total` | ×1 | – | kWh |
| 6186 | 0x182A | `exp_energy_l1` | ×1 | – | kWh |
| 6188 | 0x182C | `exp_energy_l2` | ×1 | – | kWh |
| 6190 | 0x182E | `exp_energy_l3` | ×1 | – | kWh |
| 6192 | 0x1830 | `net_exp_energy_total` | ×1 | – | kWh |
| 6204 | 0x183C | `reactive_imp_energy_total` | ×1 | – | tylko starsze słowo IEEE754, kvarh |
| 6224 | 0x1850 | `reactive_exp_energy_total` | ×1 | – | tylko starsze słowo IEEE754, kvarh |

<!-- END GENERATED: fc04-energy -->

### 5.4. FC03 — tożsamość i blok konfiguracyjny

Wspólne z mostkiem ND45, wartości z `dtsu.identity` danego mostka:

| Adres | Hex | Zawartość | Źródło |
|---:|---|---|---|
| 61696 | 0xF100 | `Sigen Sensor TPX-CH` (ASCII, 20 rejestrów) | `dtsu_sigen_identity` |
| 61716 | 0xF114 | `0x00001500` (handshake, uint32) | `dtsu_sigen_identity` |
| 0–70 | 0x0000–0x0046 | blok konfiguracyjny (`rev`, `ucode`, `ir_at`, `ur_at`, …) | `dtsu.identity` mostka |

Pełny opis bloku konfiguracyjnego: [`register-map.md`](register-map.md), sekcja 5.

---

## 6. Przykład liczbowy — od rejestru do Sigenergy

Farma oddaje **1,2345 MW**, wariant plant, `ir_at = 200`.

| Krok | Wartość |
|---|---|
| SmartLogger rejestr 40525 (I32, Gain 1000, kW) | `1234500` |
| Dekodowanie: `raw × scale × sign` = `1234500 × 1 × (−1)` | `p_total = −1234500.0` W |
| `derive split_equal` | `p_l1 = p_l2 = p_l3 = −411500.0` W |
| FC04 `0x151C`: `SI × 0.001` | `−1234.5` (kW) |
| FC03 `0x2012`: `(SI / 200) × 10` | `−61725.0` (W×10, strona wtórna) |

Sprawdzenie odwrotne po stronie Sigenergy dla FC03:
`−61725 / 10 × 200 = −1 234 500 W` ✓

Napięcie w tym samym przebiegu:

| Krok | Wartość |
|---|---|
| Rejestr 40575 Uab (U16, Gain 10, V) | `4001` |
| `raw × 0.1` | `u_l12 = 400.1` V |
| `derive phase_from_line` | `u_l1 = 400.1 / √3 = 231.0` V |
| FC03 `0x2006`: `SI × 10` | `2310.0` (V×10) |
| FC04 `0x1510`: `SI × 1` | `231.0` (V) |

---

## 7. Konfiguracja mostka

```json
{
  "name": "smartlogger",
  "enabled": true,
  "source": {
    "type": "huawei",
    "host": "192.168.22.120",
    "port": 502,
    "unit_id": 0,
    "register_map": "huawei_plant_source",
    "poll_interval_s": 5.0,
    "timeout_s": 6.0,
    "stall_timeout_s": 60.0
  },
  "dtsu": {
    "transport": "rtu",
    "slave_id": 10,
    "identity": {"rev": 103, "ucode": 701, "ir_at": 200, "ur_at": 10},
    "rtu": {"port": "/dev/ttyAMA4", "baudrate": 9600, "parity": "N", "stopbits": 1}
  },
  "safety": {"max_data_age_s": 30.0, "check_interval_s": 0.5}
}
```

Parametry, które wynikają wprost z właściwości SmartLoggera:

| Parametr | Wartość | Dlaczego |
|---|---:|---|
| `poll_interval_s` | 5.0 | koncentrator agreguje dane z falowników po RS485 — szybciej nie ma sensu |
| `timeout_s` | 6.0 | dokumentacja Huawei (4.2.4) dopuszcza 5 s timeout Modbus |
| `max_data_age_s` | 30.0 | musi być ≥ 2 × `poll_interval_s`, inaczej mostek siedzi w permanentnym fail-safe; walidator odrzuca mniejsze |
| `stall_timeout_s` | 60.0 | po tylu sekundach bez postępu poller jest odbudowywany w procesie |
| `address_offset` (w mapie) | 0 | knob na 0‑ vs 1‑based numerację rejestrów, patrz niżej |

---

## 8. Do potwierdzenia przy rozruchu

Rzeczy, których nie da się sprawdzić testami:

1. **Baza adresowa.** Huawei dokumentuje „40525"; część klientów Modbus wymaga
   `40524` (0‑ vs 1‑based). Sprawdź `mbpoll` **przed** włączeniem mostka:
   ```bash
   mbpoll -m tcp -a 0 -t 4 -r 40525 -c 2 192.168.22.120
   ```
   Jeśli trzeba skorygować, ustaw `address_offset: -1` w sekcji
   `huawei_plant_source` w `config/registers.json` — bez zmiany kodu. Offset
   przesuwa **jednocześnie** bazy bloków i adresy punktów, więc pokrycie się nie
   zmienia.
2. **Adres RS485 licznika** (tylko wariant B) — do odczytania z LCD/WebUI
   SmartLoggera; wpisać w `source.unit_id`.
3. **Znak `p_total`.** Mapa odwraca znak (`sign: -1`), zakładając, że rejestr
   40525 jest **dodatni przy produkcji**. Potwierdź to na obiekcie: jeśli
   SmartLogger sam raportuje produkcję ujemnie, `sign` wraca do `1` — jedna
   wartość w `config/registers.json`, bez zmiany kodu.
4. **Przekładnia CT mostka B** (`ir_at`). Skopiowana z mostka A; przy 1,2 MW
   sprawdź, czy Sigenergy na tej szynie czyta sensowne wartości na mapie FC03
   (dzielonej przez `ir_at`).
5. **Realne tempo odświeżania.** Obserwuj
   `nd45_dtsu666_bridge_data_age_seconds{bridge="smartlogger"}`. Jeśli regularnie
   przekracza 30 s, podnieś `max_data_age_s` **tego** mostka — nie mostka ND45.
6. **Rozdzielczość prądów** w wariancie plant (Gain 1 = krok 1 A).

Podgląd na żywo: `python -m nd45_dtsu666 monitor_hsm` pokazuje stan łącza do
SmartLoggera, wiek danych, zdekodowane wartości oraz to, czy Sigenergy faktycznie
odpytuje port `/dev/ttyAMA4`.
