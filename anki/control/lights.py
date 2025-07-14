from collections.abc import Container
from dataclasses import dataclass, replace, field
from enum import IntEnum
from abc import ABC, abstractmethod
import struct
from typing import Callable, ClassVar, Self

class _Effect(IntEnum):
    STEADY = 0
    """Set light intensity to <start>"""
    FADE = 1
    """Fade intensity from <start> to <end>"""
    THROB = 2
    """Fade intensity from <start> to <end> and then back to <start>"""
    FLASH = 3
    """Turn on LED between time 'start' and time 'end' inclusive"""
    RANDOM = 4
    """Flash erratically, ignoring <start> and <end>"""

class LightChannel(IntEnum):
    ENGINE_RED = 0
    TAIL = 1
    ENGINE_BLUE = 2
    ENGINE_GREEN = 3
    FRONT_1 = 4
    """There are two front lights that have different colours. set_light_pattern can set these individually"""
    FRONT_2 = 5
    """There are two front lights that have different colours. set_light_pattern can set these individually"""


def _to_bytes(channel: LightChannel, effect: _Effect, start: int, end: int, cycles_per_10s: int) -> bytes:
    return struct.pack("BBBBB",
        int(channel), int(effect), 
        start, end, cycles_per_10s
    )


class _RangedValue[T]:
    __slots__ = ("_attr", "_validation_range", "_error_factory")
    def __init__(self, attr: str, validation_range: Container[T], error_factory: Callable[[T], ValueError]):
        self._attr = attr
        self._validation_range = validation_range
        self._error_factory = error_factory

    def __get__(self, instance, _=None) -> T:
        if instance is None:
            raise AttributeError(f"Cannot access {type(self).__name__} from class")
        else:
            return getattr(instance, self._attr)
    
    def __set__(self, instance, value: T):
        if value not in self._validation_range:
            raise self._error_factory(value)
        else:
            setattr(instance, self._attr, value)

_RANGE_FACTORY = lambda attr, limit: field(default=_RangedValue("_" + attr, range(limit+1), lambda i: ValueError(f"field {attr} must be a non-negative integer no more than {limit}, was {i}")))

@dataclass
class BasePattern(ABC):
    MAX_INTENSITY = 14
    # According to the C SDK this should be 1, but that is wrong.
    MAX_CYCLES_PER_10s = 255

    channel: LightChannel

    @abstractmethod
    def to_bytes(self) -> bytes: ...

    def copy(self) -> Self:
        return replace(self)

@dataclass
class SteadyPattern(BasePattern):
    brightness: int = _RANGE_FACTORY("brightness", BasePattern.MAX_INTENSITY) # type: ignore

    def to_bytes(self) -> bytes:
        return _to_bytes(self.channel, _Effect.STEADY, self.brightness, 0, 0)

# Not static, not really animated
class RandomPattern(BasePattern):
    def to_bytes(self) -> bytes:
        return _to_bytes(self.channel, _Effect.RANDOM, 0, 0, 0)

@dataclass
class AnimatedPattern(BasePattern, ABC):
    _effect: ClassVar[_Effect]

    starting_brightness: int = _RANGE_FACTORY("start", BasePattern.MAX_INTENSITY) # type: ignore
    ending_brightness: int = _RANGE_FACTORY("end", BasePattern.MAX_INTENSITY) # type: ignore
    cycles_per_10s: int = _RANGE_FACTORY("cycles_per_10s", BasePattern.MAX_CYCLES_PER_10s) # type: ignore

    def to_bytes(self) -> bytes:
        return _to_bytes(self.channel, self._effect, self.starting_brightness, self.ending_brightness, self.cycles_per_10s)

class FadePattern(AnimatedPattern):
    _effect = _Effect.FADE

class ThrobPattern(AnimatedPattern):
    _effect = _Effect.THROB

class FlashPattern(AnimatedPattern):
    _effect = _Effect.FLASH