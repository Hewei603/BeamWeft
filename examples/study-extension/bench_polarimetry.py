"""A standalone example package for the BeamWeft v1 study interface."""
import math
from pydantic import Field
from app.study_api import Port, Study, StrictInput


class AnalyzerInput(StrictInput):
    incident_w: float = Field(ge=0, title="入射功率 / W")
    angle_deg: float = Field(title="透光轴夹角 / °")


def transmit(inputs):
    fraction = math.cos(math.radians(inputs.angle_deg))**2
    return {"status": "completed", "fraction": fraction, "output_w": inputs.incident_w * fraction}


power = Port(quantity="optics.power", label="光功率", unit="W", scale=1)
analyzer = Study("linear_analyzer_power", "理想线偏振分析器", "2", "对线偏振输入应用马吕斯定律。角度按入射偏振方向与透光轴的夹角定义，忽略插入损耗。",
                 AnalyzerInput, transmit, {"output_w": "透过功率 / W", "fraction": "透过比例"},
                 input_ports={"incident_w": power}, output_ports={"output_w": power})
