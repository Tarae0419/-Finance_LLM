"""Read-only checks for human review submissions; never legal grading."""

from collections import Counter

from pydantic import ValidationError

from finreg.dataset import CHECKS, HumanReview, validate_review


def preflight_reviews(connection, entries: object) -> dict:
    """Caller must use a read-only repeatable-read transaction on an existing schema.

    Report only positions, error codes and predicted states, never submitted text.
    This development workflow does not read held-out payloads.
    """
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list) or not entries:
        return {
            "valid": False,
            "db_modified": False,
            "errors": ["expected_nonempty_records"],
            "records": [],
        }
    parsed = []
    for entry in entries:
        try:
            parsed.append(HumanReview.model_validate(entry))
        except ValidationError:
            parsed.append(None)
    counts = Counter(item.example_id for item in parsed if item is not None)
    results = []
    for position, item in enumerate(parsed, 1):
        result = {"position": position, "errors": [], "warnings": [], "predicted_status": None}
        results.append(result)
        if item is None:
            result["errors"].append("invalid_review_record")
            continue
        if counts[item.example_id] > 1:
            result["errors"].append("duplicate_example")
            continue
        # Select only metadata before allowing the shared validator to read a payload.
        row = connection.execute(
            "SELECT split FROM current_example WHERE example_id=%s", (item.example_id,)
        ).fetchone()
        if not row or row[0] not in {"dev", "train"}:
            result["errors"].append("not_development_or_training")
            continue
        try:
            validate_review(connection, item)
        except ValueError as error:
            codes = {
                "Stale review: exact current revision and hash required": "stale_review",
                "Review must cover every evidence ID exactly once": "evidence_ids_mismatch",
                "Evidence ID/snapshot/quote mismatch": "evidence_source_mismatch",
            }
            result["errors"].append(codes.get(str(error), "invalid_stored_example"))
            continue
        incomplete = any(getattr(item, name).result != "pass" for name in CHECKS)
        missing_cross_check = item.reviewer_role == "developer" and not item.official_cross_checks
        if incomplete:
            result["warnings"].append("checks_not_all_passed")
        if missing_cross_check:
            result["warnings"].append("developer_cross_check_missing")
        result["predicted_status"] = (
            "excluded"
            if item.excluded_reason
            else "needs_review"
            if incomplete or missing_cross_check
            else "reviewed"
        )
    return {
        "valid": all(not row["errors"] for row in results),
        "db_modified": False,
        "errors": [],
        "records": results,
    }
