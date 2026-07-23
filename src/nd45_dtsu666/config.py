"""Pydantic config + register-map models and JSON loaders."""

from __future__ import annotations

import json

from typing import Literal

from pydantic import BaseModel, Field, model_validator


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
    ir_at: int = 10
    ur_at: int = 10
    disp: int = 0
    b_lcd: int = 0
    endian: int = 0
    protocol: int = 0


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
