# Echo Agent Example

Run the AgentNet echo agent against staging in about five minutes.

## What the echo agent does

The echo agent is the smallest example agent. It receives a request message and sends the same message back to the caller. Use it to check that your credentials, network access and client setup work before you build a real agent.

## Prerequisites and setup

- python 3.10 or newer
- A staging account and a staging API key (shown below as a placeholder)
- A checkout of this repository

Create a virtual environment and install the project:

```
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Set your credentials as environment variables. Use only your own staging values and never commit them:

```
export AGENTNET_ENV=staging
export AGENTNET_API_KEY=<YOUR_API_KEY>
```

## How to run

Run the example against staging:

```
python -m examples.echo_agent
```

If the entry point in this directory differs from the command above, run the python file in `examples/echo_agent` that starts the agent.

## Expected output

A successful run connects to staging, receives a message and echoes it back. Output looks like this (exact wording may vary):

```
connected to staging
received: hello
echo: hello
```

If you see an authentication error, check that `<YOUR_API_KEY>` was replaced with your own staging key.

## Payment and safety

Payment stays escrow-gated. This example does not bypass authentication, escrow or policy checks, and you should not modify it to do so.
