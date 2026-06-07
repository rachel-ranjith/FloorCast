"use client";

import { Suspense, useState, useEffect, useMemo, useRef } from "react";
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { Vector3 } from "three";
import { OrbitControls, Environment, ContactShadows } from "@react-three/drei";
import { EffectComposer, Bloom, Vignette } from "@react-three/postprocessing";
import { BuildingStack, BuildingFloors } from "./blocks";
import { SuiteFloor } from "./SuiteFloor";
import { HoveredRack } from "./ServerRack";
import { FloorData, Row, getRowLoadStatus } from "@/lib/types";
import {
  packGrid,
  BLOCK_CELL,
  BUILDING_BLOCK,
  DC_FLOOR_PITCH,
  BUILDING_FLOOR_PITCH,
  stackHeight,
} from "@/lib/scene-layout";
import { generateMockData } from "@/lib/mock-data";

// --- Navigation ----------------------------------------------------------
type View =
  | { level: "datacentre" }
  | { level: "building"; buildingId: string }
  | { level: "suite"; buildingId: string; suiteId: string };

function viewKey(v: View): string {
  return `${v.level}|${"buildingId" in v ? v.buildingId : ""}|${"suiteId" in v ? v.suiteId : ""}`;
}

// --- Camera framing ------------------------------------------------------
const CAM_DIR = new Vector3(15, 18, 20).normalize(); // fixed 3/4 direction

interface Focus {
  camPos: [number, number, number];
  lookAt: [number, number, number];
  minDistance: number;
  maxDistance: number;
}

function computeFocus(view: View, data: FloorData): Focus {
  if (view.level === "suite") {
    const building = data.buildings.find((b) => b.building_id === view.buildingId);
    const suite = building?.suites.find((s) => s.suite_id === view.suiteId);
    const rowCount = suite?.rows.length ?? 0;
    const floorDepth = rowCount * 3.5 + 5;
    const span = Math.max(20, floorDepth);
    const maxDistance = span * 1.05;
    const minDistance = Math.min(10, maxDistance * 0.55);
    const camDist = maxDistance * 0.88;
    const lookAt: [number, number, number] = [0, 0, -2.25]; // rack content centre
    return {
      camPos: [CAM_DIR.x * camDist, CAM_DIR.y * camDist, lookAt[2] + CAM_DIR.z * camDist],
      lookAt,
      minDistance,
      maxDistance,
    };
  }

  if (view.level === "building") {
    // A single building: one centred tower of suite floors.
    const building = data.buildings.find((b) => b.building_id === view.buildingId);
    const k = building?.suites.length ?? 1;
    const top = stackHeight(k, BUILDING_FLOOR_PITCH);
    const span = Math.max(BUILDING_BLOCK * 1.4, top * 1.2);
    const frame = span * 1.05 + 6;
    const lookAt: [number, number, number] = [0, top * 0.5, 0];
    return {
      camPos: [CAM_DIR.x * frame, lookAt[1] + CAM_DIR.y * frame, CAM_DIR.z * frame],
      lookAt,
      minDistance: Math.max(6, frame * 0.4),
      maxDistance: frame * 1.7,
    };
  }

  // Data-centre level — grid of building towers centred at origin.
  const n = data.buildings.length;
  const maxSuites = Math.max(1, ...data.buildings.map((b) => b.suites.length));
  const maxTop = stackHeight(maxSuites, DC_FLOOR_PITCH);
  const { width, depth } = packGrid(n);
  const span = Math.max(width, depth, BLOCK_CELL, maxTop * 1.3);
  const frame = span * 1.15 + 6;
  const lookAt: [number, number, number] = [0, maxTop * 0.45, 0];
  return {
    camPos: [CAM_DIR.x * frame, lookAt[1] + CAM_DIR.y * frame, CAM_DIR.z * frame],
    lookAt,
    minDistance: Math.max(6, frame * 0.4),
    maxDistance: frame * 1.7,
  };
}

// Eases the camera (and orbit target) to the focus whenever the view changes,
// then hands control back to OrbitControls so manual orbit/zoom isn't fought.
function CameraRig({ focus, animKey }: { focus: Focus; animKey: string }) {
  const { camera, controls } = useThree();
  const animating = useRef(true);
  const dest = useMemo(
    () => ({ pos: new Vector3(...focus.camPos), look: new Vector3(...focus.lookAt) }),
    [focus]
  );

  useEffect(() => {
    animating.current = true;
  }, [animKey]);

  useFrame(() => {
    if (!animating.current) return;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const orbit = controls as any;
    camera.position.lerp(dest.pos, 0.1);
    if (orbit?.target) {
      orbit.target.lerp(dest.look, 0.1);
      orbit.update?.();
    }
    if (camera.position.distanceTo(dest.pos) < 0.15) {
      camera.position.copy(dest.pos);
      orbit?.target?.copy(dest.look);
      orbit?.update?.();
      animating.current = false;
    }
  });

  return null;
}

// --- Scene ---------------------------------------------------------------
function Scene({
  view,
  data,
  onSelectBuilding,
  onSelectSuite,
  onHover,
  onUnhover,
}: {
  view: View;
  data: FloorData;
  onSelectBuilding: (id: string) => void;
  onSelectSuite: (id: string) => void;
  onHover: (info: HoveredRack) => void;
  onUnhover: (positionId: string) => void;
}) {
  const building =
    view.level !== "datacentre"
      ? data.buildings.find((b) => b.building_id === view.buildingId)
      : undefined;
  const suite =
    view.level === "suite" ? building?.suites.find((s) => s.suite_id === view.suiteId) : undefined;

  return (
    <>
      {/* Soft pastel atmosphere. */}
      <fog attach="fog" args={["#e7e9f6", 40, 140]} />
      <ambientLight intensity={0.35} />
      <directionalLight
        position={[14, 22, 12]}
        intensity={1.5}
        castShadow
        shadow-mapSize={[2048, 2048]}
        shadow-bias={-0.0004}
        shadow-normalBias={0.02}
        shadow-camera-near={1}
        shadow-camera-far={140}
        shadow-camera-left={-40}
        shadow-camera-right={40}
        shadow-camera-top={40}
        shadow-camera-bottom={-40}
      />
      <directionalLight position={[-12, 14, -8]} intensity={0.3} color="#cdd8ff" />
      <Environment preset="city" background={false} />

      {/* Ground for the container levels (the suite level draws its own floor). */}
      {view.level !== "suite" && (
        <>
          <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, -0.02, 0]} receiveShadow>
            <planeGeometry args={[400, 400]} />
            <meshStandardMaterial color="#cfd6e4" metalness={0.3} roughness={0.7} envMapIntensity={0.6} />
          </mesh>
          <gridHelper args={[80, 32, "#aab4c8", "#c4ccdb"]} position={[0, 0.005, 0]} />
        </>
      )}

      <ContactShadows position={[0, 0.005, 0]} opacity={0.6} scale={120} blur={2.4} far={14} resolution={1024} color="#1c2436" />

      {/* Level content */}
      {view.level === "datacentre" &&
        (() => {
          const { positions } = packGrid(data.buildings.length);
          return data.buildings.map((b, i) => (
            <BuildingStack
              key={b.building_id}
              position={[positions[i][0], 0, positions[i][1]]}
              building={b}
              onSelect={() => onSelectBuilding(b.building_id)}
            />
          ));
        })()}

      {view.level === "building" && building && (
        <BuildingFloors building={building} onSelectSuite={onSelectSuite} />
      )}

      {view.level === "suite" && suite && (
        <SuiteFloor rows={suite.rows} onHover={onHover} onUnhover={onUnhover} />
      )}

      {/* Bloom only catches the bright status accents / hot markers. */}
      <EffectComposer>
        <Bloom luminanceThreshold={0.85} luminanceSmoothing={0.3} intensity={1.1} radius={0.75} mipmapBlur />
        <Vignette offset={0.35} darkness={0.45} />
      </EffectComposer>
    </>
  );
}

function LoadingFallback() {
  return (
    <mesh>
      <boxGeometry args={[1, 1, 1]} />
      <meshStandardMaterial color="#14b8a6" />
    </mesh>
  );
}

// --- Overlays ------------------------------------------------------------
function Legend() {
  const items = [
    { color: "bg-teal-500", label: "Healthy (<50%)" },
    { color: "bg-amber-500", label: "Warning (50-80%)" },
    { color: "bg-red-500", label: "Critical (>80%)" },
  ];
  return (
    <div className="bg-white/90 backdrop-blur-sm rounded-xl p-4 shadow-lg border border-white/20">
      <h4 className="text-xs font-semibold text-gray-600 uppercase tracking-wide mb-3">Power Load</h4>
      <div className="space-y-2">
        {items.map((item) => (
          <div key={item.label} className="flex items-center gap-3">
            <div className={`w-3 h-3 rounded-full ${item.color}`} />
            <span className="text-sm text-gray-700">{item.label}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function Stats({ rows, title }: { rows: Row[]; title: string }) {
  const totalRacks = rows.reduce((acc, row) => acc + row.positions.filter((p) => p.occupied).length, 0);
  const totalPositions = rows.reduce((acc, row) => acc + row.positions.length, 0);
  const criticalRows = rows.filter((r) => getRowLoadStatus(r) === "critical").length;
  const totalLoad = rows.reduce((acc, row) => acc + row.load_kw, 0);
  const totalCapacity = rows.reduce((acc, row) => acc + row.capacity_kw, 0);

  return (
    <div className="bg-white/90 backdrop-blur-sm rounded-xl p-4 shadow-lg border border-white/20">
      <h4 className="text-xs font-semibold text-gray-600 uppercase tracking-wide mb-3">{title}</h4>
      <div className="space-y-2 text-sm">
        <div className="flex justify-between gap-8">
          <span className="text-gray-500">Total Racks</span>
          <span className="font-semibold text-gray-900">{totalRacks} / {totalPositions}</span>
        </div>
        <div className="flex justify-between gap-8">
          <span className="text-gray-500">Rows</span>
          <span className="font-semibold text-gray-900">{rows.length}</span>
        </div>
        <div className="flex justify-between gap-8">
          <span className="text-gray-500">Critical Rows</span>
          <span className="font-semibold text-red-600">{criticalRows}</span>
        </div>
        <div className="flex justify-between gap-8">
          <span className="text-gray-500">Total Load</span>
          <span className="font-semibold text-gray-900">{Math.round(totalLoad)} kW</span>
        </div>
        <div className="flex justify-between gap-8">
          <span className="text-gray-500">Utilization</span>
          <span className="font-semibold text-gray-900">
            {totalCapacity > 0 ? Math.round((totalLoad / totalCapacity) * 100) : 0}%
          </span>
        </div>
      </div>
    </div>
  );
}

function RackCard({ position, row }: HoveredRack) {
  const loadStatus = getRowLoadStatus(row);
  const statusColors = { healthy: "bg-teal-500", warning: "bg-amber-500", critical: "bg-red-500" };
  return (
    <div className="bg-white/90 backdrop-blur-sm rounded-xl p-4 shadow-lg border border-white/20">
      <h4 className="text-xs font-semibold text-gray-600 uppercase tracking-wide mb-2">Rack Detail</h4>
      <div className="mb-3">
        <p className="font-semibold text-gray-900 text-sm">{position.rack?.rack_type}</p>
        <p className="text-xs text-gray-500">{position.position_id}</p>
      </div>
      <div className="space-y-2 text-sm">
        <div className="flex justify-between gap-8">
          <span className="text-gray-500">Power Draw</span>
          <span className="font-semibold text-gray-900">{position.rack?.power_draw_kw} kW</span>
        </div>
        <div className="flex justify-between gap-8">
          <span className="text-gray-500">Generation</span>
          <span className="font-semibold text-gray-900">{position.rack?.generation}</span>
        </div>
        <div className="flex justify-between gap-8">
          <span className="text-gray-500">Row</span>
          <span className="font-semibold text-gray-900">{row.label}</span>
        </div>
        <div className="flex justify-between gap-8">
          <span className="text-gray-500">Row Load</span>
          <div className="flex items-center gap-2">
            <span className="font-semibold text-gray-900">
              {Math.round((row.load_kw / row.capacity_kw) * 100)}%
            </span>
            <div className={`w-2 h-2 rounded-full ${statusColors[loadStatus]}`} />
          </div>
        </div>
      </div>
    </div>
  );
}

function Breadcrumb({
  view,
  data,
  onNavigate,
}: {
  view: View;
  data: FloorData;
  onNavigate: (v: View) => void;
}) {
  const building = "buildingId" in view ? data.buildings.find((b) => b.building_id === view.buildingId) : undefined;
  const suite =
    view.level === "suite" ? building?.suites.find((s) => s.suite_id === view.suiteId) : undefined;

  const segments: { label: string; target: View }[] = [{ label: "Data Centre", target: { level: "datacentre" } }];
  if (building) segments.push({ label: building.label, target: { level: "building", buildingId: building.building_id } });
  if (suite && building)
    segments.push({ label: suite.label, target: { level: "suite", buildingId: building.building_id, suiteId: suite.suite_id } });

  return (
    <div className="flex items-center gap-1.5 mt-3">
      {segments.map((seg, i) => {
        const isLast = i === segments.length - 1;
        return (
          <div key={i} className="flex items-center gap-1.5">
            {i > 0 && <span className="text-gray-400 text-sm">/</span>}
            <button
              onClick={() => !isLast && onNavigate(seg.target)}
              disabled={isLast}
              className={`text-sm px-2 py-0.5 rounded-md transition-colors ${
                isLast
                  ? "font-semibold text-gray-900 cursor-default"
                  : "text-gray-500 hover:text-gray-900 hover:bg-white/60"
              }`}
            >
              {seg.label}
            </button>
          </div>
        );
      })}
    </div>
  );
}

// --- Root ----------------------------------------------------------------
export function FloorVisualization() {
  const [data, setData] = useState<FloorData | null>(null);
  const [view, setView] = useState<View>({ level: "datacentre" });
  const [hovered, setHovered] = useState<HoveredRack | null>(null);

  useEffect(() => {
    setData(generateMockData());
  }, []);

  // Clear any rack hover when leaving the suite level.
  useEffect(() => {
    if (view.level !== "suite") setHovered(null);
  }, [view.level]);

  const handleHover = (info: HoveredRack) => setHovered(info);
  const handleUnhover = (positionId: string) =>
    setHovered((curr) => (curr?.position.position_id === positionId ? null : curr));

  const focus = useMemo(() => (data ? computeFocus(view, data) : null), [view, data]);

  // Rows in scope for the summary panel + a title for it.
  const scope = useMemo(() => {
    if (!data) return { rows: [] as Row[], title: "Floor Summary" };
    if (view.level === "datacentre") {
      return {
        rows: data.buildings.flatMap((b) => b.suites.flatMap((s) => s.rows)),
        title: "Data Centre Summary",
      };
    }
    const building = data.buildings.find((b) => b.building_id === view.buildingId);
    if (view.level === "building") {
      return { rows: building?.suites.flatMap((s) => s.rows) ?? [], title: `${building?.label} Summary` };
    }
    const suite = building?.suites.find((s) => s.suite_id === view.suiteId);
    return { rows: suite?.rows ?? [], title: `${suite?.label} Summary` };
  }, [view, data]);

  if (!data || !focus) {
    return (
      <div className="w-full h-screen flex items-center justify-center bg-gradient-to-br from-sky-100 via-indigo-50 to-violet-100">
        <div className="text-gray-500">Loading visualization...</div>
      </div>
    );
  }

  return (
    <div className="w-full h-screen relative">
      <div className="absolute inset-0 bg-gradient-to-br from-sky-100 via-indigo-50 to-violet-100" />

      <Canvas
        camera={{ position: focus.camPos, fov: 45, near: 0.1, far: 1000 }}
        shadows
        className="absolute inset-0"
      >
        <Suspense fallback={<LoadingFallback />}>
          <Scene
            view={view}
            data={data}
            onSelectBuilding={(buildingId) => setView({ level: "building", buildingId })}
            onSelectSuite={(suiteId) =>
              setView((v) => ("buildingId" in v ? { level: "suite", buildingId: v.buildingId, suiteId } : v))
            }
            onHover={handleHover}
            onUnhover={handleUnhover}
          />
        </Suspense>
        <CameraRig focus={focus} animKey={viewKey(view)} />
        <OrbitControls
          makeDefault
          enablePan={false}
          enableZoom
          enableRotate
          enableDamping
          dampingFactor={0.08}
          minDistance={focus.minDistance}
          maxDistance={focus.maxDistance}
          minPolarAngle={Math.PI / 6}
          maxPolarAngle={Math.PI / 2.5}
        />
      </Canvas>

      {/* Right-hand info column. */}
      <div className="absolute top-6 right-6 w-[240px] flex flex-col gap-3">
        <Legend />
        <Stats rows={scope.rows} title={scope.title} />
        {hovered && <RackCard position={hovered.position} row={hovered.row} />}
      </div>

      {/* Title + breadcrumb. */}
      <div className="absolute top-6 left-6">
        <h1 className="text-xl font-semibold text-gray-900">FloorCast</h1>
        <p className="text-sm text-gray-500">Data Centre Floor Visualization</p>
        <Breadcrumb view={view} data={data} onNavigate={setView} />
      </div>
    </div>
  );
}
