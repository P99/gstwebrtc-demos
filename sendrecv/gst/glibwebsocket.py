#!/usr/bin/env python

"""
This is based on Python2.7 websocket-client >= "0.51.0"
It offers the exact same interfaces except that run_forever() is replaced by run()
And we expect the GLib main loop to be running instead
"""

import websocket
import gi

from gi.repository import GLib, GObject

PING_INTERVAL = 10
PING_TIMEOUT = 5

class GlibDispatcher(object):
    def __init__(self, app):
        self.app  = app
        self.tag = None

    def glib_signal_cb(self, iochannel, condition, context):
        if (condition & GLib.IO_IN) == GLib.IO_IN:
            try:
                self.read_callback()
            except Exception as e:
                print(e)
                return False
        return self.app.sock.connected

    def glib_timeout_cb(self):
        try:
            self.check_callback()
        except:
            GLib.source_remove(self.tag)
            self.app.keep_running = False
            self.read_callback() # Will then teardown
            return False
        return self.app.sock.connected

    def read(self, sock, read_callback, check_callback):
        self.read_callback = read_callback
        self.check_callback = check_callback
        self.tag = GLib.io_add_watch(self.app.sock.sock, GLib.IO_IN, self.glib_signal_cb, None)
        #GLib.timeout_add(WS_PING_INTERVAL * 1000, self.glib_timeout_cb)
        GLib.timeout_add(1000, self.glib_timeout_cb)


class GLibWebSocketApp(websocket.WebSocketApp):

    def __init__(self, *args, **kwargs):
        websocket.WebSocketApp.__init__(self, *args, **kwargs)

    def run(self, **kwargs):
        self.glib_dispatcher = GlibDispatcher(self)
        websocket.WebSocketApp.run_forever(self,
            dispatcher=self.glib_dispatcher,
            ping_interval=PING_INTERVAL,
            ping_timeout=PING_TIMEOUT,
            **kwargs)

    def run_forever(self, **kwargs):
        raise RuntimeError("Use run() instead or run_forever() !!!")

