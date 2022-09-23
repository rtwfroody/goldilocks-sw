"""
Thermostat head unit. Features:
    * GUI
    * Controls heat pump
    * Gets remote sensor readings
    * Easily accessible presets
"""

# pylint: disable-msg=missing-function-docstring
# pylint: disable-msg=invalid-name

import adafruit_ili9341
import adafruit_ntp
import adafruit_pcf8523
import board
import digitalio
import displayio
import json
import secrets
import socketpool
import time
import traceback
import wifi
from adafruit_bitmap_font import bitmap_font
from adafruit_button import Button
from adafruit_display_text import label
from adafruit_minimqtt import adafruit_minimqtt
from adafruit_stmpe610 import Adafruit_STMPE610_SPI

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
            with open(self.path) as fd:
                self.data.update(json.load(fd))
        except OSError as e:
            print(e)

    def save(self):
        with open(self.path, "w") as fd:
            json.dump(fd, self.data)

class Mqtt():
    mqtt_prefix = "goldilocks/sensor/temperature_F/"
    def __init__(self, server, port, username, password, socket_pool):
        self.client = None
        self.server = server
        self.port = port
        self.username = username
        self.password = password
        self.socket_pool = socket_pool
        self.temperatures = {}

    def on_message(self, _client, topic, message):
        print(f"New message on topic {topic}: {message}")
        location = topic[len(self.mqtt_prefix):]
        value = float(message)
        self.temperatures[location] = value

    def connect(self):
        print(f"MQTT connecting to {self.server}:{self.port}")
        self.client = adafruit_minimqtt.MQTT(
            broker=self.server,
            port=self.port,
            username=self.username,
            password=self.password,
            socket_pool=self.socket_pool)
        self.client.on_message = self.on_message
        try:
            self.client.connect()
            self.client.subscribe(self.mqtt_prefix + "#")
        except OSError as e:
            print("Failed to connect to {self.server}:{self.port}: {e}")

    def poll(self):
        if not self.client:
            self.connect()
        try:
            self.client.loop(0)
        except OSError as e:
            print("MQTT loop() raised:")
            traceback.print_exception(e, e, e.__traceback__)
            try:
                self.client.disconnect()
            except OSError as e:
                print("MQTT disconnect() raised:")
                traceback.print_exception(e, e, e.__traceback__)
            self.client = None
            # We'll connect again the next poll()
        return list(self.temperatures.items())

class TouchScreenEvent(object):
    DOWN = 0
    UP = 1
    DRAG = 2
    def __init__(self, typ, x, y):
        self.typ = typ
        self.x = x
        self.y = y

    def __repr__(self):
        return f"TouchScreenEvent({self.typ}, {self.x}, {self.y})"

class TouchScreenEvents(object):
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

class Gui(object):
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
        touch = Adafruit_STMPE610_SPI(spi, touch_cs,
                calibration=((276, 3820), (378, 3743)))
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
        self.time_label.text = "%04d-%02d-%02d %02d:%02d:%02d" % (
            t.tm_year, t.tm_mon, t.tm_mday,
            t.tm_hour, t.tm_min, t.tm_sec
        )

    def update_temperatures(self, temps, overall):
        self.temperature_label.text = "\n".join(repr(i) for i in temps.items())
        self.avg_label.text = "%.1fF" % overall

    def show_main(self):
        self.display.show(self.main_group)

    def poll(self):
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

class Thermostat(object):
    def __init__(self):
        self.settings = Settings()

        ### Hardware devices
        # Get splash screen going first.
        spi = board.SPI()
        self.gui = Gui(self.settings, spi)

        i2c = board.I2C()
        self.rtc = adafruit_pcf8523.PCF8523(i2c)

        # Start network, and use it.
        wifi.radio.connect(secrets.SSID, secrets.PASSWORD)
        self.socket_pool = socketpool.SocketPool(wifi.radio)
        self.mqtt = Mqtt(secrets.MQTT_SERVER, secrets.MQTT_PORT,
                         secrets.MQTT_USERNAME, secrets.MQTT_PASSWORD,
                         self.socket_pool)

        # TODO: How do you deal with timezones?
        ntp = adafruit_ntp.NTP(self.socket_pool, tz_offset=-7)
        # Sync at boot up
        try:
            self.rtc.datetime = ntp.datetime
        except OSError as e:
            # Doesn't always work.
            print("NTP failed: %r" % e)

        ### Local variables.
        self.temperatures = {}
        self.last_stamp = 0

        self.min_point = [10000, 10000, 10000]
        self.max_point = [0, 0, 0]

    def error(self, error):
        print(error)

    def now(self):
        return self.rtc.datetime

    def stamp(self, text=""):
        now = time.monotonic()
        print("%fs %s" % (now - self.last_stamp, text))
        self.last_stamp = now

    def run(self):
        self.gui.show_main()
        while True:
            self.gui.update_time(self.now())
            temperature_updates = self.mqtt.poll()
            for (k, v) in temperature_updates:
                self.temperatures[k] = v
            if temperature_updates:
                overall_temperature = sum(self.temperatures.values()) / len(self.temperatures)
                self.gui.update_temperatures(self.temperatures, overall_temperature)
            self.gui.poll()

def main():
    thermostat = Thermostat()
    thermostat.run()
