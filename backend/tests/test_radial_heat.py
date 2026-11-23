import math

import numpy as np
import pytest
from scipy.sparse import diags
from scipy.sparse.linalg import spsolve

from app.physics.radial_heat import radial_heating
from app.workbench.contracts import RadialHeating


def model(**changes):
    return RadialHeating.model_validate({"absorbed_w":[4,8],"heat_fraction":[.25,.5],"length_mm":10,
        "radius_mm":2,"conductivity":10,"zone_edges_mm":[2],"relative_density":[1],"basis":"test assumptions",**changes})


def test_uniform_rod_matches_koechner_equations_7_7_and_7_9():
    result = radial_heating(model(side_conductance=2000))
    assert result["heat_w"] == [1,4]
    center = np.array([1.,4.])/(4*math.pi*10*.010)
    contact = np.array([1.,4.])/(2*math.pi*.002*.01*2000)
    assert result["axis_to_surface_k"] == pytest.approx(center)
    assert result["axis_to_sink_k"] == pytest.approx(center+contact)
    profile = result["profile"]
    assert profile["low_k"] == pytest.approx(center[0]*(1-(np.array(profile["radius_mm"])/2)**2))


def test_hollow_heat_source_matches_independent_finite_volume_solution():
    args=model(zone_edges_mm=[.5,1.25,1.75],relative_density=[0,4,.5])
    result=radial_heating(args)
    # Integrate the steady heat equation on annular finite volumes with a fixed wall.
    faces=np.linspace(0,.002,401)
    dr=faces[1]
    centers=(faces[:-1]+faces[1:])/2
    density=np.select([centers<.0005,centers<.00125,centers<.00175],[0,4,.5],default=0)
    deposited=density*np.diff(faces**2)
    deposited/=deposited.sum()  # lower heat load is 1 W
    radial_g=2*math.pi*.010*10*faces/dr
    radial_g[-1]*=2  # half-cell to the isothermal surface
    matrix=diags([-radial_g[1:-1],radial_g[:-1]+radial_g[1:],-radial_g[1:-1]],[-1,0,1],format="csc")
    temperatures=spsolve(matrix,deposited)
    profile=result["profile"]
    x=np.array(profile["radius_mm"])*.001
    comparison=np.interp(x,np.r_[0,centers,faces[-1]],np.r_[temperatures[0],temperatures,0])
    assert profile["low_k"] == pytest.approx(comparison,abs=1e-5)
    assert "axis_to_sink_k" not in result
    assert radial_heating(model(absorbed_w=[0,0]))["axis_to_surface_k"] == [0,0]


@pytest.mark.parametrize("change",[{"absorbed_w":[2,1]},{"heat_fraction":[.8,.3]},{"relative_density":[0]},
    {"zone_edges_mm":[1,2]},{"zone_edges_mm":[3]},{"zone_edges_mm":[1,1],"relative_density":[1,2]},
    {"side_conductance":0},{"basis":" "}])
def test_invalid_heat_sources_explain_missing_physics(change):
    with pytest.raises(ValueError):
        model(**change)


@pytest.mark.parametrize("change", [{"length_mm": 1e-323}, {"side_conductance": 5e-324}])
def test_unrepresentable_thermal_scales_report_an_input_error(change):
    with pytest.raises(ValueError, match="数值范围|可表示范围"):
        radial_heating(model(**change))
