from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class TaskSpec(_message.Message):
    __slots__ = ("type", "key", "payload", "depends_on_keys", "rate_key", "tolerate_failures", "max_attempts")
    TYPE_FIELD_NUMBER: _ClassVar[int]
    KEY_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    DEPENDS_ON_KEYS_FIELD_NUMBER: _ClassVar[int]
    RATE_KEY_FIELD_NUMBER: _ClassVar[int]
    TOLERATE_FAILURES_FIELD_NUMBER: _ClassVar[int]
    MAX_ATTEMPTS_FIELD_NUMBER: _ClassVar[int]
    type: str
    key: str
    payload: bytes
    depends_on_keys: _containers.RepeatedScalarFieldContainer[str]
    rate_key: str
    tolerate_failures: bool
    max_attempts: int
    def __init__(self, type: _Optional[str] = ..., key: _Optional[str] = ..., payload: _Optional[bytes] = ..., depends_on_keys: _Optional[_Iterable[str]] = ..., rate_key: _Optional[str] = ..., tolerate_failures: _Optional[bool] = ..., max_attempts: _Optional[int] = ...) -> None: ...

class SubmitRunRequest(_message.Message):
    __slots__ = ("run_id", "workflow", "snapshot_id", "seed", "tasks")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    WORKFLOW_FIELD_NUMBER: _ClassVar[int]
    SNAPSHOT_ID_FIELD_NUMBER: _ClassVar[int]
    SEED_FIELD_NUMBER: _ClassVar[int]
    TASKS_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    workflow: str
    snapshot_id: str
    seed: int
    tasks: _containers.RepeatedCompositeFieldContainer[TaskSpec]
    def __init__(self, run_id: _Optional[str] = ..., workflow: _Optional[str] = ..., snapshot_id: _Optional[str] = ..., seed: _Optional[int] = ..., tasks: _Optional[_Iterable[_Union[TaskSpec, _Mapping]]] = ...) -> None: ...

class SubmitRunResponse(_message.Message):
    __slots__ = ("created",)
    CREATED_FIELD_NUMBER: _ClassVar[int]
    created: bool
    def __init__(self, created: _Optional[bool] = ...) -> None: ...

class LeaseRequest(_message.Message):
    __slots__ = ("worker_id", "task_types")
    WORKER_ID_FIELD_NUMBER: _ClassVar[int]
    TASK_TYPES_FIELD_NUMBER: _ClassVar[int]
    worker_id: str
    task_types: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, worker_id: _Optional[str] = ..., task_types: _Optional[_Iterable[str]] = ...) -> None: ...

class LeaseResponse(_message.Message):
    __slots__ = ("has_task", "task_id", "task_type", "payload", "attempt", "lease_seconds", "run_id", "snapshot_id", "seed", "task_key")
    HAS_TASK_FIELD_NUMBER: _ClassVar[int]
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    TASK_TYPE_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    LEASE_SECONDS_FIELD_NUMBER: _ClassVar[int]
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    SNAPSHOT_ID_FIELD_NUMBER: _ClassVar[int]
    SEED_FIELD_NUMBER: _ClassVar[int]
    TASK_KEY_FIELD_NUMBER: _ClassVar[int]
    has_task: bool
    task_id: str
    task_type: str
    payload: bytes
    attempt: int
    lease_seconds: int
    run_id: str
    snapshot_id: str
    seed: int
    task_key: str
    def __init__(self, has_task: _Optional[bool] = ..., task_id: _Optional[str] = ..., task_type: _Optional[str] = ..., payload: _Optional[bytes] = ..., attempt: _Optional[int] = ..., lease_seconds: _Optional[int] = ..., run_id: _Optional[str] = ..., snapshot_id: _Optional[str] = ..., seed: _Optional[int] = ..., task_key: _Optional[str] = ...) -> None: ...

class HeartbeatRequest(_message.Message):
    __slots__ = ("task_id", "attempt")
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    attempt: int
    def __init__(self, task_id: _Optional[str] = ..., attempt: _Optional[int] = ...) -> None: ...

class HeartbeatResponse(_message.Message):
    __slots__ = ("lease_lost",)
    LEASE_LOST_FIELD_NUMBER: _ClassVar[int]
    lease_lost: bool
    def __init__(self, lease_lost: _Optional[bool] = ...) -> None: ...

class Record(_message.Message):
    __slots__ = ("kind", "key", "data")
    KIND_FIELD_NUMBER: _ClassVar[int]
    KEY_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    kind: str
    key: str
    data: bytes
    def __init__(self, kind: _Optional[str] = ..., key: _Optional[str] = ..., data: _Optional[bytes] = ...) -> None: ...

class CompleteRequest(_message.Message):
    __slots__ = ("task_id", "attempt", "output", "records", "sub_dag")
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_FIELD_NUMBER: _ClassVar[int]
    RECORDS_FIELD_NUMBER: _ClassVar[int]
    SUB_DAG_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    attempt: int
    output: bytes
    records: _containers.RepeatedCompositeFieldContainer[Record]
    sub_dag: _containers.RepeatedCompositeFieldContainer[TaskSpec]
    def __init__(self, task_id: _Optional[str] = ..., attempt: _Optional[int] = ..., output: _Optional[bytes] = ..., records: _Optional[_Iterable[_Union[Record, _Mapping]]] = ..., sub_dag: _Optional[_Iterable[_Union[TaskSpec, _Mapping]]] = ...) -> None: ...

class CompleteResponse(_message.Message):
    __slots__ = ("status",)
    class Status(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        OK: _ClassVar[CompleteResponse.Status]
        STALE_LEASE: _ClassVar[CompleteResponse.Status]
    OK: CompleteResponse.Status
    STALE_LEASE: CompleteResponse.Status
    STATUS_FIELD_NUMBER: _ClassVar[int]
    status: CompleteResponse.Status
    def __init__(self, status: _Optional[_Union[CompleteResponse.Status, str]] = ...) -> None: ...

class FailRequest(_message.Message):
    __slots__ = ("task_id", "attempt", "error", "fatal", "retry_after_seconds")
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    FATAL_FIELD_NUMBER: _ClassVar[int]
    RETRY_AFTER_SECONDS_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    attempt: int
    error: str
    fatal: bool
    retry_after_seconds: int
    def __init__(self, task_id: _Optional[str] = ..., attempt: _Optional[int] = ..., error: _Optional[str] = ..., fatal: _Optional[bool] = ..., retry_after_seconds: _Optional[int] = ...) -> None: ...

class FailResponse(_message.Message):
    __slots__ = ("status",)
    class Status(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        OK: _ClassVar[FailResponse.Status]
        STALE_LEASE: _ClassVar[FailResponse.Status]
    OK: FailResponse.Status
    STALE_LEASE: FailResponse.Status
    STATUS_FIELD_NUMBER: _ClassVar[int]
    status: FailResponse.Status
    def __init__(self, status: _Optional[_Union[FailResponse.Status, str]] = ...) -> None: ...
