#!/bin/bash

[[ -d .venv ]] || (virtualenv --python python3 .venv &&
                   .venv/bin/pip install -r requirements.txt &&
                   .venv/bin/pip install -r test-requirements.txt)
source .venv/bin/activate
flake8 revlimiter.py

# make sure we cleanup this background job when we're done
trap 'kill $(jobs -p)' EXIT
python revlimiter.py > revlimiter.log 2>&1 &

# let it start
sleep 1

echo -n "time of a single regular call: "
python test_timing.py

echo -n "time of a single lenient call: "
python test_timing.py --route '/lenient'

for n in 1000 5000 10000; do
    echo -n "time of $n async calls: ";
    python test_timing.py -n "$n" --async;
    echo -n "time of $n sequential calls: ";
    python test_timing.py -n "$n";
done
