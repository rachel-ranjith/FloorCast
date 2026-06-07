// Shared layout constants + grid packing for the container (building / suite)
// levels. Everything is centred on the origin so the camera framing is simple.

export const BLOCK_W = 6;
export const BLOCK_H = 2.4;
export const BLOCK_D = 6;
export const BLOCK_GAP = 3.5;
export const BLOCK_CELL = BLOCK_W + BLOCK_GAP; // footprint + gap

// Stacked-floor metaphor: a building is a vertical stack of suite "floors".
export const FLOOR_H = 0.45; // thickness of one suite floor plate
export const STACK_BASE = 0.18; // gap between ground and the first floor
export const DC_FLOOR_PITCH = FLOOR_H + 0.7; // compact stack at data-centre level
export const BUILDING_FLOOR_PITCH = FLOOR_H + 1.75; // exploded for inspection
export const BUILDING_BLOCK = 9; // suite-floor footprint when inside a building

export function stackHeight(suiteCount: number, pitch: number): number {
  // Top of the last floor plate.
  return STACK_BASE + (suiteCount - 1) * pitch + FLOOR_H;
}

export interface GridLayout {
  positions: [number, number][]; // [x, z] per item, centred on origin
  cols: number;
  rows: number;
  width: number; // total extent in x
  depth: number; // total extent in z
}

// Pack n items into a centred square-ish grid with the given cell pitch.
export function packGrid(n: number, cell: number = BLOCK_CELL): GridLayout {
  const count = Math.max(n, 1);
  const cols = Math.ceil(Math.sqrt(count));
  const rows = Math.ceil(count / cols);
  const positions: [number, number][] = [];
  for (let i = 0; i < count; i++) {
    const col = i % cols;
    const row = Math.floor(i / cols);
    const x = (col - (cols - 1) / 2) * cell;
    const z = (row - (rows - 1) / 2) * cell;
    positions.push([x, z]);
  }
  return { positions, cols, rows, width: cols * cell, depth: rows * cell };
}

// Spread n points to fill a width × depth area edge-to-edge (used for the
// little heat markers inside a container volume).
export function spreadInArea(n: number, width: number, depth: number): [number, number][] {
  const count = Math.max(n, 1);
  const cols = Math.ceil(Math.sqrt(count));
  const rows = Math.ceil(count / cols);
  const out: [number, number][] = [];
  for (let i = 0; i < count; i++) {
    const col = i % cols;
    const row = Math.floor(i / cols);
    const x = cols > 1 ? (col / (cols - 1) - 0.5) * width : 0;
    const z = rows > 1 ? (row / (rows - 1) - 0.5) * depth : 0;
    out.push([x, z]);
  }
  return out;
}
