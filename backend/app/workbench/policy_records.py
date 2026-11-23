"""Archive a qualification alongside the candidate table and its frozen evidence."""
from sqlalchemy.orm import Session

from ..models import GeneratedContent
from .adjustment_policy import focused_design_index, inspect_adjustment_policy
from .study_search import store_search


def policy_selection(evaluation):
    view = {"mode": "adjustment", "roles": evaluation["roles"]}
    if evaluation.get("focus_design") is not None:
        coordinates = evaluation["focus_design"]
        view.update(focus_design=coordinates, designIndex=focused_design_index(evaluation["designs"], coordinates))
    distribution = evaluation.get("distribution")
    if distribution:
        view["distribution"] = {
            "basis": distribution["basis"], "source": distribution["source"],
            "scenarios": [{key: scenario[key] for key in ("coordinates", "weight")} for scenario in distribution["scenarios"]],
        }
        if distribution.get("originals"):
            view["distribution"]["originals"] = [
                {key: ref[key] for key in ("attachment_id", "sha256")} for ref in distribution["originals"]]
    return view


def save_policy_comparison(bind, case_id, request, task_id=None):
    result = inspect_adjustment_policy(bind, case_id, request)
    with Session(bind) as db:
        source = db.query(GeneratedContent).filter_by(
            case_id=case_id, id=request.search_id, content_type="study_search").one_or_none()
        if source is None:
            raise ValueError("原比较已不可用，请重新打开要保存的比较。")
        table = source.content
        # Alternate policies over a table are peers. Saving the same one again from its
        # own history entry should resolve to the existing record.
        parent = (table.get("adjustment_evaluation") or {}).get("search_id", source.id)
        report = {k: v for k, v in table.items() if k not in {"refinement", "original_refinement_execution"}}
        report.update(source_search_id=parent, root_search_id=table.get("root_search_id", parent),
                      adjustment_evaluation={**result, "search_id": parent})
        return store_search(db, case_id, report, task_id)
