"""Schema-v2 regression tests for multi-system ticket contracts."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[1] / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import routing_contract as rc  # noqa: E402
import ticket_audit  # noqa: E402
import ticket_mover  # noqa: E402
import ticket_writer  # noqa: E402


REGISTRY = {
    "source": "fixture://systems",
    "checked_at": "2026-08-22T08:00:00Z",
    "systems": {
        "ASUS-GEI": {"active": True, "slot": "laptop"},
        "WORKSTATION-LG": {"active": True, "slot": "workstation"},
        "MAC-STUDIO": {"active": False, "slot": "mac-studio"},
    },
}


def resolver(selector: str, runner: str | None = None):
    aliases = {"gpt": "codex"}
    canonical = aliases.get(selector, selector)
    profiles = {
        "claude": ("runner", "self", "claude", None, None),
        "codex": ("runner", "self", "codex", None, None),
        "agy": ("runner", "self", "agy", None, None),
        "claude-sonnet": ("family", "family", "claude", None, None),
        "claude-opus": ("family", "family", "claude", None, None),
        "gpt5": ("family", "family", "codex", None, None),
        "openai-gpt-5.6-sol": (
            "exact", "exact", "codex", "openai", "gpt-5.6-sol"
        ),
    }
    resolved = profiles.get(canonical)
    if resolved is None:
        return {
            "requested_selector": selector,
            "canonical_selector": None,
            "selector_type": "unresolved",
            "model_selection": None,
            "resolved": False,
            "claimable": False,
            "runner": None,
            "provider": None,
            "registry_name": None,
            "model_id": None,
            "allowed_runners": [],
            "eligible_models": [],
            "availability": {},
            "reason": "selector-not-in-registry",
            "registry_fingerprint": "sha256:fixture",
            "resolved_at": "2026-08-22T08:00:00Z",
        }
    selector_type, selection, selected_runner, provider, model_id = resolved
    compatible = runner is None or runner == selected_runner
    return {
        "requested_selector": selector,
        "canonical_selector": canonical,
        "selector_type": selector_type,
        "model_selection": selection,
        "resolved": True,
        "claimable": compatible,
        "runner": selected_runner,
        "provider": provider,
        "registry_name": canonical if selector_type == "exact" else None,
        "model_id": model_id,
        "allowed_runners": [selected_runner],
        "eligible_models": [canonical] if compatible else [],
        "availability": {
            "provider_documented": True,
            "provider_api_listed": True,
            "account_accessible": True,
            "runner_compatible": compatible,
            "host_ready": True,
        },
        "reason": None if compatible else "runner-not-compatible",
        "registry_fingerprint": "sha256:fixture",
        "resolved_at": "2026-08-22T08:00:00Z",
    }


class FixedRandom:
    def __init__(self, number: int = 123456789):
        self.number = number

    def randrange(self, *_args, **_kwargs):
        return self.number


def make_contract(
    tmp_path: Path,
    *,
    target_kind: str = "grouped",
    target: str | None = None,
    targets: list[str] | None = None,
    via: str | None = None,
    ticket_kind: str = "fork",
    binding_mode: str = "required",
    binding_ttl: int | str | None = None,
    execution_matrix: dict | None = None,
) -> Path:
    if target_kind == "grouped" and targets is None:
        targets = ["ASUS-GEI", "WORKSTATION-LG"]
    path = ticket_writer.create_routed_ticket(
        "Mehrsystemprüfung",
        "Prüfe den belegten Anteil je Zielsystem.",
        tickets_dir=tmp_path,
        registry_snapshot=REGISTRY,
        ticket_kind=ticket_kind,
        target_kind=target_kind,
        target=target,
        targets=targets,
        via=via,
        binding_mode=binding_mode,
        binding_ttl=binding_ttl,
        primary_ticket="T-20260822-100000000",
        original_owner="ASUS-GEI",
        receipt_to="T-20260822-100000000",
        execution_matrix=execution_matrix,
        resolver=resolver,
        today="2026-08-22",
        rng=FixedRandom(),
    )
    return Path(path)


def receipt(host: str, signature: str, *, status: str = "done", runner: str = "claude"):
    return {
        "signature": signature,
        "status": status,
        "executed_by": runner,
        "actual_provider": "anthropic" if runner == "claude" else "openai",
        "actual_model": "fixture-model",
        "occurred_at": "2026-08-22T10:00:00Z",
        "evidence": f"receipt://{host}/{signature}",
    }


def test_filename_grammar_keeps_v2_axes_and_legacy_claim_unambiguous():
    parsed = rc.parse_ticket_name(
        "T-20260822-123456789.to-grouped.via-agy.claim-ASUS-GEI.txt"
    )
    assert (parsed.target, parsed.via, parsed.claim) == ("grouped", "agy", "ASUS-GEI")
    exact = rc.parse_ticket_name(
        "T-20260822-123456789.via-openai-gpt-5.6-sol.txt"
    )
    assert exact.via == "openai-gpt-5.6-sol"
    legacy = rc.parse_ticket_name(
        "T-20260731-123456789.LAPTOP-WORKSTATION-LG.txt"
    )
    assert legacy.legacy_claim == "LAPTOP-WORKSTATION-LG"
    assert not legacy.is_v2


@pytest.mark.parametrize(
    "name",
    [
        "T-20260822-123456789.claim-ASUS-GEI.to-all.txt",
        "T-20260822-123456789.via-claude.to-all.txt",
        "T-20260822-123456789.to-all.to-grouped.txt",
        "T-20260822-123456789.via-claude.via-codex.txt",
        "T-20260822-123456789.claim-ASUS-GEI.extra.txt",
    ],
)
def test_filename_grammar_rejects_order_duplicates_and_unknown_segments(name):
    with pytest.raises(rc.RoutingContractError):
        rc.parse_ticket_name(name)


def test_user_aliases_normalize_through_system_registry_and_clutch_only():
    name, note = rc.normalize_alias(
        "T-20260822-123456789", ["all", "claude"],
        registry_snapshot=REGISTRY, resolver=resolver,
    )
    assert name == "T-20260822-123456789.to-all.via-claude.txt"
    assert note["registry_fingerprint"] == "sha256:fixture"
    name, _note = rc.normalize_alias(
        "T-20260822-123456789", ["WORKSTATION-LG", "claude-opus"],
        registry_snapshot=REGISTRY, resolver=resolver,
    )
    assert name == "T-20260822-123456789.to-WORKSTATION-LG.via-claude-opus.txt"
    name, _note = rc.normalize_alias(
        "T-20260822-123456789", ["gpt"],
        registry_snapshot=REGISTRY, resolver=resolver,
    )
    assert name == "T-20260822-123456789.via-codex.txt"
    for alias, canonical in (
        ("claude", "claude"),
        ("codex", "codex"),
        ("agy", "agy"),
        ("claude-sonnet", "claude-sonnet"),
        ("claude-opus", "claude-opus"),
        ("gpt5", "gpt5"),
        ("openai-gpt-5.6-sol", "openai-gpt-5.6-sol"),
    ):
        name, note = rc.normalize_alias(
            "T-20260822-123456789", [alias],
            registry_snapshot=REGISTRY, resolver=resolver,
        )
        assert name == f"T-20260822-123456789.via-{canonical}.txt"
        assert note["resolved"]
    name, note = rc.normalize_alias(
        "T-20260822-123456789", ["claude-opus-5"],
        registry_snapshot=REGISTRY, resolver=resolver,
    )
    assert name.endswith(".via-claude-opus-5.txt")
    assert not note["resolved"] and not note["claimable"]


def test_target_snapshot_is_fixed_evidence_and_never_a_hardcoded_host_list():
    all_targets = rc.resolve_targets("all", registry_snapshot=REGISTRY)
    assert all_targets["systems"] == ["ASUS-GEI", "WORKSTATION-LG"]
    assert all_targets["details"]["ASUS-GEI"]["slot"] == "laptop"
    assert all_targets["source"] == "fixture://systems"
    assert all_targets["fingerprint"].startswith("sha256:")
    grouped = rc.resolve_targets(
        "grouped", targets=["WORKSTATION-LG", "ASUS-GEI"],
        registry_snapshot=REGISTRY,
    )
    assert grouped["systems"] == ["ASUS-GEI", "WORKSTATION-LG"]
    with pytest.raises(rc.RoutingContractError, match="unknown grouped"):
        rc.resolve_targets("grouped", targets=["INVENTED"], registry_snapshot=REGISTRY)


def test_writer_creates_one_transfer_contract_with_primary_owner_and_ledger(tmp_path):
    path = make_contract(
        tmp_path, target_kind="exact", target="WORKSTATION-LG",
        ticket_kind="transfer",
    )
    assert path.name == "T-20260822-123456789.to-WORKSTATION-LG.txt"
    view = rc.load_contract(path)
    assert view.ticket_kind == "transfer"
    assert view.fields["PRIMARY_TICKET"] == "T-20260822-100000000"
    assert view.fields["ORIGINAL_OWNER"] == "ASUS-GEI"
    assert [row["system"] for row in view.ledger] == ["WORKSTATION-LG"]
    assert rc.contract_errors(path, now="2026-08-22T09:00:00Z") == []

    all_path = ticket_writer.create_routed_ticket(
        "All systems", "One circulating contract.", tickets_dir=tmp_path / "all",
        registry_snapshot=REGISTRY, ticket_kind="fork", target_kind="all",
        primary_ticket="T-20260822-100000000", original_owner="ASUS-GEI",
        receipt_to="T-20260822-100000000", today="2026-08-22", rng=FixedRandom(),
    )
    assert Path(all_path).name.endswith(".to-all.txt")
    assert len(rc.load_contract(all_path).ledger) == 2

    cli_root = tmp_path / "cli"
    registry_path = tmp_path / "systems.json"
    registry_path.write_text(json.dumps(REGISTRY), encoding="utf-8")
    assert ticket_writer._cli([
        "--title", "CLI transfer", "--body", "Targeted through public CLI",
        "--tickets-dir", str(cli_root), "--ticket-kind", "transfer",
        "--target-kind", "exact", "--target", "WORKSTATION-LG",
        "--systems-registry", str(registry_path),
        "--primary-ticket", "T-20260822-100000000",
        "--original-owner", "ASUS-GEI", "--receipt-to", "T-20260822-100000000",
    ]) == 0
    assert len(list((cli_root / "INBOX").glob("*.to-WORKSTATION-LG.txt"))) == 1

    request_root = tmp_path / "idempotent"
    kwargs = dict(
        tickets_dir=request_root, registry_snapshot=REGISTRY,
        ticket_kind="fork", target_kind="grouped",
        targets=["ASUS-GEI", "WORKSTATION-LG"],
        primary_ticket="T-20260822-100000000", original_owner="ASUS-GEI",
        receipt_to="T-20260822-100000000", idempotency_key="gap:req-42",
        today="2026-08-22", rng=FixedRandom(),
    )
    first = ticket_writer.create_routed_ticket("Retry safe", "Same request", **kwargs)
    second = ticket_writer.create_routed_ticket("Retry safe", "Same request", **kwargs)
    assert first == second
    assert len(list((request_root / "INBOX").glob("*.txt"))) == 1
    with pytest.raises(ValueError, match="different routed request"):
        ticket_writer.create_routed_ticket("Retry safe", "Changed body", **kwargs)


def test_wrong_or_done_target_cannot_claim(tmp_path):
    path = make_contract(
        tmp_path, target_kind="exact", target="WORKSTATION-LG", ticket_kind="transfer"
    )
    with pytest.raises(rc.ClaimDeniedError, match="outside"):
        rc.claim_contract(path, host="ASUS-GEI", actor="codex", now="2026-08-22T09:00:00Z")
    claimed = rc.claim_contract(
        path, host="WORKSTATION-LG", actor="codex", now="2026-08-22T09:00:00Z"
    )
    rc.record_receipt(claimed, host="WORKSTATION-LG", receipt=receipt(
        "WORKSTATION-LG", "sig-one"
    ))
    released = rc.release_contract(claimed, host="WORKSTATION-LG")
    with pytest.raises(rc.ClaimDeniedError, match="done"):
        rc.claim_contract(released, host="WORKSTATION-LG", actor="again")


def test_claim_release_preserves_routing_suffix_and_rejects_double_claim(tmp_path):
    path = make_contract(tmp_path, via="claude")
    claimed = rc.claim_contract(
        path, host="ASUS-GEI", actor="claude@ASUS", runner="claude",
        resolver=resolver, now="2026-08-22T09:00:00Z",
    )
    assert claimed.name.endswith(".to-grouped.via-claude.claim-ASUS-GEI.txt")
    with pytest.raises(rc.ClaimDeniedError, match="already"):
        rc.claim_contract(claimed, host="WORKSTATION-LG", actor="other")
    released = rc.release_contract(claimed, host="ASUS-GEI")
    assert released.name.endswith(".to-grouped.via-claude.txt")


def test_crash_recovery_waits_for_lease_and_preserves_signatures(tmp_path):
    path = make_contract(tmp_path)
    claimed = rc.claim_contract(
        path, host="ASUS-GEI", actor="worker", now="2026-08-22T09:00:00Z",
        lease_seconds=60,
    )
    with pytest.raises(rc.ClaimDeniedError, match="not expired"):
        rc.recover_expired_claim(claimed, now="2026-08-22T09:00:30Z")
    recovered = rc.recover_expired_claim(claimed, now="2026-08-22T09:01:01Z")
    assert ".claim-" not in recovered.name
    assert rc.load_contract(recovered).ledger[0]["status"] == "pending"


def test_partial_receipts_remain_open_and_only_last_target_completes(tmp_path):
    path = make_contract(tmp_path)
    first = rc.claim_contract(path, host="ASUS-GEI", actor="claude")
    assert rc.record_receipt(first, host="ASUS-GEI", receipt=receipt("ASUS-GEI", "sig-a"))
    assert not rc.completion_ready(rc.load_contract(first))
    with pytest.raises(rc.ClaimDeniedError, match="not every"):
        rc.complete_contract(first, host="ASUS-GEI", solved_dir=tmp_path / "SOLVED")
    open_path = rc.release_contract(first, host="ASUS-GEI")
    second = rc.claim_contract(open_path, host="WORKSTATION-LG", actor="claude")
    rc.record_receipt(second, host="WORKSTATION-LG", receipt=receipt(
        "WORKSTATION-LG", "sig-b"
    ))
    solved = rc.complete_contract(second, host="WORKSTATION-LG", solved_dir=tmp_path / "SOLVED")
    assert solved.parent.name == "SOLVED"
    assert ".claim-" not in solved.name
    assert rc.completion_ready(rc.load_contract(solved))

    blocked_path = make_contract(tmp_path / "blocked")
    blocked_claim = rc.claim_contract(blocked_path, host="ASUS-GEI", actor="claude")
    rc.record_receipt(
        blocked_claim, host="ASUS-GEI",
        receipt=receipt("ASUS-GEI", "sig-blocked", status="blocked"),
    )
    circulating = rc.release_contract(blocked_claim, host="ASUS-GEI")
    next_target = rc.claim_contract(circulating, host="WORKSTATION-LG", actor="claude")
    assert rc.load_contract(next_target).ledger[0]["status"] == "blocked"


def test_receipt_retry_is_idempotent_but_signature_collision_fails_closed(tmp_path):
    path = make_contract(tmp_path)
    claimed = rc.claim_contract(path, host="ASUS-GEI", actor="claude")
    payload = receipt("ASUS-GEI", "sig-a")
    assert rc.record_receipt(claimed, host="ASUS-GEI", receipt=payload)
    assert not rc.record_receipt(claimed, host="ASUS-GEI", receipt=payload)
    altered = dict(payload, evidence="receipt://different")
    with pytest.raises(rc.ReceiptConflictError, match="signature"):
        rc.record_receipt(claimed, host="ASUS-GEI", receipt=altered)


def test_transport_state_cannot_become_domain_done(tmp_path):
    path = make_contract(tmp_path)
    claimed = rc.claim_contract(path, host="ASUS-GEI", actor="claude")
    with pytest.raises(rc.ReceiptConflictError, match="transport state"):
        rc.record_receipt(
            claimed, host="ASUS-GEI",
            receipt=receipt("ASUS-GEI", "sig-delivered", status="delivered"),
        )


def test_binding_ttl_default_override_and_never():
    assert rc.binding_expires("2026-08-22T00:00:00Z") == "2026-08-29T00:00:00Z"
    assert rc.binding_expires("2026-08-22T00:00:00Z", 2) == "2026-08-24T00:00:00Z"
    assert rc.binding_expires("2026-08-22T00:00:00Z", "never") == "never"


def test_expired_binding_is_removed_only_before_next_successful_claim(tmp_path):
    path = make_contract(tmp_path, via="claude", binding_ttl=1)
    claimed = rc.claim_contract(
        path, host="ASUS-GEI", actor="codex-after-expiry", runner="codex",
        resolver=resolver, now="2026-08-24T00:00:00Z",
    )
    view = rc.load_contract(claimed)
    assert view.fields["BINDING_STATE"] == "expired-unbound"
    assert not view.fields["MODEL_SELECTOR"]
    assert ".via-" not in claimed.name
    assert view.ledger[0]["status"] == "claimed"
    assert all(row["status"] != "done" for row in view.ledger)

    proactive = make_contract(tmp_path / "proactive", via="claude", binding_ttl=1)
    normalized = rc.normalize_expired_binding(
        proactive, now="2026-08-24T00:00:00Z"
    )
    assert ".via-" not in normalized.name
    proactive_view = rc.load_contract(normalized)
    assert proactive_view.fields["BINDING_STATE"] == "expired-unbound"
    assert all(row["status"] == "pending" for row in proactive_view.ledger)


def test_binding_expiry_during_active_claim_does_not_change_that_claim(tmp_path):
    path = make_contract(tmp_path, via="claude", binding_ttl=1)
    claimed = rc.claim_contract(
        path, host="ASUS-GEI", actor="claude", runner="claude",
        resolver=resolver, now="2026-08-22T09:00:00Z",
    )
    assert ".via-claude.claim-ASUS-GEI" in claimed.name
    released = rc.release_contract(claimed, host="ASUS-GEI", now="2026-08-24T00:00:00Z")
    next_claim = rc.claim_contract(
        released, host="WORKSTATION-LG", actor="codex", runner="codex",
        resolver=resolver, now="2026-08-24T00:00:01Z",
    )
    assert ".via-" not in next_claim.name


def test_required_binding_fails_closed_and_preferred_fallback_is_evidenced(tmp_path):
    required = make_contract(tmp_path / "required", via="claude")
    with pytest.raises(rc.ClaimDeniedError):
        rc.claim_contract(
            required, host="ASUS-GEI", actor="codex", runner="codex",
            resolver=resolver, now="2026-08-22T09:00:00Z",
        )
    preferred = make_contract(
        tmp_path / "preferred", via="claude", binding_mode="preferred"
    )
    claimed = rc.claim_contract(
        preferred, host="ASUS-GEI", actor="codex", runner="codex",
        resolver=resolver, now="2026-08-22T09:00:00Z",
    )
    payload = receipt("ASUS-GEI", "sig-fallback", runner="codex")
    with pytest.raises(rc.ReceiptConflictError, match="fallback requires"):
        rc.record_receipt(claimed, host="ASUS-GEI", receipt=payload)
    payload["fallback_reason"] = "preferred runner unavailable"
    assert rc.record_receipt(claimed, host="ASUS-GEI", receipt=payload)

    exact = make_contract(tmp_path / "exact", via="openai-gpt-5.6-sol")
    exact_claim = rc.claim_contract(
        exact, host="ASUS-GEI", actor="codex", runner="codex",
        resolver=resolver, now="2026-08-22T09:00:00Z",
    )
    wrong_model = receipt("ASUS-GEI", "sig-exact", runner="codex")
    with pytest.raises(rc.ReceiptConflictError, match="exact model"):
        rc.record_receipt(exact_claim, host="ASUS-GEI", receipt=wrong_model)
    wrong_model["actual_model"] = "gpt-5.6-sol"
    assert rc.record_receipt(exact_claim, host="ASUS-GEI", receipt=wrong_model)


def test_registry_outage_stays_unresolved_then_recovers_without_substitution(tmp_path):
    def outage(_selector, runner=None):
        raise RuntimeError("provider detail must not leak")

    path = ticket_writer.create_routed_ticket(
        "Unresolved", "Wait for registry.", tickets_dir=tmp_path,
        registry_snapshot=REGISTRY, ticket_kind="transfer", target_kind="exact",
        target="WORKSTATION-LG", via="claude-opus-5",
        primary_ticket="T-20260822-100000000", original_owner="ASUS-GEI",
        receipt_to="T-20260822-100000000", resolver=outage,
        today="2026-08-22", rng=FixedRandom(),
    )
    view = rc.load_contract(path)
    assert view.fields["BINDING_STATE"] == "unresolved"
    assert view.resolution["reason"] == "registry-error:RuntimeError"
    assert "provider detail" not in json.dumps(view.resolution)
    with pytest.raises(rc.ClaimDeniedError):
        rc.claim_contract(
            path, host="WORKSTATION-LG", actor="claude", runner="claude",
            resolver=outage, now="2026-08-22T09:00:00Z",
        )

    def refreshed(selector, runner=None):
        data = resolver("claude-opus", runner=runner)
        data.update(canonical_selector="claude-opus-5", requested_selector=selector)
        return data

    assert rc.refresh_resolution(path, resolver=refreshed)
    assert rc.load_contract(path).resolution["canonical_selector"] == "claude-opus-5"


def test_execution_matrix_is_per_target_and_runner_completion_is_not_global(tmp_path):
    matrix = {
        "ASUS-GEI": {"selector": "claude", "variant": "windows-local"},
        "WORKSTATION-LG": {"selector": "agy", "variant": "workstation-local"},
    }
    path = make_contract(tmp_path, via="mixed", execution_matrix=matrix)
    view = rc.load_contract(path)
    assert set(view.execution_matrix) == set(view.target_systems)
    first = rc.claim_contract(
        path, host="ASUS-GEI", actor="claude", runner="claude", resolver=resolver
    )
    rc.record_receipt(first, host="ASUS-GEI", receipt=receipt("ASUS-GEI", "sig-matrix"))
    assert not rc.completion_ready(rc.load_contract(first))


def test_audit_reports_target_claim_ledger_and_premature_solved_errors(tmp_path):
    path = make_contract(tmp_path)
    root_path = tmp_path / path.name
    path.rename(root_path)
    assert str(root_path) not in ticket_audit.audit(tmp_path)["claimed_in_root"]
    path = root_path
    bad_name = path.with_name(path.name.replace(".txt", ".claim-INVENTED.txt"))
    path.rename(bad_name)
    report = ticket_audit.audit(tmp_path)
    errors = report["routing_errors"][str(bad_name)]
    assert any("claim" in error for error in errors)
    path = bad_name
    text = path.read_text(encoding="utf-8").replace("CLAIMED_BY_HOST: ", "CLAIMED_BY_HOST: INVENTED")
    fields = rc.parse_fields(text)
    ledger = json.loads(fields["SYSTEM_LEDGER"])
    ledger.append(dict(ledger[0]))
    text = rc.update_fields(text, {"SYSTEM_LEDGER": ledger, "PRIMARY_TICKET": ""})
    path.write_text(text, encoding="utf-8")
    solved = tmp_path / "SOLVED" / path.name
    solved.parent.mkdir()
    path.rename(solved)
    errors = ticket_audit.audit(tmp_path)["routing_errors"][str(solved)]
    assert any("outside the target" in error for error in errors)
    assert any("exactly one" in error for error in errors)
    assert any("PRIMARY_TICKET" in error for error in errors)
    assert any("SOLVED" in error for error in errors)


def test_route_intent_boundary_is_stable_and_contains_no_transport_machine(tmp_path):
    view = rc.load_contract(make_contract(tmp_path))
    first = rc.build_route_intent(view)
    second = rc.build_route_intent(view)
    assert first == second
    assert set(first) == {
        "route_intent", "ticket_id", "target_snapshot", "receipt_to", "idempotency_key"
    }
    serialized = json.dumps(first)
    for forbidden in ("queued", "delivered", "retry", "inbox", "outbox", "drop-zone"):
        assert forbidden not in serialized.lower()


def test_v2_release_helper_and_legacy_multi_host_release_stay_distinct(tmp_path):
    legacy = tmp_path / "ACTIONABLE" / "T-20260731-123456789.LAPTOP-WORKSTATION-LG.txt"
    legacy.parent.mkdir()
    legacy.write_text("legacy", encoding="utf-8")
    assert ticket_mover.claim_suffix(legacy.name) == "LAPTOP-WORKSTATION-LG"
    assert ticket_mover.release_claims(tmp_path, host="LAPTOP") == []
    assert legacy.exists()

    routed = make_contract(tmp_path / "v2")
    claimed = rc.claim_contract(routed, host="ASUS-GEI", actor="worker")
    released = ticket_mover.release_claim(claimed)
    assert ".to-grouped" in released.name and ".claim-" not in released.name
