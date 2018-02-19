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

    /Settings/DigitalInput/x/Type
      0 = Disabled
      1 = Pulse meter
      2 = Door
      3 = Bilge pump
      4 = Bilge alarm
      5 = Burglar alarm
      6 = Smoke alarm
      7 = Fire alarm
      8 = CO2 alarm
      9 = Generator
    /Settings/DigitalInput/x/Multiplier        for Type=1, cubic meters per pulse, defaults to 0.001
    /Settings/DigitalInput/x/InvertTranslation Swaps the interpretation of the logic, for inputs that are active low
    /Settings/DigitalInput/x/AlarmSetting      When Type!=1, whether to raise an alarm if the pin is active

It also creates one other path for each input:

    /Settings/DigitalInput/x/Count      non-volatile store for the actual pulse count

Inputs with their type set to pulse meter will create a service
`com.victronenergy.pulsemeter.input0x`, and these paths:

    /Aggregate  the measured amount in cubic meters
    /Count      counted pulses
    
Inputs with their type set to a digital input will create a service
`com.victronenergy.digitalinput.input0x`, and these dbus paths:

    /Alarm  if /AlarmSetting is set above, this will indicate an alarm condition.
    /Count  count of active pulses
    /State One of the below, depending on the selected type
      0 = low
      1 = high
      2 = off
      3 = on
      4 = no
      5 = yes
      6 = open
      7 = closed
      8 = ok
      9 = alarm
      10 = running
      11 = stopped

    /Type   integer reflecting the type as documented above. Calling GetText returns a text string.
