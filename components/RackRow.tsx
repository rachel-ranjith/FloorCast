"use client";

import { useMemo } from "react";
import { ServerRack } from "./ServerRack";
import { Row, getRowLoadStatus } from "@/lib/types";

interface RackRowProps {
  row: Row;
  rowIndex: number;
}

export function RackRow({ row, rowIndex }: RackRowProps) {
  const loadStatus = useMemo(() => getRowLoadStatus(row), [row]);

  // Calculate z position based on row index (with aisle spacing)
  const zOffset = rowIndex * 3.5; // 3.5 units between rows

  return (
    <group position={[0, 0, zOffset]}>
      {row.positions.map((position, posIndex) => {
        // Calculate x position (centered grid)
        const xOffset = (posIndex - row.positions.length / 2 + 0.5) * 1.2;

        return (
          <ServerRack
            key={position.position_id}
            position={position}
            row={row}
            x={xOffset}
            z={0}
            loadStatus={loadStatus}
          />
        );
      })}

      {/* Row label */}
      <mesh position={[-7.5, 0.05, 0]} rotation={[-Math.PI / 2, 0, 0]}>
        <planeGeometry args={[1.5, 0.5]} />
        <meshStandardMaterial color="#64748b" transparent opacity={0.5} />
      </mesh>
    </group>
  );
}
