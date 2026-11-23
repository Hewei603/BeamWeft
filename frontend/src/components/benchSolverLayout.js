import {equalValues} from './projectDraft.js';

// The project keeps schematic coordinates for editing and package round trips.
// Calculations use all optical inputs, including actual port-to-port lengths.
export function createBenchLayoutSelector() {
  const snapshots = new WeakMap();
  let previous;
  return project => {
    if (!project) return null;
    if (snapshots.has(project)) {
      const known = snapshots.get(project);
      previous = known;
      return known;
    }
    let network = project.network ?? null;
    if (network) {
      const components = network.components.map(component => {
        if (!Object.hasOwn(component, 'diagram_xy')) return component;
        const optical = {...component};
        delete optical.diagram_xy;
        return optical;
      });
      network = {...network, components};
    }
    const input = {
      system: project.system,
      incident_paths: project.incident_paths || [],
      observation_planes: project.observation_planes || [],
      single_pass_power: project.single_pass_power ?? null,
      network,
    };
    if (!equalValues(previous, input)) previous = input;
    snapshots.set(project, previous);
    return previous;
  };
}
