from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from usd1_monitor.engine.redemption_rules import (
    RecoveryDecision,
    RedemptionClassification,
    apply_recovery,
    classify_redemption,
)
from usd1_monitor.models import RiskLevel


@pytest.mark.parametrize(
    ("text", "usd1_specific", "expected"),
    [
        ("USD1 redemptions are suspended", True, RiskLevel.RED),
        ("USD1 redemption settlement is delayed", True, RiskLevel.YELLOW),
        ("Stablecoins settlement service disruption", False, RiskLevel.YELLOW),
        ("USD1 redemptions are not suspended", True, RiskLevel.GREEN),
        (
            "BitGo may suspend redemptions under these terms",
            True,
            RiskLevel.GREEN,
        ),
    ],
)
def test_required_redemption_classification_examples(
    text: str,
    usd1_specific: bool,
    expected: RiskLevel,
) -> None:
    assert classify_redemption(text, usd1_specific=usd1_specific).level is expected


def test_redemption_rule_values_are_frozen() -> None:
    classification = RedemptionClassification(
        RiskLevel.RED,
        "USD1 官方赎回已暂停",
        "USD1 redemptions are suspended",
        True,
    )
    recovery = RecoveryDecision(RiskLevel.YELLOW, 1)

    with pytest.raises(FrozenInstanceError):
        classification.level = RiskLevel.GREEN  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        recovery.clear_checks = 2  # type: ignore[misc]


@pytest.mark.parametrize(
    "text",
    [
        "USD1 redemptions are suspended.",
        "Redemption for USD1 is CLOSED!",
        "USD1 redemption: frozen",
        "USD1 redeem service is unavailable",
    ],
)
def test_explicit_usd1_redemption_stop_is_red(text: str) -> None:
    result = classify_redemption(text, usd1_specific=True)

    assert result.level is RiskLevel.RED
    assert result.summary == "USD1 官方赎回已暂停或不可用"
    assert result.matched_text
    assert result.confirmed_usd1 is True


@pytest.mark.parametrize(
    "text",
    [
        "USD1 redemption is delayed due to a bank processing issue.",
        "USD1 redemptions are subject to a temporary limit.",
        "A settlement delay is affecting USD1 redemption.",
        "USD1 redemption has a banking disruption.",
    ],
)
def test_explicit_usd1_redemption_problem_is_yellow(text: str) -> None:
    result = classify_redemption(text, usd1_specific=True)

    assert result.level is RiskLevel.YELLOW
    assert result.summary == "USD1 官方赎回可能延迟或受限"
    assert result.matched_text
    assert result.confirmed_usd1 is True


@pytest.mark.parametrize(
    ("text", "usd1_specific"),
    [
        (
            "Investigating reports USD1 redemption may be delayed.",
            True,
        ),
        (
            "USD1 redemption may experience delays during an incident.",
            True,
        ),
        (
            "Stablecoins may be experiencing a partial outage.",
            False,
        ),
        ("USD1 redemption may be delayed.", True),
        ("Stablecoins could have a partial outage.", False),
    ],
)
def test_tentative_active_incident_wording_is_yellow(
    text: str,
    usd1_specific: bool,
) -> None:
    assert classify_redemption(text, usd1_specific).level is RiskLevel.YELLOW


@pytest.mark.parametrize(
    "text",
    [
        "We are delaying USD1 redemptions.",
        "BitGo has limited USD1 redemption requests.",
        "BitGo restricted USD1 redemptions.",
        "Settlement delays are affecting USD1 redemption.",
    ],
)
def test_active_yellow_predicates_bind_a_usd1_object(text: str) -> None:
    assert classify_redemption(text, usd1_specific=True).level is RiskLevel.YELLOW


@pytest.mark.parametrize(
    "text",
    [
        "We are delaying USDC redemptions.",
        "BitGo restricted USDC redemptions.",
    ],
)
def test_active_yellow_predicates_do_not_cross_assets(text: str) -> None:
    assert classify_redemption(text, usd1_specific=True).level is RiskLevel.GREEN


@pytest.mark.parametrize(
    "text",
    [
        "We delayed USD1 redemptions last year.",
        "BitGo limited USD1 redemption requests in 2025.",
        "BitGo restricted USD1 redemptions last year.",
    ],
)
def test_active_yellow_object_with_past_time_is_historical(text: str) -> None:
    assert classify_redemption(text, usd1_specific=True).level is RiskLevel.GREEN


def test_active_yellow_current_marker_remains_a_risk() -> None:
    assert classify_redemption(
        "We are currently delaying USD1 redemptions.",
        usd1_specific=True,
    ).level is RiskLevel.YELLOW


@pytest.mark.parametrize(
    "text",
    [
        "Ongoing maintenance may delay USD1 redemptions.",
        "The active incident could restrict USD1 redemptions.",
    ],
)
def test_dynamic_context_modal_with_usd1_object_is_yellow(text: str) -> None:
    assert classify_redemption(text, usd1_specific=True).level is RiskLevel.YELLOW


def test_sentence_and_list_item_boundaries_prevent_keyword_pileup() -> None:
    text = """USD1 remains available.
    - Redemption services for another asset are suspended.
    - Bank settlement maintenance is in progress."""

    result = classify_redemption(text, usd1_specific=True)

    assert result == RedemptionClassification(
        RiskLevel.GREEN,
        "未发现官方限制",
        None,
        False,
    )


@pytest.mark.parametrize(
    "text",
    [
        "USD1 is supported. Redemption services are suspended.",
        "USD1; redemption is unavailable",
        "USD1\n- redemption is frozen",
    ],
)
def test_usd1_and_restriction_must_be_in_same_semantic_item(text: str) -> None:
    assert classify_redemption(text, usd1_specific=True).level is RiskLevel.GREEN


@pytest.mark.parametrize(
    "text",
    [
        "USD1 redemption is not suspended.",
        "No USD1 redemption is suspended.",
        "USD1 redemption continues without delay.",
        "The previously suspended USD1 redemption incident is resolved.",
        "USD1 redemption is operational; no restrictions apply.",
        "USD1 redemption may be suspended under the terms.",
        "We can limit USD1 redemption.",
        "We reserve the right to freeze USD1 redemptions.",
        "The terms allow suspension of USD1 redemption.",
    ],
)
def test_negated_resolved_and_conditional_wording_is_not_a_risk(text: str) -> None:
    result = classify_redemption(text, usd1_specific=True)

    assert result.level is RiskLevel.GREEN
    assert result.summary == "未发现官方限制"
    assert "赎回正常" not in result.summary


@pytest.mark.parametrize(
    "text",
    [
        "Historically, USD1 redemption was unavailable.",
        "Previously USD1 redemption was suspended.",
        "Formerly, USD1 redemption was frozen.",
        "USD1 redemption will not be suspended.",
        "USD1 redemption won't be suspended.",
        "USD1 redemption isn't suspended.",
        "USD1 redemptions aren't suspended.",
        "USD1 redemption is never suspended.",
    ],
)
def test_historical_and_extended_negation_are_clear(text: str) -> None:
    assert classify_redemption(text, usd1_specific=True).level is RiskLevel.GREEN


@pytest.mark.parametrize(
    "text",
    [
        (
            "Previously USD1 redemption was unavailable, but USD1 redemption "
            "is now suspended."
        ),
        (
            "Historically USD1 redemption was frozen whereas USD1 redemption "
            "remains unavailable."
        ),
        "Previously USD1 redemption was unavailable but remains suspended.",
        "Formerly USD1 redemption was frozen but is now unavailable.",
    ],
)
def test_current_restriction_wins_over_historical_wording(text: str) -> None:
    assert classify_redemption(text, usd1_specific=True).level is RiskLevel.RED


@pytest.mark.parametrize(
    "text",
    [
        (
            "Previously announced maintenance is complete, "
            "USD1 redemption is suspended."
        ),
        (
            "The resolved Wallets incident is unrelated, and "
            "USD1 redemption is unavailable."
        ),
    ],
)
def test_unrelated_history_or_resolution_does_not_hide_current_risk(
    text: str,
) -> None:
    assert classify_redemption(text, usd1_specific=True).level is RiskLevel.RED


def test_resolved_incident_summary_is_historical_without_current_marker() -> None:
    assert classify_redemption(
        "Resolved incident: USD1 redemption was unavailable.",
        usd1_specific=True,
    ).level is RiskLevel.GREEN


@pytest.mark.parametrize(
    ("text", "usd1_specific"),
    [
        ("Resolved incident, USD1 redemption was unavailable.", True),
        ("Resolved incident, Stablecoins had a partial outage.", False),
        (
            "Historically, during maintenance, "
            "USD1 redemption was unavailable.",
            True,
        ),
    ],
)
def test_leading_historical_summary_applies_to_later_past_tense_event(
    text: str,
    usd1_specific: bool,
) -> None:
    assert classify_redemption(text, usd1_specific).level is RiskLevel.GREEN


@pytest.mark.parametrize(
    "text",
    [
        (
            "Historically, USD1 redemption was unavailable and "
            "is now suspended."
        ),
        (
            "Previously, USD1 redemption was delayed and "
            "is now unavailable."
        ),
        (
            "Resolved incident, USD1 redemption was unavailable and "
            "is now suspended again."
        ),
    ],
)
def test_leading_history_does_not_hide_later_marked_current_risk(
    text: str,
) -> None:
    assert classify_redemption(text, usd1_specific=True).level is RiskLevel.RED


@pytest.mark.parametrize(
    ("text", "usd1_specific"),
    [
        (
            "Historically active maintenance made "
            "USD1 redemption unavailable.",
            True,
        ),
        (
            "Historically ongoing maintenance delayed USD1 redemptions.",
            True,
        ),
        (
            "Previously new banking rules restricted USD1 redemptions.",
            True,
        ),
        (
            "Formerly active incident made Stablecoins unavailable.",
            False,
        ),
    ],
)
def test_descriptive_active_new_ongoing_words_remain_historical(
    text: str,
    usd1_specific: bool,
) -> None:
    assert classify_redemption(text, usd1_specific).level is RiskLevel.GREEN


@pytest.mark.parametrize(
    "text",
    [
        "Resolved incident, USD1 redemption unavailable.",
        "Resolved incident, USD1 redemptions experienced delays.",
    ],
)
def test_resolved_compact_summary_defaults_to_historical(text: str) -> None:
    assert classify_redemption(text, usd1_specific=True).level is RiskLevel.GREEN


@pytest.mark.parametrize(
    "text",
    [
        "Resolved incident, USD1 redemption is unavailable.",
        "Resolved incident, USD1 redemption unavailable again.",
    ],
)
def test_resolved_summary_does_not_hide_explicit_current_context(
    text: str,
) -> None:
    assert classify_redemption(text, usd1_specific=True).level is RiskLevel.RED


@pytest.mark.parametrize(
    "text",
    [
        "USD1 redemptions were suspended last year.",
        "USD1 redemption was unavailable last month.",
        "USD1 redemption was frozen last week.",
        "In 2025, USD1 redemption was suspended.",
        "USD1 redemption was unavailable in 2024.",
        "USD1 redemption suspension occurred last year.",
    ],
)
def test_explicit_past_time_restrictions_are_historical(text: str) -> None:
    assert classify_redemption(text, usd1_specific=True).level is RiskLevel.GREEN


@pytest.mark.parametrize(
    "text",
    [
        "USD1 redemption was unavailable but is now operational.",
        "USD1 redemption was suspended but is currently resolved.",
        "USD1 redemption was frozen but has now been restored.",
    ],
)
def test_later_current_clear_status_overrides_earlier_same_subject_risk(
    text: str,
) -> None:
    assert classify_redemption(text, usd1_specific=True).level is RiskLevel.GREEN


def test_later_explicit_usd1_clear_sentence_overrides_earlier_risk() -> None:
    assert classify_redemption(
        "USD1 redemption was unavailable. USD1 redemption is now operational.",
        usd1_specific=True,
    ).level is RiskLevel.GREEN


def test_current_risk_after_clear_sentence_still_wins() -> None:
    assert classify_redemption(
        "USD1 redemption is operational. USD1 redemption is now suspended.",
        usd1_specific=True,
    ).level is RiskLevel.RED


@pytest.mark.parametrize(
    "text",
    [
        "USD1 redemption cannot be suspended.",
        "USD1 redemption can't be suspended.",
        "USD1 redemption is not delayed or limited.",
        "USD1 redemption is neither delayed nor limited.",
    ],
)
def test_negation_covers_cannot_and_coordinated_predicates(text: str) -> None:
    assert classify_redemption(text, usd1_specific=True).level is RiskLevel.GREEN


def test_risk_sentence_is_not_cancelled_by_unrelated_normal_sentence() -> None:
    result = classify_redemption(
        "Other services are operational. USD1 redemption is unavailable!",
        usd1_specific=True,
    )

    assert result.level is RiskLevel.RED
    assert result.matched_text == "USD1 redemption is unavailable"


def test_risk_is_not_cancelled_by_unrelated_no_timeframe_wording() -> None:
    result = classify_redemption(
        "USD1 redemption is suspended with no estimated reopening time.",
        usd1_specific=True,
    )

    assert result.level is RiskLevel.RED


def test_risk_is_not_cancelled_by_unrelated_operational_service() -> None:
    result = classify_redemption(
        "USD1 redemption is suspended while account service is operational.",
        usd1_specific=True,
    )

    assert result.level is RiskLevel.RED


@pytest.mark.parametrize(
    "text",
    [
        "Stablecoins service: partial outage, investigating.",
        "Settlement service has an active incident and degraded performance.",
    ],
)
def test_generic_active_bitgo_status_is_unconfirmed_yellow(text: str) -> None:
    result = classify_redemption(text, usd1_specific=False)

    assert result.level is RiskLevel.YELLOW
    assert result.summary == "可能影响 USD1，尚未确认"
    assert result.matched_text
    assert result.confirmed_usd1 is False


def test_generic_incident_is_not_cancelled_by_an_operational_component() -> None:
    result = classify_redemption(
        "Stablecoins service is degraded while Wallets is operational.",
        usd1_specific=False,
    )

    assert result.level is RiskLevel.YELLOW


@pytest.mark.parametrize(
    ("text", "usd1_specific"),
    [
        (
            "Stablecoins is operational while Wallets has a partial outage.",
            False,
        ),
        (
            "Stablecoins is operational but Wallets has a partial outage.",
            False,
        ),
        (
            "Wallets has a partial outage whereas Settlement is operational.",
            False,
        ),
        (
            "Although Wallets is unavailable, Stablecoins is operational.",
            False,
        ),
        (
            "USD1 redemption is operational while Wallets is unavailable.",
            True,
        ),
        (
            "Stablecoins is operational and Wallets has a partial outage.",
            False,
        ),
        (
            "USD1 redemption is operational and Wallets is unavailable.",
            True,
        ),
        (
            "Stablecoins is operational and the Wallets component is unavailable.",
            False,
        ),
        (
            "Stablecoins is operational, with Wallets unavailable.",
            False,
        ),
    ],
)
def test_component_failure_predicate_must_share_its_clause_with_target(
    text: str,
    usd1_specific: bool,
) -> None:
    assert classify_redemption(text, usd1_specific).level is RiskLevel.GREEN


def test_and_inside_one_subject_phrase_does_not_break_usd1_context() -> None:
    assert classify_redemption(
        "USD1 deposits and redemptions are suspended.",
        usd1_specific=True,
    ).level is RiskLevel.RED


@pytest.mark.parametrize(
    "text",
    [
        "USD1 redemption and Wallets are unavailable.",
        "Wallets and USD1 redemption are unavailable.",
        "USD1 redemption, Wallets, and Settlement are unavailable.",
    ],
)
def test_shared_unavailable_predicate_includes_usd1_redemption(text: str) -> None:
    assert classify_redemption(text, usd1_specific=True).level is RiskLevel.RED


@pytest.mark.parametrize(
    "text",
    [
        "USD1 redemption and Wallets are both unavailable.",
        "USD1 redemption and Settlement remain unavailable.",
    ],
)
def test_shared_predicate_modifiers_keep_usd1_subject(text: str) -> None:
    assert classify_redemption(text, usd1_specific=True).level is RiskLevel.RED


@pytest.mark.parametrize(
    "text",
    [
        "USD1 redemption is delayed or unavailable.",
        "USD1 redemption is limited or suspended.",
    ],
)
def test_affirmative_or_inherits_the_same_redemption_entity(text: str) -> None:
    assert classify_redemption(text, usd1_specific=True).level is RiskLevel.RED


@pytest.mark.parametrize(
    "text",
    [
        "USD1 redemption is unavailable or delayed.",
        "USD1 redemption is delayed or unavailable.",
        "USD1 redemption is unavailable or operational.",
        "USD1 redemption is operational or unavailable.",
    ],
)
def test_or_group_uses_the_most_severe_state_regardless_of_order(
    text: str,
) -> None:
    assert classify_redemption(text, usd1_specific=True).level is RiskLevel.RED


@pytest.mark.parametrize(
    "text",
    [
        "USD1 redemption is unavailable and operational.",
        "USD1 redemption is unavailable but operational.",
    ],
)
def test_non_temporal_clear_does_not_override_parallel_risk(text: str) -> None:
    assert classify_redemption(text, usd1_specific=True).level is RiskLevel.RED


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "USD1 deposits and USDC redemptions are suspended.",
            RiskLevel.GREEN,
        ),
        (
            "USDC deposits and USD1 redemptions are suspended.",
            RiskLevel.RED,
        ),
    ],
)
def test_usd1_must_modify_the_redemption_subject(
    text: str,
    expected: RiskLevel,
) -> None:
    assert classify_redemption(text, usd1_specific=True).level is expected


@pytest.mark.parametrize(
    "text",
    [
        "USD1's redemption service is unavailable.",
        "USD1 deposits, withdrawals, and redemptions are suspended.",
        "We have suspended USD1 redemptions.",
    ],
)
def test_common_usd1_redemption_subjects_and_voice_are_supported(
    text: str,
) -> None:
    assert classify_redemption(text, usd1_specific=True).level is RiskLevel.RED


@pytest.mark.parametrize(
    "text",
    [
        "We suspended USD1 redemptions last year.",
        "We suspended USD1 redemptions in 2025.",
    ],
)
def test_active_voice_past_time_after_object_is_historical(text: str) -> None:
    assert classify_redemption(text, usd1_specific=True).level is RiskLevel.GREEN


def test_active_voice_current_time_remains_a_risk() -> None:
    assert classify_redemption(
        "We have currently suspended USD1 redemptions.",
        usd1_specific=True,
    ).level is RiskLevel.RED


@pytest.mark.parametrize(
    "text",
    [
        "USD1 redemption and Settlement processing are delayed.",
        "Settlement processing and USD1 redemption are delayed.",
        "USD1 redemption, Wallets, and Settlement processing are delayed.",
    ],
)
def test_shared_processing_delay_includes_usd1_redemption(text: str) -> None:
    assert classify_redemption(text, usd1_specific=True).level is RiskLevel.YELLOW


def test_support_page_availability_does_not_clear_redemption_suspension() -> None:
    result = classify_redemption(
        "USD1 redemption is suspended. "
        "USD1 redemption support page is now available.",
        usd1_specific=True,
    )

    assert result.level is RiskLevel.RED


def test_information_page_clear_in_same_sentence_does_not_clear_channel() -> None:
    result = classify_redemption(
        "USD1 redemption is suspended and "
        "USD1 redemption support page is available.",
        usd1_specific=True,
    )

    assert result.level is RiskLevel.RED


def test_unrelated_service_clear_does_not_clear_redemption_channel() -> None:
    result = classify_redemption(
        "USD1 redemption is suspended and account service is operational.",
        usd1_specific=True,
    )

    assert result.level is RiskLevel.RED


@pytest.mark.parametrize(
    "text",
    [
        "USD1 redemption support page is unavailable.",
        "USD1 redemption status page is unavailable.",
        "USD1 redemption documentation site is unavailable.",
        "USD1 redemption FAQ page is unavailable.",
        "USD1's redemption status page is unavailable.",
    ],
)
def test_redemption_information_pages_are_not_the_redemption_channel(
    text: str,
) -> None:
    assert classify_redemption(text, usd1_specific=True).level is RiskLevel.GREEN


@pytest.mark.parametrize(
    "text",
    [
        "USD1 redemption is operational but delayed.",
        "USD1 redemption is operational but temporarily delayed.",
    ],
)
def test_elided_later_delay_updates_same_redemption_entity(text: str) -> None:
    assert classify_redemption(text, usd1_specific=True).level is RiskLevel.YELLOW


def test_elided_later_clear_updates_same_redemption_entity() -> None:
    assert classify_redemption(
        "USD1 redemption was suspended and is now operational.",
        usd1_specific=True,
    ).level is RiskLevel.GREEN


@pytest.mark.parametrize(
    ("text", "usd1_specific"),
    [
        ("USD1 redemption suspension is resolved.", True),
        ("USD1 redemption was suspended, now operational.", True),
        ("Stablecoins service had a partial outage, now resolved.", False),
        ("Settlement has an active incident, now resolved.", False),
    ],
)
def test_adjacent_clear_inherits_the_same_entity(
    text: str,
    usd1_specific: bool,
) -> None:
    assert classify_redemption(text, usd1_specific).level is RiskLevel.GREEN


@pytest.mark.parametrize(
    "text",
    [
        "Settlement is unavailable while Stablecoins is operational.",
        "Stablecoins has a partial outage. Settlement is operational.",
    ],
)
def test_generic_clear_only_updates_its_own_entity(text: str) -> None:
    assert classify_redemption(text, usd1_specific=False).level is RiskLevel.YELLOW


@pytest.mark.parametrize(
    ("text", "usd1_specific", "expected"),
    [
        (
            "Wallets and account services are unavailable.",
            True,
            RiskLevel.GREEN,
        ),
        (
            "USD1 redemption support page and Wallets are unavailable.",
            True,
            RiskLevel.GREEN,
        ),
        (
            "Wallets are unavailable and Stablecoins is operational.",
            False,
            RiskLevel.GREEN,
        ),
    ],
)
def test_shared_predicates_do_not_cross_into_unrelated_subjects(
    text: str,
    usd1_specific: bool,
    expected: RiskLevel,
) -> None:
    assert classify_redemption(text, usd1_specific).level is expected


def test_static_wording_in_other_clause_does_not_hide_current_restriction() -> None:
    assert classify_redemption(
        "We may post updates while USD1 redemption is suspended.",
        usd1_specific=True,
    ).level is RiskLevel.RED


@pytest.mark.parametrize(
    "text",
    [
        "BitGo retains the right to suspend USD1 redemption.",
        "BitGo reserves the right to suspend USD1 redemption.",
        "BitGo may suspend USD1 redemption.",
        "BitGo can suspend USD1 redemption under these terms.",
        "BitGo could freeze USD1 redemption under these terms.",
        "These terms allow BitGo to suspend USD1 redemption.",
        "These terms allow a suspension of USD1 redemption.",
        "These terms permit temporary suspension of USD1 redemption.",
        "USD1 redemption is subject to suspension under these terms.",
        "USD1 redemption is subject to being suspended under these terms.",
        "USD1 redemption is subject to a possible suspension under these terms.",
    ],
)
def test_static_rights_are_not_current_restrictions(text: str) -> None:
    assert classify_redemption(text, usd1_specific=True).level is RiskLevel.GREEN


@pytest.mark.parametrize(
    "text",
    [
        "We do not plan to suspend USD1 redemptions.",
        "BitGo has no plans to suspend USD1 redemption.",
    ],
)
def test_negated_plan_to_suspend_is_not_a_current_restriction(text: str) -> None:
    assert classify_redemption(text, usd1_specific=True).level is RiskLevel.GREEN


@pytest.mark.parametrize(
    "text",
    [
        "Stablecoins service is operational.",
        "Settlement incident has been resolved.",
        "Custody service has an active incident.",
        "Stablecoins and settlement services are available.",
        "USD1 redemption is suspended.",
    ],
)
def test_generic_status_only_accepts_active_stablecoins_or_settlement_incident(
    text: str,
) -> None:
    assert classify_redemption(text, usd1_specific=False).level is RiskLevel.GREEN


@pytest.mark.parametrize("text", ["", "   ", "\n- \n"])
def test_empty_text_is_clear(text: str) -> None:
    assert classify_redemption(text, usd1_specific=True) == RedemptionClassification(
        RiskLevel.GREEN,
        "未发现官方限制",
        None,
        False,
    )


@pytest.mark.parametrize("text", [None, 1, [], {}])
def test_non_string_text_is_rejected(text: object) -> None:
    with pytest.raises(TypeError, match="text must be a string"):
        classify_redemption(text, usd1_specific=True)  # type: ignore[arg-type]


def test_usd1_specific_flag_must_be_boolean() -> None:
    with pytest.raises(TypeError, match="usd1_specific must be a bool"):
        classify_redemption("USD1 redemption is suspended", 1)  # type: ignore[arg-type]


def test_risk_recovery_requires_two_clear_checks() -> None:
    first = apply_recovery(RiskLevel.RED, RiskLevel.GREEN, 0)
    second = apply_recovery(first.level, RiskLevel.GREEN, first.clear_checks)

    assert first == RecoveryDecision(RiskLevel.RED, 1)
    assert second == RecoveryDecision(RiskLevel.GREEN, 0)


def test_retrigger_resets_recovery_progress() -> None:
    first = apply_recovery(RiskLevel.YELLOW, RiskLevel.GREEN, 0)
    retriggered = apply_recovery(first.level, RiskLevel.RED, first.clear_checks)

    assert first == RecoveryDecision(RiskLevel.YELLOW, 1)
    assert retriggered == RecoveryDecision(RiskLevel.RED, 0)


def test_custom_recovery_boundary_is_exact() -> None:
    first = apply_recovery(
        RiskLevel.YELLOW,
        RiskLevel.GREEN,
        0,
        required=1,
    )

    assert first == RecoveryDecision(RiskLevel.GREEN, 0)


@pytest.mark.parametrize(
    ("clear_checks", "required", "message"),
    [
        (-1, 2, "clear_checks"),
        (0, 0, "required"),
        (0, -1, "required"),
    ],
)
def test_recovery_counts_must_be_valid(
    clear_checks: int,
    required: int,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        apply_recovery(
            RiskLevel.YELLOW,
            RiskLevel.GREEN,
            clear_checks,
            required=required,
        )


@pytest.mark.parametrize(
    ("clear_checks", "required"),
    [
        (False, 2),
        (0.0, 2),
        (0, True),
        (0, 2.0),
    ],
)
def test_recovery_counts_require_exact_integer_types(
    clear_checks: object,
    required: object,
) -> None:
    with pytest.raises(TypeError, match="clear_checks and required must be int"):
        apply_recovery(
            RiskLevel.YELLOW,
            RiskLevel.GREEN,
            clear_checks,  # type: ignore[arg-type]
            required=required,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("previous", "current"),
    [
        (0, RiskLevel.GREEN),
        (RiskLevel.GREEN, 0),
        ("GREEN", RiskLevel.GREEN),
        (RiskLevel.GREEN, "GREEN"),
    ],
)
def test_recovery_levels_require_exact_risk_level_type(
    previous: object,
    current: object,
) -> None:
    with pytest.raises(TypeError, match="RiskLevel"):
        apply_recovery(current, previous, 0)  # type: ignore[arg-type]
