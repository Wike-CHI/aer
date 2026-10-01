"""Usage signals and utility labels: explicit evidence, with its source.

Sections 10-13, 21-22, 53-54, 66-67.

Two vocabularies that must never be collapsed into one. ``ADOPTED`` is what an agent
did; ``HELPFUL`` is what happened. An agent can adopt an experience that is wrong, so a
system that derived usefulness from adoption would be manufacturing the one judgement
it most needs to be told.
"""

from __future__ import annotations

import pytest

from aer import (
    AER,
    RecordNotFoundError,
    UsageSignal,
    UsageSignalSource,
    UsageTrackingError,
    UtilityLabel,
    UtilitySource,
)
from tests.knowledge.support import RecordingIndex
from tests.usage.support import indexed_for, retrieve, signal, utility


@pytest.fixture
def tracked(usage_runtime: AER, index: RecordingIndex):
    """One tracked retrieval with a single hit, ready for a signal."""
    return retrieve(usage_runtime, index, guidance=[indexed_for(usage_runtime)])


class TestUsageSignalTransitions:
    @pytest.mark.parametrize(
        ("value", "source"),
        [
            (UsageSignal.ADOPTED, UsageSignalSource.AGENT),
            (UsageSignal.IGNORED, UsageSignalSource.AGENT),
            (UsageSignal.REJECTED, UsageSignalSource.HUMAN),
        ],
    )
    def test_unknown_may_become_anything(
        self,
        usage_runtime: AER,
        tracked,
        value: UsageSignal,
        source: UsageSignalSource,
    ) -> None:
        """Section 53: ``UNKNOWN`` is the open state, and it opens into all three."""
        row = signal(usage_runtime, tracked, value=value, source=source)

        assert row.usage_signal is value
        assert row.usage_signal_source is source
        assert row.usage_signal_at is not None
        stored = usage_runtime.get_experience_usage(tracked.session_id, "exp-1")
        assert stored is not None
        assert stored.usage_signal is value

    def test_recording_the_same_signal_twice_is_a_noop(self, usage_runtime: AER, tracked) -> None:
        first = signal(usage_runtime, tracked, value=UsageSignal.IGNORED)
        second = signal(usage_runtime, tracked, value=UsageSignal.IGNORED)

        assert first.usage_signal_at == second.usage_signal_at

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            (UsageSignal.IGNORED, UsageSignal.ADOPTED),
            (UsageSignal.ADOPTED, UsageSignal.IGNORED),
            (UsageSignal.ADOPTED, UsageSignal.REJECTED),
            (UsageSignal.REJECTED, UsageSignal.ADOPTED),
        ],
    )
    def test_a_contradiction_is_refused(
        self,
        usage_runtime: AER,
        tracked,
        first: UsageSignal,
        second: UsageSignal,
    ) -> None:
        """Section 53: never a silent flip. "Ignored" quietly becoming "adopted" is
        the single most corrupting write this table can take."""
        signal(usage_runtime, tracked, value=first)

        with pytest.raises(UsageTrackingError, match="override=True"):
            signal(usage_runtime, tracked, value=second)

        stored = usage_runtime.get_experience_usage(tracked.session_id, "exp-1")
        assert stored is not None
        assert stored.usage_signal is first

    def test_an_explicit_override_is_allowed(self, usage_runtime: AER, tracked) -> None:
        signal(usage_runtime, tracked, value=UsageSignal.IGNORED)

        row = signal(usage_runtime, tracked, value=UsageSignal.ADOPTED, override=True)

        assert row.usage_signal is UsageSignal.ADOPTED
        assert row.usage_signal_source is UsageSignalSource.AGENT

    def test_an_explicit_override_still_needs_a_source(self, usage_runtime: AER, tracked) -> None:
        signal(usage_runtime, tracked, value=UsageSignal.IGNORED)

        with pytest.raises(UsageTrackingError, match="requires a source"):
            signal(
                usage_runtime,
                tracked,
                value=UsageSignal.ADOPTED,
                source=None,
                override=True,
            )


class TestSignalSources:
    def test_a_decided_signal_requires_a_source(self, usage_runtime: AER, tracked) -> None:
        """Section 11: an unsourced claim cannot be audited."""
        with pytest.raises(UsageTrackingError, match="requires a source"):
            signal(usage_runtime, tracked, value=UsageSignal.ADOPTED, source=None)

    def test_unknown_must_not_carry_a_source(self, usage_runtime: AER, tracked) -> None:
        """``UNKNOWN`` means nobody said anything, so there is nothing to attribute."""
        with pytest.raises(UsageTrackingError, match="must not carry a source"):
            signal(
                usage_runtime,
                tracked,
                value=UsageSignal.UNKNOWN,
                source=UsageSignalSource.AGENT,
            )

    def test_every_documented_source_can_be_recorded(
        self, usage_runtime: AER, index: RecordingIndex
    ) -> None:
        """The vocabulary exists so an adapter's guess can be told apart from a
        human's decision later."""
        for source in UsageSignalSource:
            hit = indexed_for(usage_runtime, experience_id=f"exp-{source.value.lower()}")
            tracked = retrieve(usage_runtime, index, guidance=[hit])

            row = signal(
                usage_runtime,
                tracked,
                experience_id=hit.experience_id,
                value=UsageSignal.ADOPTED,
                source=source,
            )

            assert row.usage_signal_source is source


class TestSignalValidation:
    def test_an_experience_outside_the_result_cannot_be_tracked(
        self, usage_runtime: AER, tracked
    ) -> None:
        with pytest.raises(UsageTrackingError, match="not part of retrieval session"):
            signal(usage_runtime, tracked, experience_id="exp-never-retrieved")

    def test_an_unknown_session_is_refused(self, usage_runtime: AER) -> None:
        with pytest.raises(RecordNotFoundError):
            usage_runtime.record_usage_signal(
                session_id="no-such-session",
                experience_id="exp-1",
                signal=UsageSignal.ADOPTED,
                source=UsageSignalSource.AGENT,
            )


class TestUtilityTransitions:
    @pytest.mark.parametrize(
        ("label", "source"),
        [
            (UtilityLabel.HELPFUL, UtilitySource.HUMAN),
            (UtilityLabel.NEUTRAL, UtilitySource.EVALUATOR),
            (UtilityLabel.HARMFUL, UtilitySource.EVALUATOR),
        ],
    )
    def test_unknown_may_become_anything(
        self,
        usage_runtime: AER,
        tracked,
        label: UtilityLabel,
        source: UtilitySource,
    ) -> None:
        row = utility(usage_runtime, tracked, value=label, source=source)

        assert row.utility_label is label
        assert row.utility_label_source is source
        assert row.utility_label_at is not None

    def test_the_source_is_preserved_on_the_stored_row(self, usage_runtime: AER, tracked) -> None:
        """Section 54: a human's judgement must be distinguishable from a model's."""
        utility(
            usage_runtime,
            tracked,
            value=UtilityLabel.HARMFUL,
            source=UtilitySource.HUMAN,
        )

        stored = usage_runtime.get_experience_usage(tracked.session_id, "exp-1")
        assert stored is not None
        assert stored.utility_label_source is UtilitySource.HUMAN

    def test_a_contradictory_utility_label_is_refused(self, usage_runtime: AER, tracked) -> None:
        utility(usage_runtime, tracked, value=UtilityLabel.HELPFUL)

        with pytest.raises(UsageTrackingError, match="override=True"):
            utility(usage_runtime, tracked, value=UtilityLabel.HARMFUL)

        stored = usage_runtime.get_experience_usage(tracked.session_id, "exp-1")
        assert stored is not None
        assert stored.utility_label is UtilityLabel.HELPFUL

    def test_utility_can_be_overridden_explicitly(self, usage_runtime: AER, tracked) -> None:
        """Section 54 allows it, and requires it to be deliberate."""
        utility(usage_runtime, tracked, value=UtilityLabel.HELPFUL, source=UtilitySource.AGENT)

        row = utility(
            usage_runtime,
            tracked,
            value=UtilityLabel.HARMFUL,
            source=UtilitySource.HUMAN,
            override=True,
        )

        assert row.utility_label is UtilityLabel.HARMFUL
        assert row.utility_label_source is UtilitySource.HUMAN

    def test_a_decided_utility_label_requires_a_source(self, usage_runtime: AER, tracked) -> None:
        with pytest.raises(UsageTrackingError, match="requires a source"):
            utility(usage_runtime, tracked, value=UtilityLabel.HELPFUL, source=None)


class TestSignalAndUtilityAreIndependent:
    def test_adoption_does_not_imply_usefulness(self, usage_runtime: AER, tracked) -> None:
        """Section 12: the agent adopted it; whether it helped is a separate claim."""
        row = signal(usage_runtime, tracked, value=UsageSignal.ADOPTED)

        assert row.utility_label is UtilityLabel.UNKNOWN
        assert row.utility_label_source is None

    def test_a_harmful_experience_may_still_have_been_adopted(
        self, usage_runtime: AER, tracked
    ) -> None:
        """The case the two vocabularies exist for: adopted and harmful at once."""
        signal(usage_runtime, tracked, value=UsageSignal.ADOPTED)

        row = utility(
            usage_runtime, tracked, value=UtilityLabel.HARMFUL, source=UtilitySource.HUMAN
        )

        assert row.is_adopted is True
        assert row.utility_label is UtilityLabel.HARMFUL

    def test_ignoring_an_experience_does_not_mark_it_harmful(
        self, usage_runtime: AER, tracked
    ) -> None:
        """Section 69's rule at the row level: not using something says nothing about
        whether it was any good."""
        row = signal(usage_runtime, tracked, value=UsageSignal.IGNORED)

        assert row.utility_label is UtilityLabel.UNKNOWN
