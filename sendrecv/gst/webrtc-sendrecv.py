import random
import ssl
import os
import sys
import json
import argparse

"""
Port of original gstwebrtc-demo with backward compatibility for python 2.7
It relies on websocket-client from https://github.com/websocket-client/websocket-client
And we are running a GLib mainloop, so we could add Gtk UI later
"""

import glibwebsocket
from gi.repository import GLib, GObject

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst
gi.require_version('GstWebRTC', '1.0')
from gi.repository import GstWebRTC
gi.require_version('GstSdp', '1.0')
from gi.repository import GstSdp

PIPELINE_DESC = '''
webrtcbin name=sendrecv bundle-policy=max-bundle
 videotestsrc is-live=true pattern=ball ! videoconvert ! queue ! vp8enc deadline=1 ! rtpvp8pay !
 queue ! application/x-rtp,media=video,encoding-name=VP8,payload=97 ! sendrecv.
 audiotestsrc is-live=true wave=red-noise ! audioconvert ! audioresample ! queue ! opusenc ! rtpopuspay !
 queue ! application/x-rtp,media=audio,encoding-name=OPUS,payload=96 ! sendrecv.
'''

def enum(*sequential, **named):
    enums = dict(zip(sequential, range(len(sequential))), **named)
    return type('Enum', (), enums)

AppState = enum(
  'APP_STATE_UNKNOWN',
  'APP_STATE_ERROR',
  'SERVER_CONNECTING',
  'SERVER_CONNECTION_ERROR',
  'SERVER_CONNECTED', # Ready to register
  'SERVER_REGISTERING',
  'SERVER_REGISTRATION_ERROR',
  'SERVER_REGISTERED', # Ready to call a peer */
  'SERVER_CLOSED', # server connection closed by us or the server */
  'PEER_CONNECTING',
  'PEER_CONNECTION_ERROR',
  'PEER_CONNECTED',
  'PEER_CALL_NEGOTIATING',
  'PEER_CALL_STARTED',
  'PEER_CALL_STOPPING',
  'PEER_CALL_STOPPED',
  'PEER_CALL_ERROR',
)

class WebRTCClient:
    def __init__(self, id_, peer_id, server):
        self.id_ = id_
        self.conn = None
        self.pipe = None
        self.webrtc = None
        self.mainloop = None
        self.peer_id = peer_id
        self.state = AppState.APP_STATE_UNKNOWN
        self.server = server or 'wss://webrtc.nirbheek.in:8443'

    def on_open(self):
        print('Socket is open')
        self.state = AppState.SERVER_CONNECTED

    def on_error(self, error):
        print('Socket got error:', error)

    def on_close(self):
        print('Socket is closed')
        self.state = AppState.SERVER_CLOSED

    def on_message(self, message):
        # print('Socket got message:', message)
        if message == 'HELLO':
            if self.state != AppState.SERVER_REGISTERING:
                print('ERROR: Received HELLO when not registering')
                self.cleanup_and_quit_loop()
                return

            self.state = AppState.SERVER_REGISTERED
            print('Registered with server')

            # Ask signalling server to connect us with a specific peer
            self.setup_call()

        elif message == 'SESSION_OK':
            if self.state != AppState.PEER_CONNECTING:
                print('ERROR: Received SESSION_OK when not calling')
                self.cleanup_and_quit_loop()
                return

            self.state = AppState.PEER_CONNECTED;

            # Start negotiation (exchange SDP and ICE candidates)
            self.start_pipeline()

        elif message.startswith('ERROR'):
            if self.state == AppState.SERVER_CONNECTING:
                self.state = AppState.SERVER_CONNECTION_ERROR;
            if self.state == AppState.SERVER_REGISTERING:
                self.state = AppState.SERVER_REGISTRATION_ERROR;
            if self.state == AppState.PEER_CONNECTING:
                self.state = AppState.PEER_CONNECTION_ERROR;
            if self.state in [AppState.PEER_CONNECTED, AppState.PEER_CALL_NEGOTIATING]:
                self.state = AppState.PEER_CALL_ERROR;
            else:
                self.state = AppState.APP_STATE_ERROR;

            print(message)
            self.cleanup_and_quit_loop()

        else:
            # Look for JSON messages containing SDP and ICE candidates
            self.handle_sdp(message)

    def run(self):
        self.mainloop = GLib.MainLoop()
        self.mainloop.run()

    def cleanup_and_quit_loop(self):
        self.mainloop.quit()

    def connect(self):
        self.conn = glibwebsocket.GLibWebSocketApp(self.server,
                on_open = self.on_open,
                on_message = self.on_message,
                on_error = self.on_error,
                on_close = self.on_close)

        self.state = AppState.SERVER_CONNECTING
        self.conn.run(sslopt={'cert_reqs': ssl.CERT_NONE})

        self.state = AppState.SERVER_REGISTERING
        self.conn.send('HELLO %d' % self.id_)

    def setup_call(self):
        self.state = AppState.PEER_CONNECTING
        self.conn.send('SESSION {}'.format(self.peer_id))

    def send_sdp_offer(self, offer):
        if self.state != AppState.PEER_CALL_NEGOTIATING:
            print("Can't send offer, not in call (state is %d)" % APP_STATE_ERROR)
            self.cleanup_and_quit_loop()
            return

        text = offer.sdp.as_text()
        print ('Sending offer:\n%s' % text)
        msg = json.dumps({'sdp': {'type': 'offer', 'sdp': text}})
        self.conn.send(msg)

    def on_offer_created(self, promise, _, __):
        if self.state != AppState.PEER_CALL_NEGOTIATING:
            print("Offer created, but not in call (state is %d)" % APP_STATE_ERROR)
            self.cleanup_and_quit_loop()
            return

        promise.wait()
        reply = promise.get_reply()
        offer = reply.get_value('offer')
        promise = Gst.Promise.new()
        self.webrtc.emit('set-local-description', offer, promise)
        promise.interrupt()
        self.send_sdp_offer(offer)

    def on_negotiation_needed(self, element):
        self.state = AppState.PEER_CALL_NEGOTIATING
        promise = Gst.Promise.new_with_change_func(self.on_offer_created, element, None)
        element.emit('create-offer', None, promise)

    def send_ice_candidate_message(self, _, mlineindex, candidate):
        if self.state != AppState.PEER_CALL_NEGOTIATING:
            print("Can't send ICE, not in call (state is %d)" % APP_STATE_ERROR)
            self.cleanup_and_quit_loop()
            return

        icemsg = json.dumps({'ice': {'candidate': candidate, 'sdpMLineIndex': mlineindex}})
        self.conn.send(icemsg)

    def on_incoming_decodebin_stream(self, _, pad):
        if not pad.has_current_caps():
            print (pad, 'has no caps, ignoring')
            return

        caps = pad.get_current_caps()
        assert (caps)
        s = caps.get_structure(0)
        name = s.get_name()
        if name.startswith('video'):
            q = Gst.ElementFactory.make('queue')
            conv = Gst.ElementFactory.make('videoconvert')
            sink = Gst.ElementFactory.make('autovideosink')
            self.pipe.add(q)
            self.pipe.add(conv)
            self.pipe.add(sink)
            self.pipe.sync_children_states()
            pad.link(q.get_static_pad('sink'))
            q.link(conv)
            conv.link(sink)
        elif name.startswith('audio'):
            q = Gst.ElementFactory.make('queue')
            conv = Gst.ElementFactory.make('audioconvert')
            resample = Gst.ElementFactory.make('audioresample')
            sink = Gst.ElementFactory.make('autoaudiosink')
            self.pipe.add(q)
            self.pipe.add(conv)
            self.pipe.add(resample)
            self.pipe.add(sink)
            self.pipe.sync_children_states()
            pad.link(q.get_static_pad('sink'))
            q.link(conv)
            conv.link(resample)
            resample.link(sink)

    def on_incoming_stream(self, _, pad):
        if pad.direction != Gst.PadDirection.SRC:
            return

        decodebin = Gst.ElementFactory.make('decodebin')
        decodebin.connect('pad-added', self.on_incoming_decodebin_stream)
        self.pipe.add(decodebin)
        decodebin.sync_state_with_parent()
        self.webrtc.link(decodebin)

    def on_live_message(self, bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            print('---')
            print(msg.parse_error())
            print('---')

    def start_pipeline(self):
        print('Starting pipeline')
        self.pipe = Gst.parse_launch(PIPELINE_DESC)
        self.bus = self.pipe.get_bus()
        self.bus.add_signal_watch()
        self.bus.connect("message", self.on_live_message)
        self.webrtc = self.pipe.get_by_name('sendrecv')
        self.webrtc.connect('on-negotiation-needed', self.on_negotiation_needed)
        self.webrtc.connect('on-ice-candidate', self.send_ice_candidate_message)
        self.webrtc.connect('pad-added', self.on_incoming_stream)
        self.pipe.set_state(Gst.State.PLAYING)

    def handle_sdp(self, message):
        assert (self.webrtc)
        msg = json.loads(message)
        if 'sdp' in msg:
            sdp = msg['sdp']
            assert(sdp['type'] == 'answer')
            sdp = sdp['sdp']
            print ('Received answer:\n%s' % sdp)
            res, sdpmsg = GstSdp.SDPMessage.new()
            GstSdp.sdp_message_parse_buffer(bytes(sdp.encode()), sdpmsg)
            answer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.ANSWER, sdpmsg)
            promise = Gst.Promise.new()
            self.webrtc.emit('set-remote-description', answer, promise)
            promise.interrupt()
        elif 'ice' in msg:
            ice = msg['ice']
            candidate = ice['candidate']
            sdpmlineindex = ice['sdpMLineIndex']
            self.webrtc.emit('add-ice-candidate', sdpmlineindex, candidate)


def check_plugins():
    needed = ["opus", "vpx", "nice", "webrtc", "dtls", "srtp", "rtp",
              "rtpmanager", "videotestsrc", "audiotestsrc"]
    missing = list(filter(lambda p: Gst.Registry.get().find_plugin(p) is None, needed))
    if len(missing):
        print('Missing gstreamer plugins:', missing)
        return False
    return True


if __name__=='__main__':
    Gst.init(None)
    if not check_plugins():
        sys.exit(1)
    parser = argparse.ArgumentParser()
    parser.add_argument('peerid', help='String ID of the peer to connect to')
    parser.add_argument('--server', help='Signalling server to connect to, eg "wss://127.0.0.1:8443"')
    args = parser.parse_args()
    our_id = random.randrange(10, 10000)
    c = WebRTCClient(our_id, args.peerid, args.server)
    c.connect()
    c.run()
    sys.exit(0)
