# Mapa rejestrów konwertera ND45 → DTSU666 (dla Sigenergy)

Ten dokument opisuje **wszystkie** rejestry, które konwerter wystawia jako
licznik DTSU666 „Sigen Sensor TPX-CH", wraz z adresami (dec/hex), opisem,
mnożnikami i obsługą przekładni CT. Zweryfikowano względem zrzutów prawdziwego
licznika (`scan_COM8_20260723_185011.txt`, `..._190151.txt`, slave 10, 9600 8N1).

Konwerter obsługuje **dwie niezależne przestrzenie adresowe** na tym samym
slave ID:

- **FC03** (Holding Registers) — klasyczna mapa DTSU666 (`0x2000`/`0x101E`) +
  blok konfiguracyjny/tożsamości (`0x0000`–`0x0046`, `0xF100`–`0xF115`).
- **FC04** (Input Registers) — mapa OEM Sigen: pomiary `0x150A` + energia
  `0x181E`.

> Sigenergy w normalnej pracy czyta **pomiary i energię przez FC04**, a przez
> FC03 tylko blok konfiguracyjny (`0x0003`/qty5), `0x0046` i handshake
> (`0xF114`). Klasyczny blok FC03 `0x2000`/`0x101E` jest utrzymywany dla
> zgodności z narzędziami (mbpoll) i dla wierności emulacji.

## Model kanoniczny (SI)

Poller ND45 dekoduje surowe rejestry do wartości w jednostkach SI (strona
**pierwotna** — ND45 sam stosuje swoją przekładnię CT). To jedyne źródło prawdy;
obie mapy wyjściowe kodują z niego.

| Klucz | Jednostka | Pochodzenie |
|---|---|---|
| `u_l1/l2/l3`, `u_l12/l23/l31` | V | odczyt ND45 |
| `i_l1/l2/l3` | A | odczyt ND45 |
| `p_l1/l2/l3`, `p_total` | W | odczyt ND45 |
| `q_l1/l2/l3`, `q_total` | var | odczyt ND45 |
| `pf_l1/l2/l3`, `pf_total` | – | odczyt ND45 |
| `freq` | Hz | odczyt ND45 |
| `imp_energy_*`, `exp_energy_*` | kWh | odczyt ND45 (compose hi/lo) |
| `reactive_imp_energy_total` | kvarh | suma importowanych (`Q+`) par ND45: `944/946` i `976/978` |
| `reactive_exp_energy_total` | kvarh | suma eksportowanych (`Q-`) par ND45: `960/962` i `992/994` |
| `s_l1/l2/l3`, `s_total` | VA | odczyt ND45: `60/84/108/132` (`float32`); suma z `132` |
| `active_energy_total` | kWh | **wyliczane**: `imp_energy_total + exp_energy_total` |
| `net_imp_energy_total` | kWh | **wyliczane**: kopia `imp_energy_total` |
| `net_exp_energy_total` | kWh | **wyliczane**: kopia `exp_energy_total` |

Each reactive component uses `Mvarh * 1000 + kvarh`; the exact directional
formulas are:

```text
reactive_imp_energy_total =
    ND45[944] * 1000 + ND45[946]
  + ND45[976] * 1000 + ND45[978]

reactive_exp_energy_total =
    ND45[960] * 1000 + ND45[962]
  + ND45[992] * 1000 + ND45[994]
```

`nd45_poller.compute_derived()` derives `active_energy_total` and the
`net_*` fields. The `net_*` fields are directional copies, not arithmetic
import-minus-export values. In `static` mode, omitted `s_l1/l2/l3` are
calculated as `|U·I|`, omitted `s_total` is their sum, and explicitly
configured `s_*` values are not overwritten.

## Przekładnia CT (`dtsu.identity.ir_at`, tu = 200)

- Mapa **klasyczna FC03** jest po **stronie wtórnej** → prąd, moc (P/Q/S) i
  energia są **dzielone przez CT** przed skalowaniem (`divide_by_ct: true`).
- Mapa **Sigen FC04** jest po **stronie pierwotnej** → **bez** dzielenia.
- Napięcie, PF, częstotliwość **nie** przechodzą przez CT (nigdy nie dzielone).

Wzory kodera (`codec.encode_point`):

```
register_float = (SI [/ CT gdy divide_by_ct]) · sign · scale + offset
```

---

## 1. FC03 — klasyczna mapa DTSU666, pomiary (baza `0x2000`)

Strona wtórna. `raw = (SI/CT)·scale` dla pozycji z „/CT".

| Adres | Hex | Wielkość | `from` | Skala | /CT | Jednostka rejestru |
|---:|---|---|---|---:|:--:|---|
| 8192 | 0x2000 | Uab | u_l12 | ×10 | – | V×10 |
| 8194 | 0x2002 | Ubc | u_l23 | ×10 | – | V×10 |
| 8196 | 0x2004 | Uca | u_l31 | ×10 | – | V×10 |
| 8198 | 0x2006 | Ua | u_l1 | ×10 | – | V×10 |
| 8200 | 0x2008 | Ub | u_l2 | ×10 | – | V×10 |
| 8202 | 0x200A | Uc | u_l3 | ×10 | – | V×10 |
| 8204 | 0x200C | Ia | i_l1 | ×1000 | ✓ | A×1000 (wtórne) |
| 8206 | 0x200E | Ib | i_l2 | ×1000 | ✓ | A×1000 |
| 8208 | 0x2010 | Ic | i_l3 | ×1000 | ✓ | A×1000 |
| 8210 | 0x2012 | Pt | p_total | ×10 | ✓ | W×10 (wtórne) |
| 8212 | 0x2014 | Pa | p_l1 | ×10 | ✓ | W×10 |
| 8214 | 0x2016 | Pb | p_l2 | ×10 | ✓ | W×10 |
| 8216 | 0x2018 | Pc | p_l3 | ×10 | ✓ | W×10 |
| 8218 | 0x201A | Qt | q_total | ×10 | ✓ | var×10 |
| 8220 | 0x201C | Qa | q_l1 | ×10 | ✓ | var×10 |
| 8222 | 0x201E | Qb | q_l2 | ×10 | ✓ | var×10 |
| 8224 | 0x2020 | Qc | q_l3 | ×10 | ✓ | var×10 |
| 8226 | 0x2022 | St | s_total | ×10 | ✓ | VA×10 |
| 8228 | 0x2024 | Sa | s_l1 | ×10 | ✓ | VA×10 |
| 8230 | 0x2026 | Sb | s_l2 | ×10 | ✓ | VA×10 |
| 8232 | 0x2028 | Sc | s_l3 | ×10 | ✓ | VA×10 |
| 8234 | 0x202A | PFt | pf_total | ×1000 | – | PF×1000 |
| 8236 | 0x202C | PFa | pf_l1 | ×1000 | – | PF×1000 |
| 8238 | 0x202E | PFb | pf_l2 | ×1000 | – | PF×1000 |
| 8240 | 0x2030 | PFc | pf_l3 | ×1000 | – | PF×1000 |
| 8260 | 0x2044 | Freq | freq | ×100 | – | Hz×100 |

## 2. FC03 — klasyczna mapa DTSU666, energia i aliasy

Strona wtórna: energia czynna w kWh, aliasy energii biernej w kvarh;
`raw = SI/CT` (×1).

| Adres | Hex | Wielkość | `from` | /CT |
|---:|---|---|---|:--:|
| 4096 | 0x1000 | Combined active energy coarse | active_energy_total | ✓ |
| 4106 | 0x100A | Exported reactive energy coarse (Q-) | reactive_exp_energy_total | ✓ |
| 4116 | 0x1014 | Imported reactive energy coarse (Q+) | reactive_imp_energy_total | ✓ |
| 4126 | 0x101E | ImpEp total | imp_energy_total | ✓ |
| 4128 | 0x1020 | ImpEp L1 | imp_energy_l1 | ✓ |
| 4130 | 0x1022 | ImpEp L2 | imp_energy_l2 | ✓ |
| 4132 | 0x1024 | ImpEp L3 | imp_energy_l3 | ✓ |
| 4134 | 0x1026 | NetImpEp | net_imp_energy_total | ✓ |
| 4136 | 0x1028 | ExpEp total | exp_energy_total | ✓ |
| 4138 | 0x102A | ExpEp L1 | exp_energy_l1 | ✓ |
| 4140 | 0x102C | ExpEp L2 | exp_energy_l2 | ✓ |
| 4142 | 0x102E | ExpEp L3 | exp_energy_l3 | ✓ |
| 4144 | 0x1030 | NetExpEp | net_exp_energy_total | ✓ |
| 4156 | 0x103C | Imported reactive energy coarse (Q+ alias) | reactive_imp_energy_total | ✓ |
| 4176 | 0x1050 | Exported reactive energy coarse (Q- alias) | reactive_exp_energy_total | ✓ |

## 3. FC04 — mapa OEM Sigen, pomiary (baza `0x150A`, offset −0x0AF6 vs FC03)

Strona **pierwotna**, bez CT. U/I/PF/Freq w SI (×1); **moc w kW/kvar/kVA (×0.001)**.
Bloki czytane przez Sigenergy: `0x150A`/qty30, `0x151C`/qty16 (szybka pętla ~60 ms),
`0x1528`/qty14, `0x154E`/qty2.

| Adres | Hex | Wielkość | `from` | Skala | Jednostka |
|---:|---|---|---|---:|---|
| 5386 | 0x150A | Uab | u_l12 | ×1 | V |
| 5388 | 0x150C | Ubc | u_l23 | ×1 | V |
| 5390 | 0x150E | Uca | u_l31 | ×1 | V |
| 5392 | 0x1510 | Ua | u_l1 | ×1 | V |
| 5394 | 0x1512 | Ub | u_l2 | ×1 | V |
| 5396 | 0x1514 | Uc | u_l3 | ×1 | V |
| 5398 | 0x1516 | Ia | i_l1 | ×1 | A (pierwotne) |
| 5400 | 0x1518 | Ib | i_l2 | ×1 | A |
| 5402 | 0x151A | Ic | i_l3 | ×1 | A |
| 5404 | 0x151C | Pt | p_total | ×0.001 | kW |
| 5406 | 0x151E | Pa | p_l1 | ×0.001 | kW |
| 5408 | 0x1520 | Pb | p_l2 | ×0.001 | kW |
| 5410 | 0x1522 | Pc | p_l3 | ×0.001 | kW |
| 5412 | 0x1524 | Qt | q_total | ×0.001 | kvar |
| 5414 | 0x1526 | Qa | q_l1 | ×0.001 | kvar |
| 5416 | 0x1528 | Qb | q_l2 | ×0.001 | kvar |
| 5418 | 0x152A | Qc | q_l3 | ×0.001 | kvar |
| 5420 | 0x152C | St | s_total | ×0.001 | kVA |
| 5422 | 0x152E | Sa | s_l1 | ×0.001 | kVA |
| 5424 | 0x1530 | Sb | s_l2 | ×0.001 | kVA |
| 5426 | 0x1532 | Sc | s_l3 | ×0.001 | kVA |
| 5428 | 0x1534 | PFt | pf_total | ×1 | – |
| 5430 | 0x1536 | PFa | pf_l1 | ×1 | – |
| 5432 | 0x1538 | PFb | pf_l2 | ×1 | – |
| 5434 | 0x153A | PFc | pf_l3 | ×1 | – |
| 5454 | 0x154E | Freq | freq | ×1 | Hz |

## 4. FC04 — mapa OEM Sigen, energia

Strona **pierwotna**: energia czynna w kWh, aliasy energii biernej w kvarh,
scale 1. Sigenergy reads `0x180A`/qty22
(directional reactive coarse energy at `0x180A` and `0x1814`, a zero-filled gap
at `0x180C`-`0x1813` and `0x1816`-`0x181D`, and `imp_ep` at `0x181E`) and
`0x1828`/qty4 (total and phase active export).

| Adres | Hex | Wielkość | `from` |
|---:|---|---|---|
| 6144 | 0x1800 | Active energy coarse | active_energy_total |
| 6154 | 0x180A | Exported reactive energy coarse (Q-) | reactive_exp_energy_total |
| 6156-6163 | 0x180C-0x1813 | Unmapped polled gap (zero-filled) | unmapped |
| 6164 | 0x1814 | Imported reactive energy coarse (Q+) | reactive_imp_energy_total |
| 6166-6173 | 0x1816-0x181D | Unmapped polled gap (zero-filled) | unmapped |
| 6174 | 0x181E | ImpEp total | imp_energy_total |
| 6176 | 0x1820 | ImpEp L1 | imp_energy_l1 |
| 6178 | 0x1822 | ImpEp L2 | imp_energy_l2 |
| 6180 | 0x1824 | ImpEp L3 | imp_energy_l3 |
| 6182 | 0x1826 | NetImpEp | net_imp_energy_total |
| 6184 | 0x1828 | ExpEp total | exp_energy_total |
| 6186 | 0x182A | ExpEp L1 | exp_energy_l1 |
| 6188 | 0x182C | ExpEp L2 | exp_energy_l2 |
| 6190 | 0x182E | ExpEp L3 | exp_energy_l3 |
| 6192 | 0x1830 | NetExpEp | net_exp_energy_total |
| 6204 | 0x183C | Imported reactive energy coarse (Q+ alias) | reactive_imp_energy_total |
| 6224 | 0x1850 | Exported reactive energy coarse (Q- alias) | reactive_exp_energy_total |

The ten coarse fields (`0x1000`, `0x100A`, `0x1014`, `0x103C`, `0x1050`,
`0x1800`, `0x180A`, `0x1814`, `0x183C`, and `0x1850`) encode the IEEE754 high
word and force the low word to zero. `0x1000` and `0x1800` are combined active
energy (CT-side and primary-side respectively). The `0x100A`/`0x1050` and
`0x180A`/`0x1850` pairs are exported-reactive (`Q-`) aliases; the
`0x1014`/`0x103C` and `0x1814`/`0x183C` pairs are imported-reactive (`Q+`)
aliases.

## 5. FC03 — blok konfiguracyjny / tożsamości

Rejestry 1-słowowe (int16), poza stringiem ASCII i handshake. Zweryfikowane
co do bitu ze zrzutem prawdziwego licznika. Wartości edytowalne przez
`dtsu.identity` w `config/config.json` (oprócz stałych obserwowanych).

| Adres | Hex | Pole | Wartość | Źródło |
|---:|---|---|---:|---|
| 0 | 0x0000 | REV (firmware) | 103 | `identity.rev` |
| 1 | 0x0001 | UCode | 701 | `identity.ucode` |
| 2 | 0x0002 | CLr.E | 0 | `identity.clr_e` |
| 3 | 0x0003 | net (3P4W=0) | 0 | `identity.net` |
| 4 | 0x0004 | (obserw., nieudok.) | 1 | stała (w oknie odczytu Sigen) |
| 6 | 0x0006 | IrAt (przekładnia CT) | 200 | `identity.ir_at` |
| 7 | 0x0007 | UrAt (×0.1 → 1.0) | 10 | `identity.ur_at` |
| 8 | 0x0008 | (obserw., nieudok.) | 4 | stała |
| 10 | 0x000A | Disp | 10 | `identity.disp` |
| 11 | 0x000B | B.LCD | 1 | `identity.b_lcd` |
| 12 | 0x000C | Endian | 4 | `identity.endian` |
| 44 | 0x002C | Protocol | 0 | `identity.protocol` |
| 45 | 0x002D | bAud (3=9600) | 3 | `dtsu.rtu.baudrate` |
| 46 | 0x002E | Addr (slave) | 10 | `dtsu.slave_id` |
| 70 | 0x0046 | (obserw.) | 0 | stała (czytana przez Sigen ~5,4 s) |
| 61696 | 0xF100 | Model (ASCII, 20 rej.) | `"Sigen Sensor TPX-CH\0"` | `dtsu_sigen_identity` |
| 61716 | 0xF114 | Handshake (uint32) | `0x00001500` (5376) | `dtsu_sigen_identity` |

---

## Weryfikacja względem prawdziwego licznika

Podając wartości pierwotne odczytane ze zrzutu jako model kanoniczny, konwerter
odtwarza surowe rejestry z dokładnością do dryfu pomiarowego (bloki FC03/FC04
skanowane były kilka minut od siebie w trakcie 10-min skanu):

| Rejestr | Konwerter | Zrzut | |
|---|---:|---:|---|
| FC04 Pt (0x151C, kW) | 3.0195 | 3.0195 | ✓ |
| FC04 Qt (0x1524, kvar) | −1.2402 | −1.2402 | ✓ |
| FC04 St (0x152C, kVA) | 5.339 | 5.320 | ✓ (~0.4%) |
| FC04 imp_ep (0x181E, kWh) | 6.3828 | 6.3828 | ✓ |
| FC03 Pt (0x2012, /CT) | 150.98 | 151.26 | ✓ (dryf) |
| FC03 Ia (0x200C) | 35.04 (=I×5) | 36.06 | ✓ (dryf prądu) |
| FC03 Freq (0x2044, ×100) | 4982.7 | 5000.6 | ✓ (dryf) |
| blok konfig. + tożsamość | co do bitu | co do bitu | ✓ |

### Latest reverse-flow scan evidence

| Field | Value |
|---|---:|
| FC04 combined active coarse (`0x1800`) | 10.0 kWh |
| FC04 exported reactive coarse, Q- (`0x180A`) | 2.796875 kvarh |
| FC04 imported reactive coarse, Q+ (`0x1814`) | 1.1953125 kvarh |
| FC04 ImpEp (`0x181E`) | 7.0 kWh |
| FC04 ExpEp (`0x1828`) | 3.0 kWh |
| FC04 export alias (`0x1830`) | 3.0 kWh |
| FC04 L1/L2/L3 export (`0x182A`/`0x182C`/`0x182E`) | 0.796875 / 1.0 / 1.0 kWh |

## Znane luki (do domknięcia w terenie)

- **Phase-angle registers** `0x153C`-`0x1540` (~304) and `0x2032`-`0x2036`
  (~3040) remain deliberately outside this energy-correction scope and are unmapped.
