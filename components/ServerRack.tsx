"use client";

import { useRef, useState, useMemo } from "react";
import { Mesh } from "three";
import { Html, RoundedBox } from "@react-three/drei";
import { Position, Row, getRowLoadStatus, getLoadColor, LoadStatus } from "@/lib/types";

interface RackProps {
  position: Position;
  row: Row;
  x: number;
  z: number;
  loadStatus: LoadStatus;
}

interface RackInfoCardProps {
  position: Position;
  row: Row;
  onClose: () => void;
}

function RackInfoCard({ position, row, onClose }: RackInfoCardProps) {
  const loadStatus = getRowLoadStatus(row);
  const statusColors = {
    healthy: "bg-teal-500",
    warning: "bg-amber-500",
    critical: "bg-red-500",
  };

  return (
    <div
      className="rack-info-card rounded-xl p-4 min-w-[220px] pointer-events-auto"
      onClick={(e) => e.stopPropagation()}
    >
      <div className="flex items-start justify-between mb-3">
        <div>
          <h3 className="font-semibold text-gray-900 text-sm">
            {position.rack?.rack_type}
          </h3>
          <p className="text-xs text-gray-500">{position.position_id}</p>
        </div>
        <button
          onClick={onClose}
          className="text-gray-400 hover:text-gray-600 transition-colors p-1 -mr-1 -mt-1"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M18 6L6 18M6 6l12 12" />
          </svg>
        </button>
      </div>

      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <span className="text-xs text-gray-500">Power Draw</span>
          <span className="text-sm font-medium text-gray-900">
            {position.rack?.power_draw_kw} kW
          </span>
        </div>
        <div className="flex items-center justify-between">
          <span className="text-xs text-gray-500">Generation</span>
          <span className="text-sm font-medium text-gray-900">
            {position.rack?.generation}
          </span>
        </div>
        <div className="flex items-center justify-between">
          <span className="text-xs text-gray-500">Row</span>
          <span className="text-sm font-medium text-gray-900">{row.label}</span>
        </div>
        <div className="flex items-center justify-between">
          <span className="text-xs text-gray-500">Row Load</span>
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium text-gray-900">
              {Math.round((row.load_kw / row.capacity_kw) * 100)}%
            </span>
            <div className={`w-2 h-2 rounded-full ${statusColors[loadStatus]}`} />
          </div>
        </div>
      </div>
    </div>
  );
}

export function ServerRack({ position, row, x, z, loadStatus }: RackProps) {
  const meshRef = useRef<Mesh>(null);
  const [hovered, setHovered] = useState(false);
  const [selected, setSelected] = useState(false);

  const color = useMemo(() => getLoadColor(loadStatus), [loadStatus]);
  const isCritical = loadStatus === "critical";

  // Rack dimensions
  const baseHeight = 1.8;
  const height = isCritical ? baseHeight * 1.15 : baseHeight;
  const width = 0.6;
  const depth = 1.0;

  if (!position.occupied) {
    // Empty position marker
    return (
      <mesh position={[x, 0.02, z]} rotation={[-Math.PI / 2, 0, 0]}>
        <planeGeometry args={[width * 0.8, depth * 0.8]} />
        <meshStandardMaterial
          color="#cbd5e1"
          transparent
          opacity={0.3}
        />
      </mesh>
    );
  }

  return (
    <group position={[x, 0, z]}>
      <RoundedBox
        ref={meshRef}
        args={[width, height, depth]}
        radius={0.04}
        smoothness={4}
        position={[0, height / 2, 0]}
        onPointerOver={(e) => {
          e.stopPropagation();
          setHovered(true);
          document.body.style.cursor = "pointer";
        }}
        onPointerOut={() => {
          setHovered(false);
          document.body.style.cursor = "auto";
        }}
        onClick={(e) => {
          e.stopPropagation();
          setSelected(!selected);
        }}
      >
        <meshStandardMaterial
          color={hovered ? "#ffffff" : color}
          emissive={color}
          emissiveIntensity={isCritical ? 0.8 : hovered ? 0.4 : 0.2}
          metalness={0.3}
          roughness={0.4}
        />
      </RoundedBox>

      {/* Rack detail lines */}
      <mesh position={[0, height / 2, depth / 2 + 0.001]}>
        <planeGeometry args={[width * 0.9, height * 0.9]} />
        <meshStandardMaterial
          color="#1e293b"
          transparent
          opacity={0.15}
        />
      </mesh>

      {/* Info card when selected */}
      {selected && (
        <Html
          position={[0, height + 0.5, 0]}
          center
          distanceFactor={10}
          style={{ pointerEvents: "auto" }}
        >
          <RackInfoCard
            position={position}
            row={row}
            onClose={() => setSelected(false)}
          />
        </Html>
      )}
    </group>
  );
}
