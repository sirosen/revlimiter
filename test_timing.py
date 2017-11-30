#!/usr/bin/env python3

import uuid
import time
import argparse
import asyncio
import aiohttp

# requests overhead, but no process overhead


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--route", default='/')
    parser.add_argument("-n", default=1, type=int)
    parser.add_argument("--async", dest='synchronous',
                        default=True, action='store_false')
    parser.add_argument("--uniquify", default=False, action='store_true')
    return parser.parse_args()


args = parse_args()

target_url = 'http://127.0.0.1:8888{}'.format(args.route)


async def make_request(session, i):
    postdata = {"requester_id": "foouser", "resource_id": "barresource"}
    if args.uniquify:
        postdata['requester_id'] = str(uuid.uuid1())
    async with session.post(target_url, json=postdata) as r:
        await r.text()


async def do_timing():
    async with aiohttp.ClientSession() as session:
        # make a single request (outside of timing) to pre-warm the session
        await make_request(session, 0)

        if args.synchronous:
            start = time.time()
            for i in range(args.n):
                await make_request(session, i)
            stop = time.time()
        else:
            start = time.time()
            await asyncio.gather(*[make_request(session, i)
                                   for i in range(args.n)])
            stop = time.time()

    print(str(stop - start))


loop = asyncio.get_event_loop()
loop.run_until_complete(do_timing())
loop.close()
