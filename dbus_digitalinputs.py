#!/usr/bin/python3 -u

import sys, os
import signal
from threading import Thread
from select import select, epoll, EPOLLPRI
from functools import partial
from collections import namedtuple
from argparse import ArgumentParser
import traceback
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'ext', 'velib_python'))

from dbus.mainloop.glib import DBusGMainLoop
import dbus
from gi.repository import GLib
from vedbus import VeDbusService, VeDbusItemImport
from settingsdevice import SettingsDevice

VERSION = '0.27'
MAXCOUNT = 2**31-1
SAVEINTERVAL = 60000

INPUT_FUNCTION_COUNTER = 1
INPUT_FUNCTION_INPUT = 2

Translation = namedtuple('Translation', ['no', 'yes'])

# Only append at the end
INPUTTYPES = [
    'Disabled',
    'Pulse meter',
    'Door',
    'Bilge pump',
    'Bilge alarm',
    'Burglar alarm',
    'Smoke alarm',
    'Fire alarm',
    'CO2 alarm',
    'Generator',
    'Generic I/O',
    'Touch enable',
]

RELAYTYPES = [
    'Disabled',
    'Alarm',
    'Genset start stop',
    'Manual',
    'Tank pump',
    'Temperature',
    'Connected genset helper relay',
]

# Translations. The text will be used only for GetText, it will be translated
# in the gui.
TRANSLATIONS = [
    Translation('low', 'high'),
    Translation('off', 'on'),
    Translation('no', 'yes'),
    Translation('open', 'closed'),
    Translation('ok', 'alarm'),
    Translation('running', 'stopped')
]

class SystemBus(dbus.bus.BusConnection):
    def __new__(cls):
        return dbus.bus.BusConnection.__new__(cls, dbus.bus.BusConnection.TYPE_SYSTEM)

class SessionBus(dbus.bus.BusConnection):
    def __new__(cls):
        return dbus.bus.BusConnection.__new__(cls, dbus.bus.BusConnection.TYPE_SESSION)

class InputPin():
    gpio_type = 'digitalinput'
    devid = None
    devinstance = None

    def __init__(self, name=None, path=None, label=None):
        self.name = name
        self.path = path
        self.label = label

class Relay():
    gpio_type = 'relay'
    devid = None
    devinstance = None
    fb = None
    state = 0

    def __init__(self, name=None, label=None, fb=None):
        self.name = name
        self.label = label
        self.fb = fb

    def setHWState(self, state):
        raise NotImplementedError

    def hasFb(self):
        return self.fb is not None

    def getHwState(self):
        if self.fb:
            try:
                with open(self.fb + '/value', 'rt') as r:
                    return int(r.read())
            except IOError:
                traceback.print_exc()

    @classmethod
    def createRelay(cls, id, name, paths, label):
        fb = None
        set = None
        res = None
        for path in paths:
            fb = path if path.endswith('_in') else fb
            res = path if path.endswith('_res') else res
            set = path if path.endswith('_set') or path.endswith(str(id)) else set

        # Monostable relay
        if set and not res:
            return MonoStableRelay(name, path, fb, label)

        # Bistable relay
        if set and res:
            return BiStableRelay(name, set, res, fb, label)

class MonoStableRelay(Relay):
    def __init__(self, name=None, path=None, fb=None, label=None):
        super(MonoStableRelay, self).__init__(name, label, fb)
        self.path = path

    def setHWState(self, state):
        try:
            with open(self.path + '/value', 'wt') as w:
                w.write(str(state))
        except IOError:
            traceback.print_exc()
            return False
        return True

class BiStableRelay(Relay):
    PULSELEN = 2000
    CHECK_INT = 100
    retries = 0
    def __init__(self, name=None, set=None, res=None, fb=None, label=None):
        super(BiStableRelay, self).__init__(name, label, fb)
        self.setpath = set
        self.respath = res
        self.path = set # TODO remove this

    def setHWState(self, state):
        try:
            with open((self.setpath if state else self.respath) + '/value', 'wt') as w:
                w.write('1')
        except IOError:
            traceback.print_exc()
            return False

        if self.fb:
            self.retries = 0
            self.timer = GLib.timeout_add(self.CHECK_INT, self.waitForState, state)
        else:
            self.timer = GLib.timeout_add(self.PULSELEN, self.clear)

        self.state = state
        return True

    def waitForState(self, state):
        self.retries += 1
        ret = self.getHwState() != state and self.retries < self.PULSELEN / self.CHECK_INT
        if not ret:
            self.clear()
        return ret

    def clear(self):
        if self.fb and self.getHwState() != self.state:
            print("Relay {} failed to set to state {}".format(self.name, self.state))

        for path in [self.setpath, self.respath]:
            try:
                with open(path + '/value', 'wt') as w:
                    w.write('0')
            except IOError:
                traceback.print_exc()
                return False
        return True

class BasePulseCounter(object):
    pass

class DebugPulseCounter(BasePulseCounter):
    def __init__(self):
        self.gpiomap = {}

    def register(self, path, gpio):
        self.gpiomap[gpio] = None
        return 0

    def unregister(self, gpio):
        del self.gpiomap[gpio]

    def registered(self, gpio):
        return gpio in self.gpiomap

    def __call__(self):
        from itertools import cycle
        from time import sleep
        for level in cycle([0, 1]):
            for gpio in list(self.gpiomap.keys()):
                yield gpio, level
                sleep(0.25/len(self.gpiomap))

class EpollPulseCounter(BasePulseCounter):
    def __init__(self):
        self.gpiomap = {}
        self.states = {}
        self.ob = epoll()

    def register(self, path, gpio):
        path = os.path.realpath(path)

        # Set up gpio for rising edge interrupts
        try:
            with open(os.path.join(path, 'edge'), 'ab') as fp:
                fp.write(b'both')
        except:
            pass

        fp = open(os.path.join(path, 'value'), 'rb')
        level = int(fp.read()) # flush it in case it's high at startup
        self.gpiomap[gpio] = fp
        self.states[gpio] = level
        self.ob.register(fp, EPOLLPRI)
        return level

    def unregister(self, gpio):
        fp = self.gpiomap[gpio]
        self.ob.unregister(fp)
        del self.gpiomap[gpio]
        del self.states[gpio]
        fp.close()

    def registered(self, gpio):
        return gpio in self.gpiomap

    def __call__(self):
        while True:
            # We have a timeout of 1 second on the poll, because poll() only
            # looks at files in the epoll object at the time poll() was called.
            # The timeout means we let other files (added via calls to
            # register/unregister) into the loop at least that often.
            self.ob.poll(1)

            # When coming out of the epoll call, we read all the gpios to make
            # sure we didn't miss any edges.  This is a safety fallback that
            # ensures everything is up to date once a second, but
            # edge-triggered results are handled immediately.
            # NOTE: There has not been a report of a missed interrupt yet.
            # Belts and suspenders.
            for gpio, fp in list(self.gpiomap.items()):
                os.lseek(fp.fileno(), 0, os.SEEK_SET)
                v = int(os.read(fp.fileno(), 1))
                if v != self.states[gpio]:
                    self.states[gpio] = v
                    yield gpio, v

class PollingPulseCounter(BasePulseCounter):
    def __init__(self):
        self.gpiomap = {}

    def register(self, path, gpio):
        path = os.path.realpath(path)

        fp = open(os.path.join(path, 'value'), 'rb')
        level = int(fp.read())
        self.gpiomap[gpio] = [fp, level]
        return level

    def unregister(self, gpio):
        del self.gpiomap[gpio]

    def registered(self, gpio):
        return gpio in self.gpiomap

    def __call__(self):
        from itertools import cycle
        from time import sleep
        while True:
            for gpio, (fp, level) in list(self.gpiomap.items()):
                fp.seek(0, os.SEEK_SET)
                v = int(fp.read())
                if v != level:
                    self.gpiomap[gpio][1] = v
                    yield gpio, v
            sleep(1)

class HandlerMaker(type):
    """ Meta-class for keeping track of all extended classes. """
    def __init__(cls, name, bases, attrs):
        if not hasattr(cls, 'handlers'):
            cls.handlers = {}
            cls.handlers[0] = {}    # Inputhandlers
            cls.handlers[1] = {}    # Relayhandlers
        else:
            cls.handlers[cls.handler_id][cls.type_id] = cls

class IoHandler(object, metaclass=HandlerMaker):
    product_id = 0xFFFF
    def __init__(self, bus, base, pin, settings):
        self.bus = bus
        self.path = pin.path
        self.settings = settings
        self._level = 0 # Remember last state

        instance = int(settings['instance'].split(':')[1])

        name = str(pin.name)
        if name[0].isdecimal():
            name = 'input_' + name

        self.service = VeDbusService(
            "{}.{}.{}".format(base, self.dbus_name, name), bus=bus,
            register=False)

        # Add objects required by ve-api
        self.service.add_path('/Mgmt/ProcessName', __file__)
        self.service.add_path('/Mgmt/ProcessVersion', VERSION)
        self.service.add_path('/Mgmt/Connection', self.path)
        self.service.add_path('/DeviceInstance', instance)
        self.service.add_path('/ProductId', self.product_id)
        self.service.add_path('/ProductName', self.product_name)
        self.service.add_path('/Connected', 1)

        # Custom name setting
        def _change_name(p, v):
            # This should fire a change event that will update product_name
            # below.
            settings['name'] = v
            return True

        self.service.add_path('/CustomName', settings['name'], writeable=True,
            onchangecallback=_change_name)

        # Register our name on dbus
        self.service.register()

    @property
    def product_name(self):
        return self.settings['name'] or self._product_name

    @property
    def active(self):
        return self.service is not None

    @product_name.setter
    def product_name(self, v):
        # Some pin types don't have an associated service (Disabled pins for
        # example)
        if self.service is not None:
            self.service['/ProductName'] = v or self._product_name

    def deactivate(self):
        self.save_count()
        self.service.__del__()
        del self.service
        self.service = None

    @property
    def level(self):
        return self._level

    @level.setter
    def level(self, l):
        self._level = int(bool(l))

    def save_count(self):
        pass

    def toggle(self, level):
        raise NotImplementedError

    def _toggle(self, level, service):
        # Only increment Count on rising edge.
        if level and level != self._level:
            service['/Count'] = (service['/Count']+1) % MAXCOUNT
        self._level = level

    def refresh(self):
        """ Toggle state to last remembered state. This is called if settings
            are changed so the Service can recalculate paths. """
        self.toggle(self._level)

    @property
    def active(self):
        return self.service is not None

    @classmethod
    def createHandler(cls, _handler_id, _type, *args, **kwargs):
        if _handler_id in cls.handlers and _type in cls.handlers[_handler_id]:
            return cls.handlers[_handler_id][_type](*args, **kwargs)
        return None

class PinHandler(IoHandler):
    _product_name = 'Generic GPIO'
    dbus_name = "digital"
    handler_id = 0
    type_id = 0
    def __init__(self, bus, base, pin, settings):
        super(PinHandler, self).__init__(bus, base, pin, settings)

        # We'll count the pulses for all types of services
        self.service.add_path('/Count', value=settings['count'])

    def save_count(self):
        if self.service is not None:
            self.settings['count'] = self.count

    @property
    def count(self):
        return self.service['/Count']

    @count.setter
    def count(self, v):
        self.service['/Count'] = v

class RelayHandler(IoHandler):
    _product_name = 'Relay'
    dbus_name = "relay"
    handler_id = 1
    type_id = 0
    def __init__(self, bus, base, relay, settings):
        super(RelayHandler, self).__init__(bus, base, relay, settings)

        self.service.add_path('/State', value=0, writeable=True, onchangecallback=self._on_relay_state_changed)
        self.relay = relay

    # Keep state in sync when changed from kernel
    def toggle(self, level):
        if self._level != level:
            self.service['/State'] = level
            self._level = level

    def _on_relay_state_changed(self, dbus_path, value):
        if value not in (0, 1):
            return False
        self.relay.setHWState(value)

        # Remember the state to restore after a restart
        self.settings['state'] = value
        self._level = value
        return True


class NopPin(object):
    """ Mixin for a pin with empty behaviour. Mix in BEFORE PinHandler so that
        __init__ overrides the base behaviour. """
    def __init__(self, bus, base, pin, settings):
        self.service = None
        self.bus = bus
        self.settings = settings
        self._level = 0 # Remember last state

    def deactivate(self):
        pass

    def toggle(self, level):
        self._level = level

    def save_count(self):
        # Do nothing
        pass

    @property
    def count(self):
        return self.settings['count']

    @count.setter
    def count(self, v):
        pass

    def refresh(self):
        pass


class DisabledPin(NopPin, PinHandler):
    """ Place holder for a disabled pin. """
    _product_name = 'Disabled'
    type_id = 0


class VolumeCounter(PinHandler):
    product_id = 0xA165
    _product_name = "Generic pulse meter"
    dbus_name = "pulsemeter"
    type_id = 1

    def __init__(self, bus, base, pin, settings):
        super(VolumeCounter, self).__init__(bus, base, pin, settings)
        self.service.add_path('/Aggregate', value=self.count*self.rate,
            gettextcallback=lambda p, v: (str(v) + ' cubic meter'))

    @property
    def rate(self):
        return self.settings['rate']

    def toggle(self, level):
        with self.service as s:
            super(VolumeCounter, self)._toggle(level, s)
            s['/Aggregate'] = self.count * self.rate

class TouchEnable(NopPin, PinHandler):
    """ The pin is used to enable/disable the Touch screen when toggled.
        No dbus-service is created. """
    _product_name = 'TouchEnable'
    type_id = 11

    def __init__(self, *args, **kwargs):
        super(TouchEnable, self).__init__(*args, **kwargs)
        self.item = VeDbusItemImport(self.bus,
            "com.victronenergy.settings", "/Settings/Gui/TouchEnabled")

    def toggle(self, level):
        super(TouchEnable, self).toggle(level)

        # Toggle the touch-enable setting on the downward edge.
        # Level is expected to be high with the switch open, and
        # pulled low when pushed.
        if level == 0:
            enabled = bool(self.item.get_value())
            self.item.set_value(int(not enabled))

    def deactivate(self):
        # Always re-enable touch when the pin is deactivated.
        # This adds another layer of protection against accidental
        # lockout.
        self.item.set_value(1)
        del self.item

class PinAlarm(PinHandler):
    product_id = 0xA166
    _product_name = "Generic digital input"
    dbus_name = "digitalinput"
    type_id = 0xFF
    translation = 0 # low, high

    def __init__(self, bus, base, pin, settings):
        super(PinAlarm, self).__init__(bus, base, pin, settings)
        self.service.add_path('/InputState', value=0)
        self.service.add_path('/State', value=self.get_state(0),
            gettextcallback=lambda p, v: TRANSLATIONS[v//2][v%2])
        self.service.add_path('/Alarm', value=self.get_alarm_state(0))

        # Also expose the type
        self.service.add_path('/Type', value=self.type_id,
            gettextcallback=lambda p, v: INPUTTYPES[v])

    def toggle(self, level):
        with self.service as s:
            super(PinAlarm, self)._toggle(level, s)
            s['/InputState'] = bool(level)*1
            s['/State'] = self.get_state(level)
            # Ensure that the alarm flag resets if the /AlarmSetting config option
            # disappears.
            s['/Alarm'] = self.get_alarm_state(level)

    def get_state(self, level):
        state = level ^ self.settings['invert']
        return 2 * self.translation + state

    def get_alarm_state(self, level):
        return 2 * bool(
            (level ^ self.settings['invertalarm']) and self.settings['alarm'])


class Generator(PinAlarm):
    _product_name = "Generator"
    type_id = 9
    translation = 5 # running, stopped
    startStopService = 'com.victronenergy.generator.startstop0'

    def __init__(self, bus, base, pin, settings):
        super(Generator, self).__init__(bus, base, pin, settings)
        self._gpio = pin.name
        # Periodically rewrite the generator selection. The Multi may reset
        # causing this to be lost, or a race condition on startup may cause
        # it to not be set properly.
        self._timer = GLib.timeout_add(30000,
            lambda: self.select_generator(self.level ^ self.settings['invert'] ^ 1) or True)

    def select_generator(self, v):
        # Find all vebus services, and let them know
        try:
            services = [n for n in self.bus.list_names() if n.startswith(
                'com.victronenergy.vebus.')]
            for n in services:
                self.bus.call_async(n, '/Ac/Control/RemoteGeneratorSelected', 'com.victronenergy.BusItem',
                    'SetValue', 'v', [v], None, None)
        except dbus.exceptions.DBusException:
            print ("DBus exception setting RemoteGeneratorSelected")
            traceback.print_exc()
        try:
            self.bus.call_async(self.startStopService, '/DigitalInput/Input', 'com.victronenergy.BusItem',
                    'SetValue', 'v', [self._gpio], None, None)
            self.bus.call_async(self.startStopService, '/DigitalInput/Running', 'com.victronenergy.BusItem',
                    'SetValue', 'v', [v], None, None)
        except dbus.exceptions.DBusException:
            print ("DBus exception setting RemoteGeneratorSelected")
            traceback.print_exc()

    def toggle(self, level):
        super(Generator, self).toggle(level)

        # Follow the same inversion sense as for display
        self.select_generator(level ^ self.settings['invert'] ^ 1)

    def deactivate(self):
        super(Generator, self).deactivate()
        # When deactivating, reset the generator selection state
        self.select_generator(0)
        try:
            self.bus.call_async(self.startStopService, '/DigitalInput/Input', 'com.victronenergy.BusItem',
                    'SetValue', 'v', [0], None, None)
        except dbus.exceptions.DBusException:
            pass
        # And kill the periodic job
        GLib.source_remove(self._timer)
        self._timer = None

# Various types of things we might want to monitor
class DoorSensor(PinAlarm):
    _product_name = "Door alarm"
    type_id = 2
    translation = 3 # open, closed

class BilgePump(PinAlarm):
    _product_name = "Bilge pump"
    type_id = 3
    translation = 1 # off, on

class BilgeAlarm(PinAlarm):
    _product_name = "Bilge alarm"
    type_id = 4
    translation = 4 # ok, alarm

class BurglarAlarm(PinAlarm):
    _product_name = "Burglar alarm"
    type_id = 5
    translation = 4 # ok, alarm

class SmokeAlarm(PinAlarm):
    _product_name = "Smoke alarm"
    type_id = 6
    translation = 4 # ok, alarm

class FireAlarm(PinAlarm):
    _product_name = "Fire alarm"
    type_id = 7
    translation = 4 # ok, alarm

class CO2Alarm(PinAlarm):
    _product_name = "CO2 alarm"
    type_id = 8
    translation = 4 # ok, alarm

class GenericIO(PinAlarm):
    _product_name = "Generic I/O"
    type_id = 10
    translation = 0 # low, high

class DisabledRelay(NopPin, RelayHandler):
    """ Place holder for a disabled relay. """
    _product_name = 'Disabled'
    type_id = 0

class AlarmRelay(RelayHandler):
    _product_name = "Alarm relay"
    dbus_name = "alarmrelay"
    type_id = 1

class GensetStartStopRelay(RelayHandler):
    _product_name = "Genset start/stop relay"
    dbus_name = "gensetstartstoprelay"
    type_id = 2

class ManualRelay(RelayHandler):
    _product_name = "Manual relay"
    dbus_name = "relay"
    type_id = 3

class TankPumpRelay(RelayHandler):
    _product_name = "Tank pump relay"
    dbus_name = "tankpumprelay"
    type_id = 4

class TemperatureRelay(RelayHandler):
    _product_name = "Temperature relay"
    dbus_name = "temperaturerelay"
    type_id = 5

class ConnectedGensetHelperRelay(RelayHandler):
    _product_name = "Connected genset helper relay"
    dbus_name = "connectedgensethelperrelay"
    type_id = 6


def dbusconnection():
    return SessionBus() if 'DBUS_SESSION_BUS_ADDRESS' in os.environ else SystemBus()

def parse_config(conf):
    f = open(conf)

    tag = None
    pins = []

    for line in f:
        cmd, arg = line.strip().split(maxsplit=1)

        if cmd == 'tag':
            tag = arg
            continue

        if cmd == 'input':
            pth, label = arg.split(maxsplit=1)
            label = label.strip('"')
            pin = InputPin(tag + '_' + os.path.basename(pth), pth, label)
            pins.append(pin)
            continue

        if cmd == 'relay':
            pth, label = arg.split(maxsplit=1)
            label = label.strip('"')

            basename = os.path.basename(pth)
            id = basename.split('_')[-1]
            pths = []
            for x in os.listdir(os.path.dirname(pth)):
                if x.startswith(os.path.basename(pth)):
                    pths.append(os.path.join(os.path.dirname(pth), x))
            pin = Relay.createRelay(id, tag + '_' + os.path.basename(pth), pths, label)
            pins.append(pin)
            continue

    f.close()

    return pins

def main():
    parser = ArgumentParser(description=sys.argv[0])
    parser.add_argument('--servicebase',
        help='Base service name on dbus, default is com.victronenergy',
        default='com.victronenergy')
    parser.add_argument('--poll',
        help='Use a different kind of polling. Options are epoll, dumb and debug',
        default='epoll')
    parser.add_argument('--conf', action='append', default=[], help='Config file')
    parser.add_argument('--inputs', nargs='*', help='Path to digital input')
    parser.add_argument('--relays', nargs='*', help='Path to relays')
    args = parser.parse_args()

    PulseCounter = {
        'debug': DebugPulseCounter,
        'poll': PollingPulseCounter,
    }.get(args.poll, EpollPulseCounter)

    DBusGMainLoop(set_as_default=True)

    ctlbus = dbusconnection()
    ctlsvc = VeDbusService(args.servicebase + '.digitalinputs', bus=ctlbus, register=True)

    # Keep track of enabled services
    services = {}
    inputs = dict(enumerate(args.inputs, 1))

    # Relays can be controlled by multiple outputs. Also feedback is possible.
    # monostable relays use 1 coil and can be named '/dev/gpio/relay_1'.
    # bistable relays use 2 coils and can be named '/dev/gpio/relay_1_set' and '/dev/gpio/relay_1_res'.
    # If there is a digital input for feedback, it should be named '/dev/gpio/relay_1_in'.

    # Map the passed paths to a dict:
    # {1: '/dev/gpio/relay_1', 2: ['/dev/gpio/relay_2_set', '/dev/gpio/relay_2_res', '/dev/gpio/relay_2_in']}

    relay_paths = {}
    for path in args.relays:
        relay_id = int(os.path.basename(path).split('_')[1])
        if relay_id not in relay_paths:
            relay_paths[relay_id] = [path]
        else:
            relay_paths[relay_id].append(path)

    pulses = PulseCounter() # callable that iterates over pulses

    def register_gpio(io_type, pin, bus, settings):
        _type = settings['type']
        print ("Registering {} {} for type {}".format(io_type, pin.name, _type))

        if io_type == 'digitalinput':
            handler = IoHandler.createHandler(0, _type, bus, args.servicebase, pin, settings)
        else:
            handler = RelayHandler.createHandler(1, _type, bus, args.servicebase, pin, settings)

        services[pin.name] = handler

        # Only monitor if enabled
        if _type > 0 and io_type == 'digitalinput':
            handler.level = pulses.register(pin.path, pin.name)
            handler.refresh()

    def unregister_gpio(gpio):
        print ("unRegistering GPIO {}".format(gpio))
        if pulses.registered(gpio):
            pulses.unregister(gpio)

        if services[gpio].active:
            services[gpio].deactivate()

    def handle_setting_change(io_type, pin, setting, old, new):
        # This handler may also be called if some attribute of a setting
        # is changed, but not the value. Bail if the value is unchanged.
        if old == new:
            return

        inp = pin.name

        if setting == 'type':
            if new:
                # Get current bus and settings objects, to be reused
                service = services[inp]
                bus, settings = service.bus, service.settings

                # Input enabled. If already enabled, unregister the old one first.
                if service.active:
                    unregister_gpio(inp)

                print("Registering {} {} for type {}".format(io_type, inp, new))
                if io_type == 'digitalinput':
                    # We only want 1 generator input at a time, so disable other inputs configured as generator.
                    for i in inputs:
                        if i != inp and services[i].settings['type'] == 9 == new:
                            services[i].settings['type'] = 0
                            unregister_gpio(i)

                    # Before registering the new input, reset its settings to defaults
                    settings['count'] = 0
                    settings['invert'] = 0
                    settings['invertalarm'] = 0
                    settings['alarm'] = 0

                # Register it
                register_gpio(io_type, pin, bus, settings)
            elif old:
                # Input disabled
                unregister_gpio(inp)

            ctlsvc['/Devices/{}/{}/Type'.format(io_type, inp)] = new
        elif setting in ('rate', 'invert', 'alarm', 'invertalarm'):
            services[inp].refresh()
        elif setting == 'name':
            services[inp].product_name = new
        elif setting == 'count':
            # Don't want this triggered on a period save, so only execute
            # if it has changed.
            v = int(new)
            s = services[inp]
            if s.active and s.count != v:
                s.count = v
                s.refresh()

    def change_type(io_type, sd, path, val):
        if io_type == 'digitalinput':
            if not 0 <= val < len(INPUTTYPES):
                return False
            sd['type'] = val
            return True
        elif io_type == 'relay':
            if not 0 <= val < len(RELAYTYPES):
                return False
            sd['type'] = val
            return True

    pins = []

    for inp, pth in inputs.items():
        pin = InputPin(inp, pth, 'Digital input {}'.format(inp))
        pin.devid = os.path.basename(pth)
        pin.devinstance = inp
        pins.append(pin)

    for inp, pths in relay_paths.items():
        relay = Relay.createRelay(inp, inp, pths, 'Relay {}'.format(inp))
        relay.devid = os.path.basename(pth)
        relay.devinstance = inp
        pins.append(relay)

    for conf in args.conf:
        pins += parse_config(conf)

    for pin in pins:
        io_type = pin.gpio_type
        s_name = 'DigitalInput' if io_type == 'digitalinput' else 'Relay'
        default_type = 0
        inp = pin.name
        devid = pin.devid or pin.name
        inst = '{}:{}'.format(io_type, pin.devinstance or 10)
        supported_settings = {
            'type': ['/Settings/{}/{}/Type'.format(s_name, inp), default_type, 0, len(INPUTTYPES)-1],
            'rate': ['/Settings/{}/{}/Multiplier'.format(s_name, inp), 0.001, 0, 1.0],
            'name': ['/Settings/{}/{}/CustomName'.format(s_name, inp), '', '', ''],
            'instance': ['/Settings/Devices/{}/ClassAndVrmInstance'.format(devid), inst, '', ''],
        }
        if io_type == 'digitalinput':
            supported_settings.update({
                'count': ['/Settings/{}/{}/Count'.format(s_name, inp), 0, 0, MAXCOUNT, 1],
                'invert': ['/Settings/{}/{}/InvertTranslation'.format(s_name, inp), 0, 0, 1],
                'invertalarm': ['/Settings/{}/{}/InvertAlarm'.format(s_name, inp), 0, 0, 1],
                'alarm': ['/Settings/{}/{}/AlarmSetting'.format(s_name, inp), 0, 0, 1],
            })
        elif io_type == 'relay':
            supported_settings.update({
                'state': ['/Settings/{}/{}/State'.format(s_name, inp), 0, 0, 1]
            })
        bus = dbusconnection()
        sd = SettingsDevice(bus, supported_settings, partial(handle_setting_change, io_type, pin), timeout=10)
        register_gpio(io_type, pin, bus, sd)
        ctlsvc.add_path('/Devices/{}/{}/Label'.format(io_type, inp), pin.label)
        ctlsvc.add_path('/Devices/{}/{}/Type'.format(io_type, inp), sd['type'],
                        writeable=True, onchangecallback=partial(change_type, io_type, sd))

    def poll(mainloop):
        from time import time
        idx = 0

        try:
            for inp, level in pulses():
                # epoll object only resyncs once a second. We may receive
                # a pulse for something that's been deregistered.
                try:
                    services[inp].toggle(level)
                except KeyError:
                    continue
        except:
            traceback.print_exc()
            mainloop.quit()

    # Need to run the gpio polling in separate thread. Pass in the mainloop so
    # the thread can kill us if there is an exception.
    mainloop = GLib.MainLoop()

    poller = Thread(target=lambda: poll(mainloop))
    poller.daemon = True
    poller.start()

    # Periodically save the counter
    def save_counters():
        for svc in services.values():
            svc.save_count()
        return True
    GLib.timeout_add(SAVEINTERVAL, save_counters)

    # Save counter on shutdown
    signal.signal(signal.SIGTERM, lambda *args: sys.exit(0))

    try:
        mainloop.run()
    except KeyboardInterrupt:
        pass
    finally:
        save_counters()

if __name__ == "__main__":
    main()
