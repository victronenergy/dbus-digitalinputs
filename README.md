# dbus-digitalinputs

This is a service for venus on the beaglebone
([Venus GX](https://www.victronenergy.com/panel-systems-remote-monitoring/venus-gx)),
though it may be applicable to other platforms as well. It reads the digital input pins
and depending on the configuration, it will either count the pulses delivered
by an external meter and publish the count and volume measured, or simply publish the state
of an input so that it can be used by other services or as an alarm input.

# Running

Normally the service will be started by a daemontools run script. However, to run
it manually on a beaglebone aka Venus GX, use this command:

    python dbus_digitalinputs.py /dev/gpio/digital_input_*
    
On the first run it will create the user settings for the 5 digital inputs:

    /Settings/DigitalInput/x/Function   [0=Disabled, 1=Pulse meter, 2=Digital Input]
    /Settings/DigitalInput/x/Type       Only used when Function=2, [0=Door alarm, 1=Bilge alarm, 2=Burglar alarm, 3=Smoke alarm, 4=Fire alarm, 5=CO2 alarm]
    /Settings/DigitalInput/x/Multiplier cubic meters per pulse, defaults to 0.001
    /Settings/DigitalInput/x/Inverted   [0=Pin is active high, 1=Pin is active low]

It also creates one other path for each input:

    /Settings/DigitalInput/x/Count      non-volatile store for the actual pulse count

Inputs with their function set to pulse meter will create a service
`com.victronenergy.pulsemeter.input0x`, and these paths:

    /Aggregate  the measured amount in cubic meters
    /Count      counted pulses
    
Inputs with their function set to digital input will create a service
`com.victronenergy.digitalinput.input0x`, and these dbus paths:

    /State      0 when active, 1 when inactive
    /Count      count of active pulses
    /Type       Text string describing the input type
