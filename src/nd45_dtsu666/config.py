"""Pydantic config + register-map models and JSON loaders."""

from __future__ import annotations

import json
import math

from typing import Annotated, ClassVar, Literal

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
    # Optional so a registers.json written before the SmartLogger source landed
    # keeps validating. Required only for a bridge whose source names it.
    huawei_plant_source: SourceSide | None = None
    huawei_meter_source: SourceSide | None = None

    # The output maps are shared by every bridge -- they all emulate the same
    # meter. Only the source section and the runtime CT ratio differ.
    @property
    def targets(self) -> list[tuple[str, TargetSide]]:
        """(map name, side) pairs served by every bridge; name becomes a label."""
        return [
            ("dtsu_target", self.dtsu_target),
            ("dtsu_sigen_ext_target", self.dtsu_sigen_ext_target),
            ("dtsu_sigen_ext_energy", self.dtsu_sigen_ext_energy),
        ]

    def source_by_name(self, name: str) -> SourceSide:
        """Resolve a bridge's `source.register_map` to its SourceSide.

        Raises rather than falling back, so a typo or a missing optional section
        fails at startup instead of presenting as a bridge that never polls.
        """
        side = getattr(self, name, None)
        if not isinstance(side, SourceSide):
            available = sorted(
                key
                for key, value in vars(self).items()
                if isinstance(value, SourceSide)
            )
            raise ValueError(
                f"registers.json has no source section {name!r} "
                f"(available: {', '.join(available)})"
            )
        return side


class Nd45Conf(BaseModel):
    host: str
    port: int = 502
    unit_id: int = 1
    poll_interval_s: float = 0.3
    timeout_s: float = 1.0
    reconnect_delay_s: float = 1.0  # initial backoff for startup connect retry
    reconnect_delay_max_s: float = 30.0  # max backoff for startup connect retry


class _SourceConfBase(BaseModel):
    """Shared shape of a bridge's upstream Modbus TCP source."""

    host: str
    port: int = 502
    unit_id: int
    poll_interval_s: float = Field(gt=0)
    timeout_s: float = Field(gt=0)
    reconnect_delay_s: float = 1.0  # initial backoff for startup connect retry
    reconnect_delay_max_s: float = 30.0  # max backoff for startup connect retry
    # No poller progress for this long => the poll loop is treated as hung and
    # its client is closed and rebuilt. Distinct from an unreachable source,
    # which the poller already survives by cycling through its error path.
    stall_timeout_s: float = Field(gt=0)
    # Name of the registers.json section this source decodes.
    register_map: str

    @model_validator(mode="after")
    def _check_stall_timeout_exceeds_poll_interval(self) -> "_SourceConfBase":
        # A stall timeout at or below the poll interval would fire mid-cycle and
        # rebuild the client forever, on a source that is working fine.
        if self.stall_timeout_s <= self.poll_interval_s * 2:
            raise ValueError(
                f"stall_timeout_s ({self.stall_timeout_s}) must exceed twice "
                f"poll_interval_s ({self.poll_interval_s})"
            )
        return self


class Nd45SourceConf(_SourceConfBase):
    """Lumel ND45 power analyser: float32 registers, sub-second polling."""

    type: Literal["nd45"] = "nd45"
    unit_id: int = 1
    poll_interval_s: float = Field(default=0.3, gt=0)
    timeout_s: float = Field(default=1.0, gt=0)
    stall_timeout_s: float = Field(default=30.0, gt=0)
    register_map: str = "nd45_source"


class HuaweiSourceConf(_SourceConfBase):
    """Huawei SmartLogger: sized-integer registers, seconds-scale refresh.

    The SmartLogger is a concentrator that aggregates inverter data over RS485,
    so its registers refresh far more slowly than a directly wired analyser's,
    and its own manual (section 4.2.4) allows a 5s Modbus timeout. A bridge fed
    from it therefore needs a much looser `safety.max_data_age_s` than the ND45
    bridge -- see AppConfig._check_freshness_threshold_allows_polling.

    `unit_id` selects which device the SmartLogger answers for: 0 is its own
    aggregated plant registers, 1-247 an attached device's RS485 address.
    """

    type: Literal["huawei"]
    unit_id: int = 0
    poll_interval_s: float = Field(default=5.0, gt=0)
    timeout_s: float = Field(default=6.0, gt=0)
    stall_timeout_s: float = Field(default=60.0, gt=0)
    register_map: str = "huawei_plant_source"


SourceConf = Annotated[Nd45SourceConf | HuaweiSourceConf, Field(discriminator="type")]


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


class BridgeConf(BaseModel):
    """One complete, independent source -> DTSU666 bridge.

    Each bridge owns its upstream source, its own canonical store, its own
    served datastore and its own output transport, and its fail-safe is judged
    only against its own source. Nothing is shared with a sibling bridge, so one
    source going dark silences that bridge's output alone.
    """

    name: str = Field(min_length=1)
    enabled: bool = True
    source: SourceConf
    dtsu: DtsuConf
    safety: SafetyConf = SafetyConf()

    @model_validator(mode="after")
    def _check_enabled_is_usable(self) -> "BridgeConf":
        # Only enforced when enabled, so a bridge can ship pre-wired with an
        # empty host as a template and be turned on once the address is known.
        if self.enabled and not self.source.host.strip():
            raise ValueError(
                f"bridge {self.name!r} is enabled but source.host is empty"
            )
        return self


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
    """Process configuration: one or more bridges plus process-wide settings.

    The top-level `nd45` / `dtsu` / `safety` keys describe the first bridge and
    are kept as-is, so every config file written before multi-bridge support
    still loads unchanged. Additional bridges go in `bridges`. Read the assembled
    list through `bridge_specs`, never the raw fields.
    """

    nd45: Nd45Conf
    dtsu: DtsuConf
    safety: SafetyConf = SafetyConf()
    # Bridges beyond the legacy top-level one.
    bridges: list[BridgeConf] = Field(default_factory=list)
    static_debug: StaticDebugConf = StaticDebugConf()
    prometheus: PrometheusConf = PrometheusConf()  # process-wide, not per bridge

    # Name given to the bridge assembled from the legacy top-level keys.
    PRIMARY_BRIDGE_NAME: ClassVar[str] = "nd45"

    @property
    def bridge_specs(self) -> list[BridgeConf]:
        """Every enabled bridge: the legacy top-level one first, then `bridges`.

        Order matters: the first entry is what single-bridge callers (diag,
        static, rtudebug) and the back-compat metric aliases refer to.
        """
        primary = BridgeConf(
            name=self.PRIMARY_BRIDGE_NAME,
            source=Nd45SourceConf(
                host=self.nd45.host,
                port=self.nd45.port,
                unit_id=self.nd45.unit_id,
                poll_interval_s=self.nd45.poll_interval_s,
                timeout_s=self.nd45.timeout_s,
                reconnect_delay_s=self.nd45.reconnect_delay_s,
                reconnect_delay_max_s=self.nd45.reconnect_delay_max_s,
            ),
            dtsu=self.dtsu,
            safety=self.safety,
        )
        return [primary, *(b for b in self.bridges if b.enabled)]

    @model_validator(mode="after")
    def _check_static_debug_freshness(self) -> "AppConfig":
        if self.static_debug.feed_interval_s >= self.safety.max_data_age_s:
            raise ValueError(
                "static_debug.feed_interval_s must be shorter than safety.max_data_age_s"
            )
        return self

    @model_validator(mode="after")
    def _check_bridge_names_unique(self) -> "AppConfig":
        names = [b.name for b in self.bridges]
        if self.PRIMARY_BRIDGE_NAME in names:
            raise ValueError(
                f"bridge name {self.PRIMARY_BRIDGE_NAME!r} is reserved for the "
                "bridge built from the top-level nd45/dtsu/safety keys"
            )
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(f"duplicate bridge name(s): {', '.join(duplicates)}")
        return self

    @model_validator(mode="after")
    def _check_output_transports_do_not_collide(self) -> "AppConfig":
        """Two bridges must not share an output transport.

        The serial case is the dangerous one: two RTU servers on one /dev/tty*
        would fight over the device, and pymodbus 3.6.9's listen() swallows the
        resulting OSError, so the loser would hang silently rather than fail.
        Catch it at load time instead.
        """
        serial_ports: dict[str, str] = {}
        tcp_binds: dict[tuple[str, int], str] = {}
        for bridge in self.bridge_specs:
            dtsu = bridge.dtsu
            if dtsu.transport == "rtu" and dtsu.rtu is not None:
                owner = serial_ports.get(dtsu.rtu.port)
                if owner is not None:
                    raise ValueError(
                        f"bridges {owner!r} and {bridge.name!r} both serve RTU on "
                        f"{dtsu.rtu.port}; each bridge needs its own serial port"
                    )
                serial_ports[dtsu.rtu.port] = bridge.name
            elif dtsu.transport == "tcp" and dtsu.tcp is not None:
                key = (dtsu.tcp.host, dtsu.tcp.port)
                owner = tcp_binds.get(key)
                if owner is not None:
                    raise ValueError(
                        f"bridges {owner!r} and {bridge.name!r} both listen on "
                        f"{dtsu.tcp.host}:{dtsu.tcp.port}"
                    )
                tcp_binds[key] = bridge.name
        return self

    @model_validator(mode="after")
    def _check_metrics_port_free(self) -> "AppConfig":
        """Reject a metrics port that collides with any bridge's Modbus TCP output.

        Without this the DTSU TCP listener simply fails to bind and the bridge
        sits in fail-safe for a reason that is not obvious from the logs.
        """
        if not self.prometheus.enabled:
            return self
        wildcards = {"0.0.0.0", "::"}
        for bridge in self.bridge_specs:
            dtsu = bridge.dtsu
            if dtsu.transport != "tcp" or dtsu.tcp is None:
                continue
            if self.prometheus.port != dtsu.tcp.port:
                continue
            overlap = (
                self.prometheus.host == dtsu.tcp.host
                or self.prometheus.host in wildcards
                or dtsu.tcp.host in wildcards
            )
            if overlap:
                raise ValueError(
                    f"prometheus.port {self.prometheus.port} collides with "
                    f"bridge {bridge.name!r} dtsu.tcp.port"
                )
        return self

    @model_validator(mode="after")
    def _check_freshness_threshold_allows_polling(self) -> "AppConfig":
        """Every bridge's fail-safe threshold must leave room for a poll cycle.

        A max_data_age_s at or near the poll interval parks the bridge in
        permanent fail-safe, which in the field is indistinguishable from a dead
        source. This matters most for a SmartLogger bridge: the ND45's 3.0s is
        ample at 0.3s polling, but reusing it for a source polled every 5s would
        silence that output forever.
        """
        for bridge in self.bridge_specs:
            floor = bridge.source.poll_interval_s * 2
            if bridge.safety.max_data_age_s < floor:
                raise ValueError(
                    f"bridge {bridge.name!r}: safety.max_data_age_s "
                    f"({bridge.safety.max_data_age_s}) must be at least twice "
                    f"source.poll_interval_s ({bridge.source.poll_interval_s})"
                )
        return self


def load_registers(path: str) -> RegisterMap:
    with open(path, encoding="utf-8") as f:
        return RegisterMap.model_validate(json.load(f))


def load_config(path: str) -> AppConfig:
    with open(path, encoding="utf-8") as f:
        return AppConfig.model_validate(json.load(f))
