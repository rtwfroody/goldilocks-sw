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
            self.client.loop(0)
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

class TouchScreenEvent(object):
    DOWN = 0
    UP = 1
    DRAG = 2
    def __init__(self, typ, x, y):
        self.typ = typ
        self.x = x
        self.y = y

    def __repr__(self):
        return "TouchScreenEvent(%d, %d, %d)" % (self.typ, self.x, self.y)

class TouchScreenEvents(object):
    def __init__(self, touch):
        self.touch = touch
        self.last = None

    def poll(self):
        point = self.touch.touch_point
        last = self.last
        self.last = point
        if point:
            x, y, pressure = point
            if last:
                return TouchScreenEvent(TouchScreenEvent.DRAG, x, y)
            else:
                return TouchScreenEvent(TouchScreenEvent.DOWN, x, y)
        else:
            if last:
                return TouchScreenEvent(TouchScreenEvent.UP, last[0], last[1])

class Gui(object):
    # TODO: play with backlight brightness, https://learn.adafruit.com/making-a-pyportal-user-interface-displayio/display
    def __init__(self, spi):
        self.width = 320
        self.height = 240

        # Set up display
        displayio.release_displays()
        display_bus = displayio.FourWire(spi, command=board.D10, chip_select=board.D9)
        self.display = adafruit_ili9341.ILI9341(display_bus, width=self.width, height=self.height)

        splash = self.make_splash()
        self.display.show(splash)

        # Set up touchscreen
        touch_cs = digitalio.DigitalInOut(board.D6)
        touch = Adafruit_STMPE610_SPI(spi, touch_cs,
                calibration=((276, 3820), (378, 3743)))
        self.tse = TouchScreenEvents(touch)

        self.make_main()

        self.selected = None

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
        self.main_group = displayio.Group()
        spacing = 10
        button_height = 40
        group_x = int(spacing/2)
        group_y = int(self.height - button_height - spacing/2)
        presets = ["Sleep", "Away", "Home"]
        self.main_buttons = []
        for i, preset in enumerate(presets):
            button = Button(
                x=group_x + int(i * self.width / 3 + spacing / 2),
                y=group_y,
                width=int(self.width / 3 - spacing),
                height = button_height,
                label=preset,
                label_font=terminalio.FONT,
                style=Button.ROUNDRECT)
            self.main_buttons.append(button)
            self.main_group.append(button)

        info_group = displayio.Group(x=int(spacing/2), y=80)
        self.time_label = label.Label(terminalio.FONT, text="time", color=0xFFFFFF, x=10, y=10)
        self.temperature_label = label.Label(terminalio.FONT, text="temperature", color=0xFF80FF, x=10, y=30)
        info_group.append(self.time_label)
        info_group.append(self.temperature_label)
        self.main_group.append(info_group)

        #self.bitmap = displayio.Bitmap(self.display.width, self.display.height, 2)
        #palette = displayio.Palette(2)
        #palette[0] = 0x000000
        #palette[1] = 0x8fffff
        #tile_grid = displayio.TileGrid(self.bitmap, pixel_shader=palette)
        #self.main_group.append(tile_grid)

    def update_time(self, t):
        self.time_label.text = "%04d-%02d-%02d %02d:%02d:%02d" % (
            t.tm_year, t.tm_mon, t.tm_mday,
            t.tm_hour, t.tm_min, t.tm_sec
        )

    def update_temperatures(self, temps):
        self.temperature_label.text = "\n".join(repr(i) for i in temps.items())

    #def draw_pixel(self, x, y):
    #    self.bitmap[int(x), int(y)] = 1

    def show_main(self):
        self.display.show(self.main_group)

    def poll(self):
        event = self.tse.poll()
        if event:
            print(event)
            x = int(self.width * event.x / 4096)
            y = int(self.height * event.y / 4096)
            if self.selected:
                if self.selected.contains((x, y)):
                    # No change needed
                    return
                else:
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
        ### Hardware devices
        # Get splash screen going first.
        spi = board.SPI()
        self.gui = Gui(spi)
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
        
        ### Local variables.
        self.temperatures = {}
        self.last_stamp = 0

        self.min_point = [10000, 10000, 10000]
        self.max_point = [0, 0, 0]

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
                self.gui.update_temperatures(self.temperatures)
            self.gui.poll()

def main():
    thermostat = Thermostat()
    thermostat.run()
