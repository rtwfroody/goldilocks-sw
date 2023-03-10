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
import adafruit_touchscreen
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
import microcontroller # pylint: disable-msg=import-error
from watchdog import WatchDogMode # pylint: disable-msg=import-error

def celsius_to_fahrenheit(celsius):
    return celsius * 9 / 5 + 32

def fahrenheit_to_celsius(fahrenheit):
    return (fahrenheit - 32) * 5 / 9

class Logger():
    """Send log messages to somewhere that I can read them, maybe."""
    def __init__(self):
        self.index = 0
        self.targets = {
            "print": print
        }

    def add_udp_destination(self, pool, host, port):
        s = pool.socket(pool.AF_INET, pool.SOCK_DGRAM)

        def write(*args):
            msg = " ".join(str(a) for a in args) + "\n"
            try:
                s.sendto(msg, (host, port))
            except OSError as e:
                print(f"Failed UDP send to {host}:{port}: {e}")

        self.targets[f"{host}:{port}"] = write

    def message(self, level, *args):
        prefix = f"{self.index} {time.monotonic():0.2f} {level}:"
        self.index += 1
        for target in self.targets.values():
            target(prefix, *args)

    def debug(self, *args):
        self.message("DEBUG", *args)

    def error(self, *args):
        self.message("ERROR", *args)

class Settings():
    """Track settings that can be stored/restored on disk."""
    path = "/goldilocks.json"
    _data = {
        "temp_high": 80,
        "temp_low": 60,
        "name": "".join(f"{b:02x}" for b in microcontroller.cpu.uid)
    }
    _dirty = False

    def __init__(self, log):
        self.log = log
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
            self.log.debug(f"Loaded {self._data}")
        except (OSError, ValueError) as e:
            self.log.error(f"Loading {self.path}: {e}")

    def save(self):
        if not self._dirty:
            return
        try:
            data = json.dumps(self._data)
            with open(self.path, "w", encoding="utf-8") as fd:
                fd.write(data)
            self._dirty = False
        except OSError as e:
            self.log.error(f"Saving {self.path}: {e}")

class Mqtt():
    """"Get temperature updates from an MQTT server."""
    temperature_prefix = "goldilocks/sensor/temperature_F/"
    def __init__(self, log, server, port, username, password, network, client_name : str):
        self.log = log
        self.client = None
        self.server = server
        self.port = port
        self.username = username
        self.password = password
        self.network = network
        self.client_name = client_name
        self.status_prefix = f"goldilocks/{self.client_name}/status/"
        self.temperatures = {}
        self.last_advertisement = 0

    def on_message(self, _client, topic, message):
        self.log.debug(f"New message on topic {topic}: {message}")
        location = topic[len(self.temperature_prefix):]
        value = float(message)
        self.temperatures[location] = value

    def connect(self):
        if not self.network.connected() or self.client:
            return
        socket_pool = self.network.socket_pool()
        if not socket_pool:
            return
        self.log.debug(f"MQTT connecting to {self.server}:{self.port}")
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
            self.client.subscribe(self.temperature_prefix + "#")
        except (RuntimeError, OSError, adafruit_minimqtt.MMQTTException) as e:
            self.log.error(f"Failed to connect to {self.server}:{self.port}: {e}")
            self.client = None

    def advertise(self):
        if not self.client:
            return
        try:
            self.client.publish(f"homeassistant/sensor/{self.client_name}/uptime/config",
                                json.dumps({
                                    "name": "uptime",
                                    "device_class": "duration",
                                    "state_topic": self.status_prefix + "uptime",
                                    "unit_of_measurement": "s",
                                    "expire_after": 15
                                }))
        except (adafruit_minimqtt.MMQTTException, OSError, AttributeError) as e:
            self.recover(e)
        self.last_advertisement = time.monotonic()

    def poll(self):
        if self.client is None:
            return []
        try:
            self.client.loop(0)
            if time.monotonic() - self.last_advertisement > 600:
                self.advertise()
            self.client.publish(self.status_prefix + "uptime", time.monotonic())
        except (adafruit_minimqtt.MMQTTException, OSError, AttributeError) as e:
            self.recover(e)
        retval = list(self.temperatures.items())
        self.temperatures = {}
        return retval

    def recover(self, e):
        self.log.error("MQTT loop() raised:", repr(e))
        traceback.print_exception(e, e, e.__traceback__)
        try:
            self.client.disconnect()
        except (adafruit_minimqtt.MMQTTException, OSError, AttributeError) as exc:
            self.log.error("MQTT disconnect() raised:")
            traceback.print_exception(exc, exc, exc.__traceback__)
        self.client = None
        # We'll connect again the next poll()

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
    x_range = [65536, 0]
    y_range = [65536, 0]

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
            self.x_range[0] = min(x, self.x_range[0])
            self.x_range[1] = max(x, self.x_range[1])
            self.y_range[0] = min(y, self.y_range[0])
            self.y_range[1] = max(y, self.y_range[1])
            print(f"point={point}, calibration=({self.x_range}, {self.y_range})")
            if self.last_point:
                event = TouchScreenEvent(TouchScreenEvent.DRAG, x, y)
            else:
                event = TouchScreenEvent(TouchScreenEvent.DOWN, x, y)

            self.last_point = point
            self.last_time = now
            return event

        return None

class Network():
    """Connect to a network.
    If we could detect when we get disconnected, then we could automatically
    reconnect as well."""
    def __init__(self, log, ssid, password, hostname):
        self.log = log
        self.ssid = ssid
        self.password = password
        self.hostname = hostname
        self._socket_pool = None
        # wifi.radio doesn't have method that indicates whether it's connected?

    @staticmethod
    def connected():
        return wifi.radio.ipv4_address is not None

    def connect(self):
        if self.connected():
            return False
        self.log.debug("Connecting to", self.ssid)
        wifi.radio.hostname = self.hostname
        try:
            wifi.radio.connect(self.ssid, self.password)
        except ConnectionError as e:
            self.log.error(f"connect to {self.ssid}: {e}")
            return False
        self.log.debug(f"Connected to {self.ssid}.",
            f"hostname={wifi.radio.hostname},",
            f"ipv4_address={wifi.radio.ipv4_address}")
        self._socket_pool = socketpool.SocketPool(wifi.radio)
        return True

    def socket_pool(self):
        return self._socket_pool

class Task():
    """Simple task."""
    def __init__(self, fn, name : str):
        self.fn = fn
        self.name = name

    def run(self):
        return self.fn()

    def repr(self):
        return f"Task({self.fn}, {self.name})"

class RepeatTask(Task):
    """Task that needs to be repeated over and over with a given period."""
    def __init__(self, fn, period : float, name : str):
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

    def age(self):
        return time.monotonic() - self.timestamp

    def __repr__(self):
        return f"Datum({self.value}, {self.timestamp})"

    def __str__(self):
        return f"{self.value:.1f} {self.age():.0f}s ago"

class TaskRunner():
    """Run tasks, most urgent first."""
    def __init__(self, log):
        self.log = log
        # Array of (next run time, Task)
        self.task_queue = PriorityQueue()

    def add(self, task : Task, delay=0):
        self.log.debug("add task", task.name)
        self.task_queue.add(task, -time.monotonic() - delay)

    def run(self):
        now = time.monotonic()
        # pylint: disable-msg=invalid-unary-operand-type
        run_time = -self.task_queue.peek_priority()
        if run_time <= now:
            task = self.task_queue.pop()
            run_after = task.run()
            end = time.monotonic()
            if end - now > .08:
                self.log.debug(f"{task.name} took {end - now}s -> {run_after}")
            if run_after:
                next_time = run_time + run_after
                if next_time < now:
                    #print(f"Can't run {task} after {run_after}s because we're already too late.")
                    next_time = now + run_after
                self.task_queue.add(task, -next_time)

class Thermostat():
    """Top-level class for the thermostat application with GUI and temperature
    control."""

    presets = {
        "Sleep": (58, 74),
        "Away": (64, 79),
        "Home": (68, 75)
    }

    def select_preset(self, name):
        low, high = self.presets[name]
        self.set_range(low, high)

    def __init__(self):
        self.log = Logger()
        self.settings = Settings(self.log)

        # Get splash screen going first.
        spi = board.SPI()
        self.gui = Gui(self.log, self, spi)

        self.task_runner = TaskRunner(self.log)

        ### Hardware devices
        try:
            i2c = board.I2C()
        except RuntimeError as e:
            self.log.error("No I2C bus found!")
            self.log.error(e)
            self.rtc = None
        else:
            self.rtc = adafruit_pcf8523.PCF8523(i2c)
            self.task_runner.add(RepeatTask(
                lambda: self.gui.update_time(self.now()),
                1, "time update"))
            self.task_runner.add(RepeatTask(self.sync_time, 12 * 3600, "time sync"), 10)

            try:
                self.bme280 = adafruit_bme280.Adafruit_BME280_I2C(i2c)
            except ValueError as e:
                self.log.error(f"Couldn't init BME280: {e}")
            else:
                self.task_runner.add(RepeatTask(self.poll_local_temp, 5, "local temp poll"))

        self.scheduler = Scheduler(self.log, self)
        #self.scheduler.test()
        self.task_runner.add(RepeatTask(self.scheduler.poll, 15, "scheduler poll"))

        # Start network, and use it.
        self.network = Network(self.log, secrets.SSID, secrets.PASSWORD,
                "goldilocks-" + self.settings.name)
        #>>> self.task_runner.add(RepeatTask(self.network_connect, 10, "network connect"))

        self.mqtt = Mqtt(self.log,
                         secrets.MQTT_SERVER, secrets.MQTT_PORT,
                         secrets.MQTT_USERNAME, secrets.MQTT_PASSWORD,
                         self.network, self.settings.name)
        self.task_runner.add(RepeatTask(self.mqtt.connect, 60, "mqtt connect"), 5)
        self.task_runner.add(RepeatTask(self.poll_mqtt, 1, "mqtt poll"), 6)
        self.task_runner.add(RepeatTask(self.mqtt.advertise, 123, "mqtt advertise"), 6)

        self.uart = busio.UART(board.TX, board.RX, baudrate=2400, bits=8,
                               parity=busio.UART.Parity.EVEN, stop=1, timeout=0)

        self.heatPump = HeatPump.HeatPump(self.uart, log=self.log)
        self.task_runner.add(RepeatTask(self.heatPump.poll, 1, "heatpump poll"), 6)

        ### Local variables.
        self.temperatures = {}
        self.last_stamp = 0
        # If set, then we're heating or cooling until we reach this temperature.
        self.target_temperature = None

        microcontroller.watchdog.timeout = 45
        microcontroller.watchdog.mode = WatchDogMode.RESET

        #self.task_runner.add(Task(self.hard_reset, "hard reset"), 900)

    def hard_reset(self):
        self.log.debug("hard reset")
        time.sleep(1)
        microcontroller.reset()

    def network_connect(self):
        if not self.network.connected():
            if self.network.connect():
                self.log.add_udp_destination(self.network.socket_pool(),
                                             secrets.LOG_SERVER, secrets.LOG_PORT)

    def poll_local_temp(self):
        self.temperatures["head"] = Datum(celsius_to_fahrenheit(self.bme280.temperature))
        self.temperature_updated()

    def poll_mqtt(self):
        temperature_updates = self.mqtt.poll()
        for (k, v) in temperature_updates:
            self.temperatures[k] = Datum(v)
        self.temperature_updated()

    def overall_temperature(self):
        total = 0
        count = 0
        for datum in self.temperatures.values():
            if datum.age() < 120:
                total += datum.value
                count += 1
            else:
                self.log.debug("ignore", datum)

        if count == 0:
            # We shouldn't really get here. Is it better to assert?
            return 70

        return total / count

    def temperature_updated(self):
        overall_temperature = self.overall_temperature()
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
            self.log.error(f"NTP failed: {e}")

    def now(self):
        if self.rtc:
            return self.rtc.datetime

        return time.localtime()

    def run(self):
        self.gui.show_main()
        while True:
            microcontroller.watchdog.feed()

            self.task_runner.run()

            self.gui.poll()

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
        self.task_runner.add(Task(self.settings.save, "settings save"), 15)

class Scheduler():
    """
    Simple scheduler that changes the thermostat preset when we cross into a new
    scheduled block of time.
    """
    daily = (
        (8, 0, "Away"),
        (22, 0, "Sleep")
    )
    def __init__(self, log : Logger, thermostat : Thermostat):
        self.log = log
        self.thermostat = thermostat
        self.index = self.find_index(self.thermostat.now())

    def find_index(self, tm):
        for i, (hour, minute, _preset) in enumerate(self.daily):
            if (hour > tm.tm_hour or
                    (hour == tm.tm_hour and minute > tm.tm_min)):
                return i-1
        return -1

    def poll(self):
        now = self.thermostat.now()
        new_index = self.find_index(now)
        if new_index != self.index:
            # Do we need more abstraction to hide gui?
            self.thermostat.gui.select_preset(self.daily[new_index][2])
            self.log.debug(now, "schedule poll change to", self.daily[new_index])
            self.index = new_index

    def test(self):
        for hour in range(0, 24):
            for minute in range(0, 60, 30):
                tm = time.struct_time([
                    2000, 1, 1, # year month day
                    hour, minute, 0, # hour minute second
                    1, 1, 0 # wday yday isdst
                    ])
                self.log.debug(f"{hour:2d}:{minute:02d}", self.find_index(tm))

class Gui():
    """Display a GUI."""
    # TODO: play with backlight brightness,
    # https://learn.adafruit.com/making-a-pyportal-user-interface-displayio/display
    def get_font(self, size, bold=False):
        if (size, bold) not in self.fonts:
            font_name = "DejaVuSansMono"
            if bold:
                font_name += "-Bold"
            self.fonts[(size, bold)] = bitmap_font.load_font(f"font/{font_name}-{size}.pcf")
        return self.fonts[(size, bold)]

    def __init__(self, log : Logger, thermostat : Thermostat, spi):
        self.log = log
        self.thermostat = thermostat

        self.fonts = {}

        self.width = 320
        self.height = 240

        # Set up display
        displayio.release_displays()
        display_bus = displayio.FourWire(spi, command=board.D10, chip_select=board.D9)
        self.display = adafruit_ili9341.ILI9341(display_bus, width=self.width, height=self.height)
        self.log.debug("display:", self.display)

        self.preset_buttons = {}

        splash = self.make_splash()
        self.display.show(splash)

        # Set up touchscreen
        touch_cs = digitalio.DigitalInOut(board.D6)
        try:
            # Look for a SPI touchscreen, as implemented on the
            # TFT FeatherWing - 2.4" 320x240.
            touch = Adafruit_STMPE610_SPI(spi, touch_cs,
                    calibration=((276, 3820), (378, 3743)),
                    size=(self.width, self.height))
        except RuntimeError as e:
            self.log.debug("No SPI touch screen connected!")
            self.log.debug(e)
            touch = None

        if not touch:
            # Look for 4-wire resistive touch screen, as implemented on Adafruit
            # 2.4" TFT LCD with Touchscreen Breakout.

            touch = adafruit_touchscreen.Touchscreen(
                x1_pin=board.IO17, x2_pin=board.IO14, y1_pin=board.IO18, y2_pin=board.IO12,
                calibration=([59889, 12545], [16373, 61473]),
                size=(self.width, self.height),
                z_threshold=34800,
                invert_pressure=False
            )

        if touch:
            self.log.debug(f"Connected to touch screen {touch}")
            self.tse = TouchScreenEvents(touch)
        else:
            self.tse = None

        self.pages = [
            self.make_main(),
            self.make_temperature_detail()
        ]

        self.selected = None
        self.drag_start = None
        self.current_page = 0
        self.colon_blink_state = True

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
        self.thermostat.select_preset(name)
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
        for name, (low, high) in self.thermostat.presets.items():
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
        preset_names = sorted(self.thermostat.presets.keys(),
                              key=lambda n: self.thermostat.presets[n])
        for i, name in enumerate(preset_names):
            button = Button(
                x=group_x + int(i * self.width / len(self.thermostat.presets) + spacing / 2),
                y=group_y,
                width=int(self.width / len(self.thermostat.presets) - spacing),
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
        if self.colon_blink_state:
            colon = ":"
        else:
            colon = " "
        self.colon_blink_state = not self.colon_blink_state
        self.time_label.text = f"%d/%d %d{colon}%02d" % (
            t.tm_mon, t.tm_mday,
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

            x = event.x
            y = event.y

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
        self.log.debug(f"swipe {x}, {y}")
        if abs(y) > 60 or abs(x) < 100:
            return
        if x > 0:
            self.current_page += 1
        else:
            self.current_page -= 1
        self.current_page = self.current_page % len(self.pages)
        self.display.show(self.pages[self.current_page])

def main():
    thermostat = Thermostat()
    thermostat.run()
