"use client";

import { RackRow } from "./RackRow";
import { FloorData } from "@/lib/types";

interface DataCentreFloorProps {
  data: FloorData;
}

export function DataCentreFloor({ data }: DataCentreFloorProps) {
  // Get all rows from all buildings/suites
  const allRows = data.buildings.flatMap((building) =>
    building.suites.flatMap((suite) => suite.rows)
  );

  // Calculate floor dimensions
  const floorWidth = 20;
  const floorDepth = allRows.length * 3.5 + 5;

  return (
    <group position={[0, 0, -floorDepth / 2 + 2]}>
      {/* Floor plane */}
      <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, -0.01, floorDepth / 2 - 2]} receiveShadow>
        <planeGeometry args={[floorWidth, floorDepth]} />
        <meshStandardMaterial
          color="#e2e8f0"
          metalness={0.1}
          roughness={0.8}
        />
      </mesh>

      {/* Floor grid lines */}
      <gridHelper
        args={[floorWidth, 20, "#cbd5e1", "#e2e8f0"]}
        position={[0, 0.01, floorDepth / 2 - 2]}
      />

      {/* Render all rows */}
      {allRows.map((row, index) => (
        <RackRow key={row.row_id} row={row} rowIndex={index} />
      ))}
    </group>
  );
}
