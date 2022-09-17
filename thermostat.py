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
            traceback.print_exception(e)
            try:
                self.client.disconnect()
            except OSError as e:
                print("MQTT disconnect() raised:")
                traceback.print_exception(e)
            self.client = None
            # We'll connect again the next poll()
        return list(self.temperatures.items())

class Display(object):
    def __init__(self, spi):
        displayio.release_displays()
        display_bus = displayio.FourWire(
            spi,
            command=board.D10, chip_select=board.D9
        )
        self.display = adafruit_ili9341.ILI9341(display_bus, width=320, height=240)

        # Make the display context
        splash = displayio.Group()

        # Draw a label
        text_group = displayio.Group(scale=3, x=57, y=120)
        text = "Goldilocks"
        text_area = label.Label(terminalio.FONT, text=text, color=0xFFFF00)
        text_group.append(text_area)  # Subgroup for text scaling
        splash.append(text_group)

        self.display.show(splash)

    def show_table(self, now, table):
        lines = ["%04d-%02d-%02d %02d:%02d:%02d" % (
            now.tm_year, now.tm_mon, now.tm_mday,
            now.tm_hour, now.tm_min, now.tm_sec
        )]
        lines += table.items()
        text = "\n".join(str(i) for i in lines)
        text_area = label.Label(terminalio.FONT, text=text, color=0xFFFF00, x=10, y=10)
        self.display.show(text_area)

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
        self.rtc.datetime = ntp.datetime

        touch_cs = digitalio.DigitalInOut(board.D6)
        self.touch = Adafruit_STMPE610_SPI(spi, touch_cs)
        
        ### Local variables.
        self.temperatures = {}

    def now(self):
        return self.rtc.datetime

    def run(self):
        while True:
            temperature_updates = self.mqtt.poll()
            for (k, v) in temperature_updates:
                self.temperatures[k] = v
            self.display.show_table(self.now(), self.temperatures)

            while not self.touch.buffer_empty:
                print(self.touch.read_data())

def main():
    thermostat = Thermostat()
    thermostat.run()
