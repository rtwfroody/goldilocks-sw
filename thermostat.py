"""
Thermostat head unit. Features:
* GUI
* Controls heat pump
* Gets remote sensor readings
* Easily accessible presets

Code intended for ESP32-S3 Feather
"""

# pylint: disable-msg=missing-function-docstring
# pylint: disable-msg=invalid-name
# pylint: disable-msg=fixme
# pylint: disable-msg=too-many-instance-attributes
# pylint: disable-msg=too-few-public-methods
# pylint: disable-msg=too-many-arguments

import json
import time
import traceback

import secrets

import adafruit_ili9341
import adafruit_ntp
import adafruit_pcf8523
import board    # pylint: disable-msg=import-error
import busio    # pylint: disable-msg=import-error
import digitalio    # pylint: disable-msg=import-error
import displayio    # pylint: disable-msg=import-error
import neopixel     # pylint: disable-msg=import-error
import socketpool   # pylint: disable-msg=import-error
import wifi # pylint: disable-msg=import-error
from adafruit_bitmap_font import bitmap_font # pylint: disable-msg=import-error
from adafruit_button import Button
from adafruit_display_text import label # pylint: disable-msg=import-error
from adafruit_minimqtt import adafruit_minimqtt # pylint: disable-msg=no-name-in-module
from adafruit_stmpe610 import Adafruit_STMPE610_SPI
from adafruit_bme280 import basic as adafruit_bme280 # pylint: disable-msg=import-error
from HeatPump import HeatPump
from priority_queue import PriorityQueue

def celsius_to_fahrenheit(celsius):
    return celsius * 9 / 5 + 32

class Settings():
    """Track settings that can be stored/restored on disk."""
    path = "/goldilocks.json"

    def __init__(self):
        self.data = {
            "temp_high": 80,
            "temp_low": 60,
            "preset": None
        }
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as fd:
                self.data.update(json.load(fd))
        except OSError as e:
            print(e)

    def save(self):
        with open(self.path, "w", encoding="utf-8") as fd:
            json.dump(fd, self.data)

class Mqtt():
    """"Get temperature updates from an MQTT server."""
    mqtt_prefix = "goldilocks/sensor/temperature_F/"
    def __init__(self, server, port, username, password, network):
        self.client = None
        self.server = server
        self.port = port
        self.username = username
        self.password = password
        self.network = network
        self.temperatures = {}

    def on_message(self, _client, topic, message):
        print(f"New message on topic {topic}: {message}")
        location = topic[len(self.mqtt_prefix):]
        value = float(message)
        self.temperatures[location] = value

    def connect(self):
        if self.client:
            return
        socket_pool = self.network.socket_pool()
        if not socket_pool:
            return
        print(f"MQTT connecting to {self.server}:{self.port}")
        self.client = adafruit_minimqtt.MQTT(
            broker=self.server,
            port=self.port,
            username=self.username,
            password=self.password,
            socket_pool=socket_pool)
        self.client.on_message = self.on_message
        try:
            self.client.connect()
            self.client.subscribe(self.mqtt_prefix + "#")
        except OSError as e:
            print(f"Failed to connect to {self.server}:{self.port}: {e}")

    def poll(self):
        if not self.client:
            return []
        try:
            self.client.loop(0)
        except (adafruit_minimqtt.MMQTTException, OSError, AttributeError) as e:
            print("MQTT loop() raised:")
            traceback.print_exception(e, e, e.__traceback__)
            try:
                self.client.disconnect()
            except (adafruit_minimqtt.MMQTTException, OSError, AttributeError) as e:
                print("MQTT disconnect() raised:")
                traceback.print_exception(e, e, e.__traceback__)
            self.client = None
            # We'll connect again the next poll()
        retval = list(self.temperatures.items())
        self.temperatures = {}
        return retval

class TouchScreenEvent():
    """Represent a touch screen event."""
    DOWN = 0
    UP = 1
    DRAG = 2
    def __init__(self, typ, x, y):
        self.typ = typ
        self.x = x
        self.y = y

    def __repr__(self):
        return f"TouchScreenEvent({self.typ}, {self.x}, {self.y})"

class TouchScreenEvents():
    """Monitor a touch screen for events."""
    def __init__(self, touch):
        self.touch = touch
        self.last = None

    def poll(self):
        point = self.touch.touch_point
        last = self.last
        self.last = point
        if point:
            x, y, _pressure = point
            if last:
                return TouchScreenEvent(TouchScreenEvent.DRAG, x, y)
            return TouchScreenEvent(TouchScreenEvent.DOWN, x, y)

        if last:
            return TouchScreenEvent(TouchScreenEvent.UP, last[0], last[1])

        return None

class Gui():
    """Display a GUI."""
    # TODO: play with backlight brightness,
    # https://learn.adafruit.com/making-a-pyportal-user-interface-displayio/display
    presets = {
        "Sleep": (58, 74),
        "Away": (64, 79),
        "Home": (68, 75)
    }

    def __init__(self, settings, spi):
        self.settings = settings

        self.fonts = {}
        self.load_font("b24", "DejaVuSansMono-Bold-24")

        self.width = 320
        self.height = 240

        # Set up display
        displayio.release_displays()
        display_bus = displayio.FourWire(spi, command=board.D10, chip_select=board.D9)
        self.display = adafruit_ili9341.ILI9341(display_bus, width=self.width, height=self.height)

        self.preset_buttons = {}

        splash = self.make_splash()
        self.display.show(splash)

        self.load_font("b18", "DejaVuSansMono-Bold-18")
        self.load_font("b12", "DejaVuSansMono-Bold-12")
        self.load_font("b8", "DejaVuSansMono-Bold-8")
        self.load_font("8", "DejaVuSansMono-8")
        self.load_font("12", "DejaVuSansMono-12")
        self.load_font("18", "DejaVuSansMono-18")

        # Set up touchscreen
        touch_cs = digitalio.DigitalInOut(board.D6)
        try:
            touch = Adafruit_STMPE610_SPI(spi, touch_cs,
                    calibration=((276, 3820), (378, 3743)))
        except RuntimeError as e:
            print("No touch screen connected!")
            print(e)
            self.tse = None
        else:
            self.tse = TouchScreenEvents(touch)

        self.make_main()

        self.selected = None

    def load_font(self, name, filename):
        self.fonts[name] = bitmap_font.load_font(f"font/{filename}.pcf")

    def make_splash(self):
        # Make the display context
        splash = displayio.Group()

        # Draw a label
        text_area = label.Label(self.fonts["b24"], text="Goldilocks", color=0xFFFF00)
        text_area.x = int((self.width - text_area.width) / 2)
        text_area.y = int((self.height - text_area.height) / 2)
        splash.append(text_area)
        return splash

    def select_preset(self, name):
        self.settings.temp_low, self.settings.temp_high = self.presets[name]
        self.low_label.text = f"{self.settings.temp_low:.0f}F"
        self.high_label.text = f"{self.settings.temp_high:.0f}F"

        for button_name, button in self.preset_buttons.items():
            if name == button_name:
                button.fill_color = 0x8fff8f
            else:
                button.fill_color = 0xffffff

    def make_main(self):
        self.main_group = displayio.Group()
        spacing = 10
        button_height = 40
        group_x = int(spacing/2)
        group_y = int(self.height - button_height - spacing/2)
        self.main_buttons = []
        for i, name in enumerate(self.presets):
            button = Button(
                x=group_x + int(i * self.width / len(self.presets) + spacing / 2),
                y=group_y,
                width=int(self.width / len(self.presets) - spacing),
                height = button_height,
                label=name,
                label_font=self.fonts["b18"],
                style=Button.ROUNDRECT)
            button.pressed = lambda name=name: self.select_preset(name)
            self.preset_buttons[name] = button
            self.main_buttons.append(button)
            self.main_group.append(button)

        info_group = displayio.Group(x=int(spacing/2), y=int(spacing/2))
        self.time_label = label.Label(self.fonts["12"], text="time", color=0xFFFFFF, x=10, y=10)
        self.low_label = label.Label(self.fonts["18"], x=10, y=50, color=0x9f9fff)
        self.high_label = label.Label(self.fonts["18"], x=260, y=50, color=0xff9f9f)
        self.avg_label = label.Label(self.fonts["b24"], x=120, y=50, color=0xffffff)
        self.temperature_label = label.Label(self.fonts["12"], color=0xFF80FF, x=10, y=100)
        info_group.append(self.low_label)
        info_group.append(self.high_label)
        info_group.append(self.time_label)
        info_group.append(self.temperature_label)
        info_group.append(self.avg_label)
        self.main_group.append(info_group)

    def update_time(self, t):
        # pylint: disable-msg=consider-using-f-string
        self.time_label.text = "%04d-%02d-%02d %02d:%02d:%02d" % (
            t.tm_year, t.tm_mon, t.tm_mday,
            t.tm_hour, t.tm_min, t.tm_sec
        )

    def update_temperatures(self, temps, overall):
        self.temperature_label.text = "\n".join(f"{k}: {v}" for k, v in temps.items())
        self.avg_label.text = f"{overall:.1f}F"

    def show_main(self):
        self.display.show(self.main_group)

    def poll(self):
        if not self.tse:
            return
        event = self.tse.poll()
        if event:
            x = int(self.width * event.x / 4096)
            y = int(self.height * event.y / 4096)
            if self.selected:
                if self.selected.contains((x, y)):
                    if event.typ == TouchScreenEvent.UP:
                        self.selected.pressed()
                    return

                self.selected.selected = False
                self.selected = None

            for button in self.main_buttons:
                if button.contains((x, y)):
                    button.selected = True
                    self.selected = button
        else:
            if self.selected:
                self.selected.selected = False
                self.selected = None

class Network():
    """Connect to a network.
    If we could detect when we get disconnected, then we could automatically
    reconnect as well."""
    def __init__(self, ssid, password):
        self.ssid = ssid
        self.password = password
        self._socket_pool = None
        # wifi.radio doesn't have method that indicates whether it's connected?
        self.connected = False

    def connect(self):
        if self.connected:
            return
        print("Connecting to", self.ssid)
        try:
            wifi.radio.connect(self.ssid, self.password)
        except ConnectionError as e:
            print(f"connect to {self.ssid}: {e}")
            return
        print("Connected to", self.ssid)
        self._socket_pool = socketpool.SocketPool(wifi.radio)
        self.connected = True

    def socket_pool(self):
        return self._socket_pool

class Task():
    """Simple task."""
    def __init__(self, fn=None):
        self.fn = fn

    def run(self):
        return self.fn()

class RepeatTask():
    """Task that needs to be repeated over and over with a given period."""
    def __init__(self, fn, period):
        self.fn = fn
        self.period = period

    def run(self):
        self.fn()
        return self.period

class Datum():
    """Store a single sensor reading."""
    def __init__(self, value, timestamp=None):
        self.value = value
        self.timestamp = timestamp or time.monotonic()

    def __repr__(self):
        return f"Datum({self.value}, {self.timestamp})"

    def __str__(self):
        ago = time.monotonic() - self.timestamp
        return f"{self.value:.1f} {ago:.1f}s ago"

class TaskRunner():
    """Run tasks, most urgent first."""
    def __init__(self):
        # Array of (next run time, Task)
        self.task_queue = PriorityQueue()

    def add(self, task, delay=0):
        self.task_queue.add(task, -time.monotonic() - delay)

    def run(self):
        now = time.monotonic()
        # pylint: disable-msg=invalid-unary-operand-type
        run_time = -self.task_queue.peek_priority()
        if run_time <= now:
            task = self.task_queue.pop()
            run_after = task.run()
            if run_after:
                next_time = run_time + run_after
                if next_time < now:
                    print(f"Can't run {self} after {run_after}s because we're already too late.")
                    next_time = now + run_after
                self.task_queue.add(task, -next_time)

class Thermostat():
    """Top-level class for the thermostat application with GUI and temperature
    control."""
    def __init__(self):
        self.settings = Settings()

        ### Hardware devices
        # Get splash screen going first.
        spi = board.SPI()
        self.gui = Gui(self.settings, spi)

        self.task_runner = TaskRunner()

        self.pixel_index = 0
        self.pixel = neopixel.NeoPixel(board.NEOPIXEL, 1, brightness=0.3,
                                       auto_write=True, pixel_order=neopixel.GRB)
        self.task_runner.add(RepeatTask(self.cycle_pixel, 1))

        try:
            i2c = board.I2C()
        except RuntimeError as e:
            print("No I2C bus found!")
            print(e)
        else:
            self.rtc = adafruit_pcf8523.PCF8523(i2c)
            self.task_runner.add(RepeatTask(
                lambda: self.gui.update_time(self.now()),
                1))
            self.task_runner.add(RepeatTask(self.sync_time, 12 * 3600), 10)

            self.bme280 = adafruit_bme280.Adafruit_BME280_I2C(i2c)
            self.task_runner.add(RepeatTask(self.poll_local_temp, 1))

        # Start network, and use it.
        self.network = Network(secrets.SSID, secrets.PASSWORD)
        self.task_runner.add(RepeatTask(self.network.connect, 10))

        self.mqtt = Mqtt(secrets.MQTT_SERVER, secrets.MQTT_PORT,
                         secrets.MQTT_USERNAME, secrets.MQTT_PASSWORD,
                         self.network)
        self.task_runner.add(RepeatTask(self.mqtt.connect, 10), 1)
        self.task_runner.add(RepeatTask(self.poll_mqtt, 10), 0.5)

        self.uart = busio.UART(board.TX, board.RX, baudrate=2400, bits=8,
                               parity=busio.UART.Parity.EVEN, stop=1, timeout=0)

        self.heatPump = HeatPump(self.uart)
        def check_heat_pump():
            if not self.heatPump.connected():
                self.heatPump.connect()
        self.task_runner.add(RepeatTask(check_heat_pump, 15))

        ### Local variables.
        self.temperatures = {}
        self.last_stamp = 0

        self.min_point = [10000, 10000, 10000]
        self.max_point = [0, 0, 0]

    def cycle_pixel(self):
        if (self.pixel_index % 3) == 0:
            self.pixel[0] = (255, 0, 0, 0.5)
        elif (self.pixel_index % 3) == 1:
            self.pixel[0] = (0, 255, 0, 0.5)
        elif (self.pixel_index % 3) == 2:
            self.pixel[0] = (0, 0, 255, 0.5)
        self.pixel_index += 1

    def poll_local_temp(self):
        self.temperatures["head"] = Datum(celsius_to_fahrenheit(self.bme280.temperature))
        self.temperature_updated()

    def poll_mqtt(self):
        temperature_updates = self.mqtt.poll()
        for (k, v) in temperature_updates:
            self.temperatures[k] = Datum(v)
        self.temperature_updated()

    def temperature_updated(self):
        if self.temperatures:
            overall_temperature = sum(v.value for v in self.temperatures.values()) / \
                    len(self.temperatures)
        else:
            overall_temperature = 70
        self.gui.update_temperatures(self.temperatures, overall_temperature)

    def sync_time(self):
        socket_pool = self.network.socket_pool()
        if not socket_pool:
            return
        # TODO: How do you deal with timezones?
        ntp = adafruit_ntp.NTP(socket_pool, tz_offset=-7)
        # Sync at boot up
        try:
            self.rtc.datetime = ntp.datetime
        except OSError as e:
            # Doesn't always work.
            print(f"NTP failed: {e}")

    @staticmethod
    def error(error):
        print(error)

    def now(self):
        return self.rtc.datetime

    def stamp(self, text=""):
        now = time.monotonic()
        print(f"{now - self.last_stamp}s {text}")
        self.last_stamp = now

    def run(self):
        self.gui.show_main()
        while True:
            self.task_runner.run()

            self.gui.poll()

            self.heatPump.poll()

            # Does this save power?
            #time.sleep(0.1)

def main():
    thermostat = Thermostat()
    thermostat.run()
