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
from adafruit_datetime import datetime
import adafruit_ntp

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
    def __init__(self):
        displayio.release_displays()
        spi = board.SPI()
        tft_cs = board.D9
        tft_dc = board.D10

        display_bus = displayio.FourWire(
            spi, command=tft_dc, chip_select=tft_cs, reset=board.D6
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

    def show_table(self, ntp, table):
        text_group = displayio.Group(x=10, y=10)
        # This does a fucking network call...
        #dt = ntp.datetime
        lines = ["%04d-%02d-%02d %02d:%02d:%02d" %
            (dt.tm_year, dt.tm_mon, dt.tm_mday, dt.tm_hour, dt.tm_min, dt.tm_sec)
        ]
        lines += table.items()
        text = "\n".join(str(i) for i in lines)
        text_area = label.Label(terminalio.FONT, text=text, color=0xFFFF00)
        text_group.append(text_area)  # Subgroup for text scaling
        self.display.show(text_group)

class Thermostat(object):
    def __init__(self):
        self.display = Display()
        wifi.radio.connect(secrets.ssid, secrets.password)
        self.socket_pool = socketpool.SocketPool(wifi.radio)
        self.mqtt = Mqtt(secrets.mqtt_server, secrets.mqtt_port,
                         secrets.mqtt_username, secrets.mqtt_password,
                         self.socket_pool)
        # TODO: How do you deal with timezones?
        self.ntp = adafruit_ntp.NTP(self.socket_pool, tz_offset=-8)
        self.temperatures = {}

    def run(self):
        while True:
            temperature_updates = self.mqtt.poll()
            for (k, v) in temperature_updates:
                self.temperatures[k] = v
            if self.temperatures:
                self.display.show_table(self.ntp, self.temperatures)

def main():
    thermostat = Thermostat()
    thermostat.run()
