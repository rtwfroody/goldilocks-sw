"""CircuitPython Essentials Storage logging boot.py file"""
import board		# pylint: disable-msg=import-error
import storage		# pylint: disable-msg=import-error
import neopixel		# pylint: disable-msg=import-error
import digitalio	# pylint: disable-msg=import-error

switch = digitalio.DigitalInOut(board.D5)

switch.direction = digitalio.Direction.INPUT
switch.pull = digitalio.Pull.UP

print("File system switch:", switch.value)

pixel = neopixel.NeoPixel(board.NEOPIXEL, 1, brightness=0.3,
                          auto_write=True, pixel_order=neopixel.GRB)
if switch.value:
    pixel[0] = (0, 0, 255, 0.5)
else:
    pixel[0] = (255, 0, 0, 0.5)

# If the switch pin is connected to ground CircuitPython can write to the drive
# storage.remount("/", switch.value)
