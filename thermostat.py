from adafruit_display_text import label
from adafruit_minimqtt import adafruit_minimqtt
import adafruit_ili9341
import board
import displayio
import secrets
import socketpool
import terminalio
import wifi
import traceback
import adafruit_ntp
import time
import adafruit_pcf8523
from adafruit_stmpe610 import Adafruit_STMPE610_SPI
import digitalio
from adafruit_button import Button

class Mqtt(object):
    mqtt_prefix = "goldilocks/sensor/temperature_F/"
    def __init__(self, server, port, username, password, socket_pool):
        self.client = None
        self.server = server
        self.port = port
        self.username = username
        self.password = password
        self.socket_pool = socket_pool
        self.temperatures = {}

    def on_message(self, client, topic, message):
        print("New message on topic {0}: {1}".format(topic, message))
        location = topic[len(self.mqtt_prefix):]
        value = float(message)
        self.temperatures[location] = value

    def connect(self):
        print("MQTT connecting to %s:%d" % (self.server, self.port))
        self.client = adafruit_minimqtt.MQTT(
            broker=self.server,
            port=self.port,
            username=self.username,
            password=self.password,
            socket_pool=self.socket_pool)
        self.client.on_message = self.on_message
        self.client.connect()
        self.client.subscribe(self.mqtt_prefix + "#")
    
    def poll(self):
        if not self.client:
            self.connect()
        try:
            self.client.loop()
        except OSError as e:
            print("MQTT loop() raised:")
            traceback.print_exception(e, e, e.__traceback__)
            try:
                self.client.disconnect()
            except OSError as e:
                print("MQTT disconnect() raised:")
                traceback.print_exception(e)
            self.client = None
            # We'll connect again the next poll()
        return list(self.temperatures.items())

class Display(object):
    # TODO: play with backlight brightness, https://learn.adafruit.com/making-a-pyportal-user-interface-displayio/display
    def make_splash(self):
        # Make the display context
        splash = displayio.Group()

        # Draw a label
        text_group = displayio.Group(scale=3, x=57, y=120)
        text = "Goldilocks"
        text_area = label.Label(terminalio.FONT, text=text, color=0xFFFF00)
        text_group.append(text_area)  # Subgroup for text scaling
        splash.append(text_group)
        return splash

    def make_main(self):
        main = displayio.Group()
        spacing = 10
        button_height = 40
        preset_group = displayio.Group(
            x=int(spacing/2),
            y=int(self.height - button_height - spacing/2))
        presets = ["Sleep", "Away", "Home"]
        buttons = []
        for i, preset in enumerate(presets):
            button = Button(
                x=int(i * self.width / 3 + spacing / 2),
                y=0,
                width=int(self.width / 3 - spacing),
                height = button_height,
                label=preset,
                label_font=terminalio.FONT)
            buttons.append(button)
            preset_group.append(button)
        main.append(preset_group)

        info_group = displayio.Group(x=int(spacing/2), y=80)
        self.time_label = label.Label(terminalio.FONT, text="time", color=0xFFFFFF, x=10, y=10)
        self.temperature_label = label.Label(terminalio.FONT, text="temperature", color=0xFF80FF, x=10, y=30)
        info_group.append(self.time_label)
        info_group.append(self.temperature_label)
        main.append(info_group)

        return main

    def __init__(self, spi):
        self.width = 320
        self.height = 240

        displayio.release_displays()
        display_bus = displayio.FourWire(
            spi,
            command=board.D10, chip_select=board.D9
        )
        self.display = adafruit_ili9341.ILI9341(display_bus, width=self.width, height=self.height)

        splash = self.make_splash()
        self.display.show(splash)

        self.main_group = self.make_main()

    def update_time(self, t):
        self.time_label.text = "%04d-%02d-%02d %02d:%02d:%02d" % (
            t.tm_year, t.tm_mon, t.tm_mday,
            t.tm_hour, t.tm_min, t.tm_sec
        )

    def update_temperatures(self, temps):
        self.temperature_label.text = "\n".join(repr(i) for i in temps.items())

    def main(self):
        self.display.show(self.main_group)

class Thermostat(object):
    def __init__(self):
        ### Hardware devices
        # Get splash screen going first.
        spi = board.SPI()
        self.display = Display(spi)
        i2c = board.I2C()
        self.rtc = adafruit_pcf8523.PCF8523(i2c)

        # Start network, and use it.
        wifi.radio.connect(secrets.ssid, secrets.password)
        self.socket_pool = socketpool.SocketPool(wifi.radio)
        self.mqtt = Mqtt(secrets.mqtt_server, secrets.mqtt_port,
                         secrets.mqtt_username, secrets.mqtt_password,
                         self.socket_pool)

        # TODO: How do you deal with timezones?
        ntp = adafruit_ntp.NTP(self.socket_pool, tz_offset=-7)
        # Sync at boot up
        try:
            self.rtc.datetime = ntp.datetime
        except OSError as e:
            # Doesn't always work.
            print("NTP failed: %r" % e)

        touch_cs = digitalio.DigitalInOut(board.D6)
        self.touch = Adafruit_STMPE610_SPI(spi, touch_cs)
        
        ### Local variables.
        self.temperatures = {}

    def now(self):
        return self.rtc.datetime

    def run(self):
        self.display.main()
        while True:
            self.display.update_time(self.now())
            temperature_updates = self.mqtt.poll()
            for (k, v) in temperature_updates:
                self.temperatures[k] = v
            if temperature_updates:
                self.display.update_temperatures(self.temperatures)

            while not self.touch.buffer_empty:
                print(self.touch.read_data())

def main():
    thermostat = Thermostat()
    thermostat.run()
