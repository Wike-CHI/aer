"""Run a local, deterministic experience reuse acceptance scenario.

Uses SQLite and the real NeuG index, but no model credentials or live WordPress site.
The simulated page is checked independently of the agent's completion declaration.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from aer import (
    AER,
    CallableDistillationProvider,
    ExperienceCandidate,
    H1CountVerifier,
    UsageSignal,
    UsageSignalSource,
    VerificationContext,
)
from aer.knowledge.formatter import FORMATTER_VERSION, ExperienceContextFormatter
from aer.usage.fingerprints import context_fingerprint


def run_demo(root: Path) -> dict[str, object]:
    """Create isolated runs; fail loudly if retrieval, verification or reuse fails."""
    provider = CallableDistillationProvider(
        name="local-demo-rule",
        func=lambda evidence: ExperienceCandidate(
            domain="wordpress",
            title="WordPress H1 permission recovery",
            problem="WordPress H1 update denied without edit_posts",
            failed_attempts=("update without edit_posts",),
            root_cause="missing edit_posts capability",
            solution="grant edit_posts before updating H1",
        ),
    )
    page = {"h1_count": 0}
    with AER(
        root / "data", knowledge_dir=root / "knowledge", distillation_provider=provider
    ) as runtime:
        first = runtime.start_run(task="WordPress H1 permission recovery", task_type="wordpress")
        attempt = first.tool("wordpress.update_h1")
        try:
            with attempt:
                raise PermissionError("missing edit_posts")
        except PermissionError:
            pass
        if attempt.error_record is None:
            raise RuntimeError("failed tool did not create an error record")
        with (
            first.recovery(reason="grant edit_posts", error_id=attempt.error_record.id),
            first.tool("wordpress.update_h1") as tool,
        ):
            page["h1_count"] = 1
            tool.set_result(dict(page))
        first.success()
        first.verify(
            H1CountVerifier(expected_count=1),
            context=VerificationContext(
                run_id=first.run_id, payload={"actual_count": page["h1_count"]}
            ),
        )
        experience = first.distill()
        if experience is None or not experience.outcome_verified:
            raise RuntimeError("recovery did not produce verified experience")
        runtime.project_experiences()

        second = runtime.start_run(task="WordPress H1 permission recovery", task_type="wordpress")
        tracked = runtime.retrieve_for_run(
            "WordPress H1 permission", run_id=second.run_id, domain="wordpress"
        )
        hits = tracked.result.guidance
        if experience.id not in {hit.experience_id for hit in hits}:
            raise RuntimeError("real index did not retrieve the recovery")
        formatter = ExperienceContextFormatter()
        rendered = formatter.format(tracked.result)
        # This local agent consumes the rendered context; recording alone is not adoption.
        if "grant edit_posts" not in rendered:
            raise RuntimeError("injected context omitted the recovery action")
        runtime.record_injection(
            session_id=tracked.session_id,
            experience_ids=[hit.experience_id for hit in hits],
            context_fingerprint=context_fingerprint(rendered),
            formatter_version=FORMATTER_VERSION,
        )
        runtime.record_usage_signal(
            session_id=tracked.session_id,
            experience_id=experience.id,
            signal=UsageSignal.ADOPTED,
            source=UsageSignalSource.AGENT,
        )
        with second.tool("wordpress.update_h1", input={"capability": "edit_posts"}) as tool:
            page["h1_count"] = 1
            tool.set_result(dict(page))
        second.success()
        second.verify(
            H1CountVerifier(expected_count=1),
            context=VerificationContext(
                run_id=second.run_id, payload={"actual_count": page["h1_count"]}
            ),
        )
        if not second.verified_success():
            raise RuntimeError("second run failed independent verification")
        before = asdict(runtime.experience_effectiveness(experience.id))
        ids = {"source_run": first.run_id, "target_run": second.run_id, "experience": experience.id}

    # Usage analytics must survive reopening and do not depend on opening the index.
    with AER(root / "data") as reopened:
        after = asdict(reopened.experience_effectiveness(experience.id))
        if before != after or after["verified_success_runs"] != 1:
            raise RuntimeError("usage evidence did not survive restart")
    return {"scenario": "local simulation; no live site or model", **ids, "usage": after}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, help="parent for a new isolated demo directory")
    args = parser.parse_args()
    if args.output_dir is None:
        with TemporaryDirectory(prefix="aer-demo-") as temporary:
            result = run_demo(Path(temporary))
    else:
        from tempfile import mkdtemp

        args.output_dir.mkdir(parents=True, exist_ok=True)
        root = Path(mkdtemp(prefix="aer-demo-", dir=args.output_dir))
        result = run_demo(root)
        result["directory"] = str(root.resolve())
        (root / "evidence.json").write_text(
            json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8"
        )
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
