from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class BlockRef(_message.Message):
    __slots__ = ("link", "unit_id", "function_code", "base", "count")
    LINK_FIELD_NUMBER: _ClassVar[int]
    UNIT_ID_FIELD_NUMBER: _ClassVar[int]
    FUNCTION_CODE_FIELD_NUMBER: _ClassVar[int]
    BASE_FIELD_NUMBER: _ClassVar[int]
    COUNT_FIELD_NUMBER: _ClassVar[int]
    link: str
    unit_id: int
    function_code: int
    base: int
    count: int
    def __init__(self, link: _Optional[str] = ..., unit_id: _Optional[int] = ..., function_code: _Optional[int] = ..., base: _Optional[int] = ..., count: _Optional[int] = ...) -> None: ...

class BlockSubscription(_message.Message):
    __slots__ = ("block", "period_ms")
    BLOCK_FIELD_NUMBER: _ClassVar[int]
    PERIOD_MS_FIELD_NUMBER: _ClassVar[int]
    block: BlockRef
    period_ms: int
    def __init__(self, block: _Optional[_Union[BlockRef, _Mapping]] = ..., period_ms: _Optional[int] = ...) -> None: ...

class SubscribeRequest(_message.Message):
    __slots__ = ("client_id", "blocks")
    CLIENT_ID_FIELD_NUMBER: _ClassVar[int]
    BLOCKS_FIELD_NUMBER: _ClassVar[int]
    client_id: str
    blocks: _containers.RepeatedCompositeFieldContainer[BlockSubscription]
    def __init__(self, client_id: _Optional[str] = ..., blocks: _Optional[_Iterable[_Union[BlockSubscription, _Mapping]]] = ...) -> None: ...

class BlockUpdate(_message.Message):
    __slots__ = ("block", "registers", "read_at_unix_nanos", "error")
    BLOCK_FIELD_NUMBER: _ClassVar[int]
    REGISTERS_FIELD_NUMBER: _ClassVar[int]
    READ_AT_UNIX_NANOS_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    block: BlockRef
    registers: _containers.RepeatedScalarFieldContainer[int]
    read_at_unix_nanos: int
    error: str
    def __init__(self, block: _Optional[_Union[BlockRef, _Mapping]] = ..., registers: _Optional[_Iterable[int]] = ..., read_at_unix_nanos: _Optional[int] = ..., error: _Optional[str] = ...) -> None: ...

class ReadBlockRequest(_message.Message):
    __slots__ = ("block", "max_age_ms", "force", "timeout_ms")
    BLOCK_FIELD_NUMBER: _ClassVar[int]
    MAX_AGE_MS_FIELD_NUMBER: _ClassVar[int]
    FORCE_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_MS_FIELD_NUMBER: _ClassVar[int]
    block: BlockRef
    max_age_ms: int
    force: bool
    timeout_ms: int
    def __init__(self, block: _Optional[_Union[BlockRef, _Mapping]] = ..., max_age_ms: _Optional[int] = ..., force: _Optional[bool] = ..., timeout_ms: _Optional[int] = ...) -> None: ...

class WriteRequest(_message.Message):
    __slots__ = ("link", "unit_id", "address", "values", "timeout_ms")
    LINK_FIELD_NUMBER: _ClassVar[int]
    UNIT_ID_FIELD_NUMBER: _ClassVar[int]
    ADDRESS_FIELD_NUMBER: _ClassVar[int]
    VALUES_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_MS_FIELD_NUMBER: _ClassVar[int]
    link: str
    unit_id: int
    address: int
    values: _containers.RepeatedScalarFieldContainer[int]
    timeout_ms: int
    def __init__(self, link: _Optional[str] = ..., unit_id: _Optional[int] = ..., address: _Optional[int] = ..., values: _Optional[_Iterable[int]] = ..., timeout_ms: _Optional[int] = ...) -> None: ...

class WriteResult(_message.Message):
    __slots__ = ("ok", "error", "written_at_unix_nanos")
    OK_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    WRITTEN_AT_UNIX_NANOS_FIELD_NUMBER: _ClassVar[int]
    ok: bool
    error: str
    written_at_unix_nanos: int
    def __init__(self, ok: _Optional[bool] = ..., error: _Optional[str] = ..., written_at_unix_nanos: _Optional[int] = ...) -> None: ...

class WatchLinksRequest(_message.Message):
    __slots__ = ("links",)
    LINKS_FIELD_NUMBER: _ClassVar[int]
    links: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, links: _Optional[_Iterable[str]] = ...) -> None: ...

class LinkStatus(_message.Message):
    __slots__ = ("link", "connected", "last_error", "last_success_unix_nanos", "consecutive_errors", "backoff_ms")
    LINK_FIELD_NUMBER: _ClassVar[int]
    CONNECTED_FIELD_NUMBER: _ClassVar[int]
    LAST_ERROR_FIELD_NUMBER: _ClassVar[int]
    LAST_SUCCESS_UNIX_NANOS_FIELD_NUMBER: _ClassVar[int]
    CONSECUTIVE_ERRORS_FIELD_NUMBER: _ClassVar[int]
    BACKOFF_MS_FIELD_NUMBER: _ClassVar[int]
    link: str
    connected: bool
    last_error: str
    last_success_unix_nanos: int
    consecutive_errors: int
    backoff_ms: int
    def __init__(self, link: _Optional[str] = ..., connected: _Optional[bool] = ..., last_error: _Optional[str] = ..., last_success_unix_nanos: _Optional[int] = ..., consecutive_errors: _Optional[int] = ..., backoff_ms: _Optional[int] = ...) -> None: ...
