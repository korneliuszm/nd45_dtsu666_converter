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


class SafetyConf(BaseModel):
    max_data_age_s: float = 3.0
    check_interval_s: float = 0.5
    min_restart_interval_s: float = 5.0  # min gap between RTU server (re)starts (anti-flap)


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
