"use client";

import { useState } from "react";
import { RoundedBox, Edges, Html } from "@react-three/drei";
import {
  Building,
  Suite,
  LoadStatus,
  Rollup,
  getLoadColor,
  getBuildingRollup,
  getBuildingChildStatuses,
  getSuiteRollup,
  getSuiteChildStatuses,
} from "@/lib/types";
import {
  BLOCK_W,
  BLOCK_D,
  BUILDING_BLOCK,
  FLOOR_H,
  STACK_BASE,
  DC_FLOOR_PITCH,
  BUILDING_FLOOR_PITCH,
  stackHeight,
} from "@/lib/scene-layout";

const ROW_GLOW: Record<LoadStatus, number> = { healthy: 0.85, warning: 1.8, critical: 3.6 };

// Glowing status-coloured frame around the edge of a floor plate — the
// at-a-glance health cue, and the thing that gives a plate definition.
function StatusFrame({ width, depth, y, color, intensity }: { width: number; depth: number; y: number; color: string; intensity: number }) {
  const t = 0.08;
  const bars: [number, number, number, number, number][] = [
    [0, y, depth / 2, width, t],
    [0, y, -depth / 2, width, t],
    [-width / 2, y, 0, t, depth],
    [width / 2, y, 0, t, depth],
  ];
  return (
    <>
      {bars.map(([x, by, z, w, d], i) => (
        <mesh key={i} position={[x, by, z]}>
          <boxGeometry args={[w, 0.08, d]} />
          <meshStandardMaterial color={color} emissive={color} emissiveIntensity={intensity} toneMapped={false} />
        </mesh>
      ))}
    </>
  );
}

// One suite, drawn as a floor plate: dark slab + glowing status rim, with its
// rows shown as glowing strips on top when `rowStatuses` is supplied.
function SuiteFloorPlate({
  y,
  width,
  depth,
  status,
  hovered,
  rowStatuses,
}: {
  y: number; // centre of the plate
  width: number;
  depth: number;
  status: LoadStatus;
  hovered: boolean;
  rowStatuses?: LoadStatus[];
}) {
  const statusColor = getLoadColor(status);
  const topY = y + FLOOR_H / 2;
  const n = rowStatuses?.length ?? 0;
  const usable = depth * 0.78;

  return (
    <group>
      {/* Dark glass-smoked floor plate. */}
      <RoundedBox args={[width, FLOOR_H, depth]} radius={0.05} smoothness={3} position={[0, y, 0]} castShadow receiveShadow>
        <meshPhysicalMaterial color="#11151f" metalness={0.35} roughness={0.25} reflectivity={0.5} envMapIntensity={1.1} />
      </RoundedBox>

      <StatusFrame width={width} depth={depth} y={topY} color={statusColor} intensity={hovered ? 4.2 : 2.6} />

      {/* Rows as glowing strips running across the plate. */}
      {rowStatuses?.map((rs, i) => {
        const z = n > 1 ? (i / (n - 1) - 0.5) * usable : 0;
        const stripDepth = Math.max(0.12, (usable / n) * 0.6);
        const color = getLoadColor(rs);
        return (
          <mesh key={i} position={[0, topY + 0.08, z]}>
            <boxGeometry args={[width * 0.82, 0.14, stripDepth]} />
            <meshStandardMaterial color={color} emissive={color} emissiveIntensity={ROW_GLOW[rs]} toneMapped={false} />
          </mesh>
        );
      })}
    </group>
  );
}

// Four dark corner posts tying the floor plates into a "building" silhouette.
function CornerPosts({ width, depth, height }: { width: number; depth: number; height: number }) {
  const t = 0.14;
  const x = width / 2 - t / 2;
  const z = depth / 2 - t / 2;
  const corners: [number, number][] = [
    [x, z],
    [-x, z],
    [x, -z],
    [-x, -z],
  ];
  return (
    <>
      {corners.map(([cx, cz], i) => (
        <mesh key={i} position={[cx, height / 2, cz]} castShadow>
          <boxGeometry args={[t, height, t]} />
          <meshStandardMaterial color="#0d111a" metalness={0.5} roughness={0.5} />
        </mesh>
      ))}
    </>
  );
}

function LabelChip({ position, label, rollup, distanceFactor }: { position: [number, number, number]; label: string; rollup: Rollup; distanceFactor: number }) {
  return (
    <Html position={position} center distanceFactor={distanceFactor} style={{ pointerEvents: "none" }} zIndexRange={[50, 0]}>
      <div className="bg-white/90 backdrop-blur-sm rounded-lg px-3 py-1.5 shadow-md border border-white/30 whitespace-nowrap text-center">
        <div className="text-sm font-semibold text-gray-900 leading-tight">{label}</div>
        <div className="flex items-center justify-center gap-2 text-[11px] text-gray-500">
          <span>{Math.round(rollup.utilization * 100)}%</span>
          {rollup.counts.critical > 0 && <span className="text-red-600 font-medium">{rollup.counts.critical} hot</span>}
        </div>
      </div>
    </Html>
  );
}

// Soft status pool on the floor + base pad — grounds a stack.
function StackBase({ width, depth, status }: { width: number; depth: number; status: LoadStatus }) {
  const color = getLoadColor(status);
  return (
    <>
      <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, 0.015, 0]}>
        <planeGeometry args={[width * 1.5, depth * 1.5]} />
        <meshBasicMaterial color={color} transparent opacity={status === "healthy" ? 0.05 : 0.12} depthWrite={false} />
      </mesh>
      <RoundedBox args={[width, 0.12, depth]} radius={0.04} smoothness={3} position={[0, 0.06, 0]} receiveShadow>
        <meshStandardMaterial color="#0d111a" metalness={0.5} roughness={0.5} />
      </RoundedBox>
    </>
  );
}

// DATA-CENTRE LEVEL: a building drawn as a compact stack of suite floors.
// Suite count → stack height, so buildings read as an uneven skyline.
export function BuildingStack({ position, building, onSelect }: { position: [number, number, number]; building: Building; onSelect: () => void }) {
  const [hovered, setHovered] = useState(false);
  const rollup = getBuildingRollup(building);
  const suiteStatuses = getBuildingChildStatuses(building);
  const k = suiteStatuses.length;
  const top = stackHeight(k, DC_FLOOR_PITCH);

  return (
    <group position={position} scale={hovered ? 1.03 : 1}>
      <StackBase width={BLOCK_W} depth={BLOCK_D} status={rollup.status} />
      <CornerPosts width={BLOCK_W} depth={BLOCK_D} height={top} />

      {suiteStatuses.map((s, i) => (
        <SuiteFloorPlate key={i} y={STACK_BASE + i * DC_FLOOR_PITCH + FLOOR_H / 2} width={BLOCK_W} depth={BLOCK_D} status={s} hovered={hovered} />
      ))}

      {/* Invisible hit target spanning the whole building. */}
      <mesh
        position={[0, top / 2, 0]}
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
          onSelect();
        }}
      >
        <boxGeometry args={[BLOCK_W, top, BLOCK_D]} />
        <meshBasicMaterial transparent opacity={0} depthWrite={false} />
      </mesh>

      <LabelChip position={[0, top + 0.7, 0]} label={building.label} rollup={rollup} distanceFactor={24} />
    </group>
  );
}

// BUILDING LEVEL: the same stack, exploded with gaps, each floor clickable and
// showing its rows. Clicking a floor drills into that suite.
function InteractiveFloor({ suite, y, onSelect }: { suite: Suite; y: number; onSelect: () => void }) {
  const [hovered, setHovered] = useState(false);
  const rollup = getSuiteRollup(suite);
  const rowStatuses = getSuiteChildStatuses(suite);

  return (
    <group scale={hovered ? 1.02 : 1}>
      <SuiteFloorPlate y={y} width={BUILDING_BLOCK} depth={BUILDING_BLOCK} status={rollup.status} hovered={hovered} rowStatuses={rowStatuses} />

      <mesh
        position={[0, y + 0.2, 0]}
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
          onSelect();
        }}
      >
        <boxGeometry args={[BUILDING_BLOCK, FLOOR_H + 0.5, BUILDING_BLOCK]} />
        <meshBasicMaterial transparent opacity={0} depthWrite={false} />
      </mesh>

      {/* Tag to the side at this floor's height, so labels don't stack up and
          crowd the view the way centred top-labels would. */}
      <LabelChip position={[-(BUILDING_BLOCK / 2 + 1.6), y, 0]} label={suite.label} rollup={rollup} distanceFactor={20} />
    </group>
  );
}

export function BuildingFloors({ building, onSelectSuite }: { building: Building; onSelectSuite: (suiteId: string) => void }) {
  const rollup = getBuildingRollup(building);
  const k = building.suites.length;
  const top = stackHeight(k, BUILDING_FLOOR_PITCH);

  return (
    <group>
      <StackBase width={BUILDING_BLOCK} depth={BUILDING_BLOCK} status={rollup.status} />
      <CornerPosts width={BUILDING_BLOCK} depth={BUILDING_BLOCK} height={top} />
      {building.suites.map((s, i) => (
        <InteractiveFloor
          key={s.suite_id}
          suite={s}
          y={STACK_BASE + i * BUILDING_FLOOR_PITCH + FLOOR_H / 2}
          onSelect={() => onSelectSuite(s.suite_id)}
        />
      ))}
    </group>
  );
}
