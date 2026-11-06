#!/usr/bin/env python3
"""The smallest Switch agent on switch_client.Agent: answers whoever addresses it.

  python3 examples/echo_agent.py --name m4t-echo \
      --url http://127.0.0.1:18400 --token-file devstack/state/registration.token --state /tmp/m4t-echo

Credentials are written once (0600) under --state; the cursor and handled message ids are kept
there too, so a restart resumes where it left off and never answers a message twice. Only
messages that address the agent arrive; its own messages and runaway threads are filtered out.
Add it to a room (create_room / invite_agent_to_room) and @-mention it.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from switch_client import Agent  # noqa: E402

p = argparse.ArgumentParser()
p.add_argument("--name", required=True)
p.add_argument("--url", default="http://127.0.0.1:18400")
p.add_argument("--token-file", required=True)
p.add_argument("--state", required=True, help="directory for credentials + cursor (created 0700)")
a = p.parse_args()
os.makedirs(a.state, mode=0o700, exist_ok=True)


def handle(msg):
    print(f"{msg.sender_name} in {msg.room_id}: {msg.body!r}", flush=True)
    if msg.gap:
        print("  (history was lost before this message: read_context before acting)", flush=True)
    return f"echo: {msg.body}"  # posted as a reply in the message's thread


agent = Agent(a.url, a.name, on_message=handle, description="Echoes what it is told.",
              credentials_file=os.path.join(a.state, "credentials.json"),
              registration_token_file=a.token_file,
              state_file=os.path.join(a.state, "state.json"),
              log=lambda *x: print("[switch]", *x, file=sys.stderr, flush=True)).start()
print(f"{a.name} connected: {agent.wait_connected(30)}", flush=True)
agent.run_forever()
