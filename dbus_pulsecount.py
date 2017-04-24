import sys, os
import signal
from threading import Thread
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'ext', 'velib_python'))

from dbus.mainloop.glib import DBusGMainLoop
import gobject
from vedbus import VeDbusService
from settingsdevice import SettingsDevice

MAXCOUNT = 2**31-1
SAVEINTERVAL = 60000

def pulses(path):
    from select import epoll, EPOLLPRI, EPOLLERR

    path = os.path.realpath(path)

    # Set up gpio for rising edge interrupts
    with open(os.path.join(os.path.dirname(path), 'edge'), 'ab') as fp:
        fp.write('rising')

    fp = open(path, 'rb')
    ob = epoll()
    ob.register(fp, EPOLLPRI | EPOLLERR)
    while True: 
        for fd, evt in ob.poll():
            if evt == EPOLLERR:
                raise IOError("poll failed")
            fp.seek(0)
            fp.read();
            yield 1

def main():
    DBusGMainLoop(set_as_default=True)
    dbusservice = VeDbusService('com.victronenergy.pulsecount')

    inputs = ['digital_input_{}'.format(i) for i in range(1, 3)]

    # Interface to settings, to store pulse count
    supported_settings = {
        inp: ['/Settings/PulseCount/{}/Count'.format(inp), 0, 0, MAXCOUNT] for inp in inputs
    }
    counts = SettingsDevice(dbusservice.dbusconn, supported_settings, lambda *args: None, timeout=10)

    def poll(gpio):
        from time import time
        path = os.path.join('/dev/gpio', gpio)
        stamps = [0] * 5
        idx = 0
        for _ in pulses(path):
            countpath = '/{}/Count'.format(gpio)
            dbusservice[countpath] = (dbusservice[countpath]+1) % MAXCOUNT

            now = time()
            stamps[idx] = now
            idx = (idx+1) % len(stamps)

            dbusservice['/{}/Frequency'.format(gpio)] = round((len(stamps)-1)/(now-min(stamps)), 2)

    # Need to run the gpio polling in separate threads. This will be done
    # using epoll(), so it will be very efficient.
    gobject.threads_init()

    for inp in inputs:
        dbusservice.add_path('/{}/Count'.format(inp), value=0)
        dbusservice.add_path('/{}/Frequency'.format(inp), value=0)
        dbusservice['/{}/Count'.format(inp)] = counts[inp]

        poller = Thread(target=lambda: poll(inp))
        poller.daemon = True
        poller.start()

    # Periodically save the counter
    def save_counter():
        for inp in inputs:
            counts[inp] = dbusservice['/{}/Count'.format(inp)]
        gobject.timeout_add(SAVEINTERVAL, save_counter)
    gobject.timeout_add(SAVEINTERVAL, save_counter)

    # Save counter on shutdown
    signal.signal(signal.SIGTERM, lambda *args: sys.exit(0))

    try:
        gobject.MainLoop().run()
    except KeyboardInterrupt:
        pass
    finally:
        for inp in inputs:
            counts[inp] = dbusservice['/{}/Count'.format(inp)]

if __name__ == "__main__":
    main()
