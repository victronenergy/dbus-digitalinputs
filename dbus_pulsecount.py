#!/usr/bin/python -u

import sys, os
import signal
from threading import Thread
from select import select, epoll, EPOLLPRI
from functools import partial
from argparse import ArgumentParser
import traceback
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'ext', 'velib_python'))

from dbus.mainloop.glib import DBusGMainLoop
import gobject
from vedbus import VeDbusService
from settingsdevice import SettingsDevice

MAXCOUNT = 2**31-1
SAVEINTERVAL = 60000

INPUT_FUNCTION_COUNTER = 1
INPUT_FUNCTION_ALARM = 2

class EpollPulseCounter(object):
    def __init__(self):
        self.fdmap = {}
        self.gpiomap = {}
        self.ob = epoll()

    def register(self, path, gpio):
        path = os.path.realpath(path)

        # Set up gpio for rising edge interrupts
        with open(os.path.join(os.path.dirname(path), 'edge'), 'ab') as fp:
            fp.write('both')

        fp = open(path, 'rb')
        self.fdmap[fp.fileno()] = gpio
        self.gpiomap[gpio] = fp
        self.ob.register(fp, EPOLLPRI)

    def unregister(self, gpio):
        fp = self.gpiomap[gpio]
        self.ob.unregister(fp)
        del self.gpiomap[gpio]
        del self.fdmap[fp.fileno()]
        fp.close()

    def registered(self, gpio):
        return gpio in self.gpiomap

    def __call__(self):
        while True: 
            # We have a timeout of 1 second on the poll, because poll() only
            # looks at files in the epoll object at the time poll() was called.
            # The timeout means we let other files (added via calls to
            # register/unregister) into the loop at least that often.
            for fd, evt in self.ob.poll(1):
                os.lseek(fd, 0, os.SEEK_SET)
                v = os.read(fd, 1)
                yield self.fdmap[fd], int(v)


def main():
    parser = ArgumentParser(description=sys.argv[0])
    parser.add_argument('inputs', nargs='+', help='Path to digital input')
    args = parser.parse_args()

    DBusGMainLoop(set_as_default=True)
    dbusservice = VeDbusService('com.victronenergy.digitalinput')

    inputs = dict(enumerate(args.inputs, 1))
    pulses = EpollPulseCounter() # callable that iterates over pulses

    def register_gpio(path, gpio, f):
        print "Registering GPIO {} for function {}".format(gpio, f)
        dbusservice.add_path('/{}/Count'.format(gpio), value=0)
        dbusservice['/{}/Count'.format(gpio)] = settings[gpio]['count']
        if f == INPUT_FUNCTION_COUNTER:
            dbusservice.add_path('/{}/Volume'.format(gpio), value=0)
            dbusservice['/{}/Volume'.format(gpio)] = settings[gpio]['count'] * settings[gpio]['rate']
        elif f == INPUT_FUNCTION_ALARM:
            dbusservice.add_path('/{}/Alarm'.format(gpio), value=0)
        pulses.register(path, gpio)

    def unregister_gpio(gpio):
        print "unRegistering GPIO {}".format(gpio)
        pulses.unregister(gpio)
        for pth in ('Count', 'Volume', 'Alarm'):
            k = '/{}/{}'.format(gpio, pth)
            if k in dbusservice:
                del dbusservice[k]

    # Interface to settings
    def handle_setting_change(inp, setting, old, new):
        if setting == 'function':
            if new:
                # Input enabled. If already enabled, unregister the old one first.
                if pulses.registered(inp):
                    unregister_gpio(inp)
                register_gpio(inputs[inp], inp, int(new))
            elif old:
                # Input disabled
                unregister_gpio(inp)

    settings = {}
    for inp, pth in inputs.items():
        supported_settings = {
            'function': ['/Settings/DigitalInput/{}/Function'.format(inp), 0, 0, 2],
            'rate': ['/Settings/DigitalInput/{}/LitersPerPulse'.format(inp), 1, 1, 100],
            'count': ['/Settings/DigitalInput/{}/Count'.format(inp), 0, 0, MAXCOUNT, 1]
        }
        settings[inp] = sd = SettingsDevice(dbusservice.dbusconn, supported_settings, partial(handle_setting_change, inp), timeout=10)
        if sd['function'] > 0:
            register_gpio(pth, inp, int(sd['function']))

    def poll(mainloop):
        from time import time
        #stamps = { inp: [0] * 5 for inp in gpios }
        idx = 0

        try:
            for inp, level in pulses():
                function = settings[inp]['function']

                # Only increment Count on rising edge.
                if level:
                    countpath = '/{}/Count'.format(inp)
                    v = (dbusservice[countpath]+1) % MAXCOUNT
                    dbusservice[countpath] = v
                    if function == INPUT_FUNCTION_COUNTER:
                        dbusservice['/{}/Volume'.format(inp)] = v * settings[inp]['rate']

                if function == INPUT_FUNCTION_ALARM:
                    dbusservice['/{}/Alarm'.format(inp)] = bool(level)*2 # Nasty way of limiting to 0 or 2.
        except:
            traceback.print_exc()
            mainloop.quit()

    # Need to run the gpio polling in separate thread. Pass in the mainloop so
    # the thread can kill us if there is an exception.
    gobject.threads_init()
    mainloop = gobject.MainLoop()

    poller = Thread(target=lambda: poll(mainloop))
    poller.daemon = True
    poller.start()

    # Periodically save the counter
    def _save_counters():
        for inp in inputs:
            if settings[inp]['function'] > 0:
                settings[inp]['count'] = dbusservice['/{}/Count'.format(inp)]

    def save_counters():
        _save_counters()
        gobject.timeout_add(SAVEINTERVAL, save_counters)
    gobject.timeout_add(SAVEINTERVAL, save_counters)

    # Save counter on shutdown
    signal.signal(signal.SIGTERM, lambda *args: sys.exit(0))

    try:
        mainloop.run()
    except KeyboardInterrupt:
        pass
    finally:
        _save_counters()

if __name__ == "__main__":
    main()
