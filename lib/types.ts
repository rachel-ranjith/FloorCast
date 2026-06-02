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
  rows: Row[];
}

export interface Building {
  building_id: string;
  label: string;
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
