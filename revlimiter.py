import sys
import time
import json

import tornado.escape
import tornado.httpserver
import tornado.ioloop
import tornado.netutil
import tornado.web


class Throttler(object):
    """
    A generic object which implements the Token Bucket throttling algorithm.
    Uses a simple 2-tier nested dict of storage to partition and sort requests
    by the requester and the resource being requested.
    This is an applicable method for distributed/shareded storage (inspired by
    the original version of this which ran against DynamoDB), and incurs little
    to no impact on the pure memory version.

    The only method you should need (other than the constructor) is
    `Throttler.handle_event` which consumes a dictionary representing some
    input event that may or may not be throttled and returns a data dict with
    info about whether or not to throttle the request.
    """
    __slots__ = ['_resource_buckets',
                 '_fill_rate', '_bucket_max', '_bucket_start']

    def __init__(self, fill_rate, bucket_max, bucket_start):
        """
        The main attribute of a Throttler is a set of data buckets for
        different throttleable resources.
        """
        self._resource_buckets = {}

        # Token Bucket algorithm params
        # fill rate is num tokens gained per second
        self._fill_rate = fill_rate
        # bucket max is maximum number of tokens a requester can accrue
        self._bucket_max = bucket_max
        # bucket start is the number of tokens each bucket starts with
        self._bucket_start = bucket_start

    def _get_item(self, resource_id, requester_id, now):
        """
        Retrieve a data item about the throttling state of a resource +
        requester combination. Requires that we pass the current time (now) in
        order to ensure that all functionality in the Throttler has a
        consistent view of the time.
        """
        # get the collection of requester buckets for a resource ID
        # if absent, initialize it as an empty dict
        try:
            bucket_collection = self._resource_buckets[resource_id]
        except KeyError:
            bucket_collection = self._resource_buckets[resource_id] = {}

        # now, look for an item that represents this requester in that resource
        # bucket, and if it's absent initialize it as a dict containing the
        # last access time and the number of available tokens
        try:
            item = bucket_collection[requester_id]
        except KeyError:
            item = bucket_collection[requester_id] = {
                'last_access': now,
                'num_tokens': self._bucket_start}

        return item

    def _update_item(self, item, now):
        """
        Add tokens to a bucket based on the amount of time elapsed
        Update last access time
        """
        last_access = item['last_access']
        num_tokens = item['num_tokens']

        # delta is, at worst, 0, in case of bad / inaccurate time comparisons
        delta = max(0, self._fill_rate * (now - last_access))

        item['num_tokens'] = min(self._bucket_max, num_tokens + delta)
        item['last_access'] = now

    def _consume_token(self, resource_id, requester_id, now):
        """
        Get and update an item, then attempt to consume a token from that item.
        An item is a token bucket for a resource ID + requester ID.

        Returns True if a token was consumed, and False if there was
        insufficient capacity for a token to be consumed (meaning that the
        request should probably be throttled).
        """
        item = self._get_item(resource_id, requester_id, now)
        self._update_item(item, now)

        new_val = item['num_tokens'] - 1
        if new_val < 0:
            return False
        else:
            item['num_tokens'] = new_val
            return True

    def handle_event(self, event):
        '''
        Events are throttle requests -- json data of the following format:

          {
            "requester_id": "...",
            "resource_id": "...",
            "throttle_params": {
               ...
            }
          }

        In which the components have the following meanings:

          - requester_id: Who are we throttling? Typically a Token.
          - resource_id: What resource is being throttled? Typically an
            identifier for a service + a resource on that service. Good
            examples include "example.com",
            "api.example.com/resource", and
            "example:API:6fd91eb4-59a7-11e6-8bbc-005056c00001" -- format is up
            to the developer using revlimiter
          - throttle_params: Arbitrary bucket of params to the throttler.

        handle_event() consumes an event, finds its token bucket, attempts to
        spend a token from that bucket, and then returns a datadict with two
        keys:

          - allow_request: A boolean. True means don't throttle, False means
            that the throttling limits have been exceeded.
          - denial_details: A string or null. Contains any message from the
            throttler back to the requester.
        '''
        requester_id = event['requester_id']
        resource_id = event['resource_id']
        try:
            throttle_params = event['throttle_params']
        except KeyError:
            throttle_params = {}

        # no-op with throttle_params for now
        assert throttle_params is not None

        now = time.time()

        # add tokens based on time elapsed, update last access time, and
        # attempt to remove a token (if possible)
        has_capacity = self._consume_token(requester_id, resource_id, now)

        if has_capacity:
            return {'allow_request': True, 'denial_details': None}
        else:
            return {'allow_request': False,
                    'denial_details': 'No detail today!'}


class ThrottleTornadoHandler(tornado.web.RequestHandler):
    """
    This is a Tornado web request handler for requests
    It is initialized with a throttler object to track state.

    Only accepts POST requests, which must send a requester ID and a resource
    ID encoded in a JSON body. The request is decoded and passed to the
    throttler's handle_event method.
    The results of that call are written back to the caller verbatim.
    """
    def initialize(self, throttler):
        """
        Setup the handler. Only runs once per Tornado Server.
        """
        self.throttler = throttler

    def post(self):
        """
        Handle POST /
        Runs on every request.
        """
        request = tornado.escape.json_decode(self.request.body)
        self.write(self.throttler.handle_event(request))


def make_tornado_app(config):
    """
    Given throttler config, create a Tornado application with its routes
    mapped to various handlers.
    """
    routes = config['routes']

    return tornado.web.Application([
        (path,
         ThrottleTornadoHandler,
         {'throttler': Throttler(routeconf['fill_rate'],
                                 routeconf['bucket_max'],
                                 routeconf['bucket_start'])
          }
         )
        for path, routeconf in routes.items()
    ])


def run(config):
    """
    Does these steps:
    - make a Tornado HTTP Server (synchronous) from config
    - bind to a unix socket or a port
    - start Tornado listening
    """
    server = tornado.httpserver.HTTPServer(make_tornado_app(config))

    sockconf = config['socket']
    # if in unix mode, bind a unix socket
    if sockconf['mode'] == 'unix':
        unix_socket = tornado.netutil.bind_unix_socket(sockconf['path'])
        server.add_socket(unix_socket)
    # if in net mode, bind to the given port num
    elif sockconf['mode'] == 'net':
        norm_sockets = tornado.netutil.bind_sockets(sockconf['port'])
        server.add_sockets(norm_sockets)
    # otherwise, we were given a back sock_mode
    else:
        raise ValueError('Invalid sock_mode: {}'.format(sockconf['mode']))

    tornado.ioloop.IOLoop.instance().start()


def main():
    if len(sys.argv) > 1:
        conf_file = sys.argv[1]
    else:
        conf_file = 'config.json'

    with open(conf_file) as f:
        conf = json.load(f)

    run(conf)


if __name__ == '__main__':
    main()
