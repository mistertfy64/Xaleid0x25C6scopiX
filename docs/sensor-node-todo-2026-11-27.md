# Sensor Node

## Context
The sensor node (ESP32) currently has the following:
- A heart rate sensor
- A body/infrared temperature sensor

The sensor node (ESP32) will have these components added to it:
- A DHT11 temperature sensor
- A keypad (with keys 0 to 9, A to D, #, and *)
- 2 servo motors. These can rotate all the way to 360 degrees. 
- A display

## Target
Add the following features:
- With the DHT11, measure the room temperature, and include it in the sent payload (to the gateway) as well.
- With the keypad:
  - Create a "user account" system where a user enters their number (user ID, some number of digits long) and a password, and include it in the sent payload (to the gateway) as well. (For now, ignore if the user/password is actually correct/exists, this will be handled in the gateway later.)
  - Additionally, the keypad has some "special" keys as mentioned above, use a special key to signify "guest mode", which will prevent any payload from being sent.
- With the 2 servo motors:
  - When a user hasn't entered their number and hasn't choose to measure as a guest (i.e., no data or did not signify as a guest), have the servo motors set in one position. Otherwise, set it in another position, preferably 90 degrees from the current position (like a trapdoor.) Do this for both servo motors at the same time.
- With the display: Display sensor data and health data in a way that it can fit. Note that not everything does have to fit, although it should show the most important information in a reasonable way.

## Examples for references
The current payload format sent is in the `sensor/node/node.ino file`, in the `publishReading()` function.

Be sure to ask the model of the display before starting, and ask any questions if something is unsure.
