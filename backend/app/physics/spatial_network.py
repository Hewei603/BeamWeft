"""Steady paraxial fields in a pointwise scattering network with homogeneous links."""

from graphlib import TopologicalSorter
from math import pi

import numpy as np
from scipy.fft import ifft2
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components

from .port_network import compile_scattering, link_gain
from .scalar_field import edge_fraction, fresnel_transfer


class SpatialNetwork:
    def __init__(self, components, links, wavelength_nm, driven):
        self.keys, index, self.scattering, self.devices = compile_scattering(components, wavelength_nm)
        self.names = {c.uid: c.name for c in components}
        self.wavelength_nm = wavelength_nm
        self.connections = {}
        for link in links:
            a, b = index[link.a.key()], index[link.b.key()]
            self.connections[a] = (b, link, -1)
            self.connections[b] = (a, link, 1)
        # Unknowns are incident channels. One physical coupler can be traversed
        # in both directions without necessarily forming a feedback cycle.
        n = len(self.keys)
        graph = np.zeros((n, n), dtype=bool)
        for dest, (out, link, _) in self.connections.items():
            if link.power_transmission:
                graph[:, dest] = self.scattering[out] != 0
        active = {index[k] for k in driven}
        work = list(active)
        while work:
            additions = set(np.flatnonzero(graph[work.pop()])) - active
            work.extend(additions)
            active.update(additions)
        _, labels = connected_components(csr_matrix(graph), directed=True, connection="strong")
        self.active = active
        self.outputs = set(np.flatnonzero(np.any(self.scattering[:, sorted(active)] != 0, axis=1)))
        blocks = {label: sorted(i for i in active if labels[i] == label) for label in {labels[i] for i in active}}
        dependencies = {
            label: {labels[j] for i in rows for j in np.flatnonzero(graph[:, i]) if j in active and labels[j] != label}
            for label, rows in blocks.items()
        }
        self.blocks = [
            (blocks[label], len(blocks[label]) > 1 or graph[blocks[label][0], blocks[label][0]])
            for label in TopologicalSorter(dependencies).static_order()
        ]

    def check_size(self, cells, groups):
        points = (2 * cells) ** 2
        active = sum(len(rows) for rows, _ in self.blocks)
        # Complex spectra, link transfers and FFT scratch are simultaneously resident for one group.
        if (3 * active + 16) * points * 16 > 384 * 1024**2:
            raise ValueError("当前网格和连通端口数量超出空间场内存范围，请降低采样或选取较小的独立网络。")
        cyclic_work = sum(len(rows) ** 3 for rows, cyclic in self.blocks if cyclic)
        if points * groups * cyclic_work > 3e8:
            raise ValueError("该闭环的频率逐点联立求解超出交互计算范围，请降低采样或简化反馈网络。")

    def _transfers(self, grid):
        transfers = {}
        for dest, (out, link, direction) in self.connections.items():
            if dest not in self.active:
                continue
            carrier = link_gain(link, self.wavelength_nm)
            shift_t = direction * link.shift_t_mm
            shift_s = direction * link.shift_s_mm
            transfer = fresnel_transfer(grid.freq2, grid.wavelength, link.length_mm / link.n) * carrier
            if shift_t or shift_s:
                transfer *= np.exp(-2j * pi * (shift_t * grid.frequency[None, :] + shift_s * grid.frequency[:, None]))
            transfers[dest] = transfer
        return transfers

    def _scatter(self, row, incoming, shape):
        total = np.zeros(shape, dtype=complex)
        for j in np.flatnonzero(self.scattering[row]):
            if j in incoming:
                total += self.scattering[row, j] * incoming[j]
        return total

    def _feedback(self, rows, rhs, transfers):
        # Each spatial frequency has the ordinary feedback equation (I-HS)a=b.
        # Batch frequencies without constructing a dense port × port × image tensor.
        block = self.scattering[np.ix_([self.connections[i][0] for i in rows], rows)]
        n = len(rows)
        identity = np.eye(n)
        values = np.stack([v.ravel() for v in rhs], axis=1)
        chunk = min(4096, max(1, 131072 // (n * n)))
        for start in range(0, len(values), chunk):
            span = slice(start, start + chunk)
            gains = np.stack([transfers[i].ravel()[span] for i in rows], axis=1)
            matrix = identity - gains[:, :, None] * block
            singular = np.linalg.svd(matrix, compute_uv=False)
            if np.any(singular[:, -1] <= 1e-12 * np.maximum(1.0, singular[:, 0])):
                raise ValueError("部分横向空间频率的反馈方程奇异或接近奇异，请核对损耗、端接和反馈相位。")
            values[span] = np.linalg.solve(matrix, values[span, :, None])[..., 0]
        return [values[:, j].reshape(rhs[0].shape) for j in range(n)]

    def propagate(self, grid, excitation, target, source_w):
        shape = grid.field.shape
        incoming = {self.keys.index(k): v for k, v in excitation.items()}
        transfers = self._transfers(grid)
        boundary = alias = high_angle = 0.0
        for rows, cyclic in self.blocks:
            rhs = []
            for i in rows:
                if i not in self.connections:
                    continue
                out, _, _ = self.connections[i]
                rhs.append(transfers[i] * self._scatter(out, incoming, shape))
            if rhs:
                fields = self._feedback(rows, rhs, transfers) if cyclic else rhs
                incoming.update(zip(rows, fields))
        external = {}
        selected = np.zeros(shape)
        for i, key in enumerate(self.keys):
            if i not in self.outputs:
                if i not in self.connections:
                    external[key] = 0.0
                continue
            field = self._scatter(i, incoming, shape)
            spectrum = np.abs(field) ** 2
            watts = float(spectrum.sum() * grid.dx**2)
            if i not in self.connections:
                external[key] = watts
            density = np.abs(ifft2(field, norm="ortho")) ** 2
            if key == target:
                selected = density
            if watts <= 1e-12 * source_w:
                continue
            boundary = max(boundary, edge_fraction(density))
            if i in self.connections:
                dest, link, sign = self.connections[i]
                # Sign is the translation arriving at i. Travel from i uses the opposite shift.
                walk = grid.wavelength * link.length_mm / link.n * grid.frequency
                half = len(grid.x) * grid.dx / 2
                wrapped = (np.abs(walk - sign * link.shift_t_mm) >= half)[None, :] | (
                    np.abs(walk - sign * link.shift_s_mm) >= half
                )[:, None]
                alias = max(alias, float(spectrum[wrapped].sum() * grid.dx**2) / watts)
                oblique = grid.freq2 * (grid.wavelength / link.n) ** 2 >= 0.15**2
                high_angle = max(high_angle, float(spectrum[oblique].sum() * grid.dx**2) / watts)
                if dest in incoming:
                    boundary = max(boundary, edge_fraction(np.abs(ifft2(incoming[dest], norm="ortho")) ** 2))
        return selected, external, boundary, alias, high_angle
