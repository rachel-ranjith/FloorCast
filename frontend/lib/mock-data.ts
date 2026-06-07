import { FloorData, Position, Row } from "./types";

const rackTypes = [
  { rack_type: "compute-2023", family: "compute", generation: 2023 },
  { rack_type: "compute-2024", family: "compute", generation: 2024 },
  { rack_type: "storage-2022", family: "storage", generation: 2022 },
  { rack_type: "network-2023", family: "network", generation: 2023 },
  { rack_type: "gpu-2024", family: "gpu", generation: 2024 },
];

function generatePositions(
  rowId: string,
  count: number,
  isHotRow: boolean
): { positions: Position[]; totalLoad: number } {
  const positions: Position[] = [];
  let totalLoad = 0;

  for (let i = 0; i < count; i++) {
    const occupied = Math.random() > 0.15; // 85% occupancy
    const positionId = `${rowId}-p${String(i).padStart(2, "0")}`;

    if (occupied) {
      const rackType = rackTypes[Math.floor(Math.random() * rackTypes.length)];
      // Hot rows have higher power draw
      const basePower = isHotRow
        ? 8 + Math.random() * 6 // 8-14 kW for hot rows
        : 4 + Math.random() * 5; // 4-9 kW for normal rows
      const powerDraw = Math.round(basePower * 10) / 10;

      positions.push({
        position_id: positionId,
        ordinal: i,
        occupied: true,
        rack_id: `rk-${rowId}-${i}`,
        rack: {
          ...rackType,
          power_draw_kw: powerDraw,
        },
      });
      totalLoad += powerDraw;
    } else {
      positions.push({
        position_id: positionId,
        ordinal: i,
        occupied: false,
      });
    }
  }

  return { positions, totalLoad };
}

function generateRows(suiteId: string, rowCount: number): Row[] {
  const rows: Row[] = [];
  const hotRowIndex = Math.floor(Math.random() * rowCount);

  for (let i = 0; i < rowCount; i++) {
    const rowId = `${suiteId}-r${String(i + 1).padStart(2, "0")}`;
    const isHotRow = i === hotRowIndex;
    const { positions, totalLoad } = generatePositions(rowId, 10, isHotRow);

    // For hot row, ensure it's over 80% capacity
    let finalLoad = totalLoad;
    if (isHotRow && totalLoad < 72) {
      // Bump up some rack powers to ensure hot status
      const deficit = 75 - totalLoad;
      positions.forEach((p) => {
        if (p.occupied && p.rack && deficit > 0) {
          const bump = Math.min(deficit / 3, 4);
          p.rack.power_draw_kw = Math.round((p.rack.power_draw_kw + bump) * 10) / 10;
          finalLoad += bump;
        }
      });
    }

    rows.push({
      row_id: rowId,
      label: `Row ${String(i + 1).padStart(2, "0")}`,
      capacity_kw: 90.0,
      load_kw: Math.round(finalLoad * 10) / 10,
      positions,
    });
  }

  return rows;
}

function randInt(min: number, max: number): number {
  return min + Math.floor(Math.random() * (max - min + 1));
}

export function generateMockData(): FloorData {
  // Dynamic shape: a handful of buildings, each with a few suites, each with a
  // variable number of rows. This is just dev data — the real feed replaces it.
  const buildingCount = randInt(3, 5);

  const buildings = Array.from({ length: buildingCount }, (_, b) => {
    const buildingId = `b${b + 1}`;
    const suiteCount = randInt(2, 4);

    const suites = Array.from({ length: suiteCount }, (_, s) => {
      const suiteId = `${buildingId}-s${s + 1}`;
      const rowCount = randInt(4, 10);
      return {
        suite_id: suiteId,
        label: `Suite ${s + 1}`,
        rows: generateRows(suiteId, rowCount),
      };
    });

    return {
      building_id: buildingId,
      label: `Building ${b + 1}`,
      suites,
    };
  });

  return {
    buildings,
    row_capacity_kw: 90.0,
  };
}
