from __future__ import annotations

import re
from dataclasses import dataclass

from usd1_monitor.models import RiskLevel


@dataclass(frozen=True)
class RedemptionClassification:
    level: RiskLevel
    summary: str
    matched_text: str | None
    confirmed_usd1: bool


@dataclass(frozen=True)
class RecoveryDecision:
    level: RiskLevel
    clear_checks: int


@dataclass(frozen=True)
class _Clause:
    text: str
    inherits_previous_subject: bool


@dataclass(frozen=True)
class _StatusEvent:
    position: int
    level: RiskLevel
    entities: frozenset[str]
    matched_text: str
    follows_previous: bool
    temporal_update: bool


_SEGMENT_SEPARATOR = re.compile(
    r"([.!?;。！？；]+|\r?\n+|\b(?:while|but|whereas|although|though)\b)",
    re.IGNORECASE,
)
_LIST_PREFIX = re.compile(r"^\s*(?:[-*•]+|\d+[.)])\s*")
_USD1_REDEMPTION_SUBJECT = re.compile(
    r"\busd1(?:['’]s)?\s+"
    r"(?:(?:deposits?|withdrawals?)\s*(?:,\s*|\s+and\s+))*"
    r"(?:and\s+)?"
    r"(?:redeem\w*|redemption\w*)\b|"
    r"\b(?:redeem\w*|redemption\w*)\s+(?:for|of)\s+usd1\b",
    re.IGNORECASE,
)
_NON_CHANNEL_SUBJECT = re.compile(
    r"\busd1(?:['’]s)?\s+redemptions?\s+"
    r"(?:(?:support|status|documentation|docs?|faq|help|information)\s+)*"
    r"(?:page|site|portal|cent(?:er|re))\b",
    re.IGNORECASE,
)
_STOP = re.compile(
    r"\bsuspend\w*\b|\bsuspension\w*\b|\bclos(?:e|ed|ure)\b|"
    r"\bfroz(?:en|e)\b|"
    r"\bfreez\w*\b|\bunavailable\b",
    re.IGNORECASE,
)
_DELAY_OR_LIMIT = re.compile(
    r"\bdelay\w*\b|\blimit(?:s|ed|ing|ation|ations)?\b|"
    r"\brestrict(?:s|ed|ing|ion|ions)?\b",
    re.IGNORECASE,
)
_BANK_OR_SETTLEMENT = re.compile(r"\bbank(?:ing)?\b|\bsettlement\b", re.IGNORECASE)
_PROBLEM = re.compile(
    r"\b(?:issue|problem|incident|disruption|failure|outage)s?\b|"
    r"\bdegrad(?:ed|ation)\b|\bdelay\w*\b|\bunavailable\b",
    re.IGNORECASE,
)
_WALLETS_OR_API = re.compile(r"\bwallets?\b|\bapi\b", re.IGNORECASE)
_ACTIVE_INCIDENT = re.compile(
    r"\bactive\s+incident\b|\binvestigat(?:ing|ed|ion)\b|"
    r"\bidentified\b|\bmonitoring\b|\bdegraded\b|"
    r"\b(?:partial|major)\s+outage\b|\boutage\b|\bdisruption\b|"
    r"\bdelay\w*\b|\bdown\b|\bunavailable\b",
    re.IGNORECASE,
)
_HISTORICAL = re.compile(r"\b(?:historically|previously|formerly)\b", re.IGNORECASE)
_PAST_TIME = re.compile(
    r"\b(?:last\s+(?:year|month|week)|in\s+(?:19|20)\d{2})\b",
    re.IGNORECASE,
)
_CURRENT = re.compile(
    r"\b(?:current(?:ly)?|now|again|remain(?:s|ed)?|still)\b",
    re.IGNORECASE,
)
_RESOLVED = re.compile(r"\b(?:resolved|restored|recovered)\b", re.IGNORECASE)
_CURRENT_CLEAR = re.compile(
    r"\b(?:operational|available|resolved|restored|recovered|resumed)\b",
    re.IGNORECASE,
)
_EXPLICIT_RECOVERY = re.compile(
    r"\b(?:resolved|restored|recovered|resumed)\b",
    re.IGNORECASE,
)
_NEGATION_BEFORE = re.compile(
    r"(?:\b(?:will|would|is|are|was|were|do|does|did|have|has)\s+)?"
    r"\b(?:not|never|neither|no\s+longer)\b\s+"
    r"(?:currently\s+)?(?:be(?:ing)?\s+)?$|"
    r"\bcannot\b\s+(?:be(?:ing)?\s+)?$|"
    r"\b(?:won|isn|aren|wasn|weren|don|doesn|didn|can|couldn|wouldn|"
    r"shouldn|mustn)['’]t\b\s+(?:be(?:ing)?\s+)?$|"
    r"\bwithout\s+(?:any\s+)?$|\bno\s+$",
    re.IGNORECASE,
)
_COORDINATED_NEGATION_BEFORE = re.compile(
    r"\b(?:not|neither)\b(?:\W+\w+){0,5}\W+\b(?:or|nor)\b\W*$",
    re.IGNORECASE,
)
_DIRECT_NO = re.compile(
    r"^\s*no\s+(?:usd1\s+)?redemptions?\b|"
    r"\bno\s+(?:active\s+)?(?:restriction|suspension|delay|limit|"
    r"issue|problem|incident|outage|disruption)s?\b",
    re.IGNORECASE,
)
_NEGATED_AFTER = re.compile(
    r"^.{0,24}\b(?:does|do|is|are)\s+not\s+"
    r"(?:apply|active|in\s+effect)\b",
    re.IGNORECASE,
)
_NEGATED_PLAN_BEFORE = re.compile(
    r"\b(?:(?:do|does)\s+not|don['’]t|doesn['’]t)\s+plan\s+to\W*$|"
    r"\b(?:has|have)\s+no\s+plans?\s+to\W*$",
    re.IGNORECASE,
)
_CONDITIONAL_BEFORE = re.compile(
    r"\b(?:retain|reserve)s?\s+the\s+right\s+to\W*$|"
    r"\bterms?\s+(?:permit|allow)\w*\b(?:\W+\w+){0,4}\W*$",
    re.IGNORECASE,
)
_MODAL_BEFORE = re.compile(
    r"\b(?:may|can|could)\b(?:\W+\w+){0,3}\W*$",
    re.IGNORECASE,
)
_STATIC_MODAL_CONTEXT = re.compile(
    r"\b(?:terms?|policy)\b|"
    r"\b(?:retain|reserve)s?\s+the\s+right\b",
    re.IGNORECASE,
)
_DYNAMIC_MODAL_CONTEXT = re.compile(
    r"\b(?:ongoing|active\s+incident|maintenance|investigat\w*|"
    r"reports?|current(?:ly)?)\b",
    re.IGNORECASE,
)
_LEADING_HISTORICAL_SUMMARY = re.compile(
    r"^\s*(?:resolved\s+incident\b|historically\b|previously\b|formerly\b)",
    re.IGNORECASE,
)
_PRESENT_TENSE = re.compile(r"\b(?:is|are|has)\b", re.IGNORECASE)


def _clauses(text: str) -> list[_Clause]:
    clauses: list[_Clause] = []
    inherit = False
    for raw in _SEGMENT_SEPARATOR.split(text):
        if not raw:
            continue
        if _SEGMENT_SEPARATOR.fullmatch(raw):
            inherit = raw.strip().casefold() == "but"
            continue
        cleaned = _LIST_PREFIX.sub("", " ".join(raw.split())).strip(" ,")
        if cleaned:
            clauses.append(_Clause(cleaned, inherit))
        inherit = False
    return clauses


def _is_specific_subject(scope: str) -> bool:
    without_pages = _NON_CHANNEL_SUBJECT.sub("", scope)
    return bool(_USD1_REDEMPTION_SUBJECT.search(without_pages))


def _subject_entities(scope: str) -> frozenset[str]:
    without_pages = _NON_CHANNEL_SUBJECT.sub("", scope)
    entities: set[str] = set()
    if _is_specific_subject(scope):
        entities.add("usd1_redemption")
    if re.search(r"\bstablecoins?\b", without_pages, re.IGNORECASE):
        entities.add("stablecoins")
    if re.search(r"\bsettlement\b", without_pages, re.IGNORECASE):
        entities.add("settlement")
    if _WALLETS_OR_API.search(without_pages):
        entities.add("wallets_api")
    return frozenset(entities)


def _is_specific_affected_object(suffix: str) -> bool:
    affected = re.match(
        r"^\W*(?:(?:is|are)\s+)?affecting\b",
        suffix,
        re.IGNORECASE,
    )
    return bool(affected and _is_specific_subject(suffix[affected.end() :]))


def _is_specific_stopped_object(suffix: str) -> bool:
    cleaned = re.sub(r"^\W*(?:the\s+)?", "", suffix)
    return bool(_USD1_REDEMPTION_SUBJECT.match(cleaned))


def _specific_object_has_past_time(suffix: str) -> bool:
    cleaned = re.sub(r"^\W*(?:the\s+)?", "", suffix)
    subject = _USD1_REDEMPTION_SUBJECT.match(cleaned)
    return bool(subject and _PAST_TIME.search(cleaned[subject.end() :]))


def _has_explicit_other_subject(prefix: str) -> bool:
    return not bool(
        re.fullmatch(
            r"\s*(?:(?:,|and|or|with)\s+)*"
            r"(?:(?:is|are|was|were|has|have|be|been|being|remain(?:s|ed)?|"
            r"now|currently|temporarily|still|both|also)\s+)*",
            prefix,
            re.IGNORECASE,
        )
    )


def _predicate_is_nonactual(
    segment: str,
    match: re.Match[str],
    *,
    subject_to_is_static: bool = False,
    modal_object_is_static: bool = False,
) -> bool:
    prefix = _local_event_prefix(segment, match.start())
    suffix = segment[match.end() :]
    if _LEADING_HISTORICAL_SUMMARY.search(segment):
        past_tenses = list(
            re.finditer(r"\b(?:was|were|had)\b", prefix, re.IGNORECASE)
        )
        if past_tenses and not _CURRENT.search(
            prefix[past_tenses[-1].end() :]
        ):
            return True
        if (
            re.match(r"^\s*resolved\s+incident\b", segment, re.IGNORECASE)
            and not _CURRENT.search(prefix)
            and not _CURRENT.search(suffix)
            and not _PRESENT_TENSE.search(prefix)
        ):
            return True
    history = list(_HISTORICAL.finditer(prefix))
    if history:
        since_history = prefix[history[-1].end() :]
        if not _CURRENT.search(since_history) and not _PRESENT_TENSE.search(
            since_history
        ):
            return True
    past_times = list(_PAST_TIME.finditer(prefix))
    if past_times:
        since_past = prefix[past_times[-1].end() :]
        if not _CURRENT.search(since_past):
            return True
    if re.match(
        r"^\W*(?:last\s+(?:year|month|week)|in\s+(?:19|20)\d{2})\b",
        suffix,
        re.IGNORECASE,
    ):
        return True
    if (
        subject_to_is_static or modal_object_is_static
    ) and _specific_object_has_past_time(suffix):
        return True
    if re.match(
        r"^\W*(?:occurred|happened|ended|applied|was\s+in\s+effect)\b"
        r".*\b(?:last\s+(?:year|month|week)|in\s+(?:19|20)\d{2})\b",
        suffix,
        re.IGNORECASE,
    ):
        return True
    resolutions = list(_RESOLVED.finditer(prefix))
    if resolutions:
        since_resolution = prefix[resolutions[-1].end() :]
        if not _CURRENT.search(since_resolution) and not _PRESENT_TENSE.search(
            since_resolution
        ):
            return True
    if _DIRECT_NO.search(prefix):
        return True
    if _NEGATION_BEFORE.search(prefix):
        return True
    if _COORDINATED_NEGATION_BEFORE.search(prefix):
        return True
    if _NEGATED_AFTER.search(suffix):
        return True
    if _NEGATED_PLAN_BEFORE.search(prefix):
        return True
    if _CONDITIONAL_BEFORE.search(prefix):
        return True
    if _MODAL_BEFORE.search(prefix) and _STATIC_MODAL_CONTEXT.search(segment):
        return True
    if subject_to_is_static and _MODAL_BEFORE.search(prefix):
        return True
    if (
        modal_object_is_static
        and _MODAL_BEFORE.search(prefix)
        and not _DYNAMIC_MODAL_CONTEXT.search(segment)
    ):
        return True
    return bool(
        subject_to_is_static
        and re.search(
            r"\bsubject\s+to\s+"
            r"(?:(?:being|a|the|possible|potential|temporary)\s+){0,3}$",
            prefix,
            re.IGNORECASE,
        )
    )


def _local_event_prefix(segment: str, predicate_start: int) -> str:
    prefix = segment[:predicate_start]
    for comma in reversed([match.start() for match in re.finditer(",", prefix)]):
        local = prefix[comma + 1 :]
        if not _subject_entities(local):
            continue
        earlier = prefix[:comma].strip()
        if _HISTORICAL.fullmatch(earlier) or _PAST_TIME.fullmatch(earlier):
            continue
        return local
    for connector in reversed(
        list(re.finditer(r"\band\b", prefix, re.IGNORECASE))
    ):
        local = prefix[connector.end() :]
        if _subject_entities(local) and re.search(
            r"\b(?:is|are|was|were|has|have|had)\b",
            prefix[: connector.start()],
            re.IGNORECASE,
        ):
            return local
    return prefix


def _event_entities(
    clause: str,
    match: re.Match[str],
    previous_end: int,
    previous_entities: frozenset[str],
    allow_specific_object: bool,
) -> frozenset[str]:
    local_prefix = clause[previous_end : match.start()]
    explicit = _subject_entities(local_prefix)
    if previous_end == 0 and not explicit:
        explicit = _subject_entities(clause[: match.start()])
    if _is_specific_affected_object(clause[match.end() :]):
        explicit = explicit | {"usd1_redemption"}
    if allow_specific_object and _is_specific_stopped_object(
        clause[match.end() :]
    ):
        explicit = explicit | {"usd1_redemption"}
    if explicit:
        return frozenset(explicit)
    if _NON_CHANNEL_SUBJECT.search(local_prefix) or _has_explicit_other_subject(
        local_prefix
    ):
        return frozenset()
    if previous_entities:
        return previous_entities
    return frozenset()


def _candidate_events(
    clause: str,
    *,
    usd1_specific: bool,
) -> list[tuple[re.Match[str], RiskLevel, bool]]:
    candidates: list[tuple[re.Match[str], RiskLevel, bool]] = []
    if usd1_specific:
        stop_spans: list[tuple[int, int]] = []
        for match in _STOP.finditer(clause):
            candidates.append((match, RiskLevel.RED, True))
            stop_spans.append(match.span())
        delay_spans: list[tuple[int, int]] = []
        for match in _DELAY_OR_LIMIT.finditer(clause):
            candidates.append((match, RiskLevel.YELLOW, False))
            delay_spans.append(match.span())
        if _BANK_OR_SETTLEMENT.search(clause):
            occupied = stop_spans + delay_spans
            for match in _PROBLEM.finditer(clause):
                if not any(
                    match.start() < end and start < match.end()
                    for start, end in occupied
                ):
                    candidates.append((match, RiskLevel.YELLOW, False))
    else:
        candidates.extend(
            (match, RiskLevel.YELLOW, False)
            for match in _ACTIVE_INCIDENT.finditer(clause)
        )
    candidates.extend(
        (match, RiskLevel.GREEN, False)
        for match in _CURRENT_CLEAR.finditer(clause)
    )
    return sorted(candidates, key=lambda item: (item[0].start(), item[1].value))


def _status_events(
    text: str,
    *,
    usd1_specific: bool,
) -> list[_StatusEvent]:
    events: list[_StatusEvent] = []
    previous_clause_entities: frozenset[str] = frozenset()
    absolute_position = 0
    for clause in _clauses(text):
        previous_end = 0
        previous_event_entities = (
            previous_clause_entities
            if clause.inherits_previous_subject
            else frozenset()
        )
        clause_events = _candidate_events(
            clause.text,
            usd1_specific=usd1_specific,
        )
        for match, level, subject_to_is_static in clause_events:
            local_prefix = clause.text[previous_end : match.start()]
            specific_object = bool(
                level is not RiskLevel.GREEN
                and _is_specific_stopped_object(clause.text[match.end() :])
            )
            entities = _event_entities(
                clause.text,
                match,
                previous_end,
                previous_event_entities,
                level is not RiskLevel.GREEN,
            )
            follows_previous = bool(
                entities & previous_event_entities
                and (previous_end > 0 or clause.inherits_previous_subject)
            )
            temporal_update = bool(
                level is RiskLevel.GREEN
                and (
                    _EXPLICIT_RECOVERY.fullmatch(match.group(0))
                    or re.search(
                        r"\b(?:now|currently)\b",
                        local_prefix,
                        re.IGNORECASE,
                    )
                )
            )
            previous_end = match.end()
            if entities:
                previous_event_entities = entities
            if not entities or _predicate_is_nonactual(
                clause.text,
                match,
                subject_to_is_static=subject_to_is_static,
                modal_object_is_static=specific_object,
            ):
                continue
            events.append(
                _StatusEvent(
                    absolute_position + match.start(),
                    level,
                    entities,
                    clause.text,
                    follows_previous,
                    temporal_update,
                )
            )
        previous_clause_entities = previous_event_entities
        absolute_position += len(clause.text) + 1
    return events


def classify_redemption(
    text: str,
    usd1_specific: bool,
) -> RedemptionClassification:
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if type(usd1_specific) is not bool:
        raise TypeError("usd1_specific must be a bool")

    states: dict[str, _StatusEvent] = {}
    for event in _status_events(text, usd1_specific=usd1_specific):
        for entity in event.entities:
            previous = states.get(entity)
            if (
                event.follows_previous
                and not event.temporal_update
                and previous is not None
                and previous.level.value > event.level.value
            ):
                continue
            states[entity] = event
    if not usd1_specific:
        active = [
            states[entity]
            for entity in ("stablecoins", "settlement")
            if entity in states and states[entity].level is RiskLevel.YELLOW
        ]
        if active:
            latest = max(active, key=lambda event: event.position)
            return RedemptionClassification(
                RiskLevel.YELLOW,
                "可能影响 USD1，尚未确认",
                latest.matched_text,
                False,
            )
        return RedemptionClassification(
            RiskLevel.GREEN,
            "未发现官方限制",
            None,
            False,
        )

    current = states.get("usd1_redemption")
    if current is not None and current.level is RiskLevel.RED:
        return RedemptionClassification(
            RiskLevel.RED,
            "USD1 官方赎回已暂停或不可用",
            current.matched_text,
            True,
        )
    if current is not None and current.level is RiskLevel.YELLOW:
        return RedemptionClassification(
            RiskLevel.YELLOW,
            "USD1 官方赎回可能延迟或受限",
            current.matched_text,
            True,
        )
    return RedemptionClassification(
        RiskLevel.GREEN,
        "未发现官方限制",
        None,
        False,
    )


def apply_recovery(
    previous: RiskLevel,
    current: RiskLevel,
    clear_checks: int,
    *,
    required: int = 2,
) -> RecoveryDecision:
    if type(previous) is not RiskLevel or type(current) is not RiskLevel:
        raise TypeError("previous and current must be RiskLevel values")
    if type(clear_checks) is not int or type(required) is not int:
        raise TypeError("clear_checks and required must be int")
    if clear_checks < 0:
        raise ValueError("clear_checks must not be negative")
    if required < 1:
        raise ValueError("required must be at least 1")
    if current is not RiskLevel.GREEN:
        return RecoveryDecision(current, 0)
    if previous is RiskLevel.GREEN:
        return RecoveryDecision(RiskLevel.GREEN, 0)

    next_clear = clear_checks + 1
    if next_clear < required:
        return RecoveryDecision(previous, next_clear)
    return RecoveryDecision(RiskLevel.GREEN, 0)
