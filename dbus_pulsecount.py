import sys, os
from threading import Thread
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'ext', 'velib_python'))

from dbus.mainloop.glib import DBusGMainLoop
import gobject
from vedbus import VeDbusService

def pulses(path):
    from select import epoll, EPOLLPRI, EPOLLERR

    while os.path.islink(path):
        path = os.path.realpath(os.readlink(path))

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
    dbusservice.add_path('/Count', value=0)
    dbusservice.add_path('/Frequency', value=0)


    def poll():
        from time import time
        stamps = [0] * 5
        idx = 0
        for _ in pulses('/dev/gpio/digital_input_1'):
            dbusservice['/Count'] += 1

            now = time()
            stamps[idx] = now
            idx = (idx+1) % len(stamps)

            dbusservice['/Frequency'] = round((len(stamps)-1)/(now-min(stamps)), 2)

    # Need to run the gpio polling in separate thread. This will be done
    # using epoll(), so it will be very efficient.
    gobject.threads_init()
    poller = Thread(target=poll)
    poller.daemon = True
    poller.start()

    gobject.MainLoop().run()

if __name__ == "__main__":
    main()
