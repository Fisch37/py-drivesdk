from dataclasses import dataclass
from enum import IntEnum
import struct    

@dataclass
class LightPattern:
    class Channel(IntEnum):
        ENGINE_RED = 0
        TAIL = 1
        ENGINE_BLUE = 2
        ENGINE_GREEN = 3
        FRONT_1 = 4
        """There are two front lights that have different colours. set_light_pattern can set these individually"""
        FRONT_2 = 5
        """There are two front lights that have different colours. set_light_pattern can set these individually"""
    
    class Effect(IntEnum):
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
    
    MAX_INTENSITY = 14
    # According to the C SDK this should be 1, but that is wrong.
    MAX_CYCLES_PER_10s = 255
    
    channel: Channel
    effect: Effect
    start: int
    end: int
    cycles_per_10s: int
    
    @staticmethod
    def empty():
        return LightPattern(LightPattern.Channel.ENGINE_RED, LightPattern.Effect.STEADY, 0, 0, 0)
    
    def __post_init__(self):
        if self.start > self.MAX_INTENSITY:
            raise ValueError(f"intensity must be no more than {self.MAX_INTENSITY}, start was {self.start}")
        if self.end > self.MAX_INTENSITY:
            raise ValueError(f"intensity must be no more than {self.MAX_INTENSITY}, end was {self.start}")
        if self.cycles_per_10s > self.MAX_CYCLES_PER_10s:
            raise ValueError(f"cycles_per_10s must be no more than {self.MAX_CYCLES_PER_10s}, was {self.cycles_per_10s}")
    
    def to_bytes(self) -> bytes:
        return struct.pack("BBBBB",
                           int(self.channel), int(self.effect), 
                           int(self.start), int(self.end), int(self.cycles_per_10s)
                           )