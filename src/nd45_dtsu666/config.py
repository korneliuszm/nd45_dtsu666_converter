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
    # How a multi-device source (etango_poller) combines this point across
    # its aggregating devices. Ignored by single-device sources (nd45,
    # huawei), where averaging or summing one value is a no-op either way.
    aggregate: Literal["avg", "sum"] = "avg"

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
    # Same meaning as SourcePoint.aggregate, applied to every target this op
    # produces (all of an op's targets share one physical kind, e.g. per-phase
    # apparent power, so one mode per op is sufficient).
    aggregate: Literal["avg", "sum"] = "avg"

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


# Canonical keys every source gets for free from canonical.compute_derived, so a
# target point may reference them even though no source section declares them.
# Duplicated here rather than imported because canonical.py imports this module;
# tests/test_config.py pins the two lists together.
DERIVED_CANONICAL_KEYS = frozenset(
    {"active_energy_total", "net_imp_energy_total", "net_exp_energy_total"}
)


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
    # Optional for the same reason: required only for a bridge whose source
    # names it (source.register_map == "etango_source").
    etango_source: SourceSide | None = None

    @model_validator(mode="after")
    def _check_every_source_produces_every_target_point(self) -> "RegisterMap":
        """Every target point's `from` must be produced by every source section.

        Without this a typo in a `from` -- or a target point added before the
        canonical point that feeds it -- is completely silent: update_datastore
        finds no value, the register keeps whatever it last held (0.0 from the
        initial datastore), and the store is still stamped fresh, so the
        freshness gate never fires. Sigenergy then regulates on a constant.

        Checked against *each* source separately, not their union: the output
        maps are shared by every bridge, so a point only nd45_source declares
        would leave that register frozen on a SmartLogger bridge alone --
        the same fault, harder to see.
        """
        sources = [
            (name, side)
            for name, side in vars(self).items()
            if isinstance(side, SourceSide)
        ]
        needed = {
            pt.from_ for _name, side in self.targets for pt in side.points.values()
        }
        for name, side in sources:
            produced = (
                set(side.points)
                | {t for op in side.derive for t in op.targets}
                | DERIVED_CANONICAL_KEYS
            )
            missing = sorted(needed - produced)
            if missing:
                raise ValueError(
                    f"source section {name!r} produces no canonical value for "
                    f"target point(s) {missing}; add the point (or a 'derive' "
                    f"rule for it) or correct the 'from' name in the output map"
                )
        return self

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
    """Shared shape of a bridge's upstream source, single- or multi-host."""

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


class _SingleHostSourceConfBase(_SourceConfBase):
    """A source polled over exactly one Modbus TCP connection."""

    host: str
    port: int = 502
    unit_id: int


class Nd45SourceConf(_SingleHostSourceConfBase):
    """Lumel ND45 power analyser: float32 registers, sub-second polling."""

    type: Literal["nd45"] = "nd45"
    unit_id: int = 1
    poll_interval_s: float = Field(default=0.3, gt=0)
    timeout_s: float = Field(default=1.0, gt=0)
    stall_timeout_s: float = Field(default=30.0, gt=0)
    register_map: str = "nd45_source"


class HuaweiSourceConf(_SingleHostSourceConfBase):
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


class EtangoDeviceConf(BaseModel):
    """One CHINT e2TANGO controller polled as part of an etango source."""

    host: str
    port: int = 502
    unit_id: int = 1
    # Signals that this device is part of the group of devices whose readings
    # are averaged/summed into the bridge's single canonical sample. A device
    # with aggregate=false is still polled every cycle (and can still fail the
    # whole sample on a read error) but its values are excluded from the
    # combination -- e.g. for a future monitoring-only controller.
    aggregate: bool = True


class EtangoSourceConf(_SourceConfBase):
    """CHINT e2TANGO protection relays used as power meters.

    Unlike Nd45SourceConf/HuaweiSourceConf this has no single host/port: see
    `devices`, each polled over its own TCP connection and combined by
    etango_poller.poll_once (averaged or summed per canonical point, per
    SourcePoint.aggregate/DeriveOp.aggregate in registers.json).

    `unit_id` below is unused by etango_poller -- it exists only so
    app.supervise_poller's generic call (which passes spec.source.unit_id as
    run_poller's `slave` for every source type) keeps working without a
    source-type branch. Per-device unit ids live in EtangoDeviceConf.unit_id.
    """

    type: Literal["etango"]
    unit_id: int = 0
    poll_interval_s: float = Field(default=2.0, gt=0)
    timeout_s: float = Field(default=3.0, gt=0)
    stall_timeout_s: float = Field(default=30.0, gt=0)
    register_map: str = "etango_source"
    devices: list[EtangoDeviceConf] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_devices(self) -> "EtangoSourceConf":
        if not self.devices:
            raise ValueError(
                "etango source must declare at least one entry in 'devices'"
            )
        if not any(d.aggregate for d in self.devices):
            raise ValueError(
                "etango source has no device with aggregate=true; nothing to combine"
            )
        return self


SourceConf = Annotated[
    Nd45SourceConf | HuaweiSourceConf | EtangoSourceConf, Field(discriminator="type")
]


class DtsuRtuConf(BaseModel):
    port: str
    baudrate: int = 9600
    parity: str = "N"
    stopbits: int = 1


class DtsuTcpConf(BaseModel):
    host: str = "0.0.0.0"
    port: int = 502


class DtsuIdentityConf(BaseModel):
    """The DTSU666 identity/config block (0x0000-0x002E).

    Every field is one *holding register*, so it has to fit an unsigned 16-bit
    word. pymodbus's datastore stores whatever it is given and only fails when
    the response is packed, which happens on Sigenergy's read of the config
    block (~every 5.4s) -- the server task then dies, restarts, and dies again
    on the next read, with a log line that points at the server rather than at
    the configuration. Bound it here instead, where the message names the field.
    """

    rev: int = Field(default=100, ge=0, le=0xFFFF)
    ucode: int = Field(default=0, ge=0, le=0xFFFF)
    clr_e: int = Field(default=0, ge=0, le=0xFFFF)
    net: int = Field(default=0, ge=0, le=0xFFFF)
    # CT ratio (register 0x0006, "IrAt"): unlike UrAt, this is used directly as
    # the primary/secondary current-transformer ratio (not x0.1-scaled) --
    # verified against a live meter's current, power, and energy-accumulation
    # readings. Doubles as the translator's single CT-ratio parameter: classic
    # DTSU666 (secondary-side) points divide by it, see TargetPoint.divide_by_ct.
    # ge=1: it is also the divisor in encode_target_point, so 0 is not merely
    # out of register range, it would make every classic-map point unencodable.
    ir_at: int = Field(default=10, ge=1, le=0xFFFF)
    ur_at: int = Field(default=10, ge=0, le=0xFFFF)
    disp: int = Field(default=0, ge=0, le=0xFFFF)
    b_lcd: int = Field(default=0, ge=0, le=0xFFFF)
    endian: int = Field(default=0, ge=0, le=0xFFFF)
    protocol: int = Field(default=0, ge=0, le=0xFFFF)


class DtsuConf(BaseModel):
    transport: Literal["rtu", "tcp"] = "rtu"
    # 1..247 is the Modbus RTU address range; it is also written to the Addr
    # register (0x002E) and into every RTU response header, both of which are
    # single bytes. Applied to the TCP transport too -- the unit id there is one
    # byte as well, and a bridge should not be reconfigurable into a slave id
    # that stops working the moment its transport changes.
    slave_id: int = Field(default=1, ge=1, le=247)
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
    # Prometheus port to use when this bridge runs as its own process
    # (`run --bridge <name>`, i.e. one systemd instance per bridge). Two services
    # cannot share prometheus.port, so each needs its own here. None falls back to
    # the process-wide prometheus.port, which is right when one process runs all
    # bridges (dev, `monitor`).
    metrics_port: int | None = Field(default=None, ge=1, le=65535)

    @model_validator(mode="after")
    def _check_enabled_is_usable(self) -> "BridgeConf":
        # Only enforced when enabled, so a bridge can ship pre-wired with an
        # empty host as a template and be turned on once the address is known.
        if not self.enabled:
            return self
        if isinstance(self.source, EtangoSourceConf):
            if not any(d.host.strip() for d in self.source.devices if d.aggregate):
                raise ValueError(
                    f"bridge {self.name!r} is enabled but has no aggregating "
                    "etango device with a non-empty host"
                )
        elif not self.source.host.strip():
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
    """Process configuration: a list of bridges plus process-wide settings.

    The current shape puts *every* bridge in `bridges`, each one a complete and
    identically structured `name`/`source`/`dtsu`/`safety`/`metrics_port` block --
    an ND45 bridge and a SmartLogger bridge differ only in the values.

    The top-level `nd45` / `dtsu` / `safety` / `primary_metrics_port` keys are the
    older shape, in which those four described the single bridge. They are still
    accepted, so a config file written before multi-bridge support keeps loading;
    when present they become the *first* bridge, named `PRIMARY_BRIDGE_NAME`.
    Read the assembled list through `bridge_specs`, never the raw fields.
    """

    bridges: list[BridgeConf] = Field(default_factory=list)
    static_debug: StaticDebugConf = StaticDebugConf()
    prometheus: PrometheusConf = PrometheusConf()  # process-wide, not per bridge

    # --- legacy single-bridge shape, kept only for back-compatibility ---
    nd45: Nd45Conf | None = None
    dtsu: DtsuConf | None = None
    safety: SafetyConf | None = None
    # Name given to the bridge assembled from the legacy top-level keys.
    PRIMARY_BRIDGE_NAME: ClassVar[str] = "nd45"
    # Its metrics port when run as its own service; None = use prometheus.port.
    primary_metrics_port: int | None = Field(default=None, ge=1, le=65535)

    def metrics_port_for(self, only: str | None) -> int:
        """Port the Prometheus endpoint binds for this process.

        With one bridge per systemd instance each service needs its own port; with
        every bridge in one process the shared prometheus.port is right.
        """
        if only is None:
            return self.prometheus.port
        for spec in self.bridge_specs:
            if spec.name == only and spec.metrics_port is not None:
                return spec.metrics_port
        return self.prometheus.port

    @property
    def legacy_bridge(self) -> BridgeConf | None:
        """The bridge described by the legacy top-level keys, if that shape is used."""
        if self.nd45 is None or self.dtsu is None:
            return None
        return BridgeConf(
            name=self.PRIMARY_BRIDGE_NAME,
            metrics_port=self.primary_metrics_port,
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
            safety=self.safety or SafetyConf(),
        )

    @property
    def bridge_specs(self) -> list[BridgeConf]:
        """Every enabled bridge, in config order.

        A legacy top-level bridge, if any, comes first. Order matters: the first
        entry is what single-bridge callers (diag, static, rtudebug) and the
        back-compat metric aliases refer to.
        """
        legacy = self.legacy_bridge
        listed = [b for b in self.bridges if b.enabled]
        return listed if legacy is None else [legacy, *listed]

    @model_validator(mode="after")
    def _check_at_least_one_bridge(self) -> "AppConfig":
        """A process with no bridge would start, poll nothing and serve nothing."""
        if self.nd45 is None and self.dtsu is None and not self.bridges:
            raise ValueError("config defines no bridges: add at least one entry to 'bridges'")
        if (self.nd45 is None) != (self.dtsu is None):
            raise ValueError(
                "legacy top-level 'nd45' and 'dtsu' must be set together "
                "(or dropped in favour of a 'bridges' entry)"
            )
        if not self.bridge_specs:
            raise ValueError("every configured bridge is disabled: enable at least one")
        return self

    @model_validator(mode="after")
    def _check_static_debug_freshness(self) -> "AppConfig":
        # static/selftest modes feed the first bridge, so that bridge's threshold
        # is the one the synthetic feed has to keep ahead of.
        max_data_age = self.bridge_specs[0].safety.max_data_age_s
        if self.static_debug.feed_interval_s >= max_data_age:
            raise ValueError(
                "static_debug.feed_interval_s must be shorter than safety.max_data_age_s"
            )
        return self

    @model_validator(mode="after")
    def _check_bridge_names_unique(self) -> "AppConfig":
        names = [b.name for b in self.bridges]
        if self.legacy_bridge is not None and self.PRIMARY_BRIDGE_NAME in names:
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
    def _check_per_bridge_metrics_ports(self) -> "AppConfig":
        """Two bridges run as separate services must not share a metrics port.

        They would race for the bind, and the loser retries silently rather than
        failing -- so one service would simply have no metrics endpoint.
        """
        seen: dict[int, str] = {}
        for spec in self.bridge_specs:
            if spec.metrics_port is None:
                continue
            owner = seen.get(spec.metrics_port)
            if owner is not None:
                raise ValueError(
                    f"bridges {owner!r} and {spec.name!r} both use metrics_port "
                    f"{spec.metrics_port}"
                )
            seen[spec.metrics_port] = spec.name
        # ...and none may collide with any bridge's Modbus TCP output
        wildcards = {"0.0.0.0", "::"}
        for spec in self.bridge_specs:
            dtsu = spec.dtsu
            if dtsu.transport != "tcp" or dtsu.tcp is None:
                continue
            owner = seen.get(dtsu.tcp.port)
            if owner is not None and (
                self.prometheus.host == dtsu.tcp.host
                or self.prometheus.host in wildcards
                or dtsu.tcp.host in wildcards
            ):
                raise ValueError(
                    f"bridge {owner!r} metrics_port {dtsu.tcp.port} collides with "
                    f"bridge {spec.name!r} dtsu.tcp.port"
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
