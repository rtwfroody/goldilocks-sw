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
import socketpool   # pylint: disable-msg=import-error
import wifi # pylint: disable-msg=import-error
from adafruit_bitmap_font import bitmap_font # pylint: disable-msg=import-error
from adafruit_button import Button
from adafruit_display_text import label # pylint: disable-msg=import-error
from adafruit_minimqtt import adafruit_minimqtt # pylint: disable-msg=no-name-in-module
from adafruit_stmpe610 import Adafruit_STMPE610_SPI
from adafruit_bme280 import basic as adafruit_bme280 # pylint: disable-msg=import-error
import HeatPump
from priority_queue import PriorityQueue

def celsius_to_fahrenheit(celsius):
    return celsius * 9 / 5 + 32

def fahrenheit_to_celsius(fahrenheit):
    return (fahrenheit - 32) * 5 / 9

class Settings():
    """Track settings that can be stored/restored on disk."""
    path = "/goldilocks.json"
    _data = {
        "temp_high": 80,
        "temp_low": 60
    }
    _dirty = False

    def __init__(self):
        self.load()

    def __getattr__(self, name):
        return self._data[name]

    def set(self, name, value):
        return self.__setattr__(name, value)

    # This works on my machine in python3. It does not work on circuitpython, so
    # use set() for that.
    def __setattr__(self, name, value):
        assert name in self._data
        if value != self._data[name]:
            self._data[name] = value
            self._dirty = True

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as fd:
                self._data.update(json.load(fd))
        except (OSError, ValueError) as e:
            print(f"Loading {self.path}: {e}")

    def save(self):
        if not self._dirty:
            return
        try:
            data = json.dumps(self._data)
            with open(self.path, "w", encoding="utf-8") as fd:
                fd.write(data)
            self._dirty = False
        except OSError as e:
            print(f"Saving {self.path}: {e}")

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
        if not self.network.connected() or self.client:
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
            socket_pool=socket_pool,
            socket_timeout=1,
            connect_retries=1
            )
        self.client.on_message = self.on_message
        try:
            self.client.connect()
            self.client.subscribe(self.mqtt_prefix + "#")
        except (RuntimeError, OSError, adafruit_minimqtt.MMQTTException) as e:
            print(f"Failed to connect to {self.server}:{self.port}: {e}")
            self.client = None

    def poll(self):
        if self.client is None:
            return []
        try:
            self.client.loop(0)
        except (adafruit_minimqtt.MMQTTException, OSError, AttributeError) as e:
            print("MQTT loop() raised:", repr(e))
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
        self.last_point = None
        self.last_time = 0

    def poll(self):
        now = time.monotonic()
        if now - self.last_time > .15:
            if self.last_point:
                point = self.last_point
                self.last_point = None
                return TouchScreenEvent(TouchScreenEvent.UP, point[0], point[1])

        point = self.touch.touch_point
        if point:
            x, y, _pressure = point
            if self.last_point:
                event = TouchScreenEvent(TouchScreenEvent.DRAG, x, y)
            else:
                event = TouchScreenEvent(TouchScreenEvent.DOWN, x, y)

            self.last_point = point
            self.last_time = now
            return event

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

    def get_font(self, size, bold=False):
        if (size, bold) not in self.fonts:
            font_name = "DejaVuSansMono"
            if bold:
                font_name += "-Bold"
            self.fonts[(size, bold)] = bitmap_font.load_font(f"font/{font_name}-{size}.pcf")
        return self.fonts[(size, bold)]

    def __init__(self, thermostat, spi):
        self.thermostat = thermostat

        self.fonts = {}

        self.width = 320
        self.height = 240

        # Set up display
        displayio.release_displays()
        display_bus = displayio.FourWire(spi, command=board.D10, chip_select=board.D9)
        self.display = adafruit_ili9341.ILI9341(display_bus, width=self.width, height=self.height)

        self.preset_buttons = {}

        splash = self.make_splash()
        self.display.show(splash)

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

        self.pages = [
            self.make_main(),
            self.make_temperature_detail()
        ]

        self.selected = None
        self.drag_start = None
        self.current_page = 0

    def make_splash(self):
        # Make the display context
        splash = displayio.Group()

        # Draw a label
        text_area = label.Label(self.get_font(30, True), text="Goldilocks", color=0xFFFF00)
        text_area.x = int((self.width - text_area.width) / 2)
        text_area.y = int((self.height - text_area.height) / 2)
        splash.append(text_area)
        return splash

    def select_preset(self, name):
        low, high = self.presets[name]
        self.thermostat.set_range(low, high)
        self.thermostat_setting_changed()

    def increase_low_temperature(self, amount):
        self.thermostat.set_range(
            self.thermostat.get_temp_low() + amount,
            max(self.thermostat.get_temp_high(), self.thermostat.get_temp_low() + 4))
        self.thermostat_setting_changed()

    def increase_high_temperature(self, amount):
        self.thermostat.set_range(
            min(self.thermostat.get_temp_low(), self.thermostat.get_temp_high() - 4),
            self.thermostat.get_temp_high() + amount)
        self.thermostat_setting_changed()

    def thermostat_setting_changed(self):
        self.update_low_temperature()
        self.update_high_temperature()

        # See if this matches a preset.
        preset_found = None
        for name, (low, high) in self.presets.items():
            if (abs(low - self.thermostat.get_temp_low()) < .1 and
                    abs(high - self.thermostat.get_temp_high()) < .1):
                preset_found = name
                break

        for button_name, button in self.preset_buttons.items():
            if preset_found == button_name:
                button.fill_color = 0x8fff8f
            else:
                button.fill_color = 0xffffff

    def update_low_temperature(self):
        self.low_label.text = f"{self.thermostat.get_temp_low():.0f}F"

    def update_high_temperature(self):
        self.high_label.text = f"{self.thermostat.get_temp_high():.0f}F"

    def make_main(self):
        page = displayio.Group()
        spacing = 10
        button_height = 40
        group_x = int(spacing/2)
        group_y = int(self.height - button_height - spacing/2)
        self.main_buttons = []
        # Sort preset names by temperature range.
        preset_names = sorted(self.presets.keys(), key=lambda n: self.presets[n])
        for i, name in enumerate(preset_names):
            button = Button(
                x=group_x + int(i * self.width / len(self.presets) + spacing / 2),
                y=group_y,
                width=int(self.width / len(self.presets) - spacing),
                height = button_height,
                label=name,
                label_font=self.get_font(18, True),
                style=Button.ROUNDRECT)
            button.pressed = lambda name=name: self.select_preset(name)
            self.preset_buttons[name] = button
            self.main_buttons.append(button)
            page.append(button)

        info_group = displayio.Group(x=int(spacing/2), y=int(spacing/2))
        self.time_label = label.Label(self.get_font(24), text="time", color=0xFFFFFF,
                                      anchor_point=(0.5, 0),
                                      anchored_position=(self.width/2, 0))
        info_group.append(self.time_label)

        temperature_y = int(self.height/2 - 15)

        self.low_label = label.Label(self.get_font(24), x=10, y=50, color=0x9f9fff,
                                     anchor_point=(0, 0.5),
                                     anchored_position=(0, temperature_y))
        info_group.append(self.low_label)
        self.high_label = label.Label(self.get_font(24), x=260, y=50, color=0xff9f9f,
                                      anchor_point=(1, 0.5),
                                      anchored_position=(self.width, temperature_y))
        info_group.append(self.high_label)

        self.thermostat_setting_changed()

        self.low_up = Button(x=self.low_label.x,
                             y=self.low_label.y - int(self.low_label.height / 2) - button_height,
                             width=self.low_label.width, height=button_height,
                             label="^^", label_font=self.get_font(18, True),
                             style=Button.ROUNDRECT)
        self.main_buttons.append(self.low_up)
        self.low_up.pressed = lambda: self.increase_low_temperature(1)
        info_group.append(self.low_up)

        self.low_down = Button(x=self.low_label.x,
                             y=self.low_label.y + int(self.low_label.height / 2) + 4,
                             width=self.low_label.width, height=button_height,
                             label="vv", label_font=self.get_font(18, True),
                             style=Button.ROUNDRECT)
        self.low_down.pressed = lambda: self.increase_low_temperature(-1)
        self.main_buttons.append(self.low_down)
        info_group.append(self.low_down)

        self.high_up = Button(x=self.high_label.x,
                             y=self.high_label.y - int(self.high_label.height / 2) - button_height,
                             width=self.high_label.width, height=button_height,
                             label="^^", label_font=self.get_font(18, True),
                             style=Button.ROUNDRECT)
        self.high_up.pressed = lambda: self.increase_high_temperature(1)
        self.main_buttons.append(self.high_up)
        info_group.append(self.high_up)

        self.high_down = Button(x=self.high_label.x,
                             y=self.high_label.y + int(self.high_label.height / 2) + 4,
                             width=self.high_label.width, height=button_height,
                             label="vv", label_font=self.get_font(18, True),
                             style=Button.ROUNDRECT)
        self.high_down.pressed = lambda: self.increase_high_temperature(-1)
        self.main_buttons.append(self.high_down)
        info_group.append(self.high_down)

        self.avg_label = label.Label(self.get_font(30, True),
                                     anchor_point=(0.5, 0.5), color=0xffffff,
                                     anchored_position=(self.width/2, temperature_y))
        info_group.append(self.avg_label)

        page.append(info_group)

        return page

    def make_temperature_detail(self):
        page = displayio.Group()
        self.temperature_label = label.Label(self.get_font(12), color=0xFF80FF, x=10, y=100)
        page.append(self.temperature_label)
        return page

    def update_time(self, t):
        # pylint: disable-msg=consider-using-f-string
        self.time_label.text = "%d/%d/%02d %d:%02d" % (
            t.tm_mon, t.tm_mday, t.tm_year % 100,
            t.tm_hour, t.tm_min
        )

    def update_temperatures(self, temps, overall):
        self.temperature_label.text = "\n".join(f"{k}: {v}" for k, v in temps.items())
        self.avg_label.text = f"{overall:.1f}F"

    def show_main(self):
        self.display.show(self.pages[0])

    def poll(self):
        if not self.tse:
            return
        while True:
            event = self.tse.poll()
            if not self.selected and not event:
                break

            if not event:
                continue

            x = int(self.width * event.x / 4096)
            y = int(self.height * event.y / 4096)

            self.selected = None
            for button in self.main_buttons:
                button.selected = button.contains((x, y))
                if button.selected:
                    self.selected = button

            if event.typ == TouchScreenEvent.DOWN:
                self.drag_start = x, y

            elif event.typ == TouchScreenEvent.UP:
                if self.selected:
                    self.selected.selected = False
                    self.selected.pressed()
                    self.selected = None
                else:
                    drag_x = x - self.drag_start[0]
                    drag_y = y - self.drag_start[1]
                    self.swipe(drag_x, drag_y)
                break

    def swipe(self, x, y):
        print(f"swipe {x}, {y}")
        if abs(y) > 80 or abs(x) < 150:
            return
        if x > 0:
            self.current_page += 1
        else:
            self.current_page -= 1
        self.current_page = self.current_page % len(self.pages)
        self.display.show(self.pages[self.current_page])

class Network():
    """Connect to a network.
    If we could detect when we get disconnected, then we could automatically
    reconnect as well."""
    def __init__(self, ssid, password):
        self.ssid = ssid
        self.password = password
        self._socket_pool = None
        # wifi.radio doesn't have method that indicates whether it's connected?

    @staticmethod
    def connected():
        return wifi.radio.ipv4_address is not None

    def connect(self):
        if self.connected():
            return
        print("Connecting to", self.ssid)
        try:
            wifi.radio.connect(self.ssid, self.password)
        except ConnectionError as e:
            print(f"connect to {self.ssid}: {e}")
            return
        print(f"Connected to {self.ssid}.",
            f"hostname={wifi.radio.hostname},",
            f"ipv4_address={wifi.radio.ipv4_address}")
        self._socket_pool = socketpool.SocketPool(wifi.radio)

    def socket_pool(self):
        return self._socket_pool

class Task():
    """Simple task."""
    def __init__(self, fn=None, name : str = None):
        self.fn = fn
        self.name = name

    def run(self):
        return self.fn()

    def repr(self):
        return f"Task({self.fn}, {self.name})"

class RepeatTask(Task):
    """Task that needs to be repeated over and over with a given period."""
    def __init__(self, fn, period, name=None):
        super().__init__(fn, name)
        self.fn = fn
        self.period = period
        self.name = name

    def run(self):
        self.fn()
        return self.period

    def repr(self):
        return f"Task({self.fn}, {self.period}, {self.name})"

class Datum():
    """Store a single sensor reading."""
    def __init__(self, value, timestamp=None):
        self.value = value
        self.timestamp = timestamp or time.monotonic()

    def __repr__(self):
        return f"Datum({self.value}, {self.timestamp})"

    def __str__(self):
        ago = time.monotonic() - self.timestamp
        return f"{self.value:.1f} {ago:.0f}s ago"

class TaskRunner():
    """Run tasks, most urgent first."""
    def __init__(self):
        # Array of (next run time, Task)
        self.task_queue = PriorityQueue()

    def add(self, task : Task, delay=0):
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
                    #print(f"Can't run {task} after {run_after}s because we're already too late.")
                    next_time = now + run_after
                self.task_queue.add(task, -next_time)

class Thermostat():
    """Top-level class for the thermostat application with GUI and temperature
    control."""
    def __init__(self):
        self.task_runner = TaskRunner()

        self.settings = Settings()

        ### Hardware devices
        # Get splash screen going first.
        spi = board.SPI()
        self.gui = Gui(self, spi)

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

            try:
                self.bme280 = adafruit_bme280.Adafruit_BME280_I2C(i2c)
            except ValueError as e:
                print(f"Couldn't init BME280: {e}")
            else:
                self.task_runner.add(RepeatTask(self.poll_local_temp, 1))

        # Start network, and use it.
        self.network = Network(secrets.SSID, secrets.PASSWORD)
        self.task_runner.add(RepeatTask(self.network.connect, 10))

        self.mqtt = Mqtt(secrets.MQTT_SERVER, secrets.MQTT_PORT,
                         secrets.MQTT_USERNAME, secrets.MQTT_PASSWORD,
                         self.network)
        self.task_runner.add(RepeatTask(self.mqtt.connect, 60), 5)
        self.task_runner.add(RepeatTask(self.poll_mqtt, 1), 6)

        self.uart = busio.UART(board.TX, board.RX, baudrate=2400, bits=8,
                               parity=busio.UART.Parity.EVEN, stop=1, timeout=0)

        self.heatPump = HeatPump.HeatPump(self.uart)

        ### Local variables.
        self.temperatures = {}
        self.last_stamp = 0
        # If set, then we're heating or cooling until we reach this temperature.
        self.target_temperature = None

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

        if self.target_temperature is None:
            if overall_temperature <= self.settings.temp_low:
                # It's cold
                self.heatPump.set_mode(HeatPump.Mode.HEAT)
                self.heatPump.set_power(True)
                self.target_temperature = self.settings.temp_low + 1
                self.heatPump.set_temperature_c(fahrenheit_to_celsius(self.target_temperature))
            elif overall_temperature >= self.settings.temp_high:
                # It's hot
                self.heatPump.set_mode(HeatPump.Mode.COOL)
                self.heatPump.set_power(True)
                self.target_temperature = self.settings.temp_high - 1
                self.heatPump.set_temperature_c(fahrenheit_to_celsius(self.target_temperature))
            else:
                self.heatPump.set_power(False)

        else:
            if self.settings.temp_low + 1 < overall_temperature < self.settings.temp_high - 1:
                # We've achieved our goal!
                self.heatPump.set_power(False)
                self.target_temperature = None

        self.heatPump.set_remote_temperature_c(fahrenheit_to_celsius(overall_temperature))

    def sync_time(self):
        if not self.network.connected():
            return
        socket_pool = self.network.socket_pool()
        if not socket_pool:
            return
        # TODO: How do you deal with timezones?
        ntp = adafruit_ntp.NTP(socket_pool, tz_offset=-8)
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

    def get_temp_low(self):
        return self.settings.temp_low

    def get_temp_high(self):
        return self.settings.temp_high

    def set_range(self, low, high):
        self.settings.set("temp_low", low)
        self.settings.set("temp_high", high)
        self.target_temperature = None
        # Save in a little while, so we don't save every time the user hits a button.
        self.task_runner.add(Task(lambda: self.settings.save(), name="save settings"), 15)

def main():
    thermostat = Thermostat()
    thermostat.run()
