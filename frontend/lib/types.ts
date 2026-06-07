export interface Rack {
  rack_type: string;
  family: string;
  generation: number;
  power_draw_kw: number;
}

export interface Position {
  position_id: string;
  ordinal: number;
  occupied: boolean;
  rack_id?: string;
  rack?: Rack;
}

export interface Row {
  row_id: string;
  label: string;
  capacity_kw: number;
  load_kw: number;
  positions: Position[];
}

export interface Suite {
  suite_id: string;
  label: string;
  // Optional — used when the real feed provides them; otherwise rolled up
  // from the rows by the helpers below.
  capacity_kw?: number;
  load_kw?: number;
  rows: Row[];
}

export interface Building {
  building_id: string;
  label: string;
  capacity_kw?: number;
  load_kw?: number;
  suites: Suite[];
}

export interface FloorData {
  buildings: Building[];
  row_capacity_kw: number;
}

export type LoadStatus = "healthy" | "warning" | "critical";

export function getRowLoadStatus(row: Row): LoadStatus {
  const loadPercent = (row.load_kw / row.capacity_kw) * 100;
  if (loadPercent >= 80) return "critical";
  if (loadPercent >= 50) return "warning";
  return "healthy";
}

export function getLoadColor(status: LoadStatus): string {
  switch (status) {
    case "critical":
      return "#ef4444"; // Red
    case "warning":
      return "#f59e0b"; // Amber
    case "healthy":
      return "#14b8a6"; // Teal
  }
}

// --- Roll-ups -------------------------------------------------------------
// Aggregate load/capacity up the hierarchy so building & suite containers can
// show the heat of everything inside them. Same thresholds as a single row.

export type StatusCounts = Record<LoadStatus, number>;

export interface Rollup {
  capacity_kw: number;
  load_kw: number;
  utilization: number; // 0..1
  status: LoadStatus;
  counts: StatusCounts; // distribution of immediate children by status
}

export function statusFromUtilization(utilization: number): LoadStatus {
  const pct = utilization * 100;
  if (pct >= 80) return "critical";
  if (pct >= 50) return "warning";
  return "healthy";
}

function emptyCounts(): StatusCounts {
  return { healthy: 0, warning: 0, critical: 0 };
}

// Statuses of a suite's immediate children (its rows).
export function getSuiteChildStatuses(suite: Suite): LoadStatus[] {
  return suite.rows.map(getRowLoadStatus);
}

export function getSuiteRollup(suite: Suite): Rollup {
  const capacity_kw =
    suite.capacity_kw ?? suite.rows.reduce((a, r) => a + r.capacity_kw, 0);
  const load_kw = suite.load_kw ?? suite.rows.reduce((a, r) => a + r.load_kw, 0);
  const counts = emptyCounts();
  for (const status of getSuiteChildStatuses(suite)) counts[status]++;
  const utilization = capacity_kw > 0 ? load_kw / capacity_kw : 0;
  return { capacity_kw, load_kw, utilization, status: statusFromUtilization(utilization), counts };
}

// Statuses of a building's immediate children (its suites).
export function getBuildingChildStatuses(building: Building): LoadStatus[] {
  return building.suites.map((s) => getSuiteRollup(s).status);
}

export function getBuildingRollup(building: Building): Rollup {
  const suiteRollups = building.suites.map(getSuiteRollup);
  const capacity_kw =
    building.capacity_kw ?? suiteRollups.reduce((a, s) => a + s.capacity_kw, 0);
  const load_kw =
    building.load_kw ?? suiteRollups.reduce((a, s) => a + s.load_kw, 0);
  const counts = emptyCounts();
  for (const s of suiteRollups) counts[s.status]++;
  const utilization = capacity_kw > 0 ? load_kw / capacity_kw : 0;
  return { capacity_kw, load_kw, utilization, status: statusFromUtilization(utilization), counts };
}
