from warnings import warn
from enum import IntEnum

from typing import Callable, Optional
import bleak
import asyncio
from bleak.backends.device import BLEDevice
import dataclasses
from bleak.exc import BleakDBusError, BleakError

from anki.control.lights import BasePattern

from ..misc import msg_protocol

from ..misc.msgs import (
    disassemble_charger_info,
    disassemble_track_update,
    disassemble_track_change,
    disassemble_version_resp,
    set_light_pkg,
    set_light_pattern_pkg,
    set_sdk_pkg,
    set_speed_pkg,
    change_lane_pkg,
    turn_180_pkg,
    ping_pkg,
    version_request_pkg
)
from ..misc.track_pieces import TrackPiece, TrackPieceType
from ..misc import const
from ..misc.lanes import Lane3, Lane4, BaseLane, _Lane
from .. import errors

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .controller import Controller
    pass

_Callback = Callable[[], None]


def interpret_local_name(name: str|None):
    # Get the state of the vehicle from the local name
    if name is None or len(name) < 1:  # Fix some issues that might occur
        raise ValueError("Name was empty")
        pass
    nameBytes = name.encode("utf-8")
    vehicleState = nameBytes[0]
    version = int.from_bytes(nameBytes[1:3], "little", signed=False)
    vehicleName = nameBytes[8:].decode("utf-8")

    return BatteryState.from_int(vehicleState), version, vehicleName

def _set_lights_bits(low: int, high: int, bit: int, op: bool|None) -> tuple[int, int]:
    # Updates the low and high nibbles at the bit position according to op
    # Returns the low and high nibbles (in that order)
    if op is not None:
        low |= bit
        if op:
            high |= bit
    return low, high
        

def _call_all_soon(funcs, *args):
    # Registers everything in funcs to be called soon with *args
    for f in funcs:
        asyncio.get_running_loop().call_soon(f, *args)


@dataclasses.dataclass(frozen=True)
class BatteryState:
    """Represents the state of a supercar"""
    full_battery: bool
    low_battery: bool|None
    on_charger: bool
    charging: bool|None = None

    @classmethod
    def from_int(cls, state: int):
        """Constructs a :class:`BatteryState` from an integer representation
        
        :param state: :class:`int`
            The integer state passed by the discovery process
        
        Returns
        -------
        :class:`BatteryState`
        The new :class:`BatteryState` instance
        """
        full = bool(state & (1 << const.VehicleBattery.FULL_BATTERY))
        low = bool(state & (1 << const.VehicleBattery.LOW_BATTERY))
        on_charger = bool(state & (1 << const.VehicleBattery.ON_CHARGER))

        return cls(full, low, on_charger)

    @classmethod
    def from_charger_info(cls, payload: bytes):
        """
        Constructs a :class:`BatteryState` instance from a CHARGER_INFO message.

        :param payload: :class:`bytes`
            The payload of the CHARGER_INFO message
        
        Returns
        -------
        :class:`BatteryState`
        The new :class:`BatteryState` instance
        """
        _, on_charger, charging, full = disassemble_charger_info(payload)
        return cls(full, None, on_charger, charging)


class Lights:
    ENGINE = 0b0001
    """Does not seem to work"""
    BRAKELIGHTS = 0b0010
    HEADLIGHTS = 0b0100
    BRAKELIGHTS_FLICKER = 0b1000
    """Brakelights have two bits for some reason"""

class TurnType(IntEnum):
    NONE = 0
    LEFT = 1
    RIGHT = 2
    UTURN = 3
    UTURN_JUMP = 4

class TurnTrigger(IntEnum):
    NOW = 0
    INTERSECTION = 1


class Vehicle:
    """This class represents a supercar. With it you can control all functions of said supercar.

    
    :param id: :class:`int`
        The id of the :class:`Vehicle` object
    :param device: :class:`bleak.BLEDevice`
        The BLE device representing the supercar
    :param client: :class:`Optional[bleak.BleakClient]`
        A client wrapper around the BLE device
    
    .. note::
        You should not create this class manually,
        use one of the connect methods in the :class:`Controller`.
    """

    AUTOMATIC_PING_CONTROL = {
        "interval": 10,
        "timeout": 10,
        "max_timeouts": 2
    }

    __slots__ = (
        "_client",
        "_current_track_piece",
        "_is_connected",
        "_road_offset",
        "_speed",
        "on_track_piece_change",
        "_position",
        "_map",
        "_read_chara",
        "_write_chara",
        "_id",
        "_track_piece_watchers",
        "_pong_watchers",
        "_delocal_watchers",
        "_battery_watchers",
        "_controller",
        "_ping_task",
        "_battery",
        # Futures for callback->coroutine logic
        "_version_future"
        "_track_piece_future",
    )
    
    def __init__(
            self,
            id: int,
            device: BLEDevice,
            client: bleak.BleakClient|None=None,
            controller: Optional["Controller"]=None,  # Inconsistent, but fixes failing docs
            *,
            battery: BatteryState
    ):
        if client is None:
            self._client = bleak.BleakClient(device)
        else:
            self._client = client

        self._id: int = id
        self._current_track_piece: TrackPiece|None = None
        """Do not use! This can only show the last position for... reasons"""
        self._is_connected = False
        self._road_offset: float|None = None
        self._speed: int = 0
        self._map: Optional[list[TrackPiece]] = None
        self._position: Optional[int] = None

        self.on_track_piece_change: Callable = lambda: None  # Set a dummy function by default
        self._track_piece_watchers: list[_Callback] = []
        self._pong_watchers: list[_Callback] = []
        self._delocal_watchers: list[_Callback] = []
        self._battery_watchers: list[_Callback] = []
        self._controller = controller
        self._battery: BatteryState = battery
        self._version_future: asyncio.Future[int] = asyncio.Future()
        self._track_piece_future: asyncio.Future = asyncio.Future()

    def _notify_handler(self, handler, data: bytearray) -> bool:
        """An internal handler function that gets called on a notify receive.
        Returns True if the message was processed, or False if not.
        """
        msg_type, payload = msg_protocol.disassemble_packet(data)
        if msg_type == const.VehicleMsg.TRACK_PIECE_UPDATE:
            # This gets called when part-way along a track piece (sometimes)
            loc, piece, offset, speed, clockwise = disassemble_track_update(payload)

            # Update internal variables when new info available
            self._road_offset = offset
            self._speed = speed

            # Post a warning when TrackPiece creation failed (but not an error)
            try:
                piece_obj = TrackPiece.from_raw(loc, piece, clockwise)
            except ValueError:
                warn(
                    f"A TrackPiece value received from the vehicle could not be decoded. \
                    If you are running a scan, this will break it. Received: {piece}",
                    errors.TrackPieceDecodeWarning
                )
                return True

            self._current_track_piece = piece_obj

        elif msg_type == const.VehicleMsg.TRACK_PIECE_CHANGE:
            if (
                self._current_track_piece is not None 
                and self._current_track_piece.type == TrackPieceType.FINISH
            ):
                self._position = 0
            
            uphill_count, downhill_count = disassemble_track_change(payload)[8:10]
            """TODO: Find out what to do with these"""
            if self._position is not None:
                # If vehicle is aligned
                # This may happen during scan or because of a flying realign
                # FIXME: Position index 0 does not exist on flying align o.0
                self._position += 1
                if self._map is not None:
                    # If already scanned the map, ensure position is valid
                    self._position %= len(self._map)

            self._track_piece_future.set_result(None)
            # Complete internal future when on new track piece.
            # This is used in wait_for_track_change
            self._track_piece_future = asyncio.Future()
            # Create new future since the old one is now done
            self.on_track_piece_change()
            _call_all_soon(self._track_piece_watchers)
            pass
        elif msg_type == const.VehicleMsg.PONG:
            _call_all_soon(self._pong_watchers)
        elif msg_type == const.VehicleMsg.DELOCALIZED:
            _call_all_soon(self._delocal_watchers)
            pass
        elif msg_type == const.VehicleMsg.CHARGER_INFO:
            self._battery = BatteryState.from_charger_info(payload)
            _call_all_soon(self._battery_watchers)
            pass
        elif msg_type == const.VehicleMsg.VERSION_RESP:
            self._version_future.set_result(disassemble_version_resp(payload))
            self._version_future = asyncio.Future()
        else:
            return False
        return True

    async def _auto_ping(self):
        # Automatically pings the supercars
        # and disconnects when they don't respond.
        # TODO: Remove debug prints
        # TODO: Replace future with event
        pong_reply_future = asyncio.Future()
        
        @self.pong
        def pong_watch():
            nonlocal pong_reply_future
            pong_reply_future.set_result()
            pong_reply_future = asyncio.Future()
            print("Pong reply!")
            pass
        
        config = type(self).AUTOMATIC_PING_CONTROL
        timeouts = 0
        while self.is_connected:
            await asyncio.sleep(config["interval"])
            await self.ping()
            print("Ping!")
            try:
                await asyncio.wait_for(pong_reply_future, config["timeout"])
            except asyncio.TimeoutError:
                timeouts += 1
                print("Ping failed")
            else:
                timeouts = 0
                print("Ping succeeded")

            if timeouts > config["max_timeouts"]:
                warn("The vehicle did not sufficiently respond to pings. Disconnecting...")
                await self.disconnect()

    async def __send_package(self, payload: bytes):
        """Send a payload to the supercar"""
        if self._write_chara is None:
            raise RuntimeError("A command was sent to a vehicle that has not been connected.")
        try:
            await self._client.write_gatt_char(self._write_chara, payload)
        except OSError as e:
            raise RuntimeError(
                "A command was sent to a vehicle that is already disconnected"
            ) from e

    async def wait_for_track_change(self) -> Optional[TrackPiece]:
        """Waits until the current track piece changes.
        
        Returns
        -------
        :class:`TrackPiece`
            The new track piece. `None` if :func:`Vehicle.map` is None
            (for example if the map has not been scanned yet)
        """
        await self._track_piece_future
        # Wait on a new track piece (See _notify_handler)
        return self.current_track_piece

    async def connect(self):
        """Connect to the Supercar
        **Don't forget to call Vehicle.disconnect on program exit!**
        
        Raises
        ------
        :class:`ConnectionTimedoutException`
            The connection attempt to the supercar did not succeed within the set timeout
        :class:`ConnectionDatabusException`
            A databus error occured whilst connecting to the supercar
        :class:`ConnectionFailedException`
            A generic error occured whilst connection to the supercar
        """
        try:
            await self._client.connect()
        # Translate a bunch of errors occuring on connection
        except BleakDBusError as e:
            raise errors.ConnectionDatabusError(
                "An attempt to connect to the vehicle failed. \
                This can occur sometimes and is usually not an error in your code."
            ) from e
        except BleakError as e:
            raise errors.ConnectionFailedError(
                "An attempt to connect to the vehicle failed. \
                This is usually not associated with your code."
            ) from e
        except asyncio.TimeoutError as e:
            raise errors.ConnectionTimedoutError(
                "An attempt to connect to the vehicle timed out. \
                Make sure the car is actually disconnected."
            ) from e
        
        # Get service and characteristics
        services = self._client.services
        anki_service = services.get_service(const.SERVICE_UUID)
        if anki_service is None:
            raise RuntimeError("The vehicle does not have an anki service... What?")
        read = anki_service.get_characteristic(const.READ_CHAR_UUID)
        write = anki_service.get_characteristic(const.WRITE_CHAR_UUID)
        if read is None or write is None:
            raise RuntimeError(
                "This vehicle does not have a read or write characteristic. \
                If this occurs again, something is severly wrong with your vehicle."
            )

        await self._client.write_gatt_char(
            write,
            set_sdk_pkg(True, 0x1)
        )
        # NOTE: If someone knows what the flags mean, please contact us
        await self._client.start_notify(read, lambda *args: None and self._notify_handler(*args))

        self._read_chara = read
        self._write_chara = write

        self._is_connected = True
        self._ping_task = asyncio.create_task(self._auto_ping())
        pass

    async def disconnect(self) -> bool:
        """Disconnect from the Supercar

        .. note::
            Remember to execute this for every connected :class:`Vehicle` once the program exits.
            Not doing so will result in your supercars not connecting sometimes
            as they still think they are connected.

        Returns
        -------
        :class:`bool`
        The connection state of the :class:`Vehicle` instance. This should always be `False`

        Raises
        ------
        :class:`DisconnectTimedoutException`
            The attempt to disconnect from the supercar timed out
        :class:`DisconnectFailedException`
            The attempt to disconnect from the supercar failed for an unspecified reason
        """
        try:
            self._is_connected = not await self._client.disconnect()
        except asyncio.TimeoutError as e:
            raise errors.DisconnectTimedoutError(
                "The attempt to disconnect from the vehicle timed out."
            ) from e
        if self._is_connected:
            raise errors.DisconnectFailedError("The attempt to disconnect the vehicle failed.")
        
        if not self._is_connected and self._controller is not None:
            self._controller.vehicles.remove(self)
            self._ping_task.cancel("Vehicle disconnected")

        return self._is_connected

    async def set_speed(self, speed: int, acceleration: int = 500):
        """Set the speed of the Supercar in mm/s

        :param speed: :class:`int`
            The speed in mm/s
        :param acceleration: :class:`Optional[int]`
            The acceleration in mm/s²
        """
        await self.__send_package(set_speed_pkg(speed, acceleration))
        # Update the internal speed as well
        # (this is technically an overestimate, but the error is marginal)
        self._speed = speed

    async def stop(self):
        """Stops the Supercar"""
        await self.set_speed(0, 600)
    
    async def change_lane(
            self,
            lane: BaseLane,
            horizontalSpeed: int = 300,
            horizontalAcceleration: int = 300,
            *,
            _hopIntent: int = 0x0,
            _tag: int = 0x0
    ):
        """Change to a desired lane

        :param lane: :class:`BaseLane`
            The lane to move into. These may be :class:`Lane3` or :class:`Lane4`
        :param horizontalSpeed: :class:`Optional[int]`
            The speed the vehicle will move along the track at in mm/s
        :param horizontalAcceleration: :class:`Optional[int]`
            The acceleration in mm/s² the vehicle will move horizontally with
        
        .. note::
            Due to a hardware limitation vehicles won't reliably
            perform lane changes under 300mm/s speed.
        """
        # changeLane is just changePosition but user friendly
        await self.change_position(
            lane.value,
            horizontalSpeed,
            horizontalAcceleration,
            _hopIntent=_hopIntent,
            _tag=_tag
            # NOTE: Getting hop intent and tag to work would be awesome
            # but the vehicles are buggy as ever
        )
    
    async def change_position(
            self,
            roadCenterOffset: float,
            horizontalSpeed: int = 300,
            horizontalAcceleration: int = 300,
            *,
            _hopIntent: int = 0x0,
            _tag: int = 0x0
    ):
        """Change to a position offset from the track centre
        
        :param roadCenterOffset: :class:`float`
            The target offset from the centre of the track piece in mm
        :param horizontalSpeed: :class:`int`
            The speed the vehicle will move along the track at in mm/s
        :param horizontalAcceleration: :class:`int`
            The acceleration in mm/s² the vehicle will move horizontally with

        .. note::
            Due to a hardware limitation vehicles won't reliably perform
            lane changes under 300mm/s speed.
        """
        await self.__send_package(change_lane_pkg(
            roadCenterOffset,
            horizontalSpeed,
            horizontalAcceleration,
            _hopIntent,
            _tag
        ))

    async def turn(self, type: TurnType = TurnType.UTURN, trigger: TurnTrigger = TurnTrigger.NOW):
        # type and trigger don't work correcty
        """
        .. warning::
            This does not yet function properly. It is advised not to use this method
        """
        if self.map is not None:
            warn(
                "Turning around with a map! This will cause a desync!",
                UserWarning
            )
        await self.__send_package(turn_180_pkg(int(type), int(trigger)))
    
    async def set_lights(self, *, 
            engine: bool|None=None, headlights: bool|None=None,
            brakelights: bool|None=None, brakelights_flicker: bool|None=None):
        """
        Change which lights are active on the vehicle. 
        Any lights set to `None` (the default) will keep their previous state.
        Enabling a light also resets its pattern.
        
        :param engine: :class:`bool|None`
            The engine light (big RGB light at the top).
            Does not work, probably a bug
        :param headlights: :class:`bool|None`
            Headlights. Color probably varies depending on the vehicle.
        :param brakelights: :class:`bool|None`
            Solid red brakelights.
        :param brakelights_flicker: :class:`bool|None`
            Flickering red brakelights. If set, overrides any setting on brakelights.
            (e.g. `brakelights=True,brakelights_flicker=False` means no brakelights)
        """
        low = 0b0000
        high = 0b0000
        low, high = _set_lights_bits(low, high, Lights.ENGINE, engine)
        low, high = _set_lights_bits(low, high, Lights.BRAKELIGHTS, brakelights)
        low, high = _set_lights_bits(low, high, Lights.HEADLIGHTS, headlights)
        low, high = _set_lights_bits(low, high, Lights.BRAKELIGHTS_FLICKER, brakelights_flicker)
        await self.set_lights_raw((high << 4) | low)
    
    async def set_lights_raw(self, light: int):
        """
        Set the lights of the vehicle in accordance with a bitmask.
        There's normally no need to use this method. Use :func:`Vehicle.set_lights` instead.
        """
        await self.__send_package(set_light_pkg(light))
    
    async def set_light_pattern(self, patterns: list[BasePattern]):
        """Detailed control over the vehicle lights, including animations.
        
        :param patterns: :class:`list[BasePattern]`
            A list of patterns to execute. May at most be of length three.
        """
        await self.__send_package(set_light_pattern_pkg(patterns))
    
    def get_lane(self, mode: type[_Lane]) -> Optional[_Lane]:
        """Get the current lane given a specific lane type

        :param mode: :class:`BaseLane`
            A class such as :class:`Lane3` or :class:`Lane4` inheriting from :class:`BaseLane`.
            This is the lane system being used
        
        Returns
        -------
        :class:`Optional[BaseLane]`
            The lane the vehicle is on. This may be none if no lane information is available
            (such as at the start of the program, when the vehicles haven't moved much)
        """
        if self._road_offset is None:
            return None
        else:
            return mode.get_closest_lane(self._road_offset)

    async def align(
            self,
            speed: int=300,
            *,
            target_previous_track_piece_type: TrackPieceType = TrackPieceType.FINISH
    ):
        """Align to the start piece.

        :param speed: :class:`int`
            The speed the vehicle should travel at during alignment
        """
        await self.set_speed(speed)
        # Waits until the previous track piece was FINISH (by default).
        # This means the current position is START
        while self._current_track_piece is None\
                or self._current_track_piece.type is not target_previous_track_piece_type:
            await self.wait_for_track_change()

        # Vehicle is now at START which is always 0
        self._position = 0

        await self.stop()
    
    def track_piece_change(self, func: _Callback):
        """
        A decorator marking a function to be executed when the supercar
        drives onto a new track piece

        :param func: :class:`function`
            The listening function
        
        Returns
        -------
        :class:`function`
            The function that was passed in
        """
        self._track_piece_watchers.append(func)
        return func
        pass
    
    def remove_track_piece_watcher(self, func: _Callback):
        """
        Remove a track piece event handler added by :func:`Vehicle.track_piece_change`

        :param func: :class:`function`
            The function to remove as an event handler
        
        Raises
        ------
        :class:`ValueError`
            The function passed is not an event handler
        """
        self._track_piece_watchers.remove(func)
        pass

    def delocalized(self, func: _Callback):
        """
        A decorator marking this function to be execute when the vehicle has delocalized*.

        :param func: :class:`function`
            The listening function

        .. note::
            It is not guaranteed that the handler will be called when the vehicle is delocalized.
            Furthermore, it is not guaranteed that the handler will *not* be called when the
            vehicle is still localized.
            This method should only be used for informational purposes!
        """
        self._delocal_watchers.append(func)
        pass

    def remove_delocalized_watcher(self, func: _Callback):
        """
        Remove a delocalization event handler that was added by :func:`Vehicle.delocalized`.

        :param func: :class:`function`
            The function to be removed

        Raises
        ------
        :class:`ValueError`
            The function passed is not an event handler
        """
        self._delocal_watchers.remove(func)
        pass

    def battery_change(self, func: _Callback):
        """
        Register a callback to execute on changes to the battery state.
        
        .. note::
            It is not guaranteed that the battery state has actually changed
            from the last callback.
            Further note that this function is not called on startup.
        
        Raises
        ------
        :class:`ValueError`
            The function passed is not an event handler
        """
        self._battery_watchers.append(func)
        # FIXME: Function returns None, should func
        pass

    def remove_battery_watcher(self, func: _Callback):
        # TODO: Add code comments
        self._battery_watchers.remove(func)

    async def ping(self):
        """
        Send a ping to the vehicle
        """
        await self.__send_package(ping_pkg())

    def pong(self, func):
        """
        A decorator marking an function to be executed when the supercar responds to a ping

        :param func: :class:`function`
            The function to mark as a listener
        
        Returns
        -------
        :class:`function`
            The function being passed in
        """
        self._pong_watchers.append(func)
        return func

    async def get_version(self) -> int:
        """Get the vehicle firmware version"""
        await self.__send_package(version_request_pkg())
        return await self._version_future

    @property
    def is_connected(self) -> bool:
        """
        `True` if the vehicle is currently connected
        """
        return self._is_connected

    @property
    def current_track_piece(self) -> TrackPiece|None:
        """
        The :class:`TrackPiece` the vehicle is currently located at

        .. note::
            This will return :class:`None` if either scan or align is not completed
        """
        if self.map is None or self.map_position is None:
            # If scan or align not complete, we can't find the track piece
            return None
        return self.map[self.map_position]

    @property
    def map(self) -> tuple[TrackPiece, ...]|None:
        """
        The map the :class:`Vehicle` instance is using.
        This is :class:`None` if the :class:`Vehicle` does not have a map supplied.
        """
        return tuple(self._map) if self._map is not None else None

    @property
    def map_position(self) -> int|None:
        """
        The position of the :class:`Vehicle` instance on the map.
        This is :class:`None` if :func:`Vehicle.align` has not yet been called.
        """
        return self._position

    @property
    def road_offset(self) -> float|None:
        """
        The offset from the road centre.
        This is :class:`None` if the supercar did not send any information yet.
        (Such as when it hasn't moved much)
        """
        return self._road_offset

    @property
    def speed(self) -> int:
        """
        The speed of the supercar in mm/s.
        This is :class:`None` if the supercar has not moved or :func:`Vehicle.setSpeed`
        hasn't been called yet.
        """
        return self._speed

    @property
    def current_lane3(self) -> Optional[Lane3]:
        """
        Short-hand for
        
        .. code-block:: python
            
            Vehicle.get_lane(Lane3)
        """
        return self.get_lane(Lane3)

    @property
    def current_lane4(self) -> Optional[Lane4]:
        """
        Short-hand for
        
        .. code-block:: python
            
            Vehicle.get_lane(Lane4)
        """
        return self.get_lane(Lane4)

    @property
    def id(self) -> int:
        """
        The id of the :class:`Vehicle` instance. This is set during initialisation of the object.
        """
        return self._id

    @property
    def battery_state(self) -> BatteryState:
        """
        The state of the supercar's battery
        """
        return self._battery
