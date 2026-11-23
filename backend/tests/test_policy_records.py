import io
import json
from zipfile import ZipFile

from app.models import GeneratedContent
from app.workbench.context import selected_context
from app.workbench.policy_records import policy_selection
from app.workbench.service import study_view_for_call
from .test_study_tolerance import archive, axis


def test_saved_qualification_pins_source_and_survives_reassessment(client, db):
    case, table, _ = archive(db, [axis("lens", [80,120]), axis("station", [80,120])],
                             [{"output":"error", "upper":0}], lambda f, d: ({"error":abs(f-d)}, {}))
    base = f"/api/cases/{case}"
    attachment = client.post(base+"/attachments", files={"file":("lens.csv", b"f_mm,count\n80,9\n120,1\n", "text/csv")}).json()
    policy = {"search_id":table.id, "roles":["hidden","control"], "distribution":{
        "basis":"observed", "source":"count by lens in lens.csv", "originals":[{"attachment_id":attachment["id"]}],
        "scenarios":[{"coordinates":[80], "weight":9},{"coordinates":[120], "weight":1}]}}
    source = table.id
    endpoint = base+"/studies/adjustment-policy"
    first = client.post(endpoint+"/records", json=policy)
    assert first.status_code == 200, first.text
    record = first.json()
    frozen = record["adjustment_evaluation"]
    assert frozen["distribution"]["originals"] == [{
        "attachment_id":attachment["id"], "relative_path":"lens.csv", "sha256":attachment["content_hash"]}]
    assert frozen["designs"][0]["fixed"]["mass"] == {"lower":.9, "upper":.9}
    assert record["root_search_id"] == record["source_search_id"] == source
    view = policy_selection(frozen)
    req = {k:v for k,v in view.items() if k!="mode"}
    assert client.post(endpoint+"/records",json={"search_id":record["id"], **req}).json()["id"] == record["id"]
    other = client.post(endpoint+"/records", json={**policy,"search_id":record["id"],"roles":["observed","control"]}).json()
    assert other["id"] != record["id"] and other["source_search_id"] == source
    assert other["adjustment_evaluation"]["designs"][0]["adaptive"]["mass"] == {"lower":1,"upper":1}
    ctx = selected_context(db, case, {"artifact_ids":[record["id"]]})["artifacts"][0]
    assert ctx["read_adjustment_results"]["content_id"] == record["id"]
    with ZipFile(io.BytesIO(client.get(base+"/project-file").content)) as package:
        records = json.loads(package.read("manifest.json"))["generated_contents"]
        assert next(r["content"] for r in records if r["id"]==record["id"])["adjustment_evaluation"] == frozen
    assert "adjustment_evaluation" not in db.get(GeneratedContent,source).content
    invalid = {**policy, "distribution":{**policy["distribution"], "originals":[
        {"attachment_id":attachment["id"],"sha256":"0"*64}]}}
    assert client.post(endpoint+"/records", json=invalid).status_code == 422


def test_focus_opens_the_design_named_by_its_coordinates_and_keeps_every_candidate(client, db):
    coupled = {"paths":[["spacing_mm"],["inclination_deg"]], "samples":[[40,8],[20,4]]}
    axes = [coupled, axis("temperature", [20,25]), axis("screen_mm", [50,75])]
    case, table, _ = archive(db, axes, [{"output":"deviation", "upper":100}],
                             lambda spacing, angle, temp, screen: ({"deviation":abs(screen-spacing)}, {}))
    endpoint = f"/api/cases/{case}/studies/adjustment-policy"
    plain = {"search_id":table.id, "roles":["design","hidden","design"]}
    original = client.post(endpoint, json=plain).json()
    assert "focus_design" not in original and "designIndex" not in policy_selection(original)
    selected = [20,4,75]
    request = {**plain, "focus_design":selected}
    report = client.post(endpoint, json=request).json()
    assert report["designIndex"] == 3 and report["focus_design"] == selected
    assert report["designs"] == original["designs"] and report["cells"] == original["cells"]
    assert report["designs"][3]["design"] == selected
    saved = client.post(endpoint+"/records", json=request).json()
    event = study_view_for_call("save_adjustment_evaluation", saved, 12)
    view = event["selection"]
    assert view == {"mode":"adjustment", "roles":request["roles"], "focus_design":selected, "designIndex":3}
    scientific = {k:v for k,v in view.items() if k not in {"mode","designIndex"}}
    assert client.post(endpoint+"/records", json={"search_id":saved["id"], **scientific}).json()["id"] == saved["id"]
    reordered = {**report, "designs":list(reversed(report["designs"]))}
    assert policy_selection(reordered)["designIndex"] == 0
    assert client.post(endpoint, json={**plain, "focus_design":[40,8,50]}).json()["designIndex"] == 0
    for wrong in ([20,4], [4,20,75], [20,4,74.999], [True,4,75], [1], []):
        assert client.post(endpoint, json={**plain, "focus_design":wrong}).status_code == 422
