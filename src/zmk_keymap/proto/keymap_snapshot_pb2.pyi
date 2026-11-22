import datetime

from google.protobuf import timestamp_pb2 as _timestamp_pb2
from zmk_studio_client.proto import keyboard_pb2 as _keyboard_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ArgumentKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ARGUMENT_KIND_UNSPECIFIED: _ClassVar[ArgumentKind]
    ARGUMENT_KIND_CONSTANT: _ClassVar[ArgumentKind]
    ARGUMENT_KIND_RANGE: _ClassVar[ArgumentKind]
    ARGUMENT_KIND_HID_USAGE: _ClassVar[ArgumentKind]
    ARGUMENT_KIND_LAYER_ID: _ClassVar[ArgumentKind]
    ARGUMENT_KIND_UNDECLARED: _ClassVar[ArgumentKind]
ARGUMENT_KIND_UNSPECIFIED: ArgumentKind
ARGUMENT_KIND_CONSTANT: ArgumentKind
ARGUMENT_KIND_RANGE: ArgumentKind
ARGUMENT_KIND_HID_USAGE: ArgumentKind
ARGUMENT_KIND_LAYER_ID: ArgumentKind
ARGUMENT_KIND_UNDECLARED: ArgumentKind

class KeymapSnapshot(_message.Message):
    __slots__ = ("metadata", "device", "layers", "layout", "behaviors")
    METADATA_FIELD_NUMBER: _ClassVar[int]
    DEVICE_FIELD_NUMBER: _ClassVar[int]
    LAYERS_FIELD_NUMBER: _ClassVar[int]
    LAYOUT_FIELD_NUMBER: _ClassVar[int]
    BEHAVIORS_FIELD_NUMBER: _ClassVar[int]
    metadata: SnapshotMetadata
    device: Device
    layers: _containers.RepeatedCompositeFieldContainer[Layer]
    layout: PhysicalLayout
    behaviors: _containers.RepeatedCompositeFieldContainer[_keyboard_pb2.Behavior]
    def __init__(self, metadata: _Optional[_Union[SnapshotMetadata, _Mapping]] = ..., device: _Optional[_Union[Device, _Mapping]] = ..., layers: _Optional[_Iterable[_Union[Layer, _Mapping]]] = ..., layout: _Optional[_Union[PhysicalLayout, _Mapping]] = ..., behaviors: _Optional[_Iterable[_Union[_keyboard_pb2.Behavior, _Mapping]]] = ...) -> None: ...

class SnapshotMetadata(_message.Message):
    __slots__ = ("captured_at", "tool_version", "studio_messages_revision")
    CAPTURED_AT_FIELD_NUMBER: _ClassVar[int]
    TOOL_VERSION_FIELD_NUMBER: _ClassVar[int]
    STUDIO_MESSAGES_REVISION_FIELD_NUMBER: _ClassVar[int]
    captured_at: _timestamp_pb2.Timestamp
    tool_version: str
    studio_messages_revision: str
    def __init__(self, captured_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., tool_version: _Optional[str] = ..., studio_messages_revision: _Optional[str] = ...) -> None: ...

class Device(_message.Message):
    __slots__ = ("name", "id", "serial_number")
    NAME_FIELD_NUMBER: _ClassVar[int]
    ID_FIELD_NUMBER: _ClassVar[int]
    SERIAL_NUMBER_FIELD_NUMBER: _ClassVar[int]
    name: str
    id: str
    serial_number: str
    def __init__(self, name: _Optional[str] = ..., id: _Optional[str] = ..., serial_number: _Optional[str] = ...) -> None: ...

class Layer(_message.Message):
    __slots__ = ("id", "display_order", "name", "bindings")
    ID_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_ORDER_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    BINDINGS_FIELD_NUMBER: _ClassVar[int]
    id: int
    display_order: int
    name: str
    bindings: _containers.RepeatedCompositeFieldContainer[KeyBinding]
    def __init__(self, id: _Optional[int] = ..., display_order: _Optional[int] = ..., name: _Optional[str] = ..., bindings: _Optional[_Iterable[_Union[KeyBinding, _Mapping]]] = ...) -> None: ...

class KeyBinding(_message.Message):
    __slots__ = ("position", "behavior_id", "behavior_name", "param1", "param2", "decoded", "arguments")
    POSITION_FIELD_NUMBER: _ClassVar[int]
    BEHAVIOR_ID_FIELD_NUMBER: _ClassVar[int]
    BEHAVIOR_NAME_FIELD_NUMBER: _ClassVar[int]
    PARAM1_FIELD_NUMBER: _ClassVar[int]
    PARAM2_FIELD_NUMBER: _ClassVar[int]
    DECODED_FIELD_NUMBER: _ClassVar[int]
    ARGUMENTS_FIELD_NUMBER: _ClassVar[int]
    position: int
    behavior_id: int
    behavior_name: str
    param1: int
    param2: int
    decoded: str
    arguments: _containers.RepeatedCompositeFieldContainer[BehaviorArgument]
    def __init__(self, position: _Optional[int] = ..., behavior_id: _Optional[int] = ..., behavior_name: _Optional[str] = ..., param1: _Optional[int] = ..., param2: _Optional[int] = ..., decoded: _Optional[str] = ..., arguments: _Optional[_Iterable[_Union[BehaviorArgument, _Mapping]]] = ...) -> None: ...

class BehaviorArgument(_message.Message):
    __slots__ = ("name", "raw", "decoded", "kind")
    NAME_FIELD_NUMBER: _ClassVar[int]
    RAW_FIELD_NUMBER: _ClassVar[int]
    DECODED_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    name: str
    raw: int
    decoded: str
    kind: ArgumentKind
    def __init__(self, name: _Optional[str] = ..., raw: _Optional[int] = ..., decoded: _Optional[str] = ..., kind: _Optional[_Union[ArgumentKind, str]] = ...) -> None: ...

class PhysicalLayout(_message.Message):
    __slots__ = ("name", "keys", "active_index", "available")
    NAME_FIELD_NUMBER: _ClassVar[int]
    KEYS_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_INDEX_FIELD_NUMBER: _ClassVar[int]
    AVAILABLE_FIELD_NUMBER: _ClassVar[int]
    name: str
    keys: _containers.RepeatedCompositeFieldContainer[KeyPosition]
    active_index: int
    available: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, name: _Optional[str] = ..., keys: _Optional[_Iterable[_Union[KeyPosition, _Mapping]]] = ..., active_index: _Optional[int] = ..., available: _Optional[_Iterable[str]] = ...) -> None: ...

class KeyPosition(_message.Message):
    __slots__ = ("position", "x", "y", "width", "height", "rotation", "rotation_x", "rotation_y")
    POSITION_FIELD_NUMBER: _ClassVar[int]
    X_FIELD_NUMBER: _ClassVar[int]
    Y_FIELD_NUMBER: _ClassVar[int]
    WIDTH_FIELD_NUMBER: _ClassVar[int]
    HEIGHT_FIELD_NUMBER: _ClassVar[int]
    ROTATION_FIELD_NUMBER: _ClassVar[int]
    ROTATION_X_FIELD_NUMBER: _ClassVar[int]
    ROTATION_Y_FIELD_NUMBER: _ClassVar[int]
    position: int
    x: int
    y: int
    width: int
    height: int
    rotation: int
    rotation_x: int
    rotation_y: int
    def __init__(self, position: _Optional[int] = ..., x: _Optional[int] = ..., y: _Optional[int] = ..., width: _Optional[int] = ..., height: _Optional[int] = ..., rotation: _Optional[int] = ..., rotation_x: _Optional[int] = ..., rotation_y: _Optional[int] = ...) -> None: ...
