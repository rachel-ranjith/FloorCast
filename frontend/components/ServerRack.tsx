"use client";

import { useRef, useState, useMemo } from "react";
import { Mesh } from "three";
import { RoundedBox } from "@react-three/drei";
import { Position, Row, getLoadColor, LoadStatus } from "@/lib/types";

// Reported up to FloorVisualization so the hovered rack shows in the side panel.
export interface HoveredRack {
  position: Position;
  row: Row;
}

interface RackProps {
  position: Position;
  row: Row;
  x: number;
  z: number;
  loadStatus: LoadStatus;
  onHover: (info: HoveredRack) => void;
  onUnhover: (positionId: string) => void;
}

// How strongly each status accent emits. Critical pushes well above the
// bloom luminanceThreshold so only hot racks actually glow; healthy/warning
// stay subtle so the scene doesn't wash out.
const ACCENT_INTENSITY: Record<LoadStatus, number> = {
  healthy: 0.9,
  warning: 1.6,
  critical: 3.6,
};

export function ServerRack({ position, row, x, z, loadStatus, onHover, onUnhover }: RackProps) {
  const meshRef = useRef<Mesh>(null);
  const [hovered, setHovered] = useState(false);

  const color = useMemo(() => getLoadColor(loadStatus), [loadStatus]);
  const isCritical = loadStatus === "critical";
  const accentIntensity = ACCENT_INTENSITY[loadStatus];

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
      {/* Dark cabinet body — reads as equipment, not a coloured block. */}
      <RoundedBox
        ref={meshRef}
        args={[width, height, depth]}
        radius={0.04}
        smoothness={4}
        position={[0, height / 2, 0]}
        castShadow
        receiveShadow
        onPointerOver={(e) => {
          e.stopPropagation();
          setHovered(true);
          onHover({ position, row });
          document.body.style.cursor = "pointer";
        }}
        onPointerOut={() => {
          setHovered(false);
          onUnhover(position.position_id);
          document.body.style.cursor = "auto";
        }}
      >
        <meshStandardMaterial
          color={hovered ? "#3a4250" : "#262b33"}
          emissive="#0a0d12"
          emissiveIntensity={0.4}
          metalness={0.55}
          roughness={0.5}
        />
      </RoundedBox>

      {/* Status accent — emissive strip down the front face. This is what
          bloom catches. toneMapped={false} keeps the colour pure & bright so
          the glow reads as the true status hue. */}
      <mesh position={[0, height / 2, depth / 2 + 0.012]}>
        <planeGeometry args={[width * 0.18, height * 0.82]} />
        <meshStandardMaterial
          color={color}
          emissive={color}
          emissiveIntensity={hovered ? accentIntensity + 0.8 : accentIntensity}
          toneMapped={false}
        />
      </mesh>

      {/* Thin glowing cap across the top — extra readability + a little
          extra bloom contribution on hot racks. */}
      <mesh position={[0, height + 0.012, 0]} rotation={[-Math.PI / 2, 0, 0]}>
        <planeGeometry args={[width * 0.7, depth * 0.7]} />
        <meshStandardMaterial
          color={color}
          emissive={color}
          emissiveIntensity={accentIntensity * 0.45}
          toneMapped={false}
        />
      </mesh>

    </group>
  );
}
