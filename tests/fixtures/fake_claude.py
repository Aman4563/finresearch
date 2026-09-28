#!/usr/bin/env python3
"""Stand-in for the `claude` CLI. Behaviour chosen by FAKE_CLAUDE_SCENARIO. Emits stream-json like v2.1.283."""

import json
import os
import sys

scenario = os.environ.get("FAKE_CLAUDE_SCENARIO", "ok")
args = sys.argv[1:]


def emit(ev):
    print(json.dumps(ev), flush=True)


if args[:2] == ["auth", "status"]:
    logged = scenario != "logged_out"
    emit({"loggedIn": logged, "authMethod": "claude.ai" if logged else None, "subscriptionType": "max"})
    sys.exit(0)

prompt = sys.stdin.read()
leak = "ANTHROPIC_API_KEY" in os.environ  # the Max tier must never see an API key
schema = json.loads(args[args.index("--json-schema") + 1]) if "--json-schema" in args else None
rl_ok = {
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "allowed",
        "resetsAt": 4102444800,
        "rateLimitType": "five_hour",
        "overageStatus": "rejected",
        "unifiedWindows": {
            "five_hour": {"utilization": 0.40, "resetsAt": 4102444800},
            "seven_day": {"utilization": 0.20, "resetsAt": 4103000000},
        },
    },
}
emit({"type": "system", "subtype": "init", "session_id": "s1", "model": "fake", "tools": []})

if scenario == "limit":
    rl = json.loads(json.dumps(rl_ok))
    rl["rate_limit_info"]["status"] = "rejected"
    emit(rl)
    emit(
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "result": "Claude AI usage limit reached|4102444800",
            "session_id": "s1",
        }
    )
    sys.exit(1)
if scenario == "overloaded":
    for i in range(1, 3):
        emit(
            {
                "type": "system",
                "subtype": "api_retry",
                "attempt": i,
                "error": "overloaded",
                "error_status": 529,
            }
        )
    emit(
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "result": "API Error: overloaded",
            "api_error_status": 529,
            "session_id": "s1",
        }
    )
    sys.exit(1)
if scenario == "auth_fail":
    emit({"type": "system", "subtype": "api_retry", "attempt": 1, "error": "authentication_failed"})
    emit(
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "result": "auth",
            "session_id": "s1",
        }
    )
    sys.exit(1)
if scenario == "crash":
    print("segfault-ish garbage", file=sys.stderr)
    sys.exit(3)
if scenario == "no_structured":
    emit(rl_ok)
    emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "plain text",
            "session_id": "s1",
            "total_cost_usd": 0.01,
            "usage": {"input_tokens": 5, "output_tokens": 5},
            "num_turns": 1,
        }
    )
    sys.exit(0)
if scenario == "near_ceiling":
    rl = json.loads(json.dumps(rl_ok))
    rl["rate_limit_info"]["unifiedWindows"]["five_hour"]["utilization"] = 0.95
    rl_ok = rl

emit(rl_ok)
out = {"answer": "ok", "leak": leak, "echo": prompt[:20]}
emit(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": json.dumps(out),
        "session_id": "s1",
        "structured_output": out if schema else None,
        "total_cost_usd": 0.0123,
        "usage": {"input_tokens": 10, "cache_read_input_tokens": 100, "output_tokens": 20},
        "num_turns": 2,
        "modelUsage": {"claude-haiku-4-5": {}},
    }
)
