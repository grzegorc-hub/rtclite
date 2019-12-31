# Copyright (c) 2016, Kundan Singh. All rights reserved.

'''
A server for WebRTC signaling negotiations over WebSocket.

== How does it work? ==

It receives a websocket connection with path of the form /call/{id}, e.g., /call/1234
There may be atmost two connections in each path for a specific id.

The two clients connected to the same path can exchange signaling data,

 C->S: {"method": "NOTIFY", "data": {"type": "offer", "sdp": ...}}

The message is forwarded to the other client, without modification,

 S->C: {"method": "NOTIFY", "data": {"type": "offer", "sdp": ...}}

If the other client is not already connected, the data is queued and delivered on subsequent
connection.

Additionally, a client may request configuration data to create RTCPeerConnection,

 C->S: {"method": "GET", "msg_id": 123, "resource": "/peerconnection"}

and the server responds with the data, duplicating the received "msg_id", so that the
client knows which request this response corresponds to.

 S->C: {"msg_id": 123, "code": "success", "result": {"configuration": {"iceServers": [...]}}}

== How to create the client in JavaScript? ==

First, use the getUserMedia function to get the user's camera and microphone stream,

  navigator.mediaDevices.getUserMedia({audio: true, video: true})
  .then(function(stream) {
    local_stream = stream;
    video1.srcObject = stream;
    pc.onaddstream = function(stream) {
      video2.srcObject = stream;
    }
    ...
  })

On the caller side, use some random id, say 2310, and connect, whereas on the callee side
use the same id of the caller, and connect.
  
  ws = new WebSocket("ws://this-server/call/2310")
  
When connected, get the peer connection configuration from result and create any
RTCPeerConnection object as needed.

  ws.onopen = function() {
    ws.send(JSON.stringify({method: "GET", msg_id: 1, resource: "/peerconnection"}))
  };
  
  ws.onmessage = function(event) {
    var message = JSON.parse(event.data);
    if (message.code == "success" and message.msg_id == 1) {
        pc = new RTCPeerConnection(message.result.configuration);
        ...
    }
    ...
  }

Use the methods in RTCPeerConnection to add the local stream, and create offer session,
and send it to the other client over the WebSocket.
  pc.addStream(local_stream);
  pc.createOffer()
  .then(function(offer) {
    return pc.setLocalDescription(offer);
  })
  .then(function() {
    ws.send(JSON.stringify({method: "NOTIFY", data: pc.localDescription}));
    ...
  });

Any ICE candidates generated by the RTCPeerConnection object should also be sent to the other
client as follows,

  pc.onicecandidate = function(event) {
    ws.send(JSON.stringify({method: "NOTIFY", data: event.candidate}))
  }

When an answer or remote ICE candidate is received, supply it to the RTCPeerConnection object,

  ws.onmessage = function(event) {
    ...
    if (message.method == 'NOTIFY' && message.data.candidate) {
      pc.addIceCandidate(new RTCIceCandidate(message.data));
    }
    if (message.method == 'NOTIFY' && message.data.type == "answer") {
      pc.setRemoteDescription(new RTCSessionDescription(message.data));
    }
    ...
  }

On the callee side, you may wait for the received offer before getting the local stream and
creating an RTCPeerConnection object. In any case, an offer is received, supply it to the
RTCPeerConnection object, create an answer and then send it to the other client.

  ws.onmessage = function(event) {
    ...
    if (message.method == 'NOTIFY' && message.data.type == "offer") {
      ...
      pc.setRemoteDescription(new RTCSessionDescription(message.data));
      ...
      pc.createAnswer()
      .then(function(answer) {
        return pc.setLocalDescription(answer);
      })
      .then(function() {
        ws.send(JSON.stringify({method: "NOTIFY", data: pc.localDescription}));
      });
    }
  }

Please see webrtc.html for an example web page that connects to this server over WebSocket
to exchange signaling negotiations, and to establish a video call between two instances
of the web page.
'''

import sys, traceback, logging, re, json
from ....std.ietf.rfc6455 import HTTPError, serve_forever as websocket_serve_forever


logger = logging.getLogger('notify')
configuration = {'iceServers': [{"url": "stun:stun.l.google.com:19302"}]}


class Space(object):
    def __init__(self, path):
        self.path, self.requests, self.pending = path, [], []
        logger.info('creating space %r', self.path)
    
    def __del__(self):
        logger.info('deleting space %r', self.path)
    
    @property
    def is_full(self):
        return len(self.requests) >= 2
    
    @property
    def is_empty(self):
        return len(self.requests) == 0
    
    
    def add(self, request):
        self.requests.append(request)
    
    def remove(self, request):
        try: self.requests.remove(request)
        except: pass # ignore if not found
        self.pending[:] = [(r,d) for r,d in self.pending if r != request]
    
    def get_other(self, request):
        result = [x for x in self.requests if x != request]
        return result and result[0] or None
    
    

spaces = {} # table from path to Space object


def onhandshake(request, path, headers):
    if path in spaces:
        space = spaces[path]
        if space.is_full:
            logger.error('space is full, closing')
            raise HTTPError('400 Bad Request - Space Full')


def onopen(request):
    if request.path in spaces:
        space = spaces[request.path]
    else:
        space = spaces[request.path] = Space(request.path)
        
    space.add(request)
    
    for ignore, data in space.pending:
        request.send_message(json.dumps({'method': 'NOTIFY', 'data': data}))
    space.pending[:] = []


def onclose(request):
    if request.path in spaces:
        space = spaces[request.path]
        other = space.get_other(request)
        space.remove(request)
        if other:
            space.remove(other)
            other.close()
        if space.is_empty:
            del spaces[request.path]


def onmessage(request, message):
    logger.debug("onmessage %r:\n%r", "%s:%d" % request.client_address, message)
    data = json.loads(message)
    
    if data['method'] == 'GET' and data['resource'] == '/peerconnection':
        response = {'code': 'success', 'result': {'configuration': configuration}}
        if 'msg_id' in data:
            response['msg_id'] = data['msg_id']
        request.send_message(json.dumps(response))    
    
    elif data['method'] == 'NOTIFY':
        if request.path in spaces:
            space = spaces[request.path]
        else:
            space = spaces[request.path] = Space(request.path)
        
        other = space.get_other(request)
        if other:
            try:
                other.send_message(json.dumps({'method': 'NOTIFY', 'data': data['data']}))
            except:
                pass # ignore if socket was closed.
        else:
            space.pending.append((request, data['data']))
        

def serve_forever(options):
    if not re.match('(tcp|tls):[a-z0-9_\-\.]+:\d{1,5}$', options.listen):
        raise RuntimeError('Invalid listen option %r'%(options.listen,))
        
    typ, host, port = options.listen.split(":", 2)
    if typ == 'tls' and (not options.certfile or not options.keyfile):
        raise RuntimeError('Missing certfile or keyfile option')
    
    params = dict(onopen=onopen, onmessage=onmessage, onclose=onclose, onhandshake=onhandshake)
    params['paths'] = options.paths or None
    params['hosts'] = options.hosts or None
    params['origins'] = options.origins or None
        
    params.update(hostport=(host, int(port)))
    if typ == 'tls':
        params.update(certfile=options.certfile, keyfile=options.keyfile)
    
    websocket_serve_forever(**params)


if __name__ == "__main__":
    from optparse import OptionParser, OptionGroup
    parser = OptionParser()
    parser.add_option('-d', '--verbose', dest='verbose', default=False, action='store_true',
                      help='enable debug level logging instead of default info')
    parser.add_option('-q', '--quiet', dest='quiet', default=False, action='store_true',
                      help='quiet mode with only critical debug level instead of default info')
    parser.add_option('-l', '--listen', dest='listen', metavar='TYPE:HOST:PORT',
                      help='listening transport address of the form TYPE:HOST:PORT, e.g., -l tcp:0.0.0.0:8080 or -l tls:0.0.0.0:443')
    parser.add_option('--certfile', dest='certfile', metavar='FILE',
                      help='certificate file in PEM format when a TLS listener is specified.')
    parser.add_option('--keyfile', dest='keyfile', metavar='FILE',
                      help='private key file in PEM format when a TLS listener is specified.')
    parser.add_option('--path', dest='paths', default=[], metavar='PATH', action='append',
                      help='restrict to only allowed path in request URI, and return 404 otherwise. This option can appear multiple times, e.g., --path /gateway --path /myapp')
    parser.add_option('--host', dest='hosts', default=[], metavar='HOST[:PORT]', action='append',
                      help='restrict to only allowed Host header values, and return 403 otherwise. This option can appear multiple times, e.g., --host myserver.com --host localhost:8080')
    parser.add_option('--origin', dest='origins', default=[], metavar='URL', action='append',
                      help='restrict to only allowed Origin header values, and return 403 otherwise. This option can appear multiple times, e.g., --origin https://myserver:8443 --origin http://myserver')
    parser.add_option('--test', dest='test', default=False, action='store_true',
                      help='test this module and exit')
    
    (options, args) = parser.parse_args()

    if options.test:
        sys.exit() # no tests
        
    logging.basicConfig(level=logging.CRITICAL if options.quiet else logging.DEBUG if options.verbose else logging.INFO, format='%(asctime)s.%(msecs)d %(name)s %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    
        
    if len(sys.argv) == 1: # show usage if no options supplied
        parser.print_help()
        sys.exit(-1)
        
    try:
        if not options.listen:
            raise RuntimeError('missing --listen TYPE:HOST:PORT argument')

        serve_forever(options)
    except KeyboardInterrupt:
        logger.debug('interrupted, exiting')
    except RuntimeError, e:
        logger.error(str(e))
