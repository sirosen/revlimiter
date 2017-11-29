#!/bin/bash

[[ -d .venv ]] || virtualenv .venv
source .venv/bin/activate

pip install -r requirements.txt

# make sure we cleanup this background job when we're done
trap 'kill $(jobs -p)' EXIT
python revlimiter.py > revlimiter.log 2>&1 &

# let it start
sleep 1

echo -n "time of a single regular call: "
/usr/bin/time -f '\n%e' curl -s -XPOST 'localhost:8888/' -d '{"requester_id": "foouser", "resource_id": "barresource"}'

echo -n "time of a single lenient call: "
/usr/bin/time -f '\n%e' curl -s -XPOST 'localhost:8888/lenient' -d '{"requester_id": "foouser", "resource_id": "barresource"}'

echo -e "\nmake calls until failure\n"
echo "regular:"
for i in {0..30}; do
    { echo -n "${i}: ";
      curl -s -XPOST 'localhost:8888/' -d '{"requester_id": "foouser", "resource_id": "barresource"}';
      echo ""; };
done
echo "lenient:"
for i in {0..30}; do
    { echo -n "${i}: ";
      curl -s -XPOST 'localhost:8888/lenient' -d '{"requester_id": "foouser", "resource_id": "barresource"}';
      echo ""; };
done

echo -e "\nabout to try to overload the 'superburst' config"
echo "this will all write to test-superburst.log"

echo "beginning superburst test" >> test-superburst.log
for i in {0..1000}; do
    { echo -n "${i}: ";
      curl -s -XPOST 'localhost:8888/superburst' -d '{"requester_id": "foouser", "resource_id": "barresource"}';
      echo ""; } >> test-superburst.log;
done
echo "end superburst test" >> test-superburst.log

echo "superburst test done"
