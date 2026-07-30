"""Pydantic config + register-map models and JSON loaders."""

from __future__ import annotations

import json
import math

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .codec import register_width

# FC03 caps a response at 125 registers; the SmartLogger manual additionally
# caps a whole Modbus-TCP frame at 256 bytes (section 4.2.2).
MAX_READ_REGISTERS = 125

STATIC_DEBUG_VALUE_KEYS = frozenset(
    {
        "u_l1",
        "u_l2",
        "u_l3",
        "u_l12",
        "u_l23",
        "u_l31",
        "i_l1",
        "i_l2",
        "i_l3",
        "p_l1",
        "p_l2",
        "p_l3",
        "p_total",
        "q_l1",
        "q_l2",
        "q_l3",
        "q_total",
        "s_l1",
        "s_l2",
        "s_l3",
        "s_total",
        "pf_l1",
        "pf_l2",
        "pf_l3",
        "pf_total",
        "freq",
        "imp_energy_total",
        "imp_energy_l1",
        "imp_energy_l2",
        "imp_energy_l3",
        "exp_energy_total",
        "exp_energy_l1",
        "exp_energy_l2",
        "exp_energy_l3",
        "reactive_imp_energy_total",
        "reactive_exp_energy_total",
    }
)


class SourcePoint(BaseModel):
    addr: int | None = None
    compose: list[int] | None = None
    factors: list[float] | None = None
    scale: float = 1.0
    offset: float = 0.0
    sign: int = 1
    # float32 is the ND45 (and default) encoding. The sized integers exist for
    # the Huawei SmartLogger, whose documented "Gain" folds into `scale`.
    dtype: Literal["float32", "u16", "i16", "u32", "i32", "u64", "i64"] = "float32"

    @model_validator(mode="after")
    def _check_shape(self) -> "SourcePoint":
        if self.compose is not None:
            if self.addr is not None:
                raise ValueError("source point cannot set both 'addr' and 'compose'")
            if len(self.compose) < 1:
                raise ValueError("source point 'compose' must list at least one address")
            # factors is optional (poll_once defaults it to all-1.0), but a
            # provided list that doesn't line up with compose would be silently
            # truncated by zip() and quietly under-count energy -- reject it.
            if self.factors is not None and len(self.factors) != len(self.compose):
                raise ValueError("source point 'factors' length must match 'compose' length")
            # compose sums float32 words (ND45 energy pairs); there is no
            # meaningful integer equivalent, and allowing it would silently
            # decode through the float path.
            if self.dtype != "float32":
                raise ValueError("source point 'compose' is only supported for dtype 'float32'")
        elif self.addr is None:
            raise ValueError("source point must set either 'addr' or 'compose'")
        return self

    @property
    def width(self) -> int:
        """Registers this point occupies."""
        return register_width(self.dtype)


class ReadGroup(BaseModel):
    """One Modbus read block. `unit_id` overrides the source's default slave.

    The SmartLogger addresses its own aggregated registers as logic device 0 and
    each attached device (power meter, inverter) as that device's RS485 address,
    so a single poll may need several blocks on different unit ids.
    """

    base: int
    count: int = Field(gt=0, le=MAX_READ_REGISTERS)
    unit_id: int | None = None


class DeriveOp(BaseModel):
    """One declarative step filling canonical points a source cannot report.

    Ops run in list order, so a step may consume what an earlier step wrote
    (e.g. hypot builds s_total, then split_equal fans it out per phase).
    """

    op: Literal[
        "constant",
        "copy",
        "split_equal",
        "phase_from_line",
        "hypot",
        "ratio_split",
        "pf_from_p_s",
    ]
    targets: list[str] = Field(min_length=1)
    from_: list[str] = Field(default_factory=list, alias="from")
    weights: list[str] = Field(default_factory=list)
    value: float | None = None

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _check_arity(self) -> "DeriveOp":
        n, sources = len(self.targets), len(self.from_)
        if self.op == "constant":
            if self.value is None or not math.isfinite(self.value):
                raise ValueError("derive 'constant' requires a finite 'value'")
            if sources:
                raise ValueError("derive 'constant' takes no 'from'")
        elif self.op in ("copy", "split_equal", "ratio_split"):
            if sources != 1:
                raise ValueError(f"derive {self.op!r} requires exactly one 'from' point")
        elif self.op == "phase_from_line":
            if sources != n:
                raise ValueError("derive 'phase_from_line' needs one 'from' per target")
        else:  # hypot, pf_from_p_s -- consume an (a, b) pair per target
            if sources != 2 * n:
                raise ValueError(f"derive {self.op!r} needs two 'from' points per target")
        if self.op == "ratio_split":
            if len(self.weights) != n:
                raise ValueError("derive 'ratio_split' needs one 'weights' entry per target")
        elif self.weights:
            raise ValueError(f"derive {self.op!r} does not take 'weights'")
        if self.op != "constant" and self.value is not None:
            raise ValueError(f"derive {self.op!r} does not take 'value'")
        return self


class TargetPoint(BaseModel):
    addr: int
    from_: str = Field(alias="from")
    scale: float = 1.0
    offset: float = 0.0
    sign: int = 1
    # True for classic DTSU666 (secondary/CT-side) points that must be divided
    # by the configured CT ratio (dtsu.identity.ir_at) before scaling -- see
    # update_datastore. Sigen OEM points read primary-side values directly and
    # leave this False.
    divide_by_ct: bool = False
    # Physical TPX-CH coarse energy aliases expose only the IEEE754 high word.
    zero_low_word: bool = False

    model_config = {"populate_by_name": True}


class SourceSide(BaseModel):
    word_order: str = "big"
    byte_order: str = "big"
    points: dict[str, SourcePoint]
    # Read blocks. None keeps the ND45 behaviour of nd45_poller.READ_GROUPS;
    # a Huawei source declares its own here so the map stays editable without
    # touching code (see CLAUDE.md on registers.json).
    read_groups: list[ReadGroup] | None = None
    # Added to every base and point address on the wire. Vendors disagree on
    # whether a documented "40525" is the register number or the PDU address;
    # this is the on-site correction knob for that off-by-one.
    address_offset: int = 0
    derive: list[DeriveOp] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_points_covered_by_read_groups(self) -> "SourceSide":
        """Fail at load time if a declared point falls outside every read block.

        Same rationale as nd45_poller.validate_source_coverage: an uncovered
        address would raise on every poll, and the fault reporter mutes repeats,
        leaving a permanent fail-safe indistinguishable from a real outage.
        Addresses are checked as documented -- address_offset shifts blocks and
        points together, so it cannot change coverage.
        """
        if self.read_groups is None:
            return self
        spans = [(g.base, g.base + g.count) for g in self.read_groups]
        uncovered = sorted(
            {
                pt.addr
                for pt in self.points.values()
                if pt.addr is not None
                and not any(lo <= pt.addr and pt.addr + pt.width <= hi for lo, hi in spans)
            }
        )
        if uncovered:
            raise ValueError(
                f"source addresses {uncovered} are not fully covered by read_groups "
                f"{[(g.base, g.count) for g in self.read_groups]}"
            )
        return self


class TargetSide(BaseModel):
    word_order: str = "big"
    byte_order: str = "big"
    function_code: Literal[3, 4] = 3
    points: dict[str, TargetPoint]

    @model_validator(mode="after")
    def _check_point_ranges_do_not_overlap(self) -> "TargetSide":
        occupied: dict[int, str] = {}
        for name, point in self.points.items():
            for addr in (point.addr, point.addr + 1):
                if addr in occupied:
                    raise ValueError(
                        f"target points {occupied[addr]!r} and {name!r} overlap at {addr}"
                    )
                occupied[addr] = name
        return self


class StaticIdentityPoint(BaseModel):
    addr: int
    type: Literal["ascii", "uint32"]
    static_value: str | int
    length: int | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> "StaticIdentityPoint":
        if self.type == "ascii":
            if not isinstance(self.static_value, str) or self.length is None or self.length < 1:
                raise ValueError(
                    "ascii static point requires a string value and positive register length"
                )
            try:
                byte_length = len(self.static_value.encode("ascii"))
            except UnicodeEncodeError as exc:
                raise ValueError("ascii static value must contain ASCII characters only") from exc
            if byte_length > self.length * 2:
                raise ValueError("ascii static value does not fit configured register length")
        else:
            if (
                not isinstance(self.static_value, int)
                or isinstance(self.static_value, bool)
                or self.length is not None
                or not 0 <= self.static_value <= 0xFFFFFFFF
            ):
                raise ValueError(
                    "uint32 static point requires a 0..0xFFFFFFFF integer and no length"
                )
        return self

    @property
    def register_count(self) -> int:
        return self.length if self.type == "ascii" else 2


class StaticIdentitySide(BaseModel):
    function_code: Literal[3] = 3
    points: dict[str, StaticIdentityPoint]


class RegisterMap(BaseModel):
    nd45_source: SourceSide
    dtsu_target: TargetSide
    dtsu_sigen_ext_target: TargetSide
    dtsu_sigen_ext_energy: TargetSide
    dtsu_sigen_identity: StaticIdentitySide
    # Optional so every shipped config_debug_*.json and existing registers.json
    # keeps validating untouched. Required only when huawei.enabled is set.
    huawei_plant_source: SourceSide | None = None
    huawei_meter_source: SourceSide | None = None


class Nd45Conf(BaseModel):
    host: str
    port: int = 502
    unit_id: int = 1
    poll_interval_s: float = 0.3
    timeout_s: float = 1.0
    reconnect_delay_s: float = 1.0  # initial backoff for startup connect retry
    reconnect_delay_max_s: float = 30.0  # max backoff for startup connect retry


class HuaweiConf(BaseModel):
    """Huawei SmartLogger as a secondary data source (Modbus TCP, port 502).

    Disabled by default: with `enabled` false the bridge builds and behaves
    exactly as it did before this source existed.

    `max_data_age_s` is deliberately separate from (and much looser than)
    safety.max_data_age_s. The SmartLogger is a concentrator that aggregates
    inverter data over RS485, so its registers refresh in seconds, not the
    ~0.3s of a directly wired analyser -- and its own manual allows a 5s Modbus
    timeout. This threshold only marks the PV telemetry stale in metrics and the
    dashboard; it never gates the DTSU output, which follows the ND45 alone.
    """

    enabled: bool = False
    host: str = ""
    port: int = 502
    # SmartLogger's own aggregated plant registers are logic device 0.
    plant_unit_id: int | None = 0
    # RS485 address of a power meter behind the SmartLogger; None = don't read.
    meter_unit_id: int | None = None
    poll_interval_s: float = Field(default=5.0, gt=0)
    timeout_s: float = Field(default=6.0, gt=0)  # manual: 5s Modbus timeout
    max_data_age_s: float = Field(default=60.0, gt=0)
    reconnect_delay_s: float = 1.0
    reconnect_delay_max_s: float = 30.0

    @model_validator(mode="after")
    def _check_enabled_is_usable(self) -> "HuaweiConf":
        if not self.enabled:
            return self
        if not self.host.strip():
            raise ValueError("huawei.host is required when huawei.enabled is true")
        if self.plant_unit_id is None and self.meter_unit_id is None:
            raise ValueError(
                "huawei.enabled needs at least one of plant_unit_id / meter_unit_id"
            )
        return self


class DtsuRtuConf(BaseModel):
    port: str
    baudrate: int = 9600
    parity: str = "N"
    stopbits: int = 1


class DtsuTcpConf(BaseModel):
    host: str = "0.0.0.0"
    port: int = 502


class DtsuIdentityConf(BaseModel):
    rev: int = 100
    ucode: int = 0
    clr_e: int = 0
    net: int = 0
    # CT ratio (register 0x0006, "IrAt"): unlike UrAt, this is used directly as
    # the primary/secondary current-transformer ratio (not x0.1-scaled) --
    # verified against a live meter's current, power, and energy-accumulation
    # readings. Doubles as the translator's single CT-ratio parameter: classic
    # DTSU666 (secondary-side) points divide by it, see TargetPoint.divide_by_ct.
    ir_at: int = 10
    ur_at: int = 10
    disp: int = 0
    b_lcd: int = 0
    endian: int = 0
    protocol: int = 0

    @field_validator("ir_at")
    @classmethod
    def _check_ir_at_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("dtsu.identity.ir_at (CT ratio) must be positive")
        return value


class DtsuConf(BaseModel):
    transport: Literal["rtu", "tcp"] = "rtu"
    slave_id: int = 1
    identity: DtsuIdentityConf = DtsuIdentityConf()
    rtu: DtsuRtuConf | None = None
    tcp: DtsuTcpConf | None = None

    @model_validator(mode="after")
    def _check_transport_config(self) -> "DtsuConf":
        if self.transport == "rtu" and self.rtu is None:
            raise ValueError("dtsu.rtu config required when transport='rtu'")
        if self.transport == "tcp" and self.tcp is None:
            raise ValueError("dtsu.tcp config required when transport='tcp'")
        return self


class SafetyConf(BaseModel):
    max_data_age_s: float = 3.0
    check_interval_s: float = 0.5
    min_restart_interval_s: float = 5.0  # min gap between DTSU server (re)starts (anti-flap)


class StaticDebugConf(BaseModel):
    feed_interval_s: float = Field(default=0.5, gt=0)
    values: dict[str, float] = Field(default_factory=dict)

    @field_validator("values", mode="before")
    @classmethod
    def _validate_values(cls, values):
        if not isinstance(values, dict):
            raise ValueError("static debug values must be an object")
        unknown = sorted(set(values) - STATIC_DEBUG_VALUE_KEYS)
        if unknown:
            raise ValueError(f"unknown static debug value(s): {', '.join(unknown)}")
        validated: dict[str, float] = {}
        for name, value in values.items():
            if type(value) not in (int, float) or not math.isfinite(float(value)):
                raise ValueError(f"static debug value {name!r} must be a finite number")
            validated[name] = float(value)
        return validated


class PrometheusConf(BaseModel):
    """Read-only Prometheus scrape endpoint (see metrics.py)."""

    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = Field(default=9090, ge=1, le=65535)
    # Per-register gauges (one series per DTSU output point). Cheap for this
    # map size, but switchable off if the scrape ever needs to be minimal.
    include_registers: bool = True


class AppConfig(BaseModel):
    nd45: Nd45Conf
    dtsu: DtsuConf
    safety: SafetyConf = SafetyConf()
    static_debug: StaticDebugConf = StaticDebugConf()
    prometheus: PrometheusConf = PrometheusConf()
    huawei: HuaweiConf = HuaweiConf()

    @model_validator(mode="after")
    def _check_static_debug_freshness(self) -> "AppConfig":
        if self.static_debug.feed_interval_s >= self.safety.max_data_age_s:
            raise ValueError(
                "static_debug.feed_interval_s must be shorter than safety.max_data_age_s"
            )
        return self

    @model_validator(mode="after")
    def _check_metrics_port_free(self) -> "AppConfig":
        """Reject a metrics port that collides with the Modbus TCP output.

        Without this the DTSU TCP listener simply fails to bind and the bridge
        sits in fail-safe for a reason that is not obvious from the logs.
        """
        if not self.prometheus.enabled or self.dtsu.transport != "tcp" or self.dtsu.tcp is None:
            return self
        if self.prometheus.port != self.dtsu.tcp.port:
            return self
        wildcards = {"0.0.0.0", "::"}
        overlap = (
            self.prometheus.host == self.dtsu.tcp.host
            or self.prometheus.host in wildcards
            or self.dtsu.tcp.host in wildcards
        )
        if overlap:
            raise ValueError(
                f"prometheus.port {self.prometheus.port} collides with dtsu.tcp.port"
            )
        return self


def load_registers(path: str) -> RegisterMap:
    with open(path, encoding="utf-8") as f:
        return RegisterMap.model_validate(json.load(f))


def load_config(path: str) -> AppConfig:
    with open(path, encoding="utf-8") as f:
        return AppConfig.model_validate(json.load(f))
