"""Pydantic config + register-map models and JSON loaders."""

from __future__ import annotations

import json
import math

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


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
        "net_imp_energy_total",
        "net_exp_energy_total",
    }
)


class SourcePoint(BaseModel):
    addr: int | None = None
    compose: list[int] | None = None
    factors: list[float] | None = None
    scale: float = 1.0
    offset: float = 0.0
    sign: int = 1

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
        elif self.addr is None:
            raise ValueError("source point must set either 'addr' or 'compose'")
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


class Nd45Conf(BaseModel):
    host: str
    port: int = 502
    unit_id: int = 1
    poll_interval_s: float = 0.3
    timeout_s: float = 1.0
    reconnect_delay_s: float = 1.0  # initial backoff for startup connect retry
    reconnect_delay_max_s: float = 30.0  # max backoff for startup connect retry


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


class AppConfig(BaseModel):
    nd45: Nd45Conf
    dtsu: DtsuConf
    safety: SafetyConf = SafetyConf()
    static_debug: StaticDebugConf = StaticDebugConf()

    @model_validator(mode="after")
    def _check_static_debug_freshness(self) -> "AppConfig":
        if self.static_debug.feed_interval_s >= self.safety.max_data_age_s:
            raise ValueError(
                "static_debug.feed_interval_s must be shorter than safety.max_data_age_s"
            )
        return self


def load_registers(path: str) -> RegisterMap:
    with open(path, encoding="utf-8") as f:
        return RegisterMap.model_validate(json.load(f))


def load_config(path: str) -> AppConfig:
    with open(path, encoding="utf-8") as f:
        return AppConfig.model_validate(json.load(f))
