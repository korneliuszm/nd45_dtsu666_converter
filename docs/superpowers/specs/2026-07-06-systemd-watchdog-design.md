# Spec: systemd watchdog integration (sd_notify heartbeat)

**Data:** 2026-07-06
**Status:** zatwierdzony design
**Kontekst:** stabilność długoterminowa — `Restart=always` w service już łapie crash
procesu, ale nie łapie zawieszenia (proces żyje, ale utknął). Urządzenie ma
skonfigurowanego sprzętowego watchdoga na poziomie systemu, ale to chroni tylko
przed zamrożeniem całego systemu, nie wie nic o kondycji tej konkretnej usługi.

## 1. Cel

Dodać integrację z watchdogiem systemd (`sd_notify` + `WatchdogSec=`) w service,
tak by systemd wykrywał i restartował usługę, gdy pętla pollera ND45 faktycznie
się zawiesi — nie tylko gdy proces padnie. Format danych, register mapy i reszta
architektury bez zmian.

## 2. Zakres (YAGNI)

- Tylko `run` (jedyna komenda uruchamiana pod systemd) korzysta realnie z
  watchdoga; `monitor`/`diag`/`selftest` dzielą ten sam `build_pipeline`, ale
  pętla watchdoga i tak się nie uruchomi bez zmiennych env ustawianych przez
  systemd (`WATCHDOG_USEC`/`NOTIFY_SOCKET`).
- Bez nowych pól w `config/config.json` — interwał watchdoga czytany jest z
  `WATCHDOG_USEC` (ustawianego automatycznie przez systemd na podstawie
  `WatchdogSec=` w unicie), żeby uniknąć rozjazdu między plikami configu.
- Bez nowej zależności PyPI (`sdnotify` itp.) — protokół `sd_notify` to prosty
  datagram na gniazdo unixowe, ręczna implementacja to ~15 linii.
- Bez UDP/TLS/innych transportów notyfikacji — poza zakresem.

## 3. Definicja "żywotności" (kluczowa decyzja)

asyncio jest kooperacyjne: sam fakt, że pętla zdarzeń odpowiada, nie gwarantuje,
że poller ND45 robi postęp (może utknąć w jednym `await` bez timeoutu, a inne
taski i tak będą działać dalej). Dlatego heartbeat śledzi **postęp pollera ND45**,
a nie samą żywotność event loopa:

- `connect_with_retry` dotyka heartbeat na **każdej** iteracji (przed próbą
  połączenia) — dzięki temu wolny start (ND45 wstaje z opóźnieniem po awarii
  prądu) też liczy się jako "żyję", nie tylko udany odczyt.
- `on_update`/`on_error` w `app.py` dotykają heartbeat **niezależnie od wyniku**
  odczytu ND45 — podczas realnej awarii sieci ND45 poller poprawnie kręci się w
  pętli obsługi błędu (to oczekiwany stan fail-safe), więc watchdog nie może
  tego karać restartem całej usługi.

Watchdog przestaje pingować systemd tylko, gdy heartbeat nie był dotknięty
dłużej niż pełny `WatchdogSec` — czyli gdy poller faktycznie utknął.

## 4. Moduł `src/nd45_dtsu666/watchdog.py` (nowy)

Jedna odpowiedzialność: protokół `sd_notify` + śledzenie żywotności, bez
zależności od reszty aplikacji.

```python
class Heartbeat:
    def touch(self, ts: float) -> None: ...
    def age(self, now: float) -> float: ...   # math.inf jeśli nigdy nie dotknięty

def notify_ready() -> None: ...       # wysyła "READY=1"; no-op bez NOTIFY_SOCKET
def notify_watchdog() -> None: ...    # wysyła "WATCHDOG=1"; no-op bez NOTIFY_SOCKET
def watchdog_seconds() -> float | None: ...  # parsuje WATCHDOG_USEC z env

async def watchdog_loop(
    heartbeat: Heartbeat, watchdog_sec: float, stop_event: asyncio.Event,
    now: Callable[[], float] = time.monotonic,
) -> None:
    """Co watchdog_sec/2 (rekomendacja systemd): ping tylko jeśli
    heartbeat.age(now()) <= watchdog_sec; inaczej cisza -> systemd sam
    zabija i restartuje usługę (Restart=always już to obsługuje)."""
```

`_notify(payload: str)` (prywatny helper): datagram `AF_UNIX`/`SOCK_DGRAM` na
adres z `NOTIFY_SOCKET`; adresy zaczynające się od `@` to gniazda w "abstract
namespace" — zamieniane na wiodący bajt `\0` zgodnie z konwencją Linuksa. Brak
`NOTIFY_SOCKET` w env → cichy no-op (zgodnie z kontraktem `sd_notify(3)`).
Błąd wysyłki (`OSError`) → log warning, nie wyjątek (nie może wywalić pętli
aplikacji).

## 5. Integracja w `app.py`

- `connect_with_retry(client, stop_event, delay=1.0, max_delay=30.0,
  heartbeat: Heartbeat | None = None)` — dotyka `heartbeat` (jeśli podany) na
  początku każdej iteracji pętli, przed próbą `client.connect()`.
- `build_pipeline`:
  - tworzy `Heartbeat()`;
  - w domknięciu `on_update` dotyka heartbeat obok istniejącego
    `reporter.success()`;
  - dodaje nowe domknięcie `on_error` (dotyka heartbeat, potem woła
    `reporter.failure(exc)`) i przekazuje je do `run_poller` zamiast
    bezpośrednio `reporter.failure`;
  - jeśli `watchdog_seconds()` zwróci wartość — dokłada
    `watchdog_loop(heartbeat, watchdog_sec, stop_event)` do `pipe.coros`;
  - `Pipeline` (dataclass) dostaje nowe pole `heartbeat: Heartbeat`.
- `run_app`: wywołuje `notify_ready()` raz na starcie (przed
  `connect_with_retry`), przekazuje `pipe.heartbeat` do `connect_with_retry`.
- `monitor.run_monitor`: bez zmian sygnatur — korzysta z tego samego
  `build_pipeline`; przy ręcznym uruchomieniu `WATCHDOG_USEC` nie jest
  ustawione, więc `watchdog_loop` po prostu nie trafia do `coros`.

## 6. `systemd/nd45-dtsu666.service`

```ini
[Service]
Type=notify
WatchdogSec=90
# reszta (ExecStart, Restart=always, RestartSec=2, StandardOutput/Error) bez zmian
```

**Skąd 90s:** najdłuższa *legalna* przerwa między dotknięciami heartbeat to
czas oczekiwania na reconnect ND45 (`reconnect_delay_max_s`, domyślnie 30s) +
czas jednej próby połączenia (`timeout_s`, domyślnie 1s) ≈ 31s w najgorszym
razie podczas normalnej (nie-awaryjnej z punktu widzenia aplikacji) przerwy w
łączności z ND45. `WatchdogSec=90` daje ~3× zapas — nie zrestartuje usługi
podczas oczekiwanego cyklu ponawiania połączenia, a wykryje realne zawieszenie
w niecałe 2–3 minuty (ping co 45s + do 90s ciszy przed timeoutem).
`Restart=always`/`RestartSec=2` (bez zmian) obsługuje restart po timeoucie
watchdoga tak samo jak po zwykłym crashu.

## 7. Testy

- `tests/test_watchdog.py` (nowy):
  - `Heartbeat.touch`/`age` (w tym stan początkowy = `math.inf`).
  - `notify_ready`/`notify_watchdog` na prawdziwym lokalnym gnieździe unixowym
    (bind tymczasowego gniazda, ustawienie `NOTIFY_SOCKET` przez monkeypatch,
    odbiór i weryfikacja datagramu) — bez mocków.
  - `notify_ready`/`notify_watchdog` bez `NOTIFY_SOCKET` w env → no-op, brak
    wyjątku.
  - `watchdog_seconds()` — parsuje poprawną wartość `WATCHDOG_USEC`, zwraca
    `None` gdy brak/niepoprawna wartość.
  - `watchdog_loop` z fałszywym zegarem: pinguje (datagram faktycznie
    przychodzi) gdy heartbeat świeży; milczy (brak datagramu) gdy heartbeat
    starszy niż `watchdog_sec`.
- `tests/test_app.py`:
  - `connect_with_retry` dotyka przekazany `heartbeat` na każdej próbie.
  - `build_pipeline` dokłada `watchdog_loop` do `pipe.coros` tylko gdy
    `WATCHDOG_USEC` ustawione w env (monkeypatch); bez tej zmiennej —
    `pipe.coros` ma dokładnie 2 elementy (poller + supervisor), tak jak dziś.
  - `on_error` (nowe domknięcie w `build_pipeline`) dotyka heartbeat i nadal
    woła `reporter.failure`.

## 8. Założenia / uwagi

- Watchdog jest opt-in przez obecność `WatchdogSec=` w unicie; usunięcie tej
  linii (albo uruchomienie poza systemd) całkowicie wyłącza mechanizm bez
  żadnych zmian w kodzie czy configu aplikacji.
- `READY=1` wysyłane jest natychmiast na starcie `run_app`, niezależnie od
  tego, czy ND45 jest akurat osiągalne — fail-safe (ND45 nieosiągalne = cichy,
  poprawny stan) jest częścią normalnego działania usługi, nie fazy startu.
