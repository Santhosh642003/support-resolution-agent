"""F-009: the model writes the finding; what happens next is a fact about the system.

The check catches a NAMED CLASS of commitment phrasing. It cannot catch a paraphrase, and the
residual rate is measured in M3 as its own rubric category ("unbacked commitment")."""

import pytest

from app.workflow import templates
from app.workflow.explanation import commitment_phrases

# The three sentences the live Groq run produced (task 8, step 5), verbatim.
LIVE_EXAMPLES = [
    "We’ll keep monitoring its progress and let you know if anything changes.",
    "We’ll keep monitoring it and let you know if anything changes.",
    "We are sorry for the inconvenience and will work with you to resolve this.",
]


@pytest.mark.parametrize("sentence", LIVE_EXAMPLES)
def test_the_three_live_examples_are_caught(sentence: str) -> None:
    assert commitment_phrases(sentence)


@pytest.mark.parametrize(
    "sentence",
    [
        "I'll get back to you shortly.",
        "Our team will review it.",
        "We will follow up with you.",
        "I will keep you posted.",
        "We're looking into it.",
        "Someone will reach out to you.",
        "We'll notify you when it ships.",
        "This has been escalated.",
        "WE WILL LET YOU KNOW.",
    ],
)
def test_other_named_phrasings_are_caught(sentence: str) -> None:
    assert commitment_phrases(sentence)


@pytest.mark.parametrize(
    "sentence",
    [
        "Your order shipped on 2026-04-13 and is currently in transit.",
        "It is expected to arrive by 2026-04-22.",
        "Our policy treats it as lost if it has not arrived by 2026-03-25.",
        "It has been more than 10 business days without delivery.",
        "The carrier reported a delivery problem.",
    ],
)
def test_factual_sentences_are_not_flagged(sentence: str) -> None:
    assert commitment_phrases(sentence) == []


def test_a_paraphrase_is_not_caught_and_that_is_the_stated_limit() -> None:
    """Recorded, not hidden: this passes the check although it implies a promise."""
    assert commitment_phrases("You can count on us to stay on top of this.") == []


@pytest.mark.parametrize(
    ("outcome", "says_something"),
    [
        ("in_transit_on_schedule", True),
        ("late", True),
        ("delivery_exception", True),
        ("presumed_lost", True),
        ("delivered", False),
        ("delivered_digital", False),
        ("not_shipped", False),
        ("canceled", False),
    ],
)
def test_the_closing_line_is_chosen_by_outcome_and_never_promises(
    outcome: str, says_something: bool
) -> None:
    line = templates.closing_line(outcome, handoff_raised=False)
    assert bool(line) is says_something
    assert commitment_phrases(line) == []  # the system's own line passes the model check


def test_a_raised_handoff_selects_the_handoff_notice() -> None:
    assert templates.closing_line("late", handoff_raised=True) == templates.handoff_notice()
