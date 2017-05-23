# dbus-pulsecounter

Service for ccgx/venus on beaglebone. Reads an analog input and counts the
pulses delivered by an external meter. Publish results on dbus.

# Running

Normally the service will be started by a daemontools run script. However, to run
it manually on a beaglebone aka Venus GX, use this command:

    python dbus_pulsecount.py /dev/gpio/digital_input_*
    
On the first run it will create the user settings for the 5 digital inputs:

    /Settings/DigitalInput/x/Function        [0=Disabled, 1=Pulse counter, 2=Digital Input]
    /Settings/DigitalInput/x/LitersPerPulse  liters per pulse

And it creates one other path for each input:

    /Settings/DigitalInput/x/Count           non-volatile store for the actual pulse count

Inputs set to pulse count will have these dbus paths:

    x/Volume                                 the amount of liters
    x/Count                                  counted pulses
    
Inputs set to Alarm with have these dbus paths:

    x/Alarm                                  0 when closed, 1 when open
    x/Count                                  counts the pulses, its not necessary to use
                                             this for an alarm input
