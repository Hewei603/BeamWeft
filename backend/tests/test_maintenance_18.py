"""Cross-workflow regressions from the two reviews, applied to the current code."""
import pytest

from app.api.case_modules import _load_curve_csv, _load_xy_csv
from app.models import InventoryItem, InventoryLoan
from app.physics.coating_tool import evaluate_coating
from app.physics.laser_metrics import caird, findlay_clay, fit_power_curve
from app.physics.toolkit import _roc, run_cavity_design


@pytest.mark.parametrize('radius', [-200, '-200'])
def test_convex_mirror_is_not_silently_changed_to_concave(radius):
    result = run_cavity_design({'R1_mm': 'flat', 'R2_mm': radius, 'L_mm': 50})
    assert result['geometry']['R2_mm'] == -200
    assert result['analysis']['stable'] is False
    assert result['analysis']['stability_m'] == pytest.approx(1.5)


@pytest.mark.parametrize('radius', [0, float('nan'), float('inf'), True])
def test_invalid_curvature_requires_input(radius):
    assert _roc(radius) is None


@pytest.mark.parametrize('bad_row', ['bad,2', '2,bad', '2,nan', '2,inf', '2'])
def test_bad_measurement_rows_are_never_partially_appended_or_skipped(tmp_path, bad_row):
    path = tmp_path / 'measurement.csv'
    path.write_text('pump,output\n0,0\n1,0\n' + bad_row + '\n3,1\n', encoding='utf-8')
    with pytest.raises(ValueError, match='第 4 行'):
        _load_xy_csv(str(path))


def test_quoted_percent_coating_csv_is_normalized_and_checked(tmp_path):
    path = tmp_path / 'coating.csv'
    path.write_text('"wavelength_nm","R","T"\n"1000","99.5","0.5"\n"1100","99.8","0.2"\n', encoding='utf-8')
    curve = _load_curve_csv(str(path), 'percent')
    result = evaluate_coating({'vendor_curve': curve, 'query_wavelengths_nm': [1064]})
    assert result['query_results'][0]['R'] == pytest.approx(0.99692)
    assert result['query_results'][0]['T'] == pytest.approx(0.00308)
    assert evaluate_coating({'vendor_curve': _load_curve_csv(str(path)), 'query_wavelengths_nm': [1064]})['status'] == 'failed'


def test_incomplete_optional_transmission_is_not_silently_dropped(tmp_path):
    path = tmp_path / 'curve.csv'
    path.write_text('wl,R,T\n1000,0.99,0.01\n1100,0.98\n', encoding='utf-8')
    with pytest.raises(ValueError, match='第 3 行'):
        _load_curve_csv(str(path))


@pytest.mark.parametrize('curve', [
    {'wl_nm': [900, 1000], 'R': [0.8]},
    {'wl_nm': [900, 900], 'R': [0.8, 0.9]},
    {'wl_nm': [900, 1000], 'R': [0.9, 0.9], 'T': [0.2, 0.1]},
])
def test_invalid_measured_curve_reports_failure(curve):
    assert evaluate_coating({'vendor_curve': curve, 'query_wavelengths_nm': [950]})['status'] == 'failed'


def test_negative_loss_remains_diagnostic_and_is_not_applicable():
    series = [{'output_coupler_T_pct': t, 'threshold_pump': t - 1,
               'slope_efficiency': 0.6 / (1 - 1 / t)} for t in (2, 5, 10)]
    for result in (findlay_clay(series), caird(series)):
        assert result['applicable'] is False
        assert result['round_trip_loss_pct'] == -1
        assert result['r_squared'] == 1


def test_nonfinite_or_constant_pump_data_does_not_reach_polyfit():
    assert fit_power_curve([0, 1, 2], [0, float('nan'), 1])['status'] == 'failed'
    assert fit_power_curve([1, 1, 1], [0, 1, 2])['status'] == 'failed'


def test_power_api_normalizes_units_across_multiple_couplers(client):
    case = client.post('/api/cases', json={'title': 'Synthetic multi-unit measurement', 'cavity_type': 'custom', 'goal': 'Recover passive loss'}).json()
    for t, unit, factor in [(2, 'W', 1), (5, 'mW', 1000), (10, 'μW', 1e6)]:
        threshold = 50 * (t / 100 + 0.02)
        slope = 0.6 / (1 + 0.02 / (t / 100))
        pump = list(range(16))
        output = [max(0, slope * (p - threshold)) * factor for p in pump]
        config = {'data': {'pump': pump, 'output': output}, 'pump_unit': 'W', 'output_unit': unit, 'output_coupler_T_pct': t}
        module = client.post(f'/api/cases/{case["id"]}/modules', json={'module_type': 'power_curve', 'config_json': config}).json()
        response = client.post(f'/api/cases/modules/{module["id"]}/run', json={'config_json': {}})
        assert response.status_code == 200
        result = response.json()['result_json']
        assert result['status'] == 'completed'
        assert result['fit']['slope_efficiency'] == pytest.approx(slope, abs=1e-4)
        assert result['units'] == {'pump': 'W', 'output': 'W'}
        assert result['input_units']['output'] == unit
    assert result['findlay_clay']['round_trip_loss_pct'] == pytest.approx(2, abs=0.01)
    assert result['caird']['round_trip_loss_pct'] == pytest.approx(2, abs=0.01)


@pytest.mark.parametrize('condition', ['ok', 'uncertain', None])
def test_regular_user_cannot_clear_damaged_state(client, db, condition):
    item = InventoryItem(name='Synthetic quarantined mirror', category='mirror', condition='damaged', quantity=1)
    db.add(item)
    db.commit()
    response = client.patch(f'/api/inventory/items/{item.id}/condition', headers={'x-user-id': '123', 'x-user-role': 'user'}, json={'condition': condition})
    assert response.status_code == 403
    db.refresh(item)
    assert item.effective_condition == 'damaged'


def test_return_cannot_clear_damage_and_denial_does_not_record_return(client, db):
    item = InventoryItem(name='Synthetic damaged loan', category='mirror', condition='damaged', quantity=1)
    db.add(item)
    db.flush()
    loan = InventoryLoan(item_id=item.id, borrower_name='Synthetic user', quantity=1)
    db.add(loan)
    db.commit()
    headers = {'x-user-id': '123', 'x-user-role': 'user'}
    response = client.post(f'/api/inventory/loans/{loan.id}/return', headers=headers, json={'condition_on_return': 'ok'})
    assert response.status_code == 403
    db.refresh(loan)
    assert loan.returned_at is None
    assert client.post(f'/api/inventory/loans/{loan.id}/return', headers=headers, json={}).status_code == 200
    db.refresh(item)
    assert item.effective_condition == 'damaged'
