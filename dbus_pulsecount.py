import sys, os
import signal
from threading import Thread
from select import select, epoll, EPOLLPRI
from functools import partial
import traceback
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'ext', 'velib_python'))

from dbus.mainloop.glib import DBusGMainLoop
import gobject
from vedbus import VeDbusService
from settingsdevice import SettingsDevice

MAXCOUNT = 2**31-1
SAVEINTERVAL = 60000
NUM_INPUTS = 5

class EpollPulseCounter(object):
    def __init__(self, path):
        self.path = path
        self.fdmap = {}
        self.gpiomap = {}
        self.ob = epoll()

    def register(self, gpio):
        path = os.path.join(self.path, 'digital_input_{}'.format(gpio))
        path = os.path.realpath(path)

        # Set up gpio for rising edge interrupts
        with open(os.path.join(os.path.dirname(path), 'edge'), 'ab') as fp:
            fp.write('rising')

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

    def __call__(self):
        while True: 
            for fd, evt in self.ob.poll(1):
                os.lseek(fd, 0, os.SEEK_SET)
                os.read(fd, 1)
                yield self.fdmap[fd]


def main():
    DBusGMainLoop(set_as_default=True)
    dbusservice = VeDbusService('com.victronenergy.digitalinput')

    inputs = range(1, NUM_INPUTS+1)
    pulses = EpollPulseCounter('/dev/gpio') # callable that iterates over pulses

    def register_gpio(gpio):
        dbusservice.add_path('/{}/Count'.format(gpio), value=0)
        dbusservice.add_path('/{}/Volume'.format(gpio), value=0)
        dbusservice['/{}/Count'.format(gpio)] = settings[gpio]['count']
        dbusservice['/{}/Volume'.format(gpio)] = settings[gpio]['count'] * settings[gpio]['rate']
        pulses.register(gpio)

    def unregister_gpio(gpio):
        pulses.unregister(gpio)
        del dbusservice['/{}/Count'.format(gpio)]
        del dbusservice['/{}/Volume'.format(gpio)]

    # Interface to settings
    def handle_setting_change(inp, setting, old, new):
        if setting == 'function':
            if new:
                # Input enabled
                register_gpio(inp)
            else:
                # Input disabled
                unregister_gpio(inp)

    settings = {}
    for inp in inputs:
        supported_settings = {
            'function': ['/Settings/DigitalInput/{}/Function'.format(inp), 0, 0, 2],
            'rate': ['/Settings/DigitalInput/{}/LitersPerPulse'.format(inp), 1, 1, 100],
            'count': ['/Settings/DigitalInput/{}/Count'.format(inp), 0, 0, MAXCOUNT, 1]
        }
        settings[inp] = sd = SettingsDevice(dbusservice.dbusconn, supported_settings, partial(handle_setting_change, inp), timeout=10)
        if sd['function'] > 0:
            register_gpio(inp)

    def poll(mainloop):
        from time import time
        #stamps = { inp: [0] * 5 for inp in gpios }
        idx = 0

        try:
            for inp in pulses():
                countpath = '/{}/Count'.format(inp)
                v = (dbusservice[countpath]+1) % MAXCOUNT
                dbusservice[countpath] = v
                dbusservice['/{}/Volume'.format(inp)] = v * settings[inp]['rate']
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
