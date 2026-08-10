# How often Sigenergy actually reads each register

Field measurement, 2026-08-08: a Saleae Logic capture of the RS-485 bus for one
bridge (ND45 source, `source.poll_interval_s: 0.1`), 60 seconds, decoded with
Saleae's Modbus RTU analyzer. This records what was found, mapped onto the
canonical points in `config/registers.json`, with an explicit confidence level
per finding -- the raw decode is unreliable for most of the capture (see
"Method and its limits" below), so some of this is solid and some is a
plausible read of noise.

## Method and its limits

Saleae's Modbus HLA analyzer, tapping a single half-duplex RS-485 line, cannot
tell a **request** apart from a **response** -- it has no way to know which
direction a given frame travelled. When it applies the read-request template to
a slave *response* (a different byte layout: byte-count + N bytes of register
data, no `StartAddr`/`Qty` fields), it decodes garbage, computes a checksum
over the wrong byte range, and reports `Invalid Checksum!`. Once that happens
it loses frame-boundary tracking entirely and free-wheels: in this capture it
spent 23,606 of 23,708 exported rows (99.6%) inside a single, phantom
`Write File Record` transaction whose own declared `RecordLen` (1056
registers) does not fit inside its own declared `ByteCount` (28 bytes) --
internally impossible, i.e. definitely not a real frame.

The underlying byte capture is still intact; only the analyzer's frame-level
interpretation is broken. Real requests were recovered by pattern-matching the
raw 16-bit words the "Data" rows expose: a genuine FC03/FC04 read request is
exactly 4 words (`slave+FC`, `start_addr`, `qty`, `crc`), and reconstructing
one this way and decoding the FC04 power response that follows produces
float32 values where `p_total == p_l1+p_l2+p_l3` and `q_total == q_l1+q_l2+q_l3`
hold to within rounding for 488 of 491 attempts -- strong evidence the
reconstruction is correct where it applies. Slower, rarer requests are
recovered the same way but with far fewer samples (3-8 occurrences in 60s), so
their timing is a rough estimate, and several 4-word-shaped hits don't
correspond to any address this project's register maps actually define --
those are flagged as likely noise, not real traffic, below.

## High confidence: the fast power poll

```
slave 0x0A (10), FC 04, addr 0x151C (5404), qty 16
```

This is `dtsu_sigen_ext_target` (`config/registers.json`), addresses 5404-5419
inclusive -- covers exactly:

| canonical point | addr |
|---|---|
| `p_total` | 0x151C (5404) |
| `p_l1` | 0x151E (5406) |
| `p_l2` | 0x1520 (5408) |
| `p_l3` | 0x1522 (5410) |
| `q_total` | 0x1524 (5412) |
| `q_l1` | 0x1526 (5414) |
| `q_l2` | 0x1528 (5416) |
| `q_l3` | 0x152A (5418) |

**491 occurrences in 60s, median gap 113 ms (~8.85 Hz).** Consistent with the
`~9.5 reads/s` on `fc=4, addr=5404, count=16` already documented in
`CLAUDE.md` from an independent 2026-08-04 measurement (`RtuActivity`/
`WireActivity` read tracker, not a packet capture) -- two different
measurement methods, two different sessions, agreeing within ~10%.

`s_total`/`s_l1..3` (0x152C-0x1532), `pf_total`/`pf_l1..3` (0x1534-0x153A) and
`freq` (0x154E) are **not** part of this fast block -- they sit outside the
16-register window this read covers.

## Medium confidence: a slower housekeeping round, roughly every ~5.3-5.4s

The fast poll above pauses for ~0.48-0.51s at very regular intervals -- 12
such pauses in this capture, spaced 3.22s, 8.69s, 14.04s, 19.39s, 24.85s,
30.20s, 35.58s, 40.94s, 46.34s, 51.69s, 57.04s (gaps of 4.7-5.5s, mean ~5.36s).
Inside each pause, a small group of different reads appears:

| slave | FC | addr | qty | canonical mapping | confidence |
|---|---|---|---|---|---|
| 0x0A | 03 | 0xF114 (61716) | 2 | `handshake_magic`, `dtsu_sigen_identity` | **high** -- address and function code (3) both match exactly |
| 0x0A | 04 | 0x180A (6154) | 22 | `dtsu_sigen_ext_energy`, starting at `reactive_exp_energy_coarse` | medium -- address matches a real point, but a 22-register span is unverified against which points it actually terminates on |
| 0x0A | 04 | 0x1528 (5416) | 14 | falls on `q_l2`'s own address in `dtsu_sigen_ext_target` | **low -- likely noise**: a 14-register read starting mid-block doesn't correspond to anything sensible; more likely a coincidental 4-word match inside the decoder's garbage stream |
| 0x0A | 03 | 0x0003, 0x0A00, 0x0400 | 5, 0, 21 | no match to any address in `config/registers.json` | **low -- likely noise or an unidentified probe** |

Interpretation, held loosely: Sigenergy appears to refresh identity/handshake
and an energy block on a slower cadence than the power block, which is a very
ordinary design (fast-changing values polled fast, slow-changing metadata
polled slow). The three unmatched FC03 reads and the implausible `q_l2`/qty=14
entry are most likely decoder artifacts (see "Method and its limits") rather
than genuine additional traffic, and are listed here only so a future
measurement can either confirm or rule them out.

## Low confidence: an even rarer round, roughly every 20-30s

Only 3 occurrences in 60s -- too few to fix the exact period, but each one
maps to a real address:

| slave | FC | addr | qty | canonical mapping |
|---|---|---|---|---|
| 0x0A | 04 | 0x150A (5386) | 30 | `dtsu_sigen_ext_target`, starting at `u_l12` -- 30 registers spans `u_l12` through `q_l3` (5386-5417), i.e. most of the fast block re-read in one wider sweep |
| 0x0A | 04 | 0x154E (5454) | 2 | `freq`, `dtsu_sigen_ext_target` -- a lone frequency read, which sits outside every other observed block |
| 0x0A | 04 | 0x1826 (6182) | 12 | `net_imp_ep`, `dtsu_sigen_ext_energy` |
| 0x0A | 03 | 0x0046 (70), 0x0200 (512) | 1, 29 | no match to any defined address -- likely noise |

Read as: an occasional wider re-sweep of the power block (picking up `freq`,
which the fast 16-register poll never touches) plus another energy-block
touch. Three samples is not enough to trust the ~20-30s figure; treat it as
"rare, on the order of tens of seconds" rather than a fixed period.

## Never observed in this capture

`dtsu_target` (the classic FC03 map, `0x2000`/`0x101E`) -- zero occurrences,
consistent with the existing `CLAUDE.md` finding that Sigenergy "never reads
`dtsu_target` at all." `model_string` (`dtsu_sigen_identity`, `0xF100`) was
also not observed, despite `handshake_magic` two registers away being read
regularly -- absence of evidence, not evidence of absence, given how much of
this capture is undecodable.

## What this doesn't answer

This single 60-second window cannot distinguish a genuinely fixed Sigenergy
polling schedule from something that varies by firmware state, load, or time
since connect. It also cannot rule out the low-confidence entries being real
traffic on addresses this project doesn't otherwise care about. Treat the high-
and medium-confidence rows as usable; treat everything marked low-confidence as
"worth a second, cleaner capture before relying on it" -- ideally with the
Modbus analyzer given separate master/slave channels so it can't lose frame
sync the way it did here.
