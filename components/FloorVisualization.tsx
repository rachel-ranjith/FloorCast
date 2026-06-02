"use client";

import { Suspense, useState, useEffect } from "react";
import { Canvas } from "@react-three/fiber";
import { OrbitControls, Environment, ContactShadows } from "@react-three/drei";
import { EffectComposer, Bloom, Vignette } from "@react-three/postprocessing";
import { DataCentreFloor } from "./DataCentreFloor";
import { FloorData, getRowLoadStatus } from "@/lib/types";
import { generateMockData } from "@/lib/mock-data";

function LoadingFallback() {
  return (
    <mesh>
      <boxGeometry args={[1, 1, 1]} />
      <meshStandardMaterial color="#14b8a6" />
    </mesh>
  );
}

function Scene({ data }: { data: FloorData }) {
  return (
    <>
      {/* Lighting */}
      <ambientLight intensity={0.6} />
      <directionalLight
        position={[10, 20, 10]}
        intensity={1.2}
        castShadow
        shadow-mapSize={[2048, 2048]}
      />
      <directionalLight position={[-10, 15, -10]} intensity={0.4} />

      {/* Environment for reflections and ambient lighting */}
      <Environment preset="city" background={false} />

      {/* Soft contact shadows under racks */}
      <ContactShadows
        position={[0, 0, 0]}
        opacity={0.4}
        scale={40}
        blur={2}
        far={10}
      />

      {/* The data centre floor */}
      <DataCentreFloor data={data} />

      {/* Post-processing effects */}
      <EffectComposer>
        <Bloom
          luminanceThreshold={0.4}
          luminanceSmoothing={0.9}
          intensity={0.8}
          mipmapBlur
        />
        <Vignette offset={0.3} darkness={0.4} />
      </EffectComposer>
    </>
  );
}

function Legend() {
  const items = [
    { color: "bg-teal-500", label: "Healthy (<50%)" },
    { color: "bg-amber-500", label: "Warning (50-80%)" },
    { color: "bg-red-500", label: "Critical (>80%)" },
  ];

  return (
    <div className="absolute bottom-6 left-6 bg-white/90 backdrop-blur-sm rounded-xl p-4 shadow-lg border border-white/20">
      <h4 className="text-xs font-semibold text-gray-600 uppercase tracking-wide mb-3">
        Power Load
      </h4>
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

function Stats({ data }: { data: FloorData }) {
  const allRows = data.buildings.flatMap((b) =>
    b.suites.flatMap((s) => s.rows)
  );

  const totalRacks = allRows.reduce(
    (acc, row) => acc + row.positions.filter((p) => p.occupied).length,
    0
  );
  const totalPositions = allRows.reduce((acc, row) => acc + row.positions.length, 0);
  const criticalRows = allRows.filter((r) => getRowLoadStatus(r) === "critical").length;
  const totalLoad = allRows.reduce((acc, row) => acc + row.load_kw, 0);
  const totalCapacity = allRows.reduce((acc, row) => acc + row.capacity_kw, 0);

  return (
    <div className="absolute top-6 right-6 bg-white/90 backdrop-blur-sm rounded-xl p-4 shadow-lg border border-white/20">
      <h4 className="text-xs font-semibold text-gray-600 uppercase tracking-wide mb-3">
        Floor Summary
      </h4>
      <div className="space-y-2 text-sm">
        <div className="flex justify-between gap-8">
          <span className="text-gray-500">Total Racks</span>
          <span className="font-semibold text-gray-900">{totalRacks} / {totalPositions}</span>
        </div>
        <div className="flex justify-between gap-8">
          <span className="text-gray-500">Rows</span>
          <span className="font-semibold text-gray-900">{allRows.length}</span>
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
            {Math.round((totalLoad / totalCapacity) * 100)}%
          </span>
        </div>
      </div>
    </div>
  );
}

export function FloorVisualization() {
  const [data, setData] = useState<FloorData | null>(null);

  useEffect(() => {
    // Generate mock data on client side
    setData(generateMockData());
  }, []);

  if (!data) {
    return (
      <div className="w-full h-screen flex items-center justify-center bg-gradient-to-br from-slate-100 via-blue-50 to-indigo-100">
        <div className="text-gray-500">Loading visualization...</div>
      </div>
    );
  }

  return (
    <div className="w-full h-screen relative">
      {/* Gradient background */}
      <div className="absolute inset-0 bg-gradient-to-br from-slate-100 via-blue-50 to-indigo-100" />

      {/* 3D Canvas */}
      <Canvas
        camera={{
          position: [15, 18, 25],
          fov: 45,
          near: 0.1,
          far: 1000,
        }}
        shadows
        className="absolute inset-0"
      >
        <Suspense fallback={<LoadingFallback />}>
          <Scene data={data} />
        </Suspense>
        <OrbitControls
          enablePan={true}
          enableZoom={true}
          enableRotate={true}
          minDistance={10}
          maxDistance={60}
          maxPolarAngle={Math.PI / 2.2}
          target={[0, 0, 5]}
        />
      </Canvas>

      {/* UI Overlays */}
      <Legend />
      <Stats data={data} />

      {/* Title */}
      <div className="absolute top-6 left-6">
        <h1 className="text-xl font-semibold text-gray-900">FloorCast</h1>
        <p className="text-sm text-gray-500">Data Centre Floor Visualization</p>
      </div>
    </div>
  );
}
