# Remote access: who can read the store from ChatGPT

ChatGPT cannot start a program on your machine, so it reaches `engmem mcp` through a bridge. This
page says which bridge engmem supports, what each one protects, and how to check the US-19
acceptance criteria against the real tunnel yourself.

## The two bridges

| Bridge | Who can read the store | Status in engmem's output |
|---|---|---|
| OpenAI's Secure MCP Tunnel (`tunnel-client`) | whoever OpenAI lets reach the tunnel; engmem does not see who that is | `bridge:` OpenAI decides who may reach it, and engmem did not verify that |
| Public bridge (`supergateway` plus `ngrok` or `cloudflared`) | anyone who holds the URL | `public bridge:` not protected |

The tunnel is the one supported bridge (owner decision, 2026-10-10). engmem itself still opens no
port and makes no network call; `tunnel-client` does. engmem has no authorization code of its
own, so it never calls a setup "protected": it cannot see who OpenAI lets in. Until you have run
the check below, who can reach your tunnel is OpenAI's claim, not something you have seen.

A public bridge is a test setup. `engmem mcp --read-only` behind it refuses every write, but it
does not make the store private: whoever holds the URL reads documents, titles and search
snippets through the two search tools. Read-only is not privacy.

`engmem install --agent chatgpt`, `engmem doctor --agent chatgpt` and the summary of
`scripts/setup-openai.sh` print both statuses. Doctor reports whether `tunnel-client` is on
PATH, but it does not read the tunnel profile or run the tool, so it does not claim which bridge
ChatGPT actually uses.

## Owner acceptance check (AC-19.1 to AC-19.3)

These criteria are enforced by OpenAI's tunnel, not by engmem. They were not checked by running
them: the test suite never starts `tunnel-client`, a bridge or ChatGPT. Run the steps below with
the real tunnel and record the result.

You need two ChatGPT accounts your workspace authorizes for the app, A and B, and one account
outside the workspace, C. Without a second authorized account, AC-19.3 is inconclusive.

### Set up a store with synthetic content only

Do not copy a real document into this store. Everything in it goes through the tunnel to
ChatGPT, stays in the conversation history, and is what an unauthorized account would see if a
step fails.

1. `engmem install --agent chatgpt --store ~/engmem-remote-check` creates the store and prints
   the tunnel commands.
2. Save the document below as
   `~/engmem-remote-check/sessions/20260101-zebracanary-check.md`. It is made up; the word
   `zebracanary` in its title and body is how you will recognise a leak.

<!-- canary-document: begin -->
```markdown
---
id: 20260101-zebracanary-check
title: Zebracanary remote access check
date: 2026-01-01
task_date: 2026-01-01
status: active
superseded_by:
backfilled: false
tags: [check]
entities: [Zebracanary]
related: []
covers_files: []
verified_at_commit:
capture_minutes: 1
mode: daily
---

## Decision Log

Zebracanary is a made-up word. This document exists only to check who can search this store.

## Cold-start primer

If zebracanary appears in a client that was not authorized, the bridge let it read the store.
```
<!-- canary-document: end -->

3. `engmem search zebracanary --store ~/engmem-remote-check` finds it.
4. Create the tunnel profile from the command install printed, with `--read-only` added at the
   end of the `--mcp-command` value, inside its quotes. Install prints that value already
   quoted, for example (POSIX shells):

   ```
   --mcp-command '/path/to/python -m engmem.cli mcp --store /home/you/engmem-remote-check'
   ```

   which becomes:

   ```
   export CONTROL_PLANE_API_KEY=<the tunnel's runtime key>
   tunnel-client init --sample sample_mcp_stdio_local --profile engmem-check \
     --tunnel-id <tunnel-id> \
     --mcp-command '/path/to/python -m engmem.cli mcp --store /home/you/engmem-remote-check --read-only'
   tunnel-client run --profile engmem-check
   ```

   On Windows the value is in double quotes; add ` --read-only` before the closing `"`. Do not
   wrap it in a second pair of quotes.
5. In ChatGPT, create the app with Connection: Tunnel, as `install` printed, and make it
   available to A and B.

Every search engmem serves adds one line to `~/engmem-remote-check/telemetry.jsonl`. Count the
lines (`wc -l`) before and after each step: a request that never reached engmem adds none. Keep
`tunnel-client run` going for every step below.

### AC-19.2: an authorized client reads, writes are refused

From A, ask ChatGPT to search the store for `zebracanary`. Pass: the document is found, and the
telemetry file gained a line. Then ask it to create a draft. Pass: the call fails with an error
naming `--read-only`, and `sessions/` holds the same files as before.

### AC-19.1: no authorization, no content

From C, try to add or call the same app. Then, as a positive control, search for `zebracanary`
from A once more.

- Pass: C cannot reach the tunnel, no `zebracanary`, title or snippet appears for C, and C's
  attempt added no telemetry line, while A's search found the document and added one.
- Inconclusive: A's control search fails or adds no line. The tunnel was not serving, so C's
  refusal proves nothing; fix the setup and repeat.

### AC-19.3: a revoked client is refused before reading

Revoke B's authorization for the app in your OpenAI workspace while the tunnel and
`tunnel-client run` keep running. Use the control that removes access for B only; which one your
workspace offers is OpenAI's, so record the one you used. Deleting the tunnel, stopping
`tunnel-client` or deleting the app for everyone does not count: it removes the path for every
client, so a refusal would prove nothing about B.

Then send one more search for `zebracanary` from B, and after it the same search from A.

- Pass: B's request is refused, nothing from the store appears for B, and B's attempt added no
  telemetry line, while A's search found the document and added one.
- Inconclusive: A's control search fails or adds no line, or there was no second authorized
  account to revoke.

### Clean up

Stop `tunnel-client`, delete the app and the `engmem-check` profile, delete the ChatGPT
conversations used for the check in A, B and C, and remove `~/engmem-remote-check`.

## What engmem's tests do check

- `--read-only` lists only the two search tools and refuses every write tool without changing the
  store (`tests/test_mcp_read_only.py`).
- Install output and the setup script's summary name the tunnel, mark a public bridge not
  protected, and never call anything protected (`tests/test_install_codex_chatgpt.py`). On POSIX
  the script's summary is checked by running it with stub tools: it names the tunnel only when
  `tunnel-client init` ran.
- Doctor reports the tunnel and the public bridge as `unverified`, never `ok`, and never runs
  `tunnel-client` (`tests/test_doctor.py`).
- The canary document above loads as a valid active document and is found by its word
  (`tests/test_install_codex_chatgpt.py`).
- engmem opens no socket (`tests/test_no_network.py`).
