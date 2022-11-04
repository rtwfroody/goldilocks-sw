"""CircuitPython Essentials Storage logging boot.py file"""
import board		# pylint: disable-msg=import-error
import storage		# pylint: disable-msg=import-error
import neopixel		# pylint: disable-msg=import-error
import digitalio	# pylint: disable-msg=import-error

# D5 is pin labeled 33 (for IO33) on the board.
switch = digitalio.DigitalInOut(board.D5)

switch.direction = digitalio.Direction.INPUT
switch.pull = digitalio.Pull.UP

print("File system switch:", switch.value)

pixel = neopixel.NeoPixel(board.NEOPIXEL, 1, brightness=0.3,
                          auto_write=True, pixel_order=neopixel.GRB)
if switch.value:
    # Allow programming
    pixel[0] = (0, 0, 255, 0.5)
else:
    # Let code.py access storage.
    storage.remount("/", switch.value)
    pixel[0] = (255, 0, 0, 0.5)
