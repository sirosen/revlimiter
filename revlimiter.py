#!/usr/bin/env python3

import sys
import os
import subprocess
import time
import json

import aiohttp.web
import asyncio


def getmem(unit=None):
    """use ps command to easily and reliably get mem info
    not very performant"""
    div = {
        'KB': 1,
        'MB': 1024
    }.get(unit, 1)
    lines = subprocess.run(['ps', 'v', '-p', str(os.getpid())],
                           stdout=subprocess.PIPE, check=True
                           ).stdout.split(b'\n')
    headers, content = lines[0].split(), lines[1].split()
    return int(float(content[headers.index(b'RSS')]) / div)


class Throttler(object):
    """
    A generic object which implements the Token Bucket throttling algorithm.
    Uses a simple 2-tier nested dict of storage to partition and sort requests
    by the requester and the resource being requested.
    This is an applicable method for distributed/shareded storage (inspired by
    the original version of this which ran against DynamoDB), and incurs little
    to no impact on the pure memory version.

    The only method you should need (other than the constructor) is
    `Throttler.handle_request` which consumes a request representing some
    input event that may or may not be throttled and returns a data dict with
    info about whether or not to throttle the request.
    """
    __slots__ = ['_resource_buckets', '_writer_lock',
                 '_fill_rate', '_bucket_max', '_bucket_start']

    def __init__(self, fill_rate, bucket_max):
        """
        The main attribute of a Throttler is a set of data buckets for
        different throttleable resources.
        """
        self._resource_buckets = {}

        # a lock to protect writes
        self._writer_lock = asyncio.Lock()

        # Token Bucket algorithm params
        # fill rate is num tokens gained per second
        self._fill_rate = fill_rate
        # bucket max is maximum number of tokens a requester can accrue
        self._bucket_max = bucket_max
        # bucket start is the number of tokens each bucket starts with -- for
        # our case, let's just set it to the bucket max, which keeps things
        # simpler
        self._bucket_start = bucket_max

        print('Creating throttler[{}](fill_rate={}, max={})'
              .format(id(self), fill_rate, bucket_max),
              file=sys.stderr)

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

    async def _consume_token(self, resource_id, requester_id, now):
        """
        Get and update an item, then attempt to consume a token from that item.
        An item is a token bucket for a resource ID + requester ID.

        Returns True if a token was consumed, and False if there was
        insufficient capacity for a token to be consumed (meaning that the
        request should probably be throttled).
        """
        with await self._writer_lock:
            item = self._get_item(resource_id, requester_id, now)
            self._update_item(item, now)

            new_val = item['num_tokens'] - 1
            if new_val < 0:
                return False
            else:
                item['num_tokens'] = new_val
                return True

    async def periodic_clean(self, app):
        """
        An async looping task which does a sweep of self._resource_buckets
        every N seconds.

        This is done in a two-phase procedure -- mark and sweep.
        """
        async def _clean_helper(seconds):
            while True:
                await asyncio.sleep(seconds)
                now = time.time()

                swept = 0
                with await self._writer_lock:
                    for resource_id, bucket in self._resource_buckets.items():
                        # MARK
                        marked = set()
                        for requester, item in bucket.items():
                                self._update_item(item, now)
                                if item['num_tokens'] >= self._bucket_max:
                                    marked.add(requester)
                        # SWEEP
                        if marked:
                            for requester in marked:
                                bucket.pop(requester, None)
                                swept += 1
                print('throttler[{}] Periodic cleanup got {} items'
                      .format(id(self), swept), file=sys.stderr)

        app.loop.create_task(_clean_helper(30))

    async def handle_request(self, request):
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

        handle_request() consumes an event, finds its token bucket, attempts to
        spend a token from that bucket, and then returns a datadict with two
        keys:

          - allow_request: A boolean. True means don't throttle, False means
            that the throttling limits have been exceeded.
          - denial_details: A string or null. Contains any message from the
            throttler back to the requester.
        '''
        event = await request.json()

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
        has_capacity = await self._consume_token(
            requester_id, resource_id, now)

        if has_capacity:
            response = {'allow_request': True, 'denial_details': None}
        else:
            response = {'allow_request': False,
                        'denial_details': 'No detail today!'}

        return aiohttp.web.Response(text=json.dumps(response))


async def periodic_stats(app):
    """
    Collect and print stats info to stderr
    """
    async def _stats_helper(seconds):
        while True:
            await asyncio.sleep(seconds)
            print('[stats] Using {}KB of RSS memory'.format(getmem()),
                  file=sys.stderr)

    app.loop.create_task(_stats_helper(60))


def setup_routes(app, config):
    for path, routeconf in config['routes'].items():
        throttler = Throttler(routeconf['fill_rate'], routeconf['bucket_max'])

        app.router.add_post(path, throttler.handle_request)

        app.on_startup.append(throttler.periodic_clean)

    app.on_startup.append(periodic_stats)


def run(config):
    """
    Does these steps:
    - make an aiohttp HTTP Server
    - attach routes
    - bind to a port or unix socket and start handling events
    """
    app = aiohttp.web.Application()
    setup_routes(app, config)
    sockconf = config['socket']
    if sockconf['mode'] == 'unix':
        aiohttp.web.run_app(app, path=sockconf['path'])
    elif sockconf['mode'] == 'net':
        aiohttp.web.run_app(app, port=sockconf['port'])
    # otherwise, we were given a back sock_mode
    else:
        raise ValueError('Invalid sock_mode: {}'.format(sockconf['mode']))


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
