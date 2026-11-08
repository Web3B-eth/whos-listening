#!/bin/zsh
# Re-run one spike turn against the fake model server. Scratch HERMES_HOME only.
#   1. ../../.local/venv-023/bin/python fake_llm.py 8765 requests.jsonl &   (note its PID)
#   2. copy spike_mem/ to $HERMES_HOME/plugins/spike-mem/ and config.yaml to $HERMES_HOME/
#   3. SPIKE_STRIP=1 ./run.sh run2 --provider chat-route -m chat-model
# The CLI starts a background gateway (`gateway run --idle-exit`) that keeps the
# environment of the run that started it. Stop it by its PID (see `ps`) between runs
# that change SPIKE_* variables. Stop the fake server by the PID from step 1.
name=$1; shift
export HERMES_HOME=${HERMES_HOME:?set a scratch HERMES_HOME} SPIKE_LOG=$PWD/$name.jsonl SPIKE_DUMMY_KEY=dummy-not-a-secret
rm -f $name.jsonl; : > requests.jsonl
hermes chat -Q -q "What should I cook for dinner this week given what you know about me?" "$@" < /dev/null > $name.out 2>&1
echo "exit $?"; tail -3 $name.out
echo "--- plugin log"; cat $name.jsonl
echo "--- fake server saw"; grep -v api/show requests.jsonl
