"""Acceptance evidence for the knowledge plane, runnable inside the production image.

The pytest suite already covers all of this, but the image deliberately ships no
pytest -- it ships the runtime and the ops scripts. This is the same scenario in a
form an operator can run on the host, which is what the milestone's production
acceptance step needs:

    python /app/scripts/drill_knowledge.py /tmp/knowledge-drill

It builds a throwaway store holding the two experiences from the brief (one verified
recovery, one known failure about the same problem), projects them into a real index,
retrieves, **deletes the entire knowledge database**, rebuilds it from SQLite, and
checks the answer came back unchanged. It then reports what the production database
would look like when the store is empty, which is the state it is actually in.

Exits 0 when every expectation holds. Writes ``evidence.json`` beside the log.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

PROBLEM = "WordPress REST API 403"
QUERY = "WordPress REST API 403"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workdir", nargs="?", default="/tmp/knowledge-drill")
    args = parser.parse_args(argv)

    root = Path(args.workdir)
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)

    from aer import AER, ExperienceKind, ExperienceStatus, RetrievalMode
    from aer.knowledge.formatter import ExperienceContextFormatter

    results: list[tuple[str, bool, str]] = []
    evidence: dict[str, object] = {}

    def check(label: str, holds: bool, detail: str = "") -> None:
        results.append((label, holds, detail))
        print(f"  [{'ok  ' if holds else 'FAIL'}] {label}{('  -- ' + detail) if detail else ''}")

    def store(runtime: AER, experience_id: str, kind: str, *, solution: str | None) -> None:
        from aer import Experience

        runtime.experiences.create(
            Experience(
                id=experience_id,
                kind=ExperienceKind(kind),
                domain="wordpress",
                title=PROBLEM,
                problem=f"{PROBLEM} {'应用密码无效，改用具备编辑权限的应用密码' if kind == 'RECOVERY' else '盲目重试无效'}",
                dedup_key=f"{kind}|wordpress|{experience_id}",
                root_cause="应用密码缺少 edit_posts",
                solution=solution,
                failed_attempts=("盲目重试",),
                avoid=("不要盲目重试",),
                status=ExperienceStatus.VERIFIED,
                outcome_verified=True,
            )
        )

    def answer(runtime: AER) -> dict[str, object]:
        result = runtime.retrieve(QUERY, domain="wordpress")
        return {
            "guidance": [(hit.experience_id, hit.kind.value, hit.label) for hit in result.guidance],
            "warnings": [(hit.experience_id, hit.kind.value, hit.label) for hit in result.warnings],
            "scores": [round(hit.retrieval_score, 6) for hit in result.all_hits],
        }

    print("## 1. a throwaway store with the brief's corpus")
    data = root / "data"
    knowledge = root / "knowledge"
    with AER(data, knowledge_dir=knowledge) as runtime:
        store(runtime, "exp-recovery", "RECOVERY", solution="改用有 edit_posts 权限的应用密码")
        store(runtime, "exp-failure", "FAILURE", solution=None)
        check("two experiences stored", runtime.experiences.count() == 2)

        print("\n## 2. project SQLite -> NeuG")
        report = runtime.rebuild_knowledge()
        check(
            "rebuild projected both experiences",
            report.projected == 2,
            report.description,
        )
        status = runtime.knowledge_status()
        check(
            "index is in sync with the store",
            status.in_sync,
            status.describe().replace("\n", " | "),
        )
        evidence["status"] = status.describe()
        evidence["projection_schema_version"] = status.projection_schema_version

        print("\n## 3. retrieve: roles must not blur")
        first = answer(runtime)
        evidence["answer"] = first
        check(
            "guidance is the verified recovery",
            first["guidance"] == [("exp-recovery", "RECOVERY", "Verified Recovery")],
            str(first["guidance"]),
        )
        check(
            "warnings is the known failure",
            first["warnings"] == [("exp-failure", "FAILURE", "Known Failure")],
            str(first["warnings"]),
        )
        rendered = ExperienceContextFormatter().format(runtime.retrieve(QUERY, domain="wordpress"))
        evidence["rendered"] = rendered
        check(
            "the rendered context labels both roles",
            "[Verified Recovery]" in rendered and "[Known Failure]" in rendered,
        )
        check(
            "the failure block carries no solution",
            "Solution" not in rendered.split("[Known Failure]")[1],
        )

        print("\n## 4. diagnostic mode widens without changing roles")
        diagnostic = runtime.retrieve(QUERY, mode=RetrievalMode.DIAGNOSTIC)
        check(
            "diagnostic returns the same two roles",
            [hit.label for hit in diagnostic.guidance] == ["Verified Recovery"]
            and [hit.label for hit in diagnostic.warnings] == ["Known Failure"],
        )

        print("\n## 5. delete the whole knowledge database, then rebuild from SQLite")
        index_path = Path(runtime.knowledge_index.path)
        runtime.knowledge_index.close()
        shutil.rmtree(index_path)
        check("knowledge database removed", not index_path.exists(), str(index_path))

        rebuild = runtime.rebuild_knowledge()
        second = answer(runtime)
        check(
            "the answer is identical after a rebuild",
            first == second,
            f"projected {rebuild.projected}",
        )
        check("the store was never touched", runtime.experiences.count() == 2)

        print("\n## 6. a query with nothing relevant")
        empty = runtime.retrieve("完全不相关的主题 xyzzy")
        check("an unmatched query is an empty result, not an error", empty.is_empty)

        print("\n## 7. the production state: an empty store")
        # What `/srv/aer/knowledge` looks like today. Projecting nothing must be a
        # success, and retrieval against an index with no documents must return an
        # empty result rather than failing -- that is the state production is in.
        with AER(root / "empty-data", knowledge_dir=root / "empty-knowledge") as empty_runtime:
            empty_report = empty_runtime.rebuild_knowledge()
            check("rebuilding an empty store succeeds", empty_report.projected == 0)
            empty_status = empty_runtime.knowledge_status()
            check(
                "an empty index reports zero on both sides and no drift",
                empty_status.in_sync
                and empty_status.store_experiences == 0
                and empty_status.index_experiences == 0,
                empty_status.describe().replace("\n", " | "),
            )
            try:
                result = empty_runtime.retrieve("WordPress REST API 403")
                check("retrieval against an empty index returns nothing", result.is_empty)
            except Exception as exc:
                check(
                    "retrieval against an empty index returns nothing",
                    False,
                    f"{type(exc).__name__}: {exc}",
                )

    print("\n## 8. persistence across a fresh process")
    with AER(data, knowledge_dir=knowledge) as reopened:
        reopened_status = reopened.knowledge_status()
        check("the projection survived", reopened_status.index_experiences == 2)
        check("the index still answers", answer(reopened) == first)

    evidence["checks"] = [
        {"label": label, "ok": holds, "detail": detail} for label, holds, detail in results
    ]
    (root / "evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    failures = [label for label, holds, _ in results if not holds]
    print(f"\n{len(results) - len(failures)}/{len(results)} checks held")
    if failures:
        print("\nfailed:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"evidence written to {root / 'evidence.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
