"""Steady monochromatic, power-normalized scattering in a matched scalar mode."""
import cmath
import math

import numpy as np
from scipy.sparse import csc_matrix
from scipy.sparse.linalg import splu


def phase(degrees):
    # Reduce around a quadrant so exact quarter turns and small deviations from
    # them retain their precision at all ports, including destructive interference.
    angle = math.remainder(degrees, 360)
    quarter = round(angle / 90)
    rotation = (1, 1j, -1, -1j)[quarter % 4]
    return rotation * cmath.rect(1, math.radians(angle - 90 * quarter))


def _complex_matrix(raw):
    return np.array([[complex(re, im) for re, im in row] for row in raw])


def scattering_matrix(model, wavelength_nm=None):
    if model.kind in {"matrix", "registered"}:
        raw = model.transfer(wavelength_nm)["matrix"] if model.kind == "registered" else model.matrix
        return _complex_matrix(raw)
    if model.kind == "reflector":
        return np.array([[math.sqrt(model.reflectance) * phase(model.phase_deg)]])
    direct = math.sqrt(model.through_fraction)
    cross = 1j * math.sqrt(1 - model.through_fraction)
    return np.array([[0, 0, direct, cross], [0, 0, cross, direct],
                     [direct, cross, 0, 0], [cross, direct, 0, 0]])


def validate_passivity(matrix):
    if np.abs(matrix).max() > 1 + 1e-12 or np.linalg.norm(matrix, ord=2) > 1 + 1e-12:
        raise ValueError("该散射矩阵会产生净增益。请核对功率归一化、矩阵方向以及适用波长。")


def link_gain(link, wavelength_nm):
    optical_nm = link.length_mm * 1e6 * link.n
    if math.ulp(optical_nm) / wavelength_nm > 1e-5:
        raise ValueError("光程相对波长过长，当前数值精度无法可靠分辨相位。")
    turns = math.remainder(optical_nm, wavelength_nm) / wavelength_nm
    return math.sqrt(link.power_transmission) * phase(turns * 360) * phase(link.phase_deg)


def network_islands(network):
    neighbors = {component.uid: set() for component in network.components}
    for link in network.links:
        a, b = link.a.component, link.b.component
        neighbors[a].add(b)
        neighbors[b].add(a)
    visited = set()
    for uid in neighbors:
        if uid in visited:
            continue
        group, stack = set(), [uid]
        while stack:
            here = stack.pop()
            if here not in group:
                group.add(here)
                stack.extend(neighbors[here] - group)
        visited.update(group)
        yield group


def compile_scattering(components, wavelength_nm):
    keys = [(c.uid, port) for c in components for port in c.ports()]
    indices = {key: i for i, key in enumerate(keys)}
    size = len(keys)
    scattering = np.zeros((size, size), dtype=complex)
    evaluated = []
    for component in components:
        reference = getattr(component.model, "reference_wavelength_nm", None)
        if reference is not None and not math.isclose(reference, wavelength_nm, rel_tol=1e-12):
            raise ValueError(f"{component.name}的散射矩阵标定于 {reference:g} nm，请提供 {wavelength_nm:g} nm 的数据。")
        rows = [indices[(component.uid, p)] for p in component.ports()]
        if component.model.kind == "registered":
            transfer = component.model.transfer(wavelength_nm)
            block = _complex_matrix(transfer["matrix"])
            evaluated.append({"component": component.uid, "wavelength_nm": wavelength_nm,
                              "prescription": component.model.prescription.model_dump(), **transfer})
        else:
            block = scattering_matrix(component.model, wavelength_nm)
        scattering[np.ix_(rows, rows)] = block
    return keys, indices, scattering, evaluated


def _solve(components, links, drives, wavelength_nm):
    keys, indices, scattering, evaluated = compile_scattering(components, wavelength_nm)
    size = len(keys)
    system = np.eye(size, dtype=complex)
    attached = set()
    for link in links:
        a, b = indices[link.a.key()], indices[link.b.key()]
        gain = link_gain(link, wavelength_nm)
        system[a] -= gain * scattering[b]
        system[b] -= gain * scattering[a]
        attached.update((a, b))
    # Each right-hand side represents one mutually coherent field. Independent
    # fields add in measured power only after all paths of each field interfere.
    groups = {}
    for drive, power, _ in drives:
        group = drive.coherence_group or drive.at.key()
        if group not in groups:
            groups[group] = np.zeros(size, dtype=complex)
        groups[group][indices[drive.at.key()]] = math.sqrt(power) * phase(drive.phase_deg or 0)
    incident = np.column_stack(list(groups.values()))
    try:
        factor = splu(csc_matrix(system))
    except RuntimeError as exc:
        raise ValueError("反馈网络的稳态方程奇异。请核对闭环相位、物理损耗和端接条件。") from exc
    # Exact induced 1-norm on these bounded networks. Reuse the sparse factors
    # for excitation and conditioning, including networks with internal loops.
    inverse = factor.solve(np.eye(size, dtype=complex))
    condition = float(np.linalg.norm(system, 1) * np.linalg.norm(inverse, 1))
    if not math.isfinite(condition) or condition > 1e12:
        raise ValueError("反馈网络的稳态方程奇异或接近奇异。请核对闭环相位、物理损耗和端接条件。")
    incoming = factor.solve(incident)
    outgoing = scattering @ incoming
    input_w = np.sum(np.abs(incoming)**2, axis=1)
    output_w = np.sum(np.abs(outgoing)**2, axis=1)
    if not np.isfinite(input_w).all() or not np.isfinite(output_w).all():
        raise ValueError("端口功率超出数值范围，请核对驱动和反馈条件。")
    names = {c.uid: c.name for c in components}
    ports = [{"component": key[0], "name": names[key[0]], "port": key[1], "external": i not in attached,
              "incoming_w": float(input_w[i]), "outgoing_w": float(output_w[i])}
             for i, key in enumerate(keys)]
    launched = math.fsum(p for _, p, _ in drives)
    escaped = math.fsum(p["outgoing_w"] for p in ports if p["external"])
    if escaped - launched > 1e-9 * max(launched, escaped):
        raise ValueError("外部端口的稳态功率收支未通过校验，请核对模型及数值条件。")
    return {"ports": ports, "input_w": launched, "external_w": escaped,
            "removed_w": max(0., launched - escaped), "condition_number_1": condition,
            **({"evaluated_devices":evaluated} if evaluated else {})}


def solve_network(network, wavelength_nm, sources):
    islands, ports, issues = [], [], []
    for group in network_islands(network):
        components = [c for c in network.components if c.uid in group]
        links = [link for link in network.links if link.a.component in group]
        drives = [src for src in sources if src[0].at.component in group]
        missing = [f"{d.at.component} / {d.at.port} · {issue}" for d, _, issue in drives if issue]
        item = {"components": [c.uid for c in components]}
        if not drives:
            missing.append("需要指定入射端口及光功率。")
        if missing:
            item.update(status="needs_input", message=" ".join(missing))
        else:
            try:
                solved = _solve(components, links, drives, wavelength_nm)
                ports.extend(solved.pop("ports"))
                item.update(status="completed", **solved)
            except (ArithmeticError, ValueError, np.linalg.LinAlgError) as exc:
                item.update(status="unsupported", message=str(exc))
        if "message" in item:
            issues.append("、".join(c.name for c in components) + " · " + item["message"])
        islands.append(item)
    if not islands:
        issues.append("请先添加元件并指定光源。")
    status = ("completed" if not issues else "partial" if ports else
              "unsupported" if any(g["status"] == "unsupported" for g in islands) else "needs_input")
    notes = [f"{d['component']} · {note}" for island in islands for d in island.get("evaluated_devices", []) for note in d.get("warnings", [])]
    return {"status": status, "wavelength_nm": wavelength_nm, "ports": ports, "islands": islands,
            "warnings": issues+notes, "scope": "同一真空波长下的静态线性被动散射。每个端口代表一个归一化传播模式，连接两端须采用对应的横模与偏振基底。未接线端口为匹配端接。引用顺序光路时按该参考面的前向功率驱动，回波止于驱动端口。温升、起振与空间失配需要独立模型。"}
