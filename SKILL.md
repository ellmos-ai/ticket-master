---
name: ticket-master
description: "Use when the user wants to keep an open triage console for a software project - capturing bugs/change-requests as structured tickets, scoring them, and routing them to the right AI provider/sub-agent for an immediate fix or into the project's task management. Cloud-ready: works across multiple machines sharing a cloud-synced folder via filename-based claims. Triggers on /ticket-master or 'open the triage console / ticket master'."
---

# ticket-master — Triage Console Workflow

ticket-master is a **workflow / operating mode**, not a tool that acts on its own.
You (the agent) follow the TICKET-MASTER prompt and stay at **Position 0** — an open
triage console. When the user types a bug, change request, or any project problem,
you capture it, triage it, and route it.

**Cloud-Ready / Multi-System:** The ticket queue works across multiple machines sharing
a cloud-synced folder. Claims are signalled via filename rename (atomic on NTFS) —
no lock files needed. The audit trail lives per ticket (STATUS/LOG/SOLUTION fields
in each `T-….<HOST>.txt`); the shared `tickets/_logs/` intake log is deprecated.

## How to enter this mode

1. **Read the prompt.** Load `prompts/TICKET-MASTER.${TM_LANG:-en}.md` (default `en`;
   `de` is also available) and follow it as your operating instructions for the
   whole session.
2. **Read the config.** Use `config/ticket-master.config.json`. If it does not exist,
   copy it from `config/ticket-master.config.example.json` first, then have the user
   fill in `project_roots[]` and verify the provider commands.
3. **Go to Position 0.** Orient yourself on the configured projects, then wait
   silently for the user's first ticket. Do not act until a ticket arrives.

## The loop (per ticket)

```
capture  -> unclaimed structured ticket file in INBOX
triage   -> assign to the right project (+ optional domain/endpoint) +
            urgency (decoupled from the 5-dimension score) + score
route    -> delegate to best provider/sub-agent for an immediate fix,
            OR file into the project's own task management / later sink
back to Position 0 -> wait for the next ticket
```

If `config/domains.json` and `config/urgency.json` exist (optional
personal-assistant expansion, see `config/*.example.json`), the prompt
resolves a domain/endpoint at intake and gates urgency (`sofort`/`heute`/
`woche`/`backlog`) before scoring — see the full prompt for the URGENCY GATE.

## Roles

- **Worker:** A sub-agent (or Companion sub-agent reused across a ticket series) does
  the actual reading/editing/verifying and reports back compactly (commit hash + one
  line). You stay lean and route only.
- **Advisor:** For high-stakes tickets (score >= 35), an advisor model reviews before
  changes land (see `advisor` in the config).

The full protocol — decision ladder, score formula, gates, fallback chain,
ticket lifecycle — lives in the prompt. Read it; do not duplicate it here.
