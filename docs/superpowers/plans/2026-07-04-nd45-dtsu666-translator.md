# ND45 → DTSU666 Modbus Translator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python bridge that polls a Lumel ND45 over Modbus TCP and serves the data as a DTSU666 power meter over Modbus RTU so a Sigenergy storage system can read it as its "Power Sensor".

**Architecture:** Single-process `asyncio` app. A poller task reads ND45 (TCP client), decodes float32 registers into a canonical SI model, and publishes to an in-memory store + a pymodbus RTU server datastore. The RTU server answers Sigenergy instantly from the datastore. A watchdog stops serving when data goes stale so Sigenergy sees a timeout (fail-safe).

**Tech Stack:** Python 3.10+, pymodbus (>=3.6,<3.7), pydantic v2, pytest + pytest-asyncio, ruff. Deployed as a systemd service on Seeed reComputer R1000 (Ubuntu).

## Global Constraints

- Python 3.10+ (CI matrix: 3.10, 3.11, 3.12). Target reComputer Ubuntu.
- `pymodbus>=3.6,<3.7` — pin the 3.6 API. Do NOT use `BinaryPayloadBuilder`/`BinaryPayloadDecoder` (removed in later pymodbus); use the `struct`-based codec in Task 2.
- `pydantic>=2,<3`.
- Modbus float32 word/byte order default **big/big (ABCD)** on BOTH sides.
- ND45 register addresses are **decimal**; DTSU666 addresses stored **decimal** (converted from hex).
- Transform semantics (verbatim from spec §7):
  - ND45 → canonical: `SI = (raw_float × scale × sign) + offset`.
  - canonical → DTSU register: `register_float = (SI × sign × scale) + offset`.
  - `sign ∈ {+1,-1}`; `scale` default 1; `offset` default 0.
- Line endings LF; ruff line length 100.
- All commits use Conventional Commits style (`feat:`, `test:`, `chore:`, `docs:`).

---

## File Structure

```
pyproject.toml                         # package + deps + tool config
README.md                              # setup, venv, mbpoll bench, on-site checklist
.github/workflows/ci.yml               # ruff + pytest matrix
config/config.json                     # runtime params (IP, serial, intervals)
config/registers.json                  # register maps + mapping (seed from PDFs)
systemd/nd45-dtsu666.service           # systemd unit
src/nd45_dtsu666/__init__.py
src/nd45_dtsu666/__main__.py           # argparse: run | diag | selftest
src/nd45_dtsu666/codec.py              # float32 <-> registers, scale/offset/sign, compose
src/nd45_dtsu666/config.py             # pydantic models + loaders
src/nd45_dtsu666/canonical.py          # CanonicalStore (values + timestamp + freshness)
src/nd45_dtsu666/nd45_poller.py        # async TCP client polling + decode
src/nd45_dtsu666/dtsu_server.py        # datastore build/update + RTU server + fail-safe
src/nd45_dtsu666/diagnostics.py        # diagnostic table renderer
src/nd45_dtsu666/app.py                # orchestration (wires everything for `run`)
tests/test_smoke.py
tests/test_codec.py
tests/test_config.py
tests/test_canonical.py
tests/test_poller.py
tests/test_server.py
tests/test_diagnostics.py
```

---

## Task 1: Project scaffolding + CI + smoke test

**Files:**
- Create: `pyproject.toml`, `src/nd45_dtsu666/__init__.py`, `tests/test_smoke.py`, `.github/workflows/ci.yml`, `README.md`

**Interfaces:**
- Produces: installable package `nd45_dtsu666`; `pytest` and `ruff` run green; CI on push/PR.

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "nd45_dtsu666"
version = "0.1.0"
description = "Modbus bridge: Lumel ND45 (TCP) -> DTSU666 (RTU) for Sigenergy"
requires-python = ">=3.10"
dependencies = [
    "pymodbus>=3.6,<3.7",
    "pydantic>=2,<3",
    "pyserial>=3.5",
]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.23", "ruff>=0.6"]

[project.scripts]
nd45-dtsu666 = "nd45_dtsu666.__main__:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.ruff]
line-length = 100
target-version = "py310"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 2: Create `src/nd45_dtsu666/__init__.py`**

```python
"""Modbus bridge: Lumel ND45 (TCP) -> DTSU666 (RTU) for Sigenergy."""

__version__ = "0.1.0"
```

- [ ] **Step 3: Write the smoke test `tests/test_smoke.py`**

```python
def test_package_imports():
    import nd45_dtsu666

    assert nd45_dtsu666.__version__ == "0.1.0"
```

- [ ] **Step 4: Create venv, install, run test + lint**

Run:
```bash
python -m venv .venv
. .venv/bin/activate    # Windows dev: .venv\Scripts\activate
pip install -e ".[dev]"
pytest -v
ruff check .
```
Expected: `test_package_imports PASSED`; ruff reports no errors.

- [ ] **Step 5: Create `.github/workflows/ci.yml`**

```yaml
name: CI
on:
  push:
  pull_request:
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.10", "3.11", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - run: pip install -e ".[dev]"
      - run: ruff check .
      - run: pytest -v
```

- [ ] **Step 6: Create `README.md` (skeleton — expanded in Task 10)**

```markdown
# nd45_dtsu666_converter

Temporary Modbus bridge: Lumel ND45 (Modbus TCP) → DTSU666 register map (Modbus RTU)
so a Sigenergy storage system can read it as a "Power Sensor".

See `docs/superpowers/specs/2026-07-04-nd45-dtsu666-translator-design.md` for the design.

## Quick start
```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
```
```

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml src tests .github README.md
git commit -m "chore: scaffold package, CI, smoke test"
```

---

## Task 2: Float32 codec (core translation primitive)

**Files:**
- Create: `src/nd45_dtsu666/codec.py`, `tests/test_codec.py`

**Interfaces:**
- Produces:
  - `registers_to_float(regs: list[int], word_order: str = "big", byte_order: str = "big") -> float`
  - `float_to_registers(value: float, word_order: str = "big", byte_order: str = "big") -> list[int]`
  - `decode_point(regs: list[int], scale: float, sign: int, offset: float, word_order: str, byte_order: str) -> float`
  - `encode_point(si: float, scale: float, sign: int, offset: float, word_order: str, byte_order: str) -> list[int]`
  - `compose(values: list[float], factors: list[float]) -> float`
  - `OVERRANGE: float = 1e20`

- [ ] **Step 1: Write failing tests `tests/test_codec.py`**

```python
import struct

import pytest

from nd45_dtsu666.codec import (
    compose,
    decode_point,
    encode_point,
    float_to_registers,
    registers_to_float,
)


def test_float_to_registers_big_big_known_value():
    # 230.0 -> IEEE754 big-endian = 0x43660000 -> [0x4366, 0x0000]
    assert float_to_registers(230.0, "big", "big") == [0x4366, 0x0000]


def test_registers_to_float_big_big_known_value():
    assert registers_to_float([0x4366, 0x0000], "big", "big") == pytest.approx(230.0)


def test_roundtrip_all_orders():
    for wo in ("big", "little"):
        for bo in ("big", "little"):
            regs = float_to_registers(123.456, wo, bo)
            assert registers_to_float(regs, wo, bo) == pytest.approx(123.456, rel=1e-6)


def test_word_swap_changes_bytes():
    assert float_to_registers(230.0, "big", "big") != float_to_registers(230.0, "little", "big")


def test_decode_point_applies_scale_sign_offset():
    regs = float_to_registers(100.0, "big", "big")
    # SI = raw * scale * sign + offset
    assert decode_point(regs, scale=2.0, sign=-1, offset=5.0, word_order="big", byte_order="big") == pytest.approx(-195.0)


def test_encode_point_applies_sign_scale_offset():
    # register_float = SI * sign * scale + offset ; DTSU voltage scale x10
    regs = encode_point(230.0, scale=10.0, sign=1, offset=0.0, word_order="big", byte_order="big")
    assert registers_to_float(regs, "big", "big") == pytest.approx(2300.0)


def test_encode_decode_sign_inversion_for_power():
    regs = encode_point(-1500.0, scale=10.0, sign=-1, offset=0.0, word_order="big", byte_order="big")
    assert registers_to_float(regs, "big", "big") == pytest.approx(15000.0)


def test_compose_energy_high_low():
    # MWh part=2, kWh part=345 -> 2*1000 + 345 = 2345 kWh
    assert compose([2.0, 345.0], [1000.0, 1.0]) == pytest.approx(2345.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_codec.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nd45_dtsu666.codec'`.

- [ ] **Step 3: Implement `src/nd45_dtsu666/codec.py`**

```python
"""Float32 <-> Modbus register codec with configurable word/byte order."""

from __future__ import annotations

import struct

OVERRANGE = 1e20  # ND45 writes 2e20 when a value is out of measuring range


def _word_bytes(reg: int, byte_order: str) -> bytes:
    if byte_order == "big":
        return bytes([(reg >> 8) & 0xFF, reg & 0xFF])
    return bytes([reg & 0xFF, (reg >> 8) & 0xFF])


def _bytes_word(pair: bytes, byte_order: str) -> int:
    if byte_order == "big":
        return (pair[0] << 8) | pair[1]
    return (pair[1] << 8) | pair[0]


def registers_to_float(regs: list[int], word_order: str = "big", byte_order: str = "big") -> float:
    words = [_word_bytes(r, byte_order) for r in regs]
    raw = words[0] + words[1] if word_order == "big" else words[1] + words[0]
    return struct.unpack(">f", raw)[0]


def float_to_registers(value: float, word_order: str = "big", byte_order: str = "big") -> list[int]:
    raw = struct.pack(">f", value)
    hi, lo = raw[0:2], raw[2:4]
    words = [hi, lo] if word_order == "big" else [lo, hi]
    return [_bytes_word(w, byte_order) for w in words]


def decode_point(
    regs: list[int], scale: float, sign: int, offset: float, word_order: str, byte_order: str
) -> float:
    raw = registers_to_float(regs, word_order, byte_order)
    return raw * scale * sign + offset


def encode_point(
    si: float, scale: float, sign: int, offset: float, word_order: str, byte_order: str
) -> list[int]:
    register_float = si * sign * scale + offset
    return float_to_registers(register_float, word_order, byte_order)


def compose(values: list[float], factors: list[float]) -> float:
    return sum(v * f for v, f in zip(values, factors))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_codec.py -v`
Expected: all 8 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nd45_dtsu666/codec.py tests/test_codec.py
git commit -m "feat: struct-based float32 register codec"
```

---

## Task 3: Config + register-map loaders (pydantic) and seed data

**Files:**
- Create: `src/nd45_dtsu666/config.py`, `config/registers.json`, `config/config.json`, `tests/test_config.py`

**Interfaces:**
- Produces:
  - `class SourcePoint` fields: `addr: int | None`, `compose: list[int] | None`, `factors: list[float] | None`, `scale: float = 1`, `offset: float = 0`, `sign: int = 1`
  - `class TargetPoint` fields: `addr: int`, `from_: str` (alias `from`), `scale: float = 1`, `offset: float = 0`, `sign: int = 1`
  - `class SideMap` fields: `word_order: str = "big"`, `byte_order: str = "big"`, `points: dict[str, SourcePoint|TargetPoint]`
  - `class RegisterMap` fields: `nd45_source: SideMap[SourcePoint]`, `dtsu_target: SideMap[TargetPoint]`
  - `class AppConfig` (nd45, dtsu, safety sub-models)
  - `load_registers(path: str) -> RegisterMap`
  - `load_config(path: str) -> AppConfig`

- [ ] **Step 1: Write failing tests `tests/test_config.py`**

```python
from nd45_dtsu666.config import load_config, load_registers


def test_load_registers_reads_seed(tmp_path):
    reg = load_registers("config/registers.json")
    assert reg.nd45_source.points["u_l1"].addr == 50
    assert reg.dtsu_target.points["u_l1"].addr == 8198
    assert reg.dtsu_target.points["u_l1"].from_ == "u_l1"
    assert reg.dtsu_target.points["u_l1"].scale == 10
    # energy is a two-register compose on the ND45 side
    assert reg.nd45_source.points["imp_energy_total"].compose == [912, 914]
    assert reg.nd45_source.points["imp_energy_total"].factors == [1000, 1]


def test_load_config_reads_seed():
    cfg = load_config("config/config.json")
    assert cfg.nd45.port == 502
    assert cfg.dtsu.slave_id == 1
    assert cfg.safety.max_data_age_s == 3.0


def test_target_point_defaults(tmp_path):
    reg = load_registers("config/registers.json")
    pf = reg.dtsu_target.points["pf_total"]
    assert pf.sign == 1
    assert pf.offset == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_config.py -v`
Expected: FAIL (`ModuleNotFoundError` / files missing).

- [ ] **Step 3: Create `config/registers.json` (seed from spec §6.3)**

```json
{
  "nd45_source": {
    "word_order": "big",
    "byte_order": "big",
    "points": {
      "u_l1":   {"addr": 50},
      "u_l2":   {"addr": 74},
      "u_l3":   {"addr": 98},
      "u_l12":  {"addr": 140},
      "u_l23":  {"addr": 142},
      "u_l31":  {"addr": 144},
      "i_l1":   {"addr": 52},
      "i_l2":   {"addr": 76},
      "i_l3":   {"addr": 100},
      "p_l1":   {"addr": 56},
      "p_l2":   {"addr": 80},
      "p_l3":   {"addr": 104},
      "p_total":{"addr": 128},
      "q_l1":   {"addr": 58},
      "q_l2":   {"addr": 82},
      "q_l3":   {"addr": 106},
      "q_total":{"addr": 130},
      "pf_l1":  {"addr": 64},
      "pf_l2":  {"addr": 88},
      "pf_l3":  {"addr": 112},
      "pf_total":{"addr": 136},
      "freq":   {"addr": 818},
      "imp_energy_total": {"compose": [912, 914], "factors": [1000, 1]},
      "exp_energy_total": {"compose": [928, 930], "factors": [1000, 1]}
    }
  },
  "dtsu_target": {
    "word_order": "big",
    "byte_order": "big",
    "points": {
      "u_l12":  {"addr": 8192, "from": "u_l12", "scale": 10},
      "u_l23":  {"addr": 8194, "from": "u_l23", "scale": 10},
      "u_l31":  {"addr": 8196, "from": "u_l31", "scale": 10},
      "u_l1":   {"addr": 8198, "from": "u_l1", "scale": 10},
      "u_l2":   {"addr": 8200, "from": "u_l2", "scale": 10},
      "u_l3":   {"addr": 8202, "from": "u_l3", "scale": 10},
      "i_l1":   {"addr": 8204, "from": "i_l1", "scale": 1000},
      "i_l2":   {"addr": 8206, "from": "i_l2", "scale": 1000},
      "i_l3":   {"addr": 8208, "from": "i_l3", "scale": 1000},
      "p_total":{"addr": 8210, "from": "p_total", "scale": 10, "sign": 1},
      "p_l1":   {"addr": 8212, "from": "p_l1", "scale": 10, "sign": 1},
      "p_l2":   {"addr": 8214, "from": "p_l2", "scale": 10, "sign": 1},
      "p_l3":   {"addr": 8216, "from": "p_l3", "scale": 10, "sign": 1},
      "q_total":{"addr": 8218, "from": "q_total", "scale": 10},
      "q_l1":   {"addr": 8220, "from": "q_l1", "scale": 10},
      "q_l2":   {"addr": 8222, "from": "q_l2", "scale": 10},
      "q_l3":   {"addr": 8224, "from": "q_l3", "scale": 10},
      "pf_total":{"addr": 8234, "from": "pf_total", "scale": 1000},
      "pf_l1":  {"addr": 8236, "from": "pf_l1", "scale": 1000},
      "pf_l2":  {"addr": 8238, "from": "pf_l2", "scale": 1000},
      "pf_l3":  {"addr": 8240, "from": "pf_l3", "scale": 1000},
      "freq":   {"addr": 8260, "from": "freq", "scale": 100},
      "imp_ep": {"addr": 4128, "from": "imp_energy_total", "scale": 1},
      "exp_ep": {"addr": 4130, "from": "exp_energy_total", "scale": 1}
    }
  }
}
```

- [ ] **Step 4: Create `config/config.json`**

```json
{
  "nd45":   {"host": "192.168.1.10", "port": 502, "unit_id": 1, "poll_interval_s": 0.3, "timeout_s": 1.0},
  "dtsu":   {"port": "/dev/ttyAMA0", "baudrate": 9600, "parity": "N", "stopbits": 1, "slave_id": 1},
  "safety": {"max_data_age_s": 3.0, "check_interval_s": 0.5}
}
```

- [ ] **Step 5: Implement `src/nd45_dtsu666/config.py`**

```python
"""Pydantic config + register-map models and JSON loaders."""

from __future__ import annotations

import json

from pydantic import BaseModel, Field


class SourcePoint(BaseModel):
    addr: int | None = None
    compose: list[int] | None = None
    factors: list[float] | None = None
    scale: float = 1.0
    offset: float = 0.0
    sign: int = 1


class TargetPoint(BaseModel):
    addr: int
    from_: str = Field(alias="from")
    scale: float = 1.0
    offset: float = 0.0
    sign: int = 1

    model_config = {"populate_by_name": True}


class SourceSide(BaseModel):
    word_order: str = "big"
    byte_order: str = "big"
    points: dict[str, SourcePoint]


class TargetSide(BaseModel):
    word_order: str = "big"
    byte_order: str = "big"
    points: dict[str, TargetPoint]


class RegisterMap(BaseModel):
    nd45_source: SourceSide
    dtsu_target: TargetSide


class Nd45Conf(BaseModel):
    host: str
    port: int = 502
    unit_id: int = 1
    poll_interval_s: float = 0.3
    timeout_s: float = 1.0


class DtsuConf(BaseModel):
    port: str
    baudrate: int = 9600
    parity: str = "N"
    stopbits: int = 1
    slave_id: int = 1


class SafetyConf(BaseModel):
    max_data_age_s: float = 3.0
    check_interval_s: float = 0.5


class AppConfig(BaseModel):
    nd45: Nd45Conf
    dtsu: DtsuConf
    safety: SafetyConf = SafetyConf()


def load_registers(path: str) -> RegisterMap:
    with open(path, encoding="utf-8") as f:
        return RegisterMap.model_validate(json.load(f))


def load_config(path: str) -> AppConfig:
    with open(path, encoding="utf-8") as f:
        return AppConfig.model_validate(json.load(f))
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_config.py -v`
Expected: all 3 tests PASS.

- [ ] **Step 7: Commit**

```bash
git add src/nd45_dtsu666/config.py config/registers.json config/config.json tests/test_config.py
git commit -m "feat: pydantic config + seed register maps"
```

---

## Task 4: Canonical store with freshness gate

**Files:**
- Create: `src/nd45_dtsu666/canonical.py`, `tests/test_canonical.py`

**Interfaces:**
- Produces:
  - `class CanonicalStore` with:
    - `update(self, values: dict[str, float], ts: float) -> None`
    - `snapshot(self) -> tuple[dict[str, float], float]` (returns copy of values + timestamp)
    - `age(self, now: float) -> float` (returns `inf` if never updated)
    - `is_fresh(self, now: float, max_age: float) -> bool`
  - `class HealthGate` with `should_serve(self, age: float) -> bool`

- [ ] **Step 1: Write failing tests `tests/test_canonical.py`**

```python
import math

from nd45_dtsu666.canonical import CanonicalStore, HealthGate


def test_new_store_is_infinitely_old():
    store = CanonicalStore()
    assert store.age(now=100.0) == math.inf
    assert store.is_fresh(now=100.0, max_age=3.0) is False


def test_update_then_snapshot():
    store = CanonicalStore()
    store.update({"p_total": 1500.0}, ts=10.0)
    values, ts = store.snapshot()
    assert values == {"p_total": 1500.0}
    assert ts == 10.0


def test_snapshot_is_a_copy():
    store = CanonicalStore()
    store.update({"p_total": 1.0}, ts=1.0)
    values, _ = store.snapshot()
    values["p_total"] = 999.0
    again, _ = store.snapshot()
    assert again["p_total"] == 1.0


def test_age_and_freshness():
    store = CanonicalStore()
    store.update({"x": 1.0}, ts=100.0)
    assert store.age(now=102.0) == 2.0
    assert store.is_fresh(now=102.0, max_age=3.0) is True
    assert store.is_fresh(now=104.0, max_age=3.0) is False


def test_health_gate():
    gate = HealthGate(max_age=3.0)
    assert gate.should_serve(age=1.0) is True
    assert gate.should_serve(age=3.0) is True
    assert gate.should_serve(age=3.1) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_canonical.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nd45_dtsu666.canonical'`.

- [ ] **Step 3: Implement `src/nd45_dtsu666/canonical.py`**

```python
"""In-memory canonical SI store with a freshness gate for fail-safe."""

from __future__ import annotations

import math


class CanonicalStore:
    def __init__(self) -> None:
        self._values: dict[str, float] = {}
        self._ts: float | None = None

    def update(self, values: dict[str, float], ts: float) -> None:
        self._values = dict(values)
        self._ts = ts

    def snapshot(self) -> tuple[dict[str, float], float]:
        return dict(self._values), (self._ts if self._ts is not None else math.nan)

    def age(self, now: float) -> float:
        if self._ts is None:
            return math.inf
        return now - self._ts

    def is_fresh(self, now: float, max_age: float) -> bool:
        return self.age(now) <= max_age


class HealthGate:
    def __init__(self, max_age: float) -> None:
        self.max_age = max_age

    def should_serve(self, age: float) -> bool:
        return age <= self.max_age
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_canonical.py -v`
Expected: all 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nd45_dtsu666/canonical.py tests/test_canonical.py
git commit -m "feat: canonical store with freshness gate"
```

---

## Task 5: ND45 poller (async TCP client + decode)

**Files:**
- Create: `src/nd45_dtsu666/nd45_poller.py`, `tests/test_poller.py`

**Interfaces:**
- Consumes: `codec.decode_point`, `codec.registers_to_float`, `codec.compose`, `codec.OVERRANGE`; `config.SourceSide`.
- Produces:
  - `READ_GROUPS: list[tuple[int, int]]` = `[(50, 96), (818, 2), (912, 20)]`
  - `extract_registers(addr: int, groups: list[tuple[int, list[int]]]) -> list[int]`
  - `async def poll_once(client, source: SourceSide, slave: int) -> dict[str, float]`
  - `async def run_poller(client, source, slave, interval, on_update, on_error, stop_event) -> None`
    - `on_update: Callable[[dict[str, float], float], None]`, `on_error: Callable[[Exception], None]`

- [ ] **Step 1: Write failing tests `tests/test_poller.py`**

```python
import pytest

from nd45_dtsu666.codec import float_to_registers
from nd45_dtsu666.config import load_registers
from nd45_dtsu666.nd45_poller import READ_GROUPS, extract_registers, poll_once


class FakeResponse:
    def __init__(self, registers):
        self.registers = registers

    def isError(self):
        return False


class FakeClient:
    """Serves a synthetic ND45 register image for the three read groups."""

    def __init__(self, image: dict[int, list[int]]):
        # image maps absolute addr -> 2 registers (a float32)
        self.image = image

    async def read_holding_registers(self, address, count, slave=0):
        regs = []
        for a in range(address, address + count):
            pair = self.image.get(a)
            if pair is not None:
                regs.append(pair[0])
            elif a - 1 in self.image:
                regs.append(self.image[a - 1][1])
            else:
                regs.append(0)
        return FakeResponse(regs)


def _image_for(values: dict[int, float]) -> dict[int, list[int]]:
    return {addr: float_to_registers(v, "big", "big") for addr, v in values.items()}


def test_extract_registers_finds_pair():
    groups = [(50, [0x4366, 0x0000, 0x1111, 0x2222])]
    assert extract_registers(50, groups) == [0x4366, 0x0000]
    assert extract_registers(51, groups) == [0x0000, 0x1111]


def test_read_groups_cover_all_addresses():
    src = load_registers("config/registers.json").nd45_source
    covered = set()
    for base, count in READ_GROUPS:
        covered.update(range(base, base + count))
    for pt in src.points.values():
        addrs = pt.compose if pt.compose else [pt.addr]
        for a in addrs:
            assert a in covered and (a + 1) in covered, f"addr {a} not covered"


async def test_poll_once_decodes_points():
    src = load_registers("config/registers.json").nd45_source
    image = _image_for({
        50: 230.1, 128: 1500.0, 818: 50.02,
        912: 2.0, 914: 345.0,   # imp energy -> 2345 kWh
        928: 0.0, 930: 12.0,    # exp energy -> 12 kWh
    })
    client = FakeClient(image)
    values = await poll_once(client, src, slave=1)
    assert values["u_l1"] == pytest.approx(230.1, rel=1e-5)
    assert values["p_total"] == pytest.approx(1500.0, rel=1e-5)
    assert values["freq"] == pytest.approx(50.02, rel=1e-5)
    assert values["imp_energy_total"] == pytest.approx(2345.0, rel=1e-5)
    assert values["exp_energy_total"] == pytest.approx(12.0, rel=1e-5)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_poller.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nd45_dtsu666.nd45_poller'`.

- [ ] **Step 3: Implement `src/nd45_dtsu666/nd45_poller.py`**

```python
"""Async ND45 Modbus TCP poller: read register blocks, decode into SI values."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from .codec import OVERRANGE, compose, decode_point, registers_to_float
from .config import SourceSide

log = logging.getLogger(__name__)

# Fixed read blocks (base_addr, register_count) covering every mapped ND45 point.
# Group 1: 200 ms measurements 50..145; Group 2: frequency 818..819; Group 3: energy 912..931.
READ_GROUPS: list[tuple[int, int]] = [(50, 96), (818, 2), (912, 20)]


class PollError(RuntimeError):
    pass


def extract_registers(addr: int, groups: list[tuple[int, list[int]]]) -> list[int]:
    for base, regs in groups:
        if base <= addr < base + len(regs) - 1:
            off = addr - base
            return regs[off:off + 2]
    raise KeyError(addr)


async def poll_once(client, source: SourceSide, slave: int) -> dict[str, float]:
    groups: list[tuple[int, list[int]]] = []
    for base, count in READ_GROUPS:
        rr = await client.read_holding_registers(base, count, slave=slave)
        if rr.isError():
            raise PollError(f"ND45 read error at {base}: {rr}")
        groups.append((base, rr.registers))

    wo, bo = source.word_order, source.byte_order
    values: dict[str, float] = {}
    for key, pt in source.points.items():
        if pt.compose:
            parts = [registers_to_float(extract_registers(a, groups), wo, bo) for a in pt.compose]
            si = compose(parts, pt.factors or [1.0] * len(parts))
        else:
            regs = extract_registers(pt.addr, groups)
            si = decode_point(regs, pt.scale, pt.sign, pt.offset, wo, bo)
        if abs(si) >= OVERRANGE:
            log.warning("ND45 %s over range, using 0.0", key)
            si = 0.0
        values[key] = si
    return values


async def run_poller(
    client,
    source: SourceSide,
    slave: int,
    interval: float,
    on_update: Callable[[dict[str, float], float], None],
    on_error: Callable[[Exception], None],
    stop_event: asyncio.Event,
) -> None:
    loop = asyncio.get_event_loop()
    while not stop_event.is_set():
        try:
            values = await poll_once(client, source, slave)
            on_update(values, loop.time())
        except Exception as exc:  # noqa: BLE001 - poller must never die
            on_error(exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_poller.py -v`
Expected: all 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nd45_dtsu666/nd45_poller.py tests/test_poller.py
git commit -m "feat: ND45 async poller with grouped reads and decode"
```

---

## Task 6: DTSU datastore build + update (transport-agnostic mapping)

**Files:**
- Create: `src/nd45_dtsu666/dtsu_server.py`, `tests/test_server.py`

**Interfaces:**
- Consumes: `codec.encode_point`, `codec.decode_point`; `config.TargetSide`; pymodbus datastore classes.
- Produces:
  - `build_context(target: TargetSide, slave_id: int) -> ModbusServerContext`
  - `update_datastore(context, slave_id: int, canonical: dict[str, float], target: TargetSide) -> None`

- [ ] **Step 1: Write failing tests `tests/test_server.py`**

```python
import pytest

from nd45_dtsu666.codec import registers_to_float
from nd45_dtsu666.config import load_registers
from nd45_dtsu666.dtsu_server import build_context, update_datastore


def _read_point(context, slave_id, pt, target):
    # True inverse of encode_point (register_float = SI*sign*scale + offset).
    regs = context[slave_id].getValues(3, pt.addr, count=2)
    raw = registers_to_float(regs, target.word_order, target.byte_order)
    return (raw - pt.offset) / (pt.scale * pt.sign)


def test_update_datastore_encodes_voltage_and_power():
    target = load_registers("config/registers.json").dtsu_target
    context = build_context(target, slave_id=1)
    update_datastore(context, 1, {"u_l1": 230.0, "p_total": 1500.0}, target)

    u = _read_point(context, 1, target.points["u_l1"], target)
    p = _read_point(context, 1, target.points["p_total"], target)
    assert u == pytest.approx(230.0, rel=1e-5)
    assert p == pytest.approx(1500.0, rel=1e-5)


def test_datastore_raw_scaling_matches_dtsu_spec():
    target = load_registers("config/registers.json").dtsu_target
    context = build_context(target, slave_id=1)
    update_datastore(context, 1, {"u_l1": 230.0}, target)
    # DTSU voltage is x10 -> raw register float must be 2300.0
    from nd45_dtsu666.codec import registers_to_float

    regs = context[1].getValues(3, target.points["u_l1"].addr, count=2)
    assert registers_to_float(regs, target.word_order, target.byte_order) == pytest.approx(2300.0)


def test_missing_canonical_key_is_skipped():
    target = load_registers("config/registers.json").dtsu_target
    context = build_context(target, slave_id=1)
    update_datastore(context, 1, {}, target)  # no values -> no crash
    regs = context[1].getValues(3, target.points["u_l1"].addr, count=2)
    assert regs == [0, 0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_server.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nd45_dtsu666.dtsu_server'`.

- [ ] **Step 3: Implement datastore functions in `src/nd45_dtsu666/dtsu_server.py`**

```python
"""DTSU666 RTU server: datastore build/update + fail-safe supervisor."""

from __future__ import annotations

import logging

from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)

from .codec import encode_point
from .config import TargetSide

log = logging.getLogger(__name__)

_HOLDING_FC = 3


def build_context(target: TargetSide, slave_id: int) -> ModbusServerContext:
    max_addr = max(pt.addr for pt in target.points.values())
    block = ModbusSequentialDataBlock(0, [0] * (max_addr + 2))
    slave = ModbusSlaveContext(hr=block, zero_mode=True)
    return ModbusServerContext(slaves={slave_id: slave}, single=False)


def update_datastore(
    context: ModbusServerContext, slave_id: int, canonical: dict[str, float], target: TargetSide
) -> None:
    slave = context[slave_id]
    wo, bo = target.word_order, target.byte_order
    for pt in target.points.values():
        si = canonical.get(pt.from_)
        if si is None:
            continue
        regs = encode_point(si, pt.scale, pt.sign, pt.offset, wo, bo)
        slave.setValues(_HOLDING_FC, pt.addr, regs)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_server.py -v`
Expected: all 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nd45_dtsu666/dtsu_server.py tests/test_server.py
git commit -m "feat: DTSU datastore build and canonical->register update"
```

---

## Task 7: RTU server + fail-safe supervisor

**Files:**
- Modify: `src/nd45_dtsu666/dtsu_server.py`
- Modify: `tests/test_server.py`

**Interfaces:**
- Consumes: `build_context`, `update_datastore` (Task 6); `canonical.CanonicalStore`, `canonical.HealthGate`; `config.DtsuConf`.
- Produces:
  - `async def supervise_server(cfg: DtsuConf, context, store: CanonicalStore, gate: HealthGate, check_interval: float, stop_event, server_factory=...) -> None`
  - `def make_serial_server(cfg: DtsuConf, context)` — builds a pymodbus `ModbusSerialServer` (RTU).

> **Implementation note (live-hardware caveat, spec §8):** the fail-safe silences the RTU
> side by **stopping the server** while data is stale so Sigenergy times out. The exact
> pymodbus serial start/stop API must be confirmed against the pinned 3.6 version during
> implementation; the supervisor logic below is decoupled from that via `server_factory`
> so it is unit-testable without a serial port.

- [ ] **Step 1: Write failing test for the supervisor (append to `tests/test_server.py`)**

```python
import asyncio

from nd45_dtsu666.canonical import CanonicalStore, HealthGate
from nd45_dtsu666.config import DtsuConf
from nd45_dtsu666.dtsu_server import supervise_server


class FakeServer:
    def __init__(self):
        self.running = False
        self.serve_calls = 0
        self.shutdown_calls = 0

    async def serve_forever(self):
        self.running = True
        self.serve_calls += 1
        while self.running:
            await asyncio.sleep(0.01)

    async def shutdown(self):
        self.running = False
        self.shutdown_calls += 1


async def test_supervisor_starts_when_fresh_and_stops_when_stale():
    store = CanonicalStore()
    gate = HealthGate(max_age=3.0)
    cfg = DtsuConf(port="/dev/null", slave_id=1)
    fake = FakeServer()
    stop = asyncio.Event()
    clock = {"t": 100.0}

    def now():
        return clock["t"]

    store.update({"p_total": 1.0}, ts=now())  # fresh
    task = asyncio.create_task(
        supervise_server(cfg, context=None, store=store, gate=gate,
                         check_interval=0.02, stop_event=stop,
                         server_factory=lambda: fake, now=now)
    )
    await asyncio.sleep(0.1)
    assert fake.running is True  # serving while fresh

    clock["t"] = 200.0  # data now stale (age 100s > 3s)
    await asyncio.sleep(0.1)
    assert fake.running is False  # stopped -> Sigenergy sees timeout

    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert fake.serve_calls >= 1 and fake.shutdown_calls >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_server.py::test_supervisor_starts_when_fresh_and_stops_when_stale -v`
Expected: FAIL with `ImportError: cannot import name 'supervise_server'`.

- [ ] **Step 3: Add supervisor + serial factory to `src/nd45_dtsu666/dtsu_server.py`**

Append imports at the top of the file:

```python
import asyncio
import time
from collections.abc import Callable

from pymodbus.framer.rtu_framer import ModbusRtuFramer
from pymodbus.server import ModbusSerialServer
```

Append these functions:

```python
def make_serial_server(cfg, context) -> ModbusSerialServer:
    return ModbusSerialServer(
        context=context,
        framer=ModbusRtuFramer,
        port=cfg.port,
        baudrate=cfg.baudrate,
        parity=cfg.parity,
        stopbits=cfg.stopbits,
        bytesize=8,
    )


async def supervise_server(
    cfg,
    context,
    store,
    gate,
    check_interval: float,
    stop_event: asyncio.Event,
    server_factory: Callable | None = None,
    now: Callable[[], float] | None = None,
) -> None:
    """Start the RTU server while data is fresh; stop it (silence) while stale."""
    factory = server_factory or (lambda: make_serial_server(cfg, context))
    clock = now or time.monotonic
    server = None
    serve_task: asyncio.Task | None = None

    try:
        while not stop_event.is_set():
            age = store.age(clock())
            if gate.should_serve(age) and server is None:
                server = factory()
                serve_task = asyncio.create_task(server.serve_forever())
                log.info("RTU server started (data fresh, age=%.2fs)", age)
            elif not gate.should_serve(age) and server is not None:
                await server.shutdown()
                if serve_task:
                    serve_task.cancel()
                server, serve_task = None, None
                log.warning("RTU server stopped (data stale, age=%.2fs) -> fail-safe", age)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=check_interval)
            except asyncio.TimeoutError:
                pass
    finally:
        if server is not None:
            await server.shutdown()
            if serve_task:
                serve_task.cancel()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_server.py -v`
Expected: all server tests PASS (including the supervisor test).

- [ ] **Step 5: Commit**

```bash
git add src/nd45_dtsu666/dtsu_server.py tests/test_server.py
git commit -m "feat: RTU server + fail-safe supervisor (silence on stale data)"
```

---

## Task 8: Orchestration (`app.py`) + `run` entrypoint

**Files:**
- Create: `src/nd45_dtsu666/app.py`, `src/nd45_dtsu666/__main__.py`
- Modify: `tests/test_smoke.py` (add orchestration wiring test)

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `def build_on_update(store, context, slave_id, target) -> Callable[[dict, float], None]`
  - `async def run_app(config: AppConfig, registers: RegisterMap, stop_event: asyncio.Event, client=None) -> None`
  - `def main(argv: list[str] | None = None) -> int`

- [ ] **Step 1: Write failing test `tests/test_app.py`**

```python
from nd45_dtsu666.app import build_on_update
from nd45_dtsu666.canonical import CanonicalStore
from nd45_dtsu666.codec import registers_to_float
from nd45_dtsu666.config import load_registers
from nd45_dtsu666.dtsu_server import build_context


def test_on_update_writes_store_and_datastore():
    target = load_registers("config/registers.json").dtsu_target
    store = CanonicalStore()
    context = build_context(target, slave_id=1)
    on_update = build_on_update(store, context, 1, target)

    on_update({"u_l1": 230.0}, ts=5.0)

    values, ts = store.snapshot()
    assert values["u_l1"] == 230.0 and ts == 5.0
    regs = context[1].getValues(3, target.points["u_l1"].addr, count=2)
    assert registers_to_float(regs, target.word_order, target.byte_order) == 2300.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_app.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nd45_dtsu666.app'`.

- [ ] **Step 3: Implement `src/nd45_dtsu666/app.py`**

```python
"""Orchestration: wire poller + RTU server + fail-safe under one asyncio loop."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from pymodbus.client import AsyncModbusTcpClient

from .canonical import CanonicalStore, HealthGate
from .config import AppConfig, RegisterMap
from .dtsu_server import build_context, supervise_server, update_datastore
from .nd45_poller import run_poller

log = logging.getLogger(__name__)


def build_on_update(store, context, slave_id, target) -> Callable[[dict, float], None]:
    def on_update(values: dict[str, float], ts: float) -> None:
        store.update(values, ts)
        update_datastore(context, slave_id, values, target)

    return on_update


def _on_error(exc: Exception) -> None:
    log.warning("poll failed: %s", exc)


async def run_app(
    config: AppConfig,
    registers: RegisterMap,
    stop_event: asyncio.Event,
    client=None,
) -> None:
    store = CanonicalStore()
    gate = HealthGate(config.safety.max_data_age_s)
    context = build_context(registers.dtsu_target, config.dtsu.slave_id)
    on_update = build_on_update(store, context, config.dtsu.slave_id, registers.dtsu_target)

    client = client or AsyncModbusTcpClient(
        config.nd45.host, port=config.nd45.port, timeout=config.nd45.timeout_s
    )
    await client.connect()

    poller = run_poller(
        client, registers.nd45_source, config.nd45.unit_id,
        config.nd45.poll_interval_s, on_update, _on_error, stop_event,
    )
    supervisor = supervise_server(
        config.dtsu, context, store, gate,
        config.safety.check_interval_s, stop_event,
    )
    try:
        await asyncio.gather(poller, supervisor)
    finally:
        client.close()
```

- [ ] **Step 4: Implement `src/nd45_dtsu666/__main__.py`**

```python
"""CLI entrypoint: run | diag | selftest."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from .app import run_app
from .config import load_config, load_registers


def _install_signal_handlers(loop, stop_event) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:  # Windows dev
            pass


def _cmd_run(args) -> int:
    config = load_config(args.config)
    registers = load_registers(args.registers)

    async def _main() -> None:
        stop_event = asyncio.Event()
        _install_signal_handlers(asyncio.get_event_loop(), stop_event)
        await run_app(config, registers, stop_event)

    asyncio.run(_main())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nd45-dtsu666")
    parser.add_argument("--config", default="config/config.json")
    parser.add_argument("--registers", default="config/registers.json")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run the bridge")
    sub.add_parser("diag", help="diagnostic table (Task 9)")
    sub.add_parser("selftest", help="serve synthetic data (Task 9)")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.command == "run":
        return _cmd_run(args)
    if args.command in ("diag", "selftest"):
        from .diagnostics import run_diag_command

        return run_diag_command(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

> Note: `diag`/`selftest` import is satisfied by Task 9. If executing tasks strictly in order,
> the `run` command and its test are fully functional now; `diag`/`selftest` become live after Task 9.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_app.py -v`
Expected: `test_on_update_writes_store_and_datastore PASSED`.

- [ ] **Step 6: Commit**

```bash
git add src/nd45_dtsu666/app.py src/nd45_dtsu666/__main__.py tests/test_app.py
git commit -m "feat: orchestration and run CLI entrypoint"
```

---

## Task 9: Diagnostics + selftest modes

**Files:**
- Create: `src/nd45_dtsu666/diagnostics.py`, `tests/test_diagnostics.py`

**Interfaces:**
- Consumes: `config` loaders, `codec`, `canonical`, `dtsu_server`, `nd45_poller`.
- Produces:
  - `render_table(source, target, canonical: dict[str, float], age: float, healthy: bool) -> str`
  - `run_diag_command(args) -> int`

- [ ] **Step 1: Write failing test `tests/test_diagnostics.py`**

```python
from nd45_dtsu666.config import load_registers
from nd45_dtsu666.diagnostics import render_table


def test_render_table_contains_key_columns_and_values():
    reg = load_registers("config/registers.json")
    table = render_table(
        reg.nd45_source, reg.dtsu_target,
        canonical={"u_l1": 230.0, "p_total": 1500.0},
        age=0.4, healthy=True,
    )
    assert "u_l1" in table
    assert "230.0" in table
    assert "p_total" in table
    assert "1500.0" in table
    # shows the DTSU target address for u_l1 (8198)
    assert "8198" in table
    assert "age" in table.lower()
    assert "OK" in table or "HEALTHY" in table.upper()


def test_render_table_shows_stale_status():
    reg = load_registers("config/registers.json")
    table = render_table(reg.nd45_source, reg.dtsu_target, canonical={}, age=99.0, healthy=False)
    assert "STALE" in table.upper() or "FAIL" in table.upper()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_diagnostics.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nd45_dtsu666.diagnostics'`.

- [ ] **Step 3: Implement `src/nd45_dtsu666/diagnostics.py`**

```python
"""Diagnostic table renderer + diag/selftest command runners."""

from __future__ import annotations

import asyncio
import time

from .canonical import CanonicalStore, HealthGate
from .codec import encode_point, registers_to_float
from .config import RegisterMap, load_config, load_registers


def render_table(source, target, canonical: dict[str, float], age: float, healthy: bool) -> str:
    status = "OK" if healthy else "STALE/FAILSAFE"
    lines = [
        f"status: {status}   data age: {age:.2f}s",
        f"{'canonical':<18}{'SI value':>14}   {'DTSU addr':>9}{'reg raw':>14}",
        "-" * 60,
    ]
    for key, pt in target.points.items():
        si = canonical.get(pt.from_)
        if si is None:
            si_txt, raw_txt = "-", "-"
        else:
            regs = encode_point(si, pt.scale, pt.sign, pt.offset, target.word_order, target.byte_order)
            si_txt = f"{si:.3f}"
            raw_txt = f"{registers_to_float(regs, target.word_order, target.byte_order):.1f}"
        lines.append(f"{pt.from_:<18}{si_txt:>14}   {pt.addr:>9}{raw_txt:>14}")
    return "\n".join(lines)


def run_diag_command(args) -> int:
    config = load_config(args.config)
    registers = load_registers(args.registers)
    if args.command == "selftest":
        return _run_selftest(config, registers)
    return _run_diag(config, registers)


def _synthetic_values(registers: RegisterMap) -> dict[str, float]:
    demo = {"u_l1": 230.0, "u_l2": 231.0, "u_l3": 229.0, "i_l1": 5.0, "i_l2": 5.1, "i_l3": 4.9,
            "p_total": 1500.0, "q_total": 200.0, "pf_total": 0.95, "freq": 50.0,
            "imp_energy_total": 1234.5, "exp_energy_total": 67.8}
    return {k: demo.get(k, 0.0) for k in registers.nd45_source.points}


def _run_selftest(config, registers) -> int:
    from .dtsu_server import build_context, supervise_server, update_datastore

    async def _main() -> None:
        store = CanonicalStore()
        gate = HealthGate(config.safety.max_data_age_s)
        context = build_context(registers.dtsu_target, config.dtsu.slave_id)
        values = _synthetic_values(registers)
        stop = asyncio.Event()

        async def _feeder() -> None:
            while not stop.is_set():
                store.update(values, asyncio.get_event_loop().time())
                update_datastore(context, config.dtsu.slave_id, values, registers.dtsu_target)
                await asyncio.sleep(0.5)

        print("selftest: serving synthetic DTSU data; point mbpoll at the RTU port. Ctrl-C to stop.")
        await asyncio.gather(
            _feeder(),
            supervise_server(config.dtsu, context, store, gate,
                             config.safety.check_interval_s, stop),
        )

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
    return 0


def _run_diag(config, registers) -> int:
    from pymodbus.client import AsyncModbusTcpClient

    from .nd45_poller import poll_once

    async def _main() -> None:
        client = AsyncModbusTcpClient(config.nd45.host, port=config.nd45.port,
                                      timeout=config.nd45.timeout_s)
        await client.connect()
        try:
            while True:
                t0 = time.monotonic()
                try:
                    values = await poll_once(client, registers.nd45_source, config.nd45.unit_id)
                    age = time.monotonic() - t0
                    healthy = True
                except Exception as exc:  # noqa: BLE001
                    values, age, healthy = {}, float("inf"), False
                    print(f"poll error: {exc}")
                print("\033[2J\033[H", end="")  # clear screen
                print(render_table(registers.nd45_source, registers.dtsu_target, values, age, healthy))
                await asyncio.sleep(1.0)
        finally:
            client.close()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
    return 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_diagnostics.py -v`
Expected: both tests PASS.

- [ ] **Step 5: Full suite + lint**

Run: `pytest -v && ruff check .`
Expected: all tests PASS; ruff clean.

- [ ] **Step 6: Commit**

```bash
git add src/nd45_dtsu666/diagnostics.py tests/test_diagnostics.py
git commit -m "feat: diagnostic table + diag/selftest CLI modes"
```

---

## Task 10: systemd unit + README + deployment/verification docs

**Files:**
- Create: `systemd/nd45-dtsu666.service`
- Modify: `README.md`

**Interfaces:**
- Produces: deployment artifacts and the on-site verification checklist (spec §10, §11).

- [ ] **Step 1: Create `systemd/nd45-dtsu666.service`**

```ini
[Unit]
Description=ND45 -> DTSU666 Modbus bridge (Sigenergy Power Sensor)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/nd45_dtsu666
ExecStart=/opt/nd45_dtsu666/.venv/bin/python -m nd45_dtsu666 run \
  --config /opt/nd45_dtsu666/config/config.json \
  --registers /opt/nd45_dtsu666/config/registers.json
Restart=always
RestartSec=2
# Logs go to journald via stdout/stderr
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Expand `README.md`**

```markdown
# nd45_dtsu666_converter

Temporary Modbus bridge: Lumel ND45 (Modbus TCP) → DTSU666 register map (Modbus RTU)
so a Sigenergy storage system can read it as a "Power Sensor".

## Install (reComputer R1000, Ubuntu)
```bash
sudo mkdir -p /opt/nd45_dtsu666 && sudo chown $USER /opt/nd45_dtsu666
git clone https://github.com/korneliuszm/nd45_dtsu666_converter.git /opt/nd45_dtsu666
cd /opt/nd45_dtsu666
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
```

Edit `config/config.json`: set ND45 `host`, the RS-485 device `port` (`/dev/ttyAMA0` etc.),
`slave_id`, and `baudrate`.

## Bench test before connecting Sigenergy
Run the RTU side with synthetic data and read it with mbpoll:
```bash
python -m nd45_dtsu666 selftest
# in another shell (RTU master), read holding registers as float from address 8192:
mbpoll -m rtu -a 1 -b 9600 -P none -t 4:float -r 8193 -c 4 /dev/ttyUSB0
```
(Note mbpoll `-r` is 1-based; register 8192 → `-r 8193`. Confirm word order matches.)

## Diagnostics
```bash
python -m nd45_dtsu666 diag   # live table: canonical SI, DTSU addr/raw, age, status
```

## Run as a service
```bash
sudo cp systemd/nd45-dtsu666.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nd45-dtsu666
journalctl -u nd45-dtsu666 -f
```

## On-site verification checklist (before leaving unattended)
1. **Sign convention** — with known import/export, confirm Sigenergy sees correct grid
   direction. If reversed, set `sign: -1` on the `p_*` target points in `registers.json`.
2. **Phase order** — confirm L1/L2/L3 == A/B/C. Swap `from` keys if needed.
3. **Scaling** — voltages/currents/power read plausibly on Sigenergy.
4. **Word/byte order** — if values look garbled, flip `word_order`/`byte_order` in the map.
5. **Slave ID / baud** — match what Sigenergy polls.
6. **Fail-safe** — pull the ND45 network cable; confirm the RTU side goes silent
   (`journalctl` shows "fail-safe") and Sigenergy enters its safe mode.
7. **RS-485 direction** — verify the reComputer transceiver auto-toggles direction, or
   configure pyserial RS-485 mode if the master sees no/garbled replies.
```

- [ ] **Step 3: Verify service file references a real path layout**

Run: `grep -n "ExecStart" systemd/nd45-dtsu666.service`
Expected: path `/opt/nd45_dtsu666/.venv/bin/python` present.

- [ ] **Step 4: Commit**

```bash
git add systemd/nd45-dtsu666.service README.md
git commit -m "docs: systemd unit, README, on-site verification checklist"
```

---

## Task 11: Full-suite gate + push to GitHub

**Files:** none (integration/release step)

- [ ] **Step 1: Run the whole suite and lint**

Run: `pytest -v && ruff check .`
Expected: every test PASS; ruff reports no issues.

- [ ] **Step 2: Add remote and push (confirm with user first)**

```bash
git remote add origin https://github.com/korneliuszm/nd45_dtsu666_converter.git
git push -u origin main
```
Expected: CI workflow runs on GitHub and passes on all Python versions.

- [ ] **Step 3: Confirm CI is green**

Check the Actions tab; the `test` job passes for 3.10, 3.11, 3.12.

---

## Self-Review

**Spec coverage:**
- §3 concurrency (asyncio) → Task 8 orchestration. ✓
- §4 module structure → File Structure + Tasks 2–9. ✓
- §5 canonical model → Task 4. ✓
- §6 register maps → Task 3 seed `registers.json`; Task 5 decode; Task 6 encode. ✓
- §7 JSON config + transform semantics → Task 3 (config), Task 2 (codec applies the exact formulas). ✓
- §8 fail-safe → Task 7 supervisor (silence on stale). ✓
- §9 diagnostics (run/diag/selftest) → Task 8 (run) + Task 9 (diag/selftest). ✓
- §10 test plan (unit, e2e mapping, mbpoll) → Tasks 2–9 unit/mapping tests; README mbpoll bench. ✓
- §11 assumptions to verify → Task 10 checklist. ✓
- §13 CI/CD → Task 1 workflow; Task 11 push. ✓
- §14 systemd/venv → Task 10. ✓

**Placeholder scan:** No "TBD/TODO"; the only forward references (`diag`/`selftest` in Task 8's
`__main__.py`) are explicitly resolved by Task 9 and called out inline.

**Type consistency:** `SourcePoint`/`TargetPoint`/`SourceSide`/`TargetSide` names are consistent
Tasks 3→5→6→9. `decode_point`/`encode_point`/`registers_to_float`/`float_to_registers`/`compose`
signatures identical everywhere used. `build_context`/`update_datastore`/`supervise_server`/
`build_on_update`/`run_poller`/`poll_once` signatures match across Tasks 5–9. `store.age(now)` /
`store.update(values, ts)` / `gate.should_serve(age)` consistent Tasks 4→7→8.
