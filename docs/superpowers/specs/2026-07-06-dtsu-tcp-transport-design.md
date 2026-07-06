# Spec: Modbus TCP jako alternatywny transport wyjściowy (obok RTU)

**Data:** 2026-07-06
**Status:** zatwierdzony design
**Kontekst:** rozszerzenie istniejącego mostu ND45→DTSU666 o możliwość serwowania danych
wyjściowych po Modbus TCP, zamiast wyłącznie po Modbus RTU/RS-485.

## 1. Cel

Obecnie strona wyjściowa (dla Sigenergy) działa wyłącznie po Modbus RTU (`ModbusSerialServer`
+ `ModbusRtuFramer`). Cel: dodać Modbus TCP jako **równoważną, alternatywną** opcję transportu
wyjściowego — wybieraną w `config/config.json` — bez zmiany formatu danych (mapa rejestrów
DTSU666, `config/registers.json`, zostaje identyczna) i bez zmiany logiki pollera ND45,
canonical store ani fail-safe supervisora.

RTU **zostaje** w kodzie jako pełnoprawna opcja (nie jest usuwane) — to przełącznik, nie migracja.

## 2. Zakres (YAGNI)

- Jeden aktywny transport na raz, wybierany polem `dtsu.transport` w configu — NIE oba
  jednocześnie (współbieżne serwowanie RTU+TCP nie jest potrzebne i komplikowałoby fail-safe).
- Bez zmian w `codec.py`, `canonical.py`, `nd45_poller.py`, `config/registers.json` — transformacja
  ND45→SI→DTSU i sama mapa rejestrów są niezależne od transportu wyjściowego.
- Bez zmian w logice fail-safe/supervisora (`supervise_server`, `_server_action`) — operuje na
  obiekcie serwera przez ten sam interfejs (`serve_forever()`/`shutdown()`), który `ModbusTcpServer`
  i `ModbusSerialServer` w pymodbus 3.6.9 mają identyczny (potwierdzone w źródle pymodbus).
- Bez UDP/TLS — poza zakresem, Sigenergy tego nie wymaga.

## 3. Config (`config/config.json`)

Nowy kształt sekcji `dtsu`:

```json
"dtsu": {
  "transport": "rtu",
  "slave_id": 1,
  "rtu": {"port": "/dev/ttyAMA2", "baudrate": 9600, "parity": "N", "stopbits": 1},
  "tcp": {"host": "0.0.0.0", "port": 502}
}
```

- `transport`: `"rtu"` albo `"tcp"` — wybiera aktywny serwer wyjściowy.
- `rtu` / `tcp`: oba bloki mogą współistnieć w pliku (wygodne przełączanie samym polem
  `transport`, bez usuwania configu drugiej opcji), ale walidacja wymaga, żeby blok
  odpowiadający wybranemu `transport` był obecny.
- `slave_id` zostaje na poziomie `dtsu` (niezależny od transportu) — używany jako klucz
  kontekstu pymodbus i jako rejestr identyfikacyjny `Addr` (0x002E), tak jak dziś.

`config.py`:

```python
class DtsuRtuConf(BaseModel):
    port: str
    baudrate: int = 9600
    parity: str = "N"
    stopbits: int = 1

class DtsuTcpConf(BaseModel):
    host: str = "0.0.0.0"
    port: int = 502

class DtsuConf(BaseModel):
    transport: Literal["rtu", "tcp"] = "rtu"
    slave_id: int = 1
    rtu: DtsuRtuConf | None = None
    tcp: DtsuTcpConf | None = None

    @model_validator(mode="after")
    def _check_transport_config(self) -> "DtsuConf":
        if self.transport == "rtu" and self.rtu is None:
            raise ValueError("dtsu.rtu config required when transport='rtu'")
        if self.transport == "tcp" and self.tcp is None:
            raise ValueError("dtsu.tcp config required when transport='tcp'")
        return self
```

Domyślny `config/config.json` w repo zostaje z `transport: "rtu"` (zgodny z obecnym,
fizycznie okablowanym wdrożeniem), z dopisanym blokiem `tcp` jako gotowym do przełączenia.

## 4. `dtsu_server.py`

```python
from pymodbus.framer.rtu_framer import ModbusRtuFramer
from pymodbus.framer.socket_framer import ModbusSocketFramer
from pymodbus.server import ModbusSerialServer, ModbusTcpServer

def make_serial_server(cfg: DtsuConf, context) -> ModbusSerialServer:
    return ModbusSerialServer(
        context=context, framer=ModbusRtuFramer, port=cfg.rtu.port,
        baudrate=cfg.rtu.baudrate, parity=cfg.rtu.parity, stopbits=cfg.rtu.stopbits,
        bytesize=8,
    )

def make_tcp_server(cfg: DtsuConf, context) -> ModbusTcpServer:
    return ModbusTcpServer(
        context=context, framer=ModbusSocketFramer, address=(cfg.tcp.host, cfg.tcp.port),
    )

def make_server(cfg: DtsuConf, context):
    if cfg.transport == "tcp":
        return make_tcp_server(cfg, context)
    return make_serial_server(cfg, context)
```

`supervise_server`: domyślna fabryka zmienia się z `lambda: make_serial_server(cfg, context)`
na `lambda: make_server(cfg, context)`. Reszta funkcji (throttling restartów, obsługa
wyjątków z `serve_task`, watchdog fail-safe) bez zmian — potwierdzone w źródle pymodbus
3.6.9, że `ModbusTcpServer` ma te same metody `serve_forever()`/`shutdown()` co
`ModbusSerialServer` (`ModbusBaseServer` — wspólna klasa bazowa).

**Rejestr `bAud` (0x002D)** — część formatu DTSU666 (`_STATIC_INT16_REGISTERS`), zostaje
zawsze zapisywany (format bez zmian). Wartość:
- `transport == "rtu"`: jak dziś, z `cfg.rtu.baudrate` przez `_BAUD_CODES`.
- `transport == "tcp"`: stała wartość odpowiadająca 9600 (kod 3) — nie ma już fizycznego
  baudrate do zaraportowania, ale rejestr ma pozostać w mapie z sensowną, stałą wartością.

`write_static_registers` dostaje efektywny baudrate: `cfg.rtu.baudrate if cfg.transport ==
"rtu" else 9600`.

## 5. CLI / diagnostyka

- `run`, `monitor`, `diag`, `selftest` — bez zmian sygnatur; `selftest`/`monitor` używają
  `build_context`/`make_server` pośrednio przez `build_pipeline`/`supervise_server`, więc
  automatycznie serwują w transporcie z configu.
- `diagnostics.py`: komunikat `selftest` ("point mbpoll at the RTU port") zmienia się na
  neutralny wobec transportu, np. "serving synthetic DTSU data on the configured transport
  (see config); bench with mbpoll (rtu or tcp mode per config)".

## 6. Testy

`tests/test_config.py`:
- `DtsuConf` z `transport="tcp"` i brakiem `tcp=` → `ValidationError`.
- analogicznie dla `transport="rtu"` bez `rtu=`.
- poprawne budowanie obu wariantów z configu.

`tests/test_server.py`:
- istniejące testy przechodzą na nowy kształt `DtsuConf(transport="rtu", slave_id=..,
  rtu=DtsuRtuConf(port=.., baudrate=..))`.
- nowy test: `make_tcp_server` zwraca `ModbusTcpServer` z `address == (host, port)` z configu.
- nowy test: `make_server` zwraca odpowiedni typ serwera w zależności od `cfg.transport`.
- nowy test: `write_static_registers` z `transport="tcp"` zapisuje `bAud` = kod 9600 (3)
  niezależnie od tego, co jest (lub nie ma) w `cfg.rtu`.

## 7. Dokumentacja

- `README.md`: sekcja instalacji/configu opisuje pole `transport` i oba bloki `rtu`/`tcp`;
  bench-test dopisuje wariant TCP: `mbpoll -m tcp -a 1 -t 4:float -r 8193 -c 4 <host> -p
  <port>` obok istniejącego przykładu RTU.
- `CLAUDE.md`: opis architektury wspomina, że transport wyjściowy jest przełączalny w
  configu (`dtsu.transport`), z odsyłaczem do tego spec doc.
- `systemd/nd45-dtsu666.service`: bez zmian (nie odnosi się do konkretnego transportu).

## 8. Założenia / uwagi

- Wybór transportu jest per-uruchomienie procesu (czytany raz z configu przy starcie) —
  bez przełączania "na gorąco" bez restartu usługi; to zgodne z obecnym modelem (`config.json`
  czytany raz w `_cmd_run`/`_cmd_monitor`).
- `slave_id`/`Addr` (0x002E) ma sens w obu transportach: w RTU jako adres slave na
  magistrali, w TCP jako unit id w nagłówku MBAP — Sigenergy musi być skonfigurowany
  zgodnie z wybranym transportem (poza zakresem tego bridge'a).
