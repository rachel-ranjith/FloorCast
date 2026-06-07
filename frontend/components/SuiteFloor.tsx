"use client";

import { RackRow } from "./RackRow";
import { HoveredRack } from "./ServerRack";
import { Row } from "@/lib/types";

interface SuiteFloorProps {
  rows: Row[];
  onHover: (info: HoveredRack) => void;
  onUnhover: (positionId: string) => void;
}

// The deepest level: the rack grid for a single suite. (This is the original
// floor view, now scoped to one suite instead of every suite at once.)
export function SuiteFloor({ rows, onHover, onUnhover }: SuiteFloorProps) {
  const floorWidth = 20;
  const floorDepth = rows.length * 3.5 + 5;
  const surfaceW = floorWidth + 24;
  const surfaceD = floorDepth + 24;

  return (
    <group position={[0, 0, -floorDepth / 2 + 2]}>
      {/* Floor plane — soft cool surface, lightly polished. */}
      <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, -0.01, floorDepth / 2 - 2]} receiveShadow>
        <planeGeometry args={[surfaceW, surfaceD]} />
        <meshStandardMaterial color="#cfd6e4" metalness={0.35} roughness={0.65} envMapIntensity={0.6} />
      </mesh>

      {/* Subtle grid over the rack area. */}
      <gridHelper
        args={[floorWidth, 20, "#aab4c8", "#d3dae7"]}
        position={[0, 0.012, floorDepth / 2 - 2]}
      />

      {rows.map((row, index) => (
        <RackRow key={row.row_id} row={row} rowIndex={index} onHover={onHover} onUnhover={onUnhover} />
      ))}
    </group>
  );
}
